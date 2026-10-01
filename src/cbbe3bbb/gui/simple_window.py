"""Phase 3.2 (Parts P/Q/R/R.1): the simplified end-user window (Normal mode).

Select mod → Convert → Build → Done. Everything technical stays one click away (Details, Show Log, Technical
Details, Developer Mode = the engineering window of earlier versions). Long work runs in a QThread; the conversion
itself is :class:`cbbe3bbb.app.pipeline.PackagePipeline`, whose events drive this view:

* individual outfit problems never open a modal dialog: they appear by NAME in the Problems area and the conversion
  continues (R.1: 'Problems: N', a short bullet list, '+ N more…', View All Problems);
* the final summary names the outfits that could not be converted and says that their originals were left unchanged;
* SYSTEM failures (the whole package) are shown differently from outfit problems, with Resume where it makes sense.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import PRODUCT_NAME, __version__
from ..app.controller import AppController
from ..app.messages import CONTROL_FAILURE_TEXT, LEFT_UNCHANGED, OUTCOME_LABELS, UNATTENDED_NOTE, package_message
from ..conversion.bodies import SOURCE_CHOICES, TARGET_CHOICES, body_label
from ..conversion.routes import RouteNotImplemented, resolve_request
from ..app.pipeline import LEVELS, analysis_overview, route_lines

log = logging.getLogger(__name__)

ALL_POSSIBLE_WARNING = (
    "Some outfits could not be classified with high confidence.\n\n"
    "The converter will attempt uncertain outfits where technically possible.\n\n"
    "Some items may still:\n  • have no physics;\n  • have imperfect physics;\n  • clip or deform;\n  • remain unconverted.\n\n"
    "Source mods will not be modified.\n\n"
    "Invalid generated results will not be installed when validation detects them."
)
MAX_PROBLEM_NAMES = 5


class EventWorker(QObject):
    event = Signal(object)
    finished = Signal(object)
    failed = Signal(str)
    question = Signal(object)  # 0.5.3: the conversion asks the window (the worker thread waits for the answer)

    def __init__(self, fn: Callable[[Callable[[dict], None]], Any]):
        super().__init__()
        self._fn = fn
        self._answer: Optional[str] = None
        self._answered = threading.Event()

    def ask(self, info: dict) -> str:
        """Called in the worker thread: show ``info`` in the window and wait for 'retry' | 'manual' | 'stop'."""
        self._answer = None
        self._answered.clear()
        self.question.emit(info)
        self._answered.wait()
        return self._answer or "stop"

    def answer(self, choice: str) -> None:
        self._answer = choice
        self._answered.set()

    @Slot()
    def run(self) -> None:
        try:
            res = self._fn(lambda ev: self.event.emit(ev))
        except Exception as exc:  # noqa: BLE001 - shown to the user, full trace in the log
            log.exception("background task failed")
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=4)}")
            return
        self.finished.emit(res)


def _ok(flag: Optional[bool], yes: str, no: str) -> str:
    if flag is None:
        return f"{no}  [?]"
    return f"{yes}  [✓]" if flag else f"{no}  [✗]"


class ProblemsDialog(QDialog):
    """'View Problems': one entry per outfit, human message first, technical details on request."""

    def __init__(self, items: list[dict], title: str, parent=None):
        super().__init__(parent)
        self.items = items
        self.setWindowTitle(title)
        self.resize(760, 560)
        v = QVBoxLayout(self)
        head = QLabel(f"<b>{len(items)} outfit{'s' if len(items) != 1 else ''} need attention</b>")
        v.addWidget(head)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        v.addWidget(self.view, 1)
        h = QHBoxLayout()
        self.tech = QCheckBox("Technical Details")
        self.tech.toggled.connect(self._render)
        h.addWidget(self.tech)
        h.addStretch(1)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text()))
        h.addWidget(copy)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        h.addWidget(bb)
        v.addLayout(h)
        self._render()

    def text(self) -> str:
        L = []
        for i in self.items:
            L += [i["name"], f"  {i['message']}"] + ([f"  {LEFT_UNCHANGED}"] if not i.get("runtime_issue") else [])
            if self.tech.isChecked():
                L += [f"    project: {i['project']}  status: {i.get('batch_status') or i.get('status')}"]
                L += [f"    - {t}" for t in i.get("technical", [])]
            L.append("")
        return "\n".join(L)

    @Slot()
    def _render(self, *_: Any) -> None:
        import html

        e = html.escape
        parts = []
        for i in self.items:
            s = (f"<p><b>{e(i['name'])}</b> — {e(i.get('short', ''))}<br>{e(i['message'])}"
                 + ("" if i.get("runtime_issue") else f"<br><i>{e(LEFT_UNCHANGED)}</i>"))
            if self.tech.isChecked():
                s += (f"<br><small>project: {e(i['project'])} · status {e(i.get('batch_status') or i.get('status', ''))} · "
                      f"bucket {e(i.get('bucket', ''))}</small><ul>"
                      + "".join(f"<li><small>{e(t)}</small></li>" for t in i.get("technical", [])) + "</ul>")
            parts.append(s + "</p>")
        self.view.setHtml("".join(parts) or "<p>No problems.</p>")


class RuntimeIssueDialog(QDialog):
    """'Report in-game problem…': the user's in-game check of a converted outfit (stored in manifest.json)."""

    STATES = (("No physics in game", "FAIL"), ("Physics problem in game", "PARTIAL"), ("Works now (clear the report)", "PASS"))

    def __init__(self, items: list[dict], parent=None):
        super().__init__(parent)
        self.items = sorted(items, key=lambda i: i["name"].lower())
        self.setWindowTitle("Report in-game problem")
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Outfit:"))
        self.item_combo = QComboBox()
        self.item_combo.addItems([i["name"] + ("  (reported)" if i.get("runtime_issue") else "") for i in self.items])
        v.addWidget(self.item_combo)
        v.addWidget(QLabel("What did you see in game?"))
        self.state_combo = QComboBox()
        self.state_combo.addItems([t for t, _ in self.STATES])
        v.addWidget(self.state_combo)
        v.addWidget(QLabel("Note (optional):"))
        self.note = QPlainTextEdit()
        self.note.setMaximumHeight(70)
        v.addWidget(self.note)
        hint = QLabel("This is kept with the conversion and shown under 'Needs attention' and in reports. The automatic "
                      "check result is not changed.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        v.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def values(self) -> tuple[dict, str, str]:
        return (self.items[self.item_combo.currentIndex()], self.STATES[self.state_combo.currentIndex()][1],
                self.note.toPlainText().strip())


class BuildDialog(QDialog):
    """'Build Converted Outfits': checks the output mod is enabled, then short BodySlide instructions."""

    def __init__(self, ctl: AppController, result, parent=None):
        super().__init__(parent)
        self.ctl, self.result = ctl, result
        self.setWindowTitle("Build Converted Outfits")
        self.resize(620, 420)
        v = QVBoxLayout(self)
        b = result.build
        state = b.get("output_mod_state", "")
        if state and state != "ENABLED":
            code = "OUTPUT_MOD_DISABLED_IN_MO2" if state == "DISABLED_IN_MO2" else "OUTPUT_MOD_NOT_IN_PROFILE"
            warn = QLabel(f"<b>⚠ {package_message(code).replace(chr(10), '<br>')}</b>")
            warn.setWordWrap(True)
            v.addWidget(warn)
        n = len(b.get("generated_projects", []))
        steps = QLabel("<br>".join([f"Start <b>BodySlide</b> from Mod Organizer 2, then:",
                                    f"Build only: <b>{b.get('group', '')}</b> ({n} outfits)",
                                    f"Preset: <b>{b.get('preset', 'CBBE Curvy')}</b>",
                                    f"Build Morphs: <b>{'ON' if b.get('build_morphs', True) else 'OFF'}</b>",
                                    "Click <b>Batch Build</b> → <b>Build</b>"]))
        steps.setTextFormat(Qt.RichText)
        steps.setWordWrap(True)
        v.addWidget(steps)
        note = QLabel("<b>Warning:</b> " + (b.get("warning") or b.get("order", "")))
        note.setTextFormat(Qt.RichText)
        note.setWordWrap(True)
        v.addWidget(note)
        h = QHBoxLayout()
        self.open_btn = QPushButton("Open BodySlide (via Mod Organizer 2)")
        self.open_btn.clicked.connect(self._open_bodyslide)
        self.open_btn.setEnabled(self._bodyslide_title() is not None)
        h.addWidget(self.open_btn)
        cp = QPushButton("Copy group name")
        cp.clicked.connect(lambda: QGuiApplication.clipboard().setText(b.get("group", "")))
        h.addWidget(cp)
        chk = QPushButton("Check the build")
        chk.clicked.connect(self._check)
        h.addWidget(chk)
        v.addLayout(h)
        self.out = QLabel("")
        self.out.setWordWrap(True)
        v.addWidget(self.out, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _bodyslide_title(self) -> Optional[str]:
        inst = self.ctl.installation
        if inst is None or inst.mo2 is None:
            return None
        return next((t for t in inst.mo2.executables if "bodyslide" in t.lower()), None)

    @Slot()
    def _open_bodyslide(self) -> None:
        inst = self.ctl.installation
        title = self._bodyslide_title()
        exe = Path(inst.mo2.root) / "ModOrganizer.exe" if inst and inst.mo2 else None
        if not title or exe is None or not exe.is_file():
            QMessageBox.information(self, "BodySlide", "Start BodySlide from Mod Organizer 2 (its executables list).")
            return
        try:  # the same link MO2 writes into its desktop shortcuts: runs the executable inside MO2's virtual folder
            subprocess.Popen([str(exe), f"moshortcut://:{title}"], cwd=str(exe.parent))
        except OSError as exc:
            QMessageBox.information(self, "BodySlide", f"Could not start it ({exc}). Start BodySlide from Mod Organizer 2.")

    @Slot()
    def _check(self) -> None:
        try:
            v = self.ctl.verify_package_build(self.result.package)
        except Exception as exc:  # noqa: BLE001
            self.out.setText(str(exc))
            return
        ok = v["counts"].get("BUILD_OK", 0)
        msg = [f"Built correctly: {ok} of {v['committed']} converted outfits."] + list(v.get("explanation", []))
        self.out.setText("\n".join(msg))


class SimpleWindow(QMainWindow):
    def __init__(self, controller: AppController, auto_detect: bool = True):
        super().__init__()
        self.ctl = controller
        self._thread: Optional[QThread] = None
        self._worker: Optional[EventWorker] = None
        self._events: list[dict] = []
        self._problems: list[dict] = []
        self.preview = None
        self.result = None
        self._dev = None
        self.setWindowTitle(f"{PRODUCT_NAME} {__version__}")
        self.resize(760, 720)
        self._build()
        self._body_changed()
        if auto_detect:
            self._detect()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        c = QWidget()
        v = QVBoxLayout(c)
        title = QLabel(PRODUCT_NAME)
        f = QFont()
        f.setPointSize(16)
        f.setBold(True)
        title.setFont(f)
        v.addWidget(title)
        env = QGroupBox()
        form = QFormLayout(env)
        self.game_lbl, self.mo2_lbl, self.body_lbl, self.out_lbl, self.os_lbl = (QLabel("…") for _ in range(5))
        form.addRow("Game:", self.game_lbl)
        form.addRow("Mod Manager:", self.mo2_lbl)
        form.addRow("Reference body:", self.body_lbl)
        form.addRow("Output:", self.out_lbl)
        form.addRow("Outfit Studio:", self.os_lbl)
        v.addWidget(env)
        v.addWidget(QLabel("Choose outfit mod:"))
        self.pkg_combo = QComboBox()
        self.pkg_combo.currentTextChanged.connect(self._package_changed)
        v.addWidget(self.pkg_combo)
        # 0.6.0: Source Body (Auto Detect by default) → Target Body. Only routes that can run convert; choosing a
        # source whose route is not implemented says so instead of starting anything.
        bodies = QGridLayout()
        bodies.addWidget(QLabel("Source Body:"), 0, 0)
        self.src_body = QComboBox()
        for key in SOURCE_CHOICES:
            self.src_body.addItem(body_label(key), key)
        self.src_body.currentIndexChanged.connect(self._body_changed)
        bodies.addWidget(self.src_body, 0, 1)
        self.detected_lbl = QLabel("Detected: —")
        self.detected_lbl.setStyleSheet("color: gray")
        self.detected_lbl.setWordWrap(True)
        bodies.addWidget(self.detected_lbl, 0, 2)
        bodies.addWidget(QLabel("Target Body:"), 1, 0)
        self.dst_body = QComboBox()
        for key in TARGET_CHOICES:
            self.dst_body.addItem(body_label(key), key)
        self.dst_body.currentIndexChanged.connect(self._body_changed)
        bodies.addWidget(self.dst_body, 1, 1)
        self.route_lbl = QLabel("")
        self.route_lbl.setWordWrap(True)
        bodies.addWidget(self.route_lbl, 2, 0, 1, 3)
        bodies.setColumnStretch(2, 1)
        v.addLayout(bodies)
        v.addWidget(QLabel("Conversion mode:"))
        self.safe_rb = QRadioButton("Safe")
        self.all_rb = QRadioButton("Convert All Possible")
        self.safe_rb.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.safe_rb)
        grp.addButton(self.all_rb)
        v.addWidget(self.safe_rb)
        v.addWidget(self._hint("Convert outfits the program can verify with high confidence."))
        v.addWidget(self.all_rb)
        v.addWidget(self._hint("Also try uncertain outfits. Some may remain unconverted."))
        row = QHBoxLayout()
        self.analyze_btn = QPushButton("Analyze")
        self.analyze_btn.clicked.connect(self._analyze)
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.clicked.connect(lambda: self._convert(None))
        row.addWidget(self.analyze_btn)
        row.addWidget(self.convert_btn)
        row.addStretch(1)
        self.settings_btn = QPushButton("Settings / Advanced")
        self.settings_btn.clicked.connect(self._open_developer)
        self.refresh_btn = QPushButton("Detect again")
        self.refresh_btn.clicked.connect(self._detect)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.settings_btn)
        v.addLayout(row)

        self.pages = QStackedWidget()
        self.pages.addWidget(QWidget())  # 0: empty
        self.pages.addWidget(self._analysis_page())  # 1
        self.pages.addWidget(self._progress_page())  # 2
        self.pages.addWidget(self._result_page())  # 3
        v.addWidget(self.pages, 1)
        self.setCentralWidget(c)
        self.statusBar().showMessage("Analysis and conversion never modify your mods; results go into a separate mod.")

    def _hint(self, text: str) -> QLabel:
        lb = QLabel(f"    {text}")
        lb.setStyleSheet("color: gray")
        lb.setWordWrap(True)
        return lb

    def _analysis_page(self) -> QWidget:
        w = QGroupBox("Analysis")
        v = QVBoxLayout(w)
        self.an_title = QLabel("")
        self.an_title.setStyleSheet("font-weight: bold")
        v.addWidget(self.an_title)
        g = QGridLayout()
        self.an_counts = {}
        for r, (k, label) in enumerate((("ready", "Ready to convert:"), ("try_all_possible", "Will try in All Possible:"),
                                        ("already_compatible", "Already compatible:"),
                                        ("no_conversion_needed", "No conversion needed:"),
                                        ("cannot_process", "Cannot safely process:"))):
            g.addWidget(QLabel(label), r, 0)
            n = QLabel("0")
            n.setAlignment(Qt.AlignRight)
            g.addWidget(n, r, 1)
            self.an_counts[k] = n
        v.addLayout(g)
        self.an_warn = QLabel("")
        self.an_warn.setWordWrap(True)
        self.an_warn.setStyleSheet("color: #b26b00; font-weight: bold")
        v.addWidget(self.an_warn)
        h = QHBoxLayout()
        b1 = QPushButton("Convert Safe")
        b1.clicked.connect(lambda: self._convert("SAFE"))
        b2 = QPushButton("Convert All Possible")
        b2.clicked.connect(lambda: self._convert("ALL_POSSIBLE"))
        b3 = QPushButton("Details")
        b3.clicked.connect(self._details)
        for b in (b1, b2, b3):
            h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
        v.addStretch(1)
        return w

    def _progress_page(self) -> QWidget:
        w = QGroupBox("Conversion")
        v = QVBoxLayout(w)
        self.pr_title = QLabel("")
        self.pr_title.setStyleSheet("font-weight: bold")
        v.addWidget(self.pr_title)
        self.pr_stage = QLabel("")
        v.addWidget(self.pr_stage)
        self.bar = QProgressBar()
        self.bar.setFormat("%v / %m")
        v.addWidget(self.bar)
        g = QGridLayout()
        self.pr_counts = {}
        for r, (k, label) in enumerate((("CONVERTED", "Converted:"), ("ATTENTION", "Needs attention:"),
                                        ("REMAINING", "Remaining:"))):
            g.addWidget(QLabel(label), r, 0)
            n = QLabel("0")
            n.setAlignment(Qt.AlignRight)
            g.addWidget(n, r, 1)
            self.pr_counts[k] = n
        v.addLayout(g)
        self.pr_note = QLabel(UNATTENDED_NOTE)
        self.pr_note.setWordWrap(True)
        self.pr_note.setStyleSheet("color: gray")
        v.addWidget(self.pr_note)
        self.pr_current = QLabel("")
        v.addWidget(self.pr_current)
        self.pr_session = QLabel("")
        self.pr_session.setWordWrap(True)
        self.pr_session.setStyleSheet("color: #0b6bcb")
        v.addWidget(self.pr_session)
        pb = QGroupBox("Problems")
        pv = QVBoxLayout(pb)
        self.pr_prob_head = QLabel("Problems: 0")
        pv.addWidget(self.pr_prob_head)
        self.pr_prob_list = QLabel("")
        self.pr_prob_list.setWordWrap(True)
        pv.addWidget(self.pr_prob_list)
        self.pr_prob_btn = QPushButton("View All Problems")
        self.pr_prob_btn.clicked.connect(lambda: self._view_problems(self._problems))
        self.pr_prob_btn.setVisible(False)
        pv.addWidget(self.pr_prob_btn)
        v.addWidget(pb)
        h = QHBoxLayout()
        self.log_btn = QPushButton("Show Log")
        self.log_btn.setCheckable(True)
        self.log_btn.toggled.connect(lambda on: (self.log_view.setVisible(on), self.level_combo.setVisible(on)))
        self.level_combo = QComboBox()
        self.level_combo.addItems(list(LEVELS))
        self.level_combo.currentTextChanged.connect(self._render_log)
        self.level_combo.setVisible(False)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        h.addWidget(self.log_btn)
        h.addWidget(self.level_combo)
        h.addStretch(1)
        h.addWidget(self.stop_btn)
        v.addLayout(h)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setVisible(False)
        v.addWidget(self.log_view, 1)
        return w

    def _result_page(self) -> QWidget:
        w = QGroupBox("Result")
        v = QVBoxLayout(w)
        self.rs_title = QLabel("")
        self.rs_title.setStyleSheet("font-weight: bold")
        v.addWidget(self.rs_title)
        self.rs_state = QLabel("")
        self.rs_state.setWordWrap(True)
        v.addWidget(self.rs_state)
        top = QGridLayout()  # 0.5.2: what the user needs first — names, not only counters
        self.rs_completed, self.rs_none, self.rs_attn_n = QLabel("0"), QLabel("0"), QLabel("0")
        for r, (label, n) in enumerate((("Completed:", self.rs_completed), ("No conversion needed:", self.rs_none),
                                        ("Needs attention:", self.rs_attn_n))):
            top.addWidget(QLabel(label), r, 0)
            n.setAlignment(Qt.AlignRight)
            top.addWidget(n, r, 1)
        v.addLayout(top)
        self.rs_attn_head = QLabel("<b>Needs attention / original kept:</b>")
        v.addWidget(self.rs_attn_head)
        self.rs_attention = QTextBrowser()
        self.rs_attention.setMinimumHeight(140)
        v.addWidget(self.rs_attention, 1)
        self.rs_problems = QLabel("")  # kept for compatibility (short line under the list)
        self.rs_problems.setWordWrap(True)
        v.addWidget(self.rs_problems)
        self.breakdown_btn = QPushButton("Show breakdown")
        self.breakdown_btn.setCheckable(True)
        v.addWidget(self.breakdown_btn, 0, Qt.AlignLeft)
        self.breakdown = QWidget()
        g = QGridLayout(self.breakdown)
        self.rs_counts = {}
        for r, k in enumerate(("CONVERTED", "ALREADY_COMPATIBLE", "NO_CONVERSION_NEEDED", "COULD_NOT_CONVERT_SAFELY",
                               "FAILED", "SKIPPED", "UNFINISHED")):
            lb = QLabel(OUTCOME_LABELS[k] + ":")
            n = QLabel("0")
            n.setAlignment(Qt.AlignRight)
            g.addWidget(lb, r, 0)
            g.addWidget(n, r, 1)
            self.rs_counts[k] = (lb, n)
        self.rs_percent = QLabel("")
        g.addWidget(self.rs_percent, len(self.rs_counts), 0, 1, 2)
        self.breakdown.setVisible(False)
        self.breakdown_btn.toggled.connect(self.breakdown.setVisible)
        v.addWidget(self.breakdown)
        self.rs_warn = QLabel("")
        self.rs_warn.setWordWrap(True)
        self.rs_warn.setStyleSheet("color: #b26b00; font-weight: bold")
        v.addWidget(self.rs_warn)
        h = QHBoxLayout()
        self.build_btn = QPushButton("Build Converted Outfits")
        self.build_btn.clicked.connect(self._build_dialog)
        self.viewp_btn = QPushButton("View Problems")
        self.viewp_btn.clicked.connect(lambda: self._view_problems(self.result.problems if self.result else []))
        self.detail_btn = QPushButton("View Details")
        self.detail_btn.clicked.connect(self._open_report)
        self.diag_btn = QPushButton("Export Diagnostic Report")
        self.diag_btn.clicked.connect(self._export_diag)
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.clicked.connect(self._resume)
        self.report_btn = QPushButton("Report in-game problem…")
        self.report_btn.clicked.connect(self._report_runtime)
        done = QPushButton("Done")
        done.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        for b in (self.build_btn, self.viewp_btn, self.report_btn, self.detail_btn, self.diag_btn, self.resume_btn, done):
            h.addWidget(b)
        v.addLayout(h)
        v.addStretch(1)
        return w

    # ------------------------------------------------------------------ background
    def _busy(self, busy: bool) -> None:
        for b in (self.analyze_btn, self.convert_btn, self.settings_btn, self.refresh_btn, self.pkg_combo):
            b.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

    def _run(self, fn: Callable[[Callable[[dict], None]], Any], done: Callable[[Any], None]) -> None:
        if self._thread is not None:
            return
        self._busy(True)
        th = QThread(self)
        wk = EventWorker(fn)
        wk.moveToThread(th)
        th.started.connect(wk.run)
        wk.event.connect(self._on_event)
        wk.question.connect(self._control_question)
        wk.finished.connect(done)
        wk.failed.connect(self._failed)
        wk.finished.connect(th.quit)
        wk.failed.connect(th.quit)
        th.finished.connect(self._thread_done)
        self._thread, self._worker = th, wk
        th.start()

    @Slot()
    def _thread_done(self) -> None:
        for o in (self._thread, self._worker):
            if o is not None:
                o.deleteLater()
        self._thread = self._worker = None
        self._busy(False)

    @Slot(str)
    def _failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Something went wrong",
                             "The converter stopped unexpectedly. Nothing was changed in your mods.\n\n"
                             "Use Export Diagnostic Report for a bug report.\n\n" + msg.split("\n\n")[0])

    # ------------------------------------------------------------------ detection
    @Slot()
    def _detect(self) -> None:
        prof = self.ctl.current_profile()

        def job(emit):
            if prof is None or not (prof.mo2_dir or prof.game_dir):
                return None
            inst = self.ctl.detect(prof)
            return inst, self.ctl.runtime_status(), self.ctl.package_names()

        self._run(job, self._detected)

    def _detected(self, res: Any) -> None:
        from ..conversion.package import output_mod_state

        if res is None:
            for lb in (self.game_lbl, self.mo2_lbl, self.body_lbl, self.out_lbl, self.os_lbl):
                lb.setText("not configured — open Settings / Advanced")
            return
        inst, rt, pkgs = res
        self.game_lbl.setText(_ok(inst.game_exe_found, "Fallout 4", "Fallout 4 not found"))
        self.mo2_lbl.setText(_ok(inst.mo2 is not None, "Mod Organizer 2", "Mod Organizer 2 not found")
                             + (f"   profile '{inst.mo2_profile}'" if inst.mo2 else ""))
        self.body_lbl.setText(_ok(inst.reference_template is not None, "CBBE 3BBB (installed)", "CBBE 3BBB body not found"))
        om = self.ctl.output_mod_path()
        st = output_mod_state(inst, om)
        name = om.name if om else "CBBE 3BBB Auto Conversions"
        self.out_lbl.setText({"ENABLED": f"{name}  [Enabled ✓]", "DISABLED_IN_MO2": f"{name}  [Disabled in MO2 ✗]",
                              "NOT_IN_PROFILE": f"{name}  [not in the MO2 list yet]",
                              "NOT_CREATED": f"{name}  [created on the first conversion]"}.get(st, name))
        self.os_lbl.setText(_ok(bool(rt.headless_supported), f"Outfit Studio {rt.version or ''}".strip(),
                                "not set up (Settings / Advanced)"))
        cur = self.pkg_combo.currentText()
        self.pkg_combo.blockSignals(True)
        self.pkg_combo.clear()
        self.pkg_combo.addItems(pkgs)
        if cur in pkgs:
            self.pkg_combo.setCurrentText(cur)
        self.pkg_combo.blockSignals(False)

    @Slot(str)
    def _package_changed(self, _name: str) -> None:
        self.preview = None
        self.pages.setCurrentIndex(0)

    # ------------------------------------------------------------------ analyze
    @Slot()
    def _analyze(self) -> None:
        pkg = self.pkg_combo.currentText()
        if not pkg:
            return
        self._events = []

        def job(emit):
            pl = self.ctl.pipeline(pkg, events=emit)
            return pl.analyze()

        self.statusBar().showMessage("Analyzing package…")
        self._run(job, self._analyzed)

    def _analyzed(self, prev: Any) -> None:
        self.preview = prev
        ov = analysis_overview(prev)
        self.detected_lbl.setText("Detected: " + (", ".join(f"{k} {v}" for k, v in ov.get("bodies", {}).items()) or "—"))
        self.route_lbl.setStyleSheet("")
        self.route_lbl.setText("\n".join(route_lines(ov)[2:]))
        self.route_lbl.setToolTip("\n".join(route_lines(ov)))
        self.an_title.setText(f"{prev.package}\n\n{ov['total']} outfits/projects found")
        for k, lb in self.an_counts.items():
            lb.setText(str(ov[k]))
        self.an_warn.setText("\n".join(package_message(c, Path(prev.output_mod).name) for c in prev.codes))
        self.pages.setCurrentIndex(1)
        self.statusBar().showMessage("Analysis complete.")

    @Slot()
    def _details(self) -> None:
        from .package_dialog import PackageDialog

        dlg = PackageDialog(self.ctl, self.ctl.installation.profile, self, package=self.pkg_combo.currentText())
        dlg.exec()

    # ------------------------------------------------------------------ convert
    def _convert(self, mode: Optional[str]) -> None:
        pkg = self.pkg_combo.currentText()
        if not pkg:
            return
        mode = mode or ("ALL_POSSIBLE" if self.all_rb.isChecked() else "SAFE")
        if mode == "ALL_POSSIBLE":
            box = QMessageBox(QMessageBox.Information, "Convert All Possible", ALL_POSSIBLE_WARNING, parent=self)
            go = box.addButton("Convert All Possible", QMessageBox.AcceptRole)
            box.addButton("Cancel", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not go:
                return
        bodies = self._bodies()  # 0.6.0: an unavailable route is refused before anything starts
        if bodies is None:
            return
        src, dst = bodies
        prev = self.preview if self.preview is not None and self.preview.package == pkg else None
        self._start_progress(pkg)

        def job(emit):
            return self.ctl.pipeline(pkg, mode=mode, events=emit, control_failure=self._ask_worker,
                                     source_body=src, target_body=dst).run(prev)

        self._run(job, self._finished)

    def _bodies(self) -> Optional[tuple[str, str]]:
        """(source, target) body ids, or None (after telling the user) when that conversion cannot run."""
        try:
            return resolve_request(self.src_body.currentData(), self.dst_body.currentData())
        except RouteNotImplemented as exc:
            QMessageBox.information(self, "Not implemented", f"{exc}\n\nNothing was started. "
                                    "Supported now: CBBE → CBBE 3BBB (use Source Body: Auto Detect).")
            return None

    @Slot()
    def _body_changed(self, *_a) -> None:
        try:
            src, _dst = resolve_request(self.src_body.currentData(), self.dst_body.currentData())
        except RouteNotImplemented as exc:
            self.route_lbl.setText(f"⚠ {exc}")
            self.route_lbl.setStyleSheet("color: #b35c00")
            return
        self.route_lbl.setStyleSheet("")
        self.route_lbl.setText("Each outfit's body is detected automatically." if src == "auto" else
                               f"Only outfits detected as {body_label(src)} are converted; others are left unchanged.")

    def _ask_worker(self, info: dict) -> str:
        wk = self._worker
        return wk.ask(info) if wk is not None else "stop"

    @Slot(object)
    def _control_question(self, info: dict) -> None:
        box = QMessageBox(QMessageBox.Warning, "Outfit Studio", CONTROL_FAILURE_TEXT, parent=self)
        box.setDetailedText(f"Session {info.get('session')}: {info.get('reason', '')}\n\nManual continuation: "
                            f"{info.get('fallback', '')}")
        retry = box.addButton("Retry automatically", QMessageBox.AcceptRole)
        manual = box.addButton("Continue manually", QMessageBox.ActionRole)
        box.addButton("Stop for now", QMessageBox.RejectRole)
        box.exec()
        choice = "retry" if box.clickedButton() is retry else "manual" if box.clickedButton() is manual else "stop"
        if choice == "manual":
            QMessageBox.information(self, "Continue manually",
                                    "Outfit Studio opens for this session. In Outfit Studio: File > Open Automation, "
                                    f"choose '{info.get('script', '')}', press Execute, confirm, wait for the final "
                                    "message, then close Outfit Studio.")
        if self._worker is not None:
            self._worker.answer(choice)

    @Slot()
    def _resume(self) -> None:
        if not self.result or not self.result.batch_id:
            return
        bid = self.result.batch_id
        self._start_progress(self.result.package)

        def job(emit):
            return self.ctl.pipeline("", events=emit, control_failure=self._ask_worker).resume(bid)

        self._run(job, self._finished)

    def _start_progress(self, pkg: str) -> None:
        self._events, self._problems = [], []
        self.pr_title.setText(f"Converting {pkg}")
        self.pr_stage.setText("Starting…")
        self.bar.setRange(0, 0)
        for lb in self.pr_counts.values():
            lb.setText("0")
        self.pr_note.setVisible(True)
        self.pr_current.setText("")
        self.pr_session.setText("")
        self._render_problems()
        self.log_view.clear()
        self.pages.setCurrentIndex(2)

    @Slot()
    def _stop(self) -> None:
        self.ctl.cancel()
        self.pr_stage.setText("Stopping after the current step… (Resume continues later)")

    @Slot(object)
    def _on_event(self, ev: dict) -> None:
        self._events.append(ev)
        k = ev.get("kind")
        if k == "stage":
            self.pr_stage.setText(ev.get("text", ""))
            if ev.get("total"):
                self.bar.setRange(0, int(ev["total"]))
                self.bar.setValue(0)
        elif k in ("item_ok", "item_problem", "progress"):
            if ev.get("total"):
                self.bar.setRange(0, int(ev["total"]))
                self.bar.setValue(int(ev.get("done", 0)))
            c = ev.get("counts") or {}
            conv = int(c.get("CONVERTED", 0))
            att = sum(int(v) for k2, v in c.items() if k2 != "CONVERTED")
            self.pr_counts["CONVERTED"].setText(str(conv))
            self.pr_counts["ATTENTION"].setText(str(att))
            if ev.get("total"):
                self.pr_counts["REMAINING"].setText(str(max(0, int(ev["total"]) - int(ev.get("done", 0)))))
            if k == "item_problem":
                self._problems.append({"project": ev.get("project"), "name": ev.get("name"), "message": ev.get("message"),
                                       "technical": ev.get("technical", []), "batch_status": ev.get("outcome")})
                self._render_problems()
                self.statusBar().showMessage(f"Could not convert: {ev.get('name')} — continuing…", 8000)
        elif k == "current":
            self.pr_current.setText(f"Current:\n{ev.get('name') or ev.get('project')}")
        elif k == "session_start":
            self.pr_session.setText(ev.get("text", "") if ev.get("level") == "NORMAL" else "")
            self.pr_note.setVisible(bool(ev.get("unattended")) or not ev.get("instructions"))
        elif k == "control_failure":
            self.pr_session.setText(ev.get("text", ""))
        elif k in ("session_failure", "warning"):
            self.pr_session.setText(ev.get("text", ""))
        elif k == "system_failure":
            self.pr_stage.setText(ev.get("text", ""))
        self._append_log(ev)

    def _render_problems(self) -> None:
        n = len(self._problems)
        self.pr_prob_head.setText(f"Problems: {n}")
        names = [p["name"] for p in self._problems]
        lines = [f"• {x}" for x in names[:MAX_PROBLEM_NAMES]]
        if n > MAX_PROBLEM_NAMES:
            lines.append(f"+ {n - MAX_PROBLEM_NAMES} more…")
        self.pr_prob_list.setText("\n".join(lines))
        self.pr_prob_btn.setVisible(n > 0)

    def _level_ok(self, ev: dict) -> bool:
        order = {x: i for i, x in enumerate(LEVELS)}
        return order.get(ev.get("level", "NORMAL"), 0) <= order.get(self.level_combo.currentText(), 0)

    def _append_log(self, ev: dict) -> None:
        if ev.get("text") and self._level_ok(ev):
            self.log_view.appendPlainText(f"{ev.get('time', '')} {ev['text']}")

    @Slot(str)
    def _render_log(self, *_: Any) -> None:
        self.log_view.setPlainText("\n".join(f"{e.get('time', '')} {e['text']}" for e in self._events
                                             if e.get("text") and self._level_ok(e)))

    def _finished(self, r: Any) -> None:
        self.result = r
        self.rs_title.setText(r.package)
        self.rs_state.setText(r.state_label + (f"\n\n{r.system_message}" if r.system_message else ""))
        for k, (lb, n) in self.rs_counts.items():
            v = r.counts.get(k, 0)
            n.setText(str(v))
            show = bool(v) or k in ("CONVERTED", "COULD_NOT_CONVERT_SAFELY", "FAILED")
            lb.setVisible(show)
            n.setVisible(show)
        self.rs_percent.setText(f"{r.percent}% of applicable outfits converted." if r.percent is not None else "")
        import html as _h

        attn = r.needs_attention
        self.rs_completed.setText(str(len(r.completed)))
        self.rs_none.setText(str(r.counts.get("NO_CONVERSION_NEEDED", 0)))
        self.rs_attn_n.setText(str(len(attn)))
        self.rs_attn_head.setVisible(bool(attn))
        self.rs_attention.setVisible(bool(attn))
        self.rs_attention.setHtml("".join(f"<div>• <b>{_h.escape(i['name'])}</b> — {_h.escape(i['short'])}</div>"
                                          for i in attn))
        probs = r.problems
        kept = [i for i in attn if not i.get("runtime_issue")]
        self.rs_problems.setText("<i>Original versions of the outfits above were left unchanged; they did not get "
                                 "3BBB physics.</i>" if kept else "")
        self.rs_warn.setText("\n".join(w["message"] for w in r.warnings))
        self.build_btn.setEnabled(bool(r.build.get("generated_projects")))
        self.viewp_btn.setEnabled(bool(attn))
        self.report_btn.setEnabled(bool(r.completed) or any(i.get("runtime_issue") for i in r.items))
        self.resume_btn.setVisible(r.state in ("INTERRUPTED", "SYSTEM_ERROR") and bool(r.batch_id))
        try:
            self._report = self.ctl.write_package_report(r)
        except OSError:
            self._report = {}
        self.pages.setCurrentIndex(3)

    # ------------------------------------------------------------------ dialogs
    def _view_problems(self, items: list[dict]) -> None:
        if self.result is not None and items is not self._problems:
            items = self.result.needs_attention
        ProblemsDialog(items, "Needs attention", self).exec()

    @Slot()
    def _report_runtime(self) -> None:
        """Record an in-game problem (or clear it) for a converted outfit; the validator result stays as it was."""
        if self.result is None:
            return
        dlg = RuntimeIssueDialog([i for i in self.result.items if i["outcome"] in ("CONVERTED", "ALREADY_COMPATIBLE")
                                  and i.get("generated_project")], self)
        if dlg.exec() != QDialog.Accepted:
            return
        item, state, note = dlg.values()
        try:
            self.ctl.record_runtime_issue(item["generated_project"], state, note)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "In-game problem", str(exc))
            return
        self._refresh_result()

    def _refresh_result(self) -> None:
        r = self.result
        if r is None:
            return
        import json as _json

        from ..conversion.package import PackagePreview

        prev = self.preview
        bdir = Path(r.batch_dir) if r.batch_dir else None
        if bdir is not None and (bdir / "preview.json").is_file():
            prev = PackagePreview.from_dict(_json.loads((bdir / "preview.json").read_text(encoding="utf-8")))
        if prev is None:
            return
        pl = self.ctl.pipeline(prev.package, mode=r.mode)
        self._finished(pl.result(prev, bdir))

    @Slot()
    def _build_dialog(self) -> None:
        if self.result is not None:
            BuildDialog(self.ctl, self.result, self).exec()

    @Slot()
    def _open_report(self) -> None:
        p = (getattr(self, "_report", {}) or {}).get("txt")
        if p:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    @Slot()
    def _export_diag(self) -> None:
        try:
            p = self.ctl.export_diagnostics(result=self.result, preview=self.preview)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Diagnostic report", str(exc))
            return
        QMessageBox.information(self, "Diagnostic report",
                                f"Saved:\n{p}\n\nAttach this file to a bug report. Paths are anonymised; no mod files "
                                "are included.")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))

    @Slot()
    def _open_developer(self) -> None:
        from .main_window import MainWindow

        if self._dev is None:
            self._dev = MainWindow(self.ctl)
        self._dev.show()
        self._dev.raise_()

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
