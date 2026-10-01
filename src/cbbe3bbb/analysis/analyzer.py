"""Library scan: discover every BodySlide project in the virtual Data view,
analyze it against the installed 3BBB reference and classify it.

Read-only by construction: the only files opened for writing are the tool's
own cache/report/log files.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .. import __version__
from ..bodyslide.osp import OspParseError, SliderSet, parse_osp
from ..classification.family import detect_family
from ..classification.rules import classify_project
from ..discovery.install import Installation
from ..discovery.vfs import VirtualFile
from ..nif.reader import NifError, NifFile, read_nif
from .cache import AnalysisCache
from .models import Action, FamilyResult, ProjectResult
from .reference import KnowledgeBase, build_knowledge_base
from .shape_metrics import MetricSettings, analyze_shape

log = logging.getLogger(__name__)

__all__ = ["ANALYZER_VERSION", "ScanReport", "LibraryAnalyzer", "ProgressFn", "CancelledError"]

ANALYZER_VERSION = "1.2"  # 1.1: per-shape DataFolder is body evidence only for body folders; 1.2: exact shape names (0.6.1)
ProgressFn = Callable[[int, int, str], None]


class CancelledError(Exception):
    pass


@dataclass
class ScanReport:
    tool_version: str
    analyzer_version: str
    started: str
    finished: str = ""
    elapsed_s: float = 0.0
    cancelled: bool = False
    installation: dict = field(default_factory=dict)
    reference: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    projects: list[ProjectResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["projects"] = [p.to_dict() for p in self.projects]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ScanReport":
        d = dict(d)
        projects = [ProjectResult.from_dict(p) for p in d.pop("projects", [])]
        return cls(projects=projects, **d)


def _fp(*parts: object) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


class LibraryAnalyzer:
    def __init__(
        self,
        installation: Installation,
        cache: Optional[AnalysisCache] = None,
        progress: Optional[ProgressFn] = None,
        cancel: Optional[Callable[[], bool]] = None,
        workers: Optional[int] = None,
        project_filter: Optional[Callable[[str, str], bool]] = None,
    ):
        self.inst = installation
        self.cache = cache
        self.progress = progress or (lambda *_: None)
        self.cancel = cancel or (lambda: False)
        self.workers = workers or max(1, min(4, (os.cpu_count() or 2)))
        self.project_filter = project_filter  # (mod, osp_rel) -> include?
        prof = installation.profile
        self.settings = MetricSettings(
            search_radius=prof.conversion.search_radius,
            max_vertex_targets=prof.conversion.max_vertex_targets,
            no_target_limit=prof.conversion.no_target_limit,
            region_weight_threshold=prof.analysis.region_weight_threshold,
        )
        self._nif_cache: dict[str, NifFile | Exception] = {}
        self._nif_lock = threading.Lock()
        self._tree_cache: dict = {}
        self.kb: Optional[KnowledgeBase] = None

    # ------------------------------------------------------------------
    def _load_nif(self, vf: VirtualFile) -> NifFile:
        key = str(vf.abs_path)
        with self._nif_lock:
            hit = self._nif_cache.get(key)
        if hit is None:
            try:
                hit = read_nif(vf.abs_path)
            except NifError as exc:
                hit = exc
            with self._nif_lock:
                self._nif_cache[key] = hit
        if isinstance(hit, Exception):
            raise hit
        return hit

    def _analyze_set(self, vf: VirtualFile, idx: int, ss: SliderSet) -> ProjectResult:
        t0 = time.perf_counter()
        vfs = self.inst.vfs
        kb = self.kb
        pr = ProjectResult(
            project=ss.name,
            mod=vf.provider.name,
            provider_kind=vf.provider.kind,
            osp_rel=vf.rel,
            osp_path=str(vf.abs_path),
            set_index=idx,
            output_path=ss.output_path,
            output_file=ss.output_file,
            slider_count=len(ss.sliders),
            zap_count=len(ss.zap_sliders),
            shape_names=[s.name for s in ss.shapes],
        )
        pr.osp_also_provided_by = [v.provider.name for v in vfs.versions(vf.rel)[1:]]
        if not ss.source_file:
            pr.errors.append("project has no <SourceFile>")
            return pr
        rel = f"ShapeData/{ss.data_folder}/{ss.source_file}" if ss.data_folder else f"ShapeData/{ss.source_file}"
        pr.shapedata_rel = rel
        nvf = vfs.resolve(rel)
        if nvf is None:
            pr.errors.append(f"ShapeData NIF not found: {rel}")
            return pr
        pr.shapedata_path = str(nvf.abs_path)
        pr.shapedata_provider = nvf.provider.name
        pr.shapedata_also_provided_by = [v.provider.name for v in vfs.versions(rel)[1:]]
        ref_fp = kb.reference.fingerprint if kb and kb.reference else "noref"
        pr.fingerprint = _fp(
            ANALYZER_VERSION, vf.size, int(vf.mtime), vf.provider.name, idx, ss.name, nvf.size, int(nvf.mtime), nvf.provider.name,
            ref_fp, json.dumps(asdict(self.settings), sort_keys=True), len(kb.cbbe_sliders) if kb else 0,
        )
        if self.cache is not None and self.inst.profile.analysis.use_cache:
            cached = self.cache.get(pr.key, pr.fingerprint)
            if cached is not None:
                try:
                    res = ProjectResult.from_dict(cached)
                    res.cached = True
                    res.osp_also_provided_by = pr.osp_also_provided_by
                    res.shapedata_also_provided_by = pr.shapedata_also_provided_by
                    return res
                except (TypeError, KeyError, ValueError):
                    pass
        try:
            nif = self._load_nif(nvf)
        except NifError as exc:
            pr.errors.append(f"cannot read ShapeData NIF: {exc}")
            return pr
        if not nif.header.is_fallout4:
            pr.family = FamilyResult(family="NON_FO4", label=f"non-Fallout 4 NIF (BS version {nif.header.bs_version})")
            pr.warnings.append(f"NIF is not a Fallout 4 mesh (user version {nif.header.user_version}, BS version {nif.header.bs_version})")
        pr.warnings.extend(nif.warnings[:5])
        for btype, name in nif.unsupported_shapes:
            pr.warnings.append(f"shape '{name}' uses unsupported block type {btype}; not analyzed")
        entries = {e.name: e for e in ss.shapes}
        nif_names = {s.name for s in nif.shapes}
        for e in ss.shapes:
            if e.name not in nif_names:
                pr.warnings.append(f"shape '{e.name}' listed in the project is missing from the ShapeData NIF")
        for shape in nif.shapes:
            entry = entries.get(shape.name)
            sliders = len(ss.sliders_for_target(entry.target)) if entry else 0
            try:
                sa = analyze_shape(shape, entry, kb, self.settings, sliders, self._tree_cache)
            except Exception as exc:  # noqa: BLE001 - one broken shape must not stop the project
                log.exception("shape analysis failed: %s / %s", ss.name, shape.name)
                pr.warnings.append(f"shape '{shape.name}': analysis failed ({exc})")
                continue
            if entry is None:
                sa.warnings.append("shape is not listed in the slider set (built without sliders)")
            pr.shapes.append(sa)
        if pr.family.family != "NON_FO4":
            pr.family = detect_family(ss.name, vf.provider.name, ss.data_folder, ss.slider_names, pr.shapes, kb)
        pr.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        if self.cache is not None and not pr.errors:
            self.cache.put(pr.key, pr.fingerprint, pr.to_dict())
        return pr

    # ------------------------------------------------------------------
    def run(self) -> ScanReport:
        t0 = time.time()
        rep = ScanReport(tool_version=__version__, analyzer_version=ANALYZER_VERSION, started=time.strftime("%Y-%m-%d %H:%M:%S"))
        rep.installation = self.inst.summary()
        rep.settings = asdict(self.settings)
        if self.inst.vfs is None:
            rep.notes.append("installation not detected; nothing scanned")
            return rep
        self.progress(0, 0, "Loading installed reference bodies...")
        self.kb = build_knowledge_base(self.inst)
        rep.notes.extend(self.kb.notes)
        ref = self.kb.reference
        rep.reference = (
            {
                "template": ref.display_name,
                "set": ref.set_name,
                "shape": ref.shape_name,
                "source": ref.source,
                "vertices": ref.num_vertices,
                "physics_bones": ref.physics_bones,
                "physics_regions": ref.physics_regions,
                "side_sign": ref.side_sign,
            }
            if ref
            else {}
        )
        osp_files = [
            vf for vf in self.inst.vfs.iter_winners("SliderSets", (".osp", ".xml"))
            if not self.project_filter or self.project_filter(vf.provider.name, vf.rel)
        ]
        log.info("scan: %d project files in %d data providers", len(osp_files), len(self.inst.providers))
        jobs: list[tuple[VirtualFile, int, SliderSet]] = []
        results: list[ProjectResult] = []
        parse_fail = 0
        for i, vf in enumerate(osp_files):
            if self.cancel():
                rep.cancelled = True
                break
            if i % 25 == 0:
                self.progress(i, len(osp_files), f"Reading project files ({i}/{len(osp_files)})")
            try:
                osp = parse_osp(vf.abs_path)
            except OspParseError as exc:
                parse_fail += 1
                log.warning("OSP parse failure %s (%s): %s", vf.rel, vf.provider.name, exc)
                pr = ProjectResult(project=Path(vf.rel).stem, mod=vf.provider.name, provider_kind=vf.provider.kind, osp_rel=vf.rel, osp_path=str(vf.abs_path))
                pr.errors.append(f"cannot parse project file: {exc}")
                results.append(pr)
                continue
            if osp.errors and not osp.sets:
                if vf.rel.lower().endswith(".xml"):
                    continue  # stray XML in SliderSets that is not a slider set
                pr = ProjectResult(project=Path(vf.rel).stem, mod=vf.provider.name, provider_kind=vf.provider.kind, osp_rel=vf.rel, osp_path=str(vf.abs_path))
                pr.errors.extend(osp.errors)
                results.append(pr)
                continue
            for e in osp.errors:
                log.warning("OSP %s: %s", vf.rel, e)
            for idx, ss in enumerate(osp.sets):
                jobs.append((vf, idx, ss))

        total = len(jobs)
        done = 0
        if not rep.cancelled:
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                futs = {}
                for job in jobs:
                    futs[ex.submit(self._analyze_set, *job)] = job
                for fut in as_completed(futs):
                    vf, idx, ss = futs[fut]
                    try:
                        results.append(fut.result())
                    except Exception as exc:  # noqa: BLE001 - keep scanning
                        log.exception("project analysis crashed: %s", ss.name)
                        pr = ProjectResult(project=ss.name, mod=vf.provider.name, provider_kind=vf.provider.kind, osp_rel=vf.rel, osp_path=str(vf.abs_path), set_index=idx)
                        pr.errors.append(f"analysis crashed: {exc}")
                        results.append(pr)
                    done += 1
                    if done % 5 == 0 or done == total:
                        self.progress(done, total, f"Analyzing projects ({done}/{total})")
                    if self.cancel():
                        rep.cancelled = True
                        for f in futs:
                            f.cancel()
                        break
        if self.cache is not None:
            self.cache.commit()

        self._cross_project(results)
        ref_ok = self.kb.reference is not None
        for pr in results:
            if pr.family.family == "NON_FO4":
                pr.action = Action.UNSUPPORTED.value
                pr.reasons = [pr.family.label]
                continue
            classify_project(pr, ref_ok)
        results.sort(key=lambda p: (p.mod.lower(), p.project.lower()))
        rep.projects = results
        rep.finished = time.strftime("%Y-%m-%d %H:%M:%S")
        rep.elapsed_s = round(time.time() - t0, 2)
        counts: dict[str, int] = {}
        for p in results:
            counts[p.action] = counts.get(p.action, 0) + 1
        rep.stats = {
            "project_files": len(osp_files),
            "projects": len(results),
            "shapedata_nifs": len({p.shapedata_path for p in results if p.shapedata_path}),
            "osp_parse_failures": parse_fail,
            "errors": counts.get(Action.ERROR.value, 0),
            "cached": sum(1 for p in results if p.cached),
            "mods_with_projects": len({p.mod for p in results}),
            "actions": counts,
        }
        log.info("scan finished in %.1fs: %s", rep.elapsed_s, rep.stats)
        return rep

    # ------------------------------------------------------------------
    @staticmethod
    def _cross_project(results: list[ProjectResult]) -> None:
        by_output: dict[str, list[ProjectResult]] = {}
        by_name: dict[str, list[ProjectResult]] = {}
        for p in results:
            if p.output_file:
                key = f"{p.output_path}/{p.output_file}".replace("\\", "/").lower().strip("/")
                if key.startswith("data/"):
                    key = key[5:]
                by_output.setdefault(key, []).append(p)
            by_name.setdefault(p.project.lower(), []).append(p)
        for key, group in by_output.items():
            if len(group) < 2:
                continue
            for p in group:
                others = [q for q in group if q is not p]
                p.same_output_projects = [f"{q.project} [{q.mod}]" for q in others]
                p.warnings.append(f"{len(others)} other project(s) build the same output mesh '{key}'")
                if p.family.physics != "3bbb":
                    sup = [q for q in others if q.family.physics == "3bbb" and q.osp_rel.lower() != p.osp_rel.lower()]
                    if sup:
                        p.superseded_by = f"{sup[0].project} [{sup[0].mod}]"
                    same_file = [q for q in others if q.family.physics == "3bbb" and q.osp_rel.lower() == p.osp_rel.lower()]
                    if same_file and not sup:
                        p.warnings.append(
                            f"a 3BBB variant in the same project file ('{same_file[0].project}') already builds this output (manual conversion?)"
                        )
        for name, group in by_name.items():
            files = {(q.osp_rel.lower(), q.mod) for q in group}
            if len(files) > 1:
                for p in group:
                    others = sorted({f"{q.osp_rel} [{q.mod}]" for q in group if q is not p})
                    p.warnings.append(f"duplicate slider set name also defined in: {'; '.join(others[:3])} (BodySlide lists only one)")
