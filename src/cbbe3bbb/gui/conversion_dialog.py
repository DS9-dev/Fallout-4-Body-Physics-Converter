"""Conversion window: preview (Dry Run) and Convert for one project.

Dry Run builds the plan and shows every planned operation, including the exact
Outfit Studio automation script, without writing files. Convert is enabled only
after a Dry Run without blockers, asks for confirmation, and then runs Outfit
Studio headless in a worker thread.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QThread, QUrl, Slot
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)

from ..analysis.models import ProjectResult
from ..app.controller import AppController
from ..profiles.store import Profile
from .main_window import Worker

__all__ = ["ConversionDialog"]


class ConversionDialog(QDialog):
    def __init__(self, ctl: AppController, profile: Profile, pr: ProjectResult, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        self.profile = profile
        self.pr = pr
        self.plan = None
        self.result = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker] = None
        self.setWindowTitle(f"Convert: {pr.project}")
        self.resize(1000, 760)
        lay = QVBoxLayout(self)
        head = QLabel(f"<b>{pr.project}</b> — {pr.mod} — analyzer: <b>{pr.action}</b> ({pr.family.label})")
        head.setWordWrap(True)
        lay.addWidget(head)
        row = QHBoxLayout()
        row.addWidget(QLabel("BodySlide preset (for the manual build)"))
        self.preset = QComboBox()
        row.addWidget(self.preset, 1)
        self.morphs = QCheckBox("Build Morphs")
        self.morphs.setChecked(profile.conversion.build_morphs)
        row.addWidget(self.morphs)
        lay.addLayout(row)
        self.tabs = QTabWidget()
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono)
        self.script = QPlainTextEdit()
        self.script.setReadOnly(True)
        self.script.setFont(mono)
        self.results = QPlainTextEdit()
        self.results.setReadOnly(True)
        self.results.setFont(mono)
        self.tabs.addTab(self.preview, "Preview")
        self.tabs.addTab(self.script, "Automation script")
        self.tabs.addTab(self.results, "Result / validation")
        lay.addWidget(self.tabs, 1)
        self.status = QLabel("Press Dry Run to plan the conversion. Nothing is written by a Dry Run.")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        btns = QHBoxLayout()
        self.dry_btn = QPushButton("Dry Run")
        self.dry_btn.clicked.connect(self._dry_run)
        self.conv_btn = QPushButton("Convert")
        self.conv_btn.setEnabled(False)
        self.conv_btn.clicked.connect(self._convert)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.ctl.cancel)
        self.open_ws = QPushButton("Open workspace / logs")
        self.open_ws.setEnabled(False)
        self.open_ws.clicked.connect(lambda: self._open(self.result.workspace if self.result else ""))
        self.open_mod = QPushButton("Open output mod")
        self.open_mod.setEnabled(False)
        self.open_mod.clicked.connect(lambda: self._open(self.plan.output_mod_path if self.plan else ""))
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        for b in (self.dry_btn, self.conv_btn, self.cancel_btn, self.open_ws, self.open_mod):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(close)
        lay.addLayout(btns)

    # ------------------------------------------------------------------
    def _open(self, path: str) -> None:
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _busy(self, busy: bool, msg: str = "") -> None:
        self.dry_btn.setEnabled(not busy)
        self.conv_btn.setEnabled(not busy and bool(self.plan and self.plan.can_convert) and self.result is None)
        self.cancel_btn.setEnabled(busy)
        if msg:
            self.status.setText(msg)

    def _run(self, fn, done) -> None:
        if self._thread is not None:
            return
        thread = QThread(self)
        worker = Worker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(lambda d, t, m: self.status.setText(m))
        worker.finished.connect(done)
        worker.failed.connect(self._failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._thread_done)
        self._thread, self._worker = thread, worker
        thread.start()

    @Slot()
    def _thread_done(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        if self._worker is not None:
            self._worker.deleteLater()
        self._thread = self._worker = None
        self._busy(False)

    @Slot(str)
    def _failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Error", msg)
        self.status.setText("Failed — see logs/app.log")

    # ------------------------------------------------------------------
    def _dry_run(self) -> None:
        self.result = None
        self._busy(True, "Planning (Dry Run)… re-analyzing the project, nothing is written")
        prof = self.profile

        def job(progress):
            progress(0, 0, "Detecting installation…")
            self.ctl.detect(prof)
            progress(0, 0, "Re-analyzing the project and planning…")
            return self.ctl.plan_conversion(self.pr, output_mod=prof.conversion.output_mod_path or None)

        self._run(job, self._planned)

    @Slot(object)
    def _planned(self, plan: Any) -> None:
        self.plan = plan
        self.preset.clear()
        self.preset.addItems(plan.presets_available or [plan.preset])
        if plan.preset:
            self.preset.setCurrentText(plan.preset)
        text = "\n".join(plan.preview_lines())
        if plan.script_summary:
            text += "\n\nAutomation steps:\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(plan.script_summary, 1))
        text += "\n\nFiles the conversion would write into the output mod:\n" + "\n".join(f"  {f}" for f in plan.files_to_write)
        self.preview.setPlainText(text)
        self.script.setPlainText(plan.script_xml or "(no script: the plan is blocked)")
        self.tabs.setCurrentWidget(self.preview)
        if plan.can_convert:
            self.status.setText("Dry Run complete — nothing was written. Review the preview, then press Convert.")
        else:
            self.status.setText("Dry Run complete — conversion is BLOCKED: " + "; ".join(plan.blockers))
        self._busy(False)

    def _convert(self) -> None:
        if not self.plan or not self.plan.can_convert:
            return
        ok = QMessageBox.question(
            self, "Convert",
            f"Run Outfit Studio headless to create\n\n  {self.plan.output_project}\n\nin\n\n  {self.plan.output_mod_path}\n\n"
            "Source files are only read. Continue?",
        )
        if ok != QMessageBox.Yes:
            return
        plan = self.plan
        self._busy(True, "Converting…")

        def job(progress):
            return self.ctl.convert(plan, progress=lambda m: progress(0, 0, m))

        self._run(job, self._converted)

    @Slot(object)
    def _converted(self, res: Any) -> None:
        self.result = res
        lines = [f"RESULT: {res.status}", ""]
        lines += [f"  - {r}" for r in res.reasons]
        for c in res.validation.get("checks", []):
            lines.append(f"[{'PASS' if c['ok'] else ('FAIL' if c.get('required', True) else 'WARN')}] {c['name']}: {c['detail']}")
        lines += ["", f"Analyzer on generated project: {res.generated_classification.get('action')} / {res.generated_classification.get('label')}"]
        lines += ["", f"Workspace (plan, script, Outfit Studio log, validation): {res.workspace}"]
        if res.files_written:
            lines += ["", "Written to the output mod:"] + [f"  {self.plan.output_mod_path}{os.sep}{f}" for f in res.files_written]
            args = (f'--groupbuild "CBBE 3BBB Auto Conversions" --targetdir "{self.plan.output_mod_path}" '
                    f'--preset "{self.preset.currentText()}"' + (" --trimorphs" if self.morphs.isChecked() else ""))
            lines += ["", "Next (BodySlide build, manual):",
                      "  1. MO2: press F5, enable the output mod and place it after (below) the source mod.",
                      "  2. Recommended: add an MO2 executable 'BodySlide AUTO build' = your BodySlide x64.exe with arguments:",
                      f"       {args}",
                      "     This builds into the output mod (the source mod's meshes stay untouched).",
                      f"  3. Or in BodySlide: select '{self.plan.output_project}', preset '{self.preset.currentText()}',",
                      "     tick Build Morphs and Build (note: a normal build writes the mesh wherever MO2 already has it)."]
        self.results.setPlainText("\n".join(lines))
        self.tabs.setCurrentWidget(self.results)
        self.open_ws.setEnabled(bool(res.workspace))
        self.open_mod.setEnabled(res.ok)
        self.status.setText(f"Conversion {res.status}" + ("" if res.ok else " — nothing was written to the output mod"))
        self._busy(False)
        self.conv_btn.setEnabled(False)
