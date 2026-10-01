"""UI-independent application logic shared by the GUI and the CLI.

Everything the GUI does goes through :class:`AppController`, so it can be tested
without Qt. The controller only writes inside the application's own folder
(config/, reports/, logs/, cache/) and refuses to do so if that folder sits
inside the game or MO2 directories.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..analysis.analyzer import LibraryAnalyzer, ScanReport
from ..analysis.cache import AnalysisCache
from ..analysis.models import ProjectResult
from ..discovery.install import Installation, detect_installation
from ..profiles.store import Profile, ProfileStore, app_root
from ..reports.writer import FILTERS, matches_filter, write_reports

if TYPE_CHECKING:  # pragma: no cover
    from ..conversion.engine import ConversionResult
    from ..conversion.plan import ConversionPlan
    from ..conversion.runtime import RuntimeInfo

__all__ = ["AppPaths", "AppController", "UnsafeOutputLocation", "project_details_text", "filter_projects"]

log = logging.getLogger(__name__)


class UnsafeOutputLocation(RuntimeError):
    """The tool's own output folder is inside a game/mod folder."""


@dataclass
class AppPaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @classmethod
    def default(cls) -> "AppPaths":
        return cls(app_root())


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def filter_projects(projects: list[ProjectResult], filter_name: str = "All", search: str = "") -> list[ProjectResult]:
    s = search.strip().lower()
    out = []
    for pr in projects:
        if not matches_filter(pr, filter_name):
            continue
        if s and s not in f"{pr.mod} {pr.project} {pr.family.label} {pr.action} {' '.join(pr.reasons)}".lower():
            continue
        out.append(pr)
    return out


def project_details_text(pr: ProjectResult) -> str:
    """Plain-text details for the GUI details pane."""
    lines = [f"{pr.project}", f"Mod: {pr.mod}   ({pr.provider_kind})", f"Action: {pr.action}   Confidence: {pr.geometry_confidence}", ""]
    lines.append("Reasons:")
    lines += [f"  • {r}" for r in pr.reasons] or ["  —"]
    if pr.warnings:
        lines.append("Warnings:")
        lines += [f"  • {w}" for w in pr.warnings]
    if pr.errors:
        lines.append("Errors:")
        lines += [f"  • {e}" for e in pr.errors]
    lines.append("")
    lines.append(f"Body family: {pr.family.label} (physics: {pr.family.physics}, confidence {pr.family.confidence})")
    for e in pr.family.evidence[:5]:
        lines.append(f"  evidence: {e}")
    lines.append(f"Regions: breast {pr.breast} · butt {pr.butt} · thigh {pr.thigh} · existing 3BBB {pr.existing_3bbb}")
    lines.append(f"Project file: {pr.osp_rel} (set #{pr.set_index})")
    lines.append(f"  full path: {pr.osp_path}")
    if pr.osp_also_provided_by:
        lines.append(f"  also provided by (overridden): {', '.join(pr.osp_also_provided_by)}")
    lines.append(f"ShapeData: {pr.shapedata_rel or '—'}  [{pr.shapedata_provider or '—'}]")
    if pr.shapedata_also_provided_by:
        lines.append(f"  also provided by (overridden): {', '.join(pr.shapedata_also_provided_by)}")
    out = "\\".join(x for x in (pr.output_path, pr.output_file) if x)
    lines.append(f"Output mesh: {out or '—'}")
    lines.append(f"Sliders: {pr.slider_count}   Zaps: {pr.zap_count}")
    if pr.superseded_by:
        lines.append(f"Superseded by: {pr.superseded_by}")
    if pr.same_output_projects:
        lines.append("Same output as: " + "; ".join(pr.same_output_projects[:8]))
    lines.append("")
    lines.append("Shapes:")
    for s in pr.shapes:
        tag = "  [body]" if s.is_body else ""
        lines.append(
            f"  {s.name}{tag}: {s.vertices} verts, {s.bone_count} bones, dist median {s.distance.median:.2f} / p90 {s.distance.p90:.2f}"
        )
        if s.physics_bones:
            lines.append("    3BBB bones: " + ", ".join(f"{b}({n})" for b, n in sorted(s.physics_bones.items())))
        if s.legacy_physics_bones:
            lines.append("    legacy physics bones: " + ", ".join(sorted(s.legacy_physics_bones)))
        if s.custom_bones:
            lines.append("    custom bones: " + ", ".join(s.custom_bones[:8]))
        for r, m in s.regions.items():
            if m.relevance == "NONE" and not m.existing_vertices:
                continue
            t = (
                f"    {r.lower():6s} relevance {m.relevance:6s} fit {m.fit:6s} predicted {m.predicted_vertices:5d} v "
                f"coverage {m.reference_coverage:4.0%} footprint median {m.footprint_median_distance:.2f}"
            )
            if m.existing_vertices:
                t += f" | existing {m.existing_vertices} v"
                if m.agreement is not None:
                    t += f", agreement {m.agreement:.2f}"
                if m.weight_islands is not None:
                    t += f", islands {m.weight_islands}/{m.predicted_weight_islands}"
            lines.append(t)
        for w in s.warnings[:3]:
            lines.append(f"    ! {w}")
    return "\n".join(lines)


class AppController:
    def __init__(self, paths: Optional[AppPaths] = None, store: Optional[ProfileStore] = None):
        self.paths = paths or AppPaths.default()
        self.store = store or ProfileStore(self.paths.config)
        self.installation: Optional[Installation] = None
        self.report: Optional[ScanReport] = None
        self.report_paths: dict[str, Path] = {}
        self._cancel = threading.Event()
        self.preview = None  # Phase 3 PackagePreview
        self.direct_report = None  # 0.4.0 DirectNifReport (read-only analysis)
        self.preview_paths: dict[str, Path] = {}
        self.direct_plan = None  # 0.4.1 DirectBatchPlan
        self.direct_plan_paths: dict[str, Path] = {}

    # -- profiles ----------------------------------------------------------
    def current_profile(self) -> Profile:
        name = self.store.last_profile
        p = self.store.get(name) if name else None
        if p is None and self.store.profiles:
            p = next(iter(self.store.profiles.values()))
        return p or Profile(name="Default")

    def save_profile(self, profile: Profile) -> None:
        self.store.upsert(profile)
        self.store.last_profile = profile.name
        self.store.save()

    def validate_profile(self, profile: Profile) -> list[str]:
        problems = []
        if not profile.name.strip():
            problems.append("profile name is empty")
        if not profile.game_dir and not profile.mo2_dir:
            problems.append("set at least the Fallout 4 directory or the MO2 directory")
        for label, val in (("Fallout 4 directory", profile.game_dir), ("MO2 directory", profile.mo2_dir),
                           ("mods directory", profile.mods_dir), ("BodySlide directory", profile.bodyslide_dir)):
            if val and not Path(val).is_dir():
                problems.append(f"{label} does not exist: {val}")
        return problems

    # -- safety ------------------------------------------------------------
    def check_output_location(self, inst: Optional[Installation]) -> None:
        """The tool writes only below its own root; that root must not be a game or mod folder."""
        if inst is None:
            return
        protected = [p for p in (inst.game_dir, inst.mo2.root if inst.mo2 else None, inst.mods_dir) if p]
        for p in protected:
            if _is_within(self.paths.root, Path(p)):
                raise UnsafeOutputLocation(
                    f"the tool folder {self.paths.root} is inside {p}; move the tool outside the game/MO2 folders"
                )

    # -- actions -----------------------------------------------------------
    def detect(self, profile: Profile) -> Installation:
        self._cancel.clear()
        inst = detect_installation(profile, cancel=self._cancel.is_set)
        self.check_output_location(inst)
        self.installation = inst
        log.info("detect: %s", {k: v for k, v in inst.summary().items() if k not in ("ref_templates",)})
        return inst

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def analyze(
        self,
        profile: Optional[Profile] = None,
        progress: Optional[Callable[[int, int, str], None]] = None,
        write: bool = True,
        workers: Optional[int] = None,
        project_filter: Optional[Callable[[str, str], bool]] = None,
    ) -> ScanReport:
        self._cancel.clear()
        inst = self.installation
        if profile is not None and (inst is None or inst.profile is not profile):
            inst = detect_installation(profile, cancel=self._cancel.is_set)
            self.installation = inst
        if inst is None:
            raise RuntimeError("no installation detected")
        self.check_output_location(inst)
        cache = AnalysisCache(self.paths.cache / "analysis.sqlite") if inst.profile.analysis.use_cache else None
        try:
            analyzer = LibraryAnalyzer(inst, cache=cache, progress=progress, cancel=self._cancel.is_set,
                                       workers=workers, project_filter=project_filter)
            rep = analyzer.run()
        finally:
            if cache is not None:
                cache.close()
        self.report = rep
        if write:
            self.report_paths = write_reports(rep, self.paths.reports)
            log.info("reports written to %s", self.paths.reports)
        return rep

    def clear_cache(self) -> None:
        p = self.paths.cache / "analysis.sqlite"
        if p.exists():
            c = AnalysisCache(p)
            c.clear()
            c.close()

    # -- Phase 2: conversion ------------------------------------------------
    def runtime_status(self, profile: Optional[Profile] = None) -> "RuntimeInfo":
        from ..conversion.runtime import RuntimeInfo, detect_runtime

        prof = profile or (self.installation.profile if self.installation else self.current_profile())
        d = prof.conversion.outfitstudio_dir
        return detect_runtime(d) if d else RuntimeInfo(reason="no Outfit Studio 5.8+ folder configured")

    def _knowledge_base(self):
        from ..analysis.reference import build_knowledge_base

        if self.installation is None:
            raise RuntimeError("no installation detected")
        kb = getattr(self, "_kb", None)
        if kb is None or getattr(self, "_kb_inst", None) is not self.installation:
            kb = build_knowledge_base(self.installation)
            self._kb, self._kb_inst = kb, self.installation
        return kb

    def fresh_project(self, pr: ProjectResult) -> ProjectResult:
        """Re-analyze one project (no cache, no report write) so a plan never uses stale data."""
        inst = self.installation
        if inst is None:
            raise RuntimeError("no installation detected")
        target = pr.osp_rel.lower()
        an = LibraryAnalyzer(inst, workers=1, project_filter=lambda mod, rel: rel.lower() == target)
        rep = an.run()
        fresh = [p for p in rep.projects if p.project == pr.project and p.set_index == pr.set_index]
        if not fresh:
            raise RuntimeError(f"project '{pr.project}' is no longer in {pr.osp_rel}")
        f = fresh[0]
        if self.report is not None:  # library-wide info (same output mesh) comes from the full report
            f.same_output_projects = [x for x in pr.same_output_projects]
        return f

    def plan_conversion(self, pr: ProjectResult, output_mod: Optional[str] = None) -> "ConversionPlan":
        """Dry run: builds the full plan and the automation script; writes nothing."""
        from ..conversion.plan import build_plan

        fresh = self.fresh_project(pr)
        rt = self.runtime_status()
        library = self.report.projects if self.report is not None else None
        return build_plan(self.installation, self._knowledge_base(), fresh, rt,
                          output_mod_path=Path(output_mod) if output_mod else None, library=library,
                          created=time.strftime("%Y-%m-%dT%H:%M:%S"))

    def convert(self, plan: "ConversionPlan", progress: Optional[Callable[[str], None]] = None,
                runner: Optional[Callable] = None) -> "ConversionResult":
        from ..conversion.engine import ConversionEngine

        self._cancel.clear()
        eng = ConversionEngine(self.paths.root, self.installation, self._knowledge_base(), runner=runner)
        res = eng.convert(plan, progress=progress, cancel=self._cancel.is_set)
        log.info("conversion %s: %s %s", res.id, res.status, "; ".join(res.reasons[:3]))
        return res

    def revalidate(self, workspace, commit: bool = False,
                   progress: Optional[Callable[[str], None]] = None) -> "ConversionResult":
        """Re-check an existing conversion workspace (Outfit Studio is not started again)."""
        from pathlib import Path as _P

        from ..conversion.engine import ConversionEngine

        eng = ConversionEngine(self.paths.root, self.installation, self._knowledge_base())
        ws = _P(workspace)
        if not ws.is_absolute():  # "<id>" or "work\\conversions\\<id>" relative to the tool folder
            ws = self.paths.root / ws if (self.paths.root / ws).is_dir() else eng.work_root / ws
        res = eng.revalidate(ws, commit=commit, progress=progress)
        log.info("revalidation %s: %s %s", res.id, res.status, "; ".join(res.reasons[:3]))
        return res

    # -- Phase 3: package / batch ---------------------------------------------
    def package_names(self) -> list[str]:
        from ..conversion.package import package_names

        return package_names(self.installation) if self.installation else []

    def output_mod_path(self, output_mod: Optional[str] = None) -> Optional[Path]:
        from ..conversion.plan import default_output_mod

        s = self.installation.profile.conversion
        if output_mod:
            return Path(output_mod)
        if s.output_mod_path:
            return Path(s.output_mod_path)
        return default_output_mod(self.installation)

    def analyze_package(self, package: str, output_mod: Optional[str] = None,
                        progress: Optional[Callable[[int, int, str], None]] = None, write: bool = True,
                        allow_body_replacement: bool = False, allow_direct_nif: bool = False):
        """Batch Preview of one package (Dry Run). Writes only the preview reports into reports/packages.

        ``allow_direct_nif`` (0.4.0, opt-in): also analyze the package's plugin-referenced female game meshes
        (read-only). A package without BodySlide projects then gives a DIRECT_NIF preview instead of an error."""
        from ..conversion.direct_nif import SOURCE_BODYSLIDE_PROJECT, analyze_direct_package, write_direct_reports
        from ..conversion.package import PackagePreview, build_package_preview, write_preview_reports

        inst = self.installation
        if inst is None:
            raise RuntimeError("no installation detected")
        self._cancel.clear()
        out_mod = self.output_mod_path(output_mod)
        names = {package, out_mod.name if out_mod else ""}
        cache = AnalysisCache(self.paths.cache / "analysis.sqlite") if inst.profile.analysis.use_cache else None
        try:
            an = LibraryAnalyzer(inst, cache=cache, progress=progress, cancel=self._cancel.is_set, workers=None,
                                 project_filter=lambda mod, rel: mod in names)
            rep = an.run()
        finally:
            if cache is not None:
                cache.close()
        self.direct_report = None
        has_projects = any(p.mod == package for p in rep.projects)
        if not has_projects and not allow_direct_nif:
            raise RuntimeError(f"package '{package}' provides no BodySlide project (is it the exact MO2 mod name?). "
                               "If it ships built game meshes and a plugin, analyze it with --allow-direct-nif "
                               "(read-only Direct-NIF analysis)")
        direct = None
        if allow_direct_nif:
            direct = analyze_direct_package(inst, self._knowledge_base(), package, rep.projects, out_mod,
                                            allow_body_replacement=allow_body_replacement, progress=progress)
            self.direct_report = direct
        if not has_projects:
            prev = PackagePreview(package=package, created=direct.created, output_mod=str(out_mod or ""),
                                  runtime=self.runtime_status().to_dict(), allow_body_replacement=allow_body_replacement,
                                  source_mode=direct.source_mode, direct_nif=direct.to_dict())
            prev.notes.append("no BodySlide project: Direct-NIF analysis only (read-only; nothing can be converted yet)")
            prev.notes += direct.notes
            self.preview = prev
            if write:
                self.preview_paths = write_preview_reports(prev, self.paths.reports / "packages")
                self.preview_paths.update({f"direct_{k}": v for k, v in
                                           write_direct_reports(direct, self.paths.reports / "packages").items()})
            return prev
        library = list(rep.projects)
        if self.report is not None:  # library-wide checks (other mods building the same mesh)
            library += [p for p in self.report.projects if p.mod not in names]
        prev = build_package_preview(inst, self._knowledge_base(), package, rep.projects, self.runtime_status(), out_mod,
                                     library=library, progress=progress, allow_body_replacement=allow_body_replacement)
        if self.report is None:
            prev.notes.append("same-output checks cover this package and the output mod only (run Analyze Library "
                              "first for library-wide checks)")
        prev.source_mode = direct.source_mode if direct is not None else SOURCE_BODYSLIDE_PROJECT
        if direct is not None:
            prev.direct_nif = direct.to_dict()
            prev.notes += direct.notes
        self.preview = prev
        if write:
            self.preview_paths = write_preview_reports(prev, self.paths.reports / "packages")
            if direct is not None:
                self.preview_paths.update({f"direct_{k}": v for k, v in
                                           write_direct_reports(direct, self.paths.reports / "packages").items()})
        return prev

    def plan_batch(self, preview, select: Optional[list[str]] = None, deselect: Optional[list[str]] = None,
                   write: bool = True) -> tuple[list[str], dict]:
        """Batch Dry Run: apply the selection and build the batch script. Writes only reports/packages."""
        from ..conversion.batch import dry_run_sessions
        from ..conversion.package import write_preview_reports

        problems = preview.select(select, deselect) if (select is not None or deselect) else []
        conv = self.installation.profile.conversion
        scripts = dry_run_sessions(preview, conv.batch_session_mode, conv.max_projects_per_session)
        if write:
            self.preview_paths = write_preview_reports(preview, self.paths.reports / "packages", scripts)
        return problems, scripts

    def _batch_engine(self, runner: Optional[Callable] = None):
        from ..conversion.batch import BatchEngine

        return BatchEngine(self.paths.root, self.installation, self._knowledge_base(), runner=runner)

    def convert_batch(self, preview, progress: Optional[Callable[[str], None]] = None,
                      runner: Optional[Callable] = None) -> tuple[Path, dict]:
        """Create the batch workspace and run Outfit Studio + validation. Never commits."""
        self._cancel.clear()
        eng = self._batch_engine(runner)
        bdir = eng.create(preview, progress)
        summary = eng.run(bdir, progress=progress, cancel=self._cancel.is_set)
        log.info("batch %s: %s", bdir.name, summary["counts"])
        return bdir, summary

    def resolve_batch(self, batch) -> Path:
        eng_root = self.paths.root / "work" / "batch_conversions"
        b = Path(batch)
        if not b.is_absolute():
            b = self.paths.root / b if (self.paths.root / b).is_dir() else eng_root / b
        return b

    def validate_batch(self, batch, progress: Optional[Callable[[str], None]] = None) -> dict:
        return self._batch_engine().validate(self.resolve_batch(batch), progress)

    def commit_batch(self, batch, only: Optional[list[str]] = None, progress: Optional[Callable[[str], None]] = None) -> dict:
        return self._batch_engine().commit(self.resolve_batch(batch), only=only, progress=progress)

    def batch_status(self, batch) -> dict:
        return self._batch_engine().summary(self.resolve_batch(batch))

    def verify_build(self, generated: Optional[str] = None, output_mod: Optional[str] = None,
                     write: bool = True) -> list:
        """Post-build check (read-only): the game mesh BodySlide built for each committed conversion
        (or only ``generated``) against its generated ShapeData, plus the in-game items that use it.
        Writes only reports/builds."""
        from ..conversion.buildcheck import verify_build, write_build_report
        from ..conversion.manifest import Manifest

        inst = self.installation
        if inst is None:
            raise RuntimeError("no installation detected")
        om = self.output_mod_path(output_mod)
        if om is None or not (om / "manifest.json").is_file():
            raise RuntimeError(f"no manifest.json in {om}")
        recs = [r for r in Manifest.load(om).records if r.get("status") == "SUCCESS"]
        latest: dict[str, dict] = {}
        for r in recs:  # latest record per generated project
            latest[r.get("target", {}).get("project", "")] = r
        if generated:
            if generated not in latest:
                raise KeyError(f"no committed conversion of '{generated}' in the manifest")
            latest = {generated: latest[generated]}
        try:
            kb = self._knowledge_base()
            phys = list(kb.reference.physics_bones) if kb and kb.reference else []
        except Exception:  # noqa: BLE001 - fall back to the recorded settings
            phys = []
        out = []
        for name, r in latest.items():
            bones = phys or list(r.get("settings", {}).get("physics_bones", []))
            prov = next((p for p in inst.providers if p.name == r.get("source", {}).get("mod")), None)
            res = verify_build(r, om, inst.providers, bones, prov.root if prov else None)
            if write:
                write_build_report(res, self.paths.reports / "builds")
            out.append(res)
        return out

    # -- Phase 3.2: mass conversion ------------------------------------------------------
    def resume_batch(self, batch, progress: Optional[Callable[[str], None]] = None,
                     runner: Optional[Callable] = None, control: Optional[str] = None,
                     control_failure: Optional[Callable[[dict], str]] = None) -> dict:
        """Continue an interrupted batch from its unfinished projects (never commits)."""
        self._cancel.clear()
        return self._batch_engine(runner).resume(self.resolve_batch(batch), progress=progress, cancel=self._cancel.is_set,
                                                 control=control, control_failure=control_failure)

    def probe_outfit_studio(self, driver=None) -> dict:
        """0.5.3: start the private Outfit Studio, open its Automation window with a one-step probe script preselected,
        verify it and close it again — nothing is executed. Writes only work/os_probe and reports/os_probe_*.json."""
        import time as _t

        from ..conversion.automation import AutomationScript, AutomationStep
        from ..conversion.os_control import SessionController, SessionSpec, write_session_result
        from ..conversion.plan import sha256_file
        from ..conversion.runtime import write_runtime_config

        eng = self._batch_engine()
        rt = eng.eng.resolve_runtime(prepare=True)
        if not rt.path:
            raise RuntimeError(f"Outfit Studio not usable: {rt.reason}")
        drv = driver or eng._driver(rt)
        if drv is None:
            raise RuntimeError("the unattended Outfit Studio control is not available on this system")
        ws = self.paths.root / "work" / "os_probe"
        project = ws / "Data" / "Tools" / "BodySlide"
        (project / "Automations").mkdir(parents=True, exist_ok=True)
        for sub in ("SliderSets", "ShapeData"):
            (project / sub).mkdir(parents=True, exist_ok=True)
        name = "cbbe3bbb_probe"
        sc = AutomationScript(name=name, steps=[AutomationStep("ClearProject", note="probe: never executed")])
        (project / "Automations" / f"{name}.xml").write_text(sc.to_xml(), encoding="utf-8")
        write_runtime_config(Path(rt.folder), project, ws / "Data")
        spec = SessionSpec(batch_id="probe", session=0, script_name=name,
                           script_sha256=sha256_file(project / "Automations" / f"{name}.xml"), expected_steps=1,
                           projects=[], project_dir=str(project), runtime_folder=rt.folder, exe=rt.path,
                           ready_timeout_s=120, diagnostics_dir=str(ws / "logs"))
        res = SessionController(drv, poll_s=float(getattr(drv, "poll_s", 0.25))).probe(spec)
        out = self.paths.reports / f"os_probe_{_t.strftime('%Y%m%d-%H%M%S')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        write_session_result(res, out)
        d = res.to_dict()
        d.pop("automation_output", None)
        d["report"] = str(out)
        return d

    def pipeline(self, package: str, mode: str = "SAFE", commit: bool = True, events=None,
                 runner: Optional[Callable] = None, output_mod: Optional[str] = None,
                 control_failure: Optional[Callable[[dict], str]] = None, route: Optional[str] = None,
                 source_body: Optional[str] = None, target_body: Optional[str] = None):
        from .pipeline import PackagePipeline

        return PackagePipeline(self, package, mode=mode, commit=commit, events=events, runner=runner, output_mod=output_mod,
                               control_failure=control_failure, route=route, source_body=source_body,
                               target_body=target_body)

    def export_diagnostics(self, out_path=None, batch=None, result=None, preview=None) -> Path:
        from .diagnostics import export_diagnostic_report

        return export_diagnostic_report(self, out_path, batch=batch, result=result, preview=preview)

    def write_package_report(self, result, verify: Optional[dict] = None) -> dict[str, Path]:
        from .report import write_package_report

        return write_package_report(result, self.paths.reports / "packages", verify=verify)

    def record_runtime_issue(self, name: str, state: str = "FAIL", note: str = "",
                             output_mod: Optional[str] = None) -> dict:
        """0.5.2: record the user's in-game verification of a converted outfit (``FAIL`` no physics, ``PARTIAL``
        physics problem, ``PASS`` works / clears a report) in manifest.json. ``name`` is the generated project or the
        source project. The automated validation result is not changed; reports and the GUI list FAIL / PARTIAL
        outfits under 'Needs attention'."""
        from ..conversion.manifest import Manifest

        state = state.upper()
        if state not in ("FAIL", "PARTIAL", "PASS"):
            raise ValueError(f"unknown state {state}")
        om = self.output_mod_path(output_mod)
        if om is None or not (om / "manifest.json").is_file():
            raise RuntimeError(f"no manifest.json in {om}")
        man = Manifest.load(om)
        names = {r.get("target", {}).get("project", "") for r in man.records}
        gen = name
        if gen not in names:
            suffix = self.installation.profile.conversion.project_suffix if self.installation else " - 3BBB AUTO"
            cand = [n for n in names if n == f"{name}{suffix}"
                    or any(r.get("source", {}).get("project") == name and r.get("target", {}).get("project") == n
                           for r in man.records)]
            if not cand:
                raise KeyError(f"no committed conversion of '{name}' in {om / 'manifest.json'}")
            gen = cand[0]
        rec = man.record_acceptance(gen, state, note)
        man.save()
        return {"generated_project": gen, "state": state, "note": note, "entries": len(rec["acceptance"]["history"])}

    def verify_package_build(self, package: str, output_mod: Optional[str] = None, write: bool = True) -> dict:
        """Part J (read-only): every committed AUTO project of ``package`` — its built game mesh and its SliderGroup
        membership — aggregated. Run after the BodySlide Batch Build."""
        from ..conversion.manifest import Manifest
        from ..conversion.package import MAIN_GROUP, output_mod_state, package_group_name

        inst = self.installation
        if inst is None:
            raise RuntimeError("no installation detected")
        om = self.output_mod_path(output_mod)
        if om is None or not (om / "manifest.json").is_file():
            raise RuntimeError(f"no manifest.json in {om}")
        recs = [r for r in Manifest.load(om).records if r.get("status") == "SUCCESS"
                and (r.get("source", {}).get("mod") == package or r.get("batch", {}).get("package") == package)]
        latest: dict[str, dict] = {}
        for r in recs:
            latest[r.get("target", {}).get("project", "")] = r
        checks = []
        for name in sorted(latest):
            try:
                checks.extend(self.verify_build(generated=name, output_mod=str(om), write=write))
            except Exception as exc:  # noqa: BLE001 - one broken record must not hide the others
                from ..conversion.buildcheck import BuildCheck

                checks.append(BuildCheck(generated_project=name, status="ERROR", warnings=[str(exc)]))
        counts: dict[str, int] = {}
        for c in checks:
            counts[c.status] = counts.get(c.status, 0) + 1
        groups = {}
        for g in (MAIN_GROUP, package_group_name(package)):
            f = om / "Tools" / "BodySlide" / "SliderGroups" / f"{g}.xml"
            text = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
            missing = [n for n in latest if f'"{n}"' not in text and f">{n}<" not in text]
            groups[g] = {"file": str(f), "exists": f.is_file(), "missing": missing}
        state = output_mod_state(inst, om)
        res = {"package": package, "output_mod": str(om), "output_mod_state": state, "committed": len(latest),
               "counts": counts, "groups": groups,
               "rows": [{"generated_project": c.generated_project, "status": c.status,
                         "problems": ([f"{x['name']}: {x['detail']}" for x in c.failed] + list(c.warnings))[:4],
                         "items": [g.get("armor_name", "") for g in c.game_items][:3]} for c in checks]}
        res["ok"] = (state == "ENABLED" and counts.get("BUILD_OK", 0) == len(latest) and len(latest) > 0
                     and all(g["exists"] and not g["missing"] for g in groups.values()))
        res["explanation"] = build_explanation(res, package_group_name(package))
        if write:
            d = self.paths.reports / "builds"
            d.mkdir(parents=True, exist_ok=True)
            safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in package)[:60]
            (d / f"{safe}_package_build.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        return res

    # -- 0.4.1: Direct-NIF production workflow ---------------------------------------
    def plan_direct_batch(self, package: str, select: list[str], output_mod: Optional[str] = None,
                          progress: Optional[Callable[[int, int, str], None]] = None, write: bool = True):
        """PLAN (read-only): fresh Direct-NIF analysis + explicit selection. Writes only reports/direct_batches."""
        from ..conversion.direct_batch import plan_direct_batch, write_plan_reports

        self.analyze_package(package, output_mod=output_mod, progress=progress, write=False, allow_direct_nif=True)
        rep = self.direct_report
        plan = plan_direct_batch(self.installation, self._knowledge_base(), rep, list(select or []),
                                 self.output_mod_path(output_mod), self.runtime_status())
        self.direct_plan = plan
        self.direct_plan_paths = write_plan_reports(plan, self.paths.reports / "direct_batches") if write else {}
        return plan

    def _direct_engine(self, runner: Optional[Callable] = None):
        from ..conversion.direct_batch import DirectBatchEngine

        return DirectBatchEngine(self.paths.root, self.installation, self._knowledge_base(), runner=runner)

    def convert_direct_batch(self, plan, progress: Optional[Callable[[str], None]] = None,
                             runner: Optional[Callable] = None) -> tuple[Path, dict]:
        """CONVERT TO WORKSPACE + VALIDATE. Never commits."""
        self._cancel.clear()
        eng = self._direct_engine(runner)
        bdir = eng.create(plan, progress)
        summary = eng.run(bdir, progress=progress, cancel=self._cancel.is_set)
        log.info("direct batch %s: %s", bdir.name, summary["counts"])
        return bdir, summary

    def resolve_direct_batch(self, batch) -> Path:
        root = self.paths.root / "work" / "direct_nif_batches"
        b = Path(batch)
        if not b.is_absolute():
            b = self.paths.root / b if (self.paths.root / b).is_dir() else root / b
        return b

    def validate_direct_batch(self, batch, progress: Optional[Callable[[str], None]] = None) -> dict:
        return self._direct_engine().validate(self.resolve_direct_batch(batch), progress)

    def commit_direct_batch(self, batch, only: Optional[list[str]] = None,
                            progress: Optional[Callable[[str], None]] = None) -> dict:
        return self._direct_engine().commit(self.resolve_direct_batch(batch), only=only, progress=progress)

    def direct_batch_status(self, batch) -> dict:
        return self._direct_engine().summary(self.resolve_direct_batch(batch))

    def list_direct_batches(self) -> list[Path]:
        root = self.paths.root / "work" / "direct_nif_batches"
        return sorted((p for p in root.iterdir() if (p / "batch.json").is_file()), reverse=True) if root.is_dir() else []

    def verify_direct_build(self, mesh: Optional[str] = None, output_mod: Optional[str] = None, write: bool = True) -> list:
        """VERIFY (read-only): every committed Direct-NIF mesh (or ``mesh``: file name, game path or ID)."""
        from ..conversion.direct_batch import direct_id, latest_direct_records, verify_direct_build, write_verify_report
        from ..conversion.gamedata import normalize_mesh_path

        inst = self.installation
        if inst is None:
            raise RuntimeError("no installation detected")
        om = self.output_mod_path(output_mod)
        recs = latest_direct_records(om)
        if mesh:
            m = mesh.strip()
            hits = {k: r for k, r in recs.items()
                    if m.lower() in (direct_id(k), k, Path(k).name) or normalize_mesh_path(m) == k}
            if not hits:
                return [verify_direct_build(None, m, om, inst, [], None)]
            recs = hits
        if not recs:
            raise KeyError(f"no committed Direct-NIF conversion in {om / 'manifest.json'}")
        kb = self._knowledge_base()
        phys = list(kb.reference.physics_bones) if kb and kb.reference else []
        out = []
        for key, r in sorted(recs.items()):
            prov = next((p for p in inst.providers if p.name == r.get("source", {}).get("mod")), None)
            out.append(verify_direct_build(r, r.get("game_path", key), om, inst, phys, prov.root if prov else None))
        if write:
            write_verify_report(out, self.paths.reports / "builds")
        return out

    def list_batches(self) -> list[Path]:
        root = self.paths.root / "work" / "batch_conversions"
        return sorted((p for p in root.iterdir() if (p / "batch.json").is_file()), reverse=True) if root.is_dir() else []

    @staticmethod
    def filters() -> list[str]:
        return list(FILTERS)


def build_explanation(v: dict, group: str) -> list[str]:
    """0.5.3: what a normal user reads about a package build check (the statuses stay in the report)."""
    from .messages import BUILD_STALE_TEXT, package_message

    c = v.get("counts", {})
    out: list[str] = []
    action = f"Open BodySlide and build only the package group '{group}' (Batch Build)."
    if v.get("committed") and c.get("BUILD_OK", 0) == v.get("committed"):
        out.append(f"All {v['committed']} converted outfits are built from their 3BBB projects.")
    if c.get("BUILD_STALE"):
        out.append(BUILD_STALE_TEXT.format(n=c["BUILD_STALE"]))
    if c.get("NOT_BUILT"):
        out.append(f"{c['NOT_BUILT']} converted outfits have not been built yet.")
    if c.get("BUILD_MISMATCH"):
        out.append(f"{c['BUILD_MISMATCH']} built outfits do not match their converted project (rebuild them from the "
                   "package group).")
    if c.get("BUILD_STALE") or c.get("NOT_BUILT") or c.get("BUILD_MISMATCH"):
        out.append(action)
        out.append("Do not batch-build the original projects of this mod afterwards: they build the same game mesh files.")
    if v.get("output_mod_state") not in ("ENABLED", None, ""):
        out.append(package_message("OUTPUT_MOD_DISABLED_IN_MO2"))
    return out
