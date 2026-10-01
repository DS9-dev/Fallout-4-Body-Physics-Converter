"""Minimal, read-only NIF reader for Fallout 4 BodySlide/Outfit Studio meshes.

Scope (deliberately small):
  * NIF 20.2.0.7 with a Bethesda stream header (Fallout 4: BS version 130;
    Skyrim SE BS version 100 is parsed on a best-effort basis).
  * Scene graph names (NiNode family) so skin bone references resolve to names.
  * ``BSTriShape`` and subclasses (``BSSubIndexTriShape``, ``BSMeshLODTriShape``,
    ``BSDynamicTriShape`` ...): transform, vertex positions, triangles and
    per-vertex skin weights/bone indices.
  * ``BSSkin::Instance`` / ``BSSkin::BoneData`` for bone lists.

Every block's size is stored in the header, so unknown block types are skipped
safely. Nothing here ever writes to disk.

Field layouts follow the public niftools ``nif.xml`` description
(https://github.com/niftools/nifxml, BSD-3-Clause).
"""
from __future__ import annotations

import hashlib

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

__all__ = ["NifError", "NifHeader", "NifShape", "NifFile", "read_nif"]


class NifError(Exception):
    """Raised when a file cannot be parsed as a supported NIF."""


# Vertex attribute flags (BSVertexDesc >> 44)
VA_VERTEX = 0x001
VA_UV = 0x002
VA_UV2 = 0x004
VA_NORMAL = 0x008
VA_TANGENT = 0x010
VA_COLORS = 0x020
VA_SKINNED = 0x040
VA_LANDDATA = 0x080
VA_EYEDATA = 0x100
VA_FULLPREC = 0x400

TRISHAPE_TYPES = {
    "BSTriShape",
    "BSSubIndexTriShape",
    "BSMeshLODTriShape",
    "BSLODTriShape",
    "BSDynamicTriShape",
}
NODE_TYPES = {
    "NiNode",
    "BSFadeNode",
    "BSLeafAnimNode",
    "BSMultiBoundNode",
    "BSOrderedNode",
    "BSValueNode",
    "BSTreeNode",
    "BSMasterParticleSystem",
    "NiBillboardNode",
    "NiSwitchNode",
    "NiLODNode",
    "BSBlastNode",
    "BSDamageStage",
    "BSDebrisNode",
    "BSRangeNode",
}
LEGACY_SHAPE_TYPES = {"NiTriShape", "NiTriStrips", "BSLODTriShape_legacy", "BSSegmentedTriShape"}


@dataclass
class NifHeader:
    version: int
    user_version: int
    bs_version: int
    author: str
    process_script: str
    export_script: str
    block_types: list[str]
    block_type_index: list[int]
    block_sizes: list[int]
    strings: list[str]

    @property
    def version_str(self) -> str:
        v = self.version
        return f"{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"

    @property
    def is_fallout4(self) -> bool:
        return self.version == 0x14020007 and self.user_version == 12 and self.bs_version == 130


@dataclass
class NifShape:
    """Geometry + skinning of one BSTriShape-family block."""

    name: str
    block_type: str
    block_index: int
    translation: np.ndarray  # (3,)
    rotation: np.ndarray  # (3,3)
    scale: float
    vertices: np.ndarray  # (N,3) float32, shape-local space
    triangles: np.ndarray  # (M,3) int32
    bones: list[str] = field(default_factory=list)
    # Dense per-vertex skin data (N,4); None when the shape is not skinned
    bone_weights: Optional[np.ndarray] = None
    bone_indices: Optional[np.ndarray] = None
    skinned: bool = False
    has_skin_block: bool = False
    vertex_flags: int = 0
    notes: list[str] = field(default_factory=list)
    # 4x4 matrices (column vectors). For skinned FO4 shapes the vertices are in
    # skin space and the game ignores the NiAVObject transform; the skin->global
    # transform is derived from the bone nodes and BSSkin::BoneData.
    skin_to_global: Optional[np.ndarray] = None
    world_transform: Optional[np.ndarray] = None

    @property
    def num_vertices(self) -> int:
        return int(self.vertices.shape[0])

    def world_vertices(self) -> np.ndarray:
        """Vertices in the NIF's global (skeleton) space."""
        v = self.vertices.astype(np.float64)
        m = self.skin_to_global if self.skin_to_global is not None else self.world_transform
        if m is None:
            m = _mat4(self.rotation, self.translation, self.scale)
        if np.allclose(m, np.eye(4)):
            return v
        return v @ m[:3, :3].T + m[:3, 3]

    def weight_matrix(self) -> tuple[list[str], np.ndarray]:
        """Return (bone_names, W) with W shaped (N, len(bones)), float32.

        Bone indices outside the bone list are ignored (they are recorded in
        ``notes`` when parsing).
        """
        n = self.num_vertices
        nb = len(self.bones)
        w = np.zeros((n, nb), dtype=np.float32)
        if self.bone_weights is None or self.bone_indices is None or nb == 0:
            return list(self.bones), w
        idx = self.bone_indices.astype(np.int64)
        wt = self.bone_weights.astype(np.float32)
        valid = (idx < nb) & (wt > 0)
        rows = np.repeat(np.arange(n), 4).reshape(n, 4)
        np.add.at(w, (rows[valid], idx[valid]), wt[valid])
        return list(self.bones), w


@dataclass
class NifFile:
    path: Optional[Path]
    header: NifHeader
    shapes: list[NifShape]
    node_names: list[str]
    unsupported_shapes: list[tuple[str, str]]  # (block type, name)
    warnings: list[str]
    # BSClothExtraData blocks (Havok cloth): (payload size, sha1 of the payload without the name index)
    cloth_data: list[tuple[int, str]] = field(default_factory=list)
    # the same payloads as (size, SHA-256) — for reports (0.5.3 cloth acceptance); comparison uses ``cloth_data``
    cloth_sha256: list[tuple[int, str]] = field(default_factory=list)

    def shape(self, name: str) -> Optional[NifShape]:
        for s in self.shapes:
            if s.name == name:
                return s
        low = name.lower()
        for s in self.shapes:
            if s.name.lower() == low:
                return s
        return None


class _Stream:
    __slots__ = ("buf", "pos", "end")

    def __init__(self, buf: bytes | memoryview, pos: int = 0, end: Optional[int] = None):
        self.buf = buf
        self.pos = pos
        self.end = len(buf) if end is None else end

    def need(self, n: int) -> None:
        if self.pos + n > self.end:
            raise NifError(f"unexpected end of data at offset {self.pos} (need {n} bytes)")

    def u8(self) -> int:
        self.need(1)
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def u16(self) -> int:
        self.need(2)
        v = struct.unpack_from("<H", self.buf, self.pos)[0]
        self.pos += 2
        return v

    def u32(self) -> int:
        self.need(4)
        v = struct.unpack_from("<I", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def i32(self) -> int:
        self.need(4)
        v = struct.unpack_from("<i", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def u64(self) -> int:
        self.need(8)
        v = struct.unpack_from("<Q", self.buf, self.pos)[0]
        self.pos += 8
        return v

    def f32(self, n: int = 1):
        self.need(4 * n)
        v = struct.unpack_from(f"<{n}f", self.buf, self.pos)
        self.pos += 4 * n
        return v if n > 1 else v[0]

    def raw(self, n: int) -> bytes:
        self.need(n)
        v = bytes(self.buf[self.pos : self.pos + n])
        self.pos += n
        return v

    def sized_string(self) -> str:
        n = self.u32()
        if n > 0x100000:
            raise NifError(f"implausible string length {n}")
        return self.raw(n).decode("cp1252", errors="replace")

    def export_string(self) -> str:
        n = self.u8()
        return self.raw(n).rstrip(b"\x00").decode("cp1252", errors="replace")


def _parse_header(s: _Stream) -> NifHeader:
    line_end = bytes(s.buf[: min(len(s.buf), 128)]).find(b"\n")
    if line_end < 0:
        raise NifError("missing NIF header line")
    line = bytes(s.buf[:line_end]).decode("ascii", errors="replace")
    if not (line.startswith("Gamebryo File Format") or line.startswith("NetImmerse File Format")):
        raise NifError(f"not a NIF file (header: {line[:40]!r})")
    s.pos = line_end + 1
    version = s.u32()
    if version != 0x14020007:
        raise NifError(f"unsupported NIF version 0x{version:08X} (only 20.2.0.7 is supported)")
    endian = s.u8()
    if endian != 1:
        raise NifError("big-endian NIF files are not supported")
    user_version = s.u32()
    num_blocks = s.u32()
    bs_version = 0
    author = process_script = export_script = ""
    if user_version >= 3:  # Bethesda stream header present
        bs_version = s.u32()
        author = s.export_string()
        if bs_version > 130:
            s.u32()
        if bs_version < 131:
            process_script = s.export_string()
        export_script = s.export_string()
        if bs_version >= 103:
            s.export_string()  # max filepath
    num_types = s.u16()
    block_types = [s.sized_string() for _ in range(num_types)]
    block_type_index = [s.u16() & 0x7FFF for _ in range(num_blocks)]
    block_sizes = [s.u32() for _ in range(num_blocks)]
    num_strings = s.u32()
    s.u32()  # max string length
    strings = [s.sized_string() for _ in range(num_strings)]
    num_groups = s.u32()
    for _ in range(num_groups):
        s.u32()
    return NifHeader(
        version=version,
        user_version=user_version,
        bs_version=bs_version,
        author=author,
        process_script=process_script,
        export_script=export_script,
        block_types=block_types,
        block_type_index=block_type_index,
        block_sizes=block_sizes,
        strings=strings,
    )


def _string(hdr: NifHeader, idx: int) -> str:
    if idx < 0 or idx >= len(hdr.strings):
        return ""
    return hdr.strings[idx]


def _mat4(rot: np.ndarray, trans: np.ndarray, scale: float) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = np.asarray(rot, dtype=np.float64) * float(scale)
    m[:3, 3] = trans
    return m


def _read_av_object(s: _Stream, hdr: NifHeader) -> dict:
    """NiObjectNET + NiAVObject fields for 20.2.0.7 Bethesda files."""
    name = _string(hdr, s.i32())
    n_extra = s.u32()
    if n_extra > 100000:
        raise NifError("implausible extra data count")
    s.pos += 4 * n_extra
    s.i32()  # controller
    if hdr.bs_version > 26:
        s.u32()  # flags
    else:
        s.u16()
    t = np.array(s.f32(3), dtype=np.float64)
    r = np.array(s.f32(9), dtype=np.float64).reshape(3, 3)
    sc = float(s.f32())
    if hdr.bs_version <= 34:
        n_props = s.u32()
        s.pos += 4 * n_props
    s.i32()  # collision object
    return {"name": name, "translation": t, "rotation": r, "scale": sc}


def _read_trishape(s: _Stream, hdr: NifHeader, btype: str, index: int) -> tuple[NifShape, int]:
    av = _read_av_object(s, hdr)
    s.f32(4)  # bounding sphere
    if hdr.bs_version >= 155:
        s.f32(6)
    skin_ref = s.i32()
    s.i32()  # shader property
    s.i32()  # alpha property
    desc = s.u64()
    if hdr.bs_version >= 130:
        num_tris = s.u32()
    else:
        num_tris = s.u16()
    num_verts = s.u16()
    data_size = s.u32()
    flags = (desc >> 44) & 0xFFF
    vsize = (desc & 0xF) * 4
    notes: list[str] = []
    verts = np.zeros((0, 3), dtype=np.float32)
    tris = np.zeros((0, 3), dtype=np.int32)
    weights = indices = None
    skinned = bool(flags & VA_SKINNED)
    if data_size > 0 and num_verts > 0:
        if vsize <= 0:
            raise NifError(f"shape {av['name']!r}: zero vertex size with data present")
        s.need(vsize * num_verts)
        raw = np.frombuffer(s.buf, dtype=np.uint8, count=vsize * num_verts, offset=s.pos).reshape(num_verts, vsize)
        s.pos += vsize * num_verts
        full_prec = bool(flags & VA_FULLPREC) or hdr.bs_version == 100
        if flags & VA_VERTEX:
            if full_prec:
                verts = np.ascontiguousarray(raw[:, 0:12]).view("<f4").reshape(num_verts, 3).astype(np.float32)
            else:
                verts = np.ascontiguousarray(raw[:, 0:6]).view("<f2").reshape(num_verts, 3).astype(np.float32)
        else:
            notes.append("vertex positions flag not set")
        if skinned:
            off = ((desc >> 28) & 0xF) * 4
            if off + 12 > vsize:
                notes.append("skin data offset outside vertex; skin ignored")
                skinned = False
            else:
                weights = np.ascontiguousarray(raw[:, off : off + 8]).view("<f2").reshape(num_verts, 4).astype(np.float32)
                indices = np.ascontiguousarray(raw[:, off + 8 : off + 12]).reshape(num_verts, 4).astype(np.int32)
        if num_tris > 0:
            s.need(6 * num_tris)
            tris = np.frombuffer(s.buf, dtype="<u2", count=3 * num_tris, offset=s.pos).reshape(num_tris, 3).astype(np.int32)
            s.pos += 6 * num_tris
    elif num_verts > 0:
        notes.append("geometry data stored externally (data size 0)")
    shape = NifShape(
        name=av["name"],
        block_type=btype,
        block_index=index,
        translation=av["translation"],
        rotation=av["rotation"],
        scale=av["scale"],
        vertices=verts,
        triangles=tris,
        bone_weights=weights,
        bone_indices=indices,
        skinned=skinned,
        vertex_flags=flags,
        notes=notes,
    )
    return shape, skin_ref


def _read_skin_instance(s: _Stream) -> tuple[int, list[int]]:
    s.i32()  # skeleton root
    data_ref = s.i32()
    n = s.u32()
    if n > 4096:
        raise NifError("implausible bone count in BSSkin::Instance")
    return data_ref, [s.i32() for _ in range(n)]


def _read_bone_data(s: _Stream) -> list[np.ndarray]:
    n = s.u32()
    if n > 4096:
        raise NifError("implausible bone count in BSSkin::BoneData")
    out = []
    for _ in range(n):
        s.f32(4)  # bounding sphere
        rot = np.array(s.f32(9), dtype=np.float64).reshape(3, 3)
        trans = np.array(s.f32(3), dtype=np.float64)
        scale = float(s.f32())
        out.append(_mat4(rot, trans, scale))
    return out


def read_nif_bytes(data: bytes, path: Optional[Path] = None) -> NifFile:
    s = _Stream(data)
    hdr = _parse_header(s)
    offsets = []
    pos = s.pos
    for size in hdr.block_sizes:
        offsets.append(pos)
        pos += size
    if pos > len(data):
        raise NifError(f"file truncated: blocks need {pos} bytes, file has {len(data)}")

    names: dict[int, str] = {}
    shapes: list[tuple[NifShape, int]] = []
    skins: dict[int, tuple[int, list[int]]] = {}
    bone_data: dict[int, list[np.ndarray]] = {}
    local_xf: dict[int, np.ndarray] = {}
    parent: dict[int, int] = {}
    unsupported: list[tuple[str, str]] = []
    warnings: list[str] = []
    node_names: list[str] = []

    cloth: list[tuple[int, str]] = []
    cloth256: list[tuple[int, str]] = []
    for i, (off, size) in enumerate(zip(offsets, hdr.block_sizes)):
        btype = hdr.block_types[hdr.block_type_index[i]] if hdr.block_type_index[i] < len(hdr.block_types) else "?"
        if btype == "BSClothExtraData":
            payload = data[off + 4:off + size]
            cloth.append((len(payload), hashlib.sha1(payload).hexdigest()))
            cloth256.append((len(payload), hashlib.sha256(payload).hexdigest()))
        bs = _Stream(data, off, off + size)
        try:
            if btype in TRISHAPE_TYPES:
                shp, skin_ref = _read_trishape(bs, hdr, btype, i)
                names[i] = shp.name
                local_xf[i] = _mat4(shp.rotation, shp.translation, shp.scale)
                shapes.append((shp, skin_ref))
            elif btype in NODE_TYPES:
                av = _read_av_object(bs, hdr)
                names[i] = av["name"]
                node_names.append(av["name"])
                local_xf[i] = _mat4(av["rotation"], av["translation"], av["scale"])
                n_children = bs.u32()
                if n_children > 100000:
                    raise NifError("implausible child count")
                for _ in range(n_children):
                    c = bs.i32()
                    if c >= 0:
                        parent.setdefault(c, i)
            elif btype == "BSSkin::Instance":
                skins[i] = _read_skin_instance(bs)
            elif btype == "BSSkin::BoneData":
                bone_data[i] = _read_bone_data(bs)
            elif btype in LEGACY_SHAPE_TYPES:
                nm = _string(hdr, bs.i32())
                names[i] = nm
                unsupported.append((btype, nm))
        except NifError as exc:
            warnings.append(f"block {i} ({btype}): {exc}")

    world_cache: dict[int, np.ndarray] = {}

    def world(i: int, depth: int = 0) -> np.ndarray:
        if i in world_cache:
            return world_cache[i]
        m = local_xf.get(i, np.eye(4))
        p = parent.get(i)
        if p is not None and p != i and depth < 256:
            m = world(p, depth + 1) @ m
        world_cache[i] = m
        return m

    out_shapes: list[NifShape] = []
    for shp, skin_ref in shapes:
        shp.world_transform = world(shp.block_index)
        if skin_ref >= 0:
            if skin_ref in skins:
                shp.has_skin_block = True
                data_ref, bone_refs = skins[skin_ref]
                shp.bones = [names.get(b, f"<block {b}>") for b in bone_refs]
                bd = bone_data.get(data_ref)
                if bd and len(bd) == len(bone_refs):
                    cands = np.array([world(b) @ bd[k] for k, b in enumerate(bone_refs) if b in local_xf])
                    if len(cands):
                        shp.skin_to_global = np.median(cands, axis=0)
                        spread = float(np.abs(cands[:, :3, 3] - shp.skin_to_global[:3, 3]).max())
                        if spread > 5.0:
                            shp.notes.append(f"bones disagree on the skin transform (spread {spread:.1f})")
                elif bd is not None:
                    shp.notes.append("bone data count does not match bone list")
            else:
                btype = hdr.block_types[hdr.block_type_index[skin_ref]] if 0 <= skin_ref < len(hdr.block_type_index) else "?"
                shp.notes.append(f"skin block type {btype} not supported")
        if shp.skinned and shp.bone_indices is not None and shp.bones:
            bad = int((shp.bone_indices[shp.bone_weights > 0] >= len(shp.bones)).sum())
            if bad:
                shp.notes.append(f"{bad} weight entries reference bone indices outside the bone list")
        out_shapes.append(shp)
    return NifFile(path=path, header=hdr, shapes=out_shapes, node_names=node_names, unsupported_shapes=unsupported,
                   warnings=warnings, cloth_data=cloth, cloth_sha256=cloth256)


def read_nif(path: str | Path) -> NifFile:
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise NifError(f"cannot read {p.name}: {exc}") from exc
    return read_nif_bytes(data, p)
