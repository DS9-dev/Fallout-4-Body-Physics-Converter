"""Phase 3.2 (Parts P/Q/R): one-click package conversion for normal users.

Internally the same stages as the CLI (analyze → plan → convert → validate → commit), exposed as one operation:

    result = PackagePipeline(ctl, "Clothes Pack 5 CBBE BodySlide", mode="SAFE").run()

* ``mode`` ``SAFE`` converts only what the program can verify with high confidence; ``ALL_POSSIBLE`` also tries
  uncertain outfits (MEDIUM confidence, REVIEW_ATTEMPTABLE). Validation is identical in both modes and only validated
  results are installed.
* Individual outfit failures are expected batch outcomes: they are recorded and the batch continues. Validated
  results are installed session by session (``commit=True``), so an interruption keeps everything finished so far,
  and :meth:`PackagePipeline.resume` continues from the unfinished projects.
* Progress is reported as events (``{"kind", "level", "text", ...}``) with a level — ``NORMAL`` (user-relevant),
  ``DETAILED`` (per-project results with a human reason) or ``DEBUG`` (engine text) — so a GUI can show a calm
  progress view and keep the full log one click away.
* The final :class:`PipelineResult` names every outfit that could not be converted (in-game item name first, then the
  BodySlide project, then the mesh), with a short human reason and the exact technical reasons for bug reports.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..conversion.outcomes import (MODE_ALL_POSSIBLE, MODE_SAFE, POLICY_ATTEMPTABLE, POLICY_BLOCKED, applicable_percent,
                                   display_name, final_bucket, outcome_for, package_result)
from .messages import (CONTROL_FAILURE_TEXT, failed_check, LEFT_UNCHANGED, OUTCOME_LABELS, RESULT_LABELS, RUNTIME_STATES,
                       UNATTENDED_NOTE, humanize, package_message, short_reason, system_message)

__all__ = ["PackagePipeline", "PipelineResult", "analysis_overview", "LEVELS", "result_text", "problem_lines"]

log = logging.getLogger(__name__)
LEVELS = ("NORMAL", "DETAILED", "DEBUG")
PROBLEM_OUTCOMES = ("COULD_NOT_CONVERT_SAFELY", "FAILED", "UNFINISHED")
EventFn = Callable[[dict], None]


@dataclass
class PipelineResult:
    package: str
    mode: str = MODE_SAFE
    state: str = ""  # COMPLETE / COMPLETE_WITH_WARNINGS / INTERRUPTED / SYSTEM_ERROR
    state_label: str = ""
    batch_id: str = ""
    batch_dir: str = ""
    batch_state: str = ""
    total: int = 0
    counts: dict = field(default_factory=dict)  # outcome -> n
    percent: Optional[int] = None
    items: list[dict] = field(default_factory=list)  # one per project of the package
    warnings: list[dict] = field(default_factory=list)  # {"code", "message"}
    system_failure: str = ""
    system_message: str = ""
    build: dict = field(default_factory=dict)
    representative: list[dict] = field(default_factory=list)
    buckets: dict = field(default_factory=dict)  # report bucket -> [project names]
    finished: str = ""

    @property
    def problems(self) -> list[dict]:
        return [i for i in self.items if i["outcome"] in PROBLEM_OUTCOMES]

    @property
    def needs_attention(self) -> list[dict]:
        """0.5.2: every outfit that did not get the requested physics — not converted, blocked, failed, not tried in
        this mode, unfinished — plus converted outfits with an in-game problem reported by the user. A usable
        original is not a successful conversion."""
        return [i for i in self.items if i.get("attention")]

    @property
    def completed(self) -> list[dict]:
        """Outfits that have 3BBB physics now (converted or already compatible) and no reported in-game problem."""
        return [i for i in self.items if i["outcome"] in ("CONVERTED", "ALREADY_COMPATIBLE") and not i.get("attention")]

    def attention_lines(self, limit: Optional[int] = None) -> list[str]:
        L = [f"- {i['name']} — {i['short']}" for i in self.needs_attention]
        if limit is not None and len(L) > limit:
            L = L[:limit] + [f"+ {len(self.needs_attention) - limit} more…"]
        return L

    def problem_names(self, limit: Optional[int] = None) -> list[str]:
        names = [i["name"] for i in self.problems]
        return names if limit is None else names[:limit]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["problems"] = self.problems
        d["needs_attention"] = self.needs_attention
        d["summary"] = {"completed": len(self.completed), "needs_attention": len(self.needs_attention),
                        "no_conversion_needed": self.counts.get("NO_CONVERSION_NEEDED", 0)}
        return d


def analysis_overview(prev) -> dict:
    """What a normal user sees after Analyze (Part R): real counts from the Batch Preview."""
    ready = [p for p in prev.projects if p.action == "CONVERT" and p.selectable and p.status == "NEW" and p.attempt_origin == "SAFE"]
    extra = [p for p in prev.projects if p.plan and (
        (p.action == "CONVERT" and p.selectable and p.status == "NEW" and p.attempt_origin == "MEDIUM")
        or p.review_policy == POLICY_ATTEMPTABLE)]
    already = [p for p in prev.projects if p.action == "KEEP"]
    none = [p for p in prev.projects if p.action == "SKIP"]
    cannot = [p for p in prev.projects if p not in ready and p not in extra and p not in already and p not in none]
    return {"package": prev.package, "total": len(prev.projects), "ready": len(ready), "try_all_possible": len(extra),
            "already_compatible": len(already), "no_conversion_needed": len(none), "cannot_process": len(cannot),
            "cannot_names": [p.display_name for p in cannot], "try_names": [p.display_name for p in extra],
            "output_mod_state": prev.output_mod_state, "codes": list(prev.codes), **body_overview(prev)}


def body_overview(prev) -> dict:
    """0.6.0: the detected source bodies and the routes of a preview, for normal users (labels, not ids)."""
    from ..conversion.bodies import body_label

    bodies: dict[str, int] = {}
    routes: dict[str, dict] = {}
    for p in prev.projects:
        b = getattr(p, "source_body", "") or "unknown"
        bodies[body_label(b)] = bodies.get(body_label(b), 0) + 1
        rl = getattr(p, "route_label", "") or "—"
        r = routes.setdefault(rl, {"count": 0, "status": getattr(p, "route_status", ""),
                                   "geometry": (getattr(p, "geometry_stage", {}) or {}).get("status", ""),
                                   "physics": (getattr(p, "physics_stage", {}) or {}).get("status", "")})
        r["count"] += 1
    return {"requested_source": body_label(getattr(prev, "requested_source", "auto") or "auto"),
            "target_body": body_label(getattr(prev, "target_body", "cbbe_3bbb") or "cbbe_3bbb"),
            "bodies": dict(sorted(bodies.items(), key=lambda kv: -kv[1])),
            "routes": dict(sorted(routes.items(), key=lambda kv: -kv[1]["count"]))}


ROUTE_STATUS_TEXT = {"PRODUCTION": "Ready to convert", "LEGACY_SUPPORTED": "Converted as before (CBBE-derived body)",
                     "ALREADY_COMPATIBLE": "Already compatible", "NOT_IMPLEMENTED": "Needs attention / route not implemented yet",
                     "UNSUPPORTED": "Not supported", "REVIEW": "Needs attention (body not determined)",
                     "SOURCE_OVERRIDE_CONFLICT": "Does not match the chosen source body",
                     "SOURCE_UNVERIFIED": "Body not determined for the chosen source body"}
STAGE_TEXT = {"NOT_REQUIRED": "not required", "PRODUCTION": "required", "LEGACY_SUPPORTED": "not required (as before)",
              "NOT_IMPLEMENTED": "not implemented", "NOT_APPLICABLE": "—"}


BUILD_STATUS_TEXT = (("BUILD_OK", "built correctly from the generated 3BBB project (weights compared)"),
                     ("NOT_BUILT", "not built yet (no built game mesh found)"),
                     ("BUILD_STALE", "built before the conversion or from the original project (rebuild the AUTO group)"),
                     ("BUILD_MISMATCH", "built mesh does not match the generated project (rebuild the AUTO group)"),
                     ("ERROR", "could not be checked (generated file missing or a mesh unreadable)"))


def build_summary_lines(v: dict) -> list[str]:
    """0.6.1: 'Check the build' for normal users: one line per status, then what to do (statuses are exclusive)."""
    c = v.get("counts", {}) or {}
    total = v.get("committed", 0)
    lines = [f"Built correctly: {c.get('BUILD_OK', 0)} of {total} converted outfits."]
    for key, text in BUILD_STATUS_TEXT[1:]:
        if c.get(key):
            lines.append(f"  {c[key]} {text}")
    return lines + [x for x in v.get("explanation", []) if not x.startswith("All ")]


def route_lines(ov: dict) -> list[str]:
    """User-facing body/route summary (Part K wording)."""
    L = [f"Source body: {ov.get('requested_source', 'Auto Detect')}   Target body: {ov.get('target_body', 'CBBE 3BBB')}",
         "Detected: " + ", ".join(f"{k} {v}" for k, v in ov.get("bodies", {}).items())]
    for label, r in ov.get("routes", {}).items():
        geo = STAGE_TEXT.get(r["geometry"], r["geometry"])
        phy = ("available after geometry conversion" if r["geometry"] == "NOT_IMPLEMENTED"
               else STAGE_TEXT.get(r["physics"], r["physics"]))
        L.append(f"  {label}: {r['count']} — geometry conversion: {geo}; physics conversion: {phy}; "
                 f"status: {ROUTE_STATUS_TEXT.get(r['status'], r['status'])}")
    return L


class PackagePipeline:
    def __init__(self, ctl, package: str, mode: str = MODE_SAFE, commit: bool = True, events: Optional[EventFn] = None,
                 runner: Optional[Callable] = None, output_mod: Optional[str] = None,
                 control_failure: Optional[Callable[[dict], str]] = None, route: Optional[str] = None,
                 source_body: Optional[str] = None, target_body: Optional[str] = None):
        self.ctl = ctl
        self.package = package
        self.mode = (mode or MODE_SAFE).upper().replace("-", "_")
        if self.mode not in (MODE_SAFE, MODE_ALL_POSSIBLE):
            raise ValueError(f"unknown conversion mode {mode}")
        self.commit = commit
        self._events = events or (lambda e: None)
        self.runner = runner
        self.output_mod = output_mod
        # 0.5.3: asked when the automatic Outfit Studio control cannot continue → "retry" | "manual" | "stop"
        self.control_failure = control_failure
        from ..conversion.routes import resolve_route

        self.route = resolve_route(route)  # only CBBE → CBBE 3BBB is implemented (raises for anything else)
        # 0.6.0: requested bodies (defaults = 0.5.x: Auto Detect → CBBE 3BBB); a pairing that cannot run raises
        # RouteNotImplemented here, before anything is analyzed or started
        from ..conversion.routes import resolve_request

        self.source_body, self.target_body = resolve_request(source_body, target_body)
        self.preview = None
        self.bdir: Optional[Path] = None
        self._names: dict[str, str] = {}
        self._live = {"CONVERTED": 0, "COULD_NOT_CONVERT_SAFELY": 0, "FAILED": 0}
        self._todo = 0
        self._done = 0

    # ------------------------------------------------------------------ events
    def emit(self, kind: str, text: str = "", level: str = "NORMAL", **data) -> None:
        ev = {"kind": kind, "level": level, "text": text, "time": time.strftime("%H:%M:%S"), **data}
        if level != "DEBUG":
            log.info("[%s] %s", kind, text)
        try:
            self._events(ev)
        except Exception:  # noqa: BLE001 - a broken listener must not stop a conversion
            log.exception("pipeline event listener failed")

    def _name(self, project: str) -> str:
        return self._names.get(project, project)

    def _on_engine(self, ev: dict) -> None:
        k = ev.get("kind")
        if k == "current" and ev.get("project") == getattr(self, "_last_current", None):
            return  # 0.5.3: the session start and the output monitor both name the first project: report it once
        if k == "current":
            self._last_current = ev.get("project")
            self.emit("current", f"Converting: {self._name(ev['project'])}", "DETAILED", project=ev["project"],
                      name=self._name(ev["project"]))
        elif k == "session_start":
            n = len(ev.get("projects", []))
            if ev.get("unattended"):
                self.emit("session_start", f"Converting {n} outfit(s) in Outfit Studio. {UNATTENDED_NOTE}", "DETAILED",
                          session=ev.get("session"), projects=ev.get("projects"), unattended=True)
            elif ev.get("interactive"):
                self.emit("session_start", f"Outfit Studio opened for {n} outfit(s): press Execute in its Automation "
                                           "window, then close Outfit Studio when it has finished.", "NORMAL",
                          session=ev.get("session"), projects=ev.get("projects"), instructions=ev.get("instructions", ""))
            else:
                self.emit("session_start", f"Converting {n} outfit(s)…", "DETAILED", session=ev.get("session"))
            if ev.get("projects") and ev["projects"][0] != getattr(self, "_last_current", None):
                self._last_current = ev["projects"][0]
                self.emit("current", f"Converting: {self._name(ev['projects'][0])}", "DETAILED",
                          project=ev["projects"][0], name=self._name(ev["projects"][0]))
        elif k == "project_result":
            st = ev.get("status")
            self._done += 1
            outcome = {"VALIDATED": "CONVERTED", "REVIEW": "COULD_NOT_CONVERT_SAFELY", "FAILED": "FAILED"}.get(st, "FAILED")
            self._live[outcome] = self._live.get(outcome, 0) + 1
            name = self._name(ev["project"])
            if outcome == "CONVERTED":
                self.emit("item_ok", f"Converted: {name}", "DETAILED", project=ev["project"], name=name,
                          done=self._done, total=self._todo, counts=dict(self._live))
            else:
                code, msg = humanize(outcome, ev.get("reasons", []))
                self.emit("item_problem", f"Could not convert: {name} — {msg} Continuing…", "NORMAL", project=ev["project"],
                          name=name, outcome=outcome, code=code, message=msg, technical=ev.get("reasons", []),
                          done=self._done, total=self._todo, counts=dict(self._live))
            self.emit("progress", "", "DEBUG", done=self._done, total=self._todo, counts=dict(self._live))
        elif k == "session_failure":
            iso = ev.get("isolated")
            self.emit("session_failure", "Outfit Studio stopped; the unfinished outfits are tried again in a new "
                                         "Outfit Studio session" + (f" ({self._name(iso)} on its own)." if iso else "."),
                      "NORMAL", reason=ev.get("reason"), retry=ev.get("retry"), isolated=iso)
        elif k == "control_failure":
            self.emit("control_failure", CONTROL_FAILURE_TEXT if ev.get("choice") == "stop" else
                      ("Continuing this session manually." if ev.get("choice") == "manual" else
                       "Retrying automatically."), "NORMAL", **{x: ev.get(x) for x in ("choice", "script", "reason", "session")})
        elif k == "os_control":
            data = {kk: vv for kk, vv in ev.items() if kk not in ("kind", "text", "level", "time")}
            self.emit("os_control", f"Outfit Studio: {ev.get('event', '')}", "DEBUG", **data)
        elif k == "system_failure":
            self.emit("system_failure", system_message(ev.get("reason", "")), "NORMAL", reason=ev.get("reason"))
        elif k == "validating":
            self.emit("validating", f"Checking: {self._name(ev['project'])}", "DEBUG", project=ev["project"])

    def _apply_bodies(self, prev) -> None:
        """0.6.0: route every project for the requested Source/Target body (no-op for Auto Detect → CBBE 3BBB)."""
        from ..conversion.package import apply_routing

        if (prev.requested_source or "auto", prev.target_body or "cbbe_3bbb") == (self.source_body, self.target_body) \
                and all(p.route_id for p in prev.projects):
            return
        demoted = apply_routing(prev, self.source_body, self.target_body)
        if demoted:
            self.emit("warning", f"{len(demoted)} outfit(s) do not match the chosen source body and are not converted",
                      "NORMAL", code="SOURCE_BODY_MISMATCH", projects=demoted)

    # ------------------------------------------------------------------ stages
    def analyze(self):
        self.emit("stage", "Analyzing package…", stage="analyze")
        prev = self.ctl.analyze_package(self.package, output_mod=self.output_mod, write=True,
                                        progress=lambda d, t, m: self.emit("analyze_progress", m, "DEBUG", done=d, total=t))
        self._apply_bodies(prev)
        self.preview = prev
        self._names = {p.project: p.display_name for p in prev.projects}
        ov = analysis_overview(prev)
        self.emit("analysis", f"{ov['total']} outfits/projects found", overview=ov)
        for code in prev.codes:
            self.emit("warning", package_message(code, Path(prev.output_mod).name if prev.output_mod else ""), code=code)
        return prev

    def run(self, preview=None) -> PipelineResult:
        """Analyze (unless ``preview`` is given), select by mode, convert, validate and install. Never raises for an
        individual outfit; a SYSTEM failure ends in state SYSTEM_ERROR."""
        prev = preview or self.preview or self.analyze()
        if (prev.requested_source or "auto", prev.target_body or "cbbe_3bbb") != (self.source_body, self.target_body):
            from ..conversion.package import PackagePreview

            prev = PackagePreview.from_dict(prev.to_dict())  # a copy: the caller's preview keeps its own routing
            self._apply_bodies(prev)
        self.preview = prev
        self._names = {p.project: p.display_name for p in prev.projects}
        rt = prev.runtime or {}
        if not rt.get("headless_supported"):  # a package-level problem, not 100 individual outfit failures
            why = f"Outfit Studio not usable: {rt.get('reason') or 'not found'}"
            self.emit("system_failure", system_message(why), reason=why)
            r = self.result(prev, None)
            for i in r.items:
                if i["outcome"] in ("COULD_NOT_CONVERT_SAFELY", "SKIPPED") and i["action"] == "CONVERT":
                    i["outcome"], i["outcome_label"] = "UNFINISHED", OUTCOME_LABELS["UNFINISHED"]
            r.counts = {}
            for i in r.items:
                r.counts[i["outcome"]] = r.counts.get(i["outcome"], 0) + 1
            r.state, r.state_label = "SYSTEM_ERROR", RESULT_LABELS["SYSTEM_ERROR"]
            r.system_failure, r.system_message = why, system_message(why)
            return r
        self.emit("stage", "Planning conversions…", stage="plan")
        selected = prev.select_mode(self.mode)
        self.ctl.plan_batch(prev, write=True)
        if not selected:
            self.emit("stage", "Nothing to convert in this mode.", stage="done")
            return self.result(prev, None)
        self._todo = len(selected)
        from ..conversion.batch import BatchError

        eng = self.ctl._batch_engine(self.runner)
        self.ctl._cancel.clear()
        try:
            self.bdir = eng.create(prev, lambda m: self.emit("engine", m, "DEBUG"))
        except BatchError as exc:
            self.emit("system_failure", str(exc))
            r = self.result(prev, None)
            r.state, r.system_failure = "SYSTEM_ERROR", str(exc)
            r.state_label, r.system_message = RESULT_LABELS["SYSTEM_ERROR"], str(exc)
            return r
        self.emit("stage", f"Converting {len(selected)} outfit(s)…", stage="convert", total=len(selected),
                  batch=self.bdir.name)
        eng.run(self.bdir, progress=lambda m: self.emit("engine", m, "DEBUG"), cancel=self.ctl._cancel.is_set,
                events=self._on_engine, on_session_done=self._install if self.commit else None,
                control_failure=self.control_failure)
        return self._finish(prev)

    def resume(self, batch, control: Optional[str] = None) -> PipelineResult:
        """Continue an interrupted package conversion; ``control`` = "manual" | "unattended" | "auto" (0.5.3)."""
        from ..conversion.package import PackagePreview

        eng = self.ctl._batch_engine(self.runner)
        self.bdir = self.ctl.resolve_batch(batch)
        prev = PackagePreview.from_dict(json.loads((self.bdir / "preview.json").read_text(encoding="utf-8")))
        self.preview = prev
        self.package = prev.package
        self.mode = prev.conversion_mode or self.mode
        self._names = {p.project: p.display_name for p in prev.projects}
        b = json.loads((self.bdir / "batch.json").read_text(encoding="utf-8"))
        self._todo = len(b["projects"])
        self._done = sum(1 for e in b["projects"] if e["status"] in ("VALIDATED", "REVIEW", "FAILED", "COMMITTED"))
        self.emit("stage", "Resuming package conversion…", stage="convert", batch=self.bdir.name)
        self.ctl._cancel.clear()
        eng.resume(self.bdir, progress=lambda m: self.emit("engine", m, "DEBUG"), cancel=self.ctl._cancel.is_set,
                   events=self._on_engine, on_session_done=self._install if self.commit else None,
                   control_failure=self.control_failure, control=control)
        if self.commit:  # validated before the interruption but not installed yet
            left = [e["project"] for e in json.loads((self.bdir / "batch.json").read_text(encoding="utf-8"))["projects"]
                    if e["status"] == "VALIDATED"]
            if left:
                self._install(self.bdir, left)
        return self._finish(prev)

    def _earlier_attempts(self, package: str, current: str) -> dict[str, dict]:
        """Latest earlier attempt (REVIEW / FAILED) per project of ``package`` in older batches of this tool."""
        from ..conversion.batch import BatchEngine

        out: dict[str, dict] = {}
        for b in _batch_dirs(self.ctl):  # newest first
            if b.name == current:
                continue
            try:
                data = json.loads((b / "batch.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("package") != package:
                continue
            for e in data.get("projects", []):
                if e["project"] in out or e.get("status") not in ("REVIEW", "FAILED", "COMMITTED", "VALIDATED"):
                    continue
                if e["status"] in ("COMMITTED", "VALIDATED"):
                    out[e["project"]] = None  # a later success wins over older failures
                    continue
                res = BatchEngine.latest_result(b, e) or {}
                out[e["project"]] = dict(e, earlier_batch=b.name, result=res)
        return {k: v for k, v in out.items() if v}

    def _install(self, bdir: Path, names: list[str]) -> None:
        self.emit("stage", f"Installing {len(names)} validated conversion(s)…", "DETAILED", stage="commit")
        self.ctl.commit_batch(bdir, only=names, progress=lambda m: self.emit("engine", m, "DEBUG"))

    def _finish(self, prev) -> PipelineResult:
        r = self.result(prev, self.bdir)
        self.emit("finished", r.state_label, state=r.state, counts=r.counts, percent=r.percent,
                  problems=[{"name": i["name"], "message": i["message"]} for i in r.problems])
        return r

    # ------------------------------------------------------------------ result
    def result(self, prev, bdir: Optional[Path]) -> PipelineResult:
        batch = json.loads((bdir / "batch.json").read_text(encoding="utf-8")) if bdir else {}
        rows = {e["project"]: e for e in batch.get("projects", [])}
        results = {}
        if bdir:
            from ..conversion.batch import BatchEngine

            for e in batch.get("projects", []):
                results[e["project"]] = BatchEngine.latest_result(bdir, e) or {}
        r = PipelineResult(package=prev.package, mode=prev.conversion_mode or self.mode, batch_id=batch.get("id", ""),
                           batch_dir=str(bdir or ""), batch_state=batch.get("state", ""),
                           finished=time.strftime("%Y-%m-%dT%H:%M:%S"))
        out_name = Path(prev.output_mod).name if prev.output_mod else ""
        runtime = runtime_reports(prev.output_mod)
        earlier = self._earlier_attempts(prev.package, batch.get("id", ""))
        for p in prev.projects:
            e = rows.get(p.project)
            status = e["status"] if e else None
            if e is None and p.project in earlier and p.action in ("CONVERT", "REVIEW"):
                # 0.5.2: not in this batch, but an earlier batch tried it and it did not validate: keep that visible
                e, status = earlier[p.project], earlier[p.project]["status"]
                results[p.project] = earlier[p.project]["result"]
            outcome = outcome_for(p.action, p.status, status, attempted=bool(e), committing=self.commit)
            res = results.get(p.project, {})
            technical = list(dict.fromkeys(res.get("reasons", []) if e else p.reasons))
            if e and e.get("earlier_batch"):
                technical = [f"earlier attempt in batch {e['earlier_batch']} ({e['status']})"] + technical
            if e and e.get("commit_error"):
                technical = [f"commit: {x}" for x in e["commit_error"]] + technical
            if not e and p.review_policy == POLICY_BLOCKED and p.review_policy_reason \
                    and p.review_policy_reason not in technical:
                technical = [p.review_policy_reason] + technical
            code, msg = humanize(outcome, technical, p.status, attempted=bool(e), bucket=p.bucket)
            # 0.6.1: the strict check that rejected the result, as the engine named it (shown with the message)
            fc = failed_check(technical) if e and outcome in ("COULD_NOT_CONVERT_SAFELY", "FAILED") else ""
            if outcome == "COULD_NOT_CONVERT_SAFELY" and not e and p.review_policy == POLICY_ATTEMPTABLE \
                    and r.mode == MODE_SAFE:
                outcome = "SKIPPED"
                code, msg = humanize(outcome, technical, p.status, attempted=False, bucket=p.bucket)
            item = {"project": p.project, "name": p.display_name, "item_names": [g.get("armor_name") for g in p.game_items],
                    "mesh": p.output_mesh, "outcome": outcome, "outcome_label": OUTCOME_LABELS.get(outcome, outcome),
                    "code": code, "message": msg, "failed_check": fc, "left_unchanged": LEFT_UNCHANGED if outcome in PROBLEM_OUTCOMES + ("SKIPPED",) else "",
                    "technical": technical[:12], "action": p.action, "status": p.status, "batch_status": status or "",
                    "confidence": p.confidence, "regions": list(p.regions), "review_policy": p.review_policy,
                    "origin": (e or {}).get("origin", p.attempt_origin), "bucket": final_bucket(p.bucket, status, technical),
                    "generated_project": p.generated_project, "attempts": (e or {}).get("attempts", 0),
                    "cloth": bool((p.plan or {}).get("source_cloth_data")),
                    "shapes": len((p.plan or {}).get("shape_plans", [])),
                    # 0.6.0 body route
                    "source_body": getattr(p, "source_body", ""), "target_body": getattr(p, "target_body", ""),
                    "route_id": getattr(p, "route_id", ""), "route_label": getattr(p, "route_label", ""),
                    "route_status": getattr(p, "route_status", ""),
                    "geometry_stage": (getattr(p, "geometry_stage", {}) or {}).get("status", ""),
                    "physics_stage": (getattr(p, "physics_stage", {}) or {}).get("status", "")}
            rt = runtime.get(p.generated_project)
            item["runtime_issue"] = rt
            item["attention"] = outcome in PROBLEM_OUTCOMES + ("SKIPPED",) or bool(rt)
            item["short"] = short_reason(RUNTIME_STATES[rt["state"]]) if rt and outcome in ("CONVERTED", "ALREADY_COMPATIBLE") \
                else short_reason(code) if item["attention"] else ""
            if rt and outcome in ("CONVERTED", "ALREADY_COMPATIBLE"):
                item["code"] = RUNTIME_STATES[rt["state"]]
                item["message"] = ("Converted and validated, but you reported " + ("no physics" if rt["state"] == "FAIL"
                                   else "a physics problem") + " in game" + (f": {rt['note']}" if rt.get("note") else "") + ".")
                item["technical"] = [f"runtime verification {rt['state']} ({rt.get('timestamp', '')}): {rt.get('note', '')}",
                                     "automated validation result unchanged: " + (status or p.status or outcome)] + item["technical"]
            r.items.append(item)
        r.total = len(r.items)
        for i in r.items:
            r.counts[i["outcome"]] = r.counts.get(i["outcome"], 0) + 1
            r.buckets.setdefault(i["bucket"], []).append(i["project"])
        r.percent = applicable_percent(i["outcome"] for i in r.items if i["outcome"] != "SKIPPED")
        sf = (batch.get("system_failure") or {}).get("reason", "")
        r.system_failure = sf
        r.system_message = system_message(sf) if sf else ""
        r.state = package_result([i["outcome"] for i in r.items], batch.get("state", ""))
        r.state_label = RESULT_LABELS[r.state]
        for code in prev.codes:
            r.warnings.append({"code": code, "message": package_message(code, out_name)})
        conv = self.ctl.installation.profile.conversion if self.ctl.installation else None
        from ..conversion.package import package_group_name

        converted = [i for i in r.items if i["outcome"] == "CONVERTED" and i["batch_status"]]
        group = package_group_name(prev.package)
        preset = conv.preset if conv else "CBBE Curvy"
        morphs = conv.build_morphs if conv else True
        r.build = {"group": group, "main_group": "CBBE 3BBB Auto Conversions",
                   "preset": preset, "build_morphs": morphs,
                   "generated_projects": [i["generated_project"] for i in converted],
                   "output_mod_state": prev.output_mod_state,
                   # 0.5.3: the AUTO projects intentionally build the same game meshes as the originals; only the
                   # package-specific AUTO group is built, never the original projects after it
                   "order": (f"Build only the group '{group}'. Do not batch-build the original {prev.package} projects "
                             "after the AUTO group, because both target the same game mesh files."),
                   "warning": (f"Do not batch-build the original {prev.package} projects after the AUTO group, because "
                               "both target the same game mesh files."),
                   "instructions": [f"Build only: {group}",
                                    f"Preset: {preset}",
                                    f"Build Morphs: {'ON' if morphs else 'OFF'}",
                                    "Click Batch Build → Build",
                                    f"Do not batch-build the original {prev.package} projects after the AUTO group, "
                                    "because both target the same game mesh files."],
                   "cli": (f'BodySlide --groupbuild "{group}" --preset "{preset}"' + (" --trimorphs" if morphs else "")
                           + "  (5.8.2 option names; run it through Mod Organizer 2 so the files go where your BodySlide "
                             "builds normally go)")}
        r.representative = representative_items(r.items)
        return r


def representative_items(items: list[dict], limit: int = 8) -> list[dict]:
    """5–8 converted outfits that together cover the kinds of conversion in this package (Part K)."""
    conv = sorted((i for i in items if i["outcome"] == "CONVERTED" and i["batch_status"]), key=lambda i: i["project"].lower())
    picks: list[tuple[dict, str]] = []

    def take(pred, why):
        for i in conv:
            if pred(i) and all(i is not x for x, _ in picks):
                picks.append((i, why))
                return

    R = lambda i: set(i["regions"])  # noqa: E731
    take(lambda i: R(i) == {"BREAST"}, "breast physics only")
    take(lambda i: R(i) == {"BUTT"} or R(i) == {"BUTT", "THIGH"}, "butt physics (bottom)")
    take(lambda i: "THIGH" in R(i) and "BREAST" not in R(i), "thigh physics (legwear)")
    take(lambda i: R(i) >= {"BREAST", "BUTT", "THIGH"}, "full-length outfit (breast, butt and thigh)")
    take(lambda i: i["shapes"] > 1, "several converted shapes")
    take(lambda i: i["cloth"], "outfit with cloth physics data")
    take(lambda i: i["origin"] not in ("SAFE", ""), "converted by Convert All Possible (uncertain fit)")
    take(lambda i: R(i) >= {"BREAST", "BUTT"}, "top and bottom in one outfit")
    for i in conv:
        if len(picks) >= 5:
            break
        if all(i is not x for x, _ in picks):
            picks.append((i, "additional sample"))
    return [{"name": i["name"], "project": i["project"], "mesh": i["mesh"], "regions": i["regions"], "why": why}
            for i, why in picks[:limit]]


def _batch_dirs(ctl) -> list[Path]:
    try:
        return list(ctl.list_batches())
    except Exception:  # noqa: BLE001
        return []


def runtime_reports(output_mod: Optional[str]) -> dict[str, dict]:
    """Latest in-game verification per generated project whose state is a problem (FAIL / PARTIAL). Read-only; the
    automated validation result is never changed by it."""
    if not output_mod or not (Path(output_mod) / "manifest.json").is_file():
        return {}
    from ..conversion.manifest import Manifest

    try:
        man = Manifest.load(Path(output_mod))
    except (OSError, ValueError):
        return {}
    latest: dict[str, dict] = {}
    for rec in man.records:
        name = rec.get("target", {}).get("project", "")
        if name:
            latest[name] = rec
    out = {}
    for name, rec in latest.items():
        acc = rec.get("acceptance") or {}
        if acc.get("state") in RUNTIME_STATES:
            last = (acc.get("history") or [{}])[-1]
            out[name] = {"state": acc["state"], "note": last.get("note", ""), "timestamp": last.get("timestamp", "")}
    return out


def problem_lines(result: PipelineResult, limit: Optional[int] = None) -> list[str]:
    """R.1: '• Name' lines, '+ N more…' when there are more than ``limit``."""
    names = result.problem_names()
    shown = names if limit is None else names[:limit]
    L = [f"• {n}" for n in shown]
    if limit is not None and len(names) > limit:
        L.append(f"+ {len(names) - limit} more…")
    return L


def result_text(r: PipelineResult, level: str = "NORMAL") -> str:
    """The final summary as plain text (CLI and 'copy to clipboard')."""
    L = [r.package, r.state_label, "",
         f"{'Completed:':<30}{len(r.completed):>5}",
         f"{'No conversion needed:':<30}{r.counts.get('NO_CONVERSION_NEEDED', 0):>5}",
         f"{'Needs attention:':<30}{len(r.needs_attention):>5}"]
    if r.needs_attention:
        L += ["", "Needs attention / original kept:"] + r.attention_lines()
    L += ["", "Automated result by outcome:"]
    for k in ("CONVERTED", "ALREADY_COMPATIBLE", "NO_CONVERSION_NEEDED", "COULD_NOT_CONVERT_SAFELY", "FAILED", "SKIPPED",
              "UNFINISHED"):
        if r.counts.get(k) or k in ("CONVERTED", "COULD_NOT_CONVERT_SAFELY", "FAILED"):
            L.append(f"{OUTCOME_LABELS[k] + ':':<30}{r.counts.get(k, 0):>5}")
    if r.percent is not None:
        L += ["", f"{r.percent}% of applicable outfits converted."]
    if r.system_message:
        L += ["", r.system_message]
    for w in r.warnings:
        L += ["", w["message"]]
    probs = r.problems
    if probs:
        L += ["", f"Could not convert {len(probs)} outfit(s):"]
        for i in probs:
            L.append(f"• {i['name']}")
            if level in ("DETAILED", "DEBUG"):
                L.append(f"    {i['message']} {LEFT_UNCHANGED}")
            if level == "DEBUG":
                L += [f"      - {t}" for t in i["technical"][:6]]
        L.append("Original versions of these outfits were left unchanged.")
    if r.build.get("generated_projects"):
        L += ["", "Build converted outfits in BodySlide:"] + [f"  {x}" for x in r.build["instructions"]]
    return "\n".join(L) + "\n"
