"""Read-only structural inventory of a Fallout 4 NIF (0.4.1, Direct-NIF validation).

``nif.reader`` extracts what the analyzer needs (geometry and skin weights). A Direct-NIF conversion writes a
new game mesh, so validation must also prove that everything *else* survived Outfit Studio's export: vertex
attributes other than skinning (normals, UVs, tangents, colors), triangles, the per-shape tail data
(BSSubIndexTriShape segments), shader/material and texture-set data, alpha properties and extra data blocks.
This module collects those facts in a form that can be compared between the source and the generated NIF,
independently of block numbering (Outfit Studio renumbers blocks and adds bone nodes).
"""
from __future__ import annotations

import hashlib
import struct

import numpy as np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .reader import NifError, TRISHAPE_TYPES, _parse_header, _read_av_object, _Stream, _string

__all__ = ["ShapeStructure", "NifStructure", "read_structure", "compare_structures"]

VA_VERTEX, VA_UV, VA_UV2, VA_NORMAL, VA_TANGENT, VA_COLORS = 0x001, 0x002, 0x004, 0x008, 0x010, 0x020
VA_SKINNED, VA_LANDDATA, VA_EYEDATA, VA_FULLPREC = 0x040, 0x080, 0x100, 0x400
SKIN_BYTES = 12  # 4 half-float weights + 4 bone indices

# 0.4.2: every per-vertex field of a Fallout 4 vertex, decoded separately. The skin weights and bone indices are
# the only mutable part of a physics conversion; everything else must survive byte for byte.
IMMUTABLE_FIELDS = ("position", "bitangent_x", "uv", "uv2", "normal", "bitangent_y", "tangent", "bitangent_z", "color",
                    "land", "eye", "other")
MUTABLE_FIELDS = ("skin_weights", "bone_indices")


def vertex_layout(desc: int, vsize: int) -> dict[str, tuple[int, int]]:
    """Byte range (start, end) of every field inside one vertex, from the BSVertexDesc.

    Bytes that no known field covers are reported as 'other' (compared too, so nothing escapes the check)."""
    flags = (desc >> 44) & 0xFFF
    off = lambda sh: ((desc >> sh) & 0xF) * 4  # noqa: E731
    lay: dict[str, tuple[int, int]] = {}
    if flags & VA_VERTEX:
        if flags & VA_FULLPREC:
            lay["position"], lay["bitangent_x"] = (0, 12), (12, 16)
        else:
            lay["position"], lay["bitangent_x"] = (0, 6), (6, 8)
    if flags & VA_UV:
        lay["uv"] = (off(8), off(8) + 4)
    if flags & VA_UV2:
        lay["uv2"] = (off(12), off(12) + 4)
    if flags & VA_NORMAL:
        lay["normal"], lay["bitangent_y"] = (off(16), off(16) + 3), (off(16) + 3, off(16) + 4)
    if flags & VA_TANGENT:
        lay["tangent"], lay["bitangent_z"] = (off(20), off(20) + 3), (off(20) + 3, off(20) + 4)
    if flags & VA_COLORS:
        lay["color"] = (off(24), off(24) + 4)
    if flags & VA_SKINNED:
        lay["skin_weights"], lay["bone_indices"] = (off(28), off(28) + 8), (off(28) + 8, off(28) + 12)
    if flags & VA_LANDDATA:
        lay["land"] = (off(32), off(32) + 4)
    if flags & VA_EYEDATA:
        lay["eye"] = (off(36), off(36) + 4)
    return lay


def _field_arrays(vbytes: bytes, nv: int, vsize: int, desc: int) -> dict[str, np.ndarray]:
    """Per-field (nv, width) uint8 arrays; 'other' holds the bytes of the vertex no known field covers."""
    if not nv or not vsize:
        return {}
    a = np.frombuffer(vbytes, dtype=np.uint8)[: nv * vsize].reshape(nv, vsize)
    lay = vertex_layout(desc, vsize)
    covered = np.zeros(vsize, dtype=bool)
    out = {}
    for k, (lo, hi) in lay.items():
        hi = min(hi, vsize)
        covered[lo:hi] = True
        out[k] = a[:, lo:hi].copy()
    out["other"] = a[:, ~covered].copy()
    return out


@dataclass
class ShapeStructure:
    name: str
    block_type: str
    translation: tuple = ()
    rotation: tuple = ()
    scale: float = 1.0
    vertex_desc_flags: int = 0
    vertex_size: int = 0
    num_vertices: int = 0
    num_triangles: int = 0
    vertex_hash: str = ""  # every vertex attribute except the skin weights/indices
    vertex_desc: int = 0
    fields: dict = field(default_factory=dict, repr=False)  # 0.4.2: field name → (nv, width) uint8 array
    triangles: object = field(default=None, repr=False)  # (nt, 3) uint16 array
    vertex_offset: int = 0  # 0.4.3: absolute file offset of the vertex buffer (post-export restoration)
    triangle_hash: str = ""
    tail_hash: str = ""  # bytes after the triangles (BSSubIndexTriShape segment data, particle data size)
    tail_size: int = 0
    shader: dict = field(default_factory=dict)  # type, name, textures, params hash
    alpha: str = ""  # hash of the NiAlphaProperty parameters ('' when none)
    extra_data: list = field(default_factory=list)


@dataclass
class NifStructure:
    root_name: str = ""
    shapes: list[ShapeStructure] = field(default_factory=list)
    extra_data: list = field(default_factory=list)  # (type, name, payload hash) of every *ExtraData block
    block_types: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def shape(self, name: str) -> Optional[ShapeStructure]:
        return next((s for s in self.shapes if s.name == name), None)


def _h(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _block_name(data: bytes, off: int, hdr) -> str:
    return _string(hdr, struct.unpack_from("<i", data, off)[0]) or ""


def _shader(data: bytes, off: int, size: int, btype: str, hdr, offsets, sizes, types) -> dict:
    """FO4 BSLightingShaderProperty / BSEffectShaderProperty → comparable facts (refs resolved, not compared)."""
    end = off + size
    p = off
    out: dict = {"type": btype}
    if btype == "BSLightingShaderProperty":
        out["shader_type"] = struct.unpack_from("<I", data, p)[0]
        p += 4
    out["name"] = _block_name(data, p, hdr)
    p += 4
    n_extra = struct.unpack_from("<I", data, p)[0]
    p += 4 + 4 * n_extra
    p += 4  # controller
    if btype == "BSLightingShaderProperty":
        flags_uv = data[p:p + 24]  # flags1, flags2, uv offset, uv scale
        p += 24
        tex_ref = struct.unpack_from("<i", data, p)[0]
        p += 4
        rest = data[p:end]
        if hdr.bs_version == 130 and len(rest) >= 20:  # emissive color + multiple (16 bytes), then the wet material string
            out["wet_material"] = _block_name(data, p + 16, hdr)
            rest = rest[:16] + rest[20:]
        out["params"] = _h(flags_uv + rest)
        if 0 <= tex_ref < len(offsets) and types[tex_ref] == "BSShaderTextureSet":
            q = offsets[tex_ref]
            n = struct.unpack_from("<I", data, q)[0]
            q += 4
            tex = []
            for _ in range(min(n, 64)):
                ln = struct.unpack_from("<I", data, q)[0]
                tex.append(data[q + 4:q + 4 + ln].decode("latin1"))
                q += 4 + ln
            out["textures"] = tex
        else:
            out["textures"] = None
    else:
        out["params"] = _h(data[p:end])
    return out


def read_structure(path: str | Path) -> NifStructure:
    """Parse the comparable structure of a NIF. Raises NifError for unreadable files."""
    data = Path(path).read_bytes()
    s = _Stream(data)
    hdr = _parse_header(s)
    offsets, pos = [], s.pos
    for size in hdr.block_sizes:
        offsets.append(pos)
        pos += size
    if pos > len(data):
        raise NifError(f"file truncated: blocks need {pos} bytes, file has {len(data)}")
    types = [hdr.block_types[i] if i < len(hdr.block_types) else "?" for i in hdr.block_type_index]
    st = NifStructure()
    for t in types:
        st.block_types[t] = st.block_types.get(t, 0) + 1
    for i, (off, size, t) in enumerate(zip(offsets, hdr.block_sizes, types)):
        if i == 0:
            st.root_name = _block_name(data, off, hdr)
        if t.endswith("ExtraData") and t != "BSClothExtraData":
            if t == "NiStringExtraData" and size >= 8:  # the value is a string-table index: compare the string itself
                payload = ("str:" + _block_name(data, off + 4, hdr)).encode("utf-8", "replace") + data[off + 8:off + size]
            else:
                payload = data[off + 4:off + size]
            st.extra_data.append((t, _block_name(data, off, hdr), _h(payload)))
        if t not in TRISHAPE_TYPES:
            continue
        bs = _Stream(data, off, off + size)
        av = _read_av_object(bs, hdr)
        bs.f32(4)
        if hdr.bs_version >= 155:
            bs.f32(6)
        bs.i32()  # skin
        shader_ref = bs.i32()
        alpha_ref = bs.i32()
        desc = bs.u64()
        ntri = bs.u32() if hdr.bs_version >= 130 else bs.u16()
        nv = bs.u16()
        bs.u32()  # data size
        flags = (desc >> 44) & 0xFFF
        vsize = (desc & 0xF) * 4
        vertex_offset = bs.pos
        vbytes = data[bs.pos:bs.pos + vsize * nv]
        raw_vertices = vbytes
        bs.pos += vsize * nv
        if flags & VA_SKINNED and vsize:
            so = ((desc >> 28) & 0xF) * 4
            masked = bytearray()
            for k in range(nv):
                row = vbytes[k * vsize:(k + 1) * vsize]
                masked += row[:so] + row[so + SKIN_BYTES:]
            vbytes = bytes(masked)
        tbytes = data[bs.pos:bs.pos + 6 * ntri]
        bs.pos += 6 * ntri
        tail = data[bs.pos:off + size]
        ss = ShapeStructure(name=av["name"], block_type=t, translation=tuple(round(float(x), 5) for x in av["translation"]),
                            rotation=tuple(round(float(x), 5) for x in av["rotation"].ravel()), scale=round(av["scale"], 5),
                            vertex_desc_flags=int(flags), vertex_size=vsize, num_vertices=nv, num_triangles=ntri,
                            vertex_hash=_h(vbytes), triangle_hash=_h(tbytes), tail_hash=_h(tail), tail_size=len(tail),
                            vertex_desc=int(desc), fields=_field_arrays(raw_vertices, nv, vsize, desc),
                            vertex_offset=vertex_offset,
                            triangles=np.frombuffer(tbytes, dtype="<u2").reshape(-1, 3).copy() if ntri else np.zeros((0, 3), "<u2"))
        if 0 <= shader_ref < len(types) and types[shader_ref] in ("BSLightingShaderProperty", "BSEffectShaderProperty"):
            ss.shader = _shader(data, offsets[shader_ref], hdr.block_sizes[shader_ref], types[shader_ref], hdr,
                                offsets, hdr.block_sizes, types)
        if 0 <= alpha_ref < len(types) and types[alpha_ref] == "NiAlphaProperty":
            a_off, a_size = offsets[alpha_ref], hdr.block_sizes[alpha_ref]
            ne = struct.unpack_from("<I", data, a_off + 4)[0]
            ss.alpha = _h(data[a_off + 8 + 4 * ne + 4:a_off + a_size])
        st.shapes.append(ss)
    return st


def compare_structures(src: NifStructure, gen: NifStructure, weights_may_change: set[str]) -> list[tuple[str, bool, str]]:
    """(check name, ok, detail) for everything that must survive a physics-only conversion.

    ``weights_may_change``: shapes whose skin weights were rewritten (only their skin bytes may differ; the
    vertex hash excludes skin data for every shape)."""
    out = []
    sn, gn = [s.name for s in src.shapes], [s.name for s in gen.shapes]
    out.append(("shape set and order identical (no missing, added, renamed or reference shape)", sn == gn,
                f"source {sn}; generated {gn}"))
    out.append(("root node name preserved", src.root_name == gen.root_name, f"'{src.root_name}' → '{gen.root_name}'"))
    for s in src.shapes:
        g = gen.shape(s.name)
        if g is None:
            continue
        out += compare_vertex_fields(s, g)
        out.append((f"{s.name}: transform preserved", (s.translation, s.rotation, s.scale) == (g.translation, g.rotation, g.scale),
                    f"translation {g.translation}"))
        out.append((f"{s.name}: segment / tail data identical ({s.block_type})", s.block_type == g.block_type and s.tail_hash == g.tail_hash,
                    f"{g.block_type} {g.tail_size} bytes (source {s.block_type} {s.tail_size} bytes)"))
        out.append((f"{s.name}: material / shader and textures identical", s.shader == g.shader,
                    f"{g.shader.get('name', '—')}" + ("" if s.shader == g.shader else f" (source {s.shader.get('name', '—')})")))
        out.append((f"{s.name}: alpha property identical", s.alpha == g.alpha, "same" if s.alpha == g.alpha else "DIFFERENT"))
    se, ge = sorted(src.extra_data), sorted(gen.extra_data)
    out.append(("extra data blocks preserved", se == ge, f"source {len(se)}, generated {len(ge)}"
                + ("" if se == ge else f"; missing {[x[:2] for x in se if x not in ge][:3]}, new {[x[:2] for x in ge if x not in se][:3]}")))
    skip = {"NiNode", "BSSkin::Instance", "BSSkin::BoneData", "BSFadeNode"}
    sb = {k: v for k, v in src.block_types.items() if k not in skip}
    gb = {k: v for k, v in gen.block_types.items() if k not in skip}
    out.append(("block types preserved (bone nodes and skin blocks aside)", sb == gb, f"source {sb}; generated {gb}"))
    return out


def _normal_angles(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Angle in degrees between u8-encoded directions (nifly: byte = round((x + 1) / 2 * 255))."""
    def dec(x):
        v = x.astype(np.float64) / 255.0 * 2.0 - 1.0
        n = np.linalg.norm(v, axis=1, keepdims=True)
        return v / np.where(n > 0, n, 1.0)
    return np.degrees(np.arccos(np.clip((dec(a) * dec(b)).sum(1), -1.0, 1.0)))


def _rows(diff: np.ndarray) -> str:
    idx = np.nonzero(diff)[0]
    return ", ".join(str(int(i)) for i in idx[:10]) + (" …" if len(idx) > 10 else "")


def compare_vertex_fields(s: "ShapeStructure", g: "ShapeStructure") -> list[tuple[str, bool, str]]:
    """Per-attribute comparison of one shape's vertex buffer (0.4.2).

    Each immutable attribute (position, UV, normal, tangent/bitangent, color, landscape/eye data and every byte no
    known field covers) must be byte-identical; only skin weights and bone indices may differ (those are validated by
    the weight checks). A failure names the attribute, how many vertices changed, which ones, and — for directions —
    by how many degrees."""
    out = []
    n = s.name
    fmt_ok = (s.num_vertices == g.num_vertices and s.num_triangles == g.num_triangles and s.vertex_size == g.vertex_size
              and s.vertex_desc_flags == g.vertex_desc_flags
              and vertex_layout(s.vertex_desc, s.vertex_size) == vertex_layout(g.vertex_desc, g.vertex_size))
    out.append((f"{n}: vertex/triangle counts and vertex format identical", fmt_ok,
                f"{g.num_vertices}/{s.num_vertices} v, {g.num_triangles}/{s.num_triangles} t, vertex size {g.vertex_size}/"
                f"{s.vertex_size}, flags {g.vertex_desc_flags:#x}/{s.vertex_desc_flags:#x}"))
    tri_ok = fmt_ok and s.triangles is not None and g.triangles is not None and np.array_equal(s.triangles, g.triangles)
    out.append((f"{n}: triangles / topology identical", tri_ok,
                "same" if tri_ok else ("counts differ" if not fmt_ok else
                                       f"{int((s.triangles != g.triangles).any(1).sum())} triangles differ")))
    labels = {"position": "vertex positions", "bitangent_x": None, "uv": "UVs", "uv2": "second UVs", "normal": "normals",
              "bitangent_y": None, "tangent": "tangents and bitangents", "bitangent_z": None, "color": "vertex colors",
              "land": "landscape vertex data", "eye": "eye vertex data", "other": "other vertex bytes (not a known field)"}
    groups = {"tangents and bitangents": ("tangent", "bitangent_x", "bitangent_y", "bitangent_z")}
    for f, label in labels.items():
        if label is None:
            continue
        keys = groups.get(label, (f,))
        present = [k for k in keys if k in s.fields or k in g.fields]
        if not present:
            if f in ("position", "uv", "normal", "tangent", "color"):
                out.append((f"{n}: {label} identical", True, "not present in the vertex format (source and output)"))
            continue
        if not fmt_ok:
            out.append((f"{n}: {label} identical", False, "vertex format or count differs"))
            continue
        diff = np.zeros(s.num_vertices, dtype=bool)
        for k in present:
            a, b = s.fields.get(k), g.fields.get(k)
            if a is None or b is None or a.shape != b.shape:
                diff[:] = True
                continue
            diff |= (a != b).any(1)
        detail = "identical" if not diff.any() else f"{int(diff.sum())}/{s.num_vertices} vertices differ (vertices {_rows(diff)})"
        if diff.any() and f in ("normal", "tangent") and f in s.fields and f in g.fields:
            ang = _normal_angles(s.fields[f], g.fields[f])[diff]
            detail += f"; max {float(ang.max()):.2f}°, mean {float(ang.mean()):.2f}° of change"
        if diff.any() and f == "position":
            detail += " (geometry moved)"
        out.append((f"{n}: {label} identical", not diff.any(), detail))
    return out
