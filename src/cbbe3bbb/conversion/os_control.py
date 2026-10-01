"""0.5.3: unattended Outfit Studio sessions (the GUI runs normally; nobody has to touch it).

Why the GUI stays
-----------------
Outfit Studio 5.8.0–5.8.2's headless mode (``-a``) never shows the main frame, so the OpenGL panel never initializes,
no GL meshes exist and ``CopyBoneWeights`` silently does nothing (RESEARCH.md §6, UPSTREAM_ISSUE.md). The accepted
CP5 / Wardrobe 10 conversions were all produced by the *windowed* Automation dialog. This module keeps exactly that code
path and only takes over the clicks.

How a session runs (every step is checked against observable state; timeouts are only safety guards)
------------------------------------------------------------------------------------------------
1. **Script provenance** (before anything starts): the session's script exists in the workspace project's
   ``Automations`` folder under its exact generated name ``cbbe3bbb_batch_<batch id>_<session>``, its SHA-256 equals
   the hash recorded when the batch wrote it, it passes :func:`check_batch_invariants`, it has one part per planned
   project in the planned order (each part's SaveProject names that project's generated project) and the planned
   number of steps. Otherwise nothing is executed.
2. **Exact preselection**: Outfit Studio restores ``AutomationDialog.lastScript`` from its own ``OutfitStudio.xml``
   when the Automation window opens (``AutomationDialog`` constructor, 5.8.2). The private runtime's file gets the
   exact session script name; nothing is chosen by position or "latest".
3. **Readiness**: the process runs, ``Log_OS.txt`` (deleted before the start) contains ``Outfit Studio initialized.``
   *and* ``OpenGL Context Info:`` (written when the 3D view initialized: the condition the headless mode lacks), the
   main window exists and the process is idle.
4. **Open + verify**: the menu command "Open Automation..." is sent to the main window (its native menu item ID is read
   from the menu, not guessed). The Automation window's script box must show the exact script name and its step list
   must have the planned number of steps; the script file hash is checked again. Only then Execute is pressed.
5. **Known dialogs only**, identified by title (+ buttons): *Confirm Execution* (Yes), *Automation Error* (Yes =
   continue, like the headless mode; the step is recorded and attributed to its project), *Choose cloth data* (see
   :func:`decide_cloth`), *Reference Error* / *NIF Error* / *Deleted Shapes* (OK; recorded), *Automation* (the
   completion message: OK) and at the end *Unsaved Changes* (No). Anything else is captured and the session is stopped
   (``USER_INTERACTION_REQUIRED``); the project that was running is isolated by the batch.
6. **Completion handshake**: the completion message, the status bar and the Automation window's output pane
   (``Automation: Step i/N`` for every step) together; the result is written to ``session_result.json``. The process
   exiting is *never* taken as success.
7. Outfit Studio is closed gracefully (Automation window, main window, "Unsaved Changes" → No); forced only when it
   does not exit.

Automation completing is not conversion success: every project is still validated on its own afterwards.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Callable, Optional

from .automation import ScriptInvariantError, check_batch_invariants, parse_script, split_segments

__all__ = [
    "SessionSpec", "SessionProject", "SessionResult", "SessionController", "UIDriver", "Win",
    "verify_session_script", "decide_cloth", "write_last_script", "session_script_name",
    "COMPLETED", "COMPLETED_WITH_PROJECT_ERRORS", "AUTOMATION_FAILED", "USER_INTERACTION_REQUIRED", "TIMEOUT",
    "OS_CRASHED", "CANCELLED", "CONTROL_FAILURES", "CLOTH_AMBIGUOUS_MESSAGE",
]

COMPLETED = "COMPLETED"
COMPLETED_WITH_PROJECT_ERRORS = "COMPLETED_WITH_PROJECT_ERRORS"
AUTOMATION_FAILED = "AUTOMATION_FAILED"  # the controller could not safely start/drive the script (nothing attributable)
USER_INTERACTION_REQUIRED = "USER_INTERACTION_REQUIRED"  # an unknown or unsafe dialog
TIMEOUT = "TIMEOUT"
OS_CRASHED = "OS_CRASHED"
CANCELLED = "CANCELLED"
# statuses that say nothing about any single project: the session is retried (or continued manually)
CONTROL_FAILURES = (AUTOMATION_FAILED,)

CLOTH_AMBIGUOUS_MESSAGE = "Multiple cloth physics data sources require review."

MAIN_TITLE = "Outfit Studio"
AUTOMATION_TITLE = "Automation Script"
T_CONFIRM, T_ERROR, T_CLOTH, T_DONE, T_UNSAVED = ("Confirm Execution", "Automation Error", "Choose cloth data",
                                                  "Automation", "Unsaved Changes")
T_INFO_ERRORS = ("Reference Error", "NIF Error", "Deleted Shapes")
MENU_LABEL = "Open Automation"
EXECUTE_LABEL = "Execute"
READY_MARKERS = ("Outfit Studio initialized.", "OpenGL Context Info:")
FATAL_MARKERS = ("Fatal exception has occurred", "Unhandled exception has occurred", "Unexpected exception has occurred")
IDOK, IDCANCEL, IDYES, IDNO = 1, 2, 6, 7
# The native message boxes Outfit Studio 5.8.2 shows, identified by exact title AND the exact set of button texts
# (English runtime), with the one button to press and its task-dialog button ID. wxWidgets (msw/msgdlg.cpp,
# wxMSWTaskDialogConfig) builds wxYES_NO as IDYES/IDNO, wxYES_NO|wxCANCEL as IDYES/IDNO/IDCANCEL and an OK-only box as a
# single *IDCANCEL* button labelled "OK" (so Escape can close it). The button windows themselves all have control ID 0
# (DirectUIHWND > CtrlNotifySink > Button), so a button is identified by its text, never by ID or position.
KNOWN_BOXES = {
    "Confirm Execution": (("No", "Yes"), "Yes", IDYES),
    "Automation Error": (("No", "Yes"), "Yes", IDYES),
    "Automation": (("OK",), "OK", IDCANCEL),
    "Reference Error": (("OK",), "OK", IDCANCEL),
    "NIF Error": (("OK",), "OK", IDCANCEL),
    "Deleted Shapes": (("OK",), "OK", IDCANCEL),
    "Unsaved Changes": (("Cancel", "No", "Yes"), "No", IDNO),
}
WX_ID_OK, WX_ID_CANCEL = 5100, 5101  # wxID_OK / wxID_CANCEL: the native control IDs of wx dialog buttons
_STEP = re.compile(r"Automation: Step (\d+)/(\d+)")
_STEP_FAILED = re.compile(r"Step (\d+) \((\w+)\) failed with error (-?\d+)")


def session_script_name(batch_id: str, session: int) -> str:
    from .automation import safe_script_name

    return safe_script_name(f"cbbe3bbb_batch_{batch_id}_{session}")


# --------------------------------------------------------------------------------------------- data
@dataclass
class SessionProject:
    id: str
    project: str
    generated_project: str
    step_range: tuple[int, int]
    outputs: dict[str, str]  # planned output files (absolute)
    cloth_blocks: int = 0  # BSClothExtraData blocks in the source ShapeData NIF
    source_nif: str = ""  # the staged (workspace) copy of the source ShapeData NIF
    source_nif_sha256: str = ""  # SHA-256 of the SOURCE file (recorded before staging)


@dataclass
class SessionSpec:
    batch_id: str
    session: int
    script_name: str
    script_sha256: str
    expected_steps: int
    projects: list[SessionProject]
    project_dir: str  # the workspace's Data/Tools/BodySlide (Outfit Studio's ProjectPath)
    runtime_folder: str
    exe: str
    timeout_s: int = 3600
    ready_timeout_s: int = 180
    stall_timeout_s: int = 900  # no new step for this long while no dialog is open → TIMEOUT
    close_timeout_s: int = 60
    diagnostics_dir: str = ""  # where screenshots / captured dialogs go

    @property
    def script_path(self) -> Path:
        return Path(self.project_dir) / "Automations" / f"{self.script_name}.xml"


@dataclass
class SessionResult:
    batch_id: str
    session_id: int
    script_id: str
    status: str = AUTOMATION_FAILED
    reason: str = ""
    script_sha256: str = ""
    planned_projects: list[str] = field(default_factory=list)
    processed_projects: list[str] = field(default_factory=list)  # projects whose outputs were saved in this session
    started_projects: list[str] = field(default_factory=list)  # projects whose first step ran
    save_attempts: list[dict] = field(default_factory=list)
    automation_errors: list[dict] = field(default_factory=list)  # {step, step_type, code, project, text}
    dialogs: list[dict] = field(default_factory=list)  # every dialog seen and how it was answered
    cloth: list[dict] = field(default_factory=list)  # cloth-data decisions (project, policy, origins, …)
    suspect_project: str = ""  # the project that was running when the session had to stop
    pid: Optional[int] = None
    exe: str = ""
    exe_sha256: str = ""
    launched_at: Optional[float] = None
    ready_at: Optional[float] = None
    started_at: Optional[float] = None  # Execute confirmed
    finished_at: Optional[float] = None  # completion message seen
    ended_at: Optional[float] = None  # process gone
    completion: dict = field(default_factory=dict)
    close: str = ""  # graceful | forced | not_started
    exit_code: Optional[int] = None
    steps_seen: int = 0
    fallback: str = ""  # what the user can do when the control failed
    output_captured: bool = False  # the Automation window's output pane could be read (step lines)
    automation_output: str = ""  # its text (written to a separate file, not into session_result.json)
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.status in (COMPLETED, COMPLETED_WITH_PROJECT_ERRORS)


@dataclass
class Win:
    """A window as the controller sees it (a native HWND on Windows, an object in the test double)."""
    hwnd: int
    title: str = ""
    cls: str = ""
    visible: bool = True
    enabled: bool = True
    ctrl_id: int = 0
    style: int = 0
    parent: int = 0


class UIDriver:
    """What the controller needs from the platform. :mod:`win32_ui` implements it with plain Win32 window messages
    (no simulated keyboard/mouse input, no screen coordinates); the tests implement it with a model of Outfit Studio's
    5.8.2 windows. Every method returns ``None``/``False`` instead of raising when the window is gone or busy."""

    name = "abstract"

    def launch(self, exe: Path, cwd: Path):  # -> process handle with ``pid``
        raise NotImplementedError

    def poll(self, proc) -> Optional[int]:
        raise NotImplementedError

    def kill(self, proc) -> None:
        raise NotImplementedError

    def wait_idle(self, proc, timeout_ms: int) -> bool:
        return True

    def top_windows(self, pid: int) -> list[Win]:
        raise NotImplementedError

    def children(self, hwnd: int) -> list[Win]:
        raise NotImplementedError

    def text(self, hwnd: int) -> Optional[str]:
        raise NotImplementedError

    def menu_command_id(self, hwnd: int, label_prefix: str) -> Optional[int]:
        raise NotImplementedError

    def post_command(self, hwnd: int, cmd_id: int) -> bool:
        raise NotImplementedError

    def click_button(self, btn: Win) -> bool:
        raise NotImplementedError

    def listview_count(self, hwnd: int) -> Optional[int]:
        raise NotImplementedError

    def listbox_items(self, hwnd: int) -> Optional[list[str]]:
        raise NotImplementedError

    def listbox_check(self, hwnd: int, index: int) -> bool:
        raise NotImplementedError

    def combo_select(self, combo: Win, text: str) -> bool:
        raise NotImplementedError

    def dialog_buttons(self, hwnd: int) -> list[dict]:
        """Every visible button of a dialog, in window order: {"hwnd", "id", "text" (without '&'), "enabled"}.
        Never keyed by ID: the buttons of a Windows task dialog all have control ID 0."""
        raise NotImplementedError

    def press_dialog_button(self, dlg: Win, button: dict, task_id: int) -> bool:
        """Invoke one identified button of a native dialog: ``TDM_CLICK_BUTTON task_id`` for a task dialog, the
        button's own ID for a classic message box."""
        raise NotImplementedError

    def close_window(self, hwnd: int) -> bool:
        raise NotImplementedError

    def screenshot(self, hwnd: int, path: Path) -> bool:
        return False


# --------------------------------------------------------------------------------------------- helpers
def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_main_title(title: str) -> bool:
    """'Outfit Studio', or '<project>[*] - Outfit Studio' once something is loaded (OutfitStudioFrame::UpdateTitle)."""
    t = (title or "").strip()
    return t == MAIN_TITLE or t.endswith(" - " + MAIN_TITLE)


def _norm(p: str) -> str:
    return str(p).strip().strip('"').replace("\\", "/").rstrip("/").lower()


def write_last_script(runtime_folder: Path, script_name: str) -> Path:
    """Set ``AutomationDialog.lastScript`` in the private runtime's ``OutfitStudio.xml`` (other settings kept)."""
    p = Path(runtime_folder) / "OutfitStudio.xml"
    root = None
    if p.is_file():
        try:
            root = ET.fromstring(p.read_bytes())
        except ET.ParseError:
            root = None
    if root is None or root.tag != "OutfitStudioConfig":
        root = ET.Element("OutfitStudioConfig")
    el = root.find("AutomationDialog")
    if el is None:
        el = ET.SubElement(root, "AutomationDialog")
    el.set("lastScript", script_name)
    tmp = p.with_name(p.name + ".tmp")
    ET.ElementTree(root).write(tmp, encoding="utf-8", xml_declaration=False)
    os.replace(tmp, p)
    return p


def read_last_script(runtime_folder: Path) -> str:
    try:
        root = ET.fromstring((Path(runtime_folder) / "OutfitStudio.xml").read_bytes())
    except (OSError, ET.ParseError):
        return ""
    el = root.find("AutomationDialog")
    return (el.get("lastScript") or "") if el is not None else ""


def verify_session_script(spec: SessionSpec) -> list[str]:
    """Every reason not to execute this session's script ([] = it is exactly the script the batch generated)."""
    problems: list[str] = []
    expected_name = session_script_name(spec.batch_id, spec.session)
    if spec.script_name != expected_name:
        problems.append(f"script name '{spec.script_name}' is not this batch/session's name '{expected_name}'")
    p = spec.script_path
    if not p.is_file():
        return problems + [f"script {p.name} does not exist in the workspace Automations folder"]
    try:
        h = _sha(p)
    except OSError as exc:
        return problems + [f"script cannot be read: {exc}"]
    if not spec.script_sha256:
        problems.append("no script hash was recorded when the batch wrote the script")
    elif h != spec.script_sha256:
        problems.append("script file differs from the one the batch generated (SHA-256 mismatch)")
    try:
        text = p.read_text(encoding="utf-8")
        ranges = check_batch_invariants(text)
        sc = parse_script(text)
    except (ScriptInvariantError, ET.ParseError, ValueError, OSError) as exc:
        return problems + [f"script is not a valid conversion batch script: {exc}"]
    n_steps = len(sc.steps)
    if n_steps != spec.expected_steps:
        problems.append(f"script has {n_steps} steps, the session planned {spec.expected_steps}")
    segs = split_segments(sc)
    if len(segs) != len(spec.projects):
        problems.append(f"script converts {len(segs)} project(s), the session planned {len(spec.projects)}")
    else:
        for seg, sp, rg in zip(segs, spec.projects, ranges):
            save = next((s for s in seg if s.type == "SaveProject"), None)
            name = str(save.params.get("DisplayName", "")) if save else ""
            if name != sp.generated_project:
                problems.append(f"script part for '{sp.project}' saves '{name}', planned '{sp.generated_project}'")
            if tuple(rg) != tuple(sp.step_range):
                problems.append(f"script part for '{sp.project}' has steps {rg}, planned {tuple(sp.step_range)}")
    if any(not s.active for s in sc.steps):
        problems.append("script has inactive steps")
    return problems


@dataclass
class ClothDecision:
    policy: str  # single_verified_origin_preserved | equivalent_origins_own_selected | ambiguous_not_resolved | unexpected
    select: list[int] = field(default_factory=list)
    action: str = "ok"  # ok | cancel | stop
    project: str = ""
    origins: list[str] = field(default_factory=list)
    source: str = ""
    verification: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _cloth_payloads(path: Path) -> Optional[list]:
    from ..nif.reader import NifError, read_nif

    try:
        return sorted(read_nif(Path(path)).cloth_data)
    except (NifError, OSError, ValueError):
        return None


def decide_cloth(origins: list[str], spec: SessionSpec, saving: Optional[SessionProject]) -> ClothDecision:
    """What to answer in Outfit Studio's 'Choose cloth data' dialog — only what can be proven.

    * A: exactly one origin, and it is the saving project's own staged ShapeData NIF whose content still equals the
      source (SHA-256) and carries the planned number of cloth blocks → keep it.
    * B: several origins, exactly one is the project's own verified file and every other origin's cloth payloads are
      byte-identical to it (so the choice cannot change the output) → keep only the project's own.
    * C: several origins that differ (or cannot be read) → do not guess: *Cancel* (Outfit Studio then writes no cloth
      data, so the validation fails the project and it is never installed); the session continues.
    * D: anything unexpected (no origin, an origin outside the workspace, the saving project unknown, has no cloth
      data of its own, or its file does not verify) → stop the session; the batch isolates the project.
    The saved NIF is verified afterwards in every case (the cloth payloads must equal the source's)."""
    d = ClothDecision(policy="unexpected", action="stop", origins=list(origins))
    if saving is None:
        d.reason = "the project being saved could not be identified"
        return d
    d.project = saving.project
    if not origins:
        d.reason = "the dialog listed no origin"
        return d
    proj = _norm(spec.project_dir)
    outside = [o for o in origins if not _norm(o).startswith(proj + "/")]
    if outside:
        d.reason = f"origin(s) outside this batch's workspace: {outside}"
        return d
    if not saving.cloth_blocks or not saving.source_nif:
        d.reason = "the project being saved has no cloth data of its own"
        return d
    own = [i for i, o in enumerate(origins) if _norm(o) == _norm(saving.source_nif)]
    if len(own) != 1:
        d.reason = f"the project's own ShapeData file is listed {len(own)} time(s)"
        return d
    own_i = own[0]
    src = Path(saving.source_nif)
    try:
        h = _sha(src)
    except OSError as exc:
        d.reason = f"the project's staged ShapeData file cannot be read: {exc}"
        return d
    if not saving.source_nif_sha256 or h != saving.source_nif_sha256:
        d.reason = "the project's staged ShapeData file differs from the source (SHA-256)"
        return d
    own_cloth = _cloth_payloads(src)
    if own_cloth is None or len(own_cloth) != saving.cloth_blocks:
        d.reason = (f"the project's ShapeData file has {0 if own_cloth is None else len(own_cloth)} cloth block(s), "
                    f"planned {saving.cloth_blocks}")
        return d
    d.source = str(src)
    d.verification = (f"workspace copy SHA-256 {h[:16]}… equals the source; {len(own_cloth)} BSClothExtraData block(s): "
                      + ", ".join(f"{n} bytes sha1 {s[:12]}" for n, s in own_cloth))
    if len(origins) == 1:
        d.policy, d.select, d.action = "single_verified_origin_preserved", [own_i], "ok"
        return d
    others = [(i, o) for i, o in enumerate(origins) if i != own_i]
    same = True
    for _, o in others:
        pl = _cloth_payloads(Path(o))
        if pl is None or pl != own_cloth:
            same = False
            break
    if same:
        d.policy, d.select, d.action = "equivalent_origins_own_selected", [own_i], "ok"
        d.verification += f"; {len(others)} other origin(s) carry byte-identical cloth payloads"
        return d
    d.policy, d.select, d.action = "ambiguous_not_resolved", [], "cancel"
    d.reason = CLOTH_AMBIGUOUS_MESSAGE
    return d


# --------------------------------------------------------------------------------------------- controller
class _Stop(Exception):
    def __init__(self, status: str, reason: str, suspect: str = ""):
        super().__init__(reason)
        self.status, self.reason, self.suspect = status, reason, suspect


class SessionController:
    """Runs one Outfit Studio session without user interaction (see the module docstring)."""

    def __init__(self, driver: UIDriver, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 0.25,
                 on_event: Optional[Callable[[dict], None]] = None):
        self.d = driver
        self.clock = clock
        self.sleep = sleep
        self.poll_s = poll_s
        self.on_event = on_event or (lambda e: None)

    # ----------------------------------------------------------------- bookkeeping
    def _ev(self, res: SessionResult, kind: str, **data) -> None:
        e = {"t": round(self.clock(), 3), "kind": kind, **data}
        res.events.append(e)
        try:
            self.on_event(e)
        except Exception:  # noqa: BLE001
            pass

    def _log_text(self, spec: SessionSpec) -> str:
        try:
            return (Path(spec.runtime_folder) / "Log_OS.txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _saved(self, sp: SessionProject, since: float) -> bool:
        try:
            return all(Path(f).is_file() and Path(f).stat().st_mtime >= since - 2 for f in sp.outputs.values())
        except OSError:
            return False

    def _project_at(self, spec: SessionSpec, step: int) -> Optional[SessionProject]:
        for sp in spec.projects:
            if sp.step_range[0] <= step <= sp.step_range[1]:
                return sp
        return None

    # ----------------------------------------------------------------- run
    def run(self, spec: SessionSpec, cancel: Callable[[], bool] = lambda: False) -> SessionResult:
        res = SessionResult(batch_id=spec.batch_id, session_id=spec.session, script_id=spec.script_name,
                            script_sha256=spec.script_sha256, planned_projects=[p.project for p in spec.projects],
                            exe=spec.exe)
        try:
            res.exe_sha256 = _sha(Path(spec.exe))
        except OSError:
            res.exe_sha256 = ""
        problems = verify_session_script(spec)
        if problems:
            res.status, res.reason, res.close = AUTOMATION_FAILED, "; ".join(problems), "not_started"
            self._ev(res, "script_rejected", problems=problems)
            return res
        self._ev(res, "script_verified", sha256=spec.script_sha256, steps=spec.expected_steps,
                 projects=len(spec.projects))
        write_last_script(Path(spec.runtime_folder), spec.script_name)
        if read_last_script(Path(spec.runtime_folder)) != spec.script_name:
            res.status, res.reason, res.close = AUTOMATION_FAILED, "could not preselect the script in OutfitStudio.xml", "not_started"
            return res
        log_file = Path(spec.runtime_folder) / "Log_OS.txt"
        try:
            if log_file.exists():
                log_file.unlink()
        except OSError:
            pass
        self._state = {"step": 0, "total": 0, "output": "", "main": None, "auto": None, "errors_seen": set(),
                       "dialogs": {}, "completed": False}
        proc = None
        try:
            res.launched_at = self.clock()
            proc = self.d.launch(Path(spec.exe), Path(spec.runtime_folder))
            res.pid = getattr(proc, "pid", None)
            self._ev(res, "launched", pid=res.pid)
            self._wait_ready(spec, res, proc, cancel)
            self._open_and_verify(spec, res, proc, cancel)
            self._execute(spec, res, proc, cancel)
            self._monitor(spec, res, proc, cancel)
            self._close(spec, res, proc)
            self._conclude(spec, res)
        except _Stop as st:
            res.status, res.reason = st.status, st.reason
            res.suspect_project = st.suspect or res.suspect_project
            self._ev(res, "stopped", status=st.status, reason=st.reason, suspect=res.suspect_project)
            if proc is not None and self.d.poll(proc) is None:
                if st.status in (AUTOMATION_FAILED,) and res.started_at is None:
                    try:
                        self._close(spec, res, proc)  # nothing ran: close politely
                    except Exception:  # noqa: BLE001
                        self.d.kill(proc)
                        res.close = "forced"
                else:
                    self.d.kill(proc)
                    res.close = "forced"
                    self._ev(res, "killed")
        except Exception as exc:  # noqa: BLE001 - never leave an Outfit Studio running behind the batch
            res.status = OS_CRASHED if res.started_at is not None else AUTOMATION_FAILED
            res.reason = f"session control error: {type(exc).__name__}: {exc}"
            try:
                res.suspect_project = self._current(spec, res)
            except Exception:  # noqa: BLE001
                pass
            self._ev(res, "control_error", error=res.reason)
            if proc is not None and self.d.poll(proc) is None:
                self.d.kill(proc)
                res.close = "forced"
        finally:
            if proc is not None:
                res.exit_code = self.d.poll(proc)
            res.ended_at = self.clock()
            since = res.launched_at or 0
            res.processed_projects = [sp.project for sp in spec.projects if self._saved(sp, since)]
            st = getattr(self, "_state", {})
            res.steps_seen = st.get("step", 0)
            res.output_captured = bool(st.get("output_ctl"))
            res.automation_output = st.get("output", "") or ""
            if res.status in CONTROL_FAILURES:
                res.fallback = (f"continue manually: open Outfit Studio from the converter, File > Open Automation, "
                                f"choose '{spec.script_name}', press Execute")
        return res

    def probe(self, spec: SessionSpec) -> SessionResult:
        """Start Outfit Studio, wait until ready, open the Automation window with the (harmless) probe script
        preselected, verify it exactly, and close again — nothing is executed. Checks the unattended control on a
        real installation in under a minute (CLI ``probe-outfit-studio``)."""
        res = SessionResult(batch_id=spec.batch_id, session_id=spec.session, script_id=spec.script_name,
                            script_sha256=spec.script_sha256, exe=spec.exe)
        self._state = {"step": 0, "total": 0, "output": "", "main": None, "auto": None, "errors_seen": set(),
                       "dialogs": {}, "completed": False}
        write_last_script(Path(spec.runtime_folder), spec.script_name)
        log_file = Path(spec.runtime_folder) / "Log_OS.txt"
        try:
            if log_file.exists():
                log_file.unlink()
        except OSError:
            pass
        proc = None
        try:
            res.launched_at = self.clock()
            proc = self.d.launch(Path(spec.exe), Path(spec.runtime_folder))
            res.pid = getattr(proc, "pid", None)
            self._wait_ready(spec, res, proc, lambda: False)
            self._open_and_verify(spec, res, proc, lambda: False)
            kids = self.d.children(self._state["auto"])
            res.completion = {"probe": "ok", "menu": "found", "output_pane": bool(self._state.get("output_ctl")),
                              "status_bar": bool(self._state.get("status_ctl")),
                              "controls": sorted({k.cls for k in kids})}
            res.status, res.reason = COMPLETED, "probe: Outfit Studio can be controlled (nothing was executed)"
            self._close(spec, res, proc)
        except _Stop as st:
            res.status, res.reason = st.status, st.reason
            if proc is not None and self.d.poll(proc) is None:
                self._close(spec, res, proc)
        finally:
            if proc is not None:
                res.exit_code = self.d.poll(proc)
            res.ended_at = self.clock()
        return res

    # ----------------------------------------------------------------- phases
    def _answer_box(self, spec: SessionSpec, res: SessionResult, w: Win, phase: str) -> Optional[str]:
        """Answer one known native message box. Returns None when done, else why it was NOT answered (nothing is
        pressed then): not a native dialog, unknown title, button set not exactly the expected one, the button to press
        not unique, or the box did not close after the press."""
        spec_box = KNOWN_BOXES.get(w.title)
        if spec_box is None:
            return f"unknown message '{w.title}'"
        expected, action, task_id = spec_box
        if w.cls != "#32770":
            return f"'{w.title}' is not a native message box (class {w.cls!r})"
        btns = [b for b in self.d.dialog_buttons(w.hwnd)]
        texts = sorted(str(b.get("text") or "").replace("&", "").strip() for b in btns)
        if tuple(texts) != tuple(sorted(expected)):
            return f"unexpected buttons in '{w.title}': {texts} (expected {sorted(expected)})"
        target = [b for b in btns if str(b.get("text") or "").replace("&", "").strip() == action]
        if len(target) != 1 or not target[0].get("enabled", True):
            return f"'{action}' in '{w.title}' is not one enabled button"
        if not self.d.press_dialog_button(w, target[0], task_id):
            return f"'{action}' in '{w.title}' could not be pressed"
        if not self._await_gone(res, w):
            return f"'{w.title}' did not close after '{action}'"
        res.dialogs.append({"title": w.title, "answer": action, "phase": phase, "buttons": texts,
                            "t": round(self.clock(), 3)})
        return None

    def _check_alive(self, spec: SessionSpec, res: SessionResult, proc, phase: str) -> None:
        code = self.d.poll(proc)
        if code is not None:
            res.exit_code = code
            raise _Stop(OS_CRASHED if res.started_at else AUTOMATION_FAILED,
                        f"Outfit Studio exited during '{phase}' (exit code {code})", self._current(spec, res))
        log = self._log_text(spec)
        fatal = next((m for m in FATAL_MARKERS if m in log), "")
        if fatal:
            raise _Stop(OS_CRASHED, f"Outfit Studio reported: {fatal}", self._current(spec, res))

    def _current(self, spec: SessionSpec, res: SessionResult) -> str:
        """The project that was running (from the step lines; otherwise the first one not saved yet)."""
        if res.started_at is None:
            return ""
        step = self._state.get("step", 0)
        sp = self._project_at(spec, step) if step else None
        since = res.launched_at or 0
        # the projects run in order: the running one is the first unsaved project at or after the last step seen (the
        # step lines can lag by up to a second, and a crash leaves nothing more to read)
        start = spec.projects.index(sp) if sp is not None else 0
        nxt = next((p for p in spec.projects[start:] if not self._saved(p, since)), None)
        sp = nxt or sp
        return sp.project if sp else ""

    def _dialogs(self, pid: int) -> list[Win]:
        """Visible top-level windows of the Outfit Studio process other than its main and Automation windows."""
        main, auto = self._state.get("main"), self._state.get("auto")
        out = []
        for w in self.d.top_windows(pid):
            if not w.visible or w.hwnd in (main, auto) or w.title == AUTOMATION_TITLE:
                continue
            if main is None and _is_main_title(w.title):
                continue
            if w.cls in ("tooltips_class32", "#32768", "IME", "MSCTFIME UI", "SysShadow", "wxTLWHiddenParent"):
                continue
            if not (w.title or "").strip() and w.cls != "#32770":
                continue
            out.append(w)
        return out

    def _is_modal(self, pid: int, w: Win) -> bool:
        """A dialog that waits for an answer: a native dialog, or one that disabled the window below it (the
        Automation window while it is open — it is itself modal over the main window — otherwise the main window)."""
        if w.cls == "#32770":
            return True
        owner = self._state.get("auto") or self._state.get("main")
        return any(not x.enabled for x in self.d.top_windows(pid) if x.hwnd == owner)

    def _wait_ready(self, spec: SessionSpec, res: SessionResult, proc, cancel) -> None:
        t0 = self.clock()
        while True:
            if cancel():
                raise _Stop(CANCELLED, "cancelled")
            self._check_alive(spec, res, proc, "startup")
            log = self._log_text(spec)
            wins = self.d.top_windows(res.pid)
            main = next((w for w in wins if w.visible and _is_main_title(w.title)), None)
            if main is not None:
                self._state["main"] = main.hwnd
            if all(m in log for m in READY_MARKERS) and main is not None:
                if not _norm(spec.project_dir) in _norm(log):
                    raise _Stop(AUTOMATION_FAILED, "Outfit Studio did not start with this batch's project folder")
                if self.d.wait_idle(proc, 1000):
                    others = [w for w in self._dialogs(res.pid) if self._is_modal(res.pid, w)]
                    if others:
                        w = others[0]
                        self._capture(spec, res, w, "startup")
                        raise _Stop(USER_INTERACTION_REQUIRED, f"Outfit Studio showed '{w.title}' at startup")
                    res.ready_at = self.clock()
                    self._ev(res, "ready", seconds=round(res.ready_at - (res.launched_at or res.ready_at), 2))
                    return
            if self.clock() - t0 > spec.ready_timeout_s:
                others = self._dialogs(res.pid)
                if others:
                    self._capture(spec, res, others[0], "startup")
                raise _Stop(AUTOMATION_FAILED, "Outfit Studio did not become ready in time (window/3D view)")
            self.sleep(self.poll_s)

    def _find_auto(self, pid: int) -> Optional[Win]:
        return next((w for w in self.d.top_windows(pid) if w.visible and w.title == AUTOMATION_TITLE), None)

    def _open_and_verify(self, spec: SessionSpec, res: SessionResult, proc, cancel) -> None:
        main = self._state["main"]
        mid = self.d.menu_command_id(main, MENU_LABEL)
        if mid is None:
            raise _Stop(AUTOMATION_FAILED, f"menu command '{MENU_LABEL}…' not found in the Outfit Studio window")
        if not self.d.post_command(main, mid):
            raise _Stop(AUTOMATION_FAILED, "could not send the menu command to Outfit Studio")
        t0 = self.clock()
        auto = None
        while auto is None:
            self._check_alive(spec, res, proc, "open automation")
            auto = self._find_auto(res.pid)
            if auto is None:
                others = [w for w in self._dialogs(res.pid) if self._is_modal(res.pid, w)]
                if others and self.clock() - t0 > 2:
                    self._capture(spec, res, others[0], "open automation")
                    raise _Stop(USER_INTERACTION_REQUIRED, f"Outfit Studio showed '{others[0].title}' instead of the Automation window")
                if self.clock() - t0 > 30:
                    raise _Stop(AUTOMATION_FAILED, "the Automation window did not open")
                self.sleep(self.poll_s)
        self._state["auto"] = auto.hwnd
        self._ev(res, "automation_window")
        # exact script: the restored lastScript (fallback: select it by exact text in the script box)
        t0 = self.clock()
        selected_via = "lastScript"
        tried_select = False
        while True:
            kids = self.d.children(auto.hwnd)
            combos = [w for w in kids if w.cls.lower() == "combobox" and (w.style & 0x3) == 0x2]
            texts = [(w, self.d.text(w.hwnd)) for w in combos]
            match = [w for w, t in texts if t == spec.script_name]
            lists = [w for w in kids if w.cls == "SysListView32"]
            counts = [self.d.listview_count(w.hwnd) for w in lists]
            buttons = [w for w in kids if w.cls.lower() == "button" and (self.d.text(w.hwnd) or "").replace("&", "") == EXECUTE_LABEL]
            if match and spec.expected_steps in counts and len(buttons) == 1 and buttons[0].enabled:
                break
            if not match and combos and not tried_select and self.clock() - t0 > 3:
                tried_select = True
                selected_via = "exact text selection"
                for w in combos:
                    self.d.combo_select(w, spec.script_name)
            if self.clock() - t0 > 20:
                shown = [t for _, t in texts]
                raise _Stop(AUTOMATION_FAILED, f"the Automation window does not show the session script exactly "
                                               f"(script box {shown!r}, step list {counts}, planned "
                                               f"{spec.expected_steps} steps, Execute buttons {len(buttons)})")
            self.sleep(self.poll_s)
        self._state["execute"] = buttons[0]
        outs = [w for w in kids if (w.cls.lower().startswith("richedit") or w.cls.lower() == "edit")
                and (w.style & 0x0804) == 0x0804]  # ES_MULTILINE | ES_READONLY: the output pane (wxTE_RICH)
        self._state["output_ctl"] = next((w.hwnd for w in outs if w.cls.lower().startswith("richedit")),
                                         outs[0].hwnd if outs else None)
        self._state["status_ctl"] = next((w.hwnd for w in kids if w.cls == "msctls_statusbar32"), None)
        if _sha(spec.script_path) != spec.script_sha256:
            raise _Stop(AUTOMATION_FAILED, "the script file changed while Outfit Studio was starting")
        self._ev(res, "script_selected", via=selected_via, steps=spec.expected_steps)

    def _execute(self, spec: SessionSpec, res: SessionResult, proc, cancel) -> None:
        if cancel():
            raise _Stop(CANCELLED, "cancelled")
        if not self.d.click_button(self._state["execute"]):
            raise _Stop(AUTOMATION_FAILED, "could not press Execute")
        t0 = self.clock()
        while True:
            self._check_alive(spec, res, proc, "confirm execution")
            dl = self._dialogs(res.pid)
            conf = next((w for w in dl if w.title == T_CONFIRM), None)
            if conf is not None:
                # script name, SHA-256 (twice), step count and part order were verified before Execute was pressed
                why = self._answer_box(spec, res, conf, "confirm execution")
                if why is not None:
                    self._capture(spec, res, conf, "confirm execution")
                    raise _Stop(AUTOMATION_FAILED, why)
                res.started_at = self.clock()
                self._ev(res, "execution_started")
                return
            other = next((w for w in dl if w.title != T_CONFIRM and self._is_modal(res.pid, w)), None)
            if other is not None and self.clock() - t0 > 2:
                self._capture(spec, res, other, "execute")
                raise _Stop(USER_INTERACTION_REQUIRED, f"Outfit Studio showed '{other.title}' instead of '{T_CONFIRM}'")
            if self.clock() - t0 > 30:
                raise _Stop(AUTOMATION_FAILED, f"'{T_CONFIRM}' did not appear after Execute")
            self.sleep(self.poll_s)

    def _read_output(self, spec: SessionSpec, res: SessionResult, force: bool = False) -> None:
        h = self._state.get("output_ctl")
        if not h:
            return
        now = self.clock()
        if not force and now - self._state.get("output_read_at", -1e9) < min(1.0, 10 * self.poll_s):
            return
        self._state["output_read_at"] = now
        txt = self.d.text(h)
        if txt is None or txt == self._state["output"]:
            return
        self._state["output"] = txt
        steps = [(int(a), int(b)) for a, b in _STEP.findall(txt)]
        if steps:
            step, total = steps[-1]
            if step != self._state["step"]:
                sp = self._project_at(spec, step)
                if sp is not None and sp.project not in res.started_projects:
                    res.started_projects.append(sp.project)
                    self._ev(res, "project_started", project=sp.project, step=step)
            self._state["step"], self._state["total"] = step, total
        for m in _STEP_FAILED.finditer(txt):
            key = (int(m.group(1)), m.group(2))
            if key in self._state["errors_seen"]:
                continue
            self._state["errors_seen"].add(key)
            sp = self._project_at(spec, int(m.group(1)))
            res.automation_errors.append({"step": int(m.group(1)), "step_type": m.group(2), "code": int(m.group(3)),
                                          "project": sp.project if sp else "", "text": m.group(0)})

    def _saving_project(self, spec: SessionSpec, res: SessionResult) -> Optional[SessionProject]:
        sp = self._project_at(spec, self._state.get("step", 0)) if self._state.get("step") else None
        if sp is not None:
            return sp
        cloth = [p for p in spec.projects if p.cloth_blocks]
        return cloth[0] if len(cloth) == 1 else None

    def _monitor(self, spec: SessionSpec, res: SessionResult, proc, cancel) -> None:
        last_progress = self.clock()
        last_step = 0
        unknown_since: dict[int, float] = {}
        while True:
            if cancel():
                raise _Stop(CANCELLED, "cancelled", self._current(spec, res))
            self._check_alive(spec, res, proc, "execution")
            self._read_output(spec, res)
            if self._state["step"] != last_step:
                last_step, last_progress = self._state["step"], self.clock()
            if self.clock() - self._state.get("saved_at", -1e9) >= 2.0:  # saved outputs are progress too
                self._state["saved_at"] = self.clock()
                n_saved = sum(1 for sp in spec.projects if self._saved(sp, res.launched_at or 0))
                if n_saved != self._state.get("n_saved", 0):
                    self._state["n_saved"], last_progress = n_saved, self.clock()
            dl = self._dialogs(res.pid)
            for w in dl:
                if w.title == T_ERROR:
                    self._read_output(spec, res, force=True)
                    step = self._state["step"]
                    sp = self._project_at(spec, step)
                    if not any(e["step"] == step for e in res.automation_errors):
                        res.automation_errors.append({"step": step, "step_type": "", "code": None,
                                                      "project": sp.project if sp else "",
                                                      "text": "Automation Error dialog"})
                    why = self._answer_box(spec, res, w, f"automation error at step {step}")
                    if why is not None:
                        self._capture(spec, res, w, "automation error")
                        raise _Stop(USER_INTERACTION_REQUIRED, why, sp.project if sp else self._current(spec, res))
                    last_progress = self.clock()
                elif w.title == T_CLOTH:
                    self._cloth(spec, res, w)
                    last_progress = self.clock()
                elif w.title in T_INFO_ERRORS:
                    step = self._state["step"]
                    sp = self._project_at(spec, step)
                    res.automation_errors.append({"step": step, "step_type": "", "code": None,
                                                  "project": sp.project if sp else "", "text": f"'{w.title}' message"})
                    self._capture(spec, res, w, "info", stop=False)
                    why = self._answer_box(spec, res, w, f"message at step {step}")
                    if why is not None:
                        raise _Stop(USER_INTERACTION_REQUIRED, why, sp.project if sp else self._current(spec, res))
                    last_progress = self.clock()
                elif w.title == T_DONE:
                    self._read_output(spec, res, force=True)
                    status = self.d.text(self._state["status_ctl"]) if self._state.get("status_ctl") else None
                    if status is not None and "complete" not in status.lower():
                        self._capture(spec, res, w, "completion")
                        raise _Stop(AUTOMATION_FAILED, f"'{T_DONE}' message while the status bar says {status!r}",
                                    self._current(spec, res))
                    why = self._answer_box(spec, res, w, "completion")
                    if why is not None:
                        self._capture(spec, res, w, "completion")
                        raise _Stop(AUTOMATION_FAILED, why, "")
                    res.finished_at = self.clock()
                    res.completion = {"message": T_DONE, "status_bar": status, "last_step": self._state["step"],
                                      "total_steps": self._state["total"],
                                      "output_captured": bool(self._state.get("output_ctl"))}
                    self._ev(res, "execution_finished", **res.completion)
                    return
                else:
                    if not self._is_modal(res.pid, w):
                        if w.hwnd not in unknown_since:
                            unknown_since[w.hwnd] = self.clock()
                            self._ev(res, "window_ignored", title=w.title, cls=w.cls)
                        continue
                    first = unknown_since.setdefault(w.hwnd, self.clock())
                    if self.clock() - first >= 1.5:
                        self._capture(spec, res, w, "execution")
                        raise _Stop(USER_INTERACTION_REQUIRED, f"unexpected Outfit Studio message '{w.title}'",
                                    self._current(spec, res))
            now = self.clock()
            if res.started_at is not None and now - res.started_at > spec.timeout_s:
                raise _Stop(TIMEOUT, f"the session did not finish within {spec.timeout_s}s", self._current(spec, res))
            if not dl and now - last_progress > spec.stall_timeout_s:
                raise _Stop(TIMEOUT, f"no progress for {spec.stall_timeout_s}s", self._current(spec, res))
            self.sleep(self.poll_s)

    def _await_gone(self, res: SessionResult, w: Win, timeout: float = 10.0) -> bool:
        t0 = self.clock()
        while any(x.hwnd == w.hwnd for x in self.d.top_windows(res.pid) if x.visible):
            if self.clock() - t0 > timeout:
                return False
            self.sleep(self.poll_s)
        return True

    def _cloth(self, spec: SessionSpec, res: SessionResult, w: Win) -> None:
        self._read_output(spec, res, force=True)
        kids = self.d.children(w.hwnd)
        lb = next((k for k in kids if k.cls.lower() == "listbox"), None)
        items = self.d.listbox_items(lb.hwnd) if lb is not None else None
        saving = self._saving_project(spec, res)
        if items is None:
            dec = ClothDecision(policy="unexpected", action="stop", project=saving.project if saving else "",
                                reason="the cloth data origins could not be read")
        else:
            dec = decide_cloth(items, spec, saving)
        def _btn(cid: int, label: str):
            # identity = control ID *and* label *and* enabled; never position or order (0.5.3 targeted acceptance)
            m = [k for k in kids if k.cls.lower() == "button" and k.ctrl_id == cid and k.visible]
            if len(m) != 1 or not m[0].enabled or (self.d.text(m[0].hwnd) or "").replace("&", "").strip() != label:
                return None
            return m[0]
        ok, cn = _btn(WX_ID_OK, "OK"), _btn(WX_ID_CANCEL, "Cancel")
        lbs = [k for k in kids if k.cls.lower() == "listbox"]
        if dec.action != "stop" and (ok is None or cn is None):
            dec.action, dec.policy, dec.reason = "stop", "unexpected", "the dialog has no OK (5100) / Cancel (5101) buttons"
        elif dec.action != "stop" and len(lbs) != 1:
            dec.action, dec.policy, dec.reason = "stop", "unexpected", f"the dialog has {len(lbs)} list boxes, expected 1"
        rec = dec.to_dict()
        rec["step"] = self._state.get("step", 0)
        rec["dialog"] = self._hierarchy(w, kids)
        rec["offered_origins"] = len(items or [])
        rec["selected_origins"] = [items[i] for i in dec.select] if items and dec.action == "ok" else []
        res.cloth.append(rec)
        res.save_attempts.append({"project": dec.project, "step": rec["step"], "cloth_dialog": True})
        self._ev(res, "cloth_dialog", **{k: rec[k] for k in ("project", "policy", "origins", "action")})
        if dec.action == "stop":
            self._capture(spec, res, w, "cloth data")
            raise _Stop(USER_INTERACTION_REQUIRED, f"cloth physics data could not be selected automatically: {dec.reason}",
                        dec.project or self._current(spec, res))
        if dec.action == "ok":
            for i in dec.select:
                if not self.d.listbox_check(lb.hwnd, i):
                    self._capture(spec, res, w, "cloth data")
                    raise _Stop(USER_INTERACTION_REQUIRED, "could not tick the cloth data origin", dec.project)
            self.d.click_button(ok)
            res.dialogs.append({"title": T_CLOTH, "answer": f"OK with {len(dec.select)} origin(s)", "project": dec.project,
                                "policy": dec.policy})
        else:
            self.d.click_button(cn)
            res.dialogs.append({"title": T_CLOTH, "answer": "Cancel (no cloth data written)", "project": dec.project,
                                "policy": dec.policy})
        gone = self._await_gone(res, w)
        rec["dialog"]["closed_after_confirmation"] = gone
        if not gone:
            self._capture(spec, res, w, "cloth data")
            raise _Stop(USER_INTERACTION_REQUIRED, "the cloth data dialog did not close", dec.project)

    def _hierarchy(self, w: Win, kids: Optional[list] = None) -> dict:
        """The dialog's real window/control hierarchy (class, control ID, parent, text, state) — always recorded for the
        cloth dialog so a real run documents what Outfit Studio showed, not only a failing one."""
        info: dict = {"title": w.title, "class": w.cls, "hwnd": w.hwnd}
        try:
            kids = self.d.children(w.hwnd) if kids is None else kids
            info["controls"] = [{"hwnd": k.hwnd, "parent": k.parent, "class": k.cls, "id": k.ctrl_id,
                                 "text": (self.d.text(k.hwnd) or "")[:300], "visible": k.visible, "enabled": k.enabled}
                                for k in kids[:60]]
            info["buttons"] = self.d.dialog_buttons(w.hwnd)
            info["selection_controls"] = [{"class": k.cls, "id": k.ctrl_id, "style": hex(k.style or 0),
                                           "items": self.d.listbox_items(k.hwnd)}
                                          for k in kids if k.cls.lower() == "listbox"]
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break the control flow
            info["capture_error"] = str(exc)
        return info

    def _close(self, spec: SessionSpec, res: SessionResult, proc) -> None:
        if self.d.poll(proc) is not None:
            res.close = res.close or "exited"
            return
        auto = self._state.get("auto")
        t0 = self.clock()
        if auto:
            self.d.close_window(auto)
            while self._find_auto(res.pid) is not None and self.clock() - t0 < 15:
                self.sleep(self.poll_s)
        main = self._state.get("main")
        if main:
            self.d.close_window(main)
        answered = False
        captured: set[int] = set()
        while self.d.poll(proc) is None and self.clock() - t0 < spec.close_timeout_s:
            for w in self._dialogs(res.pid):
                if w.title == T_UNSAVED and not answered:
                    answered = True
                    why = self._answer_box(spec, res, w, "closing")
                    if why is not None:
                        captured.add(w.hwnd)
                        self._capture(spec, res, w, "closing", stop=False)
                elif w.title != T_UNSAVED and w.hwnd not in captured:
                    captured.add(w.hwnd)
                    self._capture(spec, res, w, "closing", stop=False)
            self.sleep(self.poll_s)
        if self.d.poll(proc) is None:
            self.d.kill(proc)
            res.close = "forced"
            self._ev(res, "killed", why="did not exit after closing")
        else:
            res.close = res.close if res.close == "forced" else "graceful"
        self._ev(res, "closed", how=res.close)

    def _conclude(self, spec: SessionSpec, res: SessionResult) -> None:
        since = res.launched_at or 0
        saved = [sp.project for sp in spec.projects if self._saved(sp, since)]
        total, last = self._state.get("total") or 0, self._state.get("step") or 0
        if res.finished_at is None:
            res.status, res.reason = AUTOMATION_FAILED, "the completion message was not seen"
            return
        if total and (total != spec.expected_steps or last != spec.expected_steps):
            res.status = AUTOMATION_FAILED
            res.reason = f"Outfit Studio reported step {last}/{total}; the script has {spec.expected_steps} steps"
            return
        bits = []
        if len(saved) != len(spec.projects):
            bits.append(f"{len(spec.projects) - len(saved)} project(s) not saved")
        if res.automation_errors:
            bits.append(f"{len(res.automation_errors)} automation error(s)")
        if any(c.get("action") != "ok" for c in res.cloth):
            bits.append("cloth data not selected for a project (" + CLOTH_AMBIGUOUS_MESSAGE + ")")
        res.status = COMPLETED_WITH_PROJECT_ERRORS if bits else COMPLETED
        res.reason = "; ".join(bits)

    # ----------------------------------------------------------------- diagnostics
    def _capture(self, spec: SessionSpec, res: SessionResult, w: Win, phase: str, stop: bool = True) -> None:
        info = {"phase": phase, "title": w.title, "class": w.cls, "t": round(self.clock(), 3)}
        try:
            kids = self.d.children(w.hwnd)
            info["controls"] = [{"class": k.cls, "id": k.ctrl_id, "text": (self.d.text(k.hwnd) or "")[:300]}
                                for k in kids[:40] if k.cls.lower() in ("button", "static", "edit", "listbox")]
            lb = next((k for k in kids if k.cls.lower() == "listbox"), None)
            if lb is not None:
                info["list_items"] = self.d.listbox_items(lb.hwnd)
            info["buttons"] = self.d.dialog_buttons(w.hwnd)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break the control flow
            info["capture_error"] = str(exc)
        if spec.diagnostics_dir:
            try:
                p = Path(spec.diagnostics_dir) / f"session{spec.session}_{phase.replace(' ', '_')}_{len(res.dialogs)}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                if self.d.screenshot(w.hwnd, p):
                    info["screenshot"] = str(p)
            except Exception:  # noqa: BLE001
                pass
        res.dialogs.append({"title": w.title, "answer": "none (stopped)" if stop else "recorded", "capture": info})


def write_session_result(res: SessionResult, path: Path) -> Path:
    """``session_result.json``: the completion handshake of one session (the output pane text is kept separately)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    d = res.to_dict()
    d.pop("automation_output", None)
    d["schema"] = 1
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path
