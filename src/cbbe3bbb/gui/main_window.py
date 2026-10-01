"""PySide6 GUI: a thin layer over :class:`cbbe3bbb.app.controller.AppController`.

Long operations (detection, analysis) run in a QThread worker so the window
stays responsive; the Cancel button sets the controller's cancel flag.
"Convert selected…" opens the single-project conversion window (Phase 2);
"Convert Package…" the package/batch window (Phase 3).
"""
from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import PRODUCT_NAME, __version__
from ..analysis.models import ProjectResult
from ..app.controller import AppController, project_details_text
from ..profiles.store import Profile
from ..reports.writer import FILTERS, TABLE_COLUMNS, load_report, matches_filter, row_for

log = logging.getLogger(__name__)

ACTION_COLORS = {
    "CONVERT": "#0b6bcb",
    "KEEP": "#1d8a46",
    "REVIEW": "#b26b00",
    "SKIP": "#777777",
    "UNSUPPORTED": "#7a4bb3",
    "ERROR": "#c62828",
}


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class ResultsModel(QAbstractTableModel):
    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._rows: list[ProjectResult] = []
        self._cells: list[list[str]] = []

    def set_results(self, rows: list[ProjectResult]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._cells = [row_for(r) for r in self._rows]
        self.endResetModel()

    def result(self, row: int) -> Optional[ProjectResult]:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(TABLE_COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return TABLE_COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if role in (Qt.DisplayRole, Qt.EditRole):
            return self._cells[r][c]
        if role == Qt.ToolTipRole:
            pr = self._rows[r]
            return "\n".join(pr.reasons[:6])
        if role == Qt.ForegroundRole and c == len(TABLE_COLUMNS) - 1:
            col = ACTION_COLORS.get(self._rows[r].action)
            return QColor(col) if col else None
        if role == Qt.FontRole and c == len(TABLE_COLUMNS) - 1:
            f = QFont()
            f.setBold(True)
            return f
        return None


class ResultsFilter(QSortFilterProxyModel):
    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._filter = "All"
        self._search = ""
        self.setSortCaseSensitivity(Qt.CaseInsensitive)

    def _change_filter(self, apply) -> None:
        """Change filter state; Qt >= 6.10 API when available, invalidateFilter() otherwise.

        invalidateFilter() is scheduled for deprecation (Qt 6.13); its replacement is
        beginFilterChange() (Qt 6.9) + endFilterChange(Direction) (Qt 6.10).
        """
        if hasattr(self, "beginFilterChange") and hasattr(self, "endFilterChange"):
            self.beginFilterChange()
            apply()
            direction = getattr(QSortFilterProxyModel, "Direction", None)
            if direction is not None:
                self.endFilterChange(direction.Rows)
            else:  # pragma: no cover - defensive
                self.endFilterChange()
        else:
            apply()
            self.invalidateFilter()

    def set_filter(self, name: str) -> None:
        self._change_filter(lambda: setattr(self, "_filter", name))

    def set_search(self, text: str) -> None:
        self._change_filter(lambda: setattr(self, "_search", text.strip().lower()))

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        pr = model.result(source_row) if isinstance(model, ResultsModel) else None
        if pr is None:
            return False
        if not matches_filter(pr, self._filter):
            return False
        if self._search:
            hay = f"{pr.mod} {pr.project} {pr.family.label} {pr.action} {' '.join(pr.reasons)}".lower()
            return self._search in hay
        return True


# ---------------------------------------------------------------------------
# background worker
# ---------------------------------------------------------------------------
class Worker(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[Callable[[int, int, str], None]], Any]):
        super().__init__()
        self._fn = fn

    @Slot()
    def run(self) -> None:
        try:
            res = self._fn(lambda d, t, m: self.progress.emit(int(d), int(t), str(m)))
        except Exception as exc:  # noqa: BLE001
            log.exception("background task failed")
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=4)}")
            return
        self.finished.emit(res)


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, controller: AppController):
        super().__init__()
        self.ctl = controller
        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker] = None
        self.setWindowTitle(f"{PRODUCT_NAME} {__version__} — developer window")
        self.resize(1400, 900)
        self._build()
        self._load_profiles()
        self._load_last_report()

    # -- layout ------------------------------------------------------------
    def _path_row(self, placeholder: str = "", browse: bool = True) -> tuple[QWidget, QLineEdit]:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        le = QLineEdit()
        le.setPlaceholderText(placeholder)
        h.addWidget(le, 1)
        if browse:
            b = QPushButton("…")
            b.setFixedWidth(32)
            b.clicked.connect(lambda: self._browse(le))
            h.addWidget(b)
        return w, le

    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)

        banner = QLabel("<b>Analysis never modifies mods.</b> Conversion (Phase 2 validation milestone) runs only when you "
                        "press Convert in the conversion window, writes only into the generated output mod, and is "
                        "enabled for the milestone test project only.")
        banner.setWordWrap(True)
        root.addWidget(banner)

        # profile ---------------------------------------------------------
        prof_box = QGroupBox("Profile")
        form = QFormLayout(prof_box)
        top = QWidget()
        th = QHBoxLayout(top)
        th.setContentsMargins(0, 0, 0, 0)
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(220)
        self.profile_combo.currentTextChanged.connect(self._on_profile_selected)
        th.addWidget(self.profile_combo, 1)
        for text, slot in (("New…", self._new_profile), ("Save", self._save_profile), ("Delete", self._delete_profile)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            th.addWidget(b)
        form.addRow("Profile", top)
        w, self.game_edit = self._path_row("e.g. T:\\Fallout 4")
        form.addRow("Fallout 4 directory", w)
        w, self.mo2_edit = self._path_row("e.g. T:\\MO_Main _ baseCBBE (folder with ModOrganizer.ini)")
        form.addRow("MO2 directory", w)
        self.mo2_profile_edit = QLineEdit()
        self.mo2_profile_edit.setPlaceholderText("auto (the instance's selected profile)")
        form.addRow("MO2 profile", self.mo2_profile_edit)
        w, self.mods_edit = self._path_row("auto (from ModOrganizer.ini)")
        form.addRow("Mods directory", w)
        w, self.bodyslide_edit = self._path_row("auto (Data\\Tools\\BodySlide through the MO2 virtual file system)")
        form.addRow("BodySlide directory", w)
        w, self.output_mod_edit = self._path_row("auto: <MO2 mods>\\CBBE 3BBB Auto Conversions (created only when you convert)")
        form.addRow("Output mod", w)
        osw = QWidget()
        osh = QHBoxLayout(osw)
        osh.setContentsMargins(0, 0, 0, 0)
        ow, self.os_dir_edit = self._path_row("separate Outfit Studio 5.8+ folder (your MO2 BodySlide 5.7.1 stays untouched)")
        osh.addWidget(ow, 1)
        self.os_detect_btn = QPushButton("Check")
        self.os_detect_btn.clicked.connect(self._check_runtime)
        osh.addWidget(self.os_detect_btn)
        form.addRow("Outfit Studio 5.8+", osw)
        self.os_status = QLabel("Outfit Studio: not checked")
        self.os_status.setWordWrap(True)
        form.addRow("", self.os_status)
        opts = QWidget()
        oh = QHBoxLayout(opts)
        oh.setContentsMargins(0, 0, 0, 0)
        self.include_disabled_cb = QCheckBox("Include mods disabled in the MO2 profile")
        self.use_cache_cb = QCheckBox("Use analysis cache")
        self.use_cache_cb.setChecked(True)
        oh.addWidget(self.include_disabled_cb)
        oh.addWidget(self.use_cache_cb)
        oh.addStretch(1)
        form.addRow("Options", opts)
        root.addWidget(prof_box)

        # actions -----------------------------------------------------------
        act = QHBoxLayout()
        self.detect_btn = QPushButton("Detect Installation")
        self.detect_btn.clicked.connect(self._detect)
        self.analyze_btn = QPushButton("Analyze Library")
        self.analyze_btn.clicked.connect(self._analyze)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        self.open_html_btn = QPushButton("Open HTML report")
        self.open_html_btn.clicked.connect(lambda: self._open(self.ctl.paths.reports / "latest.html"))
        self.open_dir_btn = QPushButton("Open reports folder")
        self.open_dir_btn.clicked.connect(lambda: self._open(self.ctl.paths.reports))
        self.convert_btn = QPushButton("Convert selected…")
        self.convert_btn.setEnabled(False)
        self.convert_btn.setToolTip("Preview (Dry Run) and convert the selected project with Outfit Studio 5.8+ headless automation.")
        self.convert_btn.clicked.connect(self._open_conversion)
        self.package_btn = QPushButton("Convert Package…")
        self.package_btn.setToolTip("Phase 3: Batch Preview, Dry Run, Run Batch, Validate and Commit for one MO2 mod")
        self.package_btn.clicked.connect(self._open_package)
        for b in (self.detect_btn, self.analyze_btn, self.cancel_btn, self.open_html_btn, self.open_dir_btn):
            act.addWidget(b)
        act.addStretch(1)
        act.addWidget(self.convert_btn)
        act.addWidget(self.package_btn)
        root.addLayout(act)

        self.install_label = QLabel("Installation: not detected yet")
        self.install_label.setWordWrap(True)
        self.install_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.install_label)

        # filters -------------------------------------------------------------
        fl = QHBoxLayout()
        fl.addWidget(QLabel("Filter"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(FILTERS)
        fl.addWidget(self.filter_combo)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search mod, project, reason…")
        self.search_edit.setClearButtonEnabled(True)
        fl.addWidget(self.search_edit, 1)
        self.count_label = QLabel("")
        fl.addWidget(self.count_label)
        root.addLayout(fl)

        # table + details ---------------------------------------------------------
        self.model = ResultsModel(self)
        self.proxy = ResultsFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        for i, width in enumerate((230, 330, 120, 70, 70, 70, 90, 100)):
            self.table.setColumnWidth(i, width)
        self.table.selectionModel().currentRowChanged.connect(self._on_row)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.details.setFont(mono)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        self.filter_combo.currentTextChanged.connect(self._apply_filter)
        self.search_edit.textChanged.connect(self._apply_filter)

        self.setCentralWidget(central)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(320)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("Ready")

    # -- helpers -----------------------------------------------------------
    def _browse(self, le: QLineEdit) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select folder", le.text() or "")
        if d:
            le.setText(os.path.normpath(d))

    def _open(self, path: Path) -> None:
        if not Path(path).exists():
            QMessageBox.information(self, "Not found", f"{path} does not exist yet. Run an analysis first.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _profile_from_fields(self) -> Profile:
        name = self.profile_combo.currentText().strip() or "Default"
        base = self.ctl.store.get(name) or Profile(name=name)
        base.game_dir = self.game_edit.text().strip()
        base.mo2_dir = self.mo2_edit.text().strip()
        base.mo2_profile = self.mo2_profile_edit.text().strip()
        base.mods_dir = self.mods_edit.text().strip()
        base.bodyslide_dir = self.bodyslide_edit.text().strip()
        base.analysis.include_disabled_mods = self.include_disabled_cb.isChecked()
        base.analysis.use_cache = self.use_cache_cb.isChecked()
        base.conversion.output_mod_path = self.output_mod_edit.text().strip()
        base.conversion.outfitstudio_dir = self.os_dir_edit.text().strip()
        base.output_mod_enabled = False
        return base

    def _fill_fields(self, p: Profile) -> None:
        self.game_edit.setText(p.game_dir)
        self.mo2_edit.setText(p.mo2_dir)
        self.mo2_profile_edit.setText(p.mo2_profile)
        self.mods_edit.setText(p.mods_dir)
        self.bodyslide_edit.setText(p.bodyslide_dir)
        self.include_disabled_cb.setChecked(p.analysis.include_disabled_mods)
        self.use_cache_cb.setChecked(p.analysis.use_cache)
        self.output_mod_edit.setText(p.conversion.output_mod_path)
        self.os_dir_edit.setText(p.conversion.outfitstudio_dir)

    def _load_profiles(self) -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        names = list(self.ctl.store.profiles) or ["Default"]
        self.profile_combo.addItems(names)
        cur = self.ctl.current_profile()
        idx = self.profile_combo.findText(cur.name)
        self.profile_combo.setCurrentIndex(max(0, idx))
        self.profile_combo.blockSignals(False)
        self._fill_fields(cur)

    def _load_last_report(self) -> None:
        p = self.ctl.paths.reports / "latest.json"
        if not p.is_file():
            return
        try:
            rep = load_report(p)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot load previous report: %s", exc)
            return
        self.ctl.report = rep
        self._show_report(rep, loaded=True)

    def _set_busy(self, busy: bool, msg: str = "") -> None:
        for b in (self.detect_btn, self.analyze_btn):
            b.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        if msg:
            self.statusBar().showMessage(msg)

    def _run_bg(self, fn: Callable, on_done: Callable[[Any], None], msg: str) -> None:
        if self._thread is not None:
            return
        self._set_busy(True, msg)
        thread = QThread(self)
        worker = Worker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(on_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_thread_done)
        self._thread, self._worker = thread, worker
        thread.start()

    # -- slots -------------------------------------------------------------
    @Slot(int, int, str)
    def _on_progress(self, done: int, total: int, msg: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            self.progress.setRange(0, 0)
        self.statusBar().showMessage(msg)

    @Slot(str)
    def _on_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Error", msg)
        self.statusBar().showMessage("Failed — see logs/app.log")

    @Slot()
    def _on_thread_done(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        if self._worker is not None:
            self._worker.deleteLater()
        self._thread = self._worker = None
        self._set_busy(False)

    def _on_profile_selected(self, name: str) -> None:
        p = self.ctl.store.get(name)
        if p:
            self._fill_fields(p)
            self.ctl.store.last_profile = name

    def _new_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "New profile", "Profile name:")
        if ok and name.strip():
            p = Profile(name=name.strip())
            self.ctl.save_profile(p)
            self._load_profiles()
            self.profile_combo.setCurrentText(p.name)

    def _save_profile(self) -> bool:
        p = self._profile_from_fields()
        problems = self.ctl.validate_profile(p)
        if problems:
            QMessageBox.warning(self, "Profile", "\n".join(problems))
            return False
        self.ctl.save_profile(p)
        cur = p.name
        self._load_profiles()
        self.profile_combo.setCurrentText(cur)
        self.statusBar().showMessage(f"Profile '{cur}' saved to {self.ctl.store.path}")
        return True

    def _delete_profile(self) -> None:
        name = self.profile_combo.currentText()
        if name and self.ctl.store.get(name):
            if QMessageBox.question(self, "Delete profile", f"Delete profile '{name}'? (only the profile entry)") == QMessageBox.Yes:
                self.ctl.store.delete(name)
                self.ctl.store.save()
                self._load_profiles()

    def _detect(self) -> None:
        if not self._save_profile():
            return
        prof = self._profile_from_fields()
        self._run_bg(lambda _p: self.ctl.detect(prof), self._on_detected, "Detecting installation…")

    @Slot(object)
    def _on_detected(self, inst: Any) -> None:
        s = inst.summary()
        parts = [
            f"Game: {s['game_dir'] or '—'}",
            f"MO2 {s['mo2_version'] or '—'} profile '{s['mo2_profile'] or '—'}' "
            f"({s['mods_enabled']}/{s['mods_total']} mods enabled)",
            f"BodySlide {s['bodyslide_version'] or '—'} in '{s['bodyslide_provider'] or '—'}'",
            f"Reference: {s['reference_template'] or 'NOT FOUND'}",
            f"{s['vfs_files']} BodySlide files",
        ]
        if s.get("outfitstudio_headless_automation") is False:
            parts.append("Outfit Studio < 5.8: no headless automation (relevant for Phase 2 only)")
        txt = " · ".join(parts)
        if s["errors"]:
            txt += "\nErrors: " + "; ".join(s["errors"])
        if s["warnings"]:
            txt += "\nWarnings: " + "; ".join(s["warnings"][:4])
        self.install_label.setText(txt)
        if not self.bodyslide_edit.text() and s["bodyslide_dir"]:
            self.bodyslide_edit.setPlaceholderText(f"auto: {s['bodyslide_dir']}")
        if not self.mods_edit.text() and s["mods_dir"]:
            self.mods_edit.setPlaceholderText(f"auto: {s['mods_dir']}")
        self.statusBar().showMessage("Installation detected" if inst.ok else "Detection finished with errors")

    def _analyze(self) -> None:
        if not self._save_profile():
            return
        prof = self._profile_from_fields()

        def job(progress: Callable[[int, int, str], None]) -> Any:
            self.ctl.detect(prof)
            if not self.ctl.installation.ok:
                raise RuntimeError("installation problems: " + "; ".join(self.ctl.installation.errors))
            return self.ctl.analyze(progress=progress)

        self._run_bg(job, self._on_analyzed, "Analyzing library…")

    @Slot(object)
    def _on_analyzed(self, rep: Any) -> None:
        if self.ctl.installation is not None:
            self._on_detected(self.ctl.installation)
        self._show_report(rep)

    def _cancel(self) -> None:
        self.ctl.cancel()
        self.statusBar().showMessage("Cancelling…")

    def _show_report(self, rep: Any, loaded: bool = False) -> None:
        self.model.set_results(rep.projects)
        self._apply_filter()
        counts: dict[str, int] = {}
        for p in rep.projects:
            counts[p.action] = counts.get(p.action, 0) + 1
        summary = " · ".join(f"{k} {counts.get(k, 0)}" for k in ("CONVERT", "REVIEW", "KEEP", "SKIP", "UNSUPPORTED", "ERROR"))
        prefix = f"Loaded previous report ({rep.started})" if loaded else f"Analysis finished in {rep.elapsed_s:.1f}s"
        if rep.cancelled:
            prefix += " — CANCELLED, partial results"
        self.statusBar().showMessage(f"{prefix}: {len(rep.projects)} projects — {summary}")

    def _apply_filter(self, *_: Any) -> None:
        self.proxy.set_filter(self.filter_combo.currentText())
        self.proxy.set_search(self.search_edit.text())
        self.count_label.setText(f"{self.proxy.rowCount()} / {self.model.rowCount()}")

    def _selected_result(self) -> Optional[ProjectResult]:
        idx = self.table.selectionModel().currentIndex()
        if not idx.isValid():
            return None
        return self.model.result(self.proxy.mapToSource(idx).row())

    def _check_runtime(self) -> None:
        prof = self._profile_from_fields()
        info = self.ctl.runtime_status(prof)
        self.os_status.setText(
            f"Outfit Studio: version {info.version or '—'} · path {info.path or '—'} · "
            f"headless automation supported: <b>{info.headless_label}</b> — {info.reason}"
            + ("<br>" + "<br>".join(info.warnings) if info.warnings else "")
        )

    def _open_conversion(self) -> None:
        pr = self._selected_result()
        if pr is None:
            return
        if not self._save_profile():
            return
        from .conversion_dialog import ConversionDialog

        prof = self._profile_from_fields()
        dlg = ConversionDialog(self.ctl, prof, pr, self)
        dlg.exec()

    def _open_package(self) -> None:
        if not self._save_profile():
            return
        prof = self._profile_from_fields()
        try:
            if self.ctl.installation is None or self.ctl.installation.profile.name != prof.name:
                self.ctl.detect(prof)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Detect", str(exc))
            return
        from .package_dialog import PackageDialog

        pr = self._selected_result()
        dlg = PackageDialog(self.ctl, prof, self, package=pr.mod if pr else "")
        dlg.exec()

    def _on_row(self, current: QModelIndex, _prev: QModelIndex) -> None:
        self.convert_btn.setEnabled(current.isValid())
        if not current.isValid():
            self.details.clear()
            return
        src = self.proxy.mapToSource(current)
        pr = self.model.result(src.row())
        self.details.setPlainText(project_details_text(pr) if pr else "")

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        if self._thread is not None:
            self.ctl.cancel()
            self._thread.quit()
            self._thread.wait(5000)
        try:
            self.ctl.store.save()
        except OSError:
            pass
        super().closeEvent(event)


def run_gui(controller: AppController, developer: bool = False) -> int:
    """Phase 3.2: the simplified window (Normal mode) by default; ``developer`` opens the engineering window."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("CBBE3BBBConverter")  # settings identity: unchanged
    app.setApplicationDisplayName(PRODUCT_NAME)
    if developer:
        win = MainWindow(controller)
    else:
        from .simple_window import SimpleWindow

        win = SimpleWindow(controller)
    win.show()
    return app.exec()
