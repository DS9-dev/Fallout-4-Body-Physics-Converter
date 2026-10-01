"""Installed reference bodies and the Outfit Studio weight-transfer model.

The CBBE 3BBB reference is *read from the user's installation* (never
bundled). ``ReferenceBody.predict_weights`` reproduces the arithmetic of
Outfit Studio's "Copy Bone Weights" (v5.7.1): for each outfit vertex it takes
the reference vertices within *Search Radius*, sorted by distance and capped
at *Max Vertex Targets*; if the nearest one has no weight for a bone the
result is 0, otherwise it is the inverse-distance weighted mean over the
neighbours that do carry that bone (an exact hit weighs 1000).
See docs/CONVERSION.md for the source references.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.spatial import cKDTree

from ..bodyslide.osp import OspParseError, SliderSet, parse_osp
from ..bodyslide.xmlfiles import RefTemplate
from ..discovery.vfs import VirtualFS, VirtualFile
from ..nif.reader import NifError, NifShape, read_nif
from .models import Region

log = logging.getLogger(__name__)

__all__ = [
    "ReferenceError_",
    "ReferenceBody",
    "KnowledgeBase",
    "classify_bone_region",
    "LEGACY_PHYSICS_BONES",
    "build_knowledge_base",
]


class ReferenceError_(Exception):
    pass


# Bones used by pre-3BBB physics setups (present in the vanilla skeleton or
# CBBE's legacy "physics" body). Weight on these is *not* 3BBB weighting.
LEGACY_PHYSICS_BONES = {
    "LBreast_skin": Region.BREAST,
    "RBreast_skin": Region.BREAST,
    "LButtFat_skin": Region.BUTT,
    "RButtFat_skin": Region.BUTT,
    "CLOTH_Bone_Googles_00": Region.BREAST,
    "CLOTH_Bone_Googles_01": Region.BREAST,
}

# Fallback used only when the reference cannot be compared against a skeleton.
FALLBACK_3BBB_BONES = [
    "LBreast_01_skin", "LBreast_02_skin", "LBreast_03_skin",
    "RBreast_01_skin", "RBreast_02_skin", "RBreast_03_skin",
    "LButt_01_skin", "RButt_01_skin",
    "LLeg_Thigh_01_F_skin", "LLeg_Thigh_01_R_skin", "RLeg_Thigh_01_F_skin", "RLeg_Thigh_01_R_skin",
]


def classify_bone_region(bone: str) -> Region:
    b = bone.lower()
    if "breast" in b or "boob" in b:
        return Region.BREAST
    if "butt" in b or "glute" in b or "pelvis" in b:
        return Region.BUTT
    if "thigh" in b:
        return Region.THIGH
    if "belly" in b:
        return Region.BELLY
    return Region.OTHER


def bone_side(bone: str) -> Optional[str]:
    """'L' / 'R' for side-specific bones (``LBreast_01_skin``, ``RLeg_...``, ``Breast_L``)."""
    if re.match(r"^L[A-Z_]", bone) or re.search(r"[_ ]L(_|$)", bone):
        return "L"
    if re.match(r"^R[A-Z_]", bone) or re.search(r"[_ ]R(_|$)", bone):
        return "R"
    return None


@dataclass
class ReferenceBody:
    display_name: str
    set_name: str
    shape_name: str
    source: str  # "<provider>: <relative path>"
    vertices: np.ndarray  # (N,3) float64
    bones: list[str]
    weights: np.ndarray  # (N,B) float32
    physics_bones: list[str]
    physics_regions: dict[str, list[str]]  # region -> physics bones
    side_sign: dict[str, float] = field(default_factory=dict)  # "L"/"R" -> sign of x on that side
    tree: Optional[cKDTree] = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.tree is None:
            self.tree = cKDTree(self.vertices)
        self._bone_index = {b: i for i, b in enumerate(self.bones)}

    @property
    def num_vertices(self) -> int:
        return int(self.vertices.shape[0])

    def bone_indices(self, names: list[str]) -> list[int]:
        return [self._bone_index[n] for n in names if n in self._bone_index]

    def region_weight(self, region: str) -> np.ndarray:
        idx = self.bone_indices(self.physics_regions.get(region, []))
        if not idx:
            return np.zeros(self.num_vertices, dtype=np.float32)
        return self.weights[:, idx].sum(axis=1)

    def nearest(self, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        d, i = self.tree.query(pts, k=1)
        return np.asarray(d, dtype=np.float64), np.asarray(i, dtype=np.int64)

    def predict_weights(
        self,
        pts: np.ndarray,
        bones: list[str],
        radius: float = 5.0,
        max_targets: int = 4,
        no_target_limit: bool = False,
    ) -> np.ndarray:
        """Emulate Outfit Studio's Copy Bone Weights for ``bones``; returns (M, len(bones))."""
        m = pts.shape[0]
        idx_b = [self._bone_index.get(b, -1) for b in bones]
        out = np.zeros((m, len(bones)), dtype=np.float32)
        if m == 0 or not bones:
            return out
        k = min(self.num_vertices, 64 if no_target_limit else max(1, int(max_targets)))
        d, idx = self.tree.query(pts, k=k, distance_upper_bound=radius)
        if k == 1:
            d = d[:, None]
            idx = idx[:, None]
        valid = np.isfinite(d)
        n = self.num_vertices
        wsub = np.zeros((n + 1, len(bones)), dtype=np.float32)
        for j, bi in enumerate(idx_b):
            if bi >= 0:
                wsub[:n, j] = self.weights[:, bi]
        wn = wsub[np.where(valid, idx, n)]  # (M,k,B)
        with np.errstate(divide="ignore"):
            inv = np.where(d == 0.0, 1000.0, 1.0 / np.where(valid, d, 1.0))
        inv = np.where(valid, inv, 0.0).astype(np.float32)
        has = wn > 0.0
        num = (wn * inv[:, :, None] * has).sum(axis=1)
        den = (inv[:, :, None] * has).sum(axis=1)
        res = np.divide(num, den, out=np.zeros_like(num), where=den > 0)
        res[~has[:, 0, :]] = 0.0  # nearest reference vertex has no weight for the bone
        res[~valid[:, 0]] = 0.0
        return res.astype(np.float32)


@dataclass
class BodySignature:
    name: str  # template display name / family label
    family: str  # CBBE / CBBE_3BBB / ...
    shape_name: str
    vertex_count: int
    vertices: np.ndarray


@dataclass
class KnowledgeBase:
    """Everything the analyzer learned from the installation before scanning."""

    reference: Optional[ReferenceBody]
    skeleton_bones: set[str]
    body_signatures: list[BodySignature]
    cbbe_sliders: set[str]
    body_reference_sets: set[str]  # slider set names of body reference projects
    notes: list[str] = field(default_factory=list)

    @property
    def physics_bones(self) -> set[str]:
        return set(self.reference.physics_bones) if self.reference else set(FALLBACK_3BBB_BONES)


def _load_template_shape(vfs: VirtualFS, tpl: RefTemplate) -> tuple[SliderSet, NifShape, VirtualFile, VirtualFile]:
    src = vfs.resolve(tpl.source_file)
    if src is None:
        raise ReferenceError_(f"reference project file not found: {tpl.source_file}")
    try:
        osp = parse_osp(src.abs_path)
    except OspParseError as exc:
        raise ReferenceError_(f"cannot parse {tpl.source_file}: {exc}") from exc
    ss = next((s for s in osp.sets if s.name == tpl.set_name), None)
    if ss is None:
        raise ReferenceError_(f"slider set '{tpl.set_name}' not found in {tpl.source_file}")
    # A <Shape DataFolder="..."> attribute only relocates slider data (.osd);
    # the geometry always comes from ShapeData/<DataFolder>/<SourceFile>.
    folder = ss.data_folder
    nif_vf = vfs.resolve(f"ShapeData/{folder}/{ss.source_file}" if folder else f"ShapeData/{ss.source_file}")
    if nif_vf is None:
        raise ReferenceError_(f"reference ShapeData not found: ShapeData/{folder}/{ss.source_file}")
    try:
        nif = read_nif(nif_vf.abs_path)
    except NifError as exc:
        raise ReferenceError_(f"cannot read reference NIF: {exc}") from exc
    shp = nif.shape(tpl.shape)
    if shp is None:
        raise ReferenceError_(f"shape '{tpl.shape}' not found in {nif_vf.rel}")
    return ss, shp, src, nif_vf


def _skeleton_bones(vfs: VirtualFS, default_skeleton: str) -> set[str]:
    for rel in [default_skeleton, "res/skeleton_fo4.nif"]:
        if not rel:
            continue
        vf = vfs.resolve(rel)
        if vf is None:
            continue
        try:
            return set(read_nif(vf.abs_path).node_names)
        except NifError:
            continue
    return set()


def build_knowledge_base(inst) -> KnowledgeBase:  # inst: Installation (avoid import cycle)
    vfs: VirtualFS = inst.vfs
    notes: list[str] = []
    skel = _skeleton_bones(vfs, inst.bodyslide_config.default_skeleton if inst.bodyslide_config else "")
    if not skel:
        notes.append("vanilla skeleton not found; 3BBB physics bones fall back to built-in names")

    signatures: list[BodySignature] = []
    body_sets: set[str] = set()
    cbbe_sliders: set[str] = set()
    reference: Optional[ReferenceBody] = None

    for tpl in inst.ref_templates:
        text = f"{tpl.display_name} {tpl.set_name}".lower()
        if not any(k in text for k in ("body", "cbbe", "twb", "fusion", "bodytalk")):
            continue
        if text.startswith(("convert:", "sliders:")) or "convert" in tpl.display_name.lower():
            continue
        try:
            ss, shp, src, nif_vf = _load_template_shape(vfs, tpl)
        except ReferenceError_ as exc:
            notes.append(f"template '{tpl.display_name}': {exc}")
            continue
        body_sets.add(ss.name)
        fam = "CBBE_3BBB" if "3bbb" in text else ("CBBE" if "cbbe" in text else tpl.display_name)
        signatures.append(BodySignature(tpl.display_name, fam, shp.name, shp.num_vertices, shp.world_vertices()))
        if fam == "CBBE" and not cbbe_sliders:
            cbbe_sliders = set(ss.slider_names)
        if inst.reference_template is not None and tpl is inst.reference_template:
            bones, w = shp.weight_matrix()
            if skel:
                phys = [b for b in bones if b not in skel and (w[:, bones.index(b)] > 0).any()]
            else:
                phys = [b for b in FALLBACK_3BBB_BONES if b in bones]
            regions: dict[str, list[str]] = {}
            for b in phys:
                regions.setdefault(classify_bone_region(b).value, []).append(b)
            verts = shp.world_vertices()
            side_sign = {}
            for side in ("L", "R"):
                sb = [b for b in phys if bone_side(b) == side]
                if sb:
                    wi = w[:, [bones.index(b) for b in sb]].sum(axis=1)
                    if wi.sum() > 0:
                        side_sign[side] = float(np.sign((verts[:, 0] * wi).sum()))
            reference = ReferenceBody(
                display_name=tpl.display_name,
                set_name=tpl.set_name,
                shape_name=shp.name,
                source=f"{nif_vf.provider.name}: {nif_vf.rel}",
                vertices=verts,
                bones=bones,
                weights=w,
                physics_bones=phys,
                physics_regions=regions,
                side_sign=side_sign,
                fingerprint=f"{nif_vf.size}:{int(nif_vf.mtime)}:{shp.num_vertices}",
            )
            # the 3BBB body's own sliders are CBBE sliders too
            cbbe_sliders |= set(ss.slider_names)
    if reference is None and inst.reference_template is not None:
        notes.append(f"reference template '{inst.reference_template.display_name}' could not be loaded")
    if reference is not None:
        log.info(
            "reference %s: %d vertices, physics bones %s",
            reference.display_name,
            reference.num_vertices,
            {k: v for k, v in reference.physics_regions.items()},
        )
    return KnowledgeBase(
        reference=reference,
        skeleton_bones=skel,
        body_signatures=signatures,
        cbbe_sliders=cbbe_sliders,
        body_reference_sets=body_sets,
        notes=notes,
    )
