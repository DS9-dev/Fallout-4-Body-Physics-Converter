"""0.3.3: vertices out of Copy Bone Weights' reach, and untouched shapes renormalized by Outfit Studio's save.

Real case (Raider Punk Outfit 2, 2026-09-28): two Pants vertices (466, 771) are 6.58 units from the nearest
CBBE-3BBB vertex. With radius 5 Outfit Studio zeroed every reference bone and left them with no weight at all
(validation: "weight sum range 0.000..1.000"). And Hip_Case, not a copy target, came back with max change 0.0068
because nifly divides each vertex's weights by their sum on save (source sums of 0.9916).
"""
import json
import unittest
from pathlib import Path

import numpy as np

from tests._util import temp_home, tree_digest
from cbbe3bbb.app.controller import AppController, AppPaths
from cbbe3bbb.conversion.plan import ConversionPlan, build_script, plan_shape_reach
from cbbe3bbb.conversion.validate import untouched_weight_change
from cbbe3bbb.profiles.store import Profile
from fake_os import FakeOutfitStudio, make_fake_runtime
from synth import BS, CBBE_SLIDERS, _project, build_package_install, outfit_patch

PKG = "Reach Package"
PANTS = "Reach Pants - CBBE"
FAR = "Reach Far - CBBE"


def _push(shape, idx, dist):
    v = shape.vertices.copy()
    radial = v[idx, :2] / np.linalg.norm(v[idx, :2], axis=1, keepdims=True)
    v[idx, :2] += radial * dist
    shape.vertices = v
    return shape


def reach_package(mods, body, body_nophys):
    m = mods / PKG
    pants = outfit_patch(body, (30, 70), name="Pants", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])
    _push(pants, [5, 6], 6.5)  # a belt loop sticking out: no reference vertex within radius 5
    pouch = outfit_patch(body, (74, 79), name="Pouch", only_bones=["SPINE1", "SPINE2"])
    w = pouch.weights.copy()
    w[:, 0] = 0.99  # the author's weights do not sum to 1 (like Hip_Case: 0.9916)
    pouch.weights = w
    _project(m, "Reach Pants.osp", [dict(name=PANTS, folder="Reach Pants", source="Reach Pants.nif",
                                         shapes=[("Pants", "Pants"), ("Pouch", "Pouch")], sliders=CBBE_SLIDERS,
                                         output_path="meshes\\Reach", output_file="pants")],
             {"Reach Pants/Reach Pants.nif": [pants, pouch]})
    far = outfit_patch(body, (30, 70), name="FarPants", only_bones=["Pelvis", "LLeg_Thigh", "RLeg_Thigh"])
    _push(far, [5], 14.0)  # beyond twice the radius: no safe radius
    _project(m, "Reach Far.osp", [dict(name=FAR, folder="Reach Far", source="Reach Far.nif",
                                       shapes=[("FarPants", "FarPants")], sliders=CBBE_SLIDERS,
                                       output_path="meshes\\Reach", output_file="far")],
             {"Reach Far/Reach Far.nif": [far]})


def setup():
    home = temp_home()
    fi = build_package_install(home / "inst", extra={PKG: reach_package})
    rt = make_fake_runtime(home / "os", (5, 8, 2, 0))
    ctl = AppController(AppPaths(home / "tool"))
    p = Profile(name="t", mo2_dir=str(fi.mo2_dir))
    p.conversion.outfitstudio_dir = str(rt)
    ctl.detect(p)
    return ctl, fi


def by_name(prev):
    return {p.project: p for p in prev.projects}


class ReachTests(unittest.TestCase):
    def test_plan_raises_radius_only_for_the_unreachable_shape(self):
        ctl, fi = setup()
        b = by_name(ctl.analyze_package(PKG, write=False))
        p = b[PANTS]
        self.assertEqual(p.action, "CONVERT", p.reasons)
        plan = ConversionPlan.from_dict(p.plan)
        sp = next(s for s in plan.shape_plans if s.name == "Pants")
        self.assertEqual(sp.out_of_reach_vertices, [5, 6])
        self.assertGreater(sp.search_radius, 5.0)
        self.assertLessEqual(sp.search_radius, 10.0)
        self.assertIn("Pouch", plan.untouched_shapes)
        steps = [s for s in build_script(plan).steps if s.type == "CopyBoneWeights"]
        self.assertEqual([(s.target_meshes, s.params["ProximityRadius"]) for s in steps], [(["Pants"], sp.search_radius)])
        self.assertTrue(any("have no reference vertex within radius 5" in w for w in plan.warnings))
        # a vertex beyond twice the radius: REVIEW, never converted with a guessed radius
        self.assertEqual(b[FAR].action, "REVIEW")
        self.assertTrue(any("no safe radius" in r for r in b[FAR].reasons + b[FAR].warnings), b[FAR].reasons)

    def test_other_projects_keep_the_project_radius(self):
        ctl, fi = setup()
        for p in ctl.analyze_package("Test Package CBBE", write=False).projects:
            for sp in (p.plan or {}).get("shape_plans", []):
                self.assertEqual(sp.get("search_radius", 0.0), 0.0, (p.project, sp["name"]))
                self.assertEqual(sp.get("out_of_reach_vertices", []), [], (p.project, sp["name"]))

    def test_convert_validates_and_old_radius_would_leave_vertices_unweighted(self):
        ctl, fi = setup()
        before = tree_digest(fi.mods / PKG)
        prev = ctl.analyze_package(PKG, write=False)
        prev.select([PANTS])
        bdir, s = ctl.convert_batch(prev, runner=FakeOutfitStudio("windows_interactive"))
        rows = {r["project"]: r for r in s["projects"]}
        self.assertEqual(rows[PANTS]["status"], "VALIDATED", rows[PANTS])
        res = json.loads(next((bdir / "projects").glob("*/result.json")).read_text())
        checks = {c["name"]: c for c in res["validation"]["checks"]}
        self.assertTrue(checks["Pouch: weights unchanged"]["ok"], checks["Pouch: weights unchanged"])
        self.assertIn("after Outfit Studio's per-vertex normalization", checks["Pouch: weights unchanged"]["detail"])
        self.assertIn("raw 0.0098", checks["Pouch: weights unchanged"]["detail"])
        self.assertEqual(tree_digest(fi.mods / PKG), before)
        # the same project planned with the old behaviour (one radius for every shape) must FAIL validation
        ctl2, fi2 = setup()
        prev2 = ctl2.analyze_package(PKG, write=False)
        pp = by_name(prev2)[PANTS]
        for sp in pp.plan["shape_plans"]:
            sp["search_radius"] = 0.0
        prev2.select([PANTS])
        bdir2, s2 = ctl2.convert_batch(prev2, runner=FakeOutfitStudio("windows_interactive"))
        r2 = {r["project"]: r for r in s2["projects"]}[PANTS]
        self.assertEqual(r2["status"], "FAILED")
        self.assertIn("Pants: weights normalized", r2["failed_checks"])
        res2 = json.loads(next((bdir2 / "projects").glob("*/result.json")).read_text())
        det = next(c["detail"] for c in res2["validation"]["checks"] if c["name"] == "Pants: weights normalized")
        self.assertIn("vertices WITHOUT any weight: 5, 6", det)

    def test_real_change_to_an_untouched_shape_still_fails(self):
        ctl, fi = setup()
        prev = ctl.analyze_package(PKG, write=False)
        prev.select([PANTS])
        bdir, s = ctl.convert_batch(prev, runner=FakeOutfitStudio("windows_interactive", per_project={PANTS: "untouched_changed"}))
        r = {x["project"]: x for x in s["projects"]}[PANTS]
        self.assertEqual(r["status"], "FAILED")
        self.assertIn("Pouch: weights unchanged", r["failed_checks"])

    def test_untouched_weight_change_semantics(self):
        bones = ["A", "B", "C"]
        src = np.array([[0.8003, 0.1412, 0.05], [0.5, 0.5, 0.0]], dtype=np.float32)  # sum 0.9915 / 1.0
        gen = src / src.sum(1, keepdims=True)
        sem, raw, _ = untouched_weight_change(bones, gen, bones, src)
        self.assertLess(sem, 1e-3)
        self.assertGreater(raw, 5e-3)  # the old check (raw < 1e-3) failed here
        moved = gen.copy()
        moved[1] = [0.6, 0.4, 0.0]
        self.assertGreater(untouched_weight_change(bones, moved, bones, src)[0], 1e-3)
        new_bone = gen.copy()
        new_bone[1] = [0.5, 0.4995, 0.0005]  # tiny weight on a new bone for that vertex: below 0.001, ignored
        self.assertLess(untouched_weight_change(bones, new_bone, bones, src)[0], 1e-3)
        other = gen.copy()
        other[1] = [0.5, 0.49, 0.01]  # a new influence
        self.assertGreater(untouched_weight_change(bones, other, bones, src)[0], 1e-3)
        self.assertGreaterEqual(untouched_weight_change(["A", "B", "D"], gen, bones, src)[0], 1e-3)

    def test_plan_shape_reach_unit(self):
        from cbbe3bbb.analysis.reference import ReferenceBody

        g = np.stack(np.meshgrid(np.arange(0, 20.0), np.arange(0, 20.0), indexing="ij"), -1).reshape(-1, 2)
        verts = np.c_[g, np.zeros(len(g))]
        ref = ReferenceBody(display_name="r", set_name="", shape_name="", source="", vertices=verts, bones=["A"],
                            weights=np.ones((len(verts), 1), np.float32), physics_bones=[], physics_regions={},
                            side_sign={}, fingerprint="")
        near = np.array([[5.0, 5.0, 1.0], [6.0, 6.0, 1.0]])
        self.assertEqual(plan_shape_reach(ref, near, 5.0, 4, False)["far"], [])
        far = np.array([[5.0, 5.0, 1.0], [10.0, 10.0, 6.5]])
        r = plan_shape_reach(ref, far, 5.0, 4, False)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["far"], [1])
        self.assertEqual(r["changed"], [1])
        self.assertEqual(r["radius"], 7.0)
        r = plan_shape_reach(ref, far, 5.0, 4, True)
        self.assertFalse(r["ok"])
        r = plan_shape_reach(ref, np.array([[10.0, 10.0, 12.0]]), 5.0, 4, False)
        self.assertFalse(r["ok"])
        self.assertIn("more than", r["reason"])


if __name__ == "__main__":
    unittest.main()
