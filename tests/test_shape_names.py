"""0.6.1: exact shape names (real case: Vtaw Overalls Belts lists the shape "Metal Overalls " with a trailing space).

Before 0.6.1 the OSP parser stripped the name, the analysis lost the shape, Outfit Studio still saved it, and validation
rejected the result ("conversion result rejected"). Outfit Studio itself trims TargetMeshes entries, so such a shape is
addressed with an exact anchored pattern (TargetRegex).
"""
import unittest

import tests._util  # noqa: F401  (sys.path)

from cbbe3bbb.bodyslide.osp import parse_osp
from cbbe3bbb.conversion.automation import AutomationScript, AutomationStep, parse_script, pattern_target, target_pattern
from fake_win_os import FakeWinOS
from synth import CBBE_SLIDERS, _project, outfit_patch
from test_batch import setup
from test_mass import fake

PKG = "Spaced Names"


def _spaced(mods, body, body_nophys):
    _project(mods / PKG, "Spaced.osp", [dict(name="Spaced Top", folder="Spaced", source="Spaced.nif",
                                             shapes=[("Top Part ", "Top Part ")], sliders=CBBE_SLIDERS,
                                             output_path="meshes\\Spaced", output_file="spaced")],
             {"Spaced/Spaced.nif": [outfit_patch(body, (85, 115), name="Top Part ")]})


class ShapeNameTests(unittest.TestCase):
    def test_osp_keeps_exact_names(self):
        xml = ('<SliderSetInfo version="1"><SliderSet name="S"><DataFolder>F</DataFolder><SourceFile>x.nif</SourceFile>'
               '<Shape target="Metal Overalls ">Metal Overalls </Shape><Shape target="A">A</Shape>'
               '<Shape target="B">\n   B\n</Shape></SliderSet></SliderSetInfo>')
        import tempfile, pathlib
        p = pathlib.Path(tempfile.mkdtemp()) / "s.osp"
        p.write_text(xml, encoding="utf-8")
        names = [s.name for s in parse_osp(p).sets[0].shapes]
        self.assertEqual(names, ["Metal Overalls ", "A", "B"])

    def test_target_patterns_round_trip(self):
        for n in ("Metal Overalls ", " lead", "a,b (x) [1].*?", "Plain"):
            self.assertEqual(pattern_target(target_pattern(n)), n)
        plain = AutomationScript("t", [AutomationStep("CopyBoneWeights", target_meshes=["A", "B C"])]).to_xml()
        self.assertIn("<TargetRegex>false</TargetRegex>", plain)  # unchanged for normal names (accepted scripts)
        spaced = AutomationScript("t", [AutomationStep("CopyBoneWeights", target_meshes=["A", "Metal Overalls "])]).to_xml()
        self.assertIn("<TargetRegex>true</TargetRegex>", spaced)
        self.assertIn("^Metal Overalls $", spaced)
        self.assertEqual(parse_script(spaced).steps[0].target_meshes, ["A", "Metal Overalls "])

    def test_trailing_space_shape_is_planned_and_converted(self):
        ctl, fi, _ = setup(extra={PKG: _spaced})
        prev = ctl.analyze_package(PKG, write=False)
        p = prev.projects[0]
        self.assertEqual(p.action, "CONVERT", p.reasons)
        plan = p.conversion_plan()
        self.assertEqual([sp.name for sp in plan.shape_plans], ["Top Part "])
        self.assertIn("<TargetRegex>true</TargetRegex>", plan.script_xml)
        r = ctl.pipeline(PKG, "ALL_POSSIBLE", runner=FakeWinOS(conv=fake())).run(prev)
        self.assertEqual(r.items[0]["outcome"], "CONVERTED", r.items[0]["technical"])


if __name__ == "__main__":
    unittest.main()
