"""Phase 3.1: embedded body identity and the embedded CBBE → CBBE-3BBB body replacement.

Many CBBE full outfits build a CBBE body *into* the outfit mesh (the shape is listed in
the slider set). Converting only the clothes would leave that body without 3BBB
physics. Phase 3.1 replaces it, but only after the body is **positively identified**.

Identification (``identify_body``) compares the embedded body with the installed
CBBE-3BBB reference, which has the same topology as the installed CBBE body. Evidence:

* every body vertex coincides with a reference vertex (≤ 1e-3). Outfits often
  *delete* body vertices hidden under the clothes, so the body may be a subset;
* the body's triangles are reference triangles (seam duplicates welded);
* the body's slider morphs equal the reference body's morphs for the same sliders
  (the body behaves like the reference in BodySlide);
* the body carries no 3BBB physics weights yet;
* name and data folder are weak supporting evidence only.

Classes: ``BODY_CBBE_COMPATIBLE`` (the only one that may be replaced), ``BODY_ALREADY_3BBB``,
``BODY_AMBIGUOUS``, ``BODY_UNSUPPORTED``, ``NOT_BODY``.

Replacement method (chosen from experiments with Outfit Studio 5.8.2 on copies of the
real Raider Punk Outfit, see ``docs/RESEARCH.md`` §7): the body shape and its geometry
(including the author's deleted vertices), triangles and slider/zap data stay as they
are. Its weights are replaced by the CBBE-3BBB reference's weights **by vertex identity**:
a CopyBoneWeights step with a 0.01 search radius and one target, so every body vertex
takes exactly the weights of the reference vertex it coincides with. This is not the
clothing transfer (radius 5, 4 targets, inverse-distance mean). The body's slider data
is then re-imported as project-local data (ImportSliderData), because Outfit Studio's
AddProject loses the link of local data of shapes that carry a ``DataFolder``
attribute (the embedded ``CBBE`` body), and BodySlide would build the body without
morphs.

Rejected alternative: ``DeleteShape`` + ``SaveProject AutoCopyRef=true``. It inserts the
full 22 708-vertex reference body (restoring every vertex the author deleted) without
any slider data, so BodySlide builds an unmorphed full body under CBBE Curvy.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from scipy.spatial import cKDTree

from ..bodyslide.osd import OsdError, read_osd
from ..bodyslide.osp import SliderSet
from ..nif.reader import NifShape

__all__ = [
    "BODY_COMPATIBLE", "BODY_ALREADY_3BBB", "BODY_AMBIGUOUS", "BODY_UNSUPPORTED", "NOT_BODY",
    "REPLACEMENT_METHOD", "IDENTITY_RADIUS", "COINCIDE_TOL", "MORPH_TOL", "WEIGHT_TOL", "LEGACY_BODY_BONES",
    "BodyIdentity", "identify_body", "target_morphs", "geometry_signature", "vertex_map",
]

BODY_COMPATIBLE = "BODY_CBBE_COMPATIBLE"
BODY_ALREADY_3BBB = "BODY_ALREADY_3BBB"
BODY_AMBIGUOUS = "BODY_AMBIGUOUS"
BODY_UNSUPPORTED = "BODY_UNSUPPORTED"
NOT_BODY = "NOT_BODY"
REPLACEMENT_METHOD = "reference_weights_by_vertex_identity"
IDENTITY_RADIUS = 0.01  # CopyBoneWeights search radius for the body: only the coinciding reference vertex
COINCIDE_TOL = 1e-3  # a body vertex "is" a reference vertex within this distance
MORPH_TOL = 0.05  # slider morph offsets may differ this much (the real Raider Punk body: ≤ 0.015 on 1 vertex)
WEIGHT_TOL = 0.002  # half-float weights
TRI_SUBSET_MIN = 0.999
LEGACY_BODY_BONES = ("LBreast_skin", "RBreast_skin", "LButtFat_skin", "RButtFat_skin",
                     "CLOTH_Bone_Googles_00", "CLOTH_Bone_Googles_01")


def geometry_signature(verts: np.ndarray, tris: np.ndarray) -> str:
    h = hashlib.sha1()
    h.update(np.round(np.asarray(verts, dtype=np.float64), 3).astype(np.float32).tobytes())
    h.update(np.asarray(tris, dtype=np.int64).tobytes())
    return h.hexdigest()[:20]


def vertex_map(body_verts: np.ndarray, ref_verts: np.ndarray, tol: float = COINCIDE_TOL) -> tuple[np.ndarray, np.ndarray]:
    """For each body vertex the coinciding reference vertex index (-1 if none) and the distance."""
    d, i = cKDTree(ref_verts).query(body_verts, k=1)
    i = np.where(d <= tol, i, -1)
    return i, d


def _weld(verts: np.ndarray, tol: float = COINCIDE_TOL) -> np.ndarray:
    q = np.round(verts / tol).astype(np.int64)
    _, inv = np.unique(q, axis=0, return_inverse=True)
    return inv.reshape(-1)


def target_morphs(ss: SliderSet, target: str, n: int, resolve: Callable[[str, str], Optional[Path]]) -> dict[str, np.ndarray]:
    """Dense slider morphs (n, 3) of one slider-set target, keyed by slider name.

    ``resolve(folder, osd_file)`` returns the file or None; data folders follow BodySlide
    (local data in the set's folder, other data in the target shape's DataFolder list).
    """
    from .plan import data_folders_for

    out: dict[str, np.ndarray] = {}
    cache: dict[Path, object] = {}
    for sl in ss.sliders:
        for d in sl.data:
            if d.target != target:
                continue
            f = d.data_file.replace("\\", "/")
            if "/" not in f:
                continue
            osd_file, entry = f.split("/", 1)
            path = None
            for fo in data_folders_for(ss, d.target, bool(getattr(d, "local", False))):
                path = resolve(fo, osd_file)
                if path is not None:
                    break
            if path is None:
                continue
            if path not in cache:
                try:
                    cache[path] = read_osd(path)
                except (OsdError, OSError):
                    cache[path] = None
            osd = cache[path]
            if osd is not None and entry in osd.entries:
                out[sl.name] = osd.dense(entry, n)
    return out


@dataclass
class BodyIdentity:
    shape: str
    target: str = ""
    classification: str = NOT_BODY
    confidence: str = ""
    evidence: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    vertices: int = 0
    triangles: int = 0
    reference_vertices: int = 0
    coincident_fraction: float = 0.0
    max_distance: float = 0.0
    reference_coverage: float = 0.0  # fraction of the reference body present (rest: deleted by the outfit author)
    triangle_subset: float = 0.0
    morph_sliders_compared: int = 0
    morph_max_delta: float = 0.0
    morph_sliders_differing: list[str] = field(default_factory=list)
    body_only_sliders: list[str] = field(default_factory=list)
    zaps_on_body: list[str] = field(default_factory=list)
    existing_physics_bones: list[str] = field(default_factory=list)
    legacy_bones: list[str] = field(default_factory=list)
    weight_changes: dict = field(default_factory=dict)  # what the replacement changes: vertices / bones
    geometry_signature: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def identify_body(shape: NifShape, target: str, ss: SliderSet, body_morphs: dict[str, np.ndarray],
                  ref_shape: NifShape, ref_morphs: dict[str, np.ndarray], physics_bones: set[str],
                  name_hint: bool = False) -> BodyIdentity:
    """Classify an embedded body shape against the installed CBBE-3BBB reference body."""
    bi = BodyIdentity(shape=shape.name, target=target, vertices=shape.num_vertices, triangles=int(len(shape.triangles)),
                      reference_vertices=ref_shape.num_vertices)
    v = shape.world_vertices()
    rv = ref_shape.world_vertices()
    bi.geometry_signature = geometry_signature(v, shape.triangles)
    m, d = vertex_map(v, rv)
    ok = m >= 0
    bi.coincident_fraction = round(float(ok.mean()), 5) if len(m) else 0.0
    bi.max_distance = float(d.max()) if len(d) else 0.0
    weld = _weld(rv)
    bi.reference_coverage = round(len(set(weld[m[ok]])) / max(1, len(set(weld))), 4)
    if ok.all() and len(shape.triangles):
        rt = {tuple(sorted(t)) for t in weld[ref_shape.triangles].tolist()}
        bt = [tuple(sorted(t)) for t in weld[m[shape.triangles]].tolist()]
        bi.triangle_subset = round(sum(t in rt for t in bt) / len(bt), 5)
    bones, w = shape.weight_matrix()
    counts = (w > 0.001).sum(axis=0) if bones else []
    bi.existing_physics_bones = sorted(b for b, c in zip(bones, counts) if b in physics_bones and c > 0)
    bi.legacy_bones = sorted(b for b, c in zip(bones, counts) if b in LEGACY_BODY_BONES and c > 0)
    # slider behaviour: the body's morphs against the reference's morphs of the same sliders
    if ok.all():
        diffs = []
        for name, dense in body_morphs.items():
            ref = ref_morphs.get(name)
            if ref is None:
                if np.abs(dense).max() > 1e-4:
                    bi.body_only_sliders.append(name)
                continue
            delta = float(np.abs(dense - ref[m]).max())
            diffs.append(delta)
            if delta > MORPH_TOL:
                bi.morph_sliders_differing.append(name)
        bi.morph_sliders_compared = len(diffs)
        bi.morph_max_delta = round(max(diffs), 4) if diffs else 0.0
    bi.zaps_on_body = sorted(sl.name for sl in ss.sliders if sl.zap and any(dd.target == target for dd in sl.data))
    # what the replacement changes (informational): weights that differ from the target reference
    if ok.all():
        rb, rw = ref_shape.weight_matrix()
        col = lambda n, ww, b: ww[:, n.index(b)] if b in n else np.zeros(ww.shape[0])  # noqa: E731
        allb = sorted(set(bones) | set(rb))
        delta = np.max([np.abs(col(bones, w, b) - col(rb, rw[m], b)) for b in allb], axis=0) if allb else np.zeros(len(v))
        changed = {b: int((np.abs(col(bones, w, b) - col(rb, rw[m], b)) > 0.05).sum()) for b in allb}
        bi.weight_changes = {"vertices_changed": int((delta > 0.05).sum()),
                             "bones": {b: c for b, c in sorted(changed.items(), key=lambda x: -x[1]) if c}}

    # ---- evidence and decision -------------------------------------------------------------
    E, R = bi.evidence, bi.reasons
    E.append(f"{bi.coincident_fraction:.1%} of {bi.vertices} vertices coincide with the installed CBBE-3BBB reference "
             f"(max distance {bi.max_distance:.1e})")
    if ok.all():
        E.append(f"body is {'the full' if bi.reference_coverage >= 0.999 else 'a vertex subset of the'} reference body "
                 f"({bi.reference_coverage:.0%} of the reference present"
                 + ("; the rest was deleted by the outfit author and stays deleted)" if bi.reference_coverage < 0.999 else ")"))
        E.append(f"{bi.triangle_subset:.2%} of its triangles are reference triangles")
        E.append(f"{bi.morph_sliders_compared} slider morphs compared with the reference body: max offset difference "
                 f"{bi.morph_max_delta:.4f}")
    E.append(f"name/data folder hint: {'yes' if name_hint else 'no'} (weak evidence only)")
    if bi.existing_physics_bones:
        if ok.all() and not bi.weight_changes.get("vertices_changed"):
            bi.classification, bi.confidence = BODY_ALREADY_3BBB, "HIGH"
            R.append("the embedded body already carries the CBBE-3BBB reference weights: nothing to replace")
        else:
            bi.classification, bi.confidence = BODY_AMBIGUOUS, "LOW"
            R.append(f"the body already has 3BBB physics bones ({', '.join(bi.existing_physics_bones[:4])}) but not the "
                     "reference's weights: a custom 3BBB body, not replaced automatically")
        return bi
    if bi.coincident_fraction < 0.5:
        bi.classification, bi.confidence = BODY_UNSUPPORTED, "HIGH"
        R.append("the body does not correspond to the installed CBBE-3BBB reference (another body family or topology)")
        return bi
    problems = []
    if not ok.all():
        problems.append(f"{int((~ok).sum())} body vertices do not coincide with the reference (sculpted or custom body)")
    if ok.all() and bi.triangle_subset < TRI_SUBSET_MIN:
        problems.append(f"only {bi.triangle_subset:.2%} of the triangles are reference triangles (re-meshed body)")
    if bi.morph_sliders_differing:
        problems.append(f"slider morphs differ from the reference for {', '.join(bi.morph_sliders_differing[:5])}")
    if bi.body_only_sliders:
        problems.append(f"sliders with body morphs the reference does not have: {', '.join(bi.body_only_sliders[:5])}")
    if ok.all() and bi.morph_sliders_compared == 0 and body_morphs:
        problems.append("no slider morph of the body could be compared with the reference")
    if problems:
        bi.classification, bi.confidence = BODY_AMBIGUOUS, "LOW"
        R.extend(problems)
        return bi
    bi.classification, bi.confidence = BODY_COMPATIBLE, "HIGH"
    R.append("the embedded body is the installed CBBE body geometry (same topology as CBBE-3BBB)"
             + (", minus vertices the outfit author deleted" if bi.reference_coverage < 0.999 else "")
             + ", with the reference's slider morphs and no 3BBB weights")
    if bi.weight_changes.get("vertices_changed"):
        top = ", ".join(f"{b} ({c})" for b, c in list(bi.weight_changes["bones"].items())[:4])
        R.append(f"replacement changes the weights of {bi.weight_changes['vertices_changed']} body vertices to the "
                 f"CBBE-3BBB reference weights (most changed bones: {top})")
    if bi.legacy_bones:
        R.append(f"legacy physics bones {', '.join(bi.legacy_bones)} are replaced by the 3BBB bones")
    return bi
