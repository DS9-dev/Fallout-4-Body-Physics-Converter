"""0.4.1 Phase 4.1: the controlled Direct-NIF production workflow (PLAN → CONVERT → VALIDATE → COMMIT → VERIFY).

Synthetic package only (tests/synth.py ``direct_package``); Outfit Studio is the fake in tests/fake_os.py, which
re-serializes the imported NIF like nifly does and can inject every export failure the validation must catch.
Numbers in the test names refer to the Phase 4.1 test list.
"""
import json
import shutil
import struct
import unittest
from pathlib import Path

import numpy as np

from tests._util import temp_home, tree_digest
from cbbe3bbb.app.controller import AppController, AppPaths
from cbbe3bbb.cli import main as cli_main
from cbbe3bbb.conversion.automation import AutomationStep, ScriptInvariantError, check_invariants, parse_script
from cbbe3bbb.conversion.direct_batch import (DirectBatchError, build_direct_script, check_direct_script,
                                              compare_with_saved_plan, direct_id)
from cbbe3bbb.conversion.manifest import Manifest
from cbbe3bbb.nif.reader import read_nif
from cbbe3bbb.profiles.store import Profile
from fake_os import FakeOutfitStudio, make_fake_runtime
from synth import BONE_POS, DIRECT_PACKAGE, PACKAGE, build_package_install, direct_package, outfit_patch, make_body, write_nif

OUT = "CBBE 3BBB Auto Conversions"


def setup():
    home = temp_home()
    fi = build_package_install(home / "inst", extra={DIRECT_PACKAGE: direct_package})
    rt = make_fake_runtime(home / "os", (5, 8, 2, 0))
    ctl = AppController(AppPaths(home / "tool"))
    p = Profile(name="t", mo2_dir=str(fi.mo2_dir))
    p.conversion.outfitstudio_dir = str(rt)
    ctl.detect(p)
    return ctl, fi, p


def convert(ctl, select, mode="windows_interactive", per=None):
    plan = ctl.plan_direct_batch(DIRECT_PACKAGE, select, write=False)
    assert plan.ok, (plan.problems, [e["problems"] for e in plan.refused])
    fake = FakeOutfitStudio(mode, per_project=per or {})
    bdir, s = ctl.convert_direct_batch(plan, runner=fake)
    return bdir, s, fake


def rows(s):
    return {Path(m["mesh"]).name: m for m in s["meshes"]}


def checks(bdir, name="result.json"):
    out = {}
    for f in sorted(Path(bdir).glob(f"meshes/*/{name}")):
        r = json.loads(f.read_text())
        out[Path(r["mesh"]).name] = {c["name"]: c for c in r.get("validation", {}).get("checks", [])}
    return out


def failed(bdir, mesh):
    return [n for n, c in checks(bdir)[mesh].items() if not c["ok"]]


def mesh_path(fi, name):
    return fi.mods / DIRECT_PACKAGE / "meshes" / "Direct" / name


def out_path(fi, name):
    return fi.mods / OUT / "meshes" / "Direct" / name


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, cls.prof = setup()

    def test_01_plan_is_read_only(self):
        before = tree_digest(self.fi.mods)
        plan = self.ctl.plan_direct_batch(DIRECT_PACKAGE, ["top_F.nif", "shorts_F.nif"])
        self.assertTrue(plan.ok, plan.problems)
        self.assertEqual(tree_digest(self.fi.mods), before)
        self.assertFalse((self.ctl.paths.root / "work").exists())
        self.assertTrue(all(p.parent.name == "direct_batches" for p in self.ctl.direct_plan_paths.values()))
        e = plan.meshes[0]
        for key in ("id", "game_rel", "items", "plugins", "source", "winner", "sex", "action", "confidence", "shapes",
                    "untouched_shapes", "embedded_body", "cloth_data", "name_collisions", "output_rel", "warnings"):
            self.assertIn(key, e)
        sd = e["shapes"][0]
        for key in ("vertices", "regions", "existing_bones", "planned_bones", "planned_radius"):
            self.assertIn(key, sd)
        self.assertEqual(len(e["source"]["sha256"]), 64)

    def test_02_only_high_convert_female_cbbe_selectable(self):
        plan = self.ctl.plan_direct_batch(DIRECT_PACKAGE, ["top_F.nif", "shorts_F.nif", "suit_F.nif"], write=False)
        self.assertTrue(plan.ok)
        for e in plan.meshes:
            self.assertEqual((e["action"], e["confidence"], e["sex"], e["status"]),
                             ("CONVERT", "HIGH", "FEMALE_CBBE_COMPATIBLE", "DIRECT_NIF_HIGH_CONFIDENCE"))
        self.assertFalse(plan.refused)

    def _refused(self, sel):
        plan = self.ctl.plan_direct_batch(DIRECT_PACKAGE, ["top_F.nif", sel], write=False)
        self.assertFalse(plan.ok)
        with self.assertRaises(DirectBatchError):
            self.ctl._direct_engine().create(plan)
        return plan

    def test_03_review_cannot_be_selected(self):
        for sel in ("piercing_F.nif", "far_F.nif", "cloth_F.nif", "clash_F.nif", "bootsbody_F.nif"):
            plan = self._refused(sel)
            self.assertEqual(len(plan.refused), 1, sel)
            self.assertTrue(any(p.startswith("NOT_CONVERT") for p in plan.refused[0]["problems"]), plan.refused[0]["problems"])

    def test_04_skip_cannot_be_selected(self):
        plan = self._refused("band_F.nif")
        self.assertIn("SKIP", plan.refused[0]["problems"][0])

    def test_05_male_cannot_be_selected(self):
        plan = self._refused("male_M.nif")  # a male-only model: not even a female game mesh record
        self.assertTrue(any("no female game mesh" in p for p in plan.problems))
        plan = self._refused("malebody_F.nif")
        self.assertTrue(any("NOT_FEMALE_CBBE" in p for p in plan.refused[0]["problems"]))
        plan = self._refused("unisex.nif")
        self.assertTrue(any("NOT_FEMALE_CBBE" in p for p in plan.refused[0]["problems"]))

    def test_selection_by_path_id_and_ambiguity(self):
        did = direct_id("meshes\\Direct\\top_F.nif")
        plan = self.ctl.plan_direct_batch(DIRECT_PACKAGE, [did, "meshes/direct/SHORTS_F.nif", "Direct\\suit_F.nif"], write=False)
        self.assertTrue(plan.ok, plan.problems)
        self.assertEqual(plan.meshes[0]["id"], did)
        from cbbe3bbb.conversion.direct_batch import resolve_selection

        rep = self.ctl.direct_report
        clone = type(rep.meshes[0])(**{**rep.meshes[0].__dict__, "mesh": "meshes\\Other\\top_F.nif"})
        rep.meshes.append(clone)
        try:
            got, problems = resolve_selection(rep, ["top_F.nif"])
            self.assertEqual(got, [])
            self.assertIn("ambiguous", problems[0])
            got, problems = resolve_selection(rep, ["meshes\\Direct\\top_F.nif"])
            self.assertEqual(len(got), 1)
        finally:
            rep.meshes.remove(clone)

    def test_26_unsafe_radius_remains_review(self):
        plan = self._refused("far_F.nif")
        self.assertIn("UNSAFE_RADIUS", plan.refused[0]["problems"][0])

    def test_full_copy_outside_planned_regions_is_review(self):
        # like the real EasyGirl MiniDress: a full Copy Bone Weights would add weak thigh weights at the hem while only
        # breast + butt are planned. Here: the shorts, analyzed with a reference whose THIGH region is not planned.
        from cbbe3bbb.conversion import direct_nif

        kb = self.ctl._knowledge_base()
        saved = dict(kb.reference.physics_regions)
        kb.reference.physics_regions.pop("THIGH")
        try:
            dm = direct_nif.DirectMesh(mesh="meshes\\Direct\\shorts_F.nif", items=[])
            direct_nif.analyze_mesh(dm, mesh_path(self.fi, "shorts_F.nif"), kb,
                                    direct_nif._reference_shape(self.ctl.installation), self.ctl._direct_engine()._settings(),
                                    False, {})
        finally:
            kb.reference.physics_regions.clear()
            kb.reference.physics_regions.update(saved)
        self.assertEqual((dm.action, dm.status, dm.confidence), ("REVIEW", "FULL_COPY_OUTSIDE_REGIONS", "MEDIUM"))
        self.assertIn("LLeg_Thigh_01_F_skin", dm.reasons[0])
        self.assertTrue(all(s.decision == "NONE" for s in dm.shapes))

    def test_cli_plan_requires_opt_in_and_selection(self):
        home = self.ctl.paths.root
        argv = ["--home", str(home), "plan-direct-batch", "--mo2", str(self.fi.mo2_dir), "--package", DIRECT_PACKAGE]
        self.assertEqual(cli_main(argv + ["--select", "top_F.nif"]), 2)  # no --allow-direct-nif
        self.assertEqual(cli_main(argv + ["--allow-direct-nif"]), 2)  # no --select
        self.assertEqual(cli_main(argv + ["--allow-direct-nif", "--select", "band_F.nif"]), 3)
        self.assertEqual(cli_main(argv + ["--allow-direct-nif", "--select", "top_F.nif"]), 0)


class ConvertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, cls.prof = setup()
        cls.before = tree_digest(cls.fi.mods / DIRECT_PACKAGE)
        cls.seen = []

        base = FakeOutfitStudio("windows_interactive")

        def runner(rf, exe, name, t, cancel=None, interactive=False):  # test 8: what exists when OS starts
            import xml.etree.ElementTree as ET

            project = Path(ET.parse(Path(rf) / "Config.xml").getroot().findtext("ProjectPath"))
            sc = parse_script((project / "Automations" / f"{name}.xml").read_text())
            for st in sc.steps:
                if st.type == "ImportFile":
                    p = Path(st.params["FilePath"])
                    cls.seen.append((str(p), p.is_file(), p.read_bytes() if p.is_file() else b""))
            return base(rf, exe, name, t, cancel, interactive)

        cls.fake = base
        plan = cls.ctl.plan_direct_batch(DIRECT_PACKAGE, ["top_F.nif", "shorts_F.nif", "suit_F.nif", "reach_F.nif",
                                                          "Direct\\TOP_F.nif"], write=False)
        cls.plan = plan
        cls.bdir, cls.s = cls.ctl.convert_direct_batch(plan, runner=runner)
        cls.rows = rows(cls.s)
        cls.checks = checks(cls.bdir)

    def test_all_validated(self):
        self.assertEqual({k: v["status"] for k, v in self.rows.items()},
                         {"top_F.nif": "VALIDATED", "shorts_F.nif": "VALIDATED", "suit_F.nif": "VALIDATED", "reach_F.nif": "VALIDATED"})

    def test_06_duplicate_items_one_nif_converted_once(self):
        self.assertEqual(len(self.plan.meshes), 4)
        top = self.rows["top_F.nif"]
        self.assertEqual(top["items"], ["Direct Top Black", "Direct Top Red", "Direct Top White"])
        self.assertEqual(sum(1 for s, _, _ in self.fake.direct_exports if s.endswith("top_F.nif")), 1)

    def test_08_source_copied_to_workspace_before_outfit_studio(self):
        self.assertEqual(len(self.seen), 4)
        for path, existed, data in self.seen:
            self.assertTrue(existed)
            self.assertTrue(Path(path).is_relative_to(self.bdir.resolve()), path)
            self.assertEqual(data, mesh_path(self.fi, Path(path).name).read_bytes())
            self.assertIn("source", Path(path).parts)

    def test_09_source_mod_unchanged_and_output_not_written(self):
        self.assertEqual(tree_digest(self.fi.mods / DIRECT_PACKAGE), self.before)
        self.assertFalse((self.fi.mods / OUT / "meshes").exists())

    def test_10_11_absolute_export_path_and_reference_excluded(self):
        sc = parse_script(next(self.bdir.glob("cbbe3bbb_direct_*.xml")).read_text())
        exports = [s for s in sc.steps if s.type == "ExportFile"]
        self.assertEqual(len(exports), 4)
        for st in exports:
            self.assertTrue(Path(st.params["FilePath"]).is_absolute())
            self.assertTrue(Path(st.params["FilePath"]).is_relative_to(self.bdir.resolve()))
            self.assertEqual(st.params["WithRef"], "false")
        self.assertFalse(any(s.type in ("SaveProject", "AddProject") for s in sc.steps))
        self.assertTrue(all(not w for _, _, w in self.fake.direct_exports))
        for name in self.rows:
            gen = read_nif(next(self.bdir.glob(f"meshes/*/generated/{name}")))
            self.assertNotIn("CBBE-3BBB", [s.name for s in gen.shapes])
        # the script safety check refuses a relative path, WithRef true, a SaveProject and a BoneList copy
        e = self.plan.meshes[0]
        ref = self.plan.reference
        good = build_direct_script("x", [{"entry": e, "source": self.bdir / "a.nif", "output": self.bdir / "b.nif"}], ref,
                                   self.plan.settings)
        check_direct_script(good, self.bdir)
        for mutate in (lambda s: s.steps[-1].params.update(FilePath="generated/b.nif"),
                       lambda s: s.steps[-1].params.update(FilePath="/elsewhere/b.nif"),
                       lambda s: s.steps[-1].params.update(WithRef=True),
                       lambda s: s.steps[-1].params.pop("WithRef"),
                       lambda s: s.steps.insert(-1, AutomationStep("SaveProject", {"DisplayName": "x"})),
                       lambda s: s.steps[3].params.update(BoneList=["LBreast_01_skin"]),
                       lambda s: s.steps[2].params.update(FilePath=str(mesh_path(self.fi, "top_F.nif")))):
            sc2 = parse_script(good.to_xml())
            mutate(sc2)
            with self.assertRaises(ScriptInvariantError):
                check_direct_script(sc2, self.bdir)

    def test_20_breast_weights_validate(self):
        c = self.checks["top_F.nif"]
        for n in ("TopShape: breast 3BBB bones present", "TopShape: expected 3BBB bones carry weight",
                  "TopShape: no physics bones outside the planned regions (forbidden region)",
                  "TopShape: matches the Copy Bone Weights prediction", "TopShape: cross-side / quality anomaly checks",
                  "structure: TopShape: segment / tail data identical (BSSubIndexTriShape)",
                  "structure: TopShape: material / shader and textures identical", "structure: TopShape: alpha property identical",
                  "structure: extra data blocks preserved"):
            self.assertTrue(c[n]["ok"], (n, c[n]))

    def test_21_22_butt_and_thigh_weights_validate(self):
        c = self.checks["shorts_F.nif"]
        self.assertTrue(c["ShortsShape: butt 3BBB bones present"]["ok"])
        self.assertTrue(c["ShortsShape: thigh 3BBB bones present"]["ok"])
        self.assertIn("LLeg_Thigh_01_F_skin", c["ShortsShape: thigh 3BBB bones present"]["detail"])

    def test_25_adaptive_radius_reused(self):
        e = next(x for x in self.plan.meshes if x["file_name"] == "reach_F.nif")
        r = e["shapes"][0]["planned_radius"]
        self.assertGreater(r, 5.0)
        self.assertLessEqual(r, 10.0)
        sc = parse_script(next(self.bdir.glob("cbbe3bbb_direct_*.xml")).read_text())
        radii = [float(s.params["ProximityRadius"]) for s in sc.steps if s.type == "CopyBoneWeights" and "ReachShorts" in s.target_meshes]
        self.assertEqual(radii, [r])
        c = self.checks["reach_F.nif"]
        self.assertTrue(c["ReachShorts: weights normalized, no vertex without weight"]["ok"])

    def test_27_untouched_normalization_only_rewrite_passes(self):
        c = self.checks["suit_F.nif"]["SuitBand: untouched shape — weights unchanged (normalization-aware)"]
        self.assertTrue(c["ok"], c)
        self.assertIn("raw 0.0098", c["detail"])
        sc = parse_script(next(self.bdir.glob("cbbe3bbb_direct_*.xml")).read_text())
        targets = [t for s in sc.steps if s.type == "CopyBoneWeights" for t in s.target_meshes]
        self.assertNotIn("SuitBand", targets)

    def test_29_generated_nif_independently_reparsed(self):
        for name, c in self.checks.items():
            self.assertTrue(c["generated NIF independently re-parsed"]["ok"], name)
            self.assertTrue(c["Direct-NIF analyzer classifies the generated mesh as 3BBB KEEP"]["ok"], name)

    def test_41_bodyslide_script_rules_unchanged(self):
        # a BodySlide conversion script still may not contain the Direct-NIF steps
        sc = parse_script(next(self.bdir.glob("cbbe3bbb_direct_*.xml")).read_text())
        with self.assertRaises(ScriptInvariantError):
            check_invariants(sc)


class FailureTests(unittest.TestCase):
    """Every injected export defect makes the mesh FAILED (never VALIDATED)."""

    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, cls.prof = setup()

    def _one(self, mode, mesh="top_F.nif", status="FAILED"):
        bdir, s, fake = convert(self.ctl, [mesh], per={mesh: mode})
        self.assertEqual(rows(s)[mesh]["status"], status, (mode, rows(s)[mesh]))
        return failed(bdir, mesh)

    def test_12_reference_body_leak_fails(self):
        f = self._one("embed_ref")
        self.assertIn("no CBBE-3BBB reference body in the output (WithRef=false)", f)

    def test_13_shape_rename_fails(self):
        self.assertIn("structure: shape set and order identical (no missing, added, renamed or reference shape)",
                      self._one("rename_shape"))

    def test_14_missing_shape_fails(self):
        f = self._one("drop_shape", "suit_F.nif")
        self.assertIn("structure: shape set and order identical (no missing, added, renamed or reference shape)", f)
        self.assertIn("SuitBand: shape present", f)

    def test_15_added_shape_fails(self):
        self.assertIn("structure: shape set and order identical (no missing, added, renamed or reference shape)",
                      self._one("add_shape"))

    def test_16_geometry_change_fails(self):
        self.assertIn("structure: TopShape: vertex positions identical", self._one("move_vertex"))

    def test_17_material_loss_fails(self):
        self.assertIn("structure: TopShape: material / shader and textures identical", self._one("lose_material"))

    def test_18_segment_loss_fails(self):
        self.assertIn("structure: TopShape: segment / tail data identical (BSSubIndexTriShape)", self._one("lose_segment"))
        self.assertIn("structure: extra data blocks preserved", self._one("lose_extra_data"))

    def test_19_unexpected_cloth_data_fails(self):
        self.assertIn("cloth physics data: none in source or output", self._one("add_cloth"))

    def test_23_forbidden_region_fails(self):
        self.assertIn("TopShape: no physics bones outside the planned regions (forbidden region)", self._one("forbidden_region"))

    def test_24_zero_weight_vertex_fails(self):
        f = self._one("zero_weight")
        self.assertIn("TopShape: weights normalized, no vertex without weight", f)

    def test_28_meaningful_untouched_change_fails(self):
        self.assertIn("SuitBand: untouched shape — weights unchanged (normalization-aware)",
                      self._one("untouched_changed", "suit_F.nif"))

    def test_30_malformed_generated_nif_fails(self):
        self.assertIn("generated NIF independently re-parsed", self._one("malformed"))

    def test_not_executed_fails(self):
        f = self._one("no_output")
        self.assertIn("execution: generated NIF written by this Outfit Studio session", f)

    def test_failure_isolation(self):
        bdir, s, _ = convert(self.ctl, ["top_F.nif", "shorts_F.nif"], per={"shorts_F.nif": "zero_weight"})
        self.assertEqual({k: v["status"] for k, v in rows(s).items()}, {"top_F.nif": "VALIDATED", "shorts_F.nif": "FAILED"})


class WinnerAndCommitTests(unittest.TestCase):
    def test_07_32_source_winner_change_blocks_conversion_and_commit(self):
        ctl, fi, prof = setup()
        plan = ctl.plan_direct_batch(DIRECT_PACKAGE, ["top_F.nif", "shorts_F.nif"], write=False)
        saved = json.loads(json.dumps(plan.to_dict()))
        eng = ctl._direct_engine(FakeOutfitStudio("windows_interactive"))
        bdir = eng.create(plan)
        # another mod now wins meshes\Direct\top_F.nif
        other = fi.mods / "Late Override" / "meshes" / "Direct" / "top_F.nif"
        other.parent.mkdir(parents=True)
        shutil.copy2(mesh_path(fi, "top_F.nif"), other)
        ml = fi.mo2_dir / "profiles" / "Default" / "modlist.txt"
        ml.write_text(ml.read_text().replace("+CBBE 3BBB Auto Conversions", "+CBBE 3BBB Auto Conversions\n+Late Override"))
        ctl.detect(prof)
        fake = FakeOutfitStudio("windows_interactive")
        s = ctl._direct_engine(fake).run(bdir)
        r = rows(s)
        self.assertEqual(r["top_F.nif"]["status"], "REVIEW")
        self.assertIn("SOURCE_WINNER_CHANGED", r["top_F.nif"]["reasons"][0])
        self.assertEqual(r["shorts_F.nif"]["status"], "VALIDATED")
        self.assertEqual([Path(x).name for x, _, _ in fake.direct_exports], ["shorts_F.nif"])  # never converted
        # a fresh plan against the inspected plan file reports the change
        ctl2_plan = ctl.plan_direct_batch(DIRECT_PACKAGE, ["top_F.nif", "shorts_F.nif"], write=False)
        self.assertFalse(ctl2_plan.ok)  # the package copy no longer wins: NOT_PACKAGE_WINNER / MO2_CONFLICT
        # 32: REVIEW is never committed
        s = ctl.commit_direct_batch(bdir.name)
        self.assertEqual(rows(s)["top_F.nif"]["status"], "REVIEW")
        self.assertFalse(out_path(fi, "top_F.nif").exists())
        self.assertTrue(out_path(fi, "shorts_F.nif").exists())
        # the plan comparison helper
        plan3 = type(plan).from_dict(json.loads(json.dumps(saved)))
        plan3.meshes[0] = dict(plan3.meshes[0], source=dict(plan3.meshes[0]["source"], sha256="0" * 64))
        self.assertTrue(any(d.startswith("SOURCE_CHANGED") for d in compare_with_saved_plan(plan3, saved)))

    def test_source_changed_before_conversion_is_review(self):
        ctl, fi, prof = setup()
        plan = ctl.plan_direct_batch(DIRECT_PACKAGE, ["top_F.nif"], write=False)
        eng = ctl._direct_engine(FakeOutfitStudio("windows_interactive"))
        bdir = eng.create(plan)
        p = mesh_path(fi, "top_F.nif")
        p.write_bytes(p.read_bytes() + b"\0")
        s = eng.run(bdir)
        self.assertEqual(rows(s)["top_F.nif"]["status"], "REVIEW")
        self.assertIn("SOURCE_CHANGED", rows(s)["top_F.nif"]["reasons"][0])

    def test_31_33_34_35_commit(self):
        ctl, fi, prof = setup()
        # an output mod that already holds a BodySlide conversion record (must be preserved)
        (fi.mods / OUT).mkdir()
        man = Manifest.load(fi.mods / OUT)
        man.records.append({"id": "old", "status": "SUCCESS", "source": {"project": "Old - CBBE"}, "generated_files": []})
        man.save()
        old_records = json.loads((fi.mods / OUT / "manifest.json").read_text())["conversions"]
        before_pkg = tree_digest(fi.mods / DIRECT_PACKAGE)
        bdir, s, _ = convert(ctl, ["top_F.nif", "shorts_F.nif", "suit_F.nif", "reach_F.nif"],
                             per={"shorts_F.nif": "zero_weight"})
        # 34: an unexpected file at the target is never overwritten
        manual = out_path(fi, "suit_F.nif")
        manual.parent.mkdir(parents=True, exist_ok=True)
        manual.write_bytes(b"user file")
        # 33: the source changes after the conversion
        rp = mesh_path(fi, "reach_F.nif")
        rp.write_bytes(rp.read_bytes() + b"\0")
        s = ctl.commit_direct_batch(bdir.name)
        r = rows(s)
        self.assertEqual(r["top_F.nif"]["status"], "COMMITTED")
        self.assertEqual(r["shorts_F.nif"]["status"], "FAILED")  # 31: only VALIDATED is written
        self.assertFalse(out_path(fi, "shorts_F.nif").exists())
        self.assertEqual(r["suit_F.nif"]["status"], "VALIDATED")
        self.assertIn("manual user file", r["suit_F.nif"]["commit_error"][0])
        self.assertEqual(manual.read_bytes(), b"user file")
        self.assertEqual(r["reach_F.nif"]["status"], "VALIDATED")
        self.assertTrue(any("SOURCE_CHANGED" in x for x in r["reach_F.nif"]["commit_error"]))
        self.assertFalse(out_path(fi, "reach_F.nif").exists())
        # the written file is exactly the validated one, at the same game path
        gen = next(bdir.glob("meshes/*/generated/top_F.nif"))
        self.assertEqual(out_path(fi, "top_F.nif").read_bytes(), gen.read_bytes())
        # 35: Direct-NIF manifest record; the BodySlide records are untouched
        m = json.loads((fi.mods / OUT / "manifest.json").read_text())
        self.assertEqual(m["conversions"], old_records)
        self.assertEqual(len(m["direct_conversions"]), 1)
        rec = m["direct_conversions"][0]
        self.assertEqual(rec["kind"], "DIRECT_NIF")
        self.assertEqual(rec["game_key"], "direct/top_f.nif")
        self.assertEqual(rec["generated"]["path"], "meshes/Direct/top_F.nif")
        self.assertEqual(rec["source"]["sha256"], before_pkg_sha(fi, "top_F.nif"))
        self.assertEqual(rec["shapes"][0]["radius"], 5.0)
        self.assertTrue(rec["reference"]["files"])
        self.assertEqual(rec["validation"]["status"], "VALIDATED")
        self.assertIn("Direct Top Red", rec["item_names"])
        # no leftovers of the atomic write
        self.assertFalse(list((fi.mods / OUT).rglob("*.cbbe3bbb-tmp")))
        # the source package is untouched except for the file this test changed on purpose
        after = tree_digest(fi.mods / DIRECT_PACKAGE)
        self.assertEqual({k for k in after if after[k] != before_pkg.get(k)}, {str(Path("meshes/Direct/reach_F.nif"))})


def before_pkg_sha(fi, name):
    import hashlib

    return hashlib.sha256(mesh_path(fi, name).read_bytes()).hexdigest()


class IdempotencyAndVerifyTests(unittest.TestCase):
    def committed(self):
        ctl, fi, prof = setup()
        bdir, s, _ = convert(ctl, ["top_F.nif", "shorts_F.nif"])
        ctl.commit_direct_batch(bdir.name)
        ctl.detect(prof)  # the output mod folder exists now: MO2 serves it
        return ctl, fi, prof, bdir

    def analyze(self, ctl):
        ctl.analyze_package(DIRECT_PACKAGE, allow_direct_nif=True, write=False)
        return {Path(m.mesh.replace("\\", "/")).name.lower(): m for m in ctl.direct_report.meshes}

    def test_36_39_keep_after_commit_and_verify_ok(self):
        ctl, fi, prof, bdir = self.committed()
        m = self.analyze(ctl)
        self.assertEqual((m["top_f.nif"].action, m["top_f.nif"].status), ("KEEP", "ALREADY_CONVERTED_DIRECT_NIF"))
        self.assertEqual((m["shorts_f.nif"].action, m["shorts_f.nif"].status), ("KEEP", "ALREADY_CONVERTED_DIRECT_NIF"))
        self.assertEqual(m["suit_f.nif"].action, "CONVERT")
        # a new plan refuses the already converted mesh
        plan = ctl.plan_direct_batch(DIRECT_PACKAGE, ["top_F.nif"], write=False)
        self.assertFalse(plan.ok)
        res = {Path(r.mesh.replace("\\", "/")).name: r for r in ctl.verify_direct_build()}
        self.assertEqual({k: r.status for k, r in res.items()}, {"top_F.nif": "DIRECT_OK", "shorts_F.nif": "DIRECT_OK"})
        top = res["top_F.nif"]
        self.assertEqual(top.winner["provider"], OUT)
        self.assertEqual(top.winner_sha256, top.generated_sha256)
        self.assertEqual({i["armor_name"] for i in top.game_items}, {"Direct Top Black", "Direct Top Red", "Direct Top White"})
        self.assertTrue(top.shapes[0]["actual"])
        self.assertEqual(ctl.verify_direct_build("suit_F.nif")[0].status, "NOT_COMMITTED")
        # CLI: verify and status are read-only and succeed
        argv = ["--home", str(ctl.paths.root), "verify-direct-build", "--mo2", str(fi.mo2_dir)]
        self.assertEqual(cli_main(argv), 0)
        self.assertEqual(cli_main(["--home", str(ctl.paths.root), "direct-batch-status", "--mo2", str(fi.mo2_dir),
                                   "--batch", bdir.name]), 0)

    def test_37_changed_source_invalidates_keep(self):
        ctl, fi, prof, bdir = self.committed()
        p = mesh_path(fi, "top_F.nif")
        p.write_bytes(p.read_bytes() + b"\0")
        m = self.analyze(ctl)
        self.assertEqual((m["top_f.nif"].action, m["top_f.nif"].status), ("REVIEW", "SOURCE_CHANGED"))
        self.assertEqual(ctl.verify_direct_build("top_F.nif")[0].status, "SOURCE_CHANGED")

    def test_38_changed_target_reference_reviews_keep(self):
        ctl, fi, prof, bdir = self.committed()
        ref = fi.mods / "CBBE" / "Tools" / "BodySlide" / "ShapeData" / "CBBE 3BBB" / "CBBE 3BBB.nif"
        data = bytearray(ref.read_bytes())
        data[-20] ^= 0x01
        ref.write_bytes(bytes(data))
        m = self.analyze(ctl)
        self.assertEqual((m["top_f.nif"].action, m["top_f.nif"].status), ("REVIEW", "TARGET_REFERENCE_CHANGED"))

    def test_40_wrong_winner_and_changed_output_fail_verification(self):
        ctl, fi, prof, bdir = self.committed()
        out = out_path(fi, "shorts_F.nif")
        out.write_bytes(out.read_bytes() + b"\0")
        other = fi.mods / "Late Override" / "meshes" / "Direct" / "top_F.nif"
        other.parent.mkdir(parents=True)
        shutil.copy2(mesh_path(fi, "top_F.nif"), other)
        ml = fi.mo2_dir / "profiles" / "Default" / "modlist.txt"
        ml.write_text(ml.read_text().replace("+CBBE 3BBB Auto Conversions", "+Late Override\n+CBBE 3BBB Auto Conversions"))
        ctl.detect(prof)
        res = {Path(r.mesh.replace("\\", "/")).name: r.status for r in ctl.verify_direct_build()}
        self.assertEqual(res, {"top_F.nif": "OUTPUT_NOT_WINNER", "shorts_F.nif": "OUTPUT_MISMATCH"})
        m = self.analyze(ctl)
        self.assertEqual(m["top_f.nif"].status, "OUTPUT_NOT_WINNER")
        self.assertEqual(m["shorts_f.nif"].status, "OUTPUT_CHANGED")
        argv = ["--home", str(ctl.paths.root), "verify-direct-build", "--mo2", str(fi.mo2_dir)]
        self.assertEqual(cli_main(argv), 1)


class BodySlideUnchangedTests(unittest.TestCase):
    def test_41_42_bodyslide_batch_and_direct_commit_coexist(self):
        ctl, fi, prof = setup()
        prev = ctl.analyze_package(PACKAGE, write=False)
        self.assertEqual(prev.source_mode, "BODYSLIDE_PROJECT")
        prev.select(["Pkg Top - CBBE"])
        bdir, s = ctl.convert_batch(prev, runner=FakeOutfitStudio("windows_interactive"))
        self.assertEqual({r["project"]: r["status"] for r in s["projects"]}, {"Pkg Top - CBBE": "VALIDATED"})
        ctl.commit_batch(bdir.name)
        man_bs = json.loads((fi.mods / OUT / "manifest.json").read_text())["conversions"]
        dbdir, ds, _ = convert(ctl, ["top_F.nif"])
        ctl.commit_direct_batch(dbdir.name)
        m = json.loads((fi.mods / OUT / "manifest.json").read_text())
        self.assertEqual(m["conversions"], man_bs)
        self.assertEqual(len(m["direct_conversions"]), 1)
        ctl.detect(prof)
        prev = ctl.analyze_package(PACKAGE, write=False)
        top = next(p for p in prev.projects if p.project == "Pkg Top - CBBE")
        self.assertEqual((top.action, top.status), ("KEEP", "ALREADY_CONVERTED"))
        # the BodySlide commit's verify-build view still sees only its own record
        self.assertEqual([r.get("target", {}).get("project") for r in Manifest.load(fi.mods / OUT).records],
                         ["Pkg Top - CBBE - 3BBB AUTO"])


if __name__ == "__main__":
    unittest.main()
