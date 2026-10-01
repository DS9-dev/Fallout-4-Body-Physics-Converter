"""Synthetic test fixtures, generated in code.

Nothing here is derived from third-party meshes: the "body" is a procedurally
generated cylinder with made-up weights that mimic the *structure* of a CBBE
3BBB body (which bones exist, which regions carry physics weight). Outfits are
offset patches of that cylinder.

Also contains a tiny NIF 20.2.0.7 / BS 130 writer that emits only the fields
the analyzer's reader consumes. It exists solely for tests.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# NIF writer (test only)
# ---------------------------------------------------------------------------


@dataclass
class SynthShape:
    name: str
    vertices: np.ndarray  # (N,3)
    triangles: np.ndarray  # (M,3)
    bones: list[str] = field(default_factory=list)
    weights: Optional[np.ndarray] = None  # (N,4)
    indices: Optional[np.ndarray] = None  # (N,4)
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # 0.4.1 (Direct-NIF structure tests): optional material / alpha / segment data, written like Fallout 4 does
    shader: Optional[dict] = None  # {"name": material path, "textures": [...], "shader_type": int, "params": bytes}
    alpha: Optional[bytes] = None  # NiAlphaProperty flags (u16) + threshold (u8)
    segments: Optional[bytes] = None  # set → BSSubIndexTriShape with this segment data after the triangles
    # 0.4.2: raw per-vertex attribute bytes (structure.py field names → (N, width) uint8): bitangent_x (4), uv (4),
    # normal (3), bitangent_y (1), tangent (3), bitangent_z (1), color (4), eye (4). Present keys set the vertex flags.
    vattrs: Optional[dict] = None


def _sized(s: str) -> bytes:
    b = s.encode("cp1252")
    return struct.pack("<I", len(b)) + b


def _export(s: str) -> bytes:
    b = s.encode("cp1252") + b"\x00"
    return struct.pack("<B", len(b)) + b


IDENT = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


def write_nif(
    path: Path,
    shapes: list[SynthShape],
    bone_positions: Optional[dict[str, tuple[float, float, float]]] = None,
    user_version: int = 12,
    bs_version: int = 130,
    root_name: str = "Scene Root",
    cloth: Optional[list[bytes]] = None,
    extra_data: Optional[list[tuple[str, str]]] = None,
) -> None:
    """Write a minimal NIF with a root NiNode, bone NiNodes and BSTriShapes.

    Bone nodes are children of the root at ``bone_positions``; the matching
    BSSkin::BoneData transforms are the inverse bone transforms, so the
    skin->global transform is the identity (like BodySlide output for
    Fallout 4 bodies). Shapes may carry a BSLightingShaderProperty with a
    BSShaderTextureSet, an NiAlphaProperty and segment data (then they are
    BSSubIndexTriShapes); ``extra_data`` adds NiStringExtraData blocks to the root.
    """
    bone_positions = dict(bone_positions or {})
    for s in shapes:
        for b in s.bones:
            bone_positions.setdefault(b, (0.0, 0.0, 0.0))
    strings: list[str] = []

    def sidx(s: str) -> int:
        if s not in strings:
            strings.append(s)
        return strings.index(s)

    types = ["NiNode", "BSTriShape", "BSSkin::Instance", "BSSkin::BoneData", "BSClothExtraData", "BSSubIndexTriShape",
             "BSLightingShaderProperty", "BSShaderTextureSet", "NiAlphaProperty", "NiStringExtraData"]
    T = {t: i for i, t in enumerate(types)}
    blocks: list[tuple[int, bytes]] = []  # (type index, payload)
    bone_names = list(bone_positions)
    # block numbering: 0 root, 1..nb bones, then per shape (shape, skin, bonedata, shader, texture set, alpha)
    nb = len(bone_names)
    bone_block = {b: 1 + i for i, b in enumerate(bone_names)}
    layout = []
    nxt = 1 + nb
    for s in shapes:
        ids = {"shape": nxt}
        nxt += 1
        if s.bones:
            ids["skin"], ids["bonedata"] = nxt, nxt + 1
            nxt += 2
        if s.shader is not None:
            ids["shader"], ids["texset"] = nxt, nxt + 1
            nxt += 2
        if s.alpha is not None:
            ids["alpha"] = nxt
            nxt += 1
        layout.append(ids)
    cloth_ids = list(range(nxt, nxt + len(cloth or [])))
    nxt += len(cloth or [])
    extra_ids = list(range(nxt, nxt + len(extra_data or [])))

    def av(name: str, t=(0.0, 0.0, 0.0), extra_refs: tuple = ()) -> bytes:
        return (
            struct.pack("<iI", sidx(name), len(extra_refs)) + b"".join(struct.pack("<i", r) for r in extra_refs)
            + struct.pack("<iI", -1, 14)
            + struct.pack("<3f", *t)
            + struct.pack("<9f", *IDENT)
            + struct.pack("<f", 1.0)
            + struct.pack("<i", -1)
        )

    children = [bone_block[b] for b in bone_names] + [ids["shape"] for ids in layout]
    root = (av(root_name, extra_refs=tuple(cloth_ids + extra_ids)) + struct.pack("<I", len(children))
            + struct.pack(f"<{len(children)}i", *children) + struct.pack("<I", 0))
    blocks.append((0, root))
    for b in bone_names:
        blocks.append((0, av(b, bone_positions[b]) + struct.pack("<II", 0, 0)))
    for s, ids in zip(shapes, layout):
        v = np.asarray(s.vertices, dtype=np.float32)
        t = np.asarray(s.triangles, dtype=np.uint16)
        n = len(v)
        skinned = bool(s.bones)
        va = dict(s.vattrs or {})
        flags = 0x001 | 0x400 | (0x040 if skinned else 0)
        cols: list[tuple[str, int]] = []  # (field, width) after the 16 position bytes, in Bethesda order
        offs = {}
        pos = 16
        for fld, width, flag, sh in (("uv", 4, 0x002, 8), ("uv2", 4, 0x004, 12), ("normal", 4, 0x008, 16),
                                     ("tangent", 4, 0x010, 20), ("color", 4, 0x020, 24), ("skin", 12, 0x040, 28),
                                     ("land", 4, 0x080, 32), ("eye", 4, 0x100, 36)):
            if fld == "skin" and not skinned or fld != "skin" and fld not in va:
                continue
            flags |= flag
            offs[sh] = pos // 4
            cols.append((fld, width))
            pos += width
        pad = np.asarray(va["pad"], np.uint8).reshape(n, -1) if "pad" in va else None  # bytes no flag describes
        vsize = pos + (pad.shape[1] if pad is not None else 0)
        desc = (vsize // 4) | (flags << 44)
        for sh, o in offs.items():
            desc |= o << sh
        zero = lambda w: np.zeros((n, w), dtype=np.uint8)  # noqa: E731
        bx = va.get("bitangent_x", zero(4))
        parts = [v.view(np.uint8).reshape(n, 12), np.asarray(bx, dtype=np.uint8).reshape(n, 4)]
        for fld, width in cols:
            if fld == "skin":
                w = np.asarray(s.weights, dtype=np.float16).view(np.uint8).reshape(n, 8)
                parts += [w, np.asarray(s.indices, dtype=np.uint8).reshape(n, 4)]
            elif fld == "normal":
                parts += [np.asarray(va["normal"], np.uint8).reshape(n, 3), np.asarray(va.get("bitangent_y", zero(1)), np.uint8).reshape(n, 1)]
            elif fld == "tangent":
                parts += [np.asarray(va["tangent"], np.uint8).reshape(n, 3), np.asarray(va.get("bitangent_z", zero(1)), np.uint8).reshape(n, 1)]
            else:
                parts.append(np.asarray(va[fld], np.uint8).reshape(n, width))
        if pad is not None:
            parts.append(pad)
        vb = np.concatenate(parts, 1).tobytes() if n else b""
        data_size = len(vb) + 6 * len(t)
        payload = (
            av(s.name, s.translation)
            + struct.pack("<4f", 0, 0, 0, 100)
            + struct.pack("<iii", ids.get("skin", -1), ids.get("shader", -1), ids.get("alpha", -1))
            + struct.pack("<Q", desc)
            + struct.pack("<IHI", len(t), n, data_size)
            + bytes(vb)
            + t.astype("<u2").tobytes()
            + struct.pack("<I", 0)
        )
        if s.segments is not None:
            blocks.append((T["BSSubIndexTriShape"], payload + s.segments))
        else:
            blocks.append((T["BSTriShape"], payload))
        if skinned:
            refs = [bone_block[b] for b in s.bones]
            blocks.append((T["BSSkin::Instance"], struct.pack("<iiI", 0, ids["bonedata"], len(refs)) + struct.pack(f"<{len(refs)}i", *refs)))
            bd = struct.pack("<I", len(s.bones))
            for b in s.bones:
                px, py, pz = bone_positions[b]
                bd += struct.pack("<4f", 0, 0, 0, 10) + struct.pack("<9f", *IDENT) + struct.pack("<3f", -px, -py, -pz) + struct.pack("<f", 1.0)
            blocks.append((T["BSSkin::BoneData"], bd))
        if s.shader is not None:
            sh = s.shader
            params = sh.get("params", struct.pack("<10f", 1, 1, 0.5, 0.5, 1, 1, 0, 0, 1, 1))
            blocks.append((T["BSLightingShaderProperty"],
                           struct.pack("<Ii", int(sh.get("shader_type", 0)), sidx(sh.get("name", "")))
                           + struct.pack("<Ii", 0, -1)  # no extra data, no controller
                           + struct.pack("<II4f", 0x80400201, 0x00000021, 0, 0, 1, 1)  # flags, uv offset/scale
                           + struct.pack("<i", ids["texset"])
                           + struct.pack("<4f", 1, 1, 1, 1)  # emissive color + multiple
                           + struct.pack("<i", sidx(sh.get("wet_material", "")))
                           + params))
            tex = list(sh.get("textures", [""] * 10))
            blocks.append((T["BSShaderTextureSet"], struct.pack("<I", len(tex)) + b"".join(_sized(x) for x in tex)))
        if s.alpha is not None:
            blocks.append((T["NiAlphaProperty"], struct.pack("<iIi", sidx(""), 0, -1) + s.alpha))
    for payload in cloth or []:  # Havok cloth data: name index + opaque payload (only its presence/hash matters)
        blocks.append((T["BSClothExtraData"], struct.pack("<i", sidx("Cloth")) + payload))
    for name, value in extra_data or []:
        blocks.append((T["NiStringExtraData"], struct.pack("<ii", sidx(name), sidx(value))))

    out = bytearray(b"Gamebryo File Format, Version 20.2.0.7\n")
    out += struct.pack("<IBII", 0x14020007, 1, user_version, len(blocks))
    out += struct.pack("<I", bs_version)
    out += _export("synthetic")
    if bs_version > 130:
        out += struct.pack("<I", 0)
    if bs_version < 131:
        out += _export("")
    out += _export("")
    if bs_version >= 103:
        out += _export("")
    out += struct.pack("<H", len(types)) + b"".join(_sized(x) for x in types)
    out += b"".join(struct.pack("<H", ti) for ti, _ in blocks)
    out += b"".join(struct.pack("<I", len(p)) for _, p in blocks)
    out += struct.pack("<II", len(strings), max((len(x) for x in strings), default=0))
    out += b"".join(_sized(x) for x in strings)
    out += struct.pack("<I", 0)
    for _, p in blocks:
        out += p
    out += struct.pack("<Ii", 1, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


# ---------------------------------------------------------------------------
# synthetic body
# ---------------------------------------------------------------------------

VANILLA_BONES = ["Pelvis", "SPINE1", "SPINE2", "Chest", "LLeg_Thigh", "RLeg_Thigh", "LLeg_Calf", "RLeg_Calf", "Neck", "Head"]
PHYS_BONES = [
    "LBreast_01_skin", "LBreast_02_skin", "LBreast_03_skin", "RBreast_01_skin", "RBreast_02_skin", "RBreast_03_skin",
    "LButt_01_skin", "RButt_01_skin", "LLeg_Thigh_01_F_skin", "LLeg_Thigh_01_R_skin", "RLeg_Thigh_01_F_skin", "RLeg_Thigh_01_R_skin",
]
BONE_POS = {
    "Pelvis": (0, 0, 60), "SPINE1": (0, 0, 70), "SPINE2": (0, 0, 85), "Chest": (0, 0, 100), "LLeg_Thigh": (-5, 0, 55),
    "RLeg_Thigh": (5, 0, 55), "LLeg_Calf": (-5, 0, 25), "RLeg_Calf": (5, 0, 25), "Neck": (0, 0, 115), "Head": (0, 0, 120),
}
CBBE_SLIDERS = [
    "Breasts", "BreastsSmall", "BreastsNewSH", "BreastCenter", "BreastGravity2", "Butt", "ButtSmall", "ButtShape2",
    "Waist", "Hips", "Belly", "Arms", "ChubbyLegs", "ShoulderWidth",
]
TWB_SLIDERS = ["Tone", "Athlete", "FFB Fitness Less", "FFB Fitness More", "Body Clamp", "Muscle Definition"]

RINGS = 48
Z_MIN, Z_MAX = 0, 120
RADIUS = 10.0


@dataclass
class Body:
    vertices: np.ndarray
    triangles: np.ndarray
    ring_z: np.ndarray  # z of each vertex row
    bones: list[str]
    weights: np.ndarray  # (N,4)
    indices: np.ndarray  # (N,4)


def _grid_tris(rows: int, cols: int, offset: int = 0) -> np.ndarray:
    tris = []
    for r in range(rows - 1):
        for c in range(cols):
            a = offset + r * cols + c
            b = offset + r * cols + (c + 1) % cols
            d = offset + (r + 1) * cols + c
            e = offset + (r + 1) * cols + (c + 1) % cols
            tris.append((a, b, e))
            tris.append((a, e, d))
    return np.array(tris, dtype=np.int32).reshape(-1, 3)


def make_body(with_physics: bool = True) -> Body:
    zs = np.arange(Z_MIN, Z_MAX + 1, 1.0)
    verts = []
    for z in zs:
        for c in range(RINGS):
            a = 2 * math.pi * c / RINGS
            verts.append((RADIUS * math.cos(a), RADIUS * math.sin(a), z))
    v = np.array(verts, dtype=np.float64)
    bones = VANILLA_BONES + (PHYS_BONES if with_physics else [])
    bi = {b: i for i, b in enumerate(bones)}
    n = len(v)
    W = np.zeros((n, 4), dtype=np.float32)
    I = np.zeros((n, 4), dtype=np.uint8)
    for k, (x, y, z) in enumerate(v):
        side = "L" if x < 0 else "R"
        infl: list[tuple[str, float]] = []
        if z >= 80:
            base = "Chest" if z >= 95 else "SPINE2"
        elif z >= 62:
            base = "SPINE1"
        elif z >= 50:
            base = "Pelvis"
        elif z >= 25:
            base = f"{side}Leg_Thigh"
        else:
            base = f"{side}Leg_Calf"
        if with_physics and 95 <= z <= 110 and y < -3:  # front = breasts
            infl = [(f"{side}Breast_01_skin", 0.3), (f"{side}Breast_02_skin", 0.2), (f"{side}Breast_03_skin", 0.1), (base, 0.4)]
        elif with_physics and 52 <= z <= 68 and y > 3:  # back = butt
            infl = [(f"{side}Butt_01_skin", 0.5), (base, 0.5)]
        elif with_physics and 30 <= z < 50:  # thighs
            fr = "F" if y < 0 else "R"
            infl = [(f"{side}Leg_Thigh_01_{fr}_skin", 0.4), (base, 0.6)]
        else:
            infl = [(base, 1.0)]
        for j, (b, w) in enumerate(infl):
            I[k, j] = bi[b]
            W[k, j] = w
    return Body(v, _grid_tris(len(zs), RINGS), v[:, 2].copy(), bones, W, I)


def outfit_patch(
    body: Body,
    z_range: tuple[float, float],
    offset: float = 0.3,
    copy_weights: bool = False,
    name: str = "Outfit",
    only_bones: Optional[list[str]] = None,
) -> SynthShape:
    """An outfit patch following the body surface between ``z_range``."""
    lo, hi = z_range
    rows = [z for z in np.unique(body.ring_z) if lo <= z <= hi]
    sel = np.where((body.ring_z >= lo) & (body.ring_z <= hi))[0]
    v = body.vertices[sel].copy()
    radial = v[:, :2] / np.linalg.norm(v[:, :2], axis=1, keepdims=True)
    v[:, :2] += radial * offset
    tris = _grid_tris(len(rows), RINGS)
    if copy_weights:
        bones = list(body.bones)
        W = body.weights[sel].copy()
        I = body.indices[sel].copy()
    else:
        bones = only_bones or ["SPINE2", "Pelvis", "SPINE1", "Chest"]
        # simple rigid weighting to the nearest listed vanilla bone by height
        W = np.zeros((len(v), 4), dtype=np.float32)
        I = np.zeros((len(v), 4), dtype=np.uint8)
        heights = np.array([BONE_POS[b][2] for b in bones])
        for k, z in enumerate(v[:, 2]):
            I[k, 0] = int(np.argmin(np.abs(heights - z)))
            W[k, 0] = 1.0
    return SynthShape(name, v, tris, bones, W, I)


def seam_split(shape: SynthShape, cols: int = RINGS) -> SynthShape:
    """Open a ring patch along a UV seam: column 0 is duplicated as an extra last column (same positions, own
    vertices), like the seam vertices of real game meshes."""
    v = np.asarray(shape.vertices)
    rows = len(v) // cols
    grid = v.reshape(rows, cols, 3)
    nv = np.concatenate([grid, grid[:, :1]], 1).reshape(-1, 3)
    tris = []
    w = cols + 1
    for r in range(rows - 1):
        for c in range(cols):
            a, b, d, e = r * w + c, r * w + c + 1, (r + 1) * w + c, (r + 1) * w + c + 1
            tris += [(a, b, e), (a, e, d)]
    def ext(x):
        x = np.asarray(x)
        return np.concatenate([x.reshape(rows, cols, -1), x.reshape(rows, cols, -1)[:, :1]], 1).reshape(rows * w, -1)
    return SynthShape(shape.name, nv, np.array(tris, dtype=np.int32), list(shape.bones), ext(shape.weights).astype(np.float32),
                      ext(shape.indices).astype(np.uint8))


def body_shape(body: Body, name: str) -> SynthShape:
    return SynthShape(name, body.vertices, body.triangles, body.bones, body.weights, body.indices)


# ---------------------------------------------------------------------------
# BodySlide project files
# ---------------------------------------------------------------------------


DEFAULT_OUT = "meshes\\Test"
BSL = "\\"


def osp_xml(sets: list[dict]) -> str:
    """sets: dicts with name, folder, source, output_path, output_file, shapes [(name, target)], sliders [names], zaps [names]."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<SliderSetInfo version="1">']
    for s in sets:
        out.append(f'  <SliderSet name="{s["name"]}">')
        out.append(f'    <DataFolder>{s["folder"]}</DataFolder>')
        out.append(f'    <SourceFile>{s["source"]}</SourceFile>')
        opath = s.get("output_path", DEFAULT_OUT)
        out.append(f'    <OutputPath>{opath}</OutputPath>')
        gw = "true" if s.get("gen_weights", True) else "false"
        out.append(f'    <OutputFile GenWeights="{gw}">{s.get("output_file", s["name"])}</OutputFile>')
        for shp, tgt, *df in s["shapes"]:  # optional 3rd element: the shape's own DataFolder (Outfit Studio 5.8.2)
            dfa = f' DataFolder="{df[0]}"' if df and df[0] else ""
            out.append(f'    <Shape target="{tgt}"{dfa}>{shp}</Shape>')
        for sl in s.get("sliders", []):
            out.append(f'    <Slider name="{sl}" invert="false" small="0" big="100">')
            for shp, tgt, *_df in s["shapes"]:
                osd = s.get("osd_file", s["source"][:-4] + ".osd") + BSL + tgt + sl
                out.append(f'      <Data name="{tgt}{sl}" target="{tgt}">{osd}</Data>')
            out.append("    </Slider>")
        for z in s.get("zaps", []):
            out.append(f'    <Slider name="{z}" invert="false" small="0" big="0" zap="true" uv="false" hidden="false"/>')
        out.append("  </SliderSet>")
    out.append("</SliderSetInfo>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# a complete fake installation (game + portable MO2)
# ---------------------------------------------------------------------------

BS = Path("Tools") / "BodySlide"


def write_osd(path: Path, entries: dict[str, tuple[np.ndarray, np.ndarray]]) -> None:
    """Minimal .osd writer (test only): entries name -> (vertex indices, (n,3) offsets)."""
    out = bytearray(b"\x00DSO") + struct.pack("<II", 1, len(entries))
    for name, (idx, diff) in entries.items():
        b = name.encode("utf-8")
        out += struct.pack("<B", len(b)) + b + struct.pack("<H", len(idx))
        for i, d in zip(idx, diff):
            out += struct.pack("<H3f", int(i), *[float(x) for x in d])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def _project(mod_dir: Path, osp_name: str, sets: list[dict], nifs: dict[str, list[SynthShape]],
             cloth: Optional[dict[str, list[bytes]]] = None) -> None:
    p = mod_dir / BS / "SliderSets" / osp_name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(osp_xml(sets), encoding="utf-8")
    for rel, shapes in nifs.items():
        write_nif(mod_dir / BS / "ShapeData" / rel, shapes, BONE_POS, cloth=(cloth or {}).get(rel))
    # slider data for every set that has NIF data: "<tgt><slider>" entries in <folder>/<source stem>.osd
    for st in sets:
        rel = f"{st['folder']}/{st['source']}"
        if rel not in nifs or not st.get("sliders"):
            continue
        entries = {}
        for k, sl in enumerate(st["sliders"]):
            for _shp, tgt in st["shapes"]:
                idx = np.arange(0, 12) + k
                entries[f"{tgt}{sl}"] = (idx, np.full((len(idx), 3), 0.01 * (k + 1), dtype=np.float32))
        write_osd(mod_dir / BS / "ShapeData" / st["folder"] / (st["source"][:-4] + ".osd"), entries)


@dataclass
class FakeInstall:
    root: Path
    game_dir: Path
    mo2_dir: Path

    @property
    def mods(self) -> Path:
        return self.mo2_dir / "mods"


def _base_install(root: Path):
    """game/, mo2/ (portable instance), the BodySlide mod and the CBBE / CBBE 3BBB references."""
    game = root / "game"
    (game / "Data").mkdir(parents=True, exist_ok=True)
    (game / "Fallout4.exe").write_bytes(b"MZ fake")
    mo2 = root / "mo2"
    (mo2 / "overwrite").mkdir(parents=True, exist_ok=True)
    (mo2 / "ModOrganizer.ini").write_text(
        "[General]\n"
        "gameName=Fallout 4\n"
        f"gamePath=@ByteArray({str(game).replace(chr(92), '/')})\n"
        "selected_profile=@ByteArray(Default)\n"
        "version=2.5.2\n\n[Settings]\n",
        encoding="utf-8",
    )
    mods = mo2 / "mods"
    body = make_body(True)
    body_nophys = make_body(False)

    # --- BodySlide + references ------------------------------------------------
    bsm = mods / "BodySlide and Outfit Studio"
    (bsm / BS / "BodySlide x64.exe").parent.mkdir(parents=True, exist_ok=True)
    (bsm / BS / "BodySlide x64.exe").write_bytes(b"MZ fake")
    (bsm / BS / "OutfitStudio x64.exe").write_bytes(b"MZ fake")
    (bsm / BS / "Config.xml").write_text(
        "<Config><TargetGame>3</TargetGame><Anim><DefaultSkeletonReference>res\\skeleton_fo4.nif</DefaultSkeletonReference>"
        "</Anim></Config>\n",
        encoding="utf-8",
    )
    write_nif(bsm / BS / "res" / "skeleton_fo4.nif", [], BONE_POS, root_name="skeleton.nif")

    cbbe = mods / "CBBE"
    (cbbe / BS).mkdir(parents=True, exist_ok=True)
    (cbbe / BS / "RefTemplates.xml").write_text(
        "<RefTemplates>\n"
        '  <Template sourcefile="SliderSets\\CBBE Body.osp" set="CBBE Body" shape="CBBE">CBBE Body</Template>\n'
        '  <Template sourcefile="SliderSets\\CBBE 3BBB Body.osp" set="CBBE 3BBB Body" shape="CBBE-3BBB">CBBE 3BBB Body</Template>\n'
        "</RefTemplates>\n",
        encoding="utf-8",
    )
    _project(cbbe, "CBBE Body.osp", [dict(name="CBBE Body", folder="CBBE", source="CBBE.nif", shapes=[("CBBE", "CBBE")],
                                          sliders=CBBE_SLIDERS, output_path="meshes\\Actors\\Character\\CharacterAssets", output_file="FemaleBody")],
             {"CBBE/CBBE.nif": [body_shape(body_nophys, "CBBE")]})
    _project(cbbe, "CBBE 3BBB Body.osp", [dict(name="CBBE 3BBB Body", folder="CBBE 3BBB", source="CBBE 3BBB.nif",
                                               shapes=[("CBBE-3BBB", "CBBE-3BBB")], sliders=CBBE_SLIDERS,
                                               output_path="meshes\\Actors\\Character\\CharacterAssets", output_file="FemaleBody")],
             {"CBBE 3BBB/CBBE 3BBB.nif": [body_shape(body, "CBBE-3BBB")]})

    return game, mo2, mods, body, body_nophys


def build_fake_install(root: Path) -> FakeInstall:
    """Create game/, mo2/ with mods covering all classification branches."""
    game, mo2, mods, body, body_nophys = _base_install(root)

    # --- plain CBBE outfits (conversion candidates and friends) --------------------
    m = mods / "Test Outfits CBBE"
    top = outfit_patch(body, (85, 115), name="TopShape")
    _project(m, "Test Top.osp", [dict(name="Test Top - CBBE", folder="Test Top", source="Test Top.nif",
                                      shapes=[("TopShape", "TopShape"), ("CBBE", "CBBE")], sliders=CBBE_SLIDERS,
                                      output_path="meshes\\Test", output_file="top")],
             {"Test Top/Test Top.nif": [top, body_shape(body_nophys, "CBBE")]})
    boots = outfit_patch(body, (0, 18), name="Boots", only_bones=["LLeg_Calf", "RLeg_Calf"])
    _project(m, "Test Boots.osp", [dict(name="Test Boots - CBBE", folder="Test Boots", source="Test Boots.nif",
                                        shapes=[("Boots", "Boots")], sliders=CBBE_SLIDERS, output_file="boots")],
             {"Test Boots/Test Boots.nif": [boots]})
    loose = outfit_patch(body, (85, 115), offset=9.0, name="PonchoShape")
    _project(m, "Test Poncho.osp", [dict(name="Test Poncho - CBBE", folder="Test Poncho", source="Test Poncho.nif",
                                         shapes=[("PonchoShape", "PonchoShape")], sliders=CBBE_SLIDERS, output_file="poncho")],
             {"Test Poncho/Test Poncho.nif": [loose]})
    jacket = outfit_patch(body, (85, 115), name="JacketShape")
    _project(m, "Test Jacket.osp", [dict(name="Test Jacket - CBBE", folder="Test Jacket", source="Test Jacket.nif",
                                         shapes=[("JacketShape", "JacketShape")], sliders=CBBE_SLIDERS, output_file="jacket")],
             {"Test Jacket/Test Jacket.nif": [jacket]})
    # project whose ShapeData is missing
    _project(m, "Test Missing.osp", [dict(name="Test Missing - CBBE", folder="Missing", source="Missing.nif",
                                          shapes=[("X", "X")], sliders=CBBE_SLIDERS)], {})
    # malformed XML
    (m / BS / "SliderSets" / "Broken.osp").write_text("<SliderSetInfo><SliderSet name='x'>", encoding="utf-8")
    # BodyTalk project
    bt = outfit_patch(body, (85, 115), name="BTShape")
    _project(m, "Test BT.osp", [dict(name="Test Shirt - BT3", folder="Test BT", source="Test BT.nif",
                                     shapes=[("BTShape", "BTShape")], sliders=[f"BT{x}" for x in "ABCDEFG"], output_file="bt")],
             {"Test BT/Test BT.nif": [bt]})
    # original CBBE project later superseded by a 3BBB project in another mod
    pants = outfit_patch(body, (30, 70), name="PantsShape", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])
    _project(m, "Test Pants.osp", [dict(name="Test Pants - CBBE", folder="Test Pants", source="Test Pants.nif",
                                        shapes=[("PantsShape", "PantsShape")], sliders=CBBE_SLIDERS,
                                        output_path="meshes\\Test", output_file="pants")],
             {"Test Pants/Test Pants.nif": [pants]})

    # Raider-Punk-like: one project file with the original CBBE set and a manual
    # "3BBB TEST" set that builds the same output (with the 3BBB body copied in)
    vtop = outfit_patch(body, (85, 115), name="VarTop")
    vtop3 = outfit_patch(body, (85, 115), copy_weights=True, name="VarTop")
    _project(m, "Test Variant.osp", [
        dict(name="Test Variant Top - CBBE", folder="Test Variant", source="Test Variant.nif",
             shapes=[("VarTop", "VarTop")], sliders=CBBE_SLIDERS, output_path="meshes\\Test\\Variant", output_file="vtop"),
        dict(name="Test Variant Top - 3BBB TEST", folder="Test Variant 3BBB", source="Test Variant 3BBB.nif",
             shapes=[("VarTop", "VarTop"), ("CBBE-3BBB", "CBBE-3BBB")], sliders=CBBE_SLIDERS,
             output_path="meshes\\Test\\Variant", output_file="vtop"),
    ], {"Test Variant/Test Variant.nif": [vtop], "Test Variant 3BBB/Test Variant 3BBB.nif": [vtop3, body_shape(body, "CBBE-3BBB")]})

    # --- already converted -------------------------------------------------------
    m3 = mods / "Test Outfits 3BBB"
    top3 = outfit_patch(body, (85, 115), copy_weights=True, name="TopShape")
    _project(m3, "Test Top 3BBB.osp", [dict(name="Test Top - 3BBB", folder="Test Top 3BBB", source="Test Top 3BBB.nif",
                                            shapes=[("TopShape", "TopShape")], sliders=CBBE_SLIDERS,
                                            output_path="meshes\\Test3BBB", output_file="top")],
             {"Test Top 3BBB/Test Top 3BBB.nif": [top3]})
    pants3 = outfit_patch(body, (30, 70), copy_weights=True, name="PantsShape")
    _project(m3, "Test Pants 3BBB.osp", [dict(name="Test Pants - 3BBB", folder="Test Pants 3BBB", source="Test Pants 3BBB.nif",
                                              shapes=[("PantsShape", "PantsShape")], sliders=CBBE_SLIDERS,
                                              output_path="meshes\\Test", output_file="pants")],
             {"Test Pants 3BBB/Test Pants 3BBB.nif": [pants3]})
    twb = outfit_patch(body, (85, 115), copy_weights=True, name="TWBTop")
    _project(m3, "Test TWB.osp", [dict(name="Test Top TWB", folder="Test TWB", source="Test TWB.nif",
                                       shapes=[("TWBTop", "TWBTop")], sliders=CBBE_SLIDERS + TWB_SLIDERS, output_file="twb")],
             {"Test TWB/Test TWB.nif": [twb]})
    # CP5-like case: a jacket carrying 3BBB weights without being a declared conversion
    jk3 = outfit_patch(body, (85, 115), copy_weights=True, name="JacketShape")
    m5 = mods / "Test Clothes Pack"
    _project(m5, "Test Coat.osp", [dict(name="Test Coat v3", folder="Test Coat", source="Test Coat.nif",
                                        shapes=[("JacketShape", "JacketShape")], sliders=CBBE_SLIDERS, output_file="coat")],
             {"Test Coat/Test Coat.nif": [jk3]})
    # suspicious 3BBB: breast weights mirrored to the wrong side
    bad = outfit_patch(body, (85, 115), copy_weights=True, name="BadTop")
    lb = [body.bones.index(f"LBreast_0{i}_skin") for i in (1, 2, 3)]
    rb = [body.bones.index(f"RBreast_0{i}_skin") for i in (1, 2, 3)]
    swap = {**{a: b for a, b in zip(lb, rb)}, **{b: a for a, b in zip(lb, rb)}}
    bad.indices = np.vectorize(lambda x: swap.get(int(x), int(x)))(bad.indices).astype(np.uint8)
    _project(m3, "Test Bad 3BBB.osp", [dict(name="Test Bad Top - 3BBB", folder="Test Bad", source="Test Bad.nif",
                                            shapes=[("BadTop", "BadTop")], sliders=CBBE_SLIDERS, output_file="bad")],
             {"Test Bad/Test Bad.nif": [bad]})

    # --- MO2 override: same OSP in two mods; the higher priority one wins -------------
    for mod_name, set_name in (("Override Low", "Override Top (low)"), ("Override High", "Override Top (high)")):
        mm = mods / mod_name
        _project(mm, "Override.osp", [dict(name=set_name, folder="Override", source="Override.nif",
                                           shapes=[("TopShape", "TopShape")], sliders=CBBE_SLIDERS, output_file="override")],
                 {"Override/Override.nif": [outfit_patch(body, (85, 115), name="TopShape")]})

    # --- disabled mod ---------------------------------------------------------------------
    md = mods / "Disabled Outfit"
    _project(md, "Disabled.osp", [dict(name="Disabled Top - CBBE", folder="Disabled", source="Disabled.nif",
                                       shapes=[("TopShape", "TopShape")], sliders=CBBE_SLIDERS, output_file="disabled")],
             {"Disabled/Disabled.nif": [outfit_patch(body, (85, 115), name="TopShape")]})

    order = [  # highest priority first, as in modlist.txt
        "+Test Clothes Pack", "-Disabled Outfit", "+Override High", "+Override Low", "+Test Outfits 3BBB",
        "+Test Outfits CBBE", "+Outfits_separator", "+CBBE", "+BodySlide and Outfit Studio",
    ]
    prof = mo2 / "profiles" / "Default"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "modlist.txt").write_text("# This file was automatically generated by Mod Organizer.\n" + "\n".join(order) + "\n", encoding="utf-8")
    return FakeInstall(root, game, mo2)


# ---------------------------------------------------------------------------
# Phase 3: one clothing package with every package-level case
# ---------------------------------------------------------------------------

PACKAGE = "Test Package CBBE"


def build_package_install(root: Path, extra: Optional[dict] = None) -> FakeInstall:
    """A small MO2 install with one package, ``Test Package CBBE``:

    * ``Pkg Top - CBBE``: one shape over the breast region → CONVERT (BREAST)
    * ``Pkg Shorts - CBBE``: one shape over butt and thighs → CONVERT (BUTT + THIGH)
    * ``Pkg Suit - CBBE``: top + shorts + boots shapes → CONVERT, per shape BREAST / BUTT+THIGH / NONE
    * ``Pkg Bracelet - CBBE``: waist band away from physics regions → SKIP
    * ``Pkg Shirt - BT3``: BodyTalk → UNSUPPORTED
    * ``Pkg Bra - CBBE`` + ``Pkg Bra - 3BBB TEST`` (same file, same mesh) → REVIEW (manual variant) + KEEP
    * ``Pkg Bodysuit - CBBE``: outfit with an embedded CBBE body → REVIEW (embedded body)
    * ``Pkg Broken - CBBE``: ShapeData missing → ERROR
    """
    game, mo2, mods, body, body_nophys = _base_install(root)
    m = mods / PACKAGE
    top = outfit_patch(body, (85, 115), name="TopShape")
    _project(m, "Pkg Top.osp", [dict(name="Pkg Top - CBBE", folder="Pkg Top", source="Pkg Top.nif",
                                     shapes=[("TopShape", "TopShape")], sliders=CBBE_SLIDERS,
                                     output_path="meshes\\Pkg", output_file="top", gen_weights=False)],
             {"Pkg Top/Pkg Top.nif": [top]})
    shorts = outfit_patch(body, (30, 70), name="ShortsShape", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])
    _project(m, "Pkg Shorts.osp", [dict(name="Pkg Shorts - CBBE", folder="Pkg Shorts", source="Pkg Shorts.nif",
                                        shapes=[("ShortsShape", "ShortsShape")], sliders=CBBE_SLIDERS,
                                        output_path="meshes\\Pkg", output_file="shorts")],
             {"Pkg Shorts/Pkg Shorts.nif": [shorts]}, cloth={"Pkg Shorts/Pkg Shorts.nif": [b"CLOTH-SHORTS" * 40]})
    s_top = outfit_patch(body, (85, 115), name="SuitTop")
    s_bottom = outfit_patch(body, (30, 70), name="SuitShorts", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])
    s_boots = outfit_patch(body, (0, 18), name="SuitBoots", only_bones=["LLeg_Calf", "RLeg_Calf"])
    _project(m, "Pkg Suit.osp", [dict(name="Pkg Suit - CBBE", folder="Pkg Suit", source="Pkg Suit.nif",
                                      shapes=[("SuitTop", "SuitTop"), ("SuitShorts", "SuitShorts"), ("SuitBoots", "SuitBoots")],
                                      sliders=CBBE_SLIDERS, output_path="meshes\\Pkg", output_file="suit")],
             {"Pkg Suit/Pkg Suit.nif": [s_top, s_bottom, s_boots]}, cloth={"Pkg Suit/Pkg Suit.nif": [b"CLOTH-SUIT" * 50]})
    band = outfit_patch(body, (74, 79), name="Band", only_bones=["SPINE1", "SPINE2"])
    _project(m, "Pkg Bracelet.osp", [dict(name="Pkg Bracelet - CBBE", folder="Pkg Bracelet", source="Pkg Bracelet.nif",
                                          shapes=[("Band", "Band")], sliders=CBBE_SLIDERS, output_path="meshes\\Pkg",
                                          output_file="bracelet")],
             {"Pkg Bracelet/Pkg Bracelet.nif": [band]})
    bt = outfit_patch(body, (85, 115), name="BTShape")
    _project(m, "Pkg BT.osp", [dict(name="Pkg Shirt - BT3", folder="Pkg BT", source="Pkg BT.nif",
                                    shapes=[("BTShape", "BTShape")], sliders=[f"BT{x}" for x in "ABCDEFG"], output_file="bt")],
             {"Pkg BT/Pkg BT.nif": [bt]})
    bra = outfit_patch(body, (85, 115), name="BraShape")
    bra3 = outfit_patch(body, (85, 115), copy_weights=True, name="BraShape")
    _project(m, "Pkg Bra.osp", [
        dict(name="Pkg Bra - CBBE", folder="Pkg Bra", source="Pkg Bra.nif", shapes=[("BraShape", "BraShape")],
             sliders=CBBE_SLIDERS, output_path="meshes\\Pkg", output_file="bra"),
        dict(name="Pkg Bra - 3BBB TEST", folder="Pkg Bra 3BBB", source="Pkg Bra 3BBB.nif",
             shapes=[("BraShape", "BraShape"), ("CBBE-3BBB", "CBBE-3BBB")], sliders=CBBE_SLIDERS,
             output_path="meshes\\Pkg", output_file="bra"),
    ], {"Pkg Bra/Pkg Bra.nif": [bra], "Pkg Bra 3BBB/Pkg Bra 3BBB.nif": [bra3, body_shape(body, "CBBE-3BBB")]})
    suit_top = outfit_patch(body, (85, 115), name="BodysuitTop")
    _project(m, "Pkg Bodysuit.osp", [dict(name="Pkg Bodysuit - CBBE", folder="Pkg Bodysuit", source="Pkg Bodysuit.nif",
                                          shapes=[("BodysuitTop", "BodysuitTop"), ("CBBE", "CBBE")], sliders=CBBE_SLIDERS,
                                          output_path="meshes\\Pkg", output_file="bodysuit")],
             {"Pkg Bodysuit/Pkg Bodysuit.nif": [suit_top, body_shape(body_nophys, "CBBE")]})
    _project(m, "Pkg Broken.osp", [dict(name="Pkg Broken - CBBE", folder="Pkg Broken", source="Pkg Broken.nif",
                                        shapes=[("X", "X")], sliders=CBBE_SLIDERS)], {})
    # another mod (not part of the package)
    other = mods / "Other Outfits"
    _project(other, "Other Top.osp", [dict(name="Other Top - CBBE", folder="Other Top", source="Other Top.nif",
                                           shapes=[("TopShape", "TopShape")], sliders=CBBE_SLIDERS, output_file="othertop")],
             {"Other Top/Other Top.nif": [outfit_patch(body, (85, 115), name="TopShape")]})
    for name, fn in (extra or {}).items():
        fn(mods, body, body_nophys)
    order = ["+CBBE 3BBB Auto Conversions", f"+{PACKAGE}", "+Other Outfits", "+CBBE", "+BodySlide and Outfit Studio"]
    order += [f"+{n}" for n in (extra or {})]
    prof = mo2 / "profiles" / "Default"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "modlist.txt").write_text("# This file was automatically generated by Mod Organizer.\n" + "\n".join(order) + "\n",
                                      encoding="utf-8")
    return FakeInstall(root, game, mo2)


# ---------------------------------------------------------------------------
# Phase 3.1: embedded bodies
# ---------------------------------------------------------------------------

BODY_PACKAGE = "Body Package"


def partial_body(body: Body, keep, name: str = "CBBE") -> tuple[SynthShape, np.ndarray]:
    """A body shape with the vertices ``keep`` (bool mask) only, as outfit authors delete hidden body parts.

    Returns the shape and the kept reference vertex indices (body vertex j = reference vertex idx[j])."""
    idx = np.flatnonzero(keep)
    remap = -np.ones(len(body.vertices), dtype=np.int64)
    remap[idx] = np.arange(len(idx))
    tris = body.triangles[np.all(keep[body.triangles], axis=1)]
    return SynthShape(name, body.vertices[idx], remap[tris].astype(np.int32), list(body.bones),
                      body.weights[idx], body.indices[idx]), idx


def _body_entries(target: str, idx: np.ndarray, sliders: list[str], custom: float = 0.0) -> dict:
    """Morph entries of a (partial) body target that equal the reference body's synthetic morphs."""
    pos = {int(r): j for j, r in enumerate(idx)}
    out = {}
    for k, sl in enumerate(sliders):
        ref_rows = np.arange(0, 12) + k
        rows = [pos[int(r)] for r in ref_rows if int(r) in pos]
        if rows:
            out[f"{target}{sl}"] = (np.array(rows), np.full((len(rows), 3), 0.01 * (k + 1) + custom, dtype=np.float32))
    return out


def body_package(mods: Path, body: Body, body_nophys: Body) -> None:
    """``Body Package``: outfits that build an embedded body into their mesh.

    * ``Body Raider - CBBE``: like the real Raider Punk Outfit. Top (BREAST), shorts (BUTT+THIGH), heels and a
      jewel band (NONE), and a *partial* CBBE body (the author deleted the vertices between z 70 and 85)
    * ``Body Sculpted - CBBE``: top + a CBBE body with moved vertices → ambiguous
    * ``Body Custom Morph - CBBE``: top + a CBBE body whose slider morphs differ from the reference → ambiguous
    * ``Body Two - CBBE``: top + two bodies → ambiguous (several bodies)
    * ``Body Full - CBBE``: top + the full CBBE body, with cloth physics data → compatible
    """
    m = mods / BODY_PACKAGE
    z = body.vertices[:, 2]
    pb, idx = partial_body(body_nophys, (z < 70) | (z >= 85))
    top = outfit_patch(body, (85, 115), name="RaiderTop")
    shorts = outfit_patch(body, (30, 70), name="RaiderShorts", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])
    heels = outfit_patch(body, (0, 18), name="RaiderHeels", only_bones=["LLeg_Calf", "RLeg_Calf"])
    jewel = outfit_patch(body, (74, 79), name="RaiderJewel", only_bones=["SPINE1", "SPINE2"])
    shapes = [("RaiderTop", "RaiderTop"), ("RaiderShorts", "RaiderShorts"), ("RaiderHeels", "RaiderHeels"),
              ("RaiderJewel", "RaiderJewel"), ("CBBE", "CBBE")]
    _project(m, "Body Raider.osp", [dict(name="Body Raider - CBBE", folder="Body Raider", source="Body Raider.nif",
                                         shapes=shapes, sliders=CBBE_SLIDERS, output_path="meshes\\Body", output_file="raider",
                                         gen_weights=False)],
             {"Body Raider/Body Raider.nif": [top, shorts, heels, jewel, pb]})
    # the partial body's slider data must correspond to the reference body's morphs of the kept vertices
    osd = m / BS / "ShapeData" / "Body Raider" / "Body Raider.osd"
    from cbbe3bbb.bodyslide.osd import read_osd

    entries = {n: (r["index"], r["diff"]) for n, r in read_osd(osd).entries.items() if not n.startswith("CBBE")}
    entries.update(_body_entries("CBBE", idx, CBBE_SLIDERS))
    write_osd(osd, entries)

    # a second compatible outfit: a top over the full CBBE body, with cloth physics data
    ftop = outfit_patch(body, (85, 115), name="FullTop")
    _project(m, "Body Full.osp", [dict(name="Body Full - CBBE", folder="Body Full", source="Body Full.nif",
                                       shapes=[("FullTop", "FullTop"), ("CBBE", "CBBE")], sliders=CBBE_SLIDERS,
                                       output_path="meshes\\Body", output_file="full")],
             {"Body Full/Body Full.nif": [ftop, body_shape(body_nophys, "CBBE")]},
             cloth={"Body Full/Body Full.nif": [b"CLOTH-FULL" * 30]})
    sculpt = body_shape(body_nophys, "CBBE")
    sculpt.vertices = sculpt.vertices.copy()
    sculpt.vertices[: len(sculpt.vertices) // 5, 0] += 0.4  # a customised body
    for name, extra_body, custom in (("Body Sculpted", [sculpt], 0.0),
                                     ("Body Custom Morph", [body_shape(body_nophys, "CBBE")], 0.3),
                                     ("Body Two", [body_shape(body_nophys, "CBBE"), body_shape(body_nophys, "CBBE Hands")], 0.0)):
        t = outfit_patch(body, (85, 115), name=f"{name.split()[1]}Top")
        shp = [(t.name, t.name)] + [(b.name, b.name) for b in extra_body]
        _project(m, f"{name}.osp", [dict(name=f"{name} - CBBE", folder=name, source=f"{name}.nif", shapes=shp,
                                         sliders=CBBE_SLIDERS, output_path="meshes\\Body", output_file=name.lower().replace(" ", ""))],
                 {f"{name}/{name}.nif": [t] + extra_body})
        if custom:
            osd = m / BS / "ShapeData" / name / f"{name}.osd"
            entries = {n: (r["index"], r["diff"]) for n, r in read_osd(osd).entries.items() if not n.startswith("CBBE")}
            entries.update(_body_entries("CBBE", np.arange(len(body.vertices)), CBBE_SLIDERS, custom))
            write_osd(osd, entries)


# ---------------------------------------------------------------------------
# 0.4.0: minimal Fallout 4 plugins (ARMO/ARMA) for Direct-NIF tests
# ---------------------------------------------------------------------------

def write_plugin(path: Path, addons: list[dict], localized: bool = False) -> None:
    """Test-only FO4 plugin. ``addons``: dicts with editor_id, female / male / female1st / male1st mesh paths
    (relative to meshes/), slots (biped slot numbers) and items: [(armo editor id, display name)]."""
    import struct as _st

    def sub(t, data):
        return t.encode() + _st.pack("<H", len(data)) + data

    def z(s):
        return s.encode("cp1252") + b"\0"

    def rec(t, fid, data):
        return t.encode() + _st.pack("<IIIIHH", len(data), 0, fid, 0, 131, 0) + data

    def grup(label, body):
        return b"GRUP" + _st.pack("<I", 24 + len(body)) + label.encode() + _st.pack("<iIHH", 0, 0, 131, 0) + body

    out = rec("TES4", 0, sub("HEDR", _st.pack("<fII", 1.0, 1, 0x800)))
    if localized:
        out = out[:8] + _st.pack("<I", 0x80) + out[12:]
    arma, armo, fid = b"", b"", 0x01000800
    for a in addons:
        fid += 1
        aid = fid
        flags = sum(1 << (s - 30) for s in a.get("slots", [32]))
        body = sub("EDID", z(a["editor_id"])) + sub("BOD2", _st.pack("<II", flags, 0))
        for key, tag in (("male", "MOD2"), ("female", "MOD3"), ("male1st", "MOD4"), ("female1st", "MOD5")):
            if a.get(key):
                body += sub(tag, z(a[key]))
        arma += rec("ARMA", aid, body)
        for eid, name in a.get("items", []):
            fid += 1
            armo += rec("ARMO", fid, sub("EDID", z(eid)) + sub("FULL", z(name)) + sub("BOD2", _st.pack("<II", flags, 0))
                        + sub("MODL", _st.pack("<I", aid)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out + grup("ARMO", armo) + grup("ARMA", arma))


DIRECT_PACKAGE = "Direct Package"


def direct_package(mods: Path, body: "Body", body_nophys: "Body") -> None:
    """``Direct Package``: built game meshes + a plugin, no BodySlide project (like HN66 EasyGirl)."""
    m = mods / DIRECT_PACKAGE
    d = m / "meshes" / "Direct"

    def mesh(name, shapes, **kw):
        write_nif(d / name, shapes, BONE_POS, **kw)

    upper = ["SPINE1", "SPINE2", "Chest", "Neck", "Pelvis"]  # 5 bones: a garment, not a few-bone rigid accessory
    top = outfit_patch(body_nophys, (85, 115), name="TopShape", only_bones=upper)
    # 0.4.1: game-mesh structure the Direct-NIF conversion must preserve (material, texture set, alpha, segments, extra data)
    top.shader = {"name": "Materials\\Direct\\top.bgsm", "textures": ["textures\\direct\\top_d.dds", "textures\\direct\\top_n.dds"]
                  + [""] * 8, "wet_material": "template/OutfitTemplate_Wet.bgsm"}
    top.alpha = struct.pack("<HB", 0x12ED, 128)
    top.segments = struct.pack("<4I", 3, 0, 0, 7) + b"SEGMENTDATA-TOP" * 3
    mesh("top_F.nif", [top], extra_data=[("BodyPartData", "Top")])
    shorts = outfit_patch(body_nophys, (30, 70), name="ShortsShape", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])
    shorts.shader = {"name": "Materials\\Direct\\shorts.bgsm"}
    shorts.segments = struct.pack("<4I", 2, 0, 0, 5) + b"SEGMENTDATA-SHORTS" * 2
    mesh("shorts_F.nif", [shorts])
    # top (BREAST) + an untouched waist band whose author weights do not sum to 1 (like Raider Punk's Hip_Case)
    s_top = outfit_patch(body_nophys, (85, 115), name="SuitTop", only_bones=upper)
    s_top.shader = {"name": "Materials\\Direct\\suit.bgsm"}
    s_band = outfit_patch(body_nophys, (74, 79), name="SuitBand", only_bones=["SPINE1", "SPINE2"])
    w = s_band.weights.copy()
    w[:, 0] = 0.99
    s_band.weights = w
    s_band.shader = {"name": "Materials\\Direct\\band.bgsm"}
    mesh("suit_F.nif", [s_top, s_band])
    # 0.4.2: a top with every vertex attribute a real game mesh has (UVs, normals, tangents/bitangents, colors, eye data)
    a_top = seam_split(outfit_patch(body_nophys, (85, 115), name="AttrTop", only_bones=upper))
    rng = np.random.default_rng(7)
    nv = len(a_top.vertices)
    a_top.vattrs = {"bitangent_x": rng.random((nv, 1), dtype=np.float32).view(np.uint8).reshape(nv, 4),
                    "uv": rng.random((nv, 2)).astype(np.float16).view(np.uint8).reshape(nv, 4),
                    "normal": rng.integers(0, 256, (nv, 3), dtype=np.uint8), "bitangent_y": rng.integers(0, 256, (nv, 1), dtype=np.uint8),
                    "tangent": rng.integers(0, 256, (nv, 3), dtype=np.uint8), "bitangent_z": rng.integers(0, 256, (nv, 1), dtype=np.uint8),
                    "color": rng.integers(0, 256, (nv, 4), dtype=np.uint8), "eye": rng.random((nv, 1), dtype=np.float32).view(np.uint8).reshape(nv, 4),
                    "pad": rng.integers(0, 256, (nv, 4), dtype=np.uint8)}
    a_top.shader = {"name": "Materials\\Direct\\attrs.bgsm"}
    a_top.segments = struct.pack("<4I", 1, 0, 0, 3) + b"SEGMENTDATA-ATTRS"
    mesh("attrs_F.nif", [a_top])
    mesh("band_F.nif", [outfit_patch(body_nophys, (74, 79), name="Band", only_bones=["SPINE1", "SPINE2"])])
    mesh("keep_F.nif", [outfit_patch(body, (85, 115), copy_weights=True, name="Top3BBB")])
    mesh("male_M.nif", [outfit_patch(body_nophys, (30, 70), name="MaleShorts", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])])
    mesh("unisex.nif", [outfit_patch(body_nophys, (30, 70), name="UniShorts", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])])
    z = body_nophys.vertices[:, 2]
    pb, _ = partial_body(body_nophys, z >= 20)  # feet deleted by the author
    boots = outfit_patch(body_nophys, (0, 18), name="BootShape", only_bones=["LLeg_Calf", "RLeg_Calf"])
    mesh("bootsbody_F.nif", [boots, pb])
    male_body = SynthShape("BaseMaleBody", body_nophys.vertices * np.array([1.25, 1.25, 1.0]), body_nophys.triangles,
                           list(body_nophys.bones), body_nophys.weights, body_nophys.indices)
    mesh("malebody_F.nif", [male_body, outfit_patch(body_nophys, (0, 18), name="MaleBoots", only_bones=["LLeg_Calf", "RLeg_Calf"])])
    pier = outfit_patch(body_nophys, (95, 98), name="Piercing", only_bones=["Chest"])
    mesh("piercing_F.nif", [pier])
    reach = outfit_patch(body_nophys, (30, 70), name="ReachShorts", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])
    v = reach.vertices.copy()
    v[[5, 6], :2] += v[[5, 6], :2] / np.linalg.norm(v[[5, 6], :2], axis=1, keepdims=True) * 6.5
    reach.vertices = v
    mesh("reach_F.nif", [reach])
    far = outfit_patch(body_nophys, (30, 70), name="FarShorts", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])
    v = far.vertices.copy()
    v[[5], :2] += v[[5], :2] / np.linalg.norm(v[[5], :2], axis=1, keepdims=True) * 14.0
    far.vertices = v
    mesh("far_F.nif", [far])
    mesh("cloth_F.nif", [outfit_patch(body_nophys, (30, 70), name="Skirt", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])],
         cloth=[b"CLOTH-SKIRT" * 30])
    mesh("clash_F.nif", [outfit_patch(body_nophys, (85, 115), name="Chest", only_bones=upper)])
    addons = [
        dict(editor_id="AA_Top", female="Direct\\top_F.nif", male="Direct\\male_M.nif",
             items=[("Top_Black", "Direct Top Black"), ("Top_White", "Direct Top White")]),
        dict(editor_id="AA_Top_Red", female="Direct\\TOP_F.nif", items=[("Top_Red", "Direct Top Red")]),
        dict(editor_id="AA_Shorts", female="Direct\\shorts_F.nif", items=[("Shorts", "Direct Shorts")]),
        dict(editor_id="AA_Suit", female="Direct\\suit_F.nif", items=[("Suit", "Direct Suit")]),
        dict(editor_id="AA_Attrs", female="Direct\\attrs_F.nif", items=[("Attrs", "Direct Attribute Top")]),
        dict(editor_id="AA_Band", female="Direct\\band_F.nif", items=[("Band", "Direct Band")]),
        dict(editor_id="AA_Keep", female="Direct\\keep_F.nif", items=[("Keep", "Direct 3BBB Top")]),
        dict(editor_id="AA_Uni", female="Direct\\unisex.nif", male="Direct\\unisex.nif", items=[("Uni", "Direct Unisex")]),
        dict(editor_id="AA_Boots", female="Direct\\bootsbody_F.nif", slots=[33], items=[("Boots", "Direct Boots")]),
        dict(editor_id="AA_MaleBody", female="Direct\\malebody_F.nif", slots=[33], items=[("MBoots", "Direct Male Boots")]),
        dict(editor_id="AA_Pier", female="Direct\\piercing_F.nif", items=[("Pier", "Direct Piercing")]),
        dict(editor_id="AA_Reach", female="Direct\\reach_F.nif", items=[("Reach", "Direct Reach Shorts")]),
        dict(editor_id="AA_Far", female="Direct\\far_F.nif", items=[("Far", "Direct Far Shorts")]),
        dict(editor_id="AA_Cloth", female="Direct\\cloth_F.nif", items=[("Cloth", "Direct Skirt")]),
        dict(editor_id="AA_Clash", female="Direct\\clash_F.nif", items=[("Clash", "Direct Clash Top")]),
        dict(editor_id="AA_Missing", female="Direct\\missing_F.nif", items=[("Missing", "Direct Missing")]),
        dict(editor_id="AA_Vanilla", female="Clothes\\Vanilla\\vanilla_F.nif", items=[("Vanilla", "Direct Vanilla")]),
    ]
    write_plugin(m / "Direct.esp", addons)
    write_nif(mods / "Other Outfits" / "meshes" / "Clothes" / "Vanilla" / "vanilla_F.nif",
              [outfit_patch(body_nophys, (85, 115), name="VanillaTop", only_bones=upper)], BONE_POS)


# ---------------------------------------------------------------------------
# Phase 3.2: a full-length bodysuit (CP5 "bodysuit v1" regression)
# ---------------------------------------------------------------------------

def dense_suit(body: "Body", z_range=(20.0, 118.0), cols: int = 96, step: float = 1.0, offset: float = 0.3,
               name: str = "bodysuit_v1") -> SynthShape:
    """A close-fitting full-length suit with more than 8000 vertices and more than 80 units tall: the size at which the
    analyzer's name/size body rule applies. Rigidly weighted to vanilla bones (no physics bones), like a CBBE outfit."""
    zs = np.arange(z_range[0], z_range[1] + 1e-6, step)
    v = np.array([((RADIUS + offset) * math.cos(2 * math.pi * c / cols), (RADIUS + offset) * math.sin(2 * math.pi * c / cols), z)
                  for z in zs for c in range(cols)], dtype=np.float64)
    bones = ["LLeg_Thigh", "RLeg_Thigh", "Pelvis", "SPINE1", "SPINE2", "Chest"]
    heights = np.array([BONE_POS[b][2] for b in bones])
    W = np.zeros((len(v), 4), dtype=np.float32)
    I = np.zeros((len(v), 4), dtype=np.uint8)
    for k, (x, _y, z) in enumerate(v):
        if z < 50:
            I[k, 0] = 0 if x < 0 else 1
        else:
            I[k, 0] = 2 + int(np.argmin(np.abs(heights[2:] - z)))
        W[k, 0] = 1.0
    return SynthShape(name, v, _grid_tris(len(zs), cols), bones, W, I)


def bodysuit_package(mods: Path, body: "Body", body_nophys: "Body") -> None:
    """``Pkg Catsuit - CBBE`` in the test package: one full-length suit shape (CONVERT BREAST+BUTT+THIGH)."""
    suit = dense_suit(body)
    _project(mods / PACKAGE, "Pkg Catsuit.osp", [dict(name="Pkg Catsuit - CBBE", folder="Pkg Catsuit", source="Pkg Catsuit.nif",
                                                      shapes=[("bodysuit_v1", "bodysuit_v1")], sliders=CBBE_SLIDERS,
                                                      output_path="meshes\\Pkg", output_file="catsuit", gen_weights=False)],
             {"Pkg Catsuit/Pkg Catsuit.nif": [suit]})
