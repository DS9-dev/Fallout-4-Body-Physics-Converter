"""Phase 3: controlled package/batch conversion.

Stages (each explicit, each resumable from the batch workspace):

1. ``create``: a batch workspace ``work/batch_conversions/<stamp>_<package>/`` with
   ``batch.json`` and one folder per selected project under ``projects/`` (its plan and its
   own automation script). Nothing is written anywhere else.
2. ``run``: hash and stage the sources into the batch's private Outfit Studio project
   folder (``Data/Tools/BodySlide``), then run Outfit Studio. In ``single`` session mode
   *one* script converts every selected project in turn: each project starts with
   ClearProject and ends with its own SaveProject, so with 5.8.2 (interactive) one Execute
   converts the whole batch. ``per_project`` runs one Outfit Studio session per project.
   Then every project is validated **independently** with the Phase 2 checks plus the
   0.2.1 execution evidence, and gets its own status:

   * ``VALIDATED``: every check passed; can be committed
   * ``REVIEW``: the result is structurally sound but a weight-quality check failed
     (anomalies, low agreement, analyzer not KEEP, planned bones missing)
   * ``FAILED``: anything else (not executed, wrong files, embedded body, geometry or
     sliders changed, unexpected physics bones, source changed, ...)

   One project's failure never affects another project's status.
3. ``validate``: the same checks again from the workspace, without Outfit Studio
   (recovery, or after a tool update). Writes ``result.revalidated.json`` per project.
4. ``commit``: explicit. Writes only ``VALIDATED`` projects into the output mod, each
   one transactionally (see ``ConversionEngine._commit``), after re-checking that the
   sources and the generated files still have the hashes recorded at validation.

Nothing reaches the output mod before ``commit``.

Phase 3.2 (mass conversion, fault isolation):

* sessions hold at most ``max_projects_per_session`` projects (cloth projects still last, one per session) and run
  one after the other; every session is validated as soon as it ends, and ``batch.json`` is saved after every state
  change, so a crash of the tool or of Outfit Studio loses at most the session that was running;
* per-project checkpoint states: ``PENDING`` → ``ATTEMPTING`` (its session started) → ``GENERATED`` (outputs saved,
  not yet validated) → ``VALIDATED`` / ``REVIEW`` / ``FAILED`` → ``COMMITTED`` (``PLANNED`` of 0.3/0.4 batches is read
  as ``PENDING``);
* three failure classes: a PROJECT failure only changes that project's status; a SESSION failure (crash, timeout,
  window closed without Execute, automation exit code) keeps every independently validated result and puts the
  session's unfinished projects back to ``PENDING`` for a fresh session (``session_retries``); a SYSTEM failure
  (source or reference files changed, MO2 profile changed, output storage or workspace unusable) stops the batch in
  state ``SYSTEM_ERROR`` because continuing could invalidate many results;
* ``resume`` continues an interrupted batch from its unfinished projects, never repeating VALIDATED/COMMITTED ones.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .. import __version__
from ..analysis.reference import KnowledgeBase
from ..discovery.install import BODYSLIDE_SUBTREE, Installation
from .automation import AutomationScript, check_batch_invariants, safe_script_name
from .engine import ConversionEngine, ConversionResult, RunOutcome, run_outfit_studio
from .evidence import collect_evidence, parse_log_session, planned_outputs, snapshot
from .package import MAIN_GROUP, PackagePreview, package_group_name
from .plan import ConversionPlan, build_script, check_output_mod, sha256_file
from .routes import DEFAULT_ROUTE
from .runtime import RuntimeInfo, write_runtime_config

__all__ = ["BatchEngine", "BatchError", "QUALITY_CHECK_MARKERS", "batch_script", "project_status", "PENDING_STATES",
           "FAILURE_PROJECT", "FAILURE_SESSION", "FAILURE_SYSTEM"]

log = logging.getLogger(__name__)
ProgressFn = Callable[[str], None]

# Failing one of these (and nothing else) makes a project REVIEW instead of FAILED.
QUALITY_CHECK_MARKERS = (
    ": cross-side / quality anomaly checks",
    ": predicted vs actual physics weights consistent",
    "3BBB bones present",
    "analyzer classifies generated project as 3BBB KEEP",
)


PENDING_STATES = ("PENDING", "PLANNED")  # PLANNED: batches created by 0.3.x / 0.4.x
RUNNABLE_STATES = ("CREATED", "RUNNING", "INTERRUPTED", "SYSTEM_ERROR")
FAILURE_PROJECT, FAILURE_SESSION, FAILURE_SYSTEM = "PROJECT_FAILURE", "SESSION_FAILURE", "SYSTEM_FAILURE"
EventFn = Callable[[dict], None]


class BatchError(RuntimeError):
    pass


def _quality_failure(c: dict) -> bool:
    if any(m in c["name"] for m in QUALITY_CHECK_MARKERS):
        return True
    # Phase 3.2: Outfit Studio keeps the 4 largest influences of a vertex after normalizing over all of them, so a
    # vertex that received more than 4 bones is saved with a weight sum below 1. Beyond the tolerance this is a
    # weight-transfer quality problem of that outfit (REVIEW), not a technical failure; vertices WITHOUT any weight
    # stay FAILED. The check and its tolerance are unchanged.
    return c["name"].endswith(": weights normalized") and "WITHOUT any weight" not in c.get("detail", "")


def project_status(checks: list[dict]) -> str:
    failed = [c for c in checks if c.get("required", True) and not c.get("ok")]
    if not failed:
        return "VALIDATED"
    if all(_quality_failure(c) for c in failed):
        return "REVIEW"
    return "FAILED"


def batch_script(plans: list[ConversionPlan], name: str) -> tuple[AutomationScript, list[tuple[int, int]]]:
    """One script that converts ``plans`` in turn; returns it and each project's step range."""
    steps = []
    for i, plan in enumerate(plans, 1):
        part = build_script(plan).steps
        for st in part:
            st.note = f"[{i}/{len(plans)} {plan.source_project}] {st.note}".strip()
        steps += part
    sc = AutomationScript(name=name, steps=steps)
    ranges = check_batch_invariants(sc)
    return sc, ranges


def plan_sessions(entries: list, plans: dict, mode: str, max_per_session: int = 0) -> list[list]:
    """Group projects into Outfit Studio sessions (one script, one Execute each).

    Outfit Studio 5.8.2 keeps the cloth physics data (BSClothExtraData) of every file loaded
    during a session (``OutfitProject::clothData`` is not cleared by ClearProject) and asks on
    *every* later save which of them to write. So a session holds all projects without cloth
    data first and at most one project with cloth data, as its last project; every further
    cloth project gets its own session. Its dialog then lists only that project's own file.
    ``per_project`` mode gives every project its own session.

    ``max_per_session`` (Phase 3.2, 0 = no limit) splits the projects without cloth data into sessions of at most that
    many projects, in the given (deterministic) order; the first cloth project joins the last of them if it has room.
    """
    key = (lambda e: e["id"]) if entries and isinstance(entries[0], dict) else (lambda e: e)
    if mode == "per_project":
        return [[e] for e in entries]
    clean = [e for e in entries if not plans[key(e)].source_cloth_data]
    cloth = [e for e in entries if plans[key(e)].source_cloth_data]
    limit = max_per_session if max_per_session and max_per_session > 0 else 0
    n = limit or max(len(clean), 1)
    groups = [clean[i:i + n] for i in range(0, len(clean), n)]
    if not cloth:
        return groups
    if groups and (not limit or len(groups[-1]) < limit):
        groups[-1] = groups[-1] + [cloth[0]]
    else:
        groups.append([cloth[0]])
    return groups + [[c] for c in cloth[1:]]


def session_instructions(name: str, plans: list[ConversionPlan], si: int, n: int) -> str:
    cloth = [p for p in plans if p.source_cloth_data]
    msg = (f"Outfit Studio session {si}/{n} ({len(plans)} project(s)): File > Open Automation… (Ctrl+Shift+A), "
           f"choose '{name}', press Execute once (it converts {'all ' if len(plans) > 1 else ''}{len(plans)} project(s) in turn), "
           "confirm the final message, close the Automation window and close Outfit Studio (answer 'No' if asked to save). "
           "If an 'Automation Error' asks whether to continue, answer Yes: every project is validated on its own afterwards.")
    if cloth:
        p = cloth[0]
        msg += (f" While '{p.output_project}' is saved (the last project of this session) Outfit Studio asks 'Choose cloth "
                f"data': tick only '{Path(p.source_shapedata_rel).name}' and press OK.")
    return msg


def dry_run_sessions(preview: PackagePreview, mode: str, max_per_session: int = 0) -> dict[str, str]:
    """Fill ``preview.dry_run`` with the Outfit Studio sessions of the current selection; returns the scripts."""
    chosen = [p.conversion_plan() for p in preview.selected()]
    plans = {pl.source_project: pl for pl in chosen}
    sessions = plan_sessions(list(plans), plans, mode, max_per_session)
    scripts: dict[str, str] = {}
    preview.dry_run = []
    for si, names in enumerate(sessions, 1):
        name = f"cbbe3bbb_batch_DRYRUN_{si}" if len(sessions) > 1 else "cbbe3bbb_batch_DRYRUN"
        sc, _ = batch_script([plans[n] for n in names], name)
        scripts[name] = sc.to_xml()
        preview.dry_run.append({"session": si, "script": name, "projects": names,
                                "instructions": session_instructions(name, [plans[n] for n in names], si, len(sessions))})
    for pl in chosen:
        scripts[pl.script_name] = pl.script_xml
    return scripts


def _pid(i: int, project: str) -> str:
    return f"{i:02d}_" + (re.sub(r"[^A-Za-z0-9_.-]+", "_", project).strip("._")[:50] or "project")


def _jload(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _jsave(p: Path, data) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


class BatchEngine:
    REFUSES_ACCEPTANCE_COMMIT = True  # commit() refuses a batch marked "acceptance" (checked by AcceptanceIsolation)

    def __init__(self, tool_root: Path, inst: Installation, kb: KnowledgeBase,
                 runner: Optional[Callable[..., RunOutcome]] = None):
        from .os_control import UIDriver

        # 0.5.3: ``runner`` may be a UI driver (the unattended control of a real or modelled Outfit Studio window)
        self.ui_driver = runner if isinstance(runner, UIDriver) else None
        # (a callable driver also plays the manual fallback in tests; Win32Driver is not callable → the real Outfit Studio)
        legacy = runner if self.ui_driver is None or callable(runner) else None
        self.eng = ConversionEngine(tool_root, inst, kb, runner=legacy)
        self.inst = inst
        self.kb = kb
        self.root = Path(tool_root) / "work" / "batch_conversions"
        self._lock = threading.RLock()  # commit may run inside a session hook

    # ------------------------------------------------------------------ create
    def create(self, preview: PackagePreview, say: Optional[ProgressFn] = None) -> Path:
        """Batch workspace for the preview's selected projects. Writes only inside work/."""
        say = say or (lambda m: None)
        chosen = [p for p in preview.projects if p.selected]
        if not chosen:
            raise BatchError("no project is selected")
        bad = [p.project for p in chosen if not p.plan or not (
            (p.selectable and p.action == "CONVERT") or getattr(p, "review_policy", "") == "REVIEW_ATTEMPTABLE")]
        if bad:
            raise BatchError(f"only CONVERT projects (or, in Convert All Possible, attemptable REVIEW projects) can be "
                             f"converted: {', '.join(bad)}")
        pkg = re.sub(r"[^A-Za-z0-9_.-]+", "_", preview.package).strip("._")[:40] or "package"
        bid = f"{time.strftime('%Y%m%d-%H%M%S')}_{pkg}"
        bdir = self.root / bid
        n = 1
        while bdir.exists():
            n += 1
            bdir = self.root / f"{bid}-{n}"
        (bdir / "projects").mkdir(parents=True)
        (bdir / "logs").mkdir()
        entries = []
        for i, p in enumerate(chosen, 1):
            plan = p.conversion_plan()
            pid = _pid(i, p.project)
            pdir = bdir / "projects" / pid
            (pdir / "logs").mkdir(parents=True)
            _jsave(pdir / "plan.json", plan.to_dict())
            (pdir / f"{plan.script_name}.xml").write_text(plan.script_xml, encoding="utf-8")
            (pdir / "shapes.json").write_text(json.dumps([s.__dict__ for s in p.shapes], indent=1), encoding="utf-8")
            entries.append({"id": pid, "project": p.project, "generated_project": plan.output_project,
                            "confidence": p.confidence, "regions": p.regions, "status": "PENDING", "attempts": 0,
                            "origin": getattr(p, "attempt_origin", "") or "SAFE",
                            "item_names": [g.get("armor_name") for g in (p.game_items or []) if g.get("armor_name")][:3]})
        conv = self.inst.profile.conversion
        batch = {"schema": 2, "id": bdir.name, "tool_version": __version__, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "conversion_mode": getattr(preview, "conversion_mode", "") or "SAFE",
                 "max_projects_per_session": conv.max_projects_per_session, "session_retries": conv.session_retries,
                 "package": preview.package, "output_mod": preview.output_mod, "session_mode": preview.session_mode,
                 "run_mode": preview.run_mode, "groups": [MAIN_GROUP, package_group_name(preview.package)],
                 "route": {**DEFAULT_ROUTE.to_dict(),
                           "requested_source": getattr(preview, "requested_source", "auto") or "auto",
                           "requested_target": getattr(preview, "target_body", "cbbe_3bbb") or "cbbe_3bbb"},
                 "projects": entries, "sessions": [], "state": "CREATED",
                 **({"acceptance": dict(preview.acceptance)} if getattr(preview, "acceptance", None) else {}),
                 "not_selected": [{"project": p.project, "action": p.action, "status": p.status,
                                   "reason": p.reasons[0] if p.reasons else ""} for p in preview.projects if not p.selected]}
        _jsave(bdir / "batch.json", batch)
        _jsave(bdir / "preview.json", preview.to_dict())
        say(f"Batch workspace created: {bdir}")
        return bdir

    # ------------------------------------------------------------------ helpers
    def _load(self, bdir: Path) -> dict:
        bdir = Path(bdir)
        try:
            inside = bdir.resolve().is_relative_to(self.root.resolve())
        except (OSError, ValueError):
            inside = False
        if not inside or not (bdir / "batch.json").is_file():
            raise BatchError(f"not a batch workspace of this tool (expected batch.json under {self.root})")
        return _jload(bdir / "batch.json")

    @staticmethod
    def _plan(bdir: Path, e: dict) -> ConversionPlan:
        return ConversionPlan.from_dict(_jload(Path(bdir) / "projects" / e["id"] / "plan.json"))

    @staticmethod
    def latest_result(bdir: Path, e: dict) -> Optional[dict]:
        pdir = Path(bdir) / "projects" / e["id"]
        for name in ("result.revalidated.json", "result.json"):
            if (pdir / name).is_file():
                return _jload(pdir / name)
        return None

    # ------------------------------------------------------------------ run
    def run(self, bdir: Path, progress: Optional[ProgressFn] = None, cancel: Optional[Callable[[], bool]] = None,
            events: Optional[EventFn] = None, on_session_done: Optional[Callable[[Path, list[str]], None]] = None,
            control_failure: Optional[Callable[[dict], str]] = None) -> dict:
        """Run the batch: stage the sources, then Outfit Studio session after session, each validated when it ends.

        ``events`` receives machine-readable progress (``{"kind": ...}``) for the GUI; ``on_session_done(bdir,
        validated_project_names)`` runs after every validated session (the one-click pipeline commits there)."""
        if not self._lock.acquire(blocking=False):
            raise BatchError("another batch is running")
        try:
            return self._run(Path(bdir), progress or (lambda m: None), cancel or (lambda: False),
                             events or (lambda e: None), on_session_done, control_failure)
        finally:
            self._lock.release()

    def resume(self, bdir: Path, progress: Optional[ProgressFn] = None, cancel: Optional[Callable[[], bool]] = None,
               events: Optional[EventFn] = None, on_session_done: Optional[Callable[[Path, list[str]], None]] = None,
               control_failure: Optional[Callable[[dict], str]] = None, control: Optional[str] = None) -> dict:
        """Continue an interrupted batch (tool closed or crashed, Outfit Studio crashed, cancelled, system error) from
        its unfinished projects. VALIDATED / REVIEW / FAILED / COMMITTED projects are not run again."""
        if not self._lock.acquire(blocking=False):
            raise BatchError("another batch is running")
        try:
            say = progress or (lambda m: None)
            bdir = Path(bdir)
            batch = self._load(bdir)
            if control in ("manual", "unattended"):  # 0.5.3: resume manually / automatically
                batch["control_override"] = control
            elif control == "auto":
                batch.pop("control_override", None)
            batch.pop("control_failures", None)
            if batch["state"] == "CREATED":
                _jsave(bdir / "batch.json", batch)
                return self._run(bdir, say, cancel or (lambda: False), events or (lambda e: None), on_session_done,
                                 control_failure)
            if batch["state"] not in RUNNABLE_STATES and not any(e["status"] in PENDING_STATES + ("ATTEMPTING", "GENERATED")
                                                                   for e in batch["projects"]):
                raise BatchError(f"nothing to resume (state {batch['state']})")
            plans = {e["id"]: self._plan(bdir, e) for e in batch["projects"]}
            hashes = {e["id"]: e.get("source_hashes", {}) for e in batch["projects"]}
            rt = RuntimeInfo(**{k: v for k, v in batch.get("runtime", {}).items() if k in RuntimeInfo.__dataclass_fields__})
            project = bdir / "Data" / BODYSLIDE_SUBTREE
            # a session that never ended (the tool itself stopped): keep what it saved, the rest is unfinished
            for sess in batch.get("sessions", []):
                if sess.get("ended") is None:
                    sess["ended"] = None
                    sess["interrupted"] = True
                    sess["failure"] = FAILURE_SESSION
                for pid in sess["projects"]:
                    e = next(x for x in batch["projects"] if x["id"] == pid)
                    if e["status"] == "ATTEMPTING":
                        outs = planned_outputs(plans[pid], project)
                        started = sess.get("started") or 0
                        done = all(f.is_file() and f.stat().st_mtime >= started - 2 for f in outs.values())
                        e["status"] = "GENERATED" if done else "PENDING"
            gen = [e for e in batch["projects"] if e["status"] == "GENERATED"]
            if gen:
                say(f"Resuming: validating {len(gen)} project(s) saved before the interruption")
                self._validate_all(bdir, batch, gen, plans, hashes, rt, say, result_name="result.json")
            batch.pop("system_failure", None)
            batch["state"] = "RUNNING"
            batch.setdefault("resumed", []).append(time.strftime("%Y-%m-%dT%H:%M:%S"))
            _jsave(bdir / "batch.json", batch)
            if not rt.path:
                rt = self.eng.resolve_runtime(prepare=True)
                batch["runtime"] = rt.to_dict()
            return self._drive(bdir, batch, plans, hashes, rt, say, cancel or (lambda: False), events or (lambda e: None),
                               on_session_done, control_failure)
        finally:
            self._lock.release()

    def _run(self, bdir: Path, say: ProgressFn, cancel: Callable[[], bool], events: EventFn,
             on_session_done: Optional[Callable[[Path, list[str]], None]] = None,
             control_failure: Optional[Callable[[dict], str]] = None) -> dict:
        batch = self._load(bdir)
        if batch["state"] not in ("CREATED",):
            raise BatchError(f"batch already ran (state {batch['state']}); use resume-batch / validate-batch / commit-batch")
        project = bdir / "Data" / BODYSLIDE_SUBTREE
        entries = batch["projects"]
        plans = {e["id"]: self._plan(bdir, e) for e in entries}
        hashes: dict[str, dict] = {}
        staged: dict[str, str] = {}  # rel (lower) -> abs source
        # 1. hash + stage ------------------------------------------------------------------------
        for n, e in enumerate(entries, 1):
            say(f"Preparing {n}/{len(entries)}: {e['project']}")
            events({"kind": "prepare", "done": n - 1, "total": len(entries), "project": e["project"]})
            plan = plans[e["id"]]
            problems, h = [], {}
            rs = (plan.route or {}).get("route_status")  # 0.6.0 defence in depth: never run a route that cannot run
            if rs and rs not in ("PRODUCTION", "LEGACY_SUPPORTED"):
                problems.append(f"body route {plan.route.get('route_label') or plan.route.get('route_id')} is {rs}: "
                                "not converted")
            for sf in plan.staged_files:
                p = Path(sf.abs_path)
                try:
                    st = p.stat()
                except OSError:
                    problems.append(f"{sf.rel} is missing")
                    continue
                if int(st.st_mtime) != int(sf.mtime) or st.st_size != sf.size:
                    problems.append(f"{sf.rel} changed since the preview; run the preview again")
                    continue
                prev = staged.get(sf.rel.lower())
                if prev is not None and Path(prev) != p:
                    problems.append(f"{sf.rel} would be staged from two different files ({prev} / {p})")
                    continue
                h[sf.abs_path] = sha256_file(p)
                if prev is None:
                    dst = project / sf.rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dst)
                    staged[sf.rel.lower()] = str(p)
            hashes[e["id"]] = h
            e["source_hashes"] = h  # kept in batch.json too, so a lost result.json can be rebuilt (validate-batch)
            if e["status"] in PENDING_STATES:
                e["status"] = "PENDING"
            if problems:
                e["status"] = "FAILED"
                e["failure"] = FAILURE_PROJECT
                self._write_project_result(bdir, e, plan, ConversionResult(
                    id=e["id"], status="FAILED", reasons=problems, workspace=str(bdir / "projects" / e["id"]),
                    output_mod=plan.output_mod_path, source_hashes_before=h))
        for p in (project / "SliderSets", project / "ShapeData", project / "Automations"):
            p.mkdir(parents=True, exist_ok=True)
        batch["system"] = self._system_fingerprint(bdir, batch)
        if cancel():
            return self._finish(bdir, batch, "INTERRUPTED", say)
        # 2. runtime -------------------------------------------------------------------------------
        rt = self.eng.resolve_runtime(prepare=True)
        batch["runtime"] = rt.to_dict()
        if not rt.headless_supported:
            for e in entries:
                if e["status"] in PENDING_STATES:
                    self._fail_entry(bdir, e, plans[e["id"]], hashes[e["id"]], f"Outfit Studio not usable: {rt.reason}")
            batch["system_failure"] = {"class": FAILURE_SYSTEM, "reason": f"Outfit Studio not usable: {rt.reason}"}
            return self._finish(bdir, batch, "SYSTEM_ERROR", say)
        batch["state"] = "RUNNING"
        _jsave(bdir / "batch.json", batch)
        return self._drive(bdir, batch, plans, hashes, rt, say, cancel, events, on_session_done, control_failure)

    # ------------------------------------------------------------------ sessions
    def _drive(self, bdir: Path, batch: dict, plans: dict, hashes: dict, rt: RuntimeInfo, say: ProgressFn,
               cancel: Callable[[], bool], events: EventFn,
               on_session_done: Optional[Callable[[Path, list[str]], None]] = None,
               control_failure: Optional[Callable[[dict], str]] = None) -> dict:
        """Run Outfit Studio sessions until no project is PENDING; validate (and checkpoint) after every session."""
        project = bdir / "Data" / BODYSLIDE_SUBTREE
        conv = self.inst.profile.conversion
        write_runtime_config(Path(rt.folder), project, bdir / "Data")
        interactive = batch.get("run_mode") == "interactive" or (not batch.get("run_mode") and rt.recommended_run_mode == "interactive")
        max_per = int(batch.get("max_projects_per_session", conv.max_projects_per_session) or 0)
        retries = int(batch.get("session_retries", conv.session_retries) or 0)
        entries = batch["projects"]
        total = len(entries)
        while True:
            todo = [e for e in entries if e["status"] in PENDING_STATES]
            if not todo:
                break
            if cancel():
                return self._finish(bdir, batch, "INTERRUPTED", say)
            problem = self._system_check(bdir, batch, todo, plans)
            if problem:
                batch["system_failure"] = {"class": FAILURE_SYSTEM, "reason": problem,
                                           "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                say(f"Stopped: {problem}")
                events({"kind": "system_failure", "reason": problem})
                return self._finish(bdir, batch, "SYSTEM_ERROR", say)
            control = self._control_mode(batch, interactive)
            isolate = [e for e in todo if e.get("isolate")]
            if isolate:  # 0.5.3: a project that was running when a session had to stop runs alone
                group = [isolate[0]]
            else:
                limit = max_per
                if batch.get("split_limit"):
                    limit = min(limit, int(batch["split_limit"])) if limit else int(batch["split_limit"])
                group = plan_sessions(todo, plans, conv.batch_session_mode, limit)[0]
            si = len(batch["sessions"]) + 1
            name = safe_script_name(f"cbbe3bbb_batch_{bdir.name}_{si}")
            sc, ranges = batch_script([plans[e["id"]] for e in group], name)
            xml = sc.to_xml()
            (project / "Automations" / f"{name}.xml").write_text(xml, encoding="utf-8")
            (bdir / f"{name}.xml").write_text(xml, encoding="utf-8")
            for e in group:  # 0.5.3: a retry starts clean; what an earlier attempt left is kept as evidence
                self._set_aside_leftovers(bdir, e, plans[e["id"]], project)
            before = {e["id"]: snapshot(planned_outputs(plans[e["id"]], project)) for e in group}
            sess = {"id": si, "script": name, "projects": [e["id"] for e in group],
                    "ranges": {e["id"]: list(r) for e, r in zip(group, ranges)}, "before": before,
                    "mode": "interactive" if interactive else "headless", "runtime_folder": rt.folder,
                    "control": control, "script_sha256": sha256_file(project / "Automations" / f"{name}.xml"),
                    "steps": len(sc.steps), "isolated": bool(isolate)}
            batch["sessions"].append(sess)
            for e in group:
                e["status"] = "ATTEMPTING"
                e["attempts"] = int(e.get("attempts", 0)) + 1
                e.pop("isolate", None)
            remaining_after = len([e for e in entries if e["status"] in PENDING_STATES])
            manual = interactive and control == "manual"
            if control == "unattended":
                say(f"Outfit Studio session {si}: converting {len(group)} outfit(s) automatically "
                    "(Outfit Studio may open; it does not need any input)…")
            elif manual:
                say("Waiting for Outfit Studio. " + session_instructions(name, [plans[e["id"]] for e in group], si,
                                                                         si + (1 if remaining_after else 0)))
            else:
                say(f"Running Outfit Studio {rt.version} headless (session {si}, {len(group)} project(s))…")
            events({"kind": "session_start", "session": si, "script": name, "interactive": manual,
                    "unattended": control == "unattended", "projects": [e["project"] for e in group],
                    "instructions": session_instructions(name, [plans[e["id"]] for e in group], si, si) if manual else ""})
            if control == "unattended":
                timeout = conv.automation_timeout_s + 120 * len(group)
            else:
                timeout = (conv.interactive_timeout_s if interactive else conv.automation_timeout_s) + 60 * len(group)
            sess["started"] = time.time()
            _jsave(bdir / "batch.json", batch)
            stop = threading.Event()
            mon = threading.Thread(target=self._monitor, args=(stop, group, plans, project, sess["started"], events),
                                   daemon=True)
            mon.start()
            sres = None
            try:
                if control == "unattended":
                    run, sres = self._unattended_session(bdir, sess, group, plans, rt, project, timeout, cancel, events)
                else:
                    run = self.eng.runner(Path(rt.folder), Path(rt.path), name, timeout, cancel, interactive=interactive)
            except Exception as exc:  # noqa: BLE001 - a runner failure is a session failure, not a tool crash
                log.exception("Outfit Studio session %s could not run", si)
                run = RunOutcome(command=[], exit_code=None, seconds=0.0)
                run.crashed = f"could not start Outfit Studio: {exc}"
            finally:
                stop.set()
                mon.join(timeout=5)
            sess["ended"] = time.time()
            sess.update({"exit_code": run.exit_code, "seconds": run.seconds, "timed_out": run.timed_out,
                         "cancelled": run.cancelled, "crashed": run.crashed, "command": run.command})
            (bdir / "logs" / f"Log_OS.session{si}.txt").write_text(run.log_text or "", encoding="utf-8")
            (bdir / "logs" / f"stdout.session{si}.txt").write_text((run.stdout or "") + "\n--- stderr ---\n" + (run.stderr or ""),
                                                                   encoding="utf-8")
            saved = {e["id"]: all(f.is_file() and f.stat().st_mtime >= sess["started"] - 2
                                  for f in planned_outputs(plans[e["id"]], project).values()) for e in group}
            if sres is not None:
                failure = self._unattended_failure(sres)
            else:
                failure = self._session_failure(sess, run, interactive, saved)
            if failure:
                sess["failure"] = FAILURE_SESSION
                sess["failure_reason"] = failure
            for e in group:
                e["status"] = "GENERATED" if saved[e["id"]] else "ATTEMPTING"
            _jsave(bdir / "batch.json", batch)
            # validate this session's projects now (checkpoint)
            if run.cancelled:
                for e in group:
                    if e["status"] == "ATTEMPTING":
                        e["status"] = "PENDING"  # resumable
                self._validate_all(bdir, batch, [e for e in group if e["status"] == "GENERATED"], plans, hashes, rt, say,
                                   result_name="result.json", events=events)
                return self._finish(bdir, batch, "INTERRUPTED", say)
            if sres is not None:
                decision = self._after_unattended(bdir, batch, sess, sres, group, plans, hashes, rt, retries, failure,
                                                  say, events, control_failure)
                if decision == "stop":
                    _jsave(bdir / "batch.json", batch)
                    return self._finish(bdir, batch, "INTERRUPTED", say)
            else:
                retry = []
                if failure:
                    retry = [e for e in group if int(e.get("attempts", 1)) <= retries
                             and (e["status"] == "ATTEMPTING" or run.crashed or run.timed_out)]
                validate_now = [e for e in group if e not in retry or e["status"] == "GENERATED"]
                self._validate_all(bdir, batch, validate_now, plans, hashes, rt, say, result_name="result.json", events=events)
                for e in retry:
                    if e["status"] != "VALIDATED":  # uncertain result of a failed session: try again in a clean session
                        e["status"] = "PENDING"
                        e.setdefault("retried_after", []).append(failure)
                if retry:
                    say(f"Session {si} failed ({failure}); {sum(1 for e in retry if e['status'] == 'PENDING')} unfinished "
                        "project(s) will run again in a new Outfit Studio session")
                    events({"kind": "session_failure", "session": si, "reason": failure,
                            "retry": [e["project"] for e in retry if e["status"] == "PENDING"]})
            _jsave(bdir / "batch.json", batch)
            done = [e for e in entries if e["status"] not in PENDING_STATES + ("ATTEMPTING", "GENERATED")]
            events({"kind": "session_done", "session": si, "done": len(done), "total": total,
                    "counts": self._counts(entries)})
            ok = [e["project"] for e in group if e["status"] == "VALIDATED"]
            if on_session_done is not None and ok:
                on_session_done(bdir, ok)
                batch = self._reload_into(bdir, batch)
                entries = batch["projects"]
        state = "RAN"
        if any(e["status"] == "COMMITTED" for e in entries):
            state = "COMMITTED" if all(e["status"] == "COMMITTED" for e in entries) else "PARTIALLY_COMMITTED"
        return self._finish(bdir, batch, state, say)

    @staticmethod
    def _set_aside_leftovers(bdir: Path, e: dict, plan: ConversionPlan, project: Path) -> None:
        """Move the generated files of an earlier, unfinished attempt of this project (inside the batch workspace only)
        to ``projects/<id>/previous_attempt_<n>/``, so the new session's execution evidence starts from nothing."""
        if not int(e.get("attempts", 0)):
            return
        osp = project / plan.output_osp_rel
        sd = project / "ShapeData" / plan.output_shapedata_folder
        if not osp.exists() and not sd.exists():
            return
        dest = bdir / "projects" / e["id"] / f"previous_attempt_{int(e.get('attempts', 0))}"
        n = 1
        while dest.exists():
            n += 1
            dest = dest.with_name(f"previous_attempt_{int(e.get('attempts', 0))}_{n}")
        dest.mkdir(parents=True)
        if osp.exists():
            shutil.move(str(osp), str(dest / osp.name))
        if sd.exists():
            shutil.move(str(sd), str(dest / "ShapeData"))
        e.setdefault("previous_attempts", []).append(str(dest.relative_to(bdir)))

    # ------------------------------------------------------------------ 0.5.3 unattended Outfit Studio
    def _driver(self, rt: RuntimeInfo):
        """The UI driver for unattended sessions (None = not available here → manual)."""
        if self.ui_driver is not None:
            return self.ui_driver
        if getattr(self, "_win32", None) is not None:
            return self._win32 or None
        from . import win32_ui

        self._win32 = False
        if not win32_ui.available():
            return None
        try:
            exe_sha = sha256_file(Path(rt.path)) if rt.path else ""
        except OSError:
            exe_sha = ""
        try:
            self._win32 = win32_ui.Win32Driver(exe_sha256=exe_sha)
        except Exception:  # noqa: BLE001 - no unattended control possible: the manual path still works
            log.exception("the Windows UI driver could not be created; Outfit Studio sessions run manually")
            self._win32 = False
        return self._win32 or None

    def _control_mode(self, batch: dict, interactive: bool) -> str:
        """headless | unattended (the converter drives Outfit Studio's window) | manual (the user presses Execute)."""
        if not interactive:
            return "headless"
        if batch.pop("manual_next", False):
            return "manual"
        pref = (batch.get("control_override") or getattr(self.inst.profile.conversion, "os_control", "auto") or "auto").lower()
        if pref == "manual":
            return "manual"
        rt = RuntimeInfo(**{k: v for k, v in batch.get("runtime", {}).items() if k in RuntimeInfo.__dataclass_fields__})
        if self.ui_driver is None and self.eng.runner is not run_outfit_studio:
            return "manual"  # a custom runner (tests, tools) plays the part of Outfit Studio + the user
        return "unattended" if self._driver(rt) is not None else "manual"

    def _unattended_session(self, bdir: Path, sess: dict, group: list[dict], plans: dict, rt: RuntimeInfo, project: Path,
                            timeout: int, cancel: Callable[[], bool], events: EventFn):
        from .os_control import SessionController, SessionProject, SessionSpec, write_session_result

        conv = self.inst.profile.conversion
        sps = []
        for e in group:
            plan = plans[e["id"]]
            src = next((sf for sf in plan.staged_files if sf.role == "source-nif"), None)
            h = (e.get("source_hashes") or {}).get(src.abs_path, "") if src else ""
            sps.append(SessionProject(
                id=e["id"], project=e["project"], generated_project=plan.output_project,
                step_range=tuple(sess["ranges"][e["id"]]),
                outputs={k: str(v) for k, v in planned_outputs(plan, project).items()},
                cloth_blocks=int(plan.source_cloth_data or 0),
                source_nif=str(project / plan.source_shapedata_rel) if plan.source_shapedata_rel else "",
                source_nif_sha256=h))
        spec = SessionSpec(batch_id=bdir.name, session=sess["id"], script_name=sess["script"],
                           script_sha256=sess["script_sha256"], expected_steps=int(sess["steps"]), projects=sps,
                           project_dir=str(project), runtime_folder=rt.folder, exe=rt.path, timeout_s=int(timeout),
                           stall_timeout_s=int(getattr(conv, "unattended_stall_timeout_s", 900) or 900),
                           ready_timeout_s=int(getattr(conv, "unattended_ready_timeout_s", 180) or 180),
                           close_timeout_s=int(getattr(conv, "unattended_close_timeout_s", 60) or 60),
                           diagnostics_dir=str(bdir / "logs" / "os_control"))
        driver = self._driver(rt)
        ctl = SessionController(driver, poll_s=float(getattr(driver, "poll_s", 0.25)),
                                on_event=lambda ev: events({**ev, "kind": "os_control", "event": ev.get("kind")}))
        sres = ctl.run(spec, cancel)
        write_session_result(sres, bdir / "logs" / f"session_result.session{sess['id']}.json")
        (bdir / "logs" / f"automation_output.session{sess['id']}.txt").write_text(sres.automation_output or "",
                                                                                  encoding="utf-8")
        sess["result"] = {k: getattr(sres, k) for k in (
            "status", "reason", "suspect_project", "started_projects", "processed_projects", "automation_errors",
            "cloth", "completion", "close", "exe_sha256", "pid", "launched_at", "ready_at", "started_at", "finished_at",
            "ended_at", "output_captured", "fallback")}
        sess["result"]["dialogs"] = [{k: d.get(k) for k in ("title", "answer", "project", "policy")} for d in sres.dialogs]
        from .os_control import CANCELLED, OS_CRASHED, TIMEOUT

        log_file = Path(rt.folder) / "Log_OS.txt"
        run = RunOutcome(command=[rt.path], exit_code=sres.exit_code,
                         seconds=round((sres.ended_at or 0) - (sres.launched_at or sres.ended_at or 0), 2),
                         interactive=True)
        run.timed_out = sres.status == TIMEOUT
        run.cancelled = sres.status == CANCELLED
        run.crashed = sres.reason if sres.status == OS_CRASHED else ""
        run.log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
        return run, sres

    @staticmethod
    def _unattended_failure(sres) -> str:
        from .os_control import CANCELLED, COMPLETED, COMPLETED_WITH_PROJECT_ERRORS

        if sres.status in (COMPLETED, COMPLETED_WITH_PROJECT_ERRORS, CANCELLED):
            return ""
        return f"{sres.status}: {sres.reason}"

    def _after_unattended(self, bdir: Path, batch: dict, sess: dict, sres, group: list[dict], plans: dict, hashes: dict,
                          rt: RuntimeInfo, retries: int, failure: str, say: ProgressFn, events: EventFn,
                          control_failure: Optional[Callable[[dict], str]]) -> str:
        """Checkpoint after an unattended session. Returns 'continue' or 'stop'.

        * projects saved in the session are validated on their own (as always);
        * a project that never started (the session stopped before its first step) goes back to PENDING without
          using up an attempt;
        * the project that was running when the session had to stop (crash, timeout, unknown dialog) runs ALONE in the
          next session (``isolate``) while its attempts last, then fails on its own — so one broken outfit never
          cancels the package; without an identifiable project the next sessions are halved (``split_limit``);
        * a control failure before anything ran (script/window could not be verified) is retried once in a fresh
          Outfit Studio; a second one in a row asks ``control_failure`` → retry | manual | stop (default stop)."""
        from .os_control import AUTOMATION_FAILED

        by_name = {e["project"]: e for e in group}
        unsaved = [e for e in group if e["status"] == "ATTEMPTING"]
        if not failure:
            batch["control_failures"] = 0
            batch.pop("split_limit", None)  # a clean session: back to the normal session size
            self._validate_all(bdir, batch, [e for e in group if e["status"] in ("GENERATED", "ATTEMPTING")], plans, hashes,
                               rt, say, result_name="result.json", events=events)
            return "continue"
        nothing_ran = sres.started_at is None
        suspect = by_name.get(sres.suspect_project)
        # which projects started: the step lines seen (a lower bound: a crash can hide the last ones), the saved ones,
        # and — the projects run in order — everything up to and including the one that was running
        order = [e["project"] for e in group]
        started = set(sres.started_projects) | {e["project"] for e in group if e["status"] == "GENERATED"}
        if suspect is not None:
            started |= set(order[:order.index(suspect["project"]) + 1])
        elif not sres.output_captured and not nothing_ran:
            started |= set(order)  # no step information at all: count every project as tried
        if nothing_ran:
            started = set()
        pending: list[dict] = []
        for e in unsaved:
            if e["project"] not in started:
                e["attempts"] = max(0, int(e.get("attempts", 1)) - 1)  # it never ran: no attempt used
                e.setdefault("deferred_after", []).append(failure)
                pending.append(e)
            elif e is suspect:
                if int(e.get("attempts", 1)) <= retries:
                    e["isolate"] = True
                    e.setdefault("retried_after", []).append(failure)
                    pending.append(e)
            elif int(e.get("attempts", 1)) <= retries:
                e.setdefault("retried_after", []).append(failure)
                pending.append(e)
        for e in pending:
            e["status"] = "PENDING"
        final = [e for e in group if e not in pending]
        self._validate_all(bdir, batch, final, plans, hashes, rt, say, result_name="result.json", events=events)
        if suspect is None and not nothing_ran and pending and sres.status != AUTOMATION_FAILED:
            batch["split_limit"] = max(1, (len(pending) + 1) // 2)
        if pending:
            say(f"Outfit Studio session {sess['id']} stopped ({failure}); {len(pending)} unfinished outfit(s) continue in a "
                "new session" + (f"; '{suspect['project']}' runs on its own" if suspect is not None and suspect in pending else ""))
            events({"kind": "session_failure", "session": sess["id"], "reason": failure,
                    "retry": [e["project"] for e in pending], "isolated": suspect["project"] if suspect else ""})
        if nothing_ran:  # nothing executed (any status): a control failure — counted, so it can never loop
            batch["control_failures"] = int(batch.get("control_failures", 0)) + 1
            info = {"session": sess["id"], "script": sess["script"], "reason": sres.reason, "fallback": sres.fallback,
                    "batch": bdir.name, "projects": [e["project"] for e in group]}
            batch["control_stop"] = info
            if batch["control_failures"] >= 2:
                choice = "stop"
                if control_failure is not None:
                    try:
                        choice = (control_failure(info) or "stop").lower()
                    except Exception:  # noqa: BLE001
                        log.exception("control-failure callback failed")
                        choice = "stop"
                events({"kind": "control_failure", "choice": choice, **info})
                if choice == "retry":
                    batch["control_failures"] = 0
                elif choice == "manual":
                    batch["control_failures"] = 0
                    batch["manual_next"] = True
                else:
                    say("Automatic Outfit Studio control could not continue. Your conversion progress is saved. "
                        f"Resume retries automatically; or continue this session manually: {sres.fallback}")
                    return "stop"
        else:
            batch["control_failures"] = 0
        return "continue"


    def _reload_into(self, bdir: Path, batch: dict) -> dict:
        """Re-read batch.json after a hook changed it (commit), keeping this object's session bookkeeping."""
        fresh = _jload(bdir / "batch.json")
        batch.clear()
        batch.update(fresh)
        batch["state"] = "RUNNING"
        _jsave(bdir / "batch.json", batch)
        return batch

    @staticmethod
    def _counts(entries: list[dict]) -> dict:
        c: dict[str, int] = {}
        for e in entries:
            c[e["status"]] = c.get(e["status"], 0) + 1
        return c

    @staticmethod
    def _session_failure(sess: dict, run: RunOutcome, interactive: bool, saved: dict) -> str:
        """The reason a whole session failed ('' = the session itself was fine; projects may still fail)."""
        if run.crashed:
            return f"Outfit Studio crashed ({run.crashed})"
        if run.timed_out:
            return "Outfit Studio did not finish in time"
        if not interactive and run.exit_code not in (0, 10):
            return f"Outfit Studio automation exit code {run.exit_code}"
        if saved and not any(saved.values()) and not run.cancelled:
            return ("Outfit Studio closed without saving any project of this session"
                    + (" (Execute was not pressed?)" if interactive else ""))
        return ""

    def _monitor(self, stop: threading.Event, group: list[dict], plans: dict, project: Path, started: float,
                 events: EventFn) -> None:
        """While Outfit Studio runs: report which project of the session is being converted (from saved outputs)."""
        seen: set[str] = set()
        current = None
        while not stop.wait(1.0):
            try:
                for e in group:
                    if e["id"] in seen:
                        continue
                    outs = planned_outputs(plans[e["id"]], project).values()
                    if all(f.is_file() and f.stat().st_mtime >= started - 2 for f in outs):
                        seen.add(e["id"])
                        events({"kind": "project_saved", "project": e["project"]})
                nxt = next((e["project"] for e in group if e["id"] not in seen), None)
                if nxt != current and nxt is not None:
                    current = nxt
                    events({"kind": "current", "project": nxt})
            except OSError:
                pass

    # ------------------------------------------------------------------ system checks
    def _modlist_path(self) -> Optional[Path]:
        mo2 = self.inst.mo2
        try:
            return mo2.modlist_path(self.inst.mo2_profile or None) if mo2 else None
        except Exception:  # noqa: BLE001
            return None

    def _system_fingerprint(self, bdir: Path, batch: dict) -> dict:
        ml = self._modlist_path()
        return {"modlist": str(ml) if ml else "", "modlist_sha256": sha256_file(ml) if ml and ml.is_file() else "",
                "output_mod": batch.get("output_mod", "")}

    def _system_check(self, bdir: Path, batch: dict, todo: list[dict], plans: dict) -> str:
        """'' when the batch may continue; otherwise the SYSTEM failure that makes further sessions unsafe."""
        fp = batch.get("system") or {}
        # the workspace must stay readable and writable
        try:
            _jload(bdir / "batch.json")
            probe = bdir / ".write_probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        except (OSError, ValueError) as exc:
            return f"the batch workspace cannot be used any more ({exc})"
        # MO2 profile (mod order / enabled mods decide which files win)
        ml = fp.get("modlist")
        if ml and fp.get("modlist_sha256"):
            try:
                if sha256_file(Path(ml)) != fp["modlist_sha256"]:
                    return "the MO2 profile (modlist.txt) changed during the conversion; analyze the package again"
            except OSError:
                return f"the MO2 profile file cannot be read any more: {ml}"
        # the output storage
        om = Path(batch.get("output_mod") or "")
        if str(om) and not om.parent.is_dir():
            return f"the output location is not available: {om.parent}"
        if (om / "manifest.json").is_file():
            try:
                _jload(om / "manifest.json")
            except (OSError, ValueError) as exc:
                return f"the output mod manifest cannot be read ({exc})"
        # sources and the reference of every remaining project (size / time; a changed file is re-hashed)
        for e in todo:
            h = e.get("source_hashes", {})
            for sf in plans[e["id"]].staged_files:
                p = Path(sf.abs_path)
                try:
                    st = p.stat()
                except OSError:
                    return f"{'reference' if sf.role.startswith('reference') else 'source'} file disappeared: {sf.rel}"
                if int(st.st_mtime) != int(sf.mtime) or st.st_size != sf.size:
                    if h.get(sf.abs_path) and sha256_file(p) == h[sf.abs_path]:
                        continue
                    kind = "installed CBBE 3BBB reference" if sf.role.startswith("reference") else "source mod"
                    return f"the {kind} changed during the conversion: {sf.rel}"
        return ""

    def _fail_entry(self, bdir: Path, e: dict, plan: ConversionPlan, hashes: dict, reason: str) -> None:
        e["status"] = "FAILED"
        self._write_project_result(bdir, e, plan, ConversionResult(
            id=e["id"], status="FAILED", reasons=[reason], workspace=str(bdir / "projects" / e["id"]),
            output_mod=plan.output_mod_path, source_hashes_before=hashes))

    def _session_of(self, batch: dict, pid: str) -> Optional[dict]:
        """The LATEST session that ran this project (0.5.3 fix: a project retried in a later session was checked against
        the first session's time window, which made every saved retry FAILED — real Wardrobe 10 sessions 7→8)."""
        return next((s for s in reversed(batch.get("sessions", [])) if pid in s["projects"]), None)

    def _validate_all(self, bdir: Path, batch: dict, entries: list[dict], plans: dict, hashes: dict,
                      rt: RuntimeInfo, say: ProgressFn, result_name: str, revalidate: bool = False,
                      events: Optional[EventFn] = None) -> None:
        project = bdir / "Data" / BODYSLIDE_SUBTREE
        events = events or (lambda ev: None)
        for n, e in enumerate(entries, 1):
            say(f"Validating {n}/{len(entries)}: {e['project']}")
            events({"kind": "validating", "project": e["project"]})
            plan = plans[e["id"]]
            pdir = bdir / "projects" / e["id"]
            res = ConversionResult(id=e["id"], workspace=str(pdir), output_mod=plan.output_mod_path,
                                   source_hashes_before=dict(hashes.get(e["id"], {})), runtime=rt.to_dict() if rt else {})
            sess = self._session_of(batch, e["id"])
            try:
                if sess is None:
                    res.reasons.append("Outfit Studio was never started for this project")
                    raise _Stop
                log_file = bdir / "logs" / f"Log_OS.session{sess['id']}.txt"
                log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
                if not log_text and revalidate:
                    log_text = self._recover_log(sess, project)
                (pdir / "logs" / "Log_OS.txt").write_text(log_text, encoding="utf-8")
                res.run = {"route": {k: v for k, v in (plan.route or {}).items() if k != "detection"}
                           if plan.route else {}, "mode": sess.get("mode"), "session": sess["id"], "script": sess["script"],
                           "exit_code": sess.get("exit_code"), "seconds": sess.get("seconds"),
                           "started": sess.get("started"), "ended": sess.get("ended"),
                           "timed_out": sess.get("timed_out"), "cancelled": sess.get("cancelled"), "crashed": sess.get("crashed"),
                           "step_range": sess["ranges"].get(e["id"]), "batch": bdir.name}
                if revalidate:
                    res.run["revalidated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                # a session that ended badly: fatal for this project only when its outputs are missing (Phase 3.2:
                # a project saved before the crash/timeout is judged by its own validation, like any other)
                session_trouble = []
                if sess.get("cancelled"):
                    session_trouble.append("cancelled while Outfit Studio was running")
                if sess.get("crashed"):
                    session_trouble.append(f"Outfit Studio crashed ({sess['crashed']})")
                if sess.get("timed_out"):
                    session_trouble.append("Outfit Studio did not finish in time (a dialog may have blocked it)")
                # 0.5.3: what the unattended control saw for this project
                sr = sess.get("result") or {}
                if sess.get("control"):
                    res.run["control"] = sess["control"]
                    res.run["script_sha256"] = sess.get("script_sha256", "")
                if sr:
                    mine = [x for x in sr.get("automation_errors", []) if x.get("project") == e["project"]]
                    if mine:  # Outfit Studio reported an error for this project: never accepted, whatever it saved
                        res.run["automation_errors"] = mine
                        res.reasons.append("OS_STEP_ERROR: Outfit Studio reported an error while converting this project: "
                                           + "; ".join(f"step {x.get('step')} {x.get('step_type') or ''} "
                                                       f"{x.get('text') or ''}".strip() for x in mine[:3]))
                    cl = [c for c in sr.get("cloth", []) if c.get("project") == e["project"]]
                    if cl:
                        c = cl[-1]
                        res.run["cloth_extra_data"] = {"encountered": True, "policy": c.get("policy"),
                                                       "source": c.get("source"), "verification": c.get("verification"),
                                                       "origins": c.get("origins"), "answer": c.get("action"),
                                                       "offered_origins": c.get("offered_origins",
                                                                                len(c.get("origins") or [])),
                                                       "selected_origin": (c.get("selected_origins") or [""])[0]
                                                       if len(c.get("selected_origins") or []) == 1
                                                       else c.get("selected_origins", []),
                                                       "dialog": c.get("dialog", {})}
                        if c.get("policy") == "ambiguous_not_resolved":
                            res.reasons.append("CLOTH_AMBIGUOUS: Multiple cloth physics data sources require review "
                                               f"(offered: {'; '.join(c.get('origins') or [])})")
                        elif c.get("action") == "stop":
                            session_trouble.append("CLOTH_NOT_SELECTED: cloth physics data could not be selected "
                                                   f"automatically ({c.get('reason')})")
                    elif plan.source_cloth_data:
                        res.run["cloth_extra_data"] = {"encountered": False, "policy": "",
                                                       "note": "Outfit Studio did not ask for cloth data in this session"}
                    if sr.get("status") in ("USER_INTERACTION_REQUIRED", "AUTOMATION_FAILED") \
                            and sr.get("suspect_project") == e["project"] and not cl:
                        session_trouble.append(f"OS_UNEXPECTED_MESSAGE: {sr.get('reason')}")
                interactive = sess.get("mode") == "interactive"
                window = (sess.get("started"), sess.get("ended"))
                if not revalidate:  # 0.5.1: rescale the stored weights of 4-influence-truncated vertices (strict)
                    rn = self._renormalize(plan, project, pdir, sess)
                    if rn is not None:
                        res.run["renormalization"] = rn
                ev = collect_evidence(plan, project, log_text, interactive=interactive, before=sess.get("before", {}).get(e["id"]),
                                      window=window if None not in window else (None, None),
                                      window_source="tool clock around the Outfit Studio process" if None not in window else "",
                                      not_before=(pdir / "plan.json").stat().st_mtime,
                                      step_range=tuple(sess["ranges"][e["id"]]) if e["id"] in sess.get("ranges", {}) else None)
                res.run["execution_evidence"] = ev.to_dict()
                sl = parse_log_session(log_text)
                res.run["steps_logged"] = len(sl.steps_in(*sess["ranges"][e["id"]])) if e["id"] in sess.get("ranges", {}) else 0
                if ev.outputs_missing:
                    res.reasons += session_trouble
                    res.reasons.append("the conversion script did not save this project during the Outfit Studio session"
                                       + (" (window closed without Execute, or the run was aborted before it)" if interactive else ""))
                elif session_trouble:
                    res.run["session_trouble"] = session_trouble  # saved before it: validated on its own
                if not interactive and sess.get("exit_code") not in (0, 10):
                    res.reasons.append(f"Outfit Studio automation exit code {sess.get('exit_code')}")
                if not interactive and sess.get("exit_code") == 10 and not ev.ok and not sl.error_lines:
                    res.reasons.append("Outfit Studio reported a failed step (exit code 10)")
                val = self.eng._validate(plan, project, pdir, res, ev,
                                         "validation.revalidated.txt" if revalidate else "validation.txt", None,
                                         data_root=bdir / "Data")
                cxm = (res.validation.get("metrics") or {}).get("cloth_extra_data")
                if cxm:  # 0.5.3: source vs generated cloth payloads (size + SHA-256), whatever the dialog did
                    cx = res.run.setdefault("cloth_extra_data", {"encountered": False, "policy": ""})
                    sp, gp = cxm.get("source_payloads") or [], cxm.get("generated_payloads") or []
                    cx.update({"source_payload_size": sp[0]["size"] if len(sp) == 1 else [x["size"] for x in sp],
                               "generated_payload_size": gp[0]["size"] if len(gp) == 1 else [x["size"] for x in gp],
                               "source_payload_sha256": sp[0]["sha256"] if len(sp) == 1 else [x["sha256"] for x in sp],
                               "generated_payload_sha256": gp[0]["sha256"] if len(gp) == 1 else [x["sha256"] for x in gp],
                               "payload_match": bool(cxm.get("payload_match"))})
                if not val.passed:
                    res.reasons += [f"validation failed: {c.name} — {c.detail}" for c in val.failures()]
                status = project_status(res.validation.get("checks", []))
                if any(not r.startswith("validation failed") for r in res.reasons):
                    status = "FAILED"  # not executed, crashed, cancelled, exit code: never REVIEW
                res.status = status
                if status == "VALIDATED":
                    res.run["generated_sha256"] = {k: sha256_file(p) for k, p in planned_outputs(plan, project).items()}
            except _Stop:
                res.status = "FAILED"
            except Exception as exc:  # noqa: BLE001 - one project must never break the batch
                log.exception("batch validation crashed for %s", e["project"])
                res.status = "FAILED"
                res.reasons.append(f"validation crashed: {exc}")
            e["status"] = res.status
            if res.status != "VALIDATED":
                e["failure"] = FAILURE_PROJECT
            else:
                e.pop("failure", None)
            self._write_project_result(bdir, e, plan, res, result_name)
            _jsave(bdir / "batch.json", batch)  # checkpoint after every project
            events({"kind": "project_result", "project": e["project"], "status": res.status,
                    "reasons": list(res.reasons[:6])})

    @staticmethod
    def _renormalize(plan: ConversionPlan, project: Path, pdir: Path, sess: dict) -> Optional[dict]:
        """0.5.1 post-export proportional skin-weight renormalization of this project's generated ShapeData NIF.

        Runs before validation, on the converted shapes only; the untouched Outfit Studio file is kept in
        ``projects/<id>/generated_raw/``. The full validation afterwards reads the repaired file from disk."""
        from .renormalize import renormalize_skin_weights

        nif = planned_outputs(plan, project)["ShapeData NIF"]
        started = sess.get("started") or 0
        try:
            if not nif.is_file() or nif.stat().st_mtime < started - 2:
                return None
        except OSError:
            return None
        rn = renormalize_skin_weights(nif, pdir / "generated_raw" / nif.name, [sp.name for sp in plan.shape_plans])
        d = rn.to_dict()
        _jsave(pdir / "renormalization.json", d)
        return d

    def _recover_log(self, sess: dict, project: Path) -> str:
        """After a tool crash: the runtime's Log_OS.txt, if it belongs to this batch's project folder."""
        f = Path(sess.get("runtime_folder", "")) / "Log_OS.txt"
        if not f.is_file():
            return ""
        text = f.read_text(encoding="utf-8", errors="replace")
        from .evidence import _norm

        return text if _norm(str(project)) in [_norm(p) for p in parse_log_session(text).project_paths] else ""

    @staticmethod
    def _write_project_result(bdir: Path, e: dict, plan: ConversionPlan, res: ConversionResult,
                              name: str = "result.json") -> None:
        pdir = bdir / "projects" / e["id"]
        pdir.mkdir(parents=True, exist_ok=True)
        d = res.to_dict()
        d["project"] = e["project"]
        d["generated_project"] = plan.output_project
        _jsave(pdir / name, d)

    def _finish(self, bdir: Path, batch: dict, state: str, say: ProgressFn) -> dict:
        batch["state"] = state
        _jsave(bdir / "batch.json", batch)
        summary = self.summary(bdir, batch)
        c = summary["counts"]
        say(f"Batch {state.lower()}: validated {c.get('VALIDATED', 0)}, review {c.get('REVIEW', 0)}, "
            f"failed {c.get('FAILED', 0)}, committed {c.get('COMMITTED', 0)}")
        return summary

    # ------------------------------------------------------------------ validate (no Outfit Studio)
    def validate(self, bdir: Path, progress: Optional[ProgressFn] = None) -> dict:
        say = progress or (lambda m: None)
        bdir = Path(bdir)
        with self._lock:
            batch = self._load(bdir)
            if not batch.get("sessions"):
                raise BatchError("Outfit Studio never ran for this batch; nothing to validate")
            entries = [e for e in batch["projects"] if e["status"] != "COMMITTED" and self._session_of(batch, e["id"])]
            plans = {e["id"]: self._plan(bdir, e) for e in entries}
            hashes = {}
            for e in entries:
                r = self.latest_result(bdir, e) or {}
                hashes[e["id"]] = r.get("source_hashes_before") or e.get("source_hashes", {})
            rt = RuntimeInfo(**{k: v for k, v in batch.get("runtime", {}).items() if k in RuntimeInfo.__dataclass_fields__})
            self._validate_all(bdir, batch, entries, plans, hashes, rt, say, "result.revalidated.json", revalidate=True)
            return self._finish(bdir, batch, "VALIDATED" if batch["state"] != "PARTIALLY_COMMITTED" else batch["state"], say)

    # ------------------------------------------------------------------ commit
    def commit(self, bdir: Path, only: Optional[list[str]] = None, progress: Optional[ProgressFn] = None) -> dict:
        """Write VALIDATED projects to the output mod, one transaction per project."""
        say = progress or (lambda m: None)
        bdir = Path(bdir)
        with self._lock:
            batch = self._load(bdir)
            if batch.get("acceptance"):  # 0.5.3: an acceptance batch is disposable by construction — never installed
                raise BatchError("this is an ACCEPTANCE batch (disposable output, forced no-commit): it is never installed")
            package = batch["package"]
            pgroup = package_group_name(package)
            for e in batch["projects"]:
                if only is not None and e["project"] not in only:
                    continue
                if e["status"] != "VALIDATED":
                    continue
                plan = self._plan(bdir, e)
                r = self.latest_result(bdir, e) or {}
                res = ConversionResult(id=f"{bdir.name}/{e['id']}", status="VALIDATED", workspace=str(bdir / "projects" / e["id"]),
                                       output_mod=plan.output_mod_path, validation=r.get("validation", {}), run=r.get("run", {}),
                                       source_hashes_before=r.get("source_hashes_before", {}),
                                       generated_classification=r.get("generated_classification", {}), runtime=r.get("runtime", {}))
                problems = []
                project = bdir / "Data" / BODYSLIDE_SUBTREE
                gen = res.run.get("generated_sha256", {})
                for k, p in planned_outputs(plan, project).items():
                    if not p.is_file() or sha256_file(p) != gen.get(k):
                        problems.append(f"generated {k} changed after validation; run validate-batch again")
                for path, h in res.source_hashes_before.items():
                    if not Path(path).is_file() or sha256_file(Path(path)) != h:
                        problems.append(f"source file changed since the run: {path}")
                if not plan.output_mod_path:
                    problems.append("no output mod in the plan")
                else:
                    problems += check_output_mod(Path(plan.output_mod_path), self.inst, plan.files_to_write[:2])
                if not res.source_hashes_before:
                    problems.append("no source hashes recorded")
                if problems:
                    e["commit_error"] = problems
                    say(f"Not committed: {e['project']}: {problems[0]}")
                    continue
                say(f"Committing {e['project']}…")
                from .validate import Check, ValidationReport

                val = ValidationReport(checks=[Check(c["name"], c["ok"], c.get("detail", ""), c.get("required", True))
                                               for c in res.validation.get("checks", [])],
                                       metrics=res.validation.get("metrics", {}))
                rt = RuntimeInfo(**{k: v for k, v in res.runtime.items() if k in RuntimeInfo.__dataclass_fields__})
                shapes = _jload(bdir / "projects" / e["id"] / "shapes.json") if (bdir / "projects" / e["id"] / "shapes.json").is_file() else []
                extra = {
                    "accepted_by": f"batch {bdir.name}",
                    "batch": {"id": bdir.name, "package": package, "session": res.run.get("session"),
                              "workspace": str(bdir / "projects" / e["id"])},
                    "confidence": e.get("confidence"),
                    "regions_by_shape": {sp.name: {"regions": sp.regions, "allowed_bones": sp.allowed_bones,
                                                   "copy_mode": sp.copy_mode or plan.weight_mode, "confidence": sp.confidence}
                                         for sp in plan.shape_plans},
                    "shape_decisions": [{"name": s.get("name"), "decision": s.get("decision"), "regions": s.get("regions")}
                                        for s in shapes],
                    "validation_result": res.status,
                    "conversion_mode": plan.conversion_mode,
                    "route": {**{k: v for k, v in (batch.get("route") or DEFAULT_ROUTE.to_dict()).items()
                                 if k in ("id", "source_body_family", "target_body_family", "physics_profile",
                                          "geometry_conversion_strategy", "weight_transfer_strategy")},
                              # 0.6.0 (additive): this project's own body route
                              **{k: v for k, v in (plan.route or {}).items()
                                 if k in ("route_id", "source_body", "target_body", "requested_source", "route_status",
                                          "geometry_stage", "physics_stage", "validator")}},
                    **({"body_detection": {k: v for k, v in plan.route["detection"].items()
                                           if k in ("body", "confidence", "physics", "evidence", "conflicting_evidence",
                                                    "source")}}
                       if (plan.route or {}).get("detection") else {}),
                    "acceptance": {"state": "committed, awaiting in-game test", "history": []},
                }
                if res.run.get("cloth_extra_data", {}).get("encountered"):  # 0.5.3: how the cloth dialog was answered
                    cx = dict(res.run["cloth_extra_data"])
                    cx["validated"] = {c["name"]: c["ok"] for c in res.validation.get("checks", [])
                                       if c["name"].startswith("cloth physics data")}
                    extra["cloth_extra_data"] = cx
                if res.run.get("control"):
                    extra.setdefault("outfit_studio_control", {"control": res.run.get("control"),
                                                               "session": res.run.get("session"),
                                                               "script": res.run.get("script"),
                                                               "script_sha256": res.run.get("script_sha256", "")})
                rn = res.run.get("renormalization") or {}
                if rn.get("changed"):  # 0.5.1: the repair is part of the record
                    extra["skin_weight_renormalization"] = {
                        k: rn.get(k) for k in ("status", "vertices_renormalized", "shapes", "raw_sha256", "final_sha256")}
                    extra["skin_weight_renormalization"]["rule"] = "new_weight_i = old_weight_i / sum(old_weights)"
                if plan.conversion_mode == "embedded_body_replacement":
                    br = dict(plan.body_replacement)
                    br["validation"] = {c["name"]: c["ok"] for c in res.validation.get("checks", []) if c["name"].startswith("body replacement")}
                    extra["body_replacement"] = br
                res.status = "SUCCESS"
                try:
                    files = self.eng._commit(plan, project, res, rt, val, package_group=pgroup, record_extra=extra)
                except Exception as exc:  # noqa: BLE001 - per-project transaction
                    e["commit_error"] = [f"could not write the output mod: {exc}"]
                    say(f"Not committed: {e['project']}: {exc}")
                    continue
                e["status"] = "COMMITTED"
                e.pop("commit_error", None)
                e["committed"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": files}
                _jsave(bdir / "projects" / e["id"] / "commit.json", e["committed"])
            done = [e for e in batch["projects"] if e["status"] == "COMMITTED"]
            state = "COMMITTED" if done and len(done) == len(batch["projects"]) else ("PARTIALLY_COMMITTED" if done else batch["state"])
            return self._finish(bdir, batch, state, say)

    # ------------------------------------------------------------------ status / report
    def summary(self, bdir: Path, batch: Optional[dict] = None) -> dict:
        bdir = Path(bdir)
        batch = batch or self._load(bdir)
        rows = []
        for e in batch["projects"]:
            r = self.latest_result(bdir, e) or {}
            failed = [c for c in r.get("validation", {}).get("checks", []) if c.get("required", True) and not c.get("ok")]
            rows.append({"project": e["project"], "generated_project": e.get("generated_project"), "status": e["status"],
                         "regions": e.get("regions", []), "confidence": e.get("confidence"),
                         "reasons": r.get("reasons", [])[:6], "failed_checks": [c["name"] for c in failed],
                         "checks": len(r.get("validation", {}).get("checks", [])),
                         "commit_error": e.get("commit_error", []), "committed": e.get("committed")})
        counts: dict[str, int] = {}
        for x in rows:
            counts[x["status"]] = counts.get(x["status"], 0) + 1
        summary = {"id": batch["id"], "package": batch["package"], "state": batch["state"], "tool_version": __version__,
                   "updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "output_mod": batch.get("output_mod"),
                   "groups": batch.get("groups", []), "counts": counts, "projects": rows,
                   "not_selected": batch.get("not_selected", []),
                   "sessions": [{k: v for k, v in s.items() if k not in ("before",)} for s in batch.get("sessions", [])]}
        _jsave(bdir / "summary.json", summary)
        (bdir / "report.txt").write_text(summary_text(summary), encoding="utf-8")
        (bdir / "report.html").write_text(summary_html(summary), encoding="utf-8")
        return summary


class _Stop(Exception):
    pass


def build_instructions(summary: dict) -> list[str]:
    committed = [p["generated_project"] for p in summary["projects"] if p["status"] == "COMMITTED"]
    if not committed:
        return []
    groups = summary.get("groups", [])
    group = groups[-1] if groups else MAIN_GROUP
    return [
        "BodySlide build (manual, separate stage):",
        f"  Build only: {group}",
        "  Preset: CBBE Curvy, Build Morphs: ON → Batch Build → Build",
        f"  ({len(committed)} converted outfits in the group)",
        "  Do not batch-build the original projects of this mod after the AUTO group: both target the same game mesh "
        "files (BodySlide would otherwise ask 'Choose output set', and the last build wins).",
        "  Then check the build: run_batch.bat verify-package-build --profile <NAME> --package <MOD>.",
        "  The in-game item can have a different name than the project (see GAME_ITEM_NAME_MISMATCH).",
    ]


def summary_text(s: dict) -> str:
    L = [f"Batch {s['id']} — package {s['package']} — state {s['state']}",
         "Counts: " + ", ".join(f"{k} {v}" for k, v in sorted(s["counts"].items())), ""]
    for p in s["projects"]:
        L.append(f"{p['status']:<11} {p['project']}  →  {p['generated_project']}  ({', '.join(r.lower() for r in p['regions'])}; "
                 f"{p['confidence']}; {p['checks']} checks)")
        L += [f"    - {r}" for r in p["reasons"]]
        L += [f"    ! commit: {r}" for r in p["commit_error"]]
    if s.get("not_selected"):
        L += ["", "Not selected:"]
        L += [f"  {x['action']:<11} {x['project']} ({x['status']}): {x['reason']}" for x in s["not_selected"]]
    L += [""] + build_instructions(s)
    return "\n".join(L) + "\n"


def summary_html(s: dict) -> str:
    import html as _h

    e = _h.escape
    rows = "".join(
        f"<tr><td><b>{e(p['status'])}</b></td><td>{e(p['project'])}</td><td>{e(p['generated_project'] or '')}</td>"
        f"<td>{e(', '.join(r.lower() for r in p['regions']))}</td><td>{e(str(p['confidence']))}</td>"
        f"<td>{e('; '.join(p['reasons'] + p['commit_error']))}</td></tr>" for p in s["projects"])
    ns = "".join(f"<tr><td>{e(x['action'])}</td><td>{e(x['project'])}</td><td>{e(x['status'])}</td><td>{e(x['reason'])}</td></tr>"
                 for x in s.get("not_selected", []))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Batch Report</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{{--bg:#fff;--fg:#1d1d1f;--line:#ddd}}@media (prefers-color-scheme:dark){{:root{{--bg:#1b1b1d;--fg:#eee;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif;margin:16px}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}}div{{overflow-x:auto}}</style></head><body>
<h1>Batch {e(s['id'])}</h1><p>Package {e(s['package'])} · state {e(s['state'])} · {e(', '.join(f'{k} {v}' for k, v in sorted(s['counts'].items())))}</p>
<div><table><tr><th>Status</th><th>Project</th><th>Generated</th><th>Regions</th><th>Confidence</th><th>Reasons</th></tr>{rows}</table></div>
<h2>Not selected</h2><div><table><tr><th>Action</th><th>Project</th><th>Status</th><th>Reason</th></tr>{ns}</table></div>
<pre>{e(chr(10).join(build_instructions(s)))}</pre></body></html>"""
