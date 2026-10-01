"""Phase 3.2 Part A/B: the CP5 'bodysuit v1' regression.

Real batch 20260929-222016: 'CP5_DonEb14n bodysuit v1' came back REVIEW with
  * "predicted vs actual physics weights consistent — agreement n/a (≥ 0.90 required)"
  * "analyzer classifies generated project as 3BBB KEEP — SKIP: project contains only a body/reference shape"
although the generated ShapeData NIF was correct (geometry identical, 12 planned physics bones, full-copy prediction met).

Root cause (RESEARCH.md §14): Outfit Studio 5.8.2 saves the generated project with ``<Shape DataFolder="CP5_DonEb14n
bodysuit v1">`` because the outfit keeps reading its slider data from the source project's folder. The analyzer took ANY
per-shape DataFolder as body-name evidence, and a full-length suit (34 852 vertices, > 80 units tall) then met the
name/size body rule: the shape was analysed as a body (no region metrics → agreement n/a; project → SKIP "only a body").
The two small pilot bikinis were below the size limit, so they passed. Fix: only a DataFolder that names a body/reference
project folder is body evidence. Validation thresholds are unchanged.
"""
import unittest

import numpy as np

from tests._util import temp_home  # noqa: F401 - sets up sys.path
from cbbe3bbb.analysis.shape_metrics import MetricSettings, analyze_shape, body_data_folder
from cbbe3bbb.bodyslide.osp import ShapeEntry
from test_batch import by_name, run, setup
from synth import PACKAGE, bodysuit_package, dense_suit, make_body


class BodyFolderTests(unittest.TestCase):
    def test_body_and_reference_folders_are_body_evidence(self):
        for f in ("CBBE", "CBBE Vanilla", "CBBE Reduced", "TWB Vanilla", "BodyTalk3", "CBBE;TWB 3BBB Body",
                  "Smooth Feet High Heel Body - TWB;Body References", "CBBE Helper References - 3BBB;Extra References",
                  "CBBE 3BBB Body"):
            self.assertTrue(body_data_folder(f), f)

    def test_outfit_folders_are_not(self):
        # generated projects (source outfit folder) and merged outfit projects found in the real library
        for f in ("CP5_DonEb14n bodysuit v1", "3BBB TWB Leotard", "3BBB TWB Tao's Latex Catsuit", "ApalLeotardTbackShoes",
                  "MM69 Raider Punk Top - CBBE", "CP5_Mashup-LeggingsV3-TShirtV3", "DickyPinup", ""):
            self.assertFalse(body_data_folder(f), f)


class ShapeAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests._util import temp_home
        from synth import build_package_install
        from cbbe3bbb.app.controller import AppController, AppPaths
        from cbbe3bbb.profiles.store import Profile

        home = temp_home()
        fi = build_package_install(home / "inst")
        ctl = AppController(AppPaths(home / "tool"))
        ctl.detect(Profile(name="t", mo2_dir=str(fi.mo2_dir)))
        cls.kb = ctl._knowledge_base()
        cls.body = make_body()

    def _shape(self, s):
        from cbbe3bbb.nif.reader import read_nif
        from synth import BONE_POS, write_nif
        from tests._util import temp_home

        p = temp_home() / "s.nif"
        write_nif(p, [s], BONE_POS)
        return read_nif(p).shapes[0]

    def test_large_suit_with_its_source_folder_is_an_outfit(self):
        suit = dense_suit(self.body)
        self.assertGreaterEqual(len(suit.vertices), 8000)  # the size where the name/size body rule applies
        shp = self._shape(suit)
        sa = analyze_shape(shp, ShapeEntry("bodysuit_v1", "bodysuit_v1", data_folder="Pkg Catsuit"), self.kb, MetricSettings())
        self.assertFalse(sa.is_body, sa.body_match)
        self.assertEqual(set(sa.regions), {"BREAST", "BUTT", "THIGH"})

    def test_same_shape_in_a_body_folder_is_still_a_body(self):
        shp = self._shape(dense_suit(self.body))
        sa = analyze_shape(shp, ShapeEntry("bodysuit_v1", "bodysuit_v1", data_folder="CBBE"), self.kb, MetricSettings())
        self.assertTrue(sa.is_body)
        self.assertEqual(sa.regions, {})

    def test_reference_shape_is_never_outfit_geometry(self):
        from synth import body_shape

        shp = self._shape(body_shape(self.body, "CBBE-3BBB"))
        for df in ("", "CP5_DonEb14n bodysuit v1", "CBBE"):
            sa = analyze_shape(shp, ShapeEntry("CBBE-3BBB", "CBBE-3BBB", data_folder=df), self.kb, MetricSettings())
            self.assertTrue(sa.is_body, df)


class BodysuitBatchTests(unittest.TestCase):
    """The generated project is saved exactly like the real Outfit Studio 5.8.2 saved bodysuit v1."""

    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, _ = setup(extra={"__bodysuit": bodysuit_package})

    def _convert(self, pmode=None):
        prev = self.ctl.analyze_package(PACKAGE, write=False)
        p = by_name(prev)["Pkg Catsuit - CBBE"]
        self.assertEqual((p.action, p.confidence, p.selectable), ("CONVERT", "HIGH", True), p.reasons)
        self.assertEqual(sorted(p.regions), ["BREAST", "BUTT", "THIGH"])
        prev.select(["Pkg Catsuit - CBBE"])
        per = {"Pkg Catsuit - CBBE": pmode} if pmode else {}
        return run(self.ctl, prev, mode="windows_interactive", per_project=per, source_slider_data=True)

    def test_bodysuit_validates_with_strict_checks(self):
        bdir, summary, fake, rows = self._convert()
        r = rows["Pkg Catsuit - CBBE"]
        self.assertEqual(r["status"], "VALIDATED", r.get("reasons"))
        text = next(bdir.glob("projects/*Catsuit*/logs/validation.txt")).read_text(encoding="utf-8")
        osp = next(bdir.glob("Data/Tools/BodySlide/SliderSets/*Catsuit*AUTO.osp")).read_text(encoding="utf-8")
        self.assertIn('DataFolder="Pkg Catsuit"', osp)  # saved like the real 5.8.2 save
        self.assertIn("[PASS] bodysuit_v1: predicted vs actual physics weights consistent: agreement breast", text)
        self.assertIn("(≥ 0.90 required)", text)  # threshold unchanged
        self.assertIn("[PASS] analyzer classifies generated project as 3BBB KEEP: KEEP", text)
        self.assertIn("[PASS] CBBE-3BBB body NOT embedded", text)
        self.assertNotIn("agreement n/a", text)

    def test_defects_still_fail(self):
        for mode, check in (("embed_ref", "CBBE-3BBB body NOT embedded"),
                            ("no_weights", "bodysuit_v1: breast 3BBB bones present")):
            _bdir, _s, _f, rows = self._convert(mode)
            r = rows["Pkg Catsuit - CBBE"]
            self.assertNotEqual(r["status"], "VALIDATED", mode)
            self.assertTrue(any(check in x for x in r.get("reasons", [])), (mode, r.get("reasons")))


if __name__ == "__main__":
    unittest.main()
