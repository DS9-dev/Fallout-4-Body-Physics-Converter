import unittest

from tests import _util  # noqa: F401
from cbbe3bbb.bodyslide.osp import OspParseError, parse_osp_bytes
from synth import osp_xml

TWO_SETS = osp_xml([
    dict(name="Top - CBBE", folder="Top", source="Top.nif", shapes=[("TopShape", "TopShape"), ("CBBE", "CBBE")],
         sliders=["Breasts", "Butt"], zaps=["Zap Sleeves"], output_path="meshes\\Mod\\Top", output_file="top"),
    dict(name="Top - 3BBB", folder="Top 3BBB", source="Top.nif", shapes=[("TopShape", "TopShape")], sliders=["Breasts"],
         output_path="meshes\\Mod\\Top", output_file="top"),
]).encode()


class OspTests(unittest.TestCase):
    def test_multiple_sets(self):
        osp = parse_osp_bytes(TWO_SETS)
        self.assertEqual([s.name for s in osp.sets], ["Top - CBBE", "Top - 3BBB"])
        a = osp.sets[0]
        self.assertEqual(a.data_folder, "Top")
        self.assertEqual(a.source_file, "Top.nif")
        self.assertEqual([s.name for s in a.shapes], ["TopShape", "CBBE"])
        self.assertIn("Breasts", a.slider_names)
        self.assertEqual(list(a.zap_sliders), ["Zap Sleeves"])
        self.assertTrue(a.gen_weights)
        self.assertEqual(a.output_game_path.lower(), "meshes/mod/top/top")
        self.assertEqual(len(a.sliders_for_target("TopShape")), 2)

    def test_malformed_xml(self):
        with self.assertRaises(OspParseError):
            parse_osp_bytes(b"<SliderSetInfo><SliderSet name='x'>")

    def test_wrong_root(self):
        with self.assertRaises(OspParseError):
            parse_osp_bytes(b"<Something/>")

    def test_cp1252_fallback(self):
        data = TWO_SETS.replace(b'encoding="UTF-8"', b'encoding="UTF-8"').replace(b"Top - CBBE", "Tóp - CBBE".encode("cp1252"))
        osp = parse_osp_bytes(data)
        self.assertEqual(osp.sets[0].name, "Tóp - CBBE")

    def test_set_without_source_is_kept_with_empty_source(self):
        data = b'<SliderSetInfo version="1"><SliderSet name="x"><DataFolder>d</DataFolder></SliderSet></SliderSetInfo>'
        osp = parse_osp_bytes(data)
        self.assertEqual(len(osp.sets), 1)
        self.assertEqual(osp.sets[0].source_file, "")


if __name__ == "__main__":
    unittest.main()
