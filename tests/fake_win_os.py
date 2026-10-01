"""A model of Outfit Studio 5.8.2's Windows GUI for the unattended control (0.5.3), as a :class:`UIDriver`.

It reproduces what the controller relies on, from the 5.8.2 source (OutfitStudio.cpp, AutomationDialog*.cpp,
OutfitProject::ChooseClothData) and the real Windows log:

* ``Log_OS.txt``: "Outfit Studio initialized." then "OpenGL Context Info:" once the window is up;
* the main window "Outfit Studio" with the native menu item "Open Automation...\\tCtrl+Shift+A";
* the "Automation Script" window restores ``AutomationDialog.lastScript`` from the runtime's ``OutfitStudio.xml``
  (script box = that name, step list = its steps), has one "Execute" button, a read-only rich edit output pane and a
  status bar;
* Execute → task dialog "Confirm Execution" (Yes/No) → the steps run one after the other, each logged as
  "Automation: Step i/N: <type>" in the output pane (not in Log_OS.txt);
* at every SaveProject after cloth data was loaded in this process: the wx dialog "Choose cloth data" (check list box
  of origins, OK = 5100 / Cancel = 5101; nothing ticked by default); a failing step: task dialog "Automation Error"
  (Yes = continue);
* at the end: "Automation complete." in the status bar and the task dialog "Automation" (OK); closing the main window
  may ask "Unsaved Changes" (Yes/No/Cancel).

The conversions themselves are done by :class:`fake_os.FakeOutfitStudio` (same outputs as the other tests).
``faults`` inject failures: ``no_ready``, ``menu_missing``, ``ignore_last_script``, ``preselect_other``,
``combo_select_fails``, ``no_output_pane``, ``unsaved_prompt``, ``refuse_close``, ``crash_before_execute``, and per
project (source set name → fault): ``crash``, ``hang``, ``unknown_dialog``, ``step_error``, ``extra_cloth_origin``
(another file with different cloth data is listed too), ``equivalent_cloth_origin`` (another file with the same cloth
payload is listed too). ``fail_first_n`` limits a per-project fault to its first n occurrences (a retry then works).
"""
from __future__ import annotations

import itertools
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from cbbe3bbb.conversion.automation import parse_script
from cbbe3bbb.conversion.os_control import IDCANCEL, IDNO, IDOK, IDYES, UIDriver, Win
from fake_os import FakeOutfitStudio, windows_session_log

MENU_ID = 0x7E21
ES_MULTILINE, ES_READONLY, CBS_DROPDOWN = 0x0004, 0x0800, 0x0002


class _Killed(Exception):
    pass


class _W:
    def __init__(self, hwnd, title="", cls="", kind="", parent=0, ctrl_id=0, style=0, text="", buttons=None):
        self.win = Win(hwnd=hwnd, title=title, cls=cls, parent=parent, ctrl_id=ctrl_id, style=style)
        self.kind = kind
        self.text = text
        self.buttons = buttons or {}
        self.kids: list[int] = []
        self.items: list[str] = []
        self.checked: set[int] = set()
        self.cursel = -1
        self.count = 0
        self.answer = None  # for modal dialogs: the pressed button
        self.event = threading.Event()


class FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.alive = True
        self.exit_code: Optional[int] = None


class FakeWinOS(UIDriver):
    name = "fake-win32"
    poll_s = 0.01

    def __init__(self, conv: Optional[FakeOutfitStudio] = None, faults: Optional[dict] = None,
                 fail_first_n: Optional[int] = None, step_delay: float = 0.002):
        self.conv = conv or FakeOutfitStudio("windows_interactive")
        self.faults = dict(faults or {})
        self.fail_first_n = fail_first_n
        self.fault_hits: dict[str, int] = {}
        self.step_delay = step_delay
        self.lock = threading.RLock()
        self.ids = itertools.count(1000)
        self.wins: dict[int, _W] = {}
        self.proc: Optional[FakeProc] = None
        self.pids = itertools.count(4000)
        self.launches = 0
        self.executed_scripts: list[str] = []
        self.cloth_dialogs: list[list[str]] = []
        self.pressed: list[tuple[str, int]] = []
        self.runtime: Optional[Path] = None
        self.project: Optional[Path] = None
        self.extra_origin_files: list[Path] = []

    def __call__(self, runtime_folder, exe, script_name, timeout_s, cancel=None, interactive=False):
        """The manual fallback: the user runs the script in Outfit Studio themselves (the plain fake Outfit Studio)."""
        self.manual_runs = getattr(self, "manual_runs", 0) + 1
        self.conv.cloth_chooser = None
        return self.conv(runtime_folder, exe, script_name, timeout_s, cancel, interactive)

    # ----------------------------------------------------------------- model helpers
    def _new(self, **kw) -> _W:
        w = _W(next(self.ids), **kw)
        self.wins[w.win.hwnd] = w
        if w.win.parent and w.win.parent in self.wins:
            self.wins[w.win.parent].kids.append(w.win.hwnd)
        return w

    def _remove(self, hwnd):
        w = self.wins.pop(hwnd, None)
        if w is None:
            return
        for k in list(w.kids):
            self._remove(k)
        w.event.set()

    def _fault(self, project: str) -> str:
        f = self.faults.get(project, "")
        if not f:
            return ""
        n = self.fault_hits.get(project, 0)
        if self.fail_first_n is not None and n >= self.fail_first_n:
            return ""
        self.fault_hits[project] = n + 1
        return f

    def _log(self, extra: list[str] = (), closed=False):
        t = time.time()
        text = windows_session_log(self.runtime, self.project, self.project.parent.parent, self._t0, t, list(extra))
        if not closed:
            text = "\r\n".join(ln for ln in text.split("\r\n") if "frame closed" not in ln)
        if "no_ready" in self.faults:
            text = "\r\n".join(ln for ln in text.split("\r\n") if "OpenGL Context Info" not in ln)
        (self.runtime / "Log_OS.txt").write_text(text, encoding="utf-8")

    def _check(self, proc: FakeProc):
        if not proc.alive:
            raise _Killed()

    def _modal(self, title: str, buttons: dict, cls="#32770", kind="task", owner=0) -> _W:
        """A wxMessageBox as Windows shows it (observed on the real 5.8.2 run): a task dialog #32770 whose buttons are
        Button windows under DirectUIHWND > CtrlNotifySink, ALL with control ID 0 and texts like '&Yes'.
        ``buttons`` maps the task-dialog button ID (as wx sets it) to its label; an OK-only box is {IDCANCEL: "OK"}."""
        with self.lock:
            w = self._new(title=title, cls=cls, kind=kind, buttons=buttons)
            host = self._new(cls="DirectUIHWND", parent=w.win.hwnd)
            for bid, label in buttons.items():
                sink = self._new(cls="CtrlNotifySink", parent=host.win.hwnd)
                self._new(cls="Button", parent=sink.win.hwnd, ctrl_id=0, text=("&" + label) if label != "OK" else label)
            for oh in (self.main, self.auto):
                if oh in self.wins:
                    self.wins[oh].win.enabled = False
        return w

    def _wait(self, w: _W, proc: FakeProc):
        while not w.event.wait(0.005):
            self._check(proc)
        self._check(proc)
        with self.lock:
            for oh in (self.main, self.auto):
                if oh in self.wins:
                    self.wins[oh].win.enabled = True
        return w.answer

    # ----------------------------------------------------------------- process
    def launch(self, exe: Path, cwd: Path):
        self.launches += 1
        self.runtime = Path(cwd)
        cfg = ET.parse(self.runtime / "Config.xml").getroot()
        self.project = Path(cfg.findtext("ProjectPath"))
        self._t0 = time.time()
        proc = FakeProc(next(self.pids))
        self.proc = proc
        self.conv._cloth = {}  # a new process: Outfit Studio's cloth data list starts empty
        self.main = self.auto = None
        if "crash_on_start" in self.faults:
            proc.alive, proc.exit_code = False, 3
            return proc
        with self.lock:
            m = self._new(title="Outfit Studio", cls="wxWindowNR", kind="main")
            self.main = m.win.hwnd
        self._log()
        return proc

    def poll(self, proc) -> Optional[int]:
        return None if proc.alive else proc.exit_code

    def kill(self, proc) -> None:
        with self.lock:
            proc.alive, proc.exit_code = False, 1
            for h in list(self.wins):
                self._remove(h)

    def _exit(self, proc, code=0):
        with self.lock:
            self._log(closed=True)
            proc.alive, proc.exit_code = False, code
            for h in list(self.wins):
                self._remove(h)

    # ----------------------------------------------------------------- windows
    def top_windows(self, pid: int) -> list[Win]:
        with self.lock:
            if self.proc is None or pid != self.proc.pid or not self.proc.alive:
                return []
            return [Win(**vars(w.win)) for w in self.wins.values() if not w.win.parent]

    def children(self, hwnd: int) -> list[Win]:
        with self.lock:
            out, stack = [], list(self.wins.get(hwnd, _W(0)).kids)
            while stack:
                h = stack.pop(0)
                w = self.wins.get(h)
                if w is None:
                    continue
                out.append(Win(**vars(w.win)))
                stack = list(w.kids) + stack
            return out

    def text(self, hwnd: int) -> Optional[str]:
        with self.lock:
            w = self.wins.get(hwnd)
            if w is None:
                return None
            if w.kind == "output" and "no_output_pane" in self.faults:
                return None
            return w.text

    def menu_command_id(self, hwnd: int, label_prefix: str) -> Optional[int]:
        if "menu_missing" in self.faults or hwnd != self.main:
            return None
        return MENU_ID if "Open Automation...\tCtrl+Shift+A".startswith(label_prefix) else None

    def post_command(self, hwnd: int, cmd_id: int) -> bool:
        if hwnd != self.main or cmd_id != MENU_ID or self.proc is None or not self.proc.alive:
            return False
        threading.Thread(target=self._open_automation, args=(self.proc,), daemon=True).start()
        return True

    def _script_steps(self, name: str) -> Optional[list]:
        p = self.project / "Automations" / f"{name}.xml"
        if not p.is_file():
            return None
        return parse_script(p.read_text(encoding="utf-8")).steps

    def _open_automation(self, proc):
        time.sleep(0.01)
        with self.lock:
            if not proc.alive:
                return
            a = self._new(title="Automation Script", cls="wxWindowNR", kind="auto")
            self.auto = a.win.hwnd
            name = ""
            try:
                el = ET.parse(self.runtime / "OutfitStudio.xml").getroot().find("AutomationDialog")
                name = el.get("lastScript", "") if el is not None else ""
            except (OSError, ET.ParseError):
                pass
            names = sorted(p.stem for p in (self.project / "Automations").glob("*.xml"))
            if "ignore_last_script" in self.faults:
                name = ""
            if "preselect_other" in self.faults:
                name = next((n for n in names if n != name), "") or "cbbe3bbb_batch_other_1"
            steps = self._script_steps(name) if name else None
            panel = self._new(cls="wxWindowNR", parent=a.win.hwnd, kind="panel")
            self._new(cls="ComboBox", parent=panel.win.hwnd, kind="combo", style=CBS_DROPDOWN,
                      text=name if steps is not None or "preselect_other" in self.faults else "")
            self._new(cls="ComboBox", parent=panel.win.hwnd, kind="choice", style=0x3, text="ClearProject")
            lv = self._new(cls="SysListView32", parent=panel.win.hwnd, kind="steps")
            lv.count = len(steps or []) + (1 if "step_count_off" in self.faults else 0)
            for label in ("Save", "Delete", "Open Folder", "Execute", "Close"):
                self._new(cls="Button", parent=panel.win.hwnd, kind="button", text=label)
            self._new(cls="RICHEDIT50W", parent=panel.win.hwnd, kind="output", style=ES_MULTILINE | ES_READONLY, text="")
            self._new(cls="msctls_statusbar32", parent=a.win.hwnd, kind="status", text="Ready.")
            a.loaded = name if steps is not None else ""
            self.wins[self.main].win.enabled = False  # the Automation window is modal

    def _kid(self, kind: str) -> Optional[_W]:
        a = self.wins.get(self.auto)
        if a is None:
            return None
        for h in [x.hwnd for x in self.children(self.auto)]:
            w = self.wins.get(h)
            if w is not None and w.kind == kind:
                return w
        return None

    def click_button(self, btn: Win) -> bool:
        with self.lock:
            w = self.wins.get(btn.hwnd)
            if w is None:
                return False
            parent = self.wins.get(w.win.parent)
            if parent is not None and parent.kind == "cloth":  # wx dialog buttons
                parent.answer = w.win.ctrl_id
                parent.event.set()
                self.pressed.append((parent.win.title, w.win.ctrl_id))
                return True
            if w.text == "Execute" and self.auto in self.wins:
                if "crash_before_execute" in self.faults:
                    threading.Thread(target=self._exit, args=(self.proc, 3), daemon=True).start()
                    return True
                threading.Thread(target=self._confirm_and_run, args=(self.proc,), daemon=True).start()
                return True
        return False

    def listview_count(self, hwnd: int) -> Optional[int]:
        with self.lock:
            w = self.wins.get(hwnd)
            return w.count if w is not None else None

    def listbox_items(self, hwnd: int) -> Optional[list[str]]:
        with self.lock:
            w = self.wins.get(hwnd)
            return list(w.items) if w is not None else None

    def listbox_check(self, hwnd: int, index: int) -> bool:
        with self.lock:
            w = self.wins.get(hwnd)
            if w is None or not 0 <= index < len(w.items):
                return False
            w.cursel = index
            w.checked.add(index)  # the '+' key: "set checked" (idempotent)
            return True

    def combo_select(self, combo: Win, text: str) -> bool:
        if "combo_select_fails" in self.faults:
            return False
        with self.lock:
            w = self.wins.get(combo.hwnd)
            steps = self._script_steps(text)
            if w is None or steps is None:
                return False
            w.text = text
            lv = self._kid("steps")
            if lv is not None:
                lv.count = len(steps)
            self.wins[self.auto].loaded = text
            return True

    def dialog_buttons(self, hwnd: int) -> list[dict]:
        return [{"hwnd": k.hwnd, "id": k.ctrl_id, "text": (self.text(k.hwnd) or "").replace("&", ""),
                 "enabled": k.enabled} for k in self.children(hwnd) if k.cls.lower() == "button" and k.visible]

    def press_dialog_button(self, dlg: Win, button: dict, task_id: int) -> bool:
        """TDM_CLICK_BUTTON: only a button ID the task dialog really has does anything (like Windows)."""
        with self.lock:
            w = self.wins.get(dlg.hwnd)
            if w is None or not any(k.hwnd == button.get("hwnd") for k in self.children(dlg.hwnd)):
                return False
            self.posted_ids = getattr(self, "posted_ids", []) + [(w.win.title, task_id)]
            if task_id not in w.buttons:
                return True  # posted, but nothing happens
            self.pressed.append((w.win.title, task_id))
            w.answer = task_id
            w.event.set()
            return True

    def close_window(self, hwnd: int) -> bool:
        with self.lock:
            w = self.wins.get(hwnd)
            if w is None:
                return False
            if w.kind == "auto":
                if getattr(w, "running", False):
                    return True
                self._remove(hwnd)
                if self.main in self.wins:
                    self.wins[self.main].win.enabled = True
                return True
            if w.kind == "main":
                threading.Thread(target=self._close_main, args=(self.proc,), daemon=True).start()
                return True
        return False

    def _close_main(self, proc):
        if "refuse_close" in self.faults:
            return
        try:
            if "unsaved_prompt" in self.faults:
                d = self._modal("Unsaved Changes", {IDYES: "Yes", IDNO: "No", IDCANCEL: "Cancel"})
                ans = self._wait(d, proc)
                with self.lock:
                    self._remove(d.win.hwnd)
                if ans != IDNO:
                    return
            self._exit(proc, 0)
        except _Killed:
            pass

    # ----------------------------------------------------------------- execution
    def _confirm_and_run(self, proc):
        try:
            d = self._modal("Confirm Execution", {IDYES: "Yes", IDNO: "No"})
            ans = self._wait(d, proc)
            with self.lock:
                self._remove(d.win.hwnd)
            if ans != IDYES:
                return
            self._run_script(proc)
        except _Killed:
            pass

    def _say(self, line: str):
        with self.lock:
            out = self._kid("output")
            if out is not None:
                out.text += time.strftime("%H:%M:%S: ") + line + "\r\n"

    def _run_script(self, proc):
        a = self.wins[self.auto]
        name = a.loaded
        a.running = True
        self.executed_scripts.append(name)
        steps = self._script_steps(name)
        with self.lock:
            st = self._kid("status")
            if st is not None:
                st.text = "Running automation script..."
        parts, n = [], 0
        for s in steps:
            if s.type == "ClearProject" or not parts:
                parts.append([])
            n += 1
            parts[-1].append((n, s))
        total = n
        conv = self.conv
        conv.cloth_chooser = lambda origins: self._choose_cloth(proc, origins)
        for part in parts:
            ps = [s for _, s in part]
            add = next(s for s in ps if s.type == "AddProject")
            pname = add.params["Set"]
            fault = self._fault(pname)
            pmode = conv.per_project.get(pname, "")
            failed = False
            for i, s in part:
                self._check(proc)
                time.sleep(self.step_delay)
                with self.lock:
                    st = self._kid("status")
                    if st is not None:
                        st.text = f"Step {i}/{total}: {s.type}"
                self._say(f"Automation: Step {i}/{total}: {s.type}")
                if s.type == "CopyBoneWeights":
                    if fault == "crash":
                        self._exit(proc, -1073741819)
                        raise _Killed()
                    if fault == "hang":
                        while True:
                            self._check(proc)
                            time.sleep(0.005)
                    if fault == "unknown_dialog":
                        d = self._modal("Warning", {IDCANCEL: "OK"})
                        self._wait(d, proc)
                        with self.lock:
                            self._remove(d.win.hwnd)
                    if fault == "step_error" or pmode == "step_error":
                        d = self._modal("Automation Error", {IDYES: "Yes", IDNO: "No"})
                        ans = self._wait(d, proc)
                        with self.lock:
                            self._remove(d.win.hwnd)
                        failed = True
                        if ans != IDYES:
                            return self._end("Automation aborted.", proc, done=False)
                if s.type == "SaveProject" and pmode != "not_saved" and not (failed and pmode == "step_error"):
                    # (real 5.8.2 continues after "Yes" and still saves; the fake's per-project step_error of the plain
                    # fake Outfit Studio does not save, the window-level 'step_error' fault does)
                    self._extra_fault = fault
                    conv._convert_part(self.project, ps, pmode)
                    self._extra_fault = ""
        self._end("Automation complete.", proc, done=True)

    def _end(self, status: str, proc, done: bool):
        with self.lock:
            st = self._kid("status")
            if st is not None:
                st.text = status
            self.wins[self.auto].running = False
        if done:
            d = self._modal("Automation", {IDCANCEL: "OK"})
            self._wait(d, proc)
            with self.lock:
                self._remove(d.win.hwnd)

    def _choose_cloth(self, proc, origins: list[str]):
        origins = list(origins)
        extra = getattr(self, "_extra_fault", "")
        if extra in ("extra_cloth_origin", "equivalent_cloth_origin"):
            src = Path(origins[-1])
            other = src.with_name("Other Cloth.nif")
            if extra == "equivalent_cloth_origin":
                shutil.copy2(src, other)
            else:
                from synth import BONE_POS, SynthShape, write_nif
                import numpy as np

                v = np.array([[0.0, 0, 0], [1, 0, 0], [0, 1, 0]])
                write_nif(other, [SynthShape("X", v, np.array([[0, 1, 2]]), ["Pelvis"], np.ones((3, 4), np.float32) * [1, 0, 0, 0],
                                             np.zeros((3, 4), np.uint8))], BONE_POS, cloth=[b"OTHER-CLOTH" * 30])
            self.conv._cloth[str(other)] = self.conv._cloth[origins[-1]] if extra == "equivalent_cloth_origin" else [b"OTHER-CLOTH" * 30]
            origins = sorted(origins + [str(other)])
        self.cloth_dialogs.append(origins)
        with self.lock:
            d = self._new(title="Choose cloth data", cls="wxWindowNR", kind="cloth")
            self._new(cls="Static", parent=d.win.hwnd, text="There was cloth physics data loaded at some point "
                                                             "(BSClothExtraData). Please choose all the origins to use in the output.")
            lb = self._new(cls="ListBox", parent=d.win.hwnd, kind="list")
            lb.items = list(origins)
            lb.cursel = 0
            self._new(cls="Button", parent=d.win.hwnd, ctrl_id=5100,
                      text="&Apply" if extra == "cloth_ok_relabelled" else "OK")
            self._new(cls="Button", parent=d.win.hwnd, ctrl_id=5101, text="Cancel")
            for oh in (self.main, self.auto):
                if oh in self.wins:
                    self.wins[oh].win.enabled = False
        ans = self._wait(d, proc)
        with self.lock:
            chosen = [origins[i] for i in sorted(lb.checked)]
            self._remove(d.win.hwnd)
        if ans == 5100:
            if extra == "cloth_payload_altered":  # Outfit Studio writes different cloth data than the origin's
                for o in chosen:
                    self.conv._cloth[o] = [x[:-1] + b"#" for x in self.conv._cloth.get(o, [])]
            return [o for o in chosen if o in self.conv._cloth]
        return None
