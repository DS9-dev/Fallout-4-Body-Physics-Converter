"""0.4.3: post-export restoration of immutable vertex attributes (Direct-NIF).

Interactive Outfit Studio 5.8.2 recomputes the normals of every loaded shape (``Mesh::SmoothNormals``: seam vertices
closer than 60° are averaged) and ``ExportNIF`` writes them with regenerated tangents/bitangents (RESEARCH.md §12).
In Direct-NIF mode Outfit Studio is only the *skinning backend*; it may not change authored visual vertex data.

    FINAL vertex = SOURCE immutable vertex bytes + RAW-EXPORT skin weights and bone indices

Safety, in this order:

1. **Structural gate** (no patch unless vertex N of the source is provably vertex N of the export): same shapes in
   the same order and nothing added (no reference body); per shape the same block type, vertex count, identical
   vertex descriptor (layout and stride), identical triangles, identical segment data, material, alpha and transform;
   same extra data and block types; no cloth data. Otherwise ``POST_EXPORT_RESTORE_UNSAFE``.
2. **Raw diff policy**: comparing source and raw export field by field, the only differences allowed are the skin
   weights / bone indices and the normal + tangent basis (``OS_REGENERATED_NORMALS`` /
   ``OS_REGENERATED_TANGENT_BASIS``). Any other difference (position, UV, colors, eye/landscape data, unknown bytes)
   → ``RAW_EXPORT_UNEXPECTED_CHANGE``: nothing is restored, so real corruption is never hidden.
3. **Patch** into a temporary file: only the vertex-buffer bytes of the immutable fields are rewritten; every other
   byte of the raw export is kept.
4. **Proof** on the re-parsed temporary file: immutable fields == source (byte-exact), skinning == raw export
   (byte-exact), and no byte outside the immutable vertex ranges differs from the raw export. Only then the file is
   renamed to the final name. The raw export itself is never modified.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from ..nif.reader import NifError, read_nif
from ..nif.structure import NifStructure, read_structure, vertex_layout

__all__ = ["RestoreResult", "restore_immutable_vertices", "MUTABLE", "OS_REGENERATED", "REFUSED_UNSAFE",
           "REFUSED_UNEXPECTED"]

MUTABLE = ("skin_weights", "bone_indices")  # the only fields a Direct-NIF conversion may change
OS_REGENERATED = ("normal", "tangent", "bitangent_x", "bitangent_y", "bitangent_z")  # restored from the source
REFUSED_UNSAFE = "POST_EXPORT_RESTORE_UNSAFE"
REFUSED_UNEXPECTED = "RAW_EXPORT_UNEXPECTED_CHANGE"


@dataclass
class RestoreResult:
    status: str = "NOT_RUN"  # RESTORED | REFUSED | ERROR
    code: str = ""  # '' | POST_EXPORT_RESTORE_UNSAFE | RAW_EXPORT_UNEXPECTED_CHANGE | RESTORE_VERIFY_FAILED
    reasons: list[str] = field(default_factory=list)
    source_sha256: str = ""
    raw_sha256: str = ""
    final_sha256: str = ""
    shapes: dict = field(default_factory=dict)  # name → per-shape facts
    bytes_restored: int = 0
    other_raw_differences: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "RESTORED"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _gate(src: NifStructure, raw: NifStructure, reference_shape: str) -> list[str]:
    """Why vertex N of the source can NOT be mapped to vertex N of the raw export (empty = safe)."""
    p = []
    sn, rn = [s.name for s in src.shapes], [s.name for s in raw.shapes]
    if sn != rn:
        p.append(f"shape set/order differs: source {sn}, export {rn}")
    if reference_shape and reference_shape in rn and reference_shape not in sn:
        p.append(f"the reference body '{reference_shape}' is in the export")
    if src.root_name != raw.root_name:
        p.append(f"root node renamed '{src.root_name}' → '{raw.root_name}'")
    for s in src.shapes:
        r = raw.shape(s.name)
        if r is None:
            continue
        tag = f"shape '{s.name}'"
        if s.block_type != r.block_type:
            p.append(f"{tag}: block type {s.block_type} → {r.block_type}")
        if s.num_vertices != r.num_vertices or s.num_triangles != r.num_triangles:
            p.append(f"{tag}: counts {s.num_vertices} v / {s.num_triangles} t → {r.num_vertices} v / {r.num_triangles} t")
        if s.vertex_desc != r.vertex_desc or s.vertex_size != r.vertex_size or \
                vertex_layout(s.vertex_desc, s.vertex_size) != vertex_layout(r.vertex_desc, r.vertex_size):
            p.append(f"{tag}: vertex layout {s.vertex_desc:#x}/{s.vertex_size} B → {r.vertex_desc:#x}/{r.vertex_size} B")
        if s.triangles is None or r.triangles is None or s.triangles.shape != r.triangles.shape or \
                not np.array_equal(s.triangles, r.triangles):
            p.append(f"{tag}: triangles / topology differ")
        if s.tail_hash != r.tail_hash:
            p.append(f"{tag}: segment / partition data differ")
        if s.shader != r.shader:
            p.append(f"{tag}: material / shader differs")
        if s.alpha != r.alpha:
            p.append(f"{tag}: alpha property differs")
        if (s.translation, s.rotation, s.scale) != (r.translation, r.rotation, r.scale):
            p.append(f"{tag}: transform differs")
    if sorted(src.extra_data) != sorted(raw.extra_data):
        p.append("extra data blocks differ")
    skip = {"NiNode", "BSSkin::Instance", "BSSkin::BoneData", "BSFadeNode"}
    if {k: v for k, v in src.block_types.items() if k not in skip} != {k: v for k, v in raw.block_types.items() if k not in skip}:
        p.append("block types differ (other than bone nodes and skin blocks)")
    if src.block_types.get("BSClothExtraData") or raw.block_types.get("BSClothExtraData"):
        p.append("cloth data present (not supported by Direct-NIF)")
    return p


def seam_vertices(s) -> np.ndarray:
    """Vertices that share their position with another vertex of the shape (UV/normal seams), matched like Outfit
    Studio's ``SortingMatcher`` (epsilon 1e-6 × the largest coordinate)."""
    from scipy.spatial import cKDTree

    raw = s.fields.get("position")
    if raw is None or not len(raw):
        return np.zeros(s.num_vertices, dtype=bool)
    pos = (raw.copy().view(np.float32) if raw.shape[1] == 12 else raw.copy().view(np.float16)).astype(np.float64).reshape(-1, 3)
    eps = max(1e-6 * float(np.abs(pos).max()), 1e-9)
    out = np.zeros(len(pos), dtype=bool)
    for i, j in cKDTree(pos).query_pairs(eps, p=np.inf):
        out[i] = out[j] = True
    return out


def _raw_diff(src: NifStructure, raw: NifStructure) -> tuple[dict, list[str]]:
    """Per shape: {field: vertices differing}; plus the forbidden differences.

    Normal / tangent-basis differences are only attributable to Outfit Studio's export when they are on duplicated
    seam vertices (``Mesh::SmoothNormals`` welds seams; every other normal is copied from the file). A change anywhere
    else is an unexpected change, not an exporter side effect."""
    facts, forbidden = {}, []
    for s in src.shapes:
        r = raw.shape(s.name)
        d = {}
        basis = np.zeros(s.num_vertices, dtype=bool)
        for k, a in s.fields.items():
            b = r.fields.get(k)
            rows = (a != b).any(1) if b is not None and a.shape == b.shape else np.ones(s.num_vertices, dtype=bool)
            if rows.any():
                d[k] = int(rows.sum())
            if k in OS_REGENERATED:
                basis |= rows
        facts[s.name] = d
        bad = {k: n for k, n in d.items() if k not in MUTABLE and k not in OS_REGENERATED}
        if bad:
            forbidden.append(f"shape '{s.name}': " + ", ".join(f"{k} changed on {n} vertices" for k, n in bad.items()))
        if basis.any():
            off_seam = np.nonzero(basis & ~seam_vertices(s))[0]
            if len(off_seam):
                forbidden.append(f"shape '{s.name}': normals/tangent basis changed on {len(off_seam)} vertices that are not "
                                 f"duplicated seam vertices (e.g. {', '.join(map(str, off_seam[:6]))}): not Outfit Studio's "
                                 "seam smoothing")
    return facts, forbidden


def restore_immutable_vertices(source: Path, raw: Path, final: Path, reference_shape: str = "") -> RestoreResult:
    """Write ``final`` = ``raw`` with every immutable vertex byte taken from ``source``. Never modifies ``raw``."""
    res = RestoreResult()
    source, raw, final = Path(source), Path(raw), Path(final)
    if final.exists():
        final.unlink()  # a stale candidate from an earlier run must never survive a refusal
    try:
        res.source_sha256, res.raw_sha256 = _sha(source), _sha(raw)
        src_st, raw_st = read_structure(source), read_structure(raw)
    except (NifError, OSError, ValueError, IndexError, KeyError) as exc:
        res.status, res.code = "REFUSED", REFUSED_UNSAFE
        res.reasons.append(f"cannot parse source or raw export: {exc}")
        return res
    # 1. structural gate
    gate = _gate(src_st, raw_st, reference_shape)
    if gate:
        res.status, res.code, res.reasons = "REFUSED", REFUSED_UNSAFE, gate
        return res
    # 2. raw diff policy
    diffs, forbidden = _raw_diff(src_st, raw_st)
    for name, d in diffs.items():
        res.shapes[name] = {
            "raw_differences": d,
            "os_regenerated_normals": d.get("normal", 0),
            "os_regenerated_tangent_basis": max((d.get(k, 0) for k in ("tangent", "bitangent_x", "bitangent_y", "bitangent_z")),
                                                default=0),
            "skin_changed_vertices": max(d.get("skin_weights", 0), d.get("bone_indices", 0)),
        }
    if forbidden:
        res.status, res.code, res.other_raw_differences = "REFUSED", REFUSED_UNEXPECTED, True
        res.reasons = forbidden
        return res
    # 3. patch a copy of the raw bytes
    data = bytearray(raw.read_bytes())
    src_bytes = source.read_bytes()
    allowed = np.zeros(len(data), dtype=bool)  # byte positions the patch may touch
    for s in src_st.shapes:
        r = raw_st.shape(s.name)
        lay = vertex_layout(r.vertex_desc, r.vertex_size)
        keep = np.zeros(r.vertex_size, dtype=bool)  # skin bytes stay from the raw export
        for k in MUTABLE:
            if k in lay:
                lo, hi = lay[k]
                keep[lo:hi] = True
        src_v = np.frombuffer(src_bytes, np.uint8, s.num_vertices * s.vertex_size, s.vertex_offset).reshape(s.num_vertices, -1)
        raw_v = np.frombuffer(bytes(data), np.uint8, r.num_vertices * r.vertex_size, r.vertex_offset).reshape(r.num_vertices, -1)
        fin_v = np.where(keep[None, :], raw_v, src_v)
        res.bytes_restored += int((fin_v != raw_v).sum())
        data[r.vertex_offset:r.vertex_offset + fin_v.size] = fin_v.tobytes()
        span = np.zeros(r.num_vertices * r.vertex_size, dtype=bool).reshape(r.num_vertices, -1)
        span[:, ~keep] = True
        allowed[r.vertex_offset:r.vertex_offset + span.size] = span.ravel()
    tmp = final.with_name(final.name + ".restore-tmp")
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_bytes(bytes(data))
        # 4. proof on the independently re-parsed temporary file
        problems = []
        chk = bytes(tmp.read_bytes())
        raw_bytes = raw.read_bytes()
        if len(chk) != len(raw_bytes):
            problems.append("restored file size differs from the raw export")
        else:
            outside = np.nonzero((np.frombuffer(chk, np.uint8) != np.frombuffer(raw_bytes, np.uint8)) & ~allowed)[0]
            if len(outside):
                problems.append(f"{len(outside)} bytes outside the immutable vertex fields differ from the raw export")
        read_nif(tmp)
        fin_st = read_structure(tmp)
        for s in src_st.shapes:
            f, r = fin_st.shape(s.name), raw_st.shape(s.name)
            if f is None:
                problems.append(f"shape '{s.name}' missing after restoration")
                continue
            imm = [k for k in s.fields if k not in MUTABLE]
            bad_imm = [k for k in imm if not np.array_equal(s.fields[k], f.fields.get(k))]
            bad_mut = [k for k in MUTABLE if k in r.fields and not np.array_equal(r.fields[k], f.fields.get(k))]
            if bad_imm:
                problems.append(f"shape '{s.name}': restored {bad_imm} still differ from the source")
            if bad_mut:
                problems.append(f"shape '{s.name}': skinning {bad_mut} differs from the raw export")
            res.shapes[s.name].update({"immutable_fields_equal_source": not bad_imm, "skinning_equal_raw_export": not bad_mut,
                                       "fields_restored": imm})
        if problems:
            res.status, res.code, res.reasons = "ERROR", "RESTORE_VERIFY_FAILED", problems
            tmp.unlink()
            return res
        os.replace(tmp, final)
    except Exception as exc:  # noqa: BLE001 - never leave a partial candidate
        if tmp.exists():
            tmp.unlink()
        if final.exists():
            final.unlink()
        res.status, res.code = "ERROR", "RESTORE_VERIFY_FAILED"
        res.reasons.append(f"restoration failed: {exc}")
        return res
    res.final_sha256 = _sha(final)
    res.status = "RESTORED"
    return res
