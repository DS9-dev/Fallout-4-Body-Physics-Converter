import tempfile
import unittest
from pathlib import Path

from tests._util import fake_install
from cbbe3bbb.discovery.install import choose_3bbb_template, detect_installation
from cbbe3bbb.discovery.mo2 import Mo2Error, decode_qt_value, load_mo2_instance, parse_modlist
from cbbe3bbb.discovery.vfs import DataProvider, VirtualFS
from cbbe3bbb.bodyslide.xmlfiles import RefTemplate
from cbbe3bbb.profiles.store import Profile


class Mo2Tests(unittest.TestCase):
    def test_modlist_priority_highest_first(self):
        mods = parse_modlist("# header\n+Top\n-Middle\n+Sep_separator\n*Unmanaged: DLC\n+Bottom\n")
        by = {m.name: m for m in mods}
        self.assertGreater(by["Top"].priority, by["Middle"].priority)
        self.assertGreater(by["Middle"].priority, by["Bottom"].priority)
        self.assertFalse(by["Middle"].enabled)
        self.assertEqual(by["Sep_separator"].kind, "separator")
        self.assertEqual(by["Unmanaged: DLC"].kind, "unmanaged")
        self.assertEqual(by["Bottom"].priority, 0)

    def test_decode_qt_value(self):
        self.assertEqual(decode_qt_value("@ByteArray(Fix F)"), "Fix F")
        self.assertEqual(decode_qt_value('"T:\\\\Fallout 4"'), "T:\\Fallout 4")

    def test_instance(self):
        fi = fake_install()
        inst = load_mo2_instance(fi.mo2_dir)
        self.assertEqual(inst.selected_profile, "Default")
        self.assertEqual(inst.version, "2.5.2")
        self.assertEqual(inst.mods_dir, fi.mo2_dir / "mods")
        self.assertIn("Default", inst.profiles)

    def test_missing_ini(self):
        with self.assertRaises(Mo2Error):
            load_mo2_instance(tempfile.mkdtemp())


class VfsTests(unittest.TestCase):
    def test_priority_and_case_insensitivity(self):
        root = Path(tempfile.mkdtemp())
        provs = []
        for name, prio in (("game", -1), ("low", 1), ("high", 2), ("overwrite", 100)):
            d = root / name / "Tools" / "BodySlide" / "SliderSets"
            d.mkdir(parents=True)
            if name != "overwrite":
                (d / ("A.osp" if name != "high" else "a.OSP")).write_text(name)
            provs.append(DataProvider(name, root / name, prio))
        (root / "overwrite" / "Tools" / "BodySlide" / "SliderSets" / "B.osp").write_text("ow")
        (root / "low" / "Tools" / "BodySlide" / "SliderSets" / "B.osp").write_text("low")
        vfs = VirtualFS.build(provs)
        self.assertEqual(vfs.resolve("SliderSets\\A.osp").provider.name, "high")
        self.assertEqual([v.provider.name for v in vfs.versions("sliderSets/a.osp")], ["high", "low", "game"])
        self.assertEqual(vfs.resolve("SliderSets/B.osp").provider.name, "overwrite")
        winners = {v.key for v in vfs.iter_winners("SliderSets", (".osp",))}
        self.assertEqual(winners, {"slidersets/a.osp", "slidersets/b.osp"})


class InstallTests(unittest.TestCase):
    def test_detect_fake_install(self):
        fi = fake_install()
        inst = detect_installation(Profile(name="t", mo2_dir=str(fi.mo2_dir)))
        self.assertTrue(inst.ok, inst.errors)
        self.assertEqual(inst.game_dir, fi.game_dir)  # taken from ModOrganizer.ini
        self.assertTrue(inst.game_exe_found)
        self.assertEqual(inst.reference_template.display_name, "CBBE 3BBB Body")
        names = {p.name for p in inst.providers}
        self.assertIn("<overwrite>", names)
        self.assertNotIn("Disabled Outfit", names)
        self.assertIsNotNone(inst.bodyslide_exe)
        self.assertEqual(inst.bodyslide_config.target_game, 3)

    def test_include_disabled_ranks_below_everything(self):
        fi = fake_install()
        p = Profile(name="t", mo2_dir=str(fi.mo2_dir))
        p.analysis.include_disabled_mods = True
        inst = detect_installation(p)
        dis = [x for x in inst.providers if x.name == "Disabled Outfit"]
        self.assertEqual(len(dis), 1)
        self.assertEqual(dis[0].kind, "disabled-mod")
        self.assertLess(dis[0].priority, min(x.priority for x in inst.providers if x.kind != "disabled-mod"))

    def test_no_paths(self):
        inst = detect_installation(Profile(name="empty"))
        self.assertFalse(inst.ok)

    def test_game_only(self):
        fi = fake_install()
        inst = detect_installation(Profile(name="g", game_dir=str(fi.game_dir)))
        self.assertTrue(inst.game_exe_found)
        self.assertIsNone(inst.reference_template)  # no BodySlide in the bare game Data folder

    def test_choose_template(self):
        t = [RefTemplate("CBBE Body", "", "CBBE Body", "CBBE", None), RefTemplate("CBBE 3BBB Body Reduced", "", "CBBE 3BBB Body Reduced", "", None),
             RefTemplate("CBBE 3BBB Body", "", "CBBE 3BBB Body", "CBBE-3BBB", None)]
        self.assertEqual(choose_3bbb_template(t).display_name, "CBBE 3BBB Body")
        self.assertEqual(choose_3bbb_template(t, "CBBE 3BBB Body Reduced").display_name, "CBBE 3BBB Body Reduced")
        self.assertIsNone(choose_3bbb_template(t[:1]))


if __name__ == "__main__":
    unittest.main()
