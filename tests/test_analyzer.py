"""End-to-end analysis of the synthetic installation (the regression cases)."""
import threading
import unittest

from tests._util import fake_install, tree_digest
from cbbe3bbb.analysis.analyzer import LibraryAnalyzer
from cbbe3bbb.analysis.cache import AnalysisCache
from cbbe3bbb.discovery.install import detect_installation
from cbbe3bbb.profiles.store import Profile
from tests._util import temp_home


def run(include_disabled=False, cache=None, cancel=None, progress=None):
    fi = fake_install()
    p = Profile(name="t", mo2_dir=str(fi.mo2_dir))
    p.analysis.include_disabled_mods = include_disabled
    inst = detect_installation(p)
    return LibraryAnalyzer(inst, cache=cache, cancel=cancel, progress=progress, workers=2).run()


class AnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fi = fake_install()
        cls.before = tree_digest(fi.root)
        cls.rep = run()
        cls.by = {p.project: p for p in cls.rep.projects}

    def act(self, name):
        self.assertIn(name, self.by, sorted(self.by))
        return self.by[name].action

    def test_reference_loaded(self):
        self.assertEqual(self.rep.reference["template"], "CBBE 3BBB Body")
        self.assertEqual(set(self.rep.reference["physics_regions"]), {"BREAST", "BUTT", "THIGH"})
        self.assertEqual(len(self.rep.reference["physics_bones"]), 12)

    def test_convert_candidate(self):
        pr = self.by["Test Top - CBBE"]
        self.assertEqual(pr.action, "CONVERT", pr.reasons)
        self.assertEqual(pr.breast, "HIGH")
        self.assertEqual(pr.existing_3bbb, "NO")
        self.assertTrue(pr.includes_body_shape)
        self.assertTrue(any("embedded non-3BBB body" in w for w in pr.warnings))
        self.assertTrue(pr.reasons)

    def test_already_3bbb_is_kept(self):
        self.assertEqual(self.act("Test Top - 3BBB"), "KEEP")
        self.assertEqual(self.act("Test Pants - 3BBB"), "KEEP")
        self.assertEqual(self.by["Test Top - 3BBB"].family.label, "CBBE 3BBB")

    def test_twb_detected(self):
        pr = self.by["Test Top TWB"]
        self.assertEqual(pr.family.family, "TWB")
        self.assertEqual(pr.family.label, "TWB 3BBB")
        self.assertEqual(pr.action, "KEEP")

    def test_superseded_original(self):
        pr = self.by["Test Pants - CBBE"]
        self.assertEqual(pr.action, "SKIP")
        self.assertIn("Test Pants - 3BBB", pr.superseded_by)

    def test_same_file_manual_variant(self):  # the Raider Punk Top V2 situation
        orig = self.by["Test Variant Top - CBBE"]
        test = self.by["Test Variant Top - 3BBB TEST"]
        self.assertEqual(orig.action, "CONVERT", orig.reasons)
        self.assertEqual(orig.superseded_by, "")
        self.assertTrue(any("3BBB variant in the same project file" in w for w in orig.warnings))
        self.assertEqual(test.action, "KEEP", test.reasons)
        self.assertTrue(test.includes_body_shape)

    def test_undeclared_3bbb_outerwear_is_review(self):  # the CP5 jacket case
        pr = self.by["Test Coat v3"]
        self.assertEqual(pr.action, "REVIEW")
        self.assertTrue(any("not a declared 3BBB conversion" in r for r in pr.reasons))

    def test_suspicious_3bbb_weights_are_review(self):
        pr = self.by["Test Bad Top - 3BBB"]
        self.assertEqual(pr.action, "REVIEW")
        self.assertTrue(any("opposite side" in r for r in pr.reasons), pr.reasons)

    def test_outerwear_is_review(self):
        self.assertEqual(self.act("Test Jacket - CBBE"), "REVIEW")

    def test_far_garment_is_review(self):
        pr = self.by["Test Poncho - CBBE"]
        self.assertEqual(pr.action, "REVIEW")
        self.assertTrue(any("beyond the search radius" in r for r in pr.reasons))

    def test_no_physics_region_is_skip(self):
        self.assertEqual(self.act("Test Boots - CBBE"), "SKIP")

    def test_body_projects_are_skip(self):
        self.assertEqual(self.act("CBBE 3BBB Body"), "SKIP")
        self.assertEqual(self.act("CBBE Body"), "SKIP")

    def test_bodytalk_unsupported(self):
        self.assertEqual(self.act("Test Shirt - BT3"), "UNSUPPORTED")

    def test_missing_shapedata_error(self):
        pr = self.by["Test Missing - CBBE"]
        self.assertEqual(pr.action, "ERROR")
        self.assertIn("ShapeData NIF not found", pr.errors[0])

    def test_malformed_osp_error(self):
        pr = self.by["Broken"]
        self.assertEqual(pr.action, "ERROR")
        self.assertIn("malformed", pr.errors[0])

    def test_mo2_override_winner_only(self):
        self.assertIn("Override Top (high)", self.by)
        self.assertNotIn("Override Top (low)", self.by)
        self.assertEqual(self.by["Override Top (high)"].osp_also_provided_by, ["Override Low"])

    def test_disabled_mod_excluded_by_default(self):
        self.assertNotIn("Disabled Top - CBBE", self.by)

    def test_every_project_has_reasons(self):
        for pr in self.rep.projects:
            self.assertIn(pr.action, {"KEEP", "CONVERT", "SKIP", "REVIEW", "UNSUPPORTED", "ERROR"})
            self.assertTrue(pr.reasons, pr.project)

    def test_analysis_is_read_only(self):
        self.assertEqual(tree_digest(fake_install().root), self.before)

    def test_stats(self):
        self.assertEqual(self.rep.stats["projects"], len(self.rep.projects))
        self.assertEqual(sum(self.rep.stats["actions"].values()), len(self.rep.projects))
        self.assertEqual(self.rep.stats["osp_parse_failures"], 1)


class AnalyzerOptionTests(unittest.TestCase):
    def test_include_disabled(self):
        rep = run(include_disabled=True)
        pr = next(p for p in rep.projects if p.project == "Disabled Top - CBBE")
        self.assertEqual(pr.provider_kind, "disabled-mod")
        self.assertEqual(pr.action, "CONVERT")

    def test_cache_roundtrip(self):
        home = temp_home()
        c = AnalysisCache(home / "a.sqlite")
        first = run(cache=c)
        c.close()
        c = AnalysisCache(home / "a.sqlite")
        second = run(cache=c)
        c.close()
        self.assertGreater(second.stats["cached"], 5)
        a = {p.project: (p.action, p.reasons) for p in first.projects}
        b = {p.project: (p.action, p.reasons) for p in second.projects}
        self.assertEqual(a, b)

    def test_cancel(self):
        ev = threading.Event()
        calls = []

        def progress(d, t, m):
            calls.append(m)
            if t:
                ev.set()

        rep = run(cancel=ev.is_set, progress=progress)
        self.assertTrue(rep.cancelled)
        self.assertLess(len(rep.projects), 19)
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
