"""Evidence that the prepared Outfit Studio automation actually ran.

Why the log alone is not enough
-------------------------------
In headless mode (``OutfitStudio -a``) Outfit Studio writes one
``Automation: Step i/N …`` line per step to ``Log_OS.txt``. In interactive mode
(File > Open Automation > Execute) Outfit Studio 5.8.2 does not:
``AutomationDialog::StartProgress`` installs a ``wxLogTextCtrl`` as the active
log target for the duration of the run, and ``EndProgress`` restores the old
one. Every step message therefore goes only to the Automation window's output
pane, and ``Log_OS.txt`` shows just the session (start, config, frame
loaded/closed). Requiring step lines in interactive mode turned a real,
successful run into a false FAILED.

What is accepted instead
------------------------
The evidence is the artifacts that only the prepared script produces, checked
against the Outfit Studio session that the tool started. All of these are
required:

1. ``Log_OS.txt`` is from an Outfit Studio session that used *this*
   workspace: ``Project path in config: <workspace>\\Data\\Tools\\BodySlide``.
2. The planned project file and ShapeData NIF did not exist before the run.
   They exist afterwards, and their modification times fall inside the
   Outfit Studio session window.
3. The saved project is the one the script's ``SaveProject`` step describes:
   exactly one set with the planned display name, plus the
   ``ReferenceInfo`` that Outfit Studio records for the loaded reference
   (project file, set, shape).
4. When step lines *are* present (headless mode, or a future version that
   logs them), they must include ``SaveProject``, and no step may report
   an error.

The content checks in :mod:`validate` are separate and unchanged.
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Optional

__all__ = [
    "MTIME_TOLERANCE_S",
    "LogSession",
    "ExecutionEvidence",
    "parse_log_session",
    "planned_outputs",
    "snapshot",
    "collect_evidence",
]

MTIME_TOLERANCE_S = 2.0  # FAT/exFAT store mtimes with 2 s resolution
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_HEADER = re.compile(r"^\[#Log\s+\w+\s+(\w{3})\w*\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s+(\d{4})\]")
_LINE = re.compile(r"^\[(\d{1,2}):(\d{2}):(\d{2})\]")
_PROJECT_PATH = re.compile(r"Project path in config:\s*(.+?)\s*$")
_STEP = "Automation: Step"
_STEP_NUM = re.compile(r"Automation: Step (\d+)/(\d+)")
_STEP_FAILED = re.compile(r"Step (\d+) \(\w+\) failed")
INTERACTIVE_LOG_NOTE = (
    "Outfit Studio 5.8.x sends the messages of an automation run started from the Automation window only to that "
    "window (AutomationDialog redirects wxLog while the script runs), so Log_OS.txt has no step lines; execution is "
    "proven by the saved artifacts instead")


@dataclass
class LogSession:
    start: Optional[float] = None  # epoch seconds (local time as written by Outfit Studio)
    end: Optional[float] = None
    project_paths: list[str] = field(default_factory=list)
    step_lines: list[str] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)
    initialized: bool = False
    frame_closed: bool = False
    step_index: list[int] = field(default_factory=list)  # "Step i/N" number of each step line
    error_step: list[Optional[int]] = field(default_factory=list)  # step number an error line followed

    def steps_in(self, lo: int, hi: int) -> list[str]:
        return [ln for ln, i in zip(self.step_lines, self.step_index) if lo <= i <= hi]

    def errors_in(self, lo: int, hi: int) -> list[str]:
        return [ln for ln, i in zip(self.error_lines, self.error_step) if i is not None and lo <= i <= hi]

    def unattributed_errors(self) -> list[str]:
        return [ln for ln, i in zip(self.error_lines, self.error_step) if i is None]


def _norm(p: str) -> str:
    s = str(p).strip().strip('"').replace("\\", "/").rstrip("/")
    return os.path.normcase(s).lower()


def parse_log_session(text: str) -> LogSession:
    """Parse Outfit Studio's ``Log_OS.txt``.

    The header ``[#Log Sun Sep 27 23:28:02 2026]`` gives the date. Each line starts
    with ``[HH:MM:SS]``, and a time smaller than the previous one means the session
    ran past midnight.
    """
    s = LogSession()
    day0: Optional[datetime] = None
    prev: Optional[datetime] = None
    cur_step: Optional[int] = None
    for raw in text.splitlines():
        ln = raw.rstrip("\r")
        m = _HEADER.match(ln)
        if m:
            mon = _MONTHS.get(m.group(1).lower())
            if mon:
                try:
                    day0 = datetime(int(m.group(6)), mon, int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
                    prev = day0
                    if s.start is None:
                        s.start = time.mktime(day0.timetuple())
                except ValueError:
                    pass
            continue
        m = _LINE.match(ln)
        if m and prev is not None:
            t = prev.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3)))
            if t < prev - timedelta(seconds=1):
                t += timedelta(days=1)
            prev = t
            s.end = time.mktime(t.timetuple())
            if s.start is None:
                s.start = s.end
        pm = _PROJECT_PATH.search(ln)
        if pm:
            s.project_paths.append(pm.group(1))
        sm = _STEP_NUM.search(ln)
        if sm:
            cur_step = int(sm.group(1))
            s.step_lines.append(ln.strip())
            s.step_index.append(cur_step)
        elif _STEP in ln:
            s.step_lines.append(ln.strip())
            s.step_index.append(cur_step or 0)
        if "Automation:" in ln and ("fail" in ln.lower() or "error" in ln.lower()):
            s.error_lines.append(ln.strip())
            fm = _STEP_FAILED.search(ln)
            s.error_step.append(int(fm.group(1)) if fm else cur_step)
        if "Outfit Studio initialized" in ln:
            s.initialized = True
        if "Outfit Studio frame closed" in ln:
            s.frame_closed = True
    return s


def planned_outputs(plan, project: Path) -> dict[str, Path]:
    return {
        "project file": Path(project) / plan.output_osp_rel,
        "ShapeData NIF": Path(project) / "ShapeData" / plan.output_shapedata_folder / plan.output_shapedata_file,
    }


def snapshot(paths: dict[str, Path]) -> dict[str, Optional[float]]:
    """mtime of each planned output, or None when it does not exist."""
    out: dict[str, Optional[float]] = {}
    for k, p in paths.items():
        try:
            out[k] = p.stat().st_mtime
        except OSError:
            out[k] = None
    return out


@dataclass
class ExecutionEvidence:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    window: tuple[Optional[float], Optional[float]] = (None, None)
    window_source: str = ""

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append((name, bool(ok), detail))

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c[1] for c in self.checks)

    @property
    def outputs_missing(self) -> bool:
        return any(n == "execution: planned outputs created by this Outfit Studio session" and not ok and "missing" in d
                   for n, ok, d in self.checks)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "window": list(self.window), "window_source": self.window_source, "notes": self.notes,
                "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in self.checks]}


def _fmt(t: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "?"


def _reference_info(osp: Path) -> tuple[list[str], Optional[dict]]:
    root = ET.fromstring(osp.read_bytes())
    sets = root.findall("SliderSet")
    names = [x.get("name", "") for x in sets]
    ri = sets[0].find("ReferenceInfo") if len(sets) == 1 else None
    return names, (dict(ri.attrib) if ri is not None else None)


def collect_evidence(plan, project: Path, log_text: str, *, interactive: bool,
                     before: Optional[dict[str, Optional[float]]],
                     window: tuple[Optional[float], Optional[float]] = (None, None),
                     window_source: str = "", not_before: Optional[float] = None,
                     step_range: Optional[tuple[int, int]] = None) -> ExecutionEvidence:
    """Decide whether the prepared script ran in the Outfit Studio session started for ``project``.

    ``before`` is the snapshot taken just before Outfit Studio was started (``None`` when
    revalidating an old workspace; then ``not_before``, the time the workspace was prepared,
    stands in for it). ``window`` is the session's start and end in epoch seconds; when it
    is missing, it is taken from the log timestamps. ``step_range`` (first, last step
    number) selects this project's steps when one script converts several projects;
    errors that cannot be attributed to a step count for every project.
    """
    ev = ExecutionEvidence()
    sess = parse_log_session(log_text)
    start, end = window
    if start is None or end is None:
        start, end, window_source = sess.start, sess.end, "Log_OS.txt timestamps"
    ev.window, ev.window_source = (start, end), window_source

    # 1. the log belongs to a session on this workspace
    want = _norm(str(project))
    seen = [_norm(p) for p in sess.project_paths]
    ev.add("execution: Log_OS.txt is from an Outfit Studio session on this workspace",
           bool(log_text.strip()) and want in seen,
           f"project path in config: {sess.project_paths[-1] if sess.project_paths else 'not in log'}"
           + ("" if want in seen else f" (expected {project})"))

    # 2. outputs created during the session
    outs = planned_outputs(plan, project)
    after = snapshot(outs)
    problems, facts = [], []
    if start is None or end is None:
        problems.append("the Outfit Studio session time window is unknown")
    for k, p in outs.items():
        mt = after[k]
        if mt is None:
            problems.append(f"{k} missing ({p.name})")
            continue
        if before is not None and before.get(k) is not None:
            problems.append(f"{k} already existed before Outfit Studio was started")
        if not_before is not None and mt < not_before - MTIME_TOLERANCE_S:
            problems.append(f"{k} is older than the workspace ({_fmt(mt)} < {_fmt(not_before)})")
        if start is not None and end is not None and not (start - MTIME_TOLERANCE_S <= mt <= end + MTIME_TOLERANCE_S):
            problems.append(f"{k} written at {_fmt(mt)}, outside the session {_fmt(start)} – {_fmt(end)}")
        facts.append(f"{k} {_fmt(mt)}")
    ev.add("execution: planned outputs created by this Outfit Studio session", not problems,
           "; ".join(problems) if problems else
           f"{', '.join(facts)} within session {_fmt(start)} – {_fmt(end)} ({window_source})")

    # 3. the saved project is the script's SaveProject result
    osp = outs["project file"]
    if osp.is_file():
        try:
            names, ri = _reference_info(osp)
        except (ET.ParseError, OSError) as exc:
            names, ri, err = [], None, str(exc)
        else:
            err = ""
        exp_file = PureWindowsPath(plan.reference_osp_rel.replace("/", "\\")).name.lower()
        ok_name = names == [plan.output_project]
        ok_ref = bool(ri) and PureWindowsPath(ri.get("ProjectFile", "").replace("/", "\\")).name.lower() == exp_file \
            and ri.get("ProjectName") == plan.reference_set and ri.get("ShapeName") == plan.reference_shape
        detail = err or (f"sets {names}; ReferenceInfo {ri}" if not (ok_name and ok_ref) else
                         f"set '{plan.output_project}', ReferenceInfo {ri.get('ProjectName')}/{ri.get('ShapeName')}")
        ev.add("execution: saved project matches the prepared SaveProject step", ok_name and ok_ref, detail)
    else:
        ev.add("execution: saved project matches the prepared SaveProject step", False, "project file missing")

    # 4. step log, when there is one
    steps = sess.steps_in(*step_range) if step_range else sess.step_lines
    errors = (sess.errors_in(*step_range) + sess.unattributed_errors()) if step_range else sess.error_lines
    if steps:
        saved = any("SaveProject" in ln for ln in steps)
        ev.add("execution: logged automation steps include SaveProject", saved,
               f"{len(steps)} step line(s)" + ("" if saved else "; SaveProject never ran"))
    elif interactive:
        ev.notes.append(INTERACTIVE_LOG_NOTE)
    ev.add("execution: no automation step errors in Log_OS.txt", not errors,
           " | ".join(errors[:3]) if errors else "none")
    return ev
