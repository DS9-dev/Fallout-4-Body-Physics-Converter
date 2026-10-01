"""Read-only lookup of which in-game items use a built mesh (Phase 3.1 fix).

A BodySlide project builds a mesh file; the game shows whatever the plugin's
armor addon (ARMA) points at. Project names and in-game item names do not have
to agree: ``MM69 Raider Punk.esp`` shows ``raiderPunk.nif`` (built by the
project "Outfit - CBBE") as the item "Raider Punk Outfit V2", and
``raiderPunk_2.nif`` (project "Outfit 2 - CBBE") as "Raider Punk Outfit". This
module parses the package's own plugins (ARMO/ARMA records only, never written)
so reports can name the item that must be tested in game and warn when the
names disagree.
"""
from __future__ import annotations

import re
import struct
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional

__all__ = ["ArmorAddon", "Armor", "GameItem", "PluginArmors", "read_plugin_armors", "items_for_mesh",
           "package_items_for_mesh", "variant_tokens", "item_name_mismatch", "normalize_mesh_path",
           "resolve_data_file", "PLUGIN_SUFFIXES", "slot_list", "load_order_items_for_mesh"]

PLUGIN_SUFFIXES = (".esp", ".esm", ".esl")
MODEL_ROLES = {"MOD2": "male 3rd person", "MOD3": "female 3rd person", "MOD4": "male 1st person",
               "MOD5": "female 1st person"}


@dataclass
class ArmorAddon:
    form_id: int
    editor_id: str = ""
    models: dict[str, str] = field(default_factory=dict)  # MOD2..MOD5 -> path relative to meshes/
    biped_slots: int = 0  # BOD2 first-person flags (bit n = slot 30+n)


@dataclass
class Armor:
    form_id: int
    editor_id: str = ""
    name: str = ""
    addons: list[int] = field(default_factory=list)
    biped_slots: int = 0


@dataclass
class GameItem:
    plugin: str
    armor_editor_id: str
    armor_form_id: str
    armor_name: str
    addon_editor_id: str
    addon_form_id: str
    role: str  # which ARMA model slot uses the mesh
    biped_slots: list[int] = field(default_factory=list)  # biped slots of the addon (33 = body)

    def to_dict(self) -> dict:
        return asdict(self)

    def label(self) -> str:
        return (f"'{self.armor_name or self.armor_editor_id}' (ARMO {self.armor_editor_id} {self.armor_form_id}, "
                f"ARMA {self.addon_editor_id} {self.addon_form_id}, {self.role}, {self.plugin})")


@dataclass
class PluginArmors:
    plugin: str
    localized: bool = False
    addons: dict[int, ArmorAddon] = field(default_factory=dict)
    armors: dict[int, Armor] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def normalize_mesh_path(p: str) -> str:
    """'meshes\\A\\b.nif' / 'A/b' -> 'a/b.nif' (relative to meshes/, lower case)."""
    s = p.replace("\\", "/").strip().strip("/").lower()
    if s.startswith("meshes/"):
        s = s[len("meshes/"):]
    if not s.endswith(".nif"):
        s += ".nif"
    return s


def _zstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("cp1252", errors="replace")


def _subrecords(d: bytes) -> Iterator[tuple[str, bytes]]:
    o, big = 0, None
    while o + 6 <= len(d):
        t = d[o:o + 4].decode("latin1")
        size = struct.unpack_from("<H", d, o + 4)[0]
        o += 6
        if t == "XXXX":
            big = struct.unpack_from("<I", d, o)[0]
            o += size
            continue
        if big is not None:
            size, big = big, None
        yield t, d[o:o + size]
        o += size


def _records(b: bytes, off: int, end: int, out: PluginArmors, depth: int = 0) -> Iterator[tuple[str, int, bytes]]:
    while off + 24 <= end:
        t = b[off:off + 4].decode("latin1")
        size = struct.unpack_from("<I", b, off + 4)[0]
        if t == "GRUP":
            if size < 24 or off + size > end:
                out.errors.append(f"bad group at {off}")
                return
            label = b[off + 8:off + 12].decode("latin1")
            gtype = struct.unpack_from("<i", b, off + 12)[0]
            if depth > 0 or gtype != 0 or label in ("ARMO", "ARMA"):  # skip other top-level groups quickly
                yield from _records(b, off + 24, off + size, out, depth + 1)
            off += size
            continue
        flags, fid = struct.unpack_from("<II", b, off + 8)
        data = b[off + 24:off + 24 + size]
        if flags & 0x40000:
            try:
                data = zlib.decompress(data[4:])
            except zlib.error as exc:
                out.errors.append(f"record {fid:08x}: {exc}")
                data = b""
        yield t, fid, data
        off += 24 + size


def read_plugin_armors(path: Path) -> PluginArmors:
    """Parse the ARMO/ARMA records of one Fallout 4 plugin (read-only)."""
    res = PluginArmors(plugin=path.name)
    try:
        b = path.read_bytes()
    except OSError as exc:
        res.errors.append(str(exc))
        return res
    if b[:4] != b"TES4":
        res.errors.append("not a plugin (no TES4 header)")
        return res
    hsize, hflags = struct.unpack_from("<II", b, 4)
    res.localized = bool(hflags & 0x80)
    for t, fid, d in _records(b, 24 + hsize, len(b), res):
        if t == "ARMA":
            a = ArmorAddon(form_id=fid)
            for st, sd in _subrecords(d):
                if st == "EDID":
                    a.editor_id = _zstr(sd)
                elif st in MODEL_ROLES:
                    a.models[st] = _zstr(sd)
                elif st == "BOD2" and len(sd) >= 4:
                    a.biped_slots = struct.unpack_from("<I", sd)[0]
            res.addons[fid] = a
        elif t == "ARMO":
            a2 = Armor(form_id=fid)
            for st, sd in _subrecords(d):
                if st == "EDID":
                    a2.editor_id = _zstr(sd)
                elif st == "FULL" and not a2.name:
                    a2.name = (f"(localized string {struct.unpack_from('<I', sd)[0]})"
                               if res.localized and len(sd) == 4 else _zstr(sd))
                elif st == "MODL" and len(sd) >= 4:
                    a2.addons.append(struct.unpack_from("<I", sd)[0])
                elif st == "BOD2" and len(sd) >= 4:
                    a2.biped_slots = struct.unpack_from("<I", sd)[0]
            res.armors[fid] = a2
    return res


def slot_list(flags: int) -> list[int]:
    return [30 + i for i in range(32) if flags >> i & 1]


def items_for_mesh(pa: PluginArmors, mesh: str) -> list[GameItem]:
    """In-game items of one plugin whose armor addon uses ``mesh`` (any model slot)."""
    want = normalize_mesh_path(mesh)
    out: list[GameItem] = []
    for aid, addon in pa.addons.items():
        roles = [MODEL_ROLES[k] for k, v in addon.models.items() if normalize_mesh_path(v) == want]
        if not roles:
            continue
        armors = [a for a in pa.armors.values() if aid in a.addons] or [Armor(form_id=0, editor_id="(no ARMO)")]
        for arm in armors:
            out.append(GameItem(plugin=pa.plugin, armor_editor_id=arm.editor_id, armor_form_id=f"{arm.form_id:08X}",
                                armor_name=arm.name, addon_editor_id=addon.editor_id, addon_form_id=f"{aid:08X}",
                                role=", ".join(roles)))
    return out


_PLUGIN_CACHE: dict[tuple[str, float, int], PluginArmors] = {}


def package_items_for_mesh(mod_root: Optional[Path], mesh: str) -> tuple[list[GameItem], list[str]]:
    """Items of the plugins shipped in ``mod_root`` (top level) that use ``mesh``; plus notes."""
    notes: list[str] = []
    if mod_root is None or not Path(mod_root).is_dir():
        return [], ["package folder not available: in-game items not looked up"]
    plugins = sorted(p for p in Path(mod_root).iterdir() if p.is_file() and p.suffix.lower() in PLUGIN_SUFFIXES)
    if not plugins:
        return [], ["the package ships no plugin: the in-game item cannot be named"]
    items: list[GameItem] = []
    for p in plugins:
        st = p.stat()
        key = (str(p), st.st_mtime, st.st_size)
        pa = _PLUGIN_CACHE.get(key)
        if pa is None:
            pa = _PLUGIN_CACHE[key] = read_plugin_armors(p)
        notes += [f"{p.name}: {e}" for e in pa.errors[:3]]
        items += items_for_mesh(pa, mesh)
    return items, notes


MAX_PLUGIN_BYTES = 64 * 1024 * 1024  # masters like Fallout4.esm are never read


def _plugin(p: Path) -> PluginArmors:
    st = p.stat()
    key = (str(p), st.st_mtime, st.st_size)
    pa = _PLUGIN_CACHE.get(key)
    if pa is None:
        pa = _PLUGIN_CACHE[key] = read_plugin_armors(p)
    return pa


def load_order_items_for_mesh(providers, mesh: str) -> list[GameItem]:
    """Phase 3.2: items of the plugins of every *enabled* mod (top level, ≤ 64 MB) that use ``mesh``.

    For packages whose BodySlide files come without a plugin (CP5: "Clothes Pack 5 CBBE BodySlide" builds meshes that
    "Clothes Pack 5 by DonEb14n.esp" uses), so a user sees the in-game item name. Read-only."""
    items: list[GameItem] = []
    for prov in providers or []:
        if getattr(prov, "kind", "") != "mod":
            continue
        root = Path(prov.root)
        try:
            plugins = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in PLUGIN_SUFFIXES)
        except OSError:
            continue
        for p in plugins:
            try:
                if p.stat().st_size > MAX_PLUGIN_BYTES:
                    continue
                items += items_for_mesh(_plugin(p), mesh)
            except OSError:
                continue
    return items


_WORD = re.compile(r"[a-z0-9]+")
_FAMILY = {"cbbe", "3bbb", "auto", "bt3", "bt4", "abody", "a", "body", "fg", "ft"}


def variant_tokens(name: str) -> set[str]:
    """Variant markers of a project/item name: '2', 'v2', 'V3', 'ver2' -> {'2'}, {'3'} ..."""
    s = name.lower()
    s = re.sub(r"\s-\s.*$", "", s)  # "X - CBBE", "X - CBBE - 3BBB AUTO"
    toks = _WORD.findall(s)
    if toks and re.fullmatch(r"[a-z]+\d+", toks[0]) and len(toks) > 1:  # author tag like "MM69"
        toks = toks[1:]
    out = set()
    for t in toks:
        m = re.fullmatch(r"(?:v|ver|version)?(\d+)", t)
        if m and t not in _FAMILY:
            out.add(str(int(m.group(1))))
    out.discard("1")  # "V1" / "1" mean the base item
    return out


def item_name_mismatch(project: str, item_name: str) -> bool:
    """True when project and in-game item carry different variant markers (e.g. 'Outfit' vs 'Outfit V2')."""
    if not item_name or item_name.startswith("(localized"):
        return False
    return variant_tokens(project) != variant_tokens(item_name)


def resolve_data_file(providers, rel: str) -> list[tuple[str, Path, int, float]]:
    """All providers of Data/``rel`` (highest priority first): (provider name, path, size, mtime)."""
    from ..discovery.vfs import find_case_insensitive

    out = []
    for prov in sorted(providers, key=lambda p: p.priority, reverse=True):
        p = find_case_insensitive(prov.root, rel)
        if p is not None and p.is_file():
            st = p.stat()
            out.append((prov.name, p, st.st_size, st.st_mtime))
    return out
