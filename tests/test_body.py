"""Phase 3.1: embedded CBBE body → CBBE-3BBB body replacement (MODE B)."""
import json
import os
import time
import unittest
from pathlib import Path

import numpy as np

from tests._util import temp_home, tree_digest
from cbbe3bbb.app.controller import AppController, AppPaths
from cbbe3bbb.conversion import automation as A
from cbbe3bbb.conversion.body import (
    BODY_ALREADY_3BBB, BODY_AMBIGUOUS, BODY_COMPATIBLE, BODY_UNSUPPORTED, IDENTITY_RADIUS, identify_body,
)
from cbbe3bbb.conversion.plan import ConversionPlan, build_script, sha256_file
from cbbe3bbb.nif.reader import read_nif
from cbbe3bbb.profiles.store import Profile
from fake_os import FakeOutfitStudio, make_fake_runtime
from synth import (
    BODY_PACKAGE, BONE_POS, CBBE_SLIDERS, PACKAGE, SynthShape, body_package, body_shape, build_package_install, make_body,
    outfit_patch, partial_body, write_nif,
)

BS = Path("Tools/BodySlide")
RAIDER = "Body Raider - CBBE"
FULL = "Body Full - CBBE"


def setup(allowed=None):
    home = temp_home()
    fi = build_package_install(home / "inst", extra={BODY_PACKAGE: body_package})
    rt = make_fake_runtime(home / "os", (5, 8, 2, 0))
    ctl = AppController(AppPaths(home / "tool"))
    p = Profile(name="t", mo2_dir=str(fi.mo2_dir))
    p.conversion.outfitstudio_dir = str(rt)
    if allowed is not None:
        p.conversion.allowed_projects = list(allowed)
    ctl.detect(p)
    return ctl, fi, home


def by_name(prev):
    return {p.project: p for p in prev.projects}


def out_mod(fi):
    return fi.mods / "CBBE 3BBB Auto Conversions"


def run(ctl, prev, names, **fake):
    prev.select(names)
    f = FakeOutfitStudio(fake.pop("mode", "windows_interactive"), **fake)
    bdir, summary = ctl.convert_batch(prev, runner=f)
    return bdir, summary, {p["project"]: p for p in summary["projects"]}


def result(bdir, project):
    b = json.loads((bdir / "batch.json").read_text())
    pid = next(e["id"] for e in b["projects"] if e["project"] == project)
    for n in ("result.revalidated.json", "result.json"):
        p = bdir / "projects" / pid / n
        if p.is_file():
            return json.loads(p.read_text())


def failed(row):
    return row["failed_checks"]


class BodyIdentityTests(unittest.TestCase):
    """identify_body on synthetic shapes (the reference = the synthetic CBBE-3BBB body)."""

    @classmethod
    def setUpClass(cls):
        cls.body3 = make_body(True)
        cls.body0 = make_body(False)
        d = temp_home()
        write_nif(d / "ref.nif", [body_shape(cls.body3, "CBBE-3BBB")], BONE_POS)
        write_nif(d / "plain.nif", [body_shape(cls.body0, "CBBE")], BONE_POS)
        cls.ref = read_nif(d / "ref.nif").shapes[0]
        cls.plain = read_nif(d / "plain.nif").shapes[0]
        cls.d = d
        cls.phys = {b for b in cls.body3.bones if b.endswith("_skin")}
        n = cls.ref.num_vertices
        cls.ref_morphs = {sl: np.full((n, 3), 0.01 * (k + 1)) for k, sl in enumerate(CBBE_SLIDERS)}

    def ident(self, shape, morphs=None, name_hint=True):
        from cbbe3bbb.bodyslide.osp import SliderSet

        m = morphs if morphs is not None else {sl: v[: shape.num_vertices] for sl, v in self.ref_morphs.items()}
        return identify_body(shape, "CBBE", SliderSet(name="x", output_path="x", output_file="x", data_folder="x", source_file="x.nif"), m, self.ref,
                             self.ref_morphs, self.phys, name_hint)

    def _shape(self, synth, name="s"):
        write_nif(self.d / f"{name}.nif", [synth], BONE_POS)
        return read_nif(self.d / f"{name}.nif").shapes[0]

    def test_full_body_compatible(self):
        bi = self.ident(self.plain)
        self.assertEqual((bi.classification, bi.confidence), (BODY_COMPATIBLE, "HIGH"), bi.reasons)
        self.assertEqual(bi.coincident_fraction, 1.0)
        self.assertEqual(bi.triangle_subset, 1.0)
        self.assertGreater(bi.morph_sliders_compared, 0)

    def test_partial_body_compatible_and_geometry_evidence(self):
        z = self.body0.vertices[:, 2]
        pb, idx = partial_body(self.body0, (z < 70) | (z >= 85))
        shp = self._shape(pb, "partial")
        morphs = {sl: v[idx] for sl, v in self.ref_morphs.items()}
        bi = self.ident(shp, morphs)
        self.assertEqual(bi.classification, BODY_COMPATIBLE, bi.reasons)
        self.assertLess(bi.reference_coverage, 0.95)  # geometry/topology evidence: a vertex subset of the reference
        self.assertTrue(any("subset" in e for e in bi.evidence))

    def test_name_alone_is_insufficient(self):
        patch = self._shape(outfit_patch(self.body0, (40, 110), name="CBBE"), "patch")
        bi = self.ident(patch, {})
        self.assertNotEqual(bi.classification, BODY_COMPATIBLE)
        renamed = self._shape(body_shape(self.body0, "Torso"), "torso")
        bi2 = self.ident(renamed, name_hint=False)
        self.assertEqual(bi2.classification, BODY_COMPATIBLE)  # geometry, not the name, decides
        self.assertIn("name/data folder hint: no", " ".join(bi2.evidence))

    def test_sculpted_and_remeshed_bodies_are_ambiguous(self):
        sc = body_shape(self.body0, "CBBE")
        sc.vertices = sc.vertices.copy()
        sc.vertices[:200, 0] += 0.5
        self.assertEqual(self.ident(self._shape(sc, "sculpt")).classification, BODY_AMBIGUOUS)
        rm = body_shape(self.body0, "CBBE")
        t = rm.triangles.copy()
        t[:500] = t[:500][:, [0, 2, 1]] if False else np.roll(t[:500], 7, axis=0)  # triangles rearranged
        t[:300, 2] = t[300:600, 2]
        rm.triangles = t
        self.assertEqual(self.ident(self._shape(rm, "remesh")).classification, BODY_AMBIGUOUS)

    def test_custom_morphs_are_ambiguous(self):
        m = {sl: v + 0.3 for sl, v in self.ref_morphs.items()}
        bi = self.ident(self.plain, m)
        self.assertEqual(bi.classification, BODY_AMBIGUOUS)
        self.assertTrue(bi.morph_sliders_differing)

    def test_already_3bbb_is_not_replaced(self):
        bi = self.ident(self.ref)
        self.assertEqual(bi.classification, BODY_ALREADY_3BBB)

    def test_other_body_family_unsupported(self):
        other = body_shape(self.body0, "CBBE")
        other.vertices = other.vertices * np.array([1.2, 1.2, 1.0])
        self.assertEqual(self.ident(self._shape(other, "other")).classification, BODY_UNSUPPORTED)


class BodyPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, cls.home = setup()
        cls.before = tree_digest(cls.fi.mods / BODY_PACKAGE)
        cls.off = cls.ctl.analyze_package(BODY_PACKAGE, write=False)
        cls.on = cls.ctl.analyze_package(BODY_PACKAGE, allow_body_replacement=True)

    def test_option_off_keeps_phase3_behaviour(self):
        b = by_name(self.off)
        for n in (RAIDER, FULL, "Body Sculpted - CBBE", "Body Two - CBBE", "Body Custom Morph - CBBE"):
            self.assertEqual((b[n].action, b[n].status), ("REVIEW", "EMBEDDED_BODY"), n)
            self.assertFalse(b[n].selectable)
        self.assertEqual(b[RAIDER].conversion_plan().conversion_mode, "outfit_only")
        self.assertFalse(self.off.allow_body_replacement)
        # the main Phase 3 package is unaffected by the option
        main_off = self.ctl.analyze_package(PACKAGE, write=False)
        main_on = self.ctl.analyze_package(PACKAGE, write=False, allow_body_replacement=True)
        self.assertEqual({p.project: p.status for p in main_off.projects if p.project != "Pkg Bodysuit - CBBE"},
                         {p.project: p.status for p in main_on.projects if p.project != "Pkg Bodysuit - CBBE"})

    def test_option_on_promotes_only_safe_projects(self):
        b = by_name(self.on)
        for n in (RAIDER, FULL):
            self.assertEqual((b[n].action, b[n].status, b[n].confidence), ("CONVERT", "EMBEDDED_BODY_REPLACEMENT", "HIGH"))
            self.assertTrue(b[n].selectable)
            self.assertFalse(b[n].selected, "MODE B is never selected by default")
        self.assertEqual(b["Body Sculpted - CBBE"].status, "EMBEDDED_BODY_AMBIGUOUS")
        self.assertEqual(b["Body Custom Morph - CBBE"].status, "EMBEDDED_BODY_AMBIGUOUS")
        self.assertEqual(b["Body Two - CBBE"].status, "MULTIPLE_BODIES")
        self.assertEqual(self.on.selected(), [])

    def test_mode_b_plan_and_script(self):
        pp = by_name(self.on)[RAIDER]
        plan = pp.conversion_plan()
        br = plan.body_replacement
        self.assertEqual(plan.conversion_mode, "embedded_body_replacement")
        self.assertEqual(plan.replaced_body_shapes, ["CBBE"])
        self.assertNotIn("CBBE", plan.untouched_shapes)
        self.assertEqual(sorted(plan.untouched_shapes), ["RaiderHeels", "RaiderJewel"])
        for k in ("source_body_shape", "source_body_classification", "source_body_evidence", "source_body_geometry_signature",
                  "target_body_project", "target_body_set", "target_body_shape", "target_body_sha256",
                  "target_body_geometry_signature", "replacement_method", "slider_mapping", "zap_mapping",
                  "validation_expectations"):
            self.assertIn(k, br)
        ref_nif = self.fi.mods / "CBBE" / BS / "ShapeData/CBBE 3BBB/CBBE 3BBB.nif"
        self.assertEqual(br["target_body_sha256"], sha256_file(ref_nif))  # target-reference identity stored
        self.assertEqual((br["target_body_shape"], br["target_body_set"]), ("CBBE-3BBB", "CBBE 3BBB Body"))
        sc = A.parse_script(plan.script_xml)
        cloth_cbw = [s for s in sc.steps if s.type == "CopyBoneWeights" and float(s.params["ProximityRadius"]) >= 1]
        body_cbw = [s for s in sc.steps if s.type == "CopyBoneWeights" and float(s.params["ProximityRadius"]) < 1]
        self.assertNotIn("CBBE", [t for s in cloth_cbw for t in s.target_meshes])  # never a clothing target
        self.assertEqual([s.target_meshes for s in body_cbw], [["CBBE"]])
        self.assertEqual((float(body_cbw[0].params["ProximityRadius"]), body_cbw[0].params["MaxResults"]), (IDENTITY_RADIUS, "1"))
        self.assertEqual(sorted(t for s in cloth_cbw for t in s.target_meshes), ["RaiderShorts", "RaiderTop"])
        imp = [s for s in sc.steps if s.type == "ImportSliderData"]
        self.assertEqual([s.params["SliderDataFile"] for s in imp], ["ShapeData/Body Raider/Body Raider.osd"])
        save = next(s for s in sc.steps if s.type == "SaveProject")
        self.assertEqual(save.params["AutoCopyRef"], "false")
        A.check_invariants(plan.script_xml)
        shapes = {s.name: s for s in pp.shapes}
        self.assertEqual(shapes["CBBE"].decision, "BODY_REPLACE")
        self.assertEqual(shapes["RaiderTop"].regions, ["BREAST"])
        self.assertEqual(shapes["RaiderShorts"].regions, ["BUTT", "THIGH"])
        self.assertEqual((shapes["RaiderHeels"].decision, shapes["RaiderJewel"].decision), ("NONE", "NONE"))
        self.assertTrue(any(c.startswith("body replacement:") for c in pp.expected_checks))

    def test_preview_shows_both_sections_and_writes_nothing(self):
        from cbbe3bbb.conversion.package import project_detail_lines

        text = "\n".join(project_detail_lines(by_name(self.on)[RAIDER]))
        self.assertIn("OUTFIT PHYSICS TRANSFER", text)
        self.assertIn("BODY REPLACEMENT", text)
        self.assertIn("BODY_CBBE_COMPATIBLE", text)
        self.assertIn("intentionally contains the CBBE-3BBB body", text)
        problems, scripts = self.ctl.plan_batch(self.on, select=[RAIDER])  # Dry Run
        self.assertEqual(problems, [])
        self.assertTrue(any("vertex identity" in x for x in scripts.values()))
        self.assertFalse(out_mod(self.fi).exists())
        self.assertFalse((self.ctl.paths.root / "work").exists())
        self.assertEqual(tree_digest(self.fi.mods / BODY_PACKAGE), self.before)

    def test_invariants_for_mode_b_steps(self):
        plan = by_name(self.on)[RAIDER].conversion_plan()
        steps = build_script(plan).steps
        bad = [st for st in steps]
        body = next(st for st in bad if st.type == "CopyBoneWeights" and float(st.params["ProximityRadius"]) < 1)
        body.params["MaxResults"] = 4
        with self.assertRaises(A.ScriptInvariantError):
            A.check_invariants(A.AutomationScript("x", steps=bad))
        steps = build_script(plan).steps
        next(st for st in steps if st.type == "ImportSliderData").params["SliderDataFile"] = "C:/elsewhere/x.osd"
        with self.assertRaises(A.ScriptInvariantError):
            A.check_invariants(A.AutomationScript("x", steps=steps))


class BodyBatchTests(unittest.TestCase):
    def test_convert_validate_commit_one_mode_b_project(self):
        ctl, fi, _ = setup()
        before = tree_digest(fi.mods / BODY_PACKAGE)
        prev = ctl.analyze_package(BODY_PACKAGE, allow_body_replacement=True, write=False)
        bdir, s, rows = run(ctl, prev, [RAIDER])
        self.assertEqual(rows[RAIDER]["status"], "VALIDATED", rows[RAIDER])
        r = result(bdir, RAIDER)
        names = [c["name"] for c in r["validation"]["checks"]]
        for n in ("body replacement: exactly one body in the output", "body replacement: body weights identical to the CBBE-3BBB reference",
                  "body replacement: body slider morphs identical to the source and resolvable",
                  "CBBE-3BBB body NOT embedded", "execution: planned outputs created by this Outfit Studio session"):
            self.assertIn(n, names)
        self.assertFalse(out_mod(fi).exists())
        s = ctl.commit_batch(bdir)
        self.assertEqual(s["counts"], {"COMMITTED": 1})
        om = out_mod(fi)
        nif = read_nif(om / BS / "ShapeData/Body Raider - CBBE - 3BBB AUTO/Body Raider - CBBE - 3BBB AUTO.nif")
        bones = {sh.name: {b for b, c in zip(*[sh.weight_matrix()[0], (sh.weight_matrix()[1] > 0.001).sum(0)]) if c}
                 for sh in nif.shapes}
        breast = {b for b in bones["RaiderTop"] if "Breast_0" in b}
        self.assertEqual(len(breast), 6)
        self.assertFalse(any("Butt_01" in b or "Thigh_01" in b for b in bones["RaiderTop"]))  # upper: BREAST only
        self.assertFalse(any("Breast" in b for b in bones["RaiderShorts"]))  # lower: BUTT/THIGH only
        for acc in ("RaiderHeels", "RaiderJewel"):  # rigid accessories untouched
            self.assertFalse(any(b.endswith("_skin") for b in bones[acc]), acc)
        self.assertEqual(len({b for b in bones["CBBE"] if "_0" in b and b.endswith("_skin")}), 12)
        man = json.loads((om / "manifest.json").read_text())
        rec = man["conversions"][-1]
        self.assertEqual(rec["conversion_mode"], "embedded_body_replacement")
        for k in ("source_body_shape", "source_body_classification", "target_body_sha256", "replacement_method", "validation"):
            self.assertIn(k, rec["body_replacement"])
        self.assertTrue(all(rec["body_replacement"]["validation"].values()))
        self.assertIn("acceptance", rec)
        self.assertIn("Body Raider - CBBE - 3BBB AUTO",
                      (om / BS / "SliderGroups" / f"CBBE 3BBB Auto Conversions - {BODY_PACKAGE}.xml").read_text())
        # rerun: recognised, never converted again
        b = by_name(ctl.analyze_package(BODY_PACKAGE, allow_body_replacement=True, write=False))
        self.assertEqual((b[RAIDER].action, b[RAIDER].status), ("KEEP", "ALREADY_CONVERTED"))
        self.assertEqual(tree_digest(fi.mods / BODY_PACKAGE), before, "source files must stay byte-for-byte unchanged")
        # in-game acceptance can be recorded without losing history
        from cbbe3bbb.conversion.manifest import Manifest

        m = Manifest.load(om)
        m.record_acceptance("Body Raider - CBBE - 3BBB AUTO", "PASS", "fit ok")
        m.save()
        rec2 = json.loads((om / "manifest.json").read_text())["conversions"][-1]
        self.assertEqual(rec2["acceptance"]["state"], "PASS")
        self.assertEqual(len(rec2["acceptance"]["history"]), 1)
        # target reference change → TARGET_REFERENCE_CHANGED (not ALREADY_CONVERTED)
        ref_nif = fi.mods / "CBBE" / BS / "ShapeData/CBBE 3BBB/CBBE 3BBB.nif"
        ref_nif.write_bytes(ref_nif.read_bytes() + b"\0")
        b = by_name(ctl.analyze_package(BODY_PACKAGE, allow_body_replacement=True, write=False))
        self.assertEqual((b[RAIDER].action, b[RAIDER].status), ("REVIEW", "TARGET_REFERENCE_CHANGED"))
        self.assertTrue(any("regeneration may be required" in r for r in b[RAIDER].reasons))

    def test_mode_b_failures_are_caught_and_isolated(self):
        cases = {
            "body_not_replaced": "body replacement: body weights identical to the CBBE-3BBB reference",
            "body_smeared": "body replacement: body weights identical to the CBBE-3BBB reference",
            "embed_ref": "body replacement: exactly one body in the output",
            "body_morphs_lost": "body replacement: body slider morphs identical to the source and resolvable",
        }
        for mode, check in cases.items():
            with self.subTest(mode=mode):
                ctl, fi, _ = setup()
                prev = ctl.analyze_package(BODY_PACKAGE, allow_body_replacement=True, write=False)
                bdir, s, rows = run(ctl, prev, [RAIDER, FULL], per_project={RAIDER: mode})
                self.assertEqual(rows[RAIDER]["status"], "FAILED", rows[RAIDER])
                self.assertIn(check, failed(rows[RAIDER]))
                self.assertEqual(rows[FULL]["status"], "VALIDATED", rows[FULL])  # the other project is unaffected
                c = ctl.commit_batch(bdir)
                self.assertEqual(c["counts"], {"FAILED": 1, "COMMITTED": 1})
                self.assertFalse((out_mod(fi) / BS / "SliderSets/Body Raider - CBBE - 3BBB AUTO.osp").exists())

    def test_wrong_target_body_is_rejected(self):
        ctl, fi, _ = setup()
        prev = ctl.analyze_package(BODY_PACKAGE, allow_body_replacement=True, write=False)
        pp = by_name(prev)[RAIDER]
        pp.plan["body_replacement"]["target_body_sha256"] = "0" * 64  # the plan names a different target body
        bdir, s, rows = run(ctl, prev, [RAIDER])
        self.assertEqual(rows[RAIDER]["status"], "FAILED")
        self.assertIn("body replacement: target reference unchanged since the plan (SHA-256)", failed(rows[RAIDER]))

    def test_mode_a_still_rejects_an_embedded_reference(self):
        ctl, fi, _ = setup()
        prev = ctl.analyze_package(PACKAGE, write=False)
        prev.select(["Pkg Top - CBBE"])
        bdir, s = ctl.convert_batch(prev, runner=FakeOutfitStudio("windows_interactive", per_project={"Pkg Top - CBBE": "embed_ref"}))
        row = s["projects"][0]
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("CBBE-3BBB body NOT embedded", row["failed_checks"])

    def test_cloth_sessions_and_recovery_with_mode_b(self):
        ctl, fi, _ = setup()
        prev = ctl.analyze_package(BODY_PACKAGE, allow_body_replacement=True, write=False)
        bdir, s, rows = run(ctl, prev, [RAIDER, FULL])
        b = json.loads((bdir / "batch.json").read_text())
        sess = b["sessions"]
        self.assertEqual(len(sess), 1)  # Raider (no cloth) first, Full (cloth) last
        order = [next(e["project"] for e in b["projects"] if e["id"] == pid) for pid in sess[0]["projects"]]
        self.assertEqual(order, [RAIDER, FULL])
        self.assertIn("cloth physics data (BSClothExtraData) unchanged",
                      [c["name"] for c in result(bdir, FULL)["validation"]["checks"]])
        # recovery: results lost → validate-batch rebuilds them, body fields survive in plan.json
        for p in (bdir / "projects").iterdir():
            (p / "result.json").unlink()
            plan = ConversionPlan.from_dict(json.loads((p / "plan.json").read_text()))
            self.assertEqual(plan.conversion_mode, "embedded_body_replacement")
            self.assertTrue(plan.body_replacement["target_body_sha256"])
        s = ctl.validate_batch(bdir)
        self.assertEqual(s["counts"], {"VALIDATED": 2})
        self.assertIn("body replacement: body weights identical to the CBBE-3BBB reference",
                      [c["name"] for c in result(bdir, RAIDER)["validation"]["checks"]])

    def test_manifest_merge_keeps_earlier_histories(self):
        ctl, fi, _ = setup(allowed=["Pkg Top - CBBE"])
        # an accepted Phase 2 single conversion (like Top V1) and a Phase 3 batch (like the Shorts)
        pr = next(p for p in ctl.analyze(write=False, workers=1).projects if p.project == "Pkg Top - CBBE")
        self.assertEqual(ctl.convert(ctl.plan_conversion(pr), runner=FakeOutfitStudio("windows_interactive")).status, "SUCCESS")
        prev = ctl.analyze_package(PACKAGE, write=False)
        prev.select(["Pkg Shorts - CBBE"])
        bdir, _ = ctl.convert_batch(prev, runner=FakeOutfitStudio("windows_interactive"))
        ctl.commit_batch(bdir)
        om = out_mod(fi)
        earlier = json.loads((om / "manifest.json").read_text())["conversions"]
        self.assertEqual(len(earlier), 2)
        prev = ctl.analyze_package(BODY_PACKAGE, allow_body_replacement=True, write=False)
        bdir, s, rows = run(ctl, prev, [RAIDER])
        ctl.commit_batch(bdir)
        now = json.loads((om / "manifest.json").read_text())["conversions"]
        self.assertEqual(now[:2], earlier, "earlier records (Top V1, Shorts) must stay unchanged")
        self.assertEqual(now[2]["conversion_mode"], "embedded_body_replacement")
        main_group = (om / BS / "SliderGroups/CBBE 3BBB Auto Conversions.xml").read_text()
        for g in ("Pkg Top - CBBE - 3BBB AUTO", "Pkg Shorts - CBBE - 3BBB AUTO", "Body Raider - CBBE - 3BBB AUTO"):
            self.assertIn(g, main_group)

    def test_cli_flag(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from cbbe3bbb.cli import main as cli_main
        from tests.test_app import _drop_handlers

        ctl, fi, _ = setup()
        prof = ctl.installation.profile
        ctl.save_profile(prof)

        def cli(*args):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                code = cli_main(["--home", str(ctl.paths.root), *args, "--profile", prof.name])
            _drop_handlers()
            return code, out.getvalue()

        code, out = cli("analyze-package", "--package", BODY_PACKAGE)
        self.assertEqual(code, 0)
        self.assertIn("CONVERT        0", out)
        code, out = cli("plan-batch", "--package", BODY_PACKAGE, "--allow-embedded-body-replacement", "--select", RAIDER,
                        "--details")
        self.assertEqual(code, 0, out)
        self.assertIn("EMBEDDED_BODY_REPLACEMENT", out)
        self.assertIn("BODY REPLACEMENT", out)
        self.assertFalse(out_mod(fi).exists())


class TopV1StillIdentical(unittest.TestCase):
    def test_top_v1_script_is_unchanged(self):
        d = json.loads((Path(__file__).parent / "data" / "topv1_accepted_plan.json").read_text(encoding="utf-8"))
        plan = ConversionPlan.from_dict(d)
        self.assertEqual(plan.conversion_mode, "outfit_only")
        self.assertEqual(build_script(plan).to_xml(), d["script_xml"])


if __name__ == "__main__":
    unittest.main()
