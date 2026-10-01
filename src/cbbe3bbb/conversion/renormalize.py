"""0.5.1: post-export proportional skin-weight renormalization (BodySlide-project conversions).

Outfit Studio 5.8.2 saves skinning through nifly ``NifFile::SetShapeVertWeights``: it divides a vertex's weights by
the sum over *all* its bones and then stores only the first four (the largest). After CopyBoneWeights gave a vertex
more than four bones, the stored weights sum to less than 1 (real CP5: 0.848–0.95), and BodySlide builds keep that
(RESEARCH.md §15). This module rescales exactly those stored weights:

    new_weight_i = old_weight_i / sum(old_weights)          (only the stored 1–4 weights of that vertex)

Strict by construction:

* only the converted (planned) shapes, only the half-float skin-weight bytes of the repaired vertices change; bone
  indices, every other vertex byte (positions, UVs, normals/tangents, colors …), triangles, segments/partitions,
  materials, extra data and the project file (sliders/zaps) are not touched — proven byte for byte on the re-read file;
* a vertex is repaired only if it has the truncation signature: exactly four non-zero weights and a sum in
  [0.5, 1 − tolerance). Every weight already summing to 1 (± tolerance) is left untouched;
* anything else is refused for the whole file and nothing is written (``RENORMALIZE_REFUSED``): a zero sum, a negative,
  NaN or infinite weight, a sum above 1, a deficit on a vertex with fewer than four influences (not explained by the
  truncation), a sum below 0.5, an unreadable file. The strict validation then reports the original problem;
* the raw Outfit Studio file is kept as evidence (``raw_path``); the repaired file is re-read from disk and must equal
  the raw file everywhere except the rescaled weights, with the same zero pattern and the same proportions;
* the full existing validation runs afterwards on the repaired file, with unchanged thresholds. The repair cannot
  make any other check pass: it changes nothing those checks read except the weight magnitudes.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from ..nif.reader import NifError, read_nif
from ..nif.structure import read_structure, vertex_layout

__all__ = ["RenormalizeResult", "renormalize_skin_weights", "SUM_TOLERANCE", "MIN_REPAIRABLE_SUM", "REFUSED"]

SUM_TOLERANCE = 0.002  # |sum − 1| within this: already normalized (half-float storage), never touched
MIN_REPAIRABLE_SUM = 0.5  # below: not a truncation deficit (the validator reports it as a vertex without weight)
RATIO_TOLERANCE = 2e-3  # proportions preserved within half-float precision
REFUSED = "RENORMALIZE_REFUSED"


@dataclass
class RenormalizeResult:
    status: str = "NOT_NEEDED"  # NOT_NEEDED | RENORMALIZED | REFUSED | ERROR
    code: str = ""
    reasons: list[str] = field(default_factory=list)
    raw_sha256: str = ""
    final_sha256: str = ""
    raw_path: str = ""
    vertices_renormalized: int = 0
    shapes: dict = field(default_factory=dict)  # name → {"vertices", "min_sum_before", "max_sum_before", …}

    @property
    def changed(self) -> bool:
        return self.status == "RENORMALIZED"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["changed"] = self.changed
        return d


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _weights(sw: np.ndarray) -> np.ndarray:
    """(nv, 8) uint8 skin-weight bytes → (nv, 4) float64 (little-endian half floats)."""
    return np.ascontiguousarray(sw).view("<f2").astype(np.float64).reshape(-1, 4)


def analyze_vertices(w: np.ndarray, tol: float = SUM_TOLERANCE) -> tuple[np.ndarray, list[str]]:
    """(vertices to repair, refusal reasons) for one shape's (nv, 4) weights."""
    problems = []
    finite = np.isfinite(w).all(1)
    if not finite.all():
        idx = np.nonzero(~finite)[0]
        problems.append(f"{len(idx)} vertices with NaN/Inf weights (e.g. {', '.join(map(str, idx[:6]))})")
    wf = np.where(np.isfinite(w), w, 0.0)
    neg = (wf < 0).any(1)
    if neg.any():
        idx = np.nonzero(neg)[0]
        problems.append(f"{len(idx)} vertices with negative weights (e.g. {', '.join(map(str, idx[:6]))})")
    s = wf.sum(1)
    nz = (wf > 0).sum(1)
    zero = finite & (s <= 0)
    if zero.any():
        idx = np.nonzero(zero)[0]
        problems.append(f"{len(idx)} vertices with zero weight sum (e.g. {', '.join(map(str, idx[:6]))})")
    over = finite & (s > 1 + tol)
    if over.any():
        idx = np.nonzero(over)[0]
        problems.append(f"{len(idx)} vertices with weight sum above 1 (max {s[over].max():.4f})")
    low = finite & ~neg & (s > 0) & (s < 1 - tol)
    not_trunc = low & (nz < 4)
    if not_trunc.any():
        idx = np.nonzero(not_trunc)[0]
        problems.append(f"{len(idx)} vertices with a weight deficit but fewer than 4 influences (not the 4-influence "
                        f"truncation; e.g. {', '.join(map(str, idx[:6]))})")
    too_low = low & (s < MIN_REPAIRABLE_SUM)
    if too_low.any():
        idx = np.nonzero(too_low)[0]
        problems.append(f"{len(idx)} vertices with a weight sum below {MIN_REPAIRABLE_SUM} (min {s[too_low].min():.4f})")
    repair = low & (nz == 4) & (s >= MIN_REPAIRABLE_SUM)
    return repair, problems


def renormalize_skin_weights(path: Path, raw_copy: Path, shapes: Iterable[str],
                             tol: float = SUM_TOLERANCE) -> RenormalizeResult:
    """Repair ``path`` in place (the Outfit Studio output); keep the untouched original as ``raw_copy``.

    Only ``shapes`` (the converted shapes of the plan) are considered. The file's modification time is kept, so the
    execution evidence still attributes it to the Outfit Studio session; the raw copy and both hashes are the record."""
    res = RenormalizeResult()
    path, raw_copy = Path(path), Path(raw_copy)
    want = set(shapes)
    try:
        res.raw_sha256 = _sha(path)
        st = read_structure(path)
    except (NifError, OSError, ValueError, IndexError, KeyError) as exc:
        res.status, res.code = "REFUSED", REFUSED
        res.reasons.append(f"cannot parse the exported NIF: {exc}")
        return res
    plan: dict[str, tuple[object, np.ndarray, np.ndarray]] = {}
    problems: list[str] = []
    for s in st.shapes:
        if s.name not in want:
            continue
        sw = s.fields.get("skin_weights")
        if sw is None or not len(sw):
            continue
        w = _weights(sw)
        repair, prob = analyze_vertices(w, tol)
        problems += [f"shape '{s.name}': {p}" for p in prob]
        if repair.any():
            plan[s.name] = (s, w, repair)
    if problems:
        res.status, res.code, res.reasons = "REFUSED", REFUSED, problems
        return res
    if not plan:
        return res  # NOT_NEEDED: nothing changed, no raw copy
    data = bytearray(path.read_bytes())
    allowed = np.zeros(len(data), dtype=bool)
    expected: dict[str, np.ndarray] = {}
    for name, (s, w, repair) in plan.items():
        lo, hi = vertex_layout(s.vertex_desc, s.vertex_size)["skin_weights"]
        sums = w.sum(1)
        new = w.copy()
        new[repair] = w[repair] / sums[repair, None]
        half = new.astype("<f2")
        vb = np.frombuffer(bytes(data), np.uint8, s.num_vertices * s.vertex_size, s.vertex_offset).reshape(s.num_vertices, -1).copy()
        vb[repair, lo:hi] = half[repair].view(np.uint8).reshape(-1, 8)
        data[s.vertex_offset:s.vertex_offset + vb.size] = vb.tobytes()
        span = np.zeros((s.num_vertices, s.vertex_size), dtype=bool)
        span[np.nonzero(repair)[0], lo:hi] = True
        allowed[s.vertex_offset:s.vertex_offset + span.size] = span.ravel()
        expected[name] = new
        res.shapes[name] = {"vertices": int(repair.sum()), "vertex_count": int(s.num_vertices),
                            "min_sum_before": round(float(sums[repair].min()), 4),
                            "max_sum_before": round(float(sums[repair].max()), 4),
                            "mean_sum_before": round(float(sums[repair].mean()), 4)}
        res.vertices_renormalized += int(repair.sum())
    tmp = path.with_name(path.name + ".renorm-tmp")
    stat = path.stat()
    try:
        tmp.write_bytes(bytes(data))
        # proof on the re-read temporary file
        chk = np.frombuffer(tmp.read_bytes(), np.uint8)
        orig = np.frombuffer(path.read_bytes(), np.uint8)
        bad = []
        if len(chk) != len(orig):
            bad.append("file size changed")
        else:
            outside = np.nonzero((chk != orig) & ~allowed)[0]
            if len(outside):
                bad.append(f"{len(outside)} bytes outside the repaired skin weights changed")
        read_nif(tmp)
        st2 = read_structure(tmp)
        for s in st.shapes:
            f = st2.shape(s.name)
            if f is None:
                bad.append(f"shape '{s.name}' missing after the repair")
                continue
            for k, a in s.fields.items():
                if k == "skin_weights" and s.name in plan:
                    continue
                if not np.array_equal(a, f.fields.get(k)):
                    bad.append(f"shape '{s.name}': {k} changed")
            if s.name in plan:
                _s, w, repair = plan[s.name]
                w2 = _weights(f.fields["skin_weights"])
                if not np.array_equal(w2 > 0, w > 0):
                    bad.append(f"shape '{s.name}': the set of non-zero weights changed")
                if not np.array_equal(w2[~repair], w[~repair]):
                    bad.append(f"shape '{s.name}': weights of vertices that needed no repair changed")
                s2 = w2[repair].sum(1)
                if len(s2) and float(np.abs(s2 - 1).max()) > tol:
                    bad.append(f"shape '{s.name}': repaired sums not 1 (max |Δ| {float(np.abs(s2 - 1).max()):.4f})")
                ratio = np.abs(w2[repair] - w[repair] / w[repair].sum(1, keepdims=True))
                if len(ratio) and float(ratio.max()) > RATIO_TOLERANCE:
                    bad.append(f"shape '{s.name}': proportions not preserved (max {float(ratio.max()):.5f})")
                res.shapes[s.name]["max_sum_error_after"] = round(float(np.abs(s2 - 1).max()) if len(s2) else 0.0, 5)
        if bad:
            tmp.unlink()
            res.status, res.code, res.reasons = "ERROR", "RENORMALIZE_VERIFY_FAILED", bad
            return res
        raw_copy.parent.mkdir(parents=True, exist_ok=True)
        if not raw_copy.exists():
            shutil.copy2(path, raw_copy)
        os.replace(tmp, path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except Exception as exc:  # noqa: BLE001 - the Outfit Studio output must stay as it was
        if tmp.exists():
            tmp.unlink()
        res.status, res.code = "ERROR", "RENORMALIZE_VERIFY_FAILED"
        res.reasons.append(f"repair failed: {exc}")
        return res
    res.raw_path = str(raw_copy)
    res.final_sha256 = _sha(path)
    res.status = "RENORMALIZED"
    return res
