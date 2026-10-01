"""0.4.0 Phase 4.0: DIRECT_NIF read-only analysis (synthetic package, no real assets)."""
import json
import shutil
import unittest
from pathlib import Path

from tests._util import temp_home, tree_digest
from cbbe3bbb.app.controller import AppController, AppPaths
from cbbe3bbb.cli import main as cli_main
from cbbe3bbb.conversion.direct_nif import (SOURCE_BODYSLIDE_PROJECT, SOURCE_DIRECT_NIF, SOURCE_MIXED, SOURCE_NONE,
                                            detect_source_mode, plugin_mesh_refs)
from cbbe3bbb.conversion.gamedata import read_plugin_armors
from cbbe3bbb.profiles.store import Profile
from fake_os import make_fake_runtime
from synth import DIRECT_PACKAGE, PACKAGE, build_package_install, direct_package, write_plugin


def setup(active=None):
    home = temp_home()
    fi = build_package_install(home / "inst", extra={DIRECT_PACKAGE: direct_package})
    if active is not None:
        (fi.mo2_dir / "profiles" / "Default" / "plugins.txt").write_text(
            "# plugins\n" + "".join(f"*{p}\n" for p in active), encoding="utf-8")
    rt = make_fake_runtime(home / "os", (5, 8, 2, 0))
    ctl = AppController(AppPaths(home / "tool"))
    p = Profile(name="t", mo2_dir=str(fi.mo2_dir))
    p.conversion.outfitstudio_dir = str(rt)
    ctl.detect(p)
    return ctl, fi


def meshes(ctl):
    return {Path(m.mesh.replace("\\", "/")).name.lower(): m for m in ctl.direct_report.meshes}


class DirectNifTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi = setup(active=["Direct.esp"])
        cls.before = tree_digest(cls.fi.mods / DIRECT_PACKAGE)
        cls.prev = cls.ctl.analyze_package(DIRECT_PACKAGE, allow_direct_nif=True, write=True)
        cls.rep = cls.ctl.direct_report
        cls.m = meshes(cls.ctl)
        cls.prev_on = None

    def test_01_source_mode_direct_nif(self):
        self.assertEqual(self.prev.source_mode, SOURCE_DIRECT_NIF)
        self.assertEqual(self.rep.source_mode, SOURCE_DIRECT_NIF)
        self.assertEqual(self.rep.bodyslide_projects, 0)
        self.assertEqual(self.prev.projects, [])
        self.assertEqual(self.rep.plugins[0]["plugin"], "Direct.esp")
        self.assertTrue(self.rep.plugins[0]["enabled"])
        self.assertEqual(self.prev.direct_nif["counts"], self.rep.counts)

    def test_02_03_no_projects_without_opt_in_is_the_old_safe_error(self):
        ctl, fi = setup()
        with self.assertRaises(RuntimeError) as cm:
            ctl.analyze_package(DIRECT_PACKAGE, write=False)
        self.assertIn("provides no BodySlide project", str(cm.exception))
        self.assertIn("--allow-direct-nif", str(cm.exception))
        # a BodySlide package without the opt-in: unchanged behaviour, no Direct-NIF analysis
        prev = ctl.analyze_package(PACKAGE, write=False)
        self.assertEqual(prev.source_mode, SOURCE_BODYSLIDE_PROJECT)
        self.assertEqual(prev.direct_nif, {})
        self.assertIsNone(ctl.direct_report)

    def test_04_05_female_vs_male_slots(self):
        self.assertNotIn("male_m.nif", self.m)  # male-only model: skipped, never analyzed as CBBE
        self.assertGreaterEqual(self.rep.male_meshes_skipped, 1)
        self.assertEqual((self.m["unisex.nif"].sex, self.m["unisex.nif"].action), ("AMBIGUOUS", "REVIEW"))
        mb = self.m["malebody_f.nif"]
        self.assertEqual((mb.sex, mb.action, mb.status), ("MALE", "UNSUPPORTED", "MALE_MESH"))

    def test_06_07_duplicates_deduplicate_and_keep_every_item(self):
        top = self.m["top_f.nif"]
        self.assertEqual(sum(1 for m in self.rep.meshes if m.mesh.lower().endswith("top_f.nif")), 1)
        self.assertEqual(top.item_names, ["Direct Top Black", "Direct Top Red", "Direct Top White"])

    def test_08_missing_mesh_is_error_not_crash(self):
        self.assertEqual((self.m["missing_f.nif"].action, self.m["missing_f.nif"].status), ("ERROR", "MISSING_GAME_MESH"))

    def test_09_15_embedded_partial_body(self):
        b = self.m["bootsbody_f.nif"]
        self.assertEqual(b.embedded_body, "BODY_CBBE_COMPATIBLE")
        self.assertTrue(b.body_slot)
        self.assertEqual((b.action, b.status), ("REVIEW", "EMBEDDED_BODY"))  # replacement not opted in
        ctl, fi = setup()
        ctl.analyze_package(DIRECT_PACKAGE, allow_direct_nif=True, allow_body_replacement=True, write=False)
        b2 = meshes(ctl)["bootsbody_f.nif"]
        self.assertEqual((b2.action, b2.status), ("CONVERT", "EMBEDDED_BODY_REPLACEMENT"))
        body = next(s for s in b2.shapes if s.decision == "BODY_REPLACE")
        self.assertLess(b2.body_replacement["vertices"], 5808)  # the author's partial body, never the full reference
        self.assertEqual(b2.body_replacement["vertices"], body.vertices)
        self.assertIn("never inserted", b2.body_replacement["method_details"])
        self.assertTrue(all(s.decision == "NONE" for s in b2.shapes if s.name == "BootShape"))

    def test_10_already_3bbb_keep(self):
        self.assertEqual((self.m["keep_f.nif"].action, self.m["keep_f.nif"].status), ("KEEP", "ALREADY_3BBB"))

    def test_11_no_physics_region_skip(self):
        self.assertEqual((self.m["band_f.nif"].action, self.m["band_f.nif"].status), ("SKIP", "NO_PHYSICS_REGION"))

    def test_12_13_high_breast_and_butt_thigh_convert(self):
        top, shorts = self.m["top_f.nif"], self.m["shorts_f.nif"]
        self.assertEqual((top.action, top.status, top.confidence), ("CONVERT", "DIRECT_NIF_HIGH_CONFIDENCE", "HIGH"))
        self.assertEqual(top.sex, "FEMALE_CBBE_COMPATIBLE")
        s = top.shapes[0]
        self.assertEqual((s.decision, s.planned_regions, s.planned_radius), ("CONVERT", ["BREAST"], 5.0))
        self.assertTrue(all("Breast" in b for b in s.planned_bones))
        self.assertEqual(shorts.action, "CONVERT")
        self.assertEqual(set(shorts.shapes[0].planned_regions), {"BUTT", "THIGH"})
        self.assertTrue(top.planned_target.replace("\\", "/").lower().endswith("cbbe 3bbb auto conversions/meshes/direct/top_f.nif"))

    def test_14_rigid_accessory_over_region_review(self):
        p = self.m["piercing_f.nif"]
        self.assertEqual((p.action, p.status), ("REVIEW", "RIGID_REGION_OVERLAP"))

    def test_16_17_adaptive_radius_reused_and_unsafe_radius_review(self):
        r = self.m["reach_f.nif"]
        self.assertEqual(r.action, "CONVERT")
        self.assertEqual(r.shapes[0].out_of_reach_vertices, [5, 6])
        self.assertGreater(r.shapes[0].planned_radius, 5.0)
        self.assertLessEqual(r.shapes[0].planned_radius, 10.0)
        f = self.m["far_f.nif"]
        self.assertEqual((f.action, f.status), ("REVIEW", "UNSAFE_RADIUS"))
        self.assertTrue(all(s.planned_radius is None for s in f.shapes))

    def test_18_mo2_losing_source_not_selected(self):
        ctl, fi = setup()
        ov = fi.mo2_dir / "overwrite" / "meshes" / "Direct" / "shorts_F.nif"
        ov.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fi.mods / DIRECT_PACKAGE / "meshes" / "Direct" / "shorts_F.nif", ov)
        ctl.detect(ctl.installation.profile)
        ctl.analyze_package(DIRECT_PACKAGE, allow_direct_nif=True, write=False)
        s = meshes(ctl)["shorts_f.nif"]
        self.assertEqual((s.action, s.status), ("REVIEW", "MO2_CONFLICT"))
        self.assertEqual(s.winner["provider"], "<overwrite>")
        self.assertEqual([c["provider"] for c in s.competitors], ["<overwrite>", DIRECT_PACKAGE])
        v = meshes(ctl)["vanilla_f.nif"]
        self.assertEqual((v.action, v.status), ("SKIP", "NOT_PACKAGE_MESH"))

    def test_19_source_unchanged_and_reports_only_in_tool_folder(self):
        self.assertEqual(tree_digest(self.fi.mods / DIRECT_PACKAGE), self.before)
        self.assertFalse((self.fi.mods / "CBBE 3BBB Auto Conversions").exists())
        for k in ("direct_json", "direct_csv", "direct_txt", "direct_html"):
            p = self.ctl.preview_paths[k]
            self.assertTrue(Path(p).is_file())
            self.assertTrue(str(p).startswith(str(self.ctl.paths.reports)))
        d = json.loads(Path(self.ctl.preview_paths["direct_json"]).read_text())
        self.assertEqual(d["source_mode"], SOURCE_DIRECT_NIF)
        self.assertIn("top_F.nif", Path(self.ctl.preview_paths["direct_csv"]).read_text())

    def test_cloth_and_structure_blockers(self):
        self.assertEqual((self.m["cloth_f.nif"].action, self.m["cloth_f.nif"].status), ("REVIEW", "CLOTH_DATA_UNSUPPORTED"))
        c = self.m["clash_f.nif"]
        self.assertEqual((c.action, c.status), ("REVIEW", "UNSUPPORTED_NIF_STRUCTURE"))
        self.assertIn("Chest", " ".join(c.unsupported_structures))

    def test_layering_risk_note(self):
        # the boots embed a plain CBBE body (body slot 33, not converted without the opt-in) while tops are CONVERT
        self.assertTrue(any("LAYERING RISK" in n and "bootsbody_F.nif" in n for n in self.rep.notes), self.rep.notes)

    def test_mixed_package_and_owned_meshes(self):
        ctl, fi = setup()
        # the BodySlide package ships a plugin: one item uses the mesh its project builds, one a direct mesh
        m = fi.mods / PACKAGE
        from synth import BONE_POS, outfit_patch, write_nif
        from synth import make_body

        write_nif(m / "meshes" / "Pkg" / "extra_F.nif", [outfit_patch(make_body(False), (85, 115), name="Extra",
                                                                         only_bones=["SPINE1", "SPINE2", "Chest", "Neck", "Pelvis"])], BONE_POS)
        write_plugin(m / "Pkg.esp", [dict(editor_id="AA_PkgTop", female="Pkg\\top.nif", items=[("PkgTop", "Pkg Top")]),
                                     dict(editor_id="AA_Extra", female="Pkg\\extra_F.nif", items=[("Extra", "Pkg Extra")])])
        ctl.detect(ctl.installation.profile)
        prev = ctl.analyze_package(PACKAGE, allow_direct_nif=True, write=False)
        self.assertEqual(prev.source_mode, SOURCE_MIXED)
        self.assertTrue(prev.projects)  # the BodySlide path is still the preferred source for its own meshes
        mm = meshes(ctl)
        self.assertEqual((mm["top.nif"].action, mm["top.nif"].status), ("SKIP", "DUPLICATE_GAME_MESH"))
        self.assertEqual(mm["extra_f.nif"].action, "CONVERT")

    def test_source_mode_rules_and_plugin_refs(self):
        self.assertEqual(detect_source_mode(0, 0), SOURCE_NONE)
        self.assertEqual(detect_source_mode(3, 0), SOURCE_BODYSLIDE_PROJECT)
        self.assertEqual(detect_source_mode(0, 2), SOURCE_DIRECT_NIF)
        self.assertEqual(detect_source_mode(3, 2), SOURCE_MIXED)
        pa = read_plugin_armors(self.fi.mods / DIRECT_PACKAGE / "Direct.esp")
        refs = plugin_mesh_refs(pa)
        self.assertEqual({i.role for i in refs["direct/unisex.nif"]}, {"male 3rd person", "female 3rd person"})
        self.assertEqual(refs["direct/bootsbody_f.nif"][0].biped_slots, [33])

    def test_no_convertible_source(self):
        ctl, fi = setup()
        write_plugin(fi.mods / DIRECT_PACKAGE / "Direct.esp", [dict(editor_id="AA_M", male="Direct\\male_M.nif",
                                                                    items=[("M", "Male only")])])
        ctl.analyze_package(DIRECT_PACKAGE, allow_direct_nif=True, write=False)
        self.assertEqual(ctl.direct_report.source_mode, SOURCE_NONE)
        self.assertEqual(ctl.direct_report.unique_female_meshes, 0)

    def test_inactive_plugin_is_reported(self):
        ctl, fi = setup(active=["Other.esp"])
        ctl.analyze_package(DIRECT_PACKAGE, allow_direct_nif=True, write=False)
        self.assertFalse(ctl.direct_report.plugins[0]["enabled"])
        self.assertTrue(any("not active" in n for n in ctl.direct_report.notes))

    def test_cli(self):
        ctl, fi = setup()
        rc = cli_main(["--home", str(ctl.paths.root), "analyze-package", "--mo2", str(fi.mo2_dir), "--package",
                       DIRECT_PACKAGE, "--allow-direct-nif"])
        self.assertEqual(rc, 0)
        rc = cli_main(["--home", str(ctl.paths.root), "analyze-package", "--mo2", str(fi.mo2_dir), "--package",
                       DIRECT_PACKAGE])
        self.assertEqual(rc, 2)
        self.assertFalse((fi.mods / "CBBE 3BBB Auto Conversions").exists())


if __name__ == "__main__":
    unittest.main()
