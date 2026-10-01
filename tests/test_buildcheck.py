"""Phase 3.1 fix: post-build verification and in-game item lookup (read-only)."""
import json
import os
import shutil
import struct
import unittest
import zlib
from pathlib import Path

from tests._util import tree_digest
from cbbe3bbb.cli import main as cli_main
from cbbe3bbb.conversion.gamedata import (
    item_name_mismatch, items_for_mesh, normalize_mesh_path, package_items_for_mesh, read_plugin_armors, variant_tokens,
)
from test_body import BODY_PACKAGE, BS, FULL, RAIDER, by_name, out_mod, run, setup


def _sub(t: str, data: bytes) -> bytes:
    return t.encode() + struct.pack("<H", len(data)) + data


def _z(s: str) -> bytes:
    return s.encode("cp1252") + b"\0"


def _rec(t: str, fid: int, data: bytes, compress: bool = False) -> bytes:
    flags = 0
    if compress:
        data = struct.pack("<I", len(data)) + zlib.compress(data)
        flags = 0x40000
    return t.encode() + struct.pack("<IIIIHH", len(data), flags, fid, 0, 131, 0) + data


def _grup(label: str, body: bytes) -> bytes:
    return b"GRUP" + struct.pack("<I", 24 + len(body)) + label.encode() + struct.pack("<iIHH", 0, 0, 131, 0) + body


def write_plugin(path: Path, items, compress_armo: bool = False, localized: bool = False) -> None:
    """items: (armo editor id, item name, arma editor id, female 3rd-person mesh). Test-only minimal FO4 plugin."""
    hedr = _sub("HEDR", struct.pack("<fII", 1.0, 2 * len(items), 0x800)) + _sub("CNAM", _z("test"))
    out = _rec("TES4", 0, hedr)
    if localized:
        out = out[:8] + struct.pack("<I", 0x80) + out[12:]
    arma, armo = b"", b""
    # an unrelated top-level group that must be skipped
    other = _grup("WEAP", _rec("WEAP", 0x01000500, _sub("EDID", _z("SomeGun"))))
    for i, (armo_id, name, arma_id, mesh) in enumerate(items):
        aid, oid = 0x01000800 + i, 0x01000900 + i
        arma += _rec("ARMA", aid, _sub("EDID", _z(arma_id)) + _sub("BOD2", struct.pack("<II", 4, 0))
                     + _sub("MOD2", _z(mesh[:-4] + "_M.nif")) + _sub("MOD3", _z(mesh)))
        full = _sub("FULL", struct.pack("<I", 1234 + i)) if localized else _sub("FULL", _z(name))
        armo += _rec("ARMO", oid, _sub("EDID", _z(armo_id)) + full + _sub("MODL", struct.pack("<I", aid)), compress_armo)
    path.write_bytes(out + other + _grup("ARMO", armo) + _grup("ARMA", arma))


RAIDER_ITEMS = [("Armor_Body_Raider_2", "Body Raider Outfit V2", "AA_Body_Raider_2", "Body\\raider.nif"),
                ("Armor_Body_Full", "Body Full", "AA_Body_Full", "Body\\full.nif")]


class GameDataTests(unittest.TestCase):
    def test_variant_tokens_and_mismatch(self):
        self.assertEqual(variant_tokens("MM69 Raider Punk Outfit - CBBE"), set())
        self.assertEqual(variant_tokens("MM69 Raider Punk Outfit 2 - CBBE"), {"2"})
        self.assertEqual(variant_tokens("MM69 Raider Punk Outfit v3 - CBBE"), {"3"})
        self.assertEqual(variant_tokens("Raider Punk Outfit V2"), {"2"})
        self.assertEqual(variant_tokens("MM69 Raider Punk Top V2 - 3BBB TEST"), {"2"})
        self.assertEqual(variant_tokens("Raider Punk Top V1"), set())
        # the real Raider Punk mapping: project "Outfit" builds the item "Outfit V2" and vice versa
        self.assertTrue(item_name_mismatch("MM69 Raider Punk Outfit - CBBE", "Raider Punk Outfit V2"))
        self.assertTrue(item_name_mismatch("MM69 Raider Punk Outfit 2 - CBBE", "Raider Punk Outfit"))
        for proj, item in (("MM69 Raider Punk Top - CBBE", "Raider Punk Top"),
                           ("MM69 Raider Punk Shorts V2 - CBBE", "Raider Punk Shorts V2"),
                           ("MM69 Raider Punk Outfit v3 - CBBE", "Raider Punk Outfit V3"),
                           ("MM69 Raider Punk Bottom - CBBE", "Raider Punk Pants & Boots")):
            self.assertFalse(item_name_mismatch(proj, item), (proj, item))
        self.assertFalse(item_name_mismatch("X 2 - CBBE", "(localized string 5)"))
        self.assertEqual(normalize_mesh_path("meshes\\MM69 Raider Punk\\raiderPunk"), "mm69 raider punk/raiderpunk.nif")

    def test_plugin_parse_items_compressed_and_localized(self):
        d = Path(self.id().replace(".", "_"))
        import tempfile

        d = Path(tempfile.mkdtemp())
        for kw in ({}, {"compress_armo": True}):
            write_plugin(d / "t.esp", RAIDER_ITEMS, **kw)
            pa = read_plugin_armors(d / "t.esp")
            self.assertEqual(pa.errors, [])
            self.assertEqual(len(pa.addons), 2)
            self.assertEqual(len(pa.armors), 2)
            it = items_for_mesh(pa, "meshes\\Body\\raider")
            self.assertEqual([(i.armor_name, i.role) for i in it], [("Body Raider Outfit V2", "female 3rd person")])
            self.assertEqual(items_for_mesh(pa, "meshes\\Body\\nothing"), [])
        write_plugin(d / "t.esp", RAIDER_ITEMS, localized=True)
        pa = read_plugin_armors(d / "t.esp")
        self.assertTrue(pa.localized)
        self.assertTrue(items_for_mesh(pa, "Body/raider.nif")[0].armor_name.startswith("(localized"))
        (d / "bad.esp").write_bytes(b"nope")
        items, notes = package_items_for_mesh(d, "Body/raider.nif")
        self.assertEqual(len(items), 1)
        self.assertTrue(any("bad.esp" in n for n in notes))
        shutil.rmtree(d)


class BuildCheckTests(unittest.TestCase):
    """verify-build on a committed MODE B conversion and a simulated BodySlide build."""

    def _committed(self):
        ctl, fi, home = setup()
        write_plugin(fi.mods / BODY_PACKAGE / "Body.esp", RAIDER_ITEMS)
        prev = ctl.analyze_package(BODY_PACKAGE, allow_body_replacement=True, write=False)
        b = by_name(prev)
        self.assertTrue(any("GAME_ITEM_NAME_MISMATCH" in w and "Body Raider Outfit V2" in w for w in b[RAIDER].warnings))
        self.assertFalse(any("GAME_ITEM_NAME_MISMATCH" in w for w in b[FULL].warnings))
        self.assertEqual(b[RAIDER].game_items[0]["armor_name"], "Body Raider Outfit V2")
        self.assertEqual((b[RAIDER].action, b[RAIDER].status), ("CONVERT", "EMBEDDED_BODY_REPLACEMENT"))  # unchanged
        bdir, s, rows = run(ctl, prev, [RAIDER])
        self.assertEqual(rows[RAIDER]["status"], "VALIDATED", rows[RAIDER])
        ctl.commit_batch(bdir)
        return ctl, fi, home

    def _gen(self, fi):
        return out_mod(fi) / BS / "ShapeData/Body Raider - CBBE - 3BBB AUTO/Body Raider - CBBE - 3BBB AUTO.nif"

    def _built(self, fi):
        return fi.mods / BODY_PACKAGE / "meshes/Body/raider.nif"

    def _check(self, ctl):
        res = ctl.verify_build("Body Raider - CBBE - 3BBB AUTO")
        self.assertEqual(len(res), 1)
        return res[0]

    def test_not_built_then_ok_then_mismatch_then_stale(self):
        ctl, fi, home = self._committed()
        before = tree_digest(fi.mods)
        r = self._check(ctl)
        self.assertEqual(r.status, "NOT_BUILT")
        # BodySlide build (weights are copied through unchanged; positions get the preset)
        self._built(fi).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._gen(fi), self._built(fi))
        before = tree_digest(fi.mods)
        r = self._check(ctl)
        self.assertEqual(r.status, "BUILD_OK", r.text())
        names = [c["name"] for c in r.checks]
        self.assertIn("CBBE: 3BBB physics bones survived the build", names)
        self.assertIn("RaiderTop: bone list and weights identical to the generated ShapeData", names)
        self.assertEqual([i["armor_name"] for i in r.game_items], ["Body Raider Outfit V2"])
        self.assertTrue(any("GAME_ITEM_NAME_MISMATCH" in w for w in r.warnings))
        self.assertEqual(tree_digest(fi.mods), before, "verify-build must not write into any mod")
        self.assertTrue(list((ctl.paths.reports / "builds").glob("*.json")))
        # the game mesh is an unconverted build (e.g. the original project was built last)
        src = fi.mods / BODY_PACKAGE / BS / "ShapeData/Body Raider/Body Raider.nif"
        shutil.copyfile(src, self._built(fi))
        r = self._check(ctl)
        self.assertEqual(r.status, "BUILD_MISMATCH")
        self.assertIn("CBBE: 3BBB physics bones survived the build", [c["name"] for c in r.failed])
        # a mesh older than the commit was never rebuilt
        shutil.copyfile(self._gen(fi), self._built(fi))
        rec = json.loads((out_mod(fi) / "manifest.json").read_text())["conversions"][-1]
        import time

        t = time.mktime(time.strptime(rec["timestamp"], "%Y-%m-%dT%H:%M:%S")) - 3600
        os.utime(self._built(fi), (t, t))
        self.assertEqual(self._check(ctl).status, "BUILD_STALE")

    def test_competing_provider_wins(self):
        ctl, fi, home = self._committed()
        self._built(fi).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._gen(fi), self._built(fi))
        # the overwrite folder is above every mod: an old unconverted copy there is what the game loads
        ov = fi.mo2_dir / "overwrite" / "meshes/Body/raider.nif"
        ov.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fi.mods / BODY_PACKAGE / BS / "ShapeData/Body Raider/Body Raider.nif", ov)
        ctl.detect(ctl.installation.profile)
        r = self._check(ctl)
        self.assertEqual(r.winner["provider"], "<overwrite>", r.text())
        self.assertEqual(r.status, "BUILD_MISMATCH")
        self.assertTrue(any("other mod(s) also provide" in w for w in r.warnings))

    def test_cli_verify_build(self):
        ctl, fi, home = self._committed()
        self._built(fi).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._gen(fi), self._built(fi))
        rc = cli_main(["--home", str(ctl.paths.root), "verify-build", "--mo2", str(fi.mo2_dir),
                       "--generated", "Body Raider - CBBE - 3BBB AUTO"])
        self.assertEqual(rc, 0)
        rc = cli_main(["--home", str(ctl.paths.root), "verify-build", "--mo2", str(fi.mo2_dir), "--generated", "Nope"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
