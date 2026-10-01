"""Conversion execution: stage → Outfit Studio headless run → validate → commit.

Isolation rules:

* The source files are only read. They are copied into a per-run workspace
  (``<tool>/work/conversions/<id>``) and hashed before and after the run.
* Outfit Studio runs from the tool's private runtime copy, with ``ProjectPath``
  pointing at the workspace, so it can only read and write there.
* Files reach the output mod only after every required validation check
  passed. A failed run leaves the workspace and its logs for inspection and
  writes nothing to the output mod.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

from ..analysis.analyzer import LibraryAnalyzer
from ..analysis.reference import KnowledgeBase
from ..analysis.shape_metrics import MetricSettings
from ..discovery.install import BODYSLIDE_SUBTREE, Installation
from ..discovery.vfs import DataProvider, VirtualFS
from .automation import check_invariants
from .evidence import ExecutionEvidence, collect_evidence, planned_outputs, snapshot
from .manifest import Manifest
from .plan import ConversionPlan, sha256_file
from .runtime import RuntimeInfo, detect_runtime, prepare_private_runtime, write_runtime_config
from .validate import ValidationReport, validate_generated

__all__ = ["ConversionResult", "ConversionEngine", "run_outfit_studio", "RunOutcome"]

log = logging.getLogger(__name__)
ProgressFn = Callable[[str], None]


@dataclass
class RunOutcome:
    command: list[str]
    exit_code: Optional[int]
    seconds: float
    timed_out: bool = False
    cancelled: bool = False
    crashed: str = ""  # fatal/unhandled exception reported in Log_OS.txt (a message box would block)
    interactive: bool = False
    log_text: str = ""
    stdout: str = ""
    stderr: str = ""


@dataclass
class ConversionResult:
    id: str
    status: str = "FAILED"  # SUCCESS | FAILED | VALIDATED (revalidation without commit)
    reasons: list[str] = field(default_factory=list)
    workspace: str = ""
    output_mod: str = ""
    files_written: list[str] = field(default_factory=list)
    validation: dict = field(default_factory=dict)
    run: dict = field(default_factory=dict)
    source_hashes_before: dict = field(default_factory=dict)
    source_hashes_after: dict = field(default_factory=dict)
    generated_classification: dict = field(default_factory=dict)
    runtime: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "SUCCESS"

    def to_dict(self) -> dict:
        return asdict(self)


def _needs_xvfb(exe: Path) -> bool:
    return sys.platform != "win32" and not exe.name.lower().endswith(".exe") and not os.environ.get("DISPLAY")


_FATAL_MARKERS = ("Fatal exception has occurred", "Unhandled exception has occurred", "Unexpected exception has occurred")


def _fatal_in_log(log_file: Path) -> str:
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for m in _FATAL_MARKERS:
        if m in text:
            return m
    return ""


def _kill(proc: subprocess.Popen, posix: bool) -> None:
    try:
        if posix:
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def run_outfit_studio(runtime_folder: Path, exe: Path, script_name: str, timeout_s: int,
                      cancel: Optional[Callable[[], bool]] = None, interactive: bool = False) -> RunOutcome:
    """Run Outfit Studio in its own (private) folder.

    headless: ``OutfitStudio -a <script_name>``. interactive: Outfit Studio opens normally and the
    user runs the prepared script with File > Open Automation > Execute, then closes Outfit Studio.
    """
    cmd = [str(exe)] if interactive else [str(exe), "-a", script_name]
    if _needs_xvfb(exe) and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24"] + cmd
    log_file = Path(runtime_folder) / "Log_OS.txt"
    if log_file.exists():
        try:
            log_file.unlink()
        except OSError:
            pass
    t0 = time.time()
    creation = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    posix = sys.platform != "win32"
    proc = subprocess.Popen(cmd, cwd=str(runtime_folder), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=creation, start_new_session=posix)
    out = RunOutcome(command=cmd, exit_code=None, seconds=0.0)
    while True:
        try:
            so, se = proc.communicate(timeout=1.0)
            out.stdout, out.stderr = so.decode(errors="replace"), se.decode(errors="replace")
            break
        except subprocess.TimeoutExpired:
            fatal = _fatal_in_log(log_file)
            if cancel and cancel():
                out.cancelled = True
            elif fatal:
                out.crashed = fatal
            elif time.time() - t0 > timeout_s:
                out.timed_out = True
            else:
                continue
            _kill(proc, posix)
            so, se = proc.communicate()
            out.stdout, out.stderr = so.decode(errors="replace"), se.decode(errors="replace")
            break
    out.exit_code = proc.returncode
    out.seconds = round(time.time() - t0, 2)
    out.interactive = interactive
    if log_file.is_file():
        out.log_text = log_file.read_text(encoding="utf-8", errors="replace")
    return out


def _group_members(path: Path) -> set[str]:
    import xml.etree.ElementTree as ET

    if not path.is_file():
        return set()
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return set()
    return {m.get("name", "") for m in root.iter("Member") if m.get("name")}


def _group_xml(members: list[str], name: str = "CBBE 3BBB Auto Conversions") -> str:
    from xml.sax.saxutils import quoteattr

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<SliderGroups>", f"    <Group name={quoteattr(name)}>"]
    lines += [f"        <Member name={quoteattr(m)}/>" for m in members]
    lines += ["    </Group>", "</SliderGroups>", ""]
    return "\n".join(lines)


class ConversionEngine:
    def __init__(self, tool_root: Path, inst: Installation, kb: KnowledgeBase,
                 runner: Optional[Callable[..., RunOutcome]] = None):
        self.tool_root = Path(tool_root)
        self.runner = runner or run_outfit_studio  # injectable for tests (a fake Outfit Studio)
        self.inst = inst
        self.kb = kb
        self.work_root = self.tool_root / "work" / "conversions"
        self.runtime_root = self.tool_root / "runtime"
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _protected(self) -> list[Path]:
        return [p for p in (self.inst.game_dir, self.inst.mods_dir, self.inst.mo2.root if self.inst.mo2 else None) if p]

    def resolve_runtime(self, prepare: bool) -> RuntimeInfo:
        src = self.inst.profile.conversion.outfitstudio_dir
        info = detect_runtime(src) if src else RuntimeInfo(reason="no Outfit Studio 5.8+ folder configured")
        for p in self._protected():
            try:
                if src and Path(src).resolve().is_relative_to(Path(p).resolve()) and not info.private_copy:
                    info.warnings.append(f"the selected Outfit Studio folder is inside {p}; it is only read and copied")
            except (OSError, ValueError):
                pass
        if prepare and info.headless_supported and not info.private_copy:
            info = prepare_private_runtime(src, self.runtime_root, self._protected())
        return info

    # ------------------------------------------------------------------
    def convert(self, plan: ConversionPlan, progress: Optional[ProgressFn] = None,
                cancel: Optional[Callable[[], bool]] = None) -> ConversionResult:
        say = progress or (lambda m: None)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        rid = f"{stamp}_{plan.script_name}"
        res = ConversionResult(id=rid, output_mod=plan.output_mod_path)
        if not plan.can_convert:
            res.reasons = ["plan is blocked: " + "; ".join(plan.blockers)]
            return res
        if not self._lock.acquire(blocking=False):
            res.reasons = ["another conversion is running"]
            return res
        ws = self.work_root / rid
        try:
            return self._convert(plan, res, ws, say, cancel)
        except Exception as exc:  # noqa: BLE001 - always report, never leave partial output
            log.exception("conversion crashed")
            res.status = "FAILED"
            res.reasons.append(f"conversion crashed: {exc}")
            self._write_result(ws, res)
            return res
        finally:
            self._lock.release()

    def _convert(self, plan: ConversionPlan, res: ConversionResult, ws: Path, say: ProgressFn,
                 cancel: Optional[Callable[[], bool]]) -> ConversionResult:
        check_invariants(plan.script_xml)
        project = ws / "Data" / BODYSLIDE_SUBTREE
        (ws / "logs").mkdir(parents=True, exist_ok=True)
        res.workspace = str(ws)
        (ws / "plan.json").write_text(json.dumps(plan.to_dict(), indent=1), encoding="utf-8")

        # 1. hash and stage the source files ------------------------------------------------
        say("Hashing and staging source files…")
        for sf in plan.staged_files:
            p = Path(sf.abs_path)
            st = p.stat()
            if int(st.st_mtime) != int(sf.mtime) or st.st_size != sf.size:
                res.reasons.append(f"{sf.rel} changed since the plan was made; re-run the dry run")
                return self._fail(ws, res)
            res.source_hashes_before[sf.abs_path] = sha256_file(p)
            dst = project / sf.rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
        for p in (project / "SliderSets", project / "ShapeData"):
            p.mkdir(parents=True, exist_ok=True)
        auto_dir = project / "Automations"
        auto_dir.mkdir(parents=True, exist_ok=True)
        (auto_dir / f"{plan.script_name}.xml").write_text(plan.script_xml, encoding="utf-8")

        # 2. private runtime --------------------------------------------------------------------
        say("Preparing the private Outfit Studio runtime…")
        rt = self.resolve_runtime(prepare=True)
        res.runtime = rt.to_dict()
        if not rt.headless_supported:
            res.reasons.append(f"Outfit Studio not usable: {rt.reason}")
            return self._fail(ws, res)
        write_runtime_config(Path(rt.folder), project, ws / "Data")
        if cancel and cancel():
            res.reasons.append("cancelled before Outfit Studio was started")
            return self._fail(ws, res)

        # 3. run Outfit Studio headless ----------------------------------------------------------
        conv = self.inst.profile.conversion
        interactive = plan.os_run_mode == "interactive"
        if interactive:
            say(f"Outfit Studio {rt.version} is opening. In Outfit Studio: File > Open Automation… (Ctrl+Shift+A), "
                f"choose '{plan.script_name}', press Execute, confirm the message, close the Automation window and "
                "then close Outfit Studio (answer 'No' if asked to save; the script already saved the new project)."
                + (f" When 'Choose cloth data' appears, tick only '{Path(plan.source_shapedata_rel).name}' and press OK."
                   if plan.source_cloth_data else ""))
        else:
            say(f"Running Outfit Studio {rt.version} headless automation…")
        before = snapshot(planned_outputs(plan, project))
        t_start = time.time()
        run = self.runner(Path(rt.folder), Path(rt.path), plan.script_name,
                          conv.interactive_timeout_s if interactive else conv.automation_timeout_s, cancel,
                          interactive=interactive)
        t_end = time.time()
        (ws / "logs" / "Log_OS.txt").write_text(run.log_text, encoding="utf-8")
        (ws / "logs" / "stdout.txt").write_text(run.stdout + "\n--- stderr ---\n" + run.stderr, encoding="utf-8")
        res.run = {"command": run.command, "mode": "interactive" if interactive else "headless",
                   "exit_code": run.exit_code, "seconds": run.seconds, "started": t_start, "ended": t_end,
                   "timed_out": run.timed_out, "cancelled": run.cancelled, "crashed": run.crashed,
                   "automation_errors": [ln for ln in run.log_text.splitlines() if "Automation:" in ln and ("fail" in ln.lower() or "error" in ln.lower())]}
        if run.cancelled:
            res.reasons.append("cancelled while Outfit Studio was running")
            return self._fail(ws, res)
        if run.crashed:
            res.reasons.append(f"Outfit Studio crashed ({run.crashed}); see logs/Log_OS.txt")
            return self._fail(ws, res)
        if run.timed_out:
            res.reasons.append(f"Outfit Studio did not finish within {self.inst.profile.conversion.automation_timeout_s}s (a dialog may have blocked it); see logs/Log_OS.txt")
            return self._fail(ws, res)
        # Did the prepared script actually run? In interactive mode Outfit Studio 5.8.x does not write the
        # automation step lines to Log_OS.txt (they go to the Automation window only), so the proof is the
        # artifacts the script's SaveProject step creates, checked against this session (see evidence.py).
        ev = collect_evidence(plan, project, run.log_text, interactive=interactive, before=before,
                              window=(t_start, t_end), window_source="tool clock around the Outfit Studio process")
        res.run["steps_logged"] = sum(1 for ln in run.log_text.splitlines() if "Automation: Step" in ln)
        res.run["execution_evidence"] = ev.to_dict()
        if ev.outputs_missing:
            res.reasons.append("Outfit Studio was closed without executing the conversion script "
                               "(the planned project was not saved during the session)" if interactive else
                               "the automation did not save the planned project")
        if not interactive and run.exit_code != 0:
            res.reasons.append(f"Outfit Studio automation exit code {run.exit_code} (0 = success; 10 = a step failed)")
        if res.run["automation_errors"]:
            res.reasons.append("Outfit Studio reported step errors: " + " | ".join(res.run["automation_errors"][:3]))

        # 4. validate / 5. source unchanged --------------------------------------------------------
        val = self._validate(plan, project, ws, res, ev, "validation.txt", say)
        if not val.passed:
            res.reasons += [f"validation failed: {c.name} — {c.detail}" for c in val.failures()]
        if res.reasons:
            return self._fail(ws, res)

        # 6. commit into the output mod -------------------------------------------------------------
        say("Writing the generated project into the output mod…")
        try:
            res.files_written = self._commit(plan, project, res, rt, val)
        except Exception as exc:  # noqa: BLE001
            res.reasons.append(f"could not write the output mod: {exc}")
            return self._fail(ws, res)
        res.status = "SUCCESS"
        self._write_result(ws, res)
        say("Conversion finished: SUCCESS")
        return res

    # ------------------------------------------------------------------
    def _validate(self, plan: ConversionPlan, project: Path, ws: Path, res: ConversionResult,
                  ev: ExecutionEvidence, report_name: str, progress: Optional[ProgressFn] = None,
                  data_root: Optional[Path] = None) -> ValidationReport:
        (progress or (lambda m: None))("Validating the generated project…")
        s = self.inst.profile
        ms = MetricSettings(search_radius=s.conversion.search_radius, max_vertex_targets=s.conversion.max_vertex_targets,
                            no_target_limit=s.conversion.no_target_limit, region_weight_threshold=s.analysis.region_weight_threshold)
        val = validate_generated(plan, project, project, self.kb, ms)
        # 0.6.0: route-aware validation. The CBBE → CBBE 3BBB strict validation above stays authoritative; a route
        # whose own validator does not exist yet can never pass (its stages are declared in routes.py).
        vid = (plan.route or {}).get("validator") or "cbbe_to_cbbe_3bbb_strict"
        val.metrics["validator"] = vid
        if vid != "cbbe_to_cbbe_3bbb_strict":
            val.add("validation stages of this body route implemented", False,
                    f"validator '{vid}' is not implemented; only cbbe_to_cbbe_3bbb_strict exists")
        for name, ok, detail in ev.checks:  # execution evidence: additional required checks
            val.add(name, ok, detail)
        cls = self.classify_generated(plan, data_root or ws / "Data")
        res.generated_classification = cls
        val.add("analyzer classifies generated project as 3BBB KEEP",
                cls.get("action") == "KEEP" and cls.get("physics") == "3bbb",
                f"{cls.get('action')} / {cls.get('label')}: {'; '.join(cls.get('reasons', [])[:2])}")
        for sf in plan.staged_files:
            p = Path(sf.abs_path)
            res.source_hashes_after[sf.abs_path] = sha256_file(p) if p.is_file() else "MISSING"
        changed = [p for p, h in res.source_hashes_before.items() if res.source_hashes_after.get(p) != h]
        val.add("source files unchanged (SHA-256)", bool(res.source_hashes_before) and not changed,
                f"{len(res.source_hashes_before)} files verified" if not changed else f"CHANGED: {changed}")
        res.validation = val.to_dict()
        res.validation["notes"] = list(ev.notes)
        (ws / "logs" / report_name).write_text("\n".join(val.lines() + [f"note: {n}" for n in ev.notes]), encoding="utf-8")
        return val

    # ------------------------------------------------------------------
    def revalidate(self, workspace: Path, commit: bool = False,
                   progress: Optional[ProgressFn] = None) -> ConversionResult:
        """Re-check an existing conversion workspace without running Outfit Studio again.

        This is for runs whose files are fine but whose result was wrong (for example, the
        false FAILED of interactive Outfit Studio 5.8.2 runs in tool version 0.2.0). Every
        validation check runs again, plus the execution evidence; the session window comes
        from the workspace's ``Log_OS.txt``. It writes ``result.revalidated.json`` and
        ``logs/validation.revalidated.txt`` and never changes the original result, log or
        generated files. With ``commit=True``, and only if everything passed, the files are
        written to the output mod exactly as a successful conversion would write them.
        """
        say = progress or (lambda m: None)
        ws = Path(workspace)
        res = ConversionResult(id=ws.name, workspace=str(ws))
        if not self._lock.acquire(blocking=False):
            res.reasons = ["another conversion is running"]
            return res
        try:
            return self._revalidate(ws, res, commit, say)
        except Exception as exc:  # noqa: BLE001
            log.exception("revalidation crashed")
            res.status = "FAILED"
            res.reasons.append(f"revalidation crashed: {exc}")
            self._write_result(ws, res, "result.revalidated.json")
            return res
        finally:
            self._lock.release()

    def _revalidate(self, ws: Path, res: ConversionResult, commit: bool, say: ProgressFn) -> ConversionResult:
        name = "result.revalidated.json"
        try:
            inside = ws.resolve().is_relative_to(self.work_root.resolve())
        except (OSError, ValueError):
            inside = False
        if not inside or not (ws / "plan.json").is_file() or not (ws / "result.json").is_file():
            res.reasons.append(f"not a conversion workspace of this tool (expected plan.json and result.json under {self.work_root})")
            return res  # nothing is written outside the tool's own workspaces
        say("Loading the workspace…")
        plan = ConversionPlan.from_dict(json.loads((ws / "plan.json").read_text(encoding="utf-8")))
        orig = json.loads((ws / "result.json").read_text(encoding="utf-8"))
        res.output_mod = plan.output_mod_path
        res.runtime = orig.get("runtime", {})
        res.source_hashes_before = dict(orig.get("source_hashes_before", {}))
        run = dict(orig.get("run", {}))
        res.run = dict(run, revalidated=True, revalidated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                       original_status=orig.get("status"), original_reasons=orig.get("reasons", []))
        project = ws / "Data" / BODYSLIDE_SUBTREE
        interactive = run.get("mode") == "interactive"
        if orig.get("status") == "SUCCESS" or orig.get("files_written"):
            res.reasons.append("this workspace was already committed to the output mod")
        if not res.source_hashes_before:
            res.reasons.append("result.json has no source hashes (Outfit Studio never ran for this workspace)")
        if not run or run.get("cancelled") or run.get("crashed") or run.get("timed_out"):
            res.reasons.append("the original Outfit Studio run did not finish normally (cancelled, crashed or timed out)")
        if not interactive and run.get("exit_code") != 0:
            res.reasons.append(f"Outfit Studio automation exit code {run.get('exit_code')}")
        if res.reasons:
            return self._fail(ws, res, name)
        log_file = ws / "logs" / "Log_OS.txt"
        log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
        window = (run.get("started"), run.get("ended"))
        src = "tool clock recorded in result.json" if None not in window else ""
        ev = collect_evidence(plan, project, log_text, interactive=interactive, before=None, window=window,
                              window_source=src, not_before=(ws / "plan.json").stat().st_mtime)
        if ev.window_source == "Log_OS.txt timestamps":
            a, b = ev.window
            dur = (b - a) if a is not None and b is not None else None
            secs = run.get("seconds")
            ok = dur is not None and isinstance(secs, (int, float)) and abs(dur - secs) <= 5.0
            ev.add("execution: Log_OS.txt session length matches the recorded Outfit Studio run", ok,
                   f"log {dur if dur is None else round(dur)} s, run {secs} s")
        res.run["execution_evidence"] = ev.to_dict()
        val = self._validate(plan, project, ws, res, ev, "validation.revalidated.txt", say)
        if not val.passed:
            res.reasons += [f"validation failed: {c.name} — {c.detail}" for c in val.failures()]
        if res.reasons:
            return self._fail(ws, res, name)
        res.run["generated_sha256"] = {k: sha256_file(p) for k, p in planned_outputs(plan, project).items()}
        if not commit:
            res.status = "VALIDATED"
            self._write_result(ws, res, name)
            say("Revalidation passed. Nothing was written to the output mod (use --commit).")
            return res
        # the same gates as a fresh conversion
        allowed = self.inst.profile.conversion.allowed_projects
        if plan.source_project not in allowed:
            res.reasons.append(f"'{plan.source_project}' is not enabled for conversion in this milestone")
        if not plan.output_mod_path:
            res.reasons.append("no output mod in the plan")
        else:
            from .plan import check_output_mod

            res.reasons += check_output_mod(Path(plan.output_mod_path), self.inst, plan.files_to_write[:2])
        if res.reasons:
            return self._fail(ws, res, name)
        say("Writing the generated project into the output mod…")
        rt_fields = {f for f in RuntimeInfo.__dataclass_fields__}
        rt = RuntimeInfo(**{k: v for k, v in res.runtime.items() if k in rt_fields})
        try:
            res.files_written = self._commit(plan, project, res, rt, val)
        except Exception as exc:  # noqa: BLE001
            res.reasons.append(f"could not write the output mod: {exc}")
            return self._fail(ws, res, name)
        res.status = "SUCCESS"
        self._write_result(ws, res, name)
        say("Revalidation passed and the project was committed: SUCCESS")
        return res

    # ------------------------------------------------------------------
    def classify_generated(self, plan: ConversionPlan, data_root: Path) -> dict:
        prov = DataProvider("<generated>", Path(data_root), 10**9, "generated")
        vfs = VirtualFS.build([prov] + [p for p in self.inst.providers], BODYSLIDE_SUBTREE)
        inst2 = replace(self.inst, vfs=vfs, providers=[prov] + list(self.inst.providers))
        target = plan.output_osp_rel.lower()
        an = LibraryAnalyzer(inst2, workers=1, project_filter=lambda mod, rel: mod == "<generated>" and rel.lower() == target)
        rep = an.run()
        pr = next((p for p in rep.projects if p.project == plan.output_project), None)
        if pr is None:
            return {"action": "MISSING", "label": "", "reasons": ["generated project not found by the analyzer"]}
        return {"action": pr.action, "label": pr.family.label, "physics": pr.family.physics, "reasons": pr.reasons,
                "warnings": pr.warnings, "existing_3bbb": pr.existing_3bbb, "includes_body_shape": pr.includes_body_shape}

    def _commit(self, plan: ConversionPlan, project: Path, res: ConversionResult, rt: RuntimeInfo,
                val: ValidationReport, package_group: Optional[str] = None,
                record_extra: Optional[dict] = None) -> list[str]:
        """Copy one validated project into the output mod (all or nothing for this project).

        Writes the project file, its ShapeData folder, the BodySlide group(s) and a manifest
        record. On any error every file written for this project is removed and the group
        files and manifest are restored, so a failed commit leaves the output mod as it was.
        """
        out_mod = Path(plan.output_mod_path)
        pairs = [
            (project / plan.output_osp_rel, out_mod / BODYSLIDE_SUBTREE / plan.output_osp_rel),
        ]
        sd = project / "ShapeData" / plan.output_shapedata_folder
        for f in sorted(sd.iterdir()):
            pairs.append((f, out_mod / BODYSLIDE_SUBTREE / "ShapeData" / plan.output_shapedata_folder / f.name))
        for _, dst in pairs:
            if dst.exists():
                raise FileExistsError(f"{dst} already exists")
        groups_dir = out_mod / BODYSLIDE_SUBTREE / "SliderGroups"
        # a BodySlide group with all generated projects, so they can be filtered / group-built
        group_file = groups_dir / "CBBE 3BBB Auto Conversions.xml"
        man = Manifest.load(out_mod)
        group_specs = [(group_file, "CBBE 3BBB Auto Conversions", _group_members(group_file) | {plan.output_project})]
        if package_group:
            members = {r.get("target", {}).get("project", "") for r in man.records
                       if r.get("source", {}).get("mod") == plan.source_mod and r.get("status") == "SUCCESS"}
            pfile = groups_dir / f"{package_group}.xml"
            group_specs.append((pfile, package_group, {m for m in members if m} | _group_members(pfile) | {plan.output_project}))
        backups = {g: (g.read_bytes() if g.exists() else None) for g, _, _ in group_specs}
        man_file = out_mod / "manifest.json"
        man_backup = man_file.read_bytes() if man_file.exists() else None
        written: list[Path] = []
        try:
            for src, dst in pairs:
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".cbbe3bbb-tmp")
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
                written.append(dst)
            for gfile, gname, members in group_specs:
                tmp = gfile.with_name(gfile.name + ".cbbe3bbb-tmp")
                gfile.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(_group_xml(sorted(members), gname), encoding="utf-8")
                existed = gfile.exists()
                os.replace(tmp, gfile)
                if not existed:
                    written.append(gfile)
            rec = man.add_record(plan, res, rt, [(d, sha256_file(d)) for d in written], val)
            rec["outfit_studio"]["run_mode"] = res.run.get("mode", "")
            rec["slider_data_dependencies"] = val.metrics.get("slider_data_files", [])
            rec["slider_group"] = "CBBE 3BBB Auto Conversions"
            if package_group:
                rec["package_group"] = package_group
            if res.run.get("revalidated"):
                rec["accepted_by"] = "revalidate (existing workspace, Outfit Studio not rerun)"
                rec["workspace"] = res.workspace
            rec["execution_evidence"] = res.run.get("execution_evidence", {})
            rec.update(record_extra or {})
            man.save()
        except Exception:
            for d in written:
                try:
                    d.unlink()
                except OSError:
                    pass
            for g, data in backups.items():
                try:
                    if data is not None:
                        g.write_bytes(data)
                except OSError:
                    pass
            try:
                if man_backup is not None:
                    man_file.write_bytes(man_backup)
            except OSError:
                pass
            raise
        return [str(d.relative_to(out_mod)).replace("\\", "/") for d in written] + ["manifest.json"]

    def _fail(self, ws: Path, res: ConversionResult, name: str = "result.json") -> ConversionResult:
        res.status = "FAILED"
        self._write_result(ws, res, name)
        return res

    @staticmethod
    def _write_result(ws: Path, res: ConversionResult, name: str = "result.json") -> None:
        try:
            ws.mkdir(parents=True, exist_ok=True)
            (ws / name).write_text(json.dumps(res.to_dict(), indent=1), encoding="utf-8")
        except OSError:
            pass
