"""Package / batch window (Phase 3): Batch Preview, Dry Run, Run Batch, Validate, Commit Successful.

Every step is explicit. *Analyze Package* and *Dry Run* write only into the tool's
``reports/packages`` folder. *Run Batch* runs Outfit Studio and validates each project
in the batch workspace, but never writes to the output mod. Only *Commit Successful*
writes, and only the projects whose validation passed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, QThread, QUrl, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
)

from ..app.controller import AppController
from ..profiles.store import Profile
from .main_window import ACTION_COLORS, Worker

__all__ = ["PackageDialog", "COLUMNS"]

COLUMNS = ["Sel", "Project", "Body Family", "Shapes", "Regions", "3BBB", "Confidence", "Action", "Status", "Reason"]


class PackageDialog(QDialog):
    def __init__(self, ctl: AppController, profile: Profile, parent=None, package: str = ""):
        super().__init__(parent)
        self.ctl = ctl
        self.profile = profile
        self.preview = None
        self.batch_dir: Optional[Path] = None
        self.summary: Optional[dict] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker] = None
        self._filling = False
        self.setWindowTitle("Convert Package (Phase 3)")
        self.resize(1250, 820)
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Package (MO2 mod)"))
        self.package = QComboBox()
        self.package.setEditable(True)
        self.package.addItems(ctl.package_names())
        if package:
            self.package.setCurrentText(package)
        row.addWidget(self.package, 1)
        self.analyze_btn = QPushButton("Analyze Package")
        self.analyze_btn.setToolTip("Batch Preview: classify and plan every project of the package (writes nothing to the output mod)")
        self.analyze_btn.clicked.connect(self._analyze)
        row.addWidget(self.analyze_btn)
        lay.addLayout(row)
        self.body_opt = QCheckBox("Allow safe embedded CBBE → CBBE-3BBB body replacement")
        self.body_opt.setChecked(False)
        self.body_opt.setToolTip("Phase 3.1 (opt-in): outfits that build an embedded CBBE body can be converted when the body "
                                 "is positively identified as the installed CBBE body. Such projects are never selected by "
                                 "default. Changing this option requires Analyze Package again.")
        self.body_opt.toggled.connect(self._option_changed)
        lay.addWidget(self.body_opt)
        self.direct_opt = QCheckBox("Also analyze the plugin's game meshes (Direct NIF, read-only)")
        self.direct_opt.setChecked(False)
        self.direct_opt.setToolTip("0.4.0 (opt-in): for packages that ship built meshes but no BodySlide projects. "
                                   "Only analyzes and writes reports; nothing is converted.")
        self.direct_opt.toggled.connect(self._option_changed)
        lay.addWidget(self.direct_opt)
        self.summary_lbl = QLabel("Choose a package and press Analyze Package.")
        self.summary_lbl.setWordWrap(True)
        lay.addWidget(self.summary_lbl)

        split = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(lambda r, *_: self._show_details(r))
        self.table.itemChanged.connect(self._item_changed)
        split.addWidget(self.table)
        self.tabs = QTabWidget()
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.details = QPlainTextEdit()
        self.script = QPlainTextEdit()
        self.results = QPlainTextEdit()
        for w in (self.details, self.script, self.results):
            w.setReadOnly(True)
            w.setFont(mono)
        self.tabs.addTab(self.details, "Project details")
        self.tabs.addTab(self.script, "Dry Run / batch script")
        self.tabs.addTab(self.results, "Batch result")
        split.addWidget(self.tabs)
        split.setSizes([420, 360])
        lay.addWidget(split, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        lay.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        self.dry_btn = QPushButton("Dry Run")
        self.dry_btn.clicked.connect(self._dry_run)
        self.run_btn = QPushButton("Run Batch")
        self.run_btn.clicked.connect(self._run_batch)
        self.validate_btn = QPushButton("Validate")
        self.validate_btn.clicked.connect(self._validate)
        self.commit_btn = QPushButton("Commit Successful")
        self.commit_btn.clicked.connect(self._commit)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.ctl.cancel)
        self.report_btn = QPushButton("Open preview report")
        self.report_btn.clicked.connect(lambda: self._open(self.ctl.preview_paths.get("html")))
        self.ws_btn = QPushButton("Open batch workspace")
        self.ws_btn.clicked.connect(lambda: self._open(self.batch_dir))
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        for b in (self.dry_btn, self.run_btn, self.validate_btn, self.commit_btn, self.cancel_btn, self.report_btn, self.ws_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(close)
        lay.addLayout(btns)
        self._update_buttons()

    # ------------------------------------------------------------------ helpers
    def _open(self, path) -> None:
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _update_buttons(self, busy: bool = False) -> None:
        has_prev = self.preview is not None
        selected = bool(has_prev and self.preview.selected())
        counts = (self.summary or {}).get("counts", {})
        self.analyze_btn.setEnabled(not busy)
        self.dry_btn.setEnabled(not busy and has_prev)
        self.run_btn.setEnabled(not busy and selected and self.batch_dir is None)
        self.validate_btn.setEnabled(not busy and self.batch_dir is not None)
        self.commit_btn.setEnabled(not busy and self.batch_dir is not None and counts.get("VALIDATED", 0) > 0)
        self.cancel_btn.setEnabled(busy)
        self.report_btn.setEnabled(bool(self.ctl.preview_paths.get("html")))
        self.ws_btn.setEnabled(self.batch_dir is not None)

    def _run(self, fn, done, msg: str) -> None:
        if self._thread is not None:
            return
        self.status.setText(msg)
        self.progress.setRange(0, 0)
        thread = QThread(self)
        worker = Worker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(done)
        worker.failed.connect(self._failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._thread_done)
        self._thread, self._worker = thread, worker
        self._update_buttons(busy=True)
        thread.start()

    @Slot(int, int, str)
    def _on_progress(self, done: int, total: int, msg: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.status.setText(msg)

    @Slot()
    def _thread_done(self) -> None:
        for o in (self._thread, self._worker):
            if o is not None:
                o.deleteLater()
        self._thread = self._worker = None
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._update_buttons()

    @Slot(str)
    def _failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Error", msg)
        self.status.setText("Failed — see logs/app.log")

    # ------------------------------------------------------------------ preview
    def _analyze(self) -> None:
        pkg = self.package.currentText().strip()
        if not pkg:
            return
        self.batch_dir, self.summary = None, None
        prof = self.profile
        allow = self.body_opt.isChecked()
        direct = self.direct_opt.isChecked()

        def job(progress):
            progress(0, 0, "Detecting installation…")
            self.ctl.detect(prof)
            return self.ctl.analyze_package(pkg, output_mod=prof.conversion.output_mod_path or None, progress=progress,
                                            allow_body_replacement=allow, allow_direct_nif=direct)

        self._run(job, self._analyzed, f"Analyzing package {pkg}…")

    @Slot(object)
    def _analyzed(self, prev: Any) -> None:
        self.preview = prev
        self._fill()
        dr = getattr(self.ctl, "direct_report", None)
        if dr is not None:  # Direct NIF: read-only analysis, shown as text (no conversion button in 0.4.0)
            from ..conversion.direct_nif import direct_report_text

            self.details.setPlainText(direct_report_text(dr, details=True))
            self.tabs.setCurrentWidget(self.details)
        self.status.setText("Batch Preview ready — nothing was converted or written to the output mod. "
                            f"Report: {self.ctl.preview_paths.get('html', '')}")
        self._update_buttons()

    def _fill(self) -> None:
        prev = self.preview
        c = prev.counts
        self.summary_lbl.setText(
            f"<b>{prev.package}</b> — total {len(prev.projects)} · "
            + " · ".join(f"{a} {c.get(a, 0)}" for a in ("CONVERT", "KEEP", "REVIEW", "SKIP", "UNSUPPORTED", "ERROR"))
            + f" · <b>selected {len(prev.selected())}</b>"
            + (" · embedded-body replacement <b>ON</b>" if prev.allow_body_replacement else "")
            + f" · source mode <b>{getattr(prev, 'source_mode', 'BODYSLIDE_PROJECT')}</b>"
            + "<br>" + "<br>".join(prev.notes))
        self._filling = True
        self.table.setRowCount(len(prev.projects))
        for r, p in enumerate(prev.projects):
            sel = QTableWidgetItem("")
            if p.selectable:
                sel.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                sel.setCheckState(Qt.Checked if p.selected else Qt.Unchecked)
            else:
                sel.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(r, 0, sel)
            vals = [p.project, p.family, str(len(p.shapes)), ", ".join(x.lower() for x in p.regions), p.existing_3bbb,
                    p.confidence, p.action, p.status, p.reasons[0] if p.reasons else ""]
            for ci, v in enumerate(vals, 1):
                it = QTableWidgetItem(v)
                if ci == 7:
                    it.setForeground(QColor(ACTION_COLORS.get(p.action, "#000000")))
                self.table.setItem(r, ci, it)
        self._filling = False
        self.table.resizeColumnsToContents()

    def _option_changed(self, *_: Any) -> None:
        if self.preview is not None and (self.preview.allow_body_replacement != self.body_opt.isChecked()
                                         or bool(getattr(self.preview, "direct_nif", None)) != self.direct_opt.isChecked()):
            self.preview = None  # the preview was made with the other setting: analyze again
            self.table.setRowCount(0)
            self.summary_lbl.setText("An analysis option changed: press Analyze Package again.")
        self._update_buttons()

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item.column() != 0 or self.preview is None:
            return
        p = self.preview.projects[item.row()]
        if p.selectable:
            p.selected = item.checkState() == Qt.Checked
        self.summary_lbl.setText(self.summary_lbl.text().split(" · <b>selected")[0]
                                 + f" · <b>selected {len(self.preview.selected())}</b>")
        self._update_buttons()

    def _show_details(self, row: int) -> None:
        from ..conversion.package import project_detail_lines

        if self.preview is None or not (0 <= row < len(self.preview.projects)):
            return
        self.details.setPlainText("\n".join(project_detail_lines(self.preview.projects[row])))
        self.tabs.setCurrentWidget(self.details)

    # ------------------------------------------------------------------ dry run / run / validate / commit
    def _dry_run(self) -> None:
        if self.preview is None:
            return
        problems, scripts = self.ctl.plan_batch(self.preview)
        from ..conversion.package import preview_text

        text = preview_text(self.preview, detail=False)
        for sess in self.preview.dry_run:
            text += (f"\n\nOutfit Studio session {sess['session']} — script {sess['script']} "
                     f"({', '.join(sess['projects'])}):\n\n" + scripts.get(sess["script"], ""))
        self.script.setPlainText(text)
        self.tabs.setCurrentWidget(self.script)
        self.status.setText("Dry Run complete — nothing was converted or written to the output mod. "
                            f"Scripts and report: {self.ctl.preview_paths.get('scripts', self.ctl.preview_paths.get('html', ''))}"
                            + (" — " + "; ".join(problems) if problems else ""))
        self._update_buttons()

    def _run_batch(self) -> None:
        if self.preview is None or not self.preview.selected():
            return
        names = "\n".join(f"  • {p.project}" for p in self.preview.selected())
        interactive = self.preview.run_mode == "interactive"
        ok = QMessageBox.question(
            self, "Run Batch",
            f"Convert {len(self.preview.selected())} project(s):\n\n{names}\n\n"
            + ("Outfit Studio will open. Run the prepared batch script once (File > Open Automation…, Execute).\n\n"
               if interactive else "")
            + "Results stay in the batch workspace; nothing is written to the output mod until you press "
              "Commit Successful. Continue?")
        if ok != QMessageBox.Yes:
            return
        prev = self.preview

        def job(progress):
            return self.ctl.convert_batch(prev, progress=lambda m: progress(0, 0, m))

        self._run(job, self._ran, "Preparing the batch…")

    @Slot(object)
    def _ran(self, out: Any) -> None:
        self.batch_dir, self.summary = out
        self._show_summary("Batch finished")

    def _show_summary(self, what: str) -> None:
        from ..conversion.batch import summary_text

        s = self.summary or {}
        self.results.setPlainText(summary_text(s) + f"\nWorkspace: {self.batch_dir}\n")
        self.tabs.setCurrentWidget(self.results)
        c = s.get("counts", {})
        self.status.setText(f"{what}: validated {c.get('VALIDATED', 0)} · review {c.get('REVIEW', 0)} · "
                            f"failed {c.get('FAILED', 0)} · committed {c.get('COMMITTED', 0)}")
        self._update_buttons()

    def _validate(self) -> None:
        if self.batch_dir is None:
            return
        b = self.batch_dir

        def job(progress):
            return self.ctl.validate_batch(b, progress=lambda m: progress(0, 0, m))

        self._run(job, lambda s: self._set_summary(s, "Validation finished"), "Validating…")

    def _set_summary(self, s: dict, what: str) -> None:
        self.summary = s
        self._show_summary(what)

    def _commit(self) -> None:
        if self.batch_dir is None or not self.summary:
            return
        ok_list = [p["project"] for p in self.summary["projects"] if p["status"] == "VALIDATED"]
        ok = QMessageBox.question(
            self, "Commit Successful",
            "Write these VALIDATED projects into the output mod?\n\n" + "\n".join(f"  • {n}" for n in ok_list)
            + "\n\nREVIEW and FAILED projects are not written.")
        if ok != QMessageBox.Yes:
            return
        b = self.batch_dir

        def job(progress):
            return self.ctl.commit_batch(b, progress=lambda m: progress(0, 0, m))

        self._run(job, lambda s: self._set_summary(s, "Commit finished"), "Committing…")
