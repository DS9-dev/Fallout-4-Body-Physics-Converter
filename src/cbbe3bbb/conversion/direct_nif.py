"""DIRECT_NIF: read-only analysis of packages that ship built game meshes but no BodySlide projects (Phase 4.0).

Many older Fallout 4 clothing packages (e.g. HN66 EasyGirl) contain a plugin, materials, textures and
built ``meshes/*.nif`` but no ``SliderSets/*.osp``. The BODYSLIDE_PROJECT pipeline cannot process them. This
module plans a conversion of such packages from the *game* side:

    package plugin(s) → armor addons (ARMA) → female third-person meshes actually used by the game
    → MO2 winner of each mesh → structural NIF analysis → sex / body family → embedded body identity
    → per-shape physics regions (the Phase 1 analyzer) → per-shape adaptive radius (0.3.3)
    → KEEP / CONVERT / SKIP / REVIEW / UNSUPPORTED / ERROR

It never writes anything except reports inside the tool folder. There is no conversion or commit here:
Phase 4.1 (a separately authorised write stage) will write converted meshes to the output mod at the SAME
game-relative path, so MO2 overrides the source without any plugin change.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Callable, Optional

import numpy as np

from .. import __version__
from ..analysis.models import Action, FamilyResult, Level, ProjectResult
from ..analysis.reference import LEGACY_PHYSICS_BONES, KnowledgeBase
from ..analysis.shape_metrics import BODY_NAME_PATTERNS, MetricSettings, analyze_shape
from ..bodyslide.osp import OspParseError, SliderSet, parse_osp
from ..classification.rules import ACCESSORY_WORDS, RIGID_WORDS, _hit, classify_project
from ..nif.reader import NifError, read_nif
from .body import (BODY_ALREADY_3BBB, BODY_AMBIGUOUS, BODY_COMPATIBLE, BODY_UNSUPPORTED, REPLACEMENT_METHOD,
                   identify_body, vertex_map)
from .gamedata import (MODEL_ROLES, PLUGIN_SUFFIXES, GameItem, normalize_mesh_path, read_plugin_armors,
                       resolve_data_file, slot_list)
from .plan import WEIGHT_ADDED_EPS, plan_shape_reach, region_bones

__all__ = [
    "SOURCE_BODYSLIDE_PROJECT", "SOURCE_DIRECT_NIF", "SOURCE_MIXED", "SOURCE_NONE",
    "SEX_FEMALE_CBBE", "SEX_FEMALE_3BBB", "SEX_FEMALE_NON_CBBE", "SEX_MALE", "SEX_AMBIGUOUS", "SEX_UNKNOWN",
    "DirectShape", "DirectMesh", "DirectNifReport", "plugin_mesh_refs", "analyze_direct_package",
    "detect_source_mode", "direct_report_text", "write_direct_reports",
]

SOURCE_BODYSLIDE_PROJECT = "BODYSLIDE_PROJECT"
SOURCE_DIRECT_NIF = "DIRECT_NIF"
SOURCE_MIXED = "MIXED"
SOURCE_NONE = "NO_CONVERTIBLE_SOURCE"

SEX_FEMALE_CBBE = "FEMALE_CBBE_COMPATIBLE"
SEX_FEMALE_3BBB = "FEMALE_ALREADY_3BBB"
SEX_FEMALE_NON_CBBE = "FEMALE_NON_CBBE"
SEX_MALE = "MALE"
SEX_AMBIGUOUS = "AMBIGUOUS"
SEX_UNKNOWN = "UNKNOWN"

SHAPE_OUTFIT = "OUTFIT"
SHAPE_RIGID = "RIGID_ACCESSORY"
SHAPE_UNKNOWN = "UNKNOWN"

FEMALE_THIRD = "MOD3"
MALE_THIRD = "MOD2"
SUPPORTED_BLOCKS = ("BSTriShape", "BSSubIndexTriShape", "BSMeshLODTriShape", "BSDynamicTriShape")
FIT_MEDIAN_MAX = 3.0  # clothing without a body: median distance to the CBBE reference to count as CBBE-fitted
RIGID_MAX_BONES = 4
RIGID_MAX_VERTICES = 1500


@dataclass
class DirectShape:
    name: str
    block_type: str
    vertices: int
    triangles: int
    kind: str = SHAPE_UNKNOWN  # OUTFIT / RIGID_ACCESSORY / BODY_* / UNKNOWN
    bones: int = 0
    weighted_bones: dict[str, int] = field(default_factory=dict)
    max_influences: int = 0
    weight_sum_min: float = 0.0
    weight_sum_max: float = 0.0
    unweighted_vertices: int = 0
    bounds_min: list[float] = field(default_factory=list)
    bounds_max: list[float] = field(default_factory=list)
    median_distance: Optional[float] = None
    within_radius: Optional[float] = None
    coincident_fraction: float = 0.0
    existing_3bbb: dict[str, int] = field(default_factory=dict)
    legacy_bones: dict[str, int] = field(default_factory=dict)
    regions: dict[str, dict] = field(default_factory=dict)
    decision: str = "NONE"  # CONVERT / NONE / BODY_REPLACE / KEEP
    planned_regions: list[str] = field(default_factory=list)
    planned_bones: list[str] = field(default_factory=list)
    planned_radius: Optional[float] = None
    out_of_reach_vertices: list[int] = field(default_factory=list)
    body_identity: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass
class DirectMesh:
    mesh: str  # game-relative, e.g. meshes\HN66FO4\EasyGirlOutfit\38_..._BS.nif
    items: list[dict] = field(default_factory=list)  # GameItem dicts (every item/slot that uses this mesh)
    roles: list[str] = field(default_factory=list)
    sex: str = SEX_UNKNOWN
    sex_confidence: str = Level.LOW.value
    sex_evidence: list[str] = field(default_factory=list)
    source_mod: str = ""
    exists: bool = False
    winner: dict = field(default_factory=dict)
    competitors: list[dict] = field(default_factory=list)
    sha256: str = ""
    size: int = 0
    shapes: list[DirectShape] = field(default_factory=list)
    cloth_data: int = 0
    unsupported_structures: list[str] = field(default_factory=list)
    embedded_body: str = "NONE"  # NONE / BODY_* classification
    body_slot: bool = False  # used by an addon on biped slot 33 (the body slot)
    existing_3bbb: str = "NO"
    action: str = Action.ERROR.value
    status: str = ""
    confidence: str = Level.UNKNOWN.value
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    planned_target: str = ""
    body_replacement: dict = field(default_factory=dict)

    @property
    def item_names(self) -> list[str]:
        return sorted({i.get("armor_name") or i.get("armor_editor_id") for i in self.items})


@dataclass
class DirectNifReport:
    package: str
    created: str = ""
    tool_version: str = __version__
    source_mode: str = SOURCE_NONE
    bodyslide_projects: int = 0
    provider_path: str = ""
    output_mod: str = ""
    allow_body_replacement: bool = False
    plugins: list[dict] = field(default_factory=list)
    female_items: int = 0
    unique_female_meshes: int = 0
    male_items: int = 0
    male_meshes_skipped: int = 0
    other_models: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)
    sex_counts: dict[str, int] = field(default_factory=dict)
    meshes: list[DirectMesh] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------------------------------------------
# plugin → meshes
# ---------------------------------------------------------------------------------------------------------------

def plugin_mesh_refs(pa, spelling: Optional[dict] = None) -> dict[str, list[GameItem]]:
    """normalized mesh path → every (item, addon, model slot) of the plugin that uses it.

    ``spelling`` (optional) receives normalized path → the plugin's own spelling of it."""
    out: dict[str, list[GameItem]] = defaultdict(list)
    for aid, addon in pa.addons.items():
        armors = [a for a in pa.armors.values() if aid in a.addons]
        for slot, path in addon.models.items():
            if not path:
                continue
            key = normalize_mesh_path(path)
            if spelling is not None:
                spelling.setdefault(key, "meshes\\" + path.replace("/", "\\").lstrip("\\"))
            for arm in armors or [None]:
                out[key].append(GameItem(plugin=pa.plugin, armor_editor_id=arm.editor_id if arm else "(no ARMO)",
                                         armor_form_id=f"{arm.form_id:08X}" if arm else "", armor_name=arm.name if arm else "",
                                         addon_editor_id=addon.editor_id, addon_form_id=f"{aid:08X}",
                                         role=MODEL_ROLES.get(slot, slot), biped_slots=slot_list(addon.biped_slots)))
    return out


def _active_plugins(inst) -> Optional[set[str]]:
    mo2 = getattr(inst, "mo2", None)
    if mo2 is None:
        return None
    p = Path(mo2.profiles_dir) / (inst.mo2_profile or mo2.selected_profile) / "plugins.txt"
    if not p.is_file():
        return None
    act = set()
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("*"):
            act.add(line[1:].strip().lower())
    return act


def detect_source_mode(n_projects: int, n_female_meshes_unowned: int) -> str:
    if n_projects and n_female_meshes_unowned:
        return SOURCE_MIXED
    if n_projects:
        return SOURCE_BODYSLIDE_PROJECT
    if n_female_meshes_unowned:
        return SOURCE_DIRECT_NIF
    return SOURCE_NONE


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _reference_shape(inst):
    tpl = getattr(inst, "reference_template", None)
    vfs = getattr(inst, "vfs", None)
    if tpl is None or vfs is None:
        return None
    osp = vfs.resolve(tpl.source_file)
    if osp is None:
        return None
    try:
        ss = next(x for x in parse_osp(Path(osp.abs_path)).sets if x.name == tpl.set_name)
    except (StopIteration, OspParseError):
        return None
    nf = vfs.resolve(f"ShapeData/{ss.data_folder}/{ss.source_file}")
    if nf is None:
        return None
    try:
        n = read_nif(Path(nf.abs_path))
    except (NifError, OSError):
        return None
    return n.shape(tpl.shape) or (n.shapes[0] if n.shapes else None)


# ---------------------------------------------------------------------------------------------------------------
# analysis of one female mesh
# ---------------------------------------------------------------------------------------------------------------

def _shape_structure(shp, ds: DirectShape) -> None:
    bones, w = shp.weight_matrix()
    ds.bones = len(bones)
    if len(w):
        cnt = (w > 0.001).sum(0)
        ds.weighted_bones = {b: int(c) for b, c in zip(bones, cnt) if c}
        infl = (w > 0.001).sum(1)
        ds.max_influences = int(infl.max()) if len(infl) else 0
        s = w.sum(1)
        ds.weight_sum_min, ds.weight_sum_max = round(float(s.min()), 4), round(float(s.max()), 4)
        ds.unweighted_vertices = int((s < 0.5).sum())
    v = shp.world_vertices()
    if len(v):
        ds.bounds_min = [round(float(x), 2) for x in v.min(0)]
        ds.bounds_max = [round(float(x), 2) for x in v.max(0)]


def _is_rigid(ds: DirectShape) -> bool:
    return ds.bones <= RIGID_MAX_BONES and ds.vertices <= RIGID_MAX_VERTICES


def analyze_mesh(dm: DirectMesh, path: Path, kb: KnowledgeBase, ref_shape, settings: MetricSettings,
                 allow_body_replacement: bool, tree_cache: dict) -> None:
    """Fill sex, shapes, action and plan of one female game mesh (the file is only read)."""
    R, W = dm.reasons, dm.warnings
    ref = kb.reference
    try:
        nif = read_nif(path)
    except (NifError, OSError, ValueError) as exc:
        dm.action, dm.status = Action.ERROR.value, "UNREADABLE_NIF"
        R.append(f"the NIF cannot be read: {exc}")
        return
    dm.cloth_data = len(nif.cloth_data)
    dm.unsupported_structures = [f"{t} '{n}'" for t, n in nif.unsupported_shapes]
    dm.unsupported_structures += [f"{s.block_type} '{s.name}'" for s in nif.shapes if s.block_type not in SUPPORTED_BLOCKS]
    if not nif.header.is_fallout4:
        dm.unsupported_structures.append(f"not a Fallout 4 NIF (version {nif.header.version_str})")
    # Outfit Studio's ImportNIF renames: the root node to 'Scene Root', duplicate shape names, and a shape whose name
    # is already taken in the project (observed: a shape 'Chest' next to the skeleton bone 'Chest' came back 'Chest_1')
    names = [s.name for s in nif.shapes]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        dm.unsupported_structures.append(f"duplicate shape names {dup[:3]} (Outfit Studio renames them on import)")
    taken = set(nif.node_names) | set(kb.skeleton_bones or ()) | set(kb.reference.bones if kb.reference else ())
    if ref_shape is not None:
        taken.add(ref_shape.name)
    clash = sorted(n for n in set(names) if n in taken)
    if clash:
        dm.unsupported_structures.append(f"shape name(s) {clash[:3]} equal a bone/node or reference name (Outfit Studio "
                                         "renames such a shape on import, e.g. 'Chest' → 'Chest_1')")
    if nif.node_names and nif.node_names[0] != "Scene Root":
        dm.warnings.append(f"root node '{nif.node_names[0]}' would be renamed 'Scene Root' by Outfit Studio's import")
    phys = kb.physics_bones
    analyses = []
    bodies = []
    for shp in nif.shapes:
        ds = DirectShape(name=shp.name, block_type=shp.block_type, vertices=shp.num_vertices, triangles=int(len(shp.triangles)))
        _shape_structure(shp, ds)
        ds.existing_3bbb = {b: c for b, c in ds.weighted_bones.items() if b in phys}
        ds.legacy_bones = {b: c for b, c in ds.weighted_bones.items() if b in LEGACY_PHYSICS_BONES}
        sa = analyze_shape(shp, None, kb, settings, 0, tree_cache)
        if ref_shape is not None and shp.num_vertices:
            m, _ = vertex_map(shp.world_vertices(), ref_shape.world_vertices())
            ds.coincident_fraction = round(float((m >= 0).mean()), 4)
        extent = float(np.ptp(shp.world_vertices()[:, 2])) if shp.num_vertices else 0.0
        named_body = bool(BODY_NAME_PATTERNS.match(shp.name.strip())) and shp.num_vertices >= 2000 and extent >= 40.0
        if sa.is_body or named_body or (ds.coincident_fraction >= 0.5 and shp.num_vertices >= 1000):
            sa.is_body = True
            bodies.append((shp, ds))
        else:
            ds.median_distance = sa.distance.median
            ds.within_radius = sa.distance.frac_within_radius
            ds.kind = SHAPE_RIGID if _is_rigid(ds) else SHAPE_OUTFIT
            for r, rm in sa.regions.items():
                if rm.relevance != Level.NONE.value or rm.predicted_vertices:
                    ds.regions[r] = {"relevance": rm.relevance, "fit": rm.fit, "coverage": rm.reference_coverage,
                                     "predicted_vertices": rm.predicted_vertices, "footprint_vertices": rm.footprint_vertices,
                                     "median_distance": rm.footprint_median_distance,
                                     "within_radius": rm.footprint_within_radius, "p90_distance": rm.footprint_p90_distance}
        analyses.append(sa)
        dm.shapes.append(ds)
    if not nif.shapes:
        dm.action, dm.status = Action.ERROR.value, "UNREADABLE_NIF"
        R.append("no readable shapes in the NIF")
        return

    # ---- embedded bodies (geometry identity; a direct NIF has no slider data) -------------------------------
    identities = []
    if bodies and ref_shape is not None:
        dummy = SliderSet(name=dm.mesh, data_folder="", source_file="", output_path="", output_file="")
        for shp, ds in bodies:
            bi = identify_body(shp, shp.name, dummy, {}, ref_shape, {}, phys, False)
            ds.kind = bi.classification
            ds.body_identity = {"classification": bi.classification, "confidence": bi.confidence,
                                "coincident_fraction": bi.coincident_fraction, "reference_coverage": bi.reference_coverage,
                                "triangle_subset": bi.triangle_subset, "max_distance": bi.max_distance,
                                "existing_physics_bones": bi.existing_physics_bones, "legacy_bones": bi.legacy_bones,
                                "evidence": bi.evidence, "reasons": bi.reasons, "weight_changes": bi.weight_changes}
            identities.append((shp, ds, bi))
        cl = sorted({bi.classification for _, _, bi in identities})
        dm.embedded_body = cl[0] if len(cl) == 1 else "MIXED"

    # ---- sex / body family ----------------------------------------------------------------------------------
    outfit = [(sa, ds) for sa, ds in zip(analyses, dm.shapes) if not sa.is_body]
    any_3bbb = any(ds.existing_3bbb for ds in dm.shapes)
    E = dm.sex_evidence
    E.append("referenced as the FEMALE third-person model (ARMA MOD3) of " + ", ".join(dm.item_names[:4])
             + (" …" if len(dm.item_names) > 4 else ""))
    if identities:
        classes = {bi.classification for _, _, bi in identities}
        E += [f"embedded body '{shp.name}': {bi.classification} ({bi.coincident_fraction:.1%} of its vertices on the "
              f"CBBE-3BBB reference)" for shp, _, bi in identities]
        male_named = [shp.name for shp, _, bi in identities if bi.classification == BODY_UNSUPPORTED
                      and "male" in shp.name.lower() and "female" not in shp.name.lower()]
        if male_named:
            dm.sex, dm.sex_confidence = SEX_MALE, Level.MEDIUM.value
            E.append(f"body shape named like a male body ({', '.join(male_named[:2])}) that is not the CBBE topology")
        elif classes == {BODY_UNSUPPORTED}:
            dm.sex, dm.sex_confidence = SEX_FEMALE_NON_CBBE, Level.HIGH.value
        else:
            dm.sex = SEX_FEMALE_3BBB if any_3bbb else SEX_FEMALE_CBBE
            dm.sex_confidence = Level.HIGH.value
    else:
        near = [ds.median_distance for _, ds in outfit if ds.median_distance is not None]
        if any_3bbb:
            dm.sex, dm.sex_confidence = SEX_FEMALE_3BBB, Level.MEDIUM.value
            E.append("3BBB physics bones are weighted")
        elif near and min(near) <= FIT_MEDIAN_MAX:
            dm.sex, dm.sex_confidence = SEX_FEMALE_CBBE, Level.MEDIUM.value
            E.append(f"no body shape; the closest shape lies on the CBBE reference surface (median distance "
                     f"{min(near):.2f} ≤ {FIT_MEDIAN_MAX:g}). Fitted to CBBE by geometry, not proven by a body")
        else:
            dm.sex, dm.sex_confidence = SEX_UNKNOWN, Level.LOW.value
            E.append("no body shape and no shape close to the CBBE reference surface"
                     + (f" (closest median distance {min(near):.1f})" if near else ""))
    dm.existing_3bbb = "YES" if any_3bbb else "NO"

    # ---- structural blockers --------------------------------------------------------------------------------
    if dm.unsupported_structures:
        dm.action, dm.status, dm.confidence = Action.REVIEW.value, "UNSUPPORTED_NIF_STRUCTURE", Level.LOW.value
        R.append("NIF structures the direct pipeline cannot preserve yet: " + ", ".join(dm.unsupported_structures[:4]))
        return
    if dm.sex == SEX_MALE:
        dm.action, dm.status, dm.confidence = Action.UNSUPPORTED.value, "MALE_MESH", Level.MEDIUM.value
        R.append("the mesh contains a male (non-CBBE) body although a female slot uses it: never converted as CBBE")
        return
    if dm.sex == SEX_FEMALE_NON_CBBE:
        dm.action, dm.status, dm.confidence = Action.UNSUPPORTED.value, "NON_CBBE_BODY", Level.HIGH.value
        R.append("the embedded body is not the CBBE topology (another body family): out of scope")
        return

    # ---- the Phase 1 rules on the mesh's outfit shapes ---------------------------------------------------------
    pr = ProjectResult(project=PureWindowsPath(dm.mesh).stem + " | " + " | ".join(dm.item_names[:3]), mod=dm.source_mod,
                       provider_kind="mod", osp_rel=dm.mesh, osp_path=str(path), slider_count=1,
                       shapes=[sa for sa in analyses])
    pr.family = FamilyResult(family="CBBE" if dm.sex in (SEX_FEMALE_CBBE, SEX_FEMALE_3BBB) else "UNKNOWN",
                             physics="3bbb" if any_3bbb else "none", label="CBBE" if dm.sex != SEX_UNKNOWN else "unknown",
                             confidence=dm.sex_confidence, evidence=list(E))
    classify_project(pr, ref is not None)
    dm.reasons.extend(pr.reasons)
    dm.warnings.extend(pr.warnings)
    action, conf = pr.action, pr.geometry_confidence

    # per-shape decision for convertible outfit shapes
    convert_shapes = []
    for sa, ds in outfit:
        regs = [r for r in ("BREAST", "BUTT", "THIGH") if ds.regions.get(r, {}).get("relevance") in ("MEDIUM", "HIGH")]
        regs = [r for r in ref.physics_regions if r in regs] if ref else regs
        if regs:
            convert_shapes.append((sa, ds, regs))

    # ---- body state -----------------------------------------------------------------------------------------
    body_plan = None
    if identities:
        classes = [bi.classification for _, _, bi in identities]
        dm.embedded_body = classes[0] if len(set(classes)) == 1 else "MIXED"
        if len(identities) > 1 and BODY_ALREADY_3BBB not in classes:
            dm.action, dm.status, dm.confidence = Action.REVIEW.value, "MULTIPLE_BODIES", Level.LOW.value
            R.insert(0, f"{len(identities)} embedded body shapes: not handled automatically")
            return
        shp, ds, bi = identities[0]
        if bi.classification == BODY_AMBIGUOUS:
            dm.action, dm.status, dm.confidence = Action.REVIEW.value, "AMBIGUOUS_BODY", Level.LOW.value
            R.insert(0, f"embedded body '{shp.name}' is not positively identified: " + "; ".join(bi.reasons[:2]))
            return
        if bi.classification == BODY_UNSUPPORTED:
            dm.action, dm.status, dm.confidence = Action.UNSUPPORTED.value, "NON_CBBE_BODY", Level.HIGH.value
            R.insert(0, f"embedded body '{shp.name}' is not the CBBE topology")
            return
        if bi.classification == BODY_COMPATIBLE:
            if not allow_body_replacement:
                dm.action, dm.status, dm.confidence = Action.REVIEW.value, "EMBEDDED_BODY", Level.MEDIUM.value
                R.insert(0, f"the mesh embeds a partial CBBE body ('{shp.name}', {bi.reference_coverage:.0%} of the "
                            "reference; plain CBBE weights). Converting only the clothes would leave it without physics; "
                            "re-run with --allow-embedded-body-replacement to plan the proven body replacement")
                return
            ds.decision = "BODY_REPLACE"
            body_plan = {"source_body_shape": shp.name, "vertices": shp.num_vertices,
                         "reference_coverage": bi.reference_coverage, "method": REPLACEMENT_METHOD,
                         "method_details": "the body keeps its own (partial) geometry; every vertex gets the weights of "
                                           "the coinciding CBBE-3BBB reference vertex (radius 0.01, one target). The full "
                                           "reference body is never inserted",
                         "legacy_bones_replaced": bi.legacy_bones}
        if bi.classification == BODY_ALREADY_3BBB:
            ds.decision = "KEEP"

    # ---- map the rules' result -------------------------------------------------------------------------------
    if action == Action.KEEP.value:
        dm.action, dm.status, dm.confidence = Action.KEEP.value, "ALREADY_3BBB", conf
        return
    if action == Action.UNSUPPORTED.value:
        dm.action, dm.status, dm.confidence = Action.UNSUPPORTED.value, "NON_CBBE_BODY", conf
        return
    if action == Action.ERROR.value:
        if body_plan and not outfit:  # a body-only mesh (e.g. a body replacer): the body alone
            action = Action.SKIP.value
        else:
            dm.action, dm.status, dm.confidence = Action.ERROR.value, "UNKNOWN", conf
            return
    if dm.sex == SEX_UNKNOWN:
        if action == Action.SKIP.value and not body_plan:
            dm.action, dm.status, dm.confidence = Action.SKIP.value, "NO_PHYSICS_REGION", conf
            R.insert(0, "no shape lies over a breast/butt/thigh physics region (body family not needed)")
        else:
            dm.action, dm.status, dm.confidence = Action.REVIEW.value, "UNKNOWN", Level.LOW.value
            R.insert(0, "sex/body family could not be established from geometry; never guessed")
        return
    if action == Action.SKIP.value and not body_plan:
        dm.action, dm.status, dm.confidence = Action.SKIP.value, "NO_PHYSICS_REGION", conf
        return
    if dm.cloth_data:
        dm.action, dm.status, dm.confidence = Action.REVIEW.value, "CLOTH_DATA_UNSUPPORTED", Level.MEDIUM.value
        R.insert(0, f"the NIF has cloth physics data (BSClothExtraData, {dm.cloth_data} block(s)); its preservation "
                    "through the direct-NIF Outfit Studio path is not verified yet")
        return
    if action == Action.REVIEW.value:
        names = dm.mesh + " " + " ".join(dm.item_names) + " " + " ".join(ds.name for _, ds, _ in convert_shapes)
        acc = _hit(ACCESSORY_WORDS, names) or _hit(RIGID_WORDS, names)
        dm.action, dm.confidence = Action.REVIEW.value, conf
        dm.status = "RIGID_REGION_OVERLAP" if acc else "ANALYZER_REVIEW"
        return
    # action is CONVERT (HIGH) — or SKIP with a compatible body to replace
    rigid = [ds.name for _, ds, _ in convert_shapes if ds.kind == SHAPE_RIGID]
    if rigid:
        dm.action, dm.status, dm.confidence = Action.REVIEW.value, "RIGID_REGION_OVERLAP", Level.MEDIUM.value
        R.insert(0, f"small, few-bone shapes over a physics region would move like skin: {', '.join(rigid[:4])} "
                    f"(≤ {RIGID_MAX_BONES} bones, ≤ {RIGID_MAX_VERTICES} vertices)")
        return
    unsafe, outside = [], []
    for sa, ds, regs in convert_shapes:
        shp = next(s for s in nif.shapes if s.name == ds.name)
        reach = plan_shape_reach(ref, shp.world_vertices(), settings.search_radius, settings.max_vertex_targets,
                                 settings.no_target_limit)
        ds.out_of_reach_vertices = reach["far"]
        if reach["far"] and not reach["ok"]:
            unsafe.append(f"{ds.name}: {len(reach['far'])} vertices out of reach (nearest {reach['distance']:.2f}); "
                          f"no safe radius: {reach['reason']}")
            continue
        # 0.4.1: a full Copy Bone Weights copies every reference bone; it must add only the planned 3BBB bones
        # (the BodySlide path would fall back to a region-restricted copy with MEDIUM confidence here)
        planned = region_bones(ref, regs)
        pred = ref.predict_weights(shp.world_vertices(), ref.physics_bones, reach["radius"], settings.max_vertex_targets,
                                   settings.no_target_limit)
        extra = [b for j, b in enumerate(ref.physics_bones) if (pred[:, j] > WEIGHT_ADDED_EPS).any() and b not in planned]
        if extra:
            j = [ref.physics_bones.index(b) for b in extra]
            n_v = int((pred[:, j] > WEIGHT_ADDED_EPS).any(1).sum())
            outside.append(f"{ds.name}: a full Copy Bone Weights would also add {', '.join(extra)} ({n_v} vertices, max "
                           f"weight {float(pred[:, j].max()):.3f}) outside the planned regions ({', '.join(regs).lower()})")
            continue
        ds.decision = "CONVERT"
        ds.planned_regions = regs
        ds.planned_bones = planned
        ds.planned_radius = reach["radius"]
        if reach["far"]:
            ds.reasons.append(f"radius {reach['radius']:g} instead of {settings.search_radius:g} for vertices "
                              f"{', '.join(map(str, reach['far'][:12]))} (no other vertex changes)")
    if unsafe:
        dm.action, dm.status, dm.confidence = Action.REVIEW.value, "UNSAFE_RADIUS", Level.MEDIUM.value
        R[:0] = unsafe
        for _, ds, _ in convert_shapes:
            ds.decision, ds.planned_regions, ds.planned_bones, ds.planned_radius = "NONE", [], [], None
        return
    if outside:
        dm.action, dm.status, dm.confidence = Action.REVIEW.value, "FULL_COPY_OUTSIDE_REGIONS", Level.MEDIUM.value
        R[:0] = outside + ["unexpected 3BBB region: not converted automatically (a region-restricted copy or planning the "
                           "extra region needs a separate decision)"]
        for _, ds, _ in convert_shapes:
            ds.decision, ds.planned_regions, ds.planned_bones, ds.planned_radius = "NONE", [], [], None
        return
    if body_plan:
        dm.body_replacement = body_plan
    dm.action, dm.confidence = Action.CONVERT.value, Level.HIGH.value
    dm.status = "EMBEDDED_BODY_REPLACEMENT" if body_plan else "DIRECT_NIF_HIGH_CONFIDENCE"
    if body_plan and action == Action.SKIP.value:
        R.insert(0, "only the embedded CBBE body lies over the physics regions: it gets the CBBE-3BBB reference weights "
                    "by vertex identity; the other shapes stay untouched")


# ---------------------------------------------------------------------------------------------------------------
# the package
# ---------------------------------------------------------------------------------------------------------------

def analyze_direct_package(inst, kb: KnowledgeBase, package: str, projects: list[ProjectResult],
                           output_mod: Optional[Path], allow_body_replacement: bool = False,
                           progress: Optional[Callable[[int, int, str], None]] = None,
                           settings: Optional[MetricSettings] = None) -> DirectNifReport:
    """Read-only Direct-NIF analysis of one MO2 package. Writes nothing."""
    say = progress or (lambda *_: None)
    conv = inst.profile.conversion
    rep = DirectNifReport(package=package, created=time.strftime("%Y-%m-%dT%H:%M:%S"), output_mod=str(output_mod or ""),
                          allow_body_replacement=bool(allow_body_replacement))
    mine = [p for p in projects if p.mod == package]
    rep.bodyslide_projects = len(mine)
    prov = next((p for p in inst.providers if p.name == package), None)
    if prov is None:
        rep.notes.append(f"package '{package}' is not an enabled MO2 mod / data provider")
        rep.source_mode = detect_source_mode(len(mine), 0)
        return rep
    rep.provider_path = str(prov.root)
    active = _active_plugins(inst)
    refs: dict[str, list[GameItem]] = defaultdict(list)
    spelling: dict[str, str] = {}
    for p in sorted(x for x in Path(prov.root).iterdir() if x.is_file() and x.suffix.lower() in PLUGIN_SUFFIXES):
        pa = read_plugin_armors(p)
        enabled = None if active is None else p.name.lower() in active
        rep.plugins.append({"plugin": p.name, "enabled": enabled, "localized": pa.localized, "armor": len(pa.armors),
                            "armor_addons": len(pa.addons), "errors": pa.errors[:5]})
        if enabled is False:
            rep.notes.append(f"plugin {p.name} is not active in the MO2 profile: the game does not use its items")
        for k, v in plugin_mesh_refs(pa, spelling).items():
            refs[k].extend(v)
    if not rep.plugins:
        rep.notes.append("the package ships no plugin: its meshes cannot be tied to in-game items")
    female = {k: v for k, v in refs.items() if any(i.role == MODEL_ROLES[FEMALE_THIRD] for i in v)}
    male = {k: v for k, v in refs.items() if k not in female and any(i.role == MODEL_ROLES[MALE_THIRD] for i in v)}
    rep.female_items = len({(i.plugin, i.armor_form_id) for v in female.values() for i in v if i.role == MODEL_ROLES[FEMALE_THIRD]})
    rep.male_items = len({(i.plugin, i.armor_form_id) for v in refs.values() for i in v if i.role == MODEL_ROLES[MALE_THIRD]})
    rep.unique_female_meshes = len(female)
    rep.male_meshes_skipped = len(male)
    others = [k for k in refs if k not in female and k not in male]
    rep.other_models = {"first_person_only": len(others)}
    # meshes the package's BodySlide projects build (MIXED): those stay with the BodySlide pipeline
    owned = {}
    for p in mine:
        if p.output_path and p.output_file:
            owned[normalize_mesh_path(p.output_path + "/" + p.output_file)] = p.project
    ref_shape = _reference_shape(inst)
    if kb.reference is None or ref_shape is None:
        rep.notes.append("CBBE-3BBB reference not available: geometry analysis impossible")
    settings = settings or MetricSettings(search_radius=conv.search_radius, max_vertex_targets=conv.max_vertex_targets,
                                          no_target_limit=conv.no_target_limit,
                                          region_weight_threshold=inst.profile.analysis.region_weight_threshold)
    cache: dict = {}
    from .direct_batch import existing_direct_status, latest_direct_records  # 0.4.1 idempotency

    direct_recs = latest_direct_records(output_mod)
    keys = sorted(female)
    for i, key in enumerate(keys):
        rel = "meshes/" + key
        items = female[key]
        say(i, len(keys), f"Direct NIF {i + 1}/{len(keys)}: {rel}")
        dm = DirectMesh(mesh=spelling.get(key, rel.replace("/", "\\")), items=[it.to_dict() for it in items],
                        roles=sorted({it.role for it in items}))
        rep.meshes.append(dm)
        game_rel = dm.mesh.replace("\\", "/")
        dm.planned_target = str(Path(output_mod) / game_rel) if output_mod else game_rel
        dm.body_slot = any(33 in it.biped_slots for it in items if it.role == MODEL_ROLES[FEMALE_THIRD])
        cands = resolve_data_file(inst.providers, rel)
        dm.competitors = [{"provider": n, "path": str(p), "size": s} for n, p, s, _ in cands]
        pkg_copy = next((c for c in cands if c[0] == package), None)
        dm.source_mod = package if pkg_copy else (cands[0][0] if cands else "")
        if MODEL_ROLES[MALE_THIRD] in dm.roles:
            dm.sex, dm.sex_confidence = SEX_AMBIGUOUS, Level.HIGH.value
            dm.sex_evidence.append("the same NIF is used as a male AND a female third-person model")
        if key in owned:
            dm.action, dm.status, dm.confidence = Action.SKIP.value, "DUPLICATE_GAME_MESH", Level.HIGH.value
            dm.reasons.append(f"built by the package's BodySlide project '{owned[key]}': the BodySlide pipeline owns it"
                              + ("" if cands else " (not built yet)"))
            continue
        rec = direct_recs.get(key)
        if rec is not None and output_mod:  # a committed Direct-NIF conversion of this game mesh
            dm.action, dm.status, rs = existing_direct_status(rec, cands, package, Path(output_mod), inst)
            dm.confidence = Level.HIGH.value
            dm.reasons.extend(rs)
            dm.exists = bool(cands)
            if cands:
                dm.winner = {"provider": cands[0][0], "path": str(cands[0][1]), "size": cands[0][2]}
            dm.sha256 = rec.get("source", {}).get("sha256", "")
            dm.sex = rec.get("sex", dm.sex) if dm.sex == SEX_UNKNOWN else dm.sex
            continue
        if not cands:
            dm.action, dm.status = Action.ERROR.value, "MISSING_GAME_MESH"
            dm.reasons.append(f"{dm.mesh} is referenced by {', '.join(dm.item_names[:3])} but no loose file provides it "
                              "(not in the package, another mod or overwrite; it may be in an archive)")
            continue
        wname, wpath, wsize, _ = cands[0]
        dm.exists = True
        dm.winner = {"provider": wname, "path": str(wpath), "size": wsize}
        if pkg_copy is None:
            dm.action, dm.status, dm.confidence = Action.SKIP.value, "NOT_PACKAGE_MESH", Level.HIGH.value
            dm.reasons.append(f"the mesh is not shipped by this package (provided by {wname}); not converted as part of it")
            continue
        if wname != package:
            dm.action, dm.status, dm.confidence = Action.REVIEW.value, "MO2_CONFLICT", Level.LOW.value
            dm.reasons.append(f"another provider wins the MO2 conflict ({wname}); the package's own copy is not what the "
                              "game loads and is never converted silently")
            if output_mod and Path(wpath).resolve().is_relative_to(Path(output_mod).resolve()):
                dm.status = "OUTPUT_OVERRIDE_PRESENT"
                dm.reasons.append("the winner is the output mod: a generated override already exists (not verified here)")
            continue
        dm.size = wsize
        dm.sha256 = _sha(Path(wpath))
        if dm.sex == SEX_AMBIGUOUS:
            dm.action, dm.status, dm.confidence = Action.REVIEW.value, "AMBIGUOUS", Level.LOW.value
            dm.reasons.append("used as both male and female model: never guessed")
            continue
        if kb.reference is None or ref_shape is None:
            dm.action, dm.status = Action.REVIEW.value, "UNKNOWN"
            dm.reasons.append("no CBBE-3BBB reference: cannot analyze")
            continue
        analyze_mesh(dm, Path(wpath), kb, ref_shape, settings, allow_body_replacement, cache)
    legacy_bodies = [m for m in rep.meshes if m.body_slot and m.embedded_body in (BODY_COMPATIBLE, BODY_AMBIGUOUS)
                     and m.action != Action.CONVERT.value]
    if legacy_bodies and any(m.action == Action.CONVERT.value and not m.body_replacement for m in rep.meshes):
        rep.notes.append(
            f"LAYERING RISK: {len(legacy_bodies)} body-slot (33) item mesh(es) of this package embed a plain CBBE body with "
            "legacy weights and are not converted here (e.g. " + ", ".join(PureWindowsPath(m.mesh).name for m in legacy_bodies[:3])
            + "). Converted clothing worn together with such an item moves with 3BBB physics while that item's body "
              "does not: test converted clothing with the CBBE-3BBB body (no body-slot item) or with converted body-slot items")
    unowned = [m for m in rep.meshes if m.status != "DUPLICATE_GAME_MESH"]
    rep.source_mode = detect_source_mode(len(mine), len(unowned))
    for m in rep.meshes:
        rep.counts[m.action] = rep.counts.get(m.action, 0) + 1
        rep.status_counts[m.status] = rep.status_counts.get(m.status, 0) + 1
        rep.sex_counts[m.sex] = rep.sex_counts.get(m.sex, 0) + 1
    rep.notes.append("read-only analysis: nothing was converted or written into any mod; Direct-NIF conversion is the "
                     "separate, explicit plan-direct-batch / convert-direct-batch / commit-direct-batch workflow")
    return rep


# ---------------------------------------------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------------------------------------------

ACTIONS = ("CONVERT", "KEEP", "SKIP", "REVIEW", "UNSUPPORTED", "ERROR")


def direct_report_text(rep: DirectNifReport, details: bool = False, limit: int = 60) -> str:
    L = [f"Package: {rep.package}",
         f"Source mode: {rep.source_mode}   (BodySlide projects: {rep.bodyslide_projects})",
         "Plugins: " + (", ".join(f"{p['plugin']} ({'active' if p['enabled'] else 'INACTIVE' if p['enabled'] is False else 'activation unknown'}"
                                  f", {p['armor']} ARMO / {p['armor_addons']} ARMA)" for p in rep.plugins) or "none"),
         f"Female game items: {rep.female_items}   unique female meshes: {rep.unique_female_meshes}",
         f"Male items: {rep.male_items}   male-only meshes skipped: {rep.male_meshes_skipped}   "
         f"other (first-person only) models: {rep.other_models.get('first_person_only', 0)}",
         "  " + "   ".join(f"{a} {rep.counts.get(a, 0)}" for a in ACTIONS),
         "  reasons: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.status_counts.items(), key=lambda x: -x[1])),
         "  sex/body: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.sex_counts.items(), key=lambda x: -x[1])),
         f"Embedded-body replacement planning: {'ON' if rep.allow_body_replacement else 'OFF'}", ""]
    L.append(f"{'Action':<11} {'Reason':<27} {'Conf':<7} {'Sex/body':<23} Mesh  →  item(s)")
    rows = sorted(rep.meshes, key=lambda m: (ACTIONS.index(m.action) if m.action in ACTIONS else 9, m.mesh.lower()))
    for m in rows[: (None if details else limit)]:
        L.append(f"{m.action:<11} {m.status:<27} {m.confidence:<7} {m.sex:<23} {PureWindowsPath(m.mesh).name}  →  "
                 + ", ".join(m.item_names[:3]) + (" …" if len(m.item_names) > 3 else ""))
        if details:
            L += direct_mesh_lines(m)
    if not details and len(rows) > limit:
        L.append(f"… {len(rows) - limit} more (see the report files, or --details)")
    L += [""] + [f"note: {n}" for n in rep.notes]
    return "\n".join(L) + "\n"


def _did(mesh: str) -> str:
    return "dn-" + hashlib.sha1(normalize_mesh_path(mesh).encode("utf-8")).hexdigest()[:10]


def direct_mesh_lines(m: DirectMesh) -> list[str]:
    L = [f"    mesh: {m.mesh}   sha256 {m.sha256[:16]}…  {m.size} bytes   winner {m.winner.get('provider', '—')}"
         + (f"   competitors: {', '.join(c['provider'] for c in m.competitors[1:])}" if len(m.competitors) > 1 else "")]
    for it in m.items:
        L.append(f"    item: '{it.get('armor_name') or it.get('armor_editor_id')}' ARMO {it.get('armor_editor_id')} "
                 f"{it.get('armor_form_id')} / ARMA {it.get('addon_editor_id')} ({it.get('role')})")
    L.append(f"    sex/body: {m.sex} ({m.sex_confidence}); embedded body: {m.embedded_body}; existing 3BBB: {m.existing_3bbb}"
             + ("; body slot 33" if m.body_slot else "")
             + (f"; cloth data {m.cloth_data}" if m.cloth_data else ""))
    L += [f"      evidence: {e}" for e in m.sex_evidence[:4]]
    L += [f"    - {r}" for r in m.reasons[:8]]
    L += [f"    ! {w}" for w in m.warnings[:6]]
    for s in m.shapes:
        regs = ", ".join(f"{r.lower()} {d['relevance']}/{d['fit']}" for r, d in s.regions.items()) or "no region"
        L.append(f"    shape {s.name} [{s.kind}] {s.vertices} v / {s.triangles} t, {s.bones} bones, max {s.max_influences} "
                 f"influences, sums {s.weight_sum_min:.3f}..{s.weight_sum_max:.3f}"
                 + (f", median distance {s.median_distance:.2f}" if s.median_distance is not None else "")
                 + f": {s.decision}  ({regs})")
        if s.decision == "CONVERT":
            L.append(f"      planned bones: {', '.join(b.replace('_skin', '') for b in s.planned_bones)}; radius {s.planned_radius:g}")
        if s.body_identity:
            bi = s.body_identity
            L.append(f"      body: {bi['classification']} {bi['coincident_fraction']:.1%} on reference, coverage "
                     f"{bi['reference_coverage']:.0%}, triangles {bi['triangle_subset']:.2%}; legacy {bi['legacy_bones']}")
        L += [f"      · {r}" for r in s.reasons[:3]]
    if m.body_replacement:
        L.append(f"    BODY REPLACEMENT (planned): {m.body_replacement['source_body_shape']} → {m.body_replacement['method']}")
    L.append(f"    Direct-NIF ID: {_did(m.mesh)}   target: {m.planned_target}")
    return L


CSV_COLUMNS = ["mesh", "items", "action", "reason", "confidence", "sex_body", "embedded_body", "existing_3bbb",
               "regions", "shapes", "planned_bones", "planned_radius", "winner", "competitors", "sha256", "size",
               "cloth_data", "warnings", "reasons"]


def _csv(rep: DirectNifReport) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for m in rep.meshes:
        conv = [s for s in m.shapes if s.decision == "CONVERT"]
        w.writerow([m.mesh, "; ".join(m.item_names), m.action, m.status, m.confidence, m.sex, m.embedded_body, m.existing_3bbb,
                    "; ".join(f"{s.name}:{'+'.join(s.planned_regions)}" for s in conv),
                    "; ".join(f"{s.name}[{s.kind}]" for s in m.shapes),
                    "; ".join(f"{s.name}:{','.join(s.planned_bones)}" for s in conv),
                    "; ".join(f"{s.name}:{s.planned_radius:g}" for s in conv),
                    m.winner.get("provider", ""), "; ".join(c["provider"] for c in m.competitors[1:]), m.sha256, m.size,
                    m.cloth_data, " | ".join(m.warnings[:4]), " | ".join(m.reasons[:4])])
    return buf.getvalue()


def _html(rep: DirectNifReport) -> str:
    e = html.escape
    rows = []
    for i, m in enumerate(sorted(rep.meshes, key=lambda m: (m.action, m.mesh.lower()))):
        rows.append(f"<tr class='a-{e(m.action)}' onclick=\"document.getElementById('d{i}').classList.toggle('hide')\">"
                    f"<td>{e(m.action)}</td><td>{e(m.status)}</td><td>{e(m.confidence)}</td><td>{e(m.sex)}</td>"
                    f"<td>{e(PureWindowsPath(m.mesh).name)}</td><td>{e(', '.join(m.item_names[:3]))}</td></tr>"
                    f"<tr id='d{i}' class='hide'><td colspan='6'><pre>{e(chr(10).join(direct_mesh_lines(m)))}</pre></td></tr>")
    summary = e(direct_report_text(rep, details=False, limit=0).split("\n\n")[0])
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Direct NIF: {e(rep.package)}</title><style>
body{{font:14px system-ui,sans-serif;margin:16px;background:#fff;color:#111}} table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}} tr{{cursor:pointer}}
.hide{{display:none}} pre{{white-space:pre-wrap;margin:0;font-size:12px}}
.a-CONVERT td:first-child{{color:#0a6}} .a-REVIEW td:first-child{{color:#b60}} .a-ERROR td:first-child,.a-UNSUPPORTED td:first-child{{color:#c22}}
@media (prefers-color-scheme: dark){{body{{background:#111;color:#eee}} td,th{{border-color:#333}}}}
</style></head><body><h1>Direct NIF analysis — {e(rep.package)}</h1><pre>{summary}</pre>
<p>Read-only analysis (tool {e(rep.tool_version)}, {e(rep.created)}). Click a row for details.</p>
<table><tr><th>Action</th><th>Reason</th><th>Conf</th><th>Sex/body</th><th>Mesh</th><th>Item(s)</th></tr>{''.join(rows)}</table>
</body></html>"""


def write_direct_reports(rep: DirectNifReport, folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    base = "".join(c if c.isalnum() or c in "-_." else "_" for c in rep.package) or "package"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = {"json": folder / f"{stamp}_{base}_direct.json", "csv": folder / f"{stamp}_{base}_direct.csv",
           "txt": folder / f"{stamp}_{base}_direct.txt", "html": folder / f"{stamp}_{base}_direct.html"}
    out["json"].write_text(json.dumps(rep.to_dict(), indent=1), encoding="utf-8")
    out["csv"].write_text(_csv(rep), encoding="utf-8")
    out["txt"].write_text(direct_report_text(rep, details=True), encoding="utf-8")
    out["html"].write_text(_html(rep), encoding="utf-8")
    return out
