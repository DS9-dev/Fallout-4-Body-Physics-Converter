"""0.5.1: post-export proportional skin-weight renormalization (conversion/renormalize.py + batch integration).

Outfit Studio 5.8.2 / nifly normalizes a vertex over all its bones and stores only the 4 largest weights, so a vertex
that got more than 4 bones from CopyBoneWeights is saved with a sum < 1 (real CP5: 0.848–0.95). The repair rescales
only those stored weights (w_i / sum), keeps bone indices and every other byte, refuses every other anomaly, and the
full strict validation runs on the repaired file afterwards.
"""
import hashlib
import json
import os
import shutil
import unittest
from pathlib import Path

import numpy as np

from tests._util import temp_home, tree_digest
from cbbe3bbb.conversion.renormalize import SUM_TOLERANCE, renormalize_skin_weights
from cbbe3bbb.nif.structure import read_structure
from fake_os import FakeOutfitStudio
from synth import BONE_POS, BS, PACKAGE, SynthShape, write_nif
from test_batch import out_mod
from test_mass import SAFE, batch_json, mass_setup

BONES = ["Pelvis", "SPINE1", "SPINE2", "Chest", "LBreast_01_skin"]


def _shape(name, weights, n=None, seed=3):
    w = np.asarray(weights, dtype=np.float32)
    n = len(w)
    rng = np.random.default_rng(seed)
    v = rng.random((n, 3)) * 10
    tris = np.array([[i, (i + 1) % n, (i + 2) % n] for i in range(n)], dtype=np.int32)
    idx = np.tile(np.array([0, 1, 2, 4], dtype=np.uint8), (n, 1))
    va = {"uv": rng.random((n, 2)).astype(np.float16).view(np.uint8).reshape(n, 4),
          "normal": rng.integers(0, 256, (n, 3), dtype=np.uint8), "tangent": rng.integers(0, 256, (n, 3), dtype=np.uint8),
          "color": rng.integers(0, 256, (n, 4), dtype=np.uint8)}
    return SynthShape(name, v, tris, BONES, w, idx, vattrs=va)


def _w(path, name):
    s = read_structure(path).shape(name)
    return s, np.ascontiguousarray(s.fields["skin_weights"]).view("<f2").astype(np.float64).reshape(-1, 4)


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class UnitTests(unittest.TestCase):
    def _write(self, weights_by_shape):
        home = temp_home()
        p = home / "gen.nif"
        write_nif(p, [_shape(n, w, seed=i) for i, (n, w) in enumerate(weights_by_shape.items())], BONE_POS)
        return home, p

    def test_091_and_095_become_1_with_proportions_bone_ids_and_everything_else_kept(self):
        base = np.array([[0.4, 0.3, 0.2, 0.1], [0.55, 0.25, 0.15, 0.05], [0.7, 0.3, 0.0, 0.0], [1.0, 0, 0, 0]])
        w = base.copy()
        w[0] *= 0.91
        w[1] *= 0.95
        home, p = self._write({"Top": w})
        before_st, before = _w(p, "Top")
        raw_bytes = Path(p).read_bytes()
        mtime = os.stat(p).st_mtime_ns
        r = renormalize_skin_weights(p, home / "raw" / "gen.nif", ["Top"])
        self.assertEqual(r.status, "RENORMALIZED", r.reasons)
        self.assertEqual(r.vertices_renormalized, 2)
        self.assertEqual(r.shapes["Top"]["min_sum_before"], 0.91)
        after_st, after = _w(p, "Top")
        self.assertLess(abs(after[0].sum() - 1), SUM_TOLERANCE)
        self.assertLess(abs(after[1].sum() - 1), SUM_TOLERANCE)
        # proportions preserved
        np.testing.assert_allclose(after[0] / after[0].sum(), base[0], atol=2e-3)
        np.testing.assert_allclose(after[1] / after[1].sum(), base[1], atol=2e-3)
        # already normalized vertices: byte-identical
        self.assertTrue(np.array_equal(after_st.fields["skin_weights"][2:], before_st.fields["skin_weights"][2:]))
        # bone ids and every other field unchanged
        for k in after_st.fields:
            if k != "skin_weights":
                self.assertTrue(np.array_equal(after_st.fields[k], before_st.fields[k]), k)
        self.assertTrue(np.array_equal(after_st.triangles, before_st.triangles))
        self.assertEqual(after_st.tail_hash, before_st.tail_hash)
        # raw Outfit Studio file kept byte for byte; modification time kept (execution evidence)
        self.assertEqual((home / "raw" / "gen.nif").read_bytes(), raw_bytes)
        self.assertEqual(os.stat(p).st_mtime_ns, mtime)
        self.assertEqual(r.final_sha256, _sha(p))
        # idempotent: a second run finds nothing to do
        self.assertEqual(renormalize_skin_weights(p, home / "raw2" / "gen.nif", ["Top"]).status, "NOT_NEEDED")

    def test_only_listed_shapes(self):
        w = np.array([[0.4, 0.3, 0.2, 0.1]]) * 0.91
        home, p = self._write({"Top": w.repeat(3, 0), "Other": w.repeat(3, 0)})
        r = renormalize_skin_weights(p, home / "raw.nif", ["Top"])
        self.assertEqual(r.status, "RENORMALIZED")
        self.assertAlmostEqual(_w(p, "Other")[1][0].sum(), 0.91, places=2)
        self.assertAlmostEqual(_w(p, "Top")[1][0].sum(), 1.0, places=2)

    def test_rejections_leave_the_file_untouched(self):
        good = [0.4 * 0.91, 0.3 * 0.91, 0.2 * 0.91, 0.1 * 0.91]
        cases = {
            "zero sum": [0, 0, 0, 0],
            "negative": [0.6, 0.5, -0.1, 0.0],
            "NaN": [np.nan, 0.3, 0.2, 0.1],
            "Inf": [np.inf, 0.3, 0.2, 0.1],
            "deficit with fewer than 4 influences": [0.6, 0.3, 0.0, 0.0],
            "sum above 1": [0.5, 0.4, 0.2, 0.1],
            "sum below 0.5": [0.2, 0.1, 0.05, 0.05],
        }
        for label, bad in cases.items():
            with self.subTest(label):
                home, p = self._write({"Top": [good, bad, [1, 0, 0, 0]]})
                h = _sha(p)
                r = renormalize_skin_weights(p, home / "raw.nif", ["Top"])
                self.assertEqual(r.status, "REFUSED", label)
                self.assertTrue(r.reasons)
                self.assertEqual(_sha(p), h, "a refused repair must not change the file")
                self.assertFalse((home / "raw.nif").exists())


class PipelineTests(unittest.TestCase):
    def _run(self, deficit, per=None, mode="SAFE"):
        ctl, fi, home = mass_setup()
        src_before = tree_digest(fi.mods / PACKAGE)
        runner = FakeOutfitStudio("windows_interactive", source_slider_data=True, weight_deficit=deficit, per_project=per or {})
        r = ctl.pipeline(PACKAGE, mode, runner=runner).run()
        self.assertEqual(tree_digest(fi.mods / PACKAGE), src_before, "source files must stay byte-identical")
        return ctl, fi, r

    def test_deficits_091_095_validate_and_are_recorded(self):
        for deficit in (0.09, 0.05):
            with self.subTest(deficit=deficit):
                ctl, fi, r = self._run(deficit)
                items = {i["project"]: i for i in r.items}
                for n in SAFE:
                    self.assertEqual(items[n]["outcome"], "CONVERTED", (n, items[n]["technical"]))
                bdir = r.batch_dir
                repaired = []
                for e in batch_json(bdir)["projects"]:
                    rn = json.loads(Path(f"{bdir}/projects/{e['id']}/renormalization.json").read_text())
                    if rn["changed"]:
                        repaired.append(e["project"])
                        self.assertTrue(os.path.isfile(rn["raw_path"]))
                        self.assertAlmostEqual(min(v["min_sum_before"] for v in rn["shapes"].values()), 1 - deficit, places=2)
                self.assertIn("Pkg Top - CBBE", repaired)
                man = json.loads((out_mod(fi) / "manifest.json").read_text())
                recs = {c["target"]["project"]: c for c in man["conversions"]}
                top = recs["Pkg Top - CBBE - 3BBB AUTO"]
                self.assertEqual(top["skin_weight_renormalization"]["status"], "RENORMALIZED")
                self.assertGreater(top["skin_weight_renormalization"]["vertices_renormalized"], 0)
                # the committed file is the repaired one: sums are 1
                gen = out_mod(fi) / BS / "ShapeData/Pkg Top - CBBE - 3BBB AUTO/Pkg Top - CBBE - 3BBB AUTO.nif"
                _s, w = _w(gen, "TopShape")
                self.assertLess(float(np.abs(w.sum(1) - 1).max()), SUM_TOLERANCE)

    def test_repair_cannot_hide_another_failure_and_isolation_holds(self):
        ctl, fi, r = self._run(0.09, per={"Pkg Top - CBBE": "embed_ref", "Pkg Catsuit - CBBE": "bad_weights"})
        items = {i["project"]: i for i in r.items}
        self.assertEqual(items["Pkg Top - CBBE"]["batch_status"], "FAILED")
        self.assertTrue(any("CBBE-3BBB body NOT embedded" in t for t in items["Pkg Top - CBBE"]["technical"]))
        self.assertNotEqual(items["Pkg Catsuit - CBBE"]["batch_status"], "COMMITTED")
        e = next(x for x in batch_json(r.batch_dir)["projects"] if x["project"] == "Pkg Catsuit - CBBE")
        rn = json.loads(Path(f"{r.batch_dir}/projects/{e['id']}/renormalization.json").read_text())
        self.assertEqual(rn["status"], "REFUSED")
        for n in ("Pkg Shorts - CBBE", "Pkg Suit - CBBE"):  # the others are converted
            self.assertEqual(items[n]["outcome"], "CONVERTED", items[n]["technical"])
        self.assertFalse((out_mod(fi) / BS / "SliderSets" / "Pkg Top - CBBE - 3BBB AUTO.osp").exists())
        self.assertFalse((out_mod(fi) / BS / "SliderSets" / "Pkg Catsuit - CBBE - 3BBB AUTO.osp").exists())

    def test_normalized_output_is_not_touched(self):
        ctl, fi, r = self._run(0.0)
        for e in batch_json(r.batch_dir)["projects"]:
            rn = json.loads(Path(f"{r.batch_dir}/projects/{e['id']}/renormalization.json").read_text())
            self.assertEqual(rn["status"], "NOT_NEEDED")
            self.assertFalse(os.path.isdir(f"{r.batch_dir}/projects/{e['id']}/generated_raw"))


if __name__ == "__main__":
    unittest.main()
