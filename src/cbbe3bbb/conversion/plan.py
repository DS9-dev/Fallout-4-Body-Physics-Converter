"""Conversion planning (the Dry Run): decides what a conversion would do, writes nothing.

A :class:`ConversionPlan` holds everything the preview shows (source,
reference, shapes, regions, bones, settings, output names and paths, warnings)
plus *blockers*. Conversion is refused while a blocker exists, for example:
the project is not enabled for this milestone, the analyzer does not classify it
``CONVERT``, or no Outfit Studio with headless automation is selected. The plan
also contains the exact Outfit Studio automation script that would run.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from .. import __version__
from ..analysis.models import Level, ProjectResult
from ..analysis.reference import KnowledgeBase
from ..bodyslide.osp import OspParseError, SliderSet, parse_osp
from ..discovery.install import Installation
from ..discovery.vfs import VirtualFile
from ..nif.reader import NifError, read_nif
from .automation import NO_TARGET_LIMIT, AutomationScript, AutomationStep, check_invariants, safe_script_name
from .runtime import RuntimeInfo

__all__ = [
    "DEFAULT_OUTPUT_MOD",
    "LEGACY_BONES_FOR_REGION",
    "StagedFile",
    "ShapePlan",
    "ConversionPlan",
    "shape_allowed_bones",
    "shape_copy_mode",
    "shape_bone_list",
    "region_bones",
    "generated_names",
    "build_plan",
    "build_script",
    "default_output_mod",
    "sha256_file",
]

DEFAULT_OUTPUT_MOD = "CBBE 3BBB Auto Conversions"
# vanilla/CBBE bones whose physics role a region's 3BBB bones take over
LEGACY_BONES_FOR_REGION = {
    "BREAST": ["LBreast_skin", "RBreast_skin"],
    "BUTT": ["LButtFat_skin", "RButtFat_skin"],
    "THIGH": [],
}
_FORBIDDEN = re.compile(r'[\\/:*?"<>|]')
WEIGHT_ADDED_EPS = 0.01  # a predicted weight above this counts as "bone added"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


ACCEPTANCE_COMMAND = "acceptance-test-cloth"


class AcceptanceIsolationError(RuntimeError):
    pass


def _norm_inside(p: Path, root: Path) -> bool:
    import os

    try:
        a = Path(os.path.normcase(str(Path(p).resolve())))
        b = Path(os.path.normcase(str(Path(root).resolve())))
        return a == b or a.is_relative_to(b)
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class AcceptanceIsolation:
    """0.5.3 DEVELOPMENT/ACCEPTANCE scope (``acceptance-test-cloth`` only): ONE exact original source project planned
    into a disposable, never-installed output. Only for that project, an *existing generated AUTO project of the same
    name elsewhere* (e.g. an unknown/committed one in the real output mod) is not treated as an input or a collision:
    it is ignored, never read, written, moved or hidden. Production commands never construct this object, so the
    UNKNOWN_AUTO / ALREADY_CONVERTED / "project file already exists" rules are unchanged for them.

    :meth:`verify` asserts every precondition before the exception may be used (raises otherwise)."""
    command: str
    project: str
    source_mod: str
    output_dir: str
    acceptance_root: str  # <tool>/work/acceptance
    production_output_mod: str = ""
    mods_dir: str = ""
    no_commit: bool = True

    def verify(self) -> None:
        from .batch import BatchEngine

        why = []
        if self.command != ACCEPTANCE_COMMAND:
            why.append(f"command is '{self.command}', not {ACCEPTANCE_COMMAND}")
        if not self.project or not self.source_mod:
            why.append("no exact source project")
        out, root = Path(self.output_dir), Path(self.acceptance_root)
        if root.name.lower() != "acceptance" or root.parent.name.lower() != "work" or not _norm_inside(out, root) \
                or _norm_inside(root, out):
            why.append(f"output {out} is not inside the converter-owned {root}")
        if self.production_output_mod and (_norm_inside(out, Path(self.production_output_mod))
                                           or _norm_inside(Path(self.production_output_mod), out)):
            why.append("output is (or contains) the configured production output mod")
        if self.mods_dir and _norm_inside(out, Path(self.mods_dir)):
            why.append("output is inside the MO2 mods folder")
        if not self.no_commit or not getattr(BatchEngine, "REFUSES_ACCEPTANCE_COMMIT", False):
            why.append("commit/install is not provably impossible")
        if why:
            raise AcceptanceIsolationError("acceptance isolation refused: " + "; ".join(why))

    def applies_to(self, pr) -> bool:
        return pr.project == self.project and pr.mod == self.source_mod


@dataclass
class StagedFile:
    role: str  # source-osp, source-nif, source-osd, reference-osp, reference-nif, reference-osd
    rel: str  # path relative to Tools/BodySlide, forward slashes
    abs_path: str
    provider: str
    size: int = 0
    mtime: float = 0.0


@dataclass
class ShapePlan:
    """What happens to one outfit shape. Every shape is planned on its own.

    ``regions`` are the physics regions this shape is converted for, ``allowed_bones`` the
    only physics bones it may receive, ``copy_mode`` how Outfit Studio copies them
    ("full" = Copy Bone Weights for all reference bones, predicted to add only allowed
    physics bones; "regions" = BoneList restricted to ``bone_list``).
    Plans written by 0.2.x lack the newer fields; the helpers below fall back to the
    project-level values, which is what 0.2.x did.
    """

    name: str
    vertices: int
    regions: list[str]
    predicted_full_copy_physics_bones: list[str]
    bones_outside_regions: list[str]
    allowed_bones: list[str] = field(default_factory=list)
    copy_mode: str = ""
    bone_list: list[str] = field(default_factory=list)
    confidence: str = ""
    reasons: list[str] = field(default_factory=list)
    region_details: dict = field(default_factory=dict)
    # 0.3.3: per-shape search radius (0 = the project radius). Raised only when some vertices have no reference
    # vertex within the project radius (Outfit Studio would leave them without any weight), and only when the
    # emulated copy proves that no other vertex changes.
    search_radius: float = 0.0
    out_of_reach_vertices: list[int] = field(default_factory=list)
    out_of_reach_distance: float = 0.0


def shape_radius(sp: Optional["ShapePlan"], plan: "ConversionPlan") -> float:
    return float(sp.search_radius) if sp is not None and sp.search_radius else float(plan.search_radius)


REACH_MAX_FACTOR = 2.0  # never raise a shape's radius above twice the configured radius


def plan_shape_reach(ref, pts: np.ndarray, radius: float, k: int, no_limit: bool) -> dict:
    """Which vertices Copy Bone Weights cannot reach, and the smallest safe radius that reaches them.

    Outfit Studio (``OutfitProject::CopyBoneWeights``) zeroes every copied bone of a vertex and takes new values
    only from reference vertices within the radius; a vertex without any gets no copied weight, and in full mode
    (all reference bones) it is left with no weight at all when all its weight was on reference bones.
    Returns {far, distance, radius, changed, ok, reason}.
    """
    import math

    d, _ = ref.tree.query(pts, k=1)
    far = np.nonzero(d > radius)[0]
    out = {"far": [int(i) for i in far], "distance": float(d.max()) if len(d) else 0.0, "radius": float(radius),
           "changed": [], "ok": True, "reason": ""}
    if not len(far):
        return out
    new_r = math.ceil(float(d[far].max()) * 1.05 * 2.0) / 2.0
    if no_limit:
        out.update(ok=False, reason="no-target-limit copies use every reference vertex in the radius; a larger radius "
                                    "would change other vertices")
        return out
    if new_r > REACH_MAX_FACTOR * radius:
        out.update(ok=False, reason=f"would need radius {new_r:g} (more than {REACH_MAX_FACTOR:g}× {radius:g})")
        return out
    w_old = ref.predict_weights(pts, ref.bones, radius, k, False)
    w_new = ref.predict_weights(pts, ref.bones, new_r, k, False)
    changed = np.nonzero(np.abs(w_new - w_old).max(1) > 1e-6)[0]
    out["changed"] = [int(i) for i in changed]
    if set(out["changed"]) - set(out["far"]):
        out.update(ok=False, reason=f"radius {new_r:g} would also change {len(set(out['changed']) - set(out['far']))} "
                                    "vertices that are already within reach")
        return out
    out["radius"] = new_r
    return out


def shape_allowed_bones(sp: ShapePlan, plan: "ConversionPlan") -> list[str]:
    return list(sp.allowed_bones) if sp.allowed_bones else list(plan.physics_bones)


def shape_copy_mode(sp: ShapePlan, plan: "ConversionPlan") -> str:
    return sp.copy_mode or ("regions" if plan.weight_mode == "regions" else "full")


def shape_bone_list(sp: ShapePlan, plan: "ConversionPlan") -> list[str]:
    return list(sp.bone_list) if sp.copy_mode else list(plan.bone_list)


@dataclass
class ConversionPlan:
    created: str = ""
    tool_version: str = __version__
    # --- source
    source_mod: str = ""
    source_provider_kind: str = ""
    source_project: str = ""
    source_osp_rel: str = ""
    source_osp_path: str = ""
    source_set_index: int = 0
    source_shapedata_rel: str = ""
    source_body_family: str = ""
    source_action: str = ""
    source_slider_names: list[str] = field(default_factory=list)
    source_zap_names: list[str] = field(default_factory=list)
    source_gen_weights: bool = True
    source_cloth_data: int = 0  # BSClothExtraData blocks in the source ShapeData NIF
    # 0.6.0: the body route this plan belongs to (RouteDecision.to_dict(); empty in older plans)
    route: dict = field(default_factory=dict)
    # --- reference
    reference_template: str = ""
    reference_osp_rel: str = ""
    reference_set: str = ""
    reference_shape: str = ""
    reference_source: str = ""
    reference_slider_names: list[str] = field(default_factory=list)
    # --- operation
    shapes_to_process: list[str] = field(default_factory=list)
    untouched_shapes: list[str] = field(default_factory=list)
    # Phase 3.1: "outfit_only" (Phase 2/3, MODE A) | "embedded_body_replacement" (MODE B)
    conversion_mode: str = "outfit_only"
    replaced_body_shapes: list[str] = field(default_factory=list)  # MODE B: the embedded body (kept, re-weighted)
    body_identities: list[dict] = field(default_factory=list)  # identity of every embedded body shape
    body_replacement: dict = field(default_factory=dict)  # MODE B: source/target body, method, mappings, expectations
    shape_plans: list[ShapePlan] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    physics_bones: list[str] = field(default_factory=list)  # bones the conversion is allowed to add
    weight_mode: str = "full"  # "full" = Copy Bone Weights for all reference bones; "regions" = BoneList
    weight_mode_reason: str = ""
    bone_list: list[str] = field(default_factory=list)  # only for mode "regions"
    search_radius: float = 5.0
    max_vertex_targets: int = 4
    no_target_limit: bool = False
    auto_copy_ref: bool = False
    os_run_mode: str = "headless"  # "headless" (OutfitStudio -a) | "interactive" (window open, user presses Execute)
    os_run_mode_reason: str = ""
    # --- output
    output_project: str = ""
    output_osp_rel: str = ""
    output_shapedata_folder: str = ""
    output_shapedata_file: str = ""
    output_game_path: str = ""
    output_game_file: str = ""
    output_mod_name: str = ""
    output_mod_path: str = ""
    files_to_write: list[str] = field(default_factory=list)  # relative to the output mod
    same_output_projects: list[str] = field(default_factory=list)
    # --- execution
    staged_files: list[StagedFile] = field(default_factory=list)
    script_name: str = ""
    script_xml: str = ""
    script_summary: list[str] = field(default_factory=list)
    runtime: dict = field(default_factory=dict)
    preset: str = ""
    build_morphs: bool = True
    presets_available: list[str] = field(default_factory=list)
    bodyslide_build_args: str = ""  # arguments for a BodySlide group build into the output mod (run through MO2)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def can_convert(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ConversionPlan":
        """Rebuild a plan from ``plan.json`` (unknown keys are ignored)."""
        from dataclasses import fields

        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known}
        kw["staged_files"] = [StagedFile(**{k: v for k, v in sf.items() if k in {f.name for f in fields(StagedFile)}})
                              for sf in d.get("staged_files", [])]
        kw["shape_plans"] = [ShapePlan(**{k: v for k, v in sp.items() if k in {f.name for f in fields(ShapePlan)}})
                             for sp in d.get("shape_plans", [])]
        return cls(**kw)

    def preview_lines(self) -> list[str]:
        L = [
            f"Source mod:            {self.source_mod} ({self.source_provider_kind})",
            f"Source project:        {self.source_project}   [{self.source_osp_rel}, set #{self.source_set_index}]",
            f"Source body family:    {self.source_body_family}   (analyzer: {self.source_action})",
            f"Target reference:      {self.reference_template}  →  {self.reference_osp_rel} / {self.reference_set} / {self.reference_shape}",
            f"Shapes to process:     {', '.join(self.shapes_to_process) or '—'}",
        ]
        if self.untouched_shapes:
            L.append(f"Shapes left unchanged: {', '.join(self.untouched_shapes)}")
        if self.conversion_mode == "embedded_body_replacement":
            br = self.body_replacement
            L.append(f"BODY REPLACEMENT:      {br.get('source_body_shape')} ({br.get('source_body_classification')}, "
                     f"{br.get('source_body_confidence')}) → {br.get('target_body_shape')} [{br.get('target_body_template')}]")
        L += [
            f"Physics regions:       {', '.join(r.lower() for r in self.regions) or '—'}",
            f"Physics bones allowed: {', '.join(self.physics_bones) or '—'}",
            f"Weight copy mode:      {self.weight_mode}  ({self.weight_mode_reason})",
            f"Search radius:         {self.search_radius:g}",
            f"Max vertex targets:    {'no limit' if self.no_target_limit else self.max_vertex_targets}",
            "AutoCopyRef:           false (the reference body is NOT copied into the output)",
            f"Output project name:   {self.output_project}",
            f"Output project file:   {self.output_osp_rel}",
            f"Output ShapeData:      ShapeData/{self.output_shapedata_folder}/{self.output_shapedata_file}",
            f"Game mesh (preserved): {self.output_game_path}\\{self.output_game_file}   (GenWeights={str(self.source_gen_weights).lower()})",
            f"Output mod:            {self.output_mod_path}",
            f"Outfit Studio run:     {self.os_run_mode} ({self.os_run_mode_reason})",
            f"BodySlide build:       preset '{self.preset or '—'}', Build Morphs {'ON' if self.build_morphs else 'OFF'} (manual step after conversion)",
            f"  via MO2 executable:  BodySlide x64.exe {self.bodyslide_build_args}",
        ]
        rt = self.runtime or {}
        L.append(f"Outfit Studio:         {rt.get('version') or '—'} at {rt.get('path') or '—'}  headless: {rt.get('headless', 'NO')}")
        if self.warnings:
            L.append("Warnings:")
            L += [f"  ! {w}" for w in self.warnings]
        if self.blockers:
            L.append("BLOCKED:")
            L += [f"  ✗ {b}" for b in self.blockers]
        return L


def generated_names(source_project: str, suffix: str = " - 3BBB AUTO") -> dict[str, str]:
    """Names for the generated project (unique, never equal to the source's)."""
    display = f"{source_project}{suffix}"
    file_base = _FORBIDDEN.sub("_", display).strip().rstrip(".")
    return {
        "display": display,
        "osp_rel": f"SliderSets/{file_base}.osp",
        "shapedata_folder": file_base,
        "shapedata_file": f"{file_base}.nif",
        "osd_file": f"{file_base}.osd",
    }


def default_output_mod(inst: Installation) -> Optional[Path]:
    return (inst.mods_dir / DEFAULT_OUTPUT_MOD) if inst.mods_dir else None


def data_folders_for(ss: SliderSet, target: str, local: bool) -> list[str]:
    """Folders BodySlide searches for a slider data file (``SliderSet::ResolveSliderDataFile``):
    local data lives in the set's DataFolder; other data in the target shape's DataFolder
    attribute (a ';'-separated list), falling back to the set's DataFolder."""
    if local:
        return [ss.data_folder]
    e = next((x for x in ss.shapes if x.target == target), None)
    if e is not None and e.data_folder:
        folders = [f for f in e.data_folder.replace("\\", "/").split(";") if f.strip()]
        if folders:
            return folders
    return [ss.data_folder]


def _osd_refs(ss: SliderSet) -> set[tuple[tuple[str, ...], str]]:
    """(candidate data folders, osd file name) pairs referenced by a slider set's slider data."""
    out = set()
    for sl in ss.sliders:
        for d in sl.data:
            f = d.data_file.replace("\\", "/")
            osd = f.split("/", 1)[0] if "/" in f else f
            if not osd.lower().endswith(".osd"):
                continue
            out.add((tuple(data_folders_for(ss, d.target, bool(getattr(d, "local", False)))), osd))
    return out


def _stage(vfs, role: str, rel: str) -> Optional[StagedFile]:
    vf: Optional[VirtualFile] = vfs.resolve(rel)
    if vf is None:
        return None
    return StagedFile(role=role, rel=vf.rel, abs_path=str(vf.abs_path), provider=vf.provider.name, size=vf.size, mtime=vf.mtime)


def _predicted_physics_bones(kb: KnowledgeBase, shape_vertices: np.ndarray, radius: float, k: int, no_limit: bool) -> list[str]:
    ref = kb.reference
    pred = ref.predict_weights(shape_vertices, ref.physics_bones, radius, k, no_limit)
    return [b for j, b in enumerate(ref.physics_bones) if (pred[:, j] > WEIGHT_ADDED_EPS).any()]


def _vfs_resolver(vfs):
    def resolve(folder: str, osd_file: str) -> Optional[Path]:
        vf = vfs.resolve(f"ShapeData/{folder}/{osd_file}")
        return Path(vf.abs_path) if vf is not None else None
    return resolve


def _plan_body_replacement(plan: "ConversionPlan", inst, kb, ss: SliderSet, nif, ref_ss: SliderSet, rn,
                           bodies: list, allow: bool) -> None:
    """Identify the embedded body shapes and, when allowed and safe, plan MODE B (see ``body.py``)."""
    from ..analysis.shape_metrics import BODY_NAME_PATTERNS
    from .body import BODY_COMPATIBLE, IDENTITY_RADIUS, REPLACEMENT_METHOD, geometry_signature, identify_body, target_morphs

    W = plan.warnings
    if rn is None:
        return
    try:
        ref_nif = read_nif(Path(rn.abs_path))
    except NifError as exc:
        W.append(f"reference body cannot be read for body identification: {exc}")
        return
    ref_entry = next((e for e in ref_ss.shapes if e.name == plan.reference_shape), None)
    ref_shape = ref_nif.shape(plan.reference_shape)
    if ref_shape is None or ref_entry is None:
        W.append("reference body shape not found for body identification")
        return
    resolve = _vfs_resolver(inst.vfs)
    ref_morphs = target_morphs(ref_ss, ref_entry.target, ref_shape.num_vertices, resolve)
    entries = {e.name: e for e in ss.shapes}
    ids = []
    for sa in bodies:
        shp = nif.shape(sa.name)
        e = entries.get(sa.name)
        if shp is None or e is None:
            continue
        hint = bool(BODY_NAME_PATTERNS.match(sa.name.strip())) or bool(e.data_folder)
        bi = identify_body(shp, e.target, ss, target_morphs(ss, e.target, shp.num_vertices, resolve), ref_shape, ref_morphs,
                           set(kb.physics_bones), name_hint=hint)
        ids.append(bi)
    plan.body_identities = [bi.to_dict() for bi in ids]
    if not allow:
        return
    compatible = [bi for bi in ids if bi.classification == BODY_COMPATIBLE]
    if len(ids) != 1 or len(compatible) != 1:
        return  # several bodies or not positively identified: stays REVIEW (see package.py)
    bi = compatible[0]
    # slider data of the body: re-import every .osd file its data comes from as project-local data
    imports = []
    for sl in ss.sliders:
        for d in sl.data:
            if d.target != bi.target:
                continue
            f = d.data_file.replace("\\", "/")
            if "/" not in f:
                continue
            osd = f.split("/", 1)[0]
            for fo in data_folders_for(ss, d.target, bool(getattr(d, "local", False))):
                if resolve(fo, osd) is not None:
                    rel = f"ShapeData/{fo}/{osd}"
                    if rel not in imports:
                        imports.append(rel)
                    break
    staged_rels = {x.rel.lower() for x in plan.staged_files}
    missing = [r for r in imports if r.lower() not in staged_rels]
    if missing:
        W.append(f"body slider data files are not staged: {', '.join(missing)}")
        return
    plan.conversion_mode = "embedded_body_replacement"
    plan.replaced_body_shapes = [bi.shape]
    if bi.shape in plan.untouched_shapes:
        plan.untouched_shapes.remove(bi.shape)
    plan.body_replacement = {
        "mode": "embedded_body_replacement",
        "source_body_shape": bi.shape,
        "source_body_target": bi.target,
        "source_body_classification": bi.classification,
        "source_body_confidence": bi.confidence,
        "source_body_evidence": list(bi.evidence),
        "source_body_reasons": list(bi.reasons),
        "source_body_vertices": bi.vertices,
        "source_body_geometry_signature": bi.geometry_signature,
        "source_body_reference_coverage": bi.reference_coverage,
        "target_body_template": plan.reference_template,
        "target_body_project": plan.reference_osp_rel,
        "target_body_set": plan.reference_set,
        "target_body_shape": plan.reference_shape,
        "target_body_source": rn.rel,
        "target_body_sha256": sha256_file(Path(rn.abs_path)),
        "target_body_vertices": ref_shape.num_vertices,
        "target_body_geometry_signature": geometry_signature(ref_shape.world_vertices(), ref_shape.triangles),
        "replacement_method": REPLACEMENT_METHOD,
        "method_details": (
            f"the body shape '{bi.shape}' keeps its geometry ({bi.vertices} vertices, {bi.reference_coverage:.0%} of the "
            "reference body; vertices the author deleted stay deleted), triangles and slider/zap data. Its weights become "
            f"the CBBE-3BBB reference weights by vertex identity: CopyBoneWeights with radius {IDENTITY_RADIUS:g} and one "
            "target, so each vertex takes the weights of the reference vertex it coincides with (no interpolation; not "
            "the clothing transfer). The body is not a clothing CopyBoneWeights target. AutoCopyRef stays false"),
        "slider_mapping": (f"body morphs kept from the source ({bi.morph_sliders_compared} sliders, identical to the "
                           f"reference morphs within {bi.morph_max_delta:.4f}); re-imported as project-local data from "
                           + ", ".join(imports)),
        "slider_data_imports": imports,
        "zap_mapping": ("zaps with body data kept unchanged (same vertex indices): " + ", ".join(bi.zaps_on_body)
                        if bi.zaps_on_body else "the project has no zaps on the body"),
        "weight_changes": bi.weight_changes,
        "validation_expectations": [
            "exactly one body shape in the output: the planned body, no CBBE-3BBB duplicate, no other body",
            "body geometry and triangles unchanged from the source body",
            "every body vertex coincides with the installed CBBE-3BBB reference",
            "body weights identical to the CBBE-3BBB reference (half-float precision), no legacy physics bones",
            "the reference's 3BBB physics bones present on the body",
            "body slider morphs identical to the source and resolvable as project-local data",
            "target reference file unchanged since the plan (SHA-256)",
        ],
    }
    W.append(f"MODE B (embedded body replacement): the generated output intentionally contains the CBBE-3BBB body "
             f"(the embedded '{bi.shape}' re-weighted to the '{plan.reference_shape}' reference)")


def region_bones(ref, regions: list[str]) -> list[str]:
    """Physics bones of the installed reference for the given regions (reference order)."""
    return [b for r in regions for b in ref.physics_regions.get(r, [])]


_CONF_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0, "NONE": 0}


def shape_region_decision(sa, planned: list[str], sp: ShapePlan, radius: float) -> tuple[dict, str, list[str]]:
    """Per-region diagnostics, the shape's confidence and the reasons for its region decision."""
    details: dict = {}
    reasons: list[str] = []
    fits = []
    for r, m in sa.regions.items():
        if m.relevance == "NONE" and not m.predicted_vertices:
            continue
        details[r] = {
            "planned": r in planned,
            "relevance": m.relevance,
            "fit": m.fit,
            "predicted_vertices": m.predicted_vertices,
            "predicted_fraction": m.predicted_fraction,
            "reference_coverage": m.reference_coverage,
            "footprint_vertices": m.footprint_vertices,
            "within_radius": m.footprint_within_radius,
            "median_distance": m.footprint_median_distance,
            "p90_distance": m.footprint_p90_distance,
            "predicted_lr_balance": m.predicted_lr_balance,
            "side_coverage": dict(m.side_coverage),
            "predicted_cross_side_fraction": m.predicted_cross_side_fraction,
        }
        if r in planned:
            fits.append(m.fit)
            reasons.append(
                f"{r}: relevance {m.relevance}, fit {m.fit}, {m.predicted_vertices} vertices would receive "
                f"{r.lower()} weight, covers {m.reference_coverage:.0%} of the reference region, median distance "
                f"{m.footprint_median_distance:.2f} ({m.footprint_within_radius:.0%} within radius {radius:g})")
        else:
            reasons.append(f"{r}: not planned (relevance {m.relevance}, only {m.predicted_vertices} vertices)")
    conf = min(fits, key=lambda f: _CONF_ORDER.get(f, 0)) if fits else "UNKNOWN"
    if conf == "UNKNOWN" and fits:
        conf = "MEDIUM"
    if sp.copy_mode == "regions" and _CONF_ORDER.get(conf, 0) > _CONF_ORDER["MEDIUM"]:
        conf = "MEDIUM"
        reasons.append("region-restricted (BoneList) copy: a full copy would add " + ", ".join(sp.bones_outside_regions)
                       + "; this mode has not been validated in game yet")
    return details, conf, reasons


def build_script(plan: ConversionPlan) -> AutomationScript:
    """The Outfit Studio automation that performs the conversion.

    Step order reproduces the manual workflow that was validated in game:
    load the reference body, add the outfit project, copy bone weights onto the
    outfit shapes, and save a new project without the reference.
    """
    src_osp = plan.source_osp_rel if plan.source_osp_rel.lower().startswith("slidersets/") else f"SliderSets/{Path(plan.source_osp_rel).name}"
    steps = [
        AutomationStep("ClearProject", note="start from an empty project"),
        AutomationStep(
            "LoadReference",
            {"SourceFile": plan.reference_osp_rel, "Set": plan.reference_set, "Shape": plan.reference_shape,
             "LoadAll": False, "MergeSliders": True, "MergeZaps": True, "AppendNewSliders": False},
            note=f"reference: {plan.reference_template}",
        ),
        AutomationStep(
            "AddProject",
            {"SourceFile": src_osp, "Set": plan.source_project, "AppendNewSliders": True},
            note="the original outfit project (a staged copy; the source is never opened for writing)",
        ),
    ]
    extra = [n for n in _reference_only_sliders(plan)]
    if extra:
        steps.append(AutomationStep("DeleteSlider", {"SliderName": extra, "Regex": False},
                                    note="remove sliders that only the reference body has (keep the original slider list)"))
    # one CopyBoneWeights step per group of shapes that share copy mode and bone list
    # (a project whose shapes all use the same settings gets exactly one step, as in 0.2.x)
    # (and radius: a shape whose radius was raised to reach otherwise unweighted vertices gets its own step)
    groups: list[tuple[str, tuple[str, ...], float, list[str]]] = []
    by_name = {sp.name: sp for sp in plan.shape_plans}
    for name in plan.shapes_to_process:
        sp = by_name.get(name)
        mode = shape_copy_mode(sp, plan) if sp else plan.weight_mode
        bl = tuple(shape_bone_list(sp, plan)) if sp else tuple(plan.bone_list)
        key = (mode, bl if mode == "regions" else (), shape_radius(sp, plan))
        for g in groups:
            if (g[0], g[1], g[2]) == key:
                g[3].append(name)
                break
        else:
            groups.append((key[0], key[1], key[2], [name]))
    for mode, bl, radius, names in groups:
        cbw = {"ProximityRadius": float(radius),
               "MaxResults": NO_TARGET_LIMIT if plan.no_target_limit else int(plan.max_vertex_targets)}
        if mode == "regions":
            cbw["BoneList"] = list(bl)
        steps.append(AutomationStep("CopyBoneWeights", cbw, target_meshes=names,
                                    note=f"copy bone weights from the reference ({mode} mode)"
                                    + (f"; radius {radius:g} so that every vertex is reached" if radius != float(plan.search_radius) else "")))
    br = plan.body_replacement if plan.conversion_mode == "embedded_body_replacement" else {}
    if br:
        from .body import IDENTITY_RADIUS

        steps.append(AutomationStep("CopyBoneWeights", {"ProximityRadius": IDENTITY_RADIUS, "MaxResults": 1},
                                    target_meshes=[br["source_body_shape"]],
                                    note="body replacement: exact CBBE-3BBB reference weights by vertex identity "
                                         "(one coinciding target; not the clothing transfer)"))
        for rel in br.get("slider_data_imports", []):
            steps.append(AutomationStep("ImportSliderData", {"SliderDataFile": rel, "FromFolder": False, "MergeSliders": True},
                                        note="body replacement: keep the body's slider morphs as project-local data"))
    steps.append(AutomationStep(
        "SaveProject",
        {
            "DisplayName": plan.output_project,
            "SliderSetFile": plan.output_osp_rel,
            "ShapeDataFolder": plan.output_shapedata_folder,
            "ShapeDataFile": plan.output_shapedata_file,
            "OutputDataPath": plan.output_game_path,
            "OutputFileName": plan.output_game_file,
            "GenWeights": plan.source_gen_weights,
            "AutoCopyRef": False,
            "CopyRefFromProject": False,
        },
        note="save a NEW project; AutoCopyRef=false keeps the reference body out of the output",
    ))
    sc = AutomationScript(name=plan.script_name or "cbbe3bbb_conversion", steps=steps)
    check_invariants(sc)
    return sc


def _reference_only_sliders(plan: ConversionPlan) -> list[str]:
    ref_sliders = plan.reference_slider_names
    src = set(plan.source_slider_names) | set(plan.source_zap_names)
    return [s for s in ref_sliders if s not in src]


def build_plan(
    inst: Installation,
    kb: KnowledgeBase,
    pr: ProjectResult,
    runtime: RuntimeInfo,
    *,
    output_mod_path: Optional[Path] = None,
    library: Optional[list[ProjectResult]] = None,
    created: str = "",
    milestone_gate: bool = True,
    allow_body_replacement: bool = False,
    attempt_review: bool = False,
    acceptance: Optional[AcceptanceIsolation] = None,
) -> ConversionPlan:
    """Plan the conversion of one analyzed project. Reads files, writes nothing.

    ``attempt_review`` (Phase 3.2, Convert All Possible only): plan an analyzer-REVIEW project that
    :func:`cbbe3bbb.conversion.outcomes.review_policy` found attemptable, instead of refusing it. Every other blocker
    and every validation check still applies; the result is committed only if it validates."""
    s = inst.profile.conversion
    plan = ConversionPlan(created=created)
    plan.source_mod = pr.mod
    plan.source_provider_kind = pr.provider_kind
    plan.source_project = pr.project
    plan.source_osp_rel = pr.osp_rel
    plan.source_osp_path = pr.osp_path
    plan.source_set_index = pr.set_index
    plan.source_shapedata_rel = pr.shapedata_rel
    plan.source_body_family = pr.family.label
    plan.source_action = pr.action
    plan.search_radius = s.search_radius
    plan.max_vertex_targets = s.max_vertex_targets
    plan.no_target_limit = s.no_target_limit
    plan.auto_copy_ref = False
    plan.preset = s.preset
    plan.build_morphs = s.build_morphs
    plan.presets_available = sorted({p.name for p in inst.presets})
    plan.runtime = runtime.to_dict()
    mode = (s.os_run_mode or "auto").lower()
    if mode == "auto":
        mode = runtime.recommended_run_mode or "headless"
        plan.os_run_mode_reason = (
            "chosen automatically: headless CopyBoneWeights is a no-op in this Outfit Studio version"
            if mode == "interactive" else "chosen automatically"
        )
    else:
        plan.os_run_mode_reason = "set in the profile"
        if mode == "headless" and runtime.headless_weight_copy_ok is False:
            plan.warnings.append(
                "headless mode is forced although CopyBoneWeights does nothing headless in this Outfit Studio version; "
                "the validation will fail"
            )
    plan.os_run_mode = mode
    plan.same_output_projects = list(pr.same_output_projects)
    B = plan.blockers
    W = plan.warnings

    # --- milestone gate / analyzer result -------------------------------------------
    allowed = [a for a in (s.allowed_projects or []) if a]
    if milestone_gate and allowed and pr.project not in allowed:
        B.append(f"conversion is enabled only for {', '.join(allowed)} in this milestone")
    if pr.action != "CONVERT" and not (attempt_review and pr.action == "REVIEW"):
        B.append(f"the analyzer classifies this project as {pr.action}, not CONVERT")
    elif pr.action == "REVIEW":
        W.append("Convert All Possible: the analyzer classified this project REVIEW ("
                 + "; ".join(pr.reasons[1:3] or pr.reasons[:1]) + "); it is attempted and kept only if it validates")
    if s.copy_reference_shape_into_output:
        B.append("'copy reference shape into output' is on in the profile; conversions require it to be off")
    if not runtime.headless_supported:
        B.append(f"Outfit Studio headless automation unavailable: {runtime.reason}")
    ref = kb.reference if kb else None
    if ref is None or inst.reference_template is None:
        B.append("CBBE 3BBB reference not found")
        return plan

    # --- reference ---------------------------------------------------------------------
    tpl = inst.reference_template
    plan.reference_template = tpl.display_name
    plan.reference_set = tpl.set_name
    plan.reference_shape = tpl.shape or ref.shape_name
    plan.reference_source = ref.source
    vfs = inst.vfs
    ref_osp = _stage(vfs, "reference-osp", tpl.source_file)
    if ref_osp is None:
        B.append(f"reference project file not found: {tpl.source_file}")
        return plan
    plan.reference_osp_rel = ref_osp.rel
    try:
        ref_ss = next(x for x in parse_osp(Path(ref_osp.abs_path)).sets if x.name == tpl.set_name)
    except (StopIteration, OspParseError) as exc:
        B.append(f"reference slider set '{tpl.set_name}' cannot be read: {exc}")
        return plan
    staged = [ref_osp]
    rn = _stage(vfs, "reference-nif", f"ShapeData/{ref_ss.data_folder}/{ref_ss.source_file}")
    if rn is None:
        B.append("reference ShapeData NIF not found")
    else:
        staged.append(rn)
    for folders, osd in sorted(_osd_refs(ref_ss)):
        f = next((x for x in (_stage(vfs, "reference-osd", f"ShapeData/{fo}/{osd}") for fo in folders) if x), None)
        if f is None:
            W.append(f"reference slider data not found: ShapeData/{folders[0]}/{osd} (reference sliders without data)")
        else:
            staged.append(f)

    # --- source ------------------------------------------------------------------------
    try:
        src_file = parse_osp(Path(pr.osp_path))
        ss = src_file.sets[pr.set_index]
        if ss.name != pr.project:
            raise IndexError
    except (OspParseError, IndexError, OSError) as exc:
        B.append(f"source project cannot be read again: {exc or 'set changed since the analysis'}")
        return plan
    so = _stage(vfs, "source-osp", pr.osp_rel)
    if so is None or Path(so.abs_path) != Path(pr.osp_path):
        B.append("source project file is no longer the MO2 winner (re-run the analysis)")
        return plan
    staged.append(so)
    sn = _stage(vfs, "source-nif", pr.shapedata_rel)
    if sn is None:
        B.append(f"source ShapeData NIF not found: {pr.shapedata_rel}")
        return plan
    staged.append(sn)
    for folders, osd in sorted(_osd_refs(ss)):
        f = next((x for x in (_stage(vfs, "source-osd", f"ShapeData/{fo}/{osd}") for fo in folders) if x), None)
        if f is None:
            B.append(f"source slider data not found: ShapeData/{folders[0]}/{osd}")
        else:
            staged.append(f)
    # de-duplicate (same file referenced twice)
    seen = set()
    plan.staged_files = [x for x in staged if not (x.rel.lower() in seen or seen.add(x.rel.lower()))]
    plan.source_slider_names = [x.name for x in ss.sliders if not x.zap]
    plan.source_zap_names = list(ss.zap_sliders)
    plan.source_gen_weights = ss.gen_weights
    plan.output_game_path = ss.output_path
    plan.output_game_file = ss.output_file
    plan.reference_slider_names = [x.name for x in ref_ss.sliders]
    if not ss.output_file:
        B.append("source project has no output file name")

    # --- shapes and regions (from the Phase 1 analysis) ----------------------------------------
    try:
        nif = read_nif(Path(sn.abs_path))
    except NifError as exc:
        B.append(f"cannot read source NIF: {exc}")
        return plan
    plan.source_cloth_data = len(nif.cloth_data)
    if nif.cloth_data:
        W.append(
            f"the source ShapeData NIF contains cloth physics data (BSClothExtraData, {len(nif.cloth_data)} block(s)). "
            "Outfit Studio asks 'Choose cloth data' when saving the new project; only this project's own ShapeData file "
            f"({Path(pr.shapedata_rel).name}) may be kept, so the output carries exactly the original cloth data")
        if plan.os_run_mode == "headless":
            B.append("cloth physics data needs the interactive 'Choose cloth data' answer; Outfit Studio's save dialog "
                     "would block a headless run (use run mode interactive)")
    body_names = {x.name for x in pr.shapes if x.is_body}
    listed = [e.name for e in ss.shapes]
    regions_all: set[str] = set()
    wmode = (s.weight_mode or "auto").lower()
    for sa in pr.shapes:
        if sa.name not in listed:
            continue
        if sa.is_body:
            plan.untouched_shapes.append(sa.name)
            continue
        regs = [r for r, m in sa.regions.items() if Level(m.relevance).rank >= Level.MEDIUM.rank] if sa.regions else []
        if not regs:
            plan.untouched_shapes.append(sa.name)
            continue
        shp = nif.shape(sa.name)
        if shp is None:
            B.append(f"shape '{sa.name}' is missing from the source NIF")
            continue
        regs = [r for r in ref.physics_regions if r in regs]  # the reference's region order
        reach = plan_shape_reach(ref, shp.world_vertices(), s.search_radius, s.max_vertex_targets, s.no_target_limit)
        r_shape = reach["radius"]
        pred = _predicted_physics_bones(kb, shp.world_vertices(), r_shape, s.max_vertex_targets, s.no_target_limit)
        allowed_bones = region_bones(ref, regs)
        outside = [b for b in pred if b not in allowed_bones]
        sp = ShapePlan(sa.name, sa.vertices, sorted(regs), pred, outside, allowed_bones=allowed_bones)
        if reach["far"]:
            sp.out_of_reach_vertices = reach["far"]
            sp.out_of_reach_distance = round(reach["distance"], 3)
            idx = ", ".join(str(i) for i in reach["far"][:12]) + (" …" if len(reach["far"]) > 12 else "")
            if reach["ok"]:
                sp.search_radius = r_shape
                W.append(f"{sa.name}: {len(reach['far'])} vertices ({idx}) have no reference vertex within radius "
                         f"{s.search_radius:g} (nearest {reach['distance']:.2f}); Outfit Studio would leave them without "
                         f"weights. This shape uses radius {r_shape:g}; the emulated copy changes only those vertices")
            else:
                B.append(f"{sa.name}: {len(reach['far'])} vertices ({idx}) have no reference vertex within radius "
                         f"{s.search_radius:g} (nearest {reach['distance']:.2f}) and would be left without weights; "
                         f"no safe radius: {reach['reason']}")
        if wmode == "full" or (wmode == "auto" and not outside):
            sp.copy_mode = "full"
        else:
            sp.copy_mode = "regions"
            legacy = sorted({b for r in regs for b in LEGACY_BONES_FOR_REGION.get(r, [])})
            sp.bone_list = list(allowed_bones) + legacy
        sp.region_details, sp.confidence, sp.reasons = shape_region_decision(sa, regs, sp, r_shape)
        if sp.search_radius:
            sp.reasons.append(f"radius {r_shape:g} instead of {s.search_radius:g} so that vertices "
                              f"{', '.join(map(str, sp.out_of_reach_vertices[:12]))} keep weights (no other vertex changes)")
        plan.shape_plans.append(sp)
        plan.shapes_to_process.append(sa.name)
        regions_all.update(regs)
    listed_bodies = [x for x in pr.shapes if x.is_body and x.name in listed]
    if listed_bodies:
        _plan_body_replacement(plan, inst, kb, ss, nif, ref_ss, rn, listed_bodies, allow_body_replacement)
    for b in body_names:
        if b in listed and b not in plan.replaced_body_shapes:
            W.append(f"the original project embeds body shape '{b}'; it is kept unchanged (no 3BBB weights) "
                     "and is not a weight-copy target")
    if not plan.shapes_to_process:
        B.append("no outfit shape lies over a breast/butt/thigh physics region")
    order = ["BREAST", "BUTT", "THIGH"] + [r for r in ref.physics_regions if r not in ("BREAST", "BUTT", "THIGH")]
    plan.regions = [r for r in order if r in regions_all]
    plan.physics_bones = [b for r in plan.regions for b in ref.physics_regions.get(r, [])]

    # --- weight mode (per shape; summarised for the project) ------------------------------------
    modes = {sp.copy_mode for sp in plan.shape_plans}
    outside_any = sorted({b for sp in plan.shape_plans for b in sp.bones_outside_regions})
    if modes <= {"full"}:
        plan.weight_mode = "full"
        plan.weight_mode_reason = (
            "Copy Bone Weights for all reference bones, as in the manually validated conversion; "
            + ("predicted to add only bones of the planned regions" if not outside_any else
               f"WARNING: also adds {', '.join(outside_any)}")
        )
        if outside_any:
            W.append(f"full copy would add physics bones outside the planned regions: {', '.join(outside_any)}")
    else:
        plan.weight_mode = "regions" if modes == {"regions"} else "mixed"
        plan.bone_list = sorted({b for sp in plan.shape_plans if sp.copy_mode == "regions" for b in sp.bone_list})
        restricted = [sp.name for sp in plan.shape_plans if sp.copy_mode == "regions"]
        plan.weight_mode_reason = (
            "BoneList copy restricted to each shape's planned physics bones for "
            + ", ".join(restricted)
            + ("; chosen because a full copy would add " + ", ".join(outside_any) if outside_any else "")
        )
        W.append("region-restricted weight copy has not been validated in game yet; other bones are rescaled, not re-copied")

    # --- output names, paths, collisions -------------------------------------------------------
    names = generated_names(pr.project, s.project_suffix)
    plan.output_project = names["display"]
    plan.output_osp_rel = names["osp_rel"]
    plan.output_shapedata_folder = names["shapedata_folder"]
    plan.output_shapedata_file = names["shapedata_file"]
    plan.files_to_write = [
        "Tools/BodySlide/" + names["osp_rel"],
        f"Tools/BodySlide/ShapeData/{names['shapedata_folder']}/{names['shapedata_file']}",
        "Tools/BodySlide/SliderGroups/CBBE 3BBB Auto Conversions.xml (created or updated)",
        "manifest.json",
    ]
    plan.script_name = safe_script_name("cbbe3bbb_" + names["shapedata_folder"])
    out_mod = Path(output_mod_path) if output_mod_path else (Path(s.output_mod_path) if s.output_mod_path else default_output_mod(inst))
    if out_mod is None:
        B.append("no output mod folder (set it in the profile or configure MO2)")
    else:
        plan.output_mod_path = str(out_mod)
        plan.output_mod_name = out_mod.name
        B.extend(check_output_mod(out_mod, inst, plan.files_to_write[:2]))
    if vfs.resolve(names["osp_rel"]) is not None:
        vf = vfs.resolve(names["osp_rel"])
        if not out_mod or Path(vf.abs_path).resolve() != (out_mod / "Tools/BodySlide" / names["osp_rel"]).resolve():
            if acceptance is not None and acceptance.applies_to(pr) and vf.provider.name != pr.mod:
                acceptance.verify()  # 0.5.3 acceptance only: the existing generated project is not an input
                W.append(f"ACCEPTANCE: the existing generated {names['osp_rel']} in '{vf.provider.name}' is ignored "
                         "(not an input, not a collision: the output is disposable and never installed; left untouched)")
            else:
                B.append(f"a project file named {names['osp_rel']} already exists in '{vf.provider.name}'")
    if library:
        dup = [p for p in library if p.project == names["display"] and p.mod != (out_mod.name if out_mod else "")]
        if dup:
            B.append(f"a slider set named '{names['display']}' already exists in {dup[0].mod}")
        target = f"{ss.output_path}/{ss.output_file}".replace("\\", "/").lower()
        same = [f"{p.project} [{p.mod}]" for p in library
                if p.key != pr.key and f"{p.output_path}/{p.output_file}".replace("\\", "/").lower() == target]
        plan.same_output_projects = same
    W.append(
        f"the original project '{pr.project}' keeps building the same game mesh "
        f"({ss.output_path}\\{ss.output_file}); build only the '3BBB AUTO' project, or the last build wins"
    )
    for p in plan.same_output_projects:
        W.append(f"another BodySlide set also targets this output mesh: {p}")
    if pr.provider_kind == "disabled-mod":
        W.append("the source mod is disabled in the MO2 profile")

    if plan.output_mod_path:
        plan.bodyslide_build_args = (
            f'--groupbuild "CBBE 3BBB Auto Conversions" --targetdir "{plan.output_mod_path}" --preset "{plan.preset}"'
            + (" --trimorphs" if plan.build_morphs else "")
        )
    if not B:
        script = build_script(plan)
        plan.script_xml = script.to_xml()
        plan.script_summary = [st.summary() for st in script.steps]
    return plan


def check_output_mod(out_mod: Path, inst: Installation, planned: list[str]) -> list[str]:
    """Blockers for writing into ``out_mod``: it must be a tool-owned (or new, empty) folder."""
    problems = []
    out_mod = Path(out_mod)
    guard = [p for p in (inst.game_dir, inst.game_data_dir) if p]
    for g in guard:
        try:
            out_mod.resolve().relative_to(Path(g).resolve())
            problems.append(f"output mod {out_mod} is inside the game folder {g}")
        except (ValueError, OSError):
            pass
    source_mods = {p.root.resolve() for p in inst.providers if p.kind in ("mod", "disabled-mod", "overwrite", "game")}
    manifest = out_mod / "manifest.json"
    if out_mod.exists():
        entries = [e for e in out_mod.iterdir() if e.name.lower() != "meta.ini"]
        if entries and not manifest.is_file():
            problems.append(f"output folder {out_mod} exists and is not a CBBE3BBBConverter output mod (no manifest.json)")
        elif out_mod.resolve() in source_mods and not manifest.is_file():
            problems.append(f"output folder {out_mod} is a source mod")
        for rel in planned:
            if (out_mod / rel).exists():
                problems.append(f"{rel} already exists in the output mod (remove the earlier conversion first)")
    return problems
