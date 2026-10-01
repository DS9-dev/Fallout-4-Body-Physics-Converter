"""Phase 2: runtime detection, planning (dry run), automation scripts, execution, validation."""
import json
import unittest
from pathlib import Path
from unittest import mock

from tests._util import fake_install, temp_home, tree_digest
from cbbe3bbb.app.controller import AppController, AppPaths
from cbbe3bbb.bodyslide.osd import OsdError, read_osd, read_osd_bytes
from cbbe3bbb.bodyslide.osp import parse_osp
from cbbe3bbb.conversion import automation as A
from cbbe3bbb.conversion.plan import generated_names
from cbbe3bbb.conversion.runtime import (
    RuntimeError_, detect_runtime, prepare_private_runtime, write_runtime_config,
)
from cbbe3bbb.nif.reader import read_nif
from cbbe3bbb.profiles.store import Profile
from fake_os import FakeOutfitStudio, make_fake_runtime
from synth import write_osd

TOP = "Test Top - CBBE"


def controller(os_version=(5, 8, 3, 0), automation=True, run_mode="auto", allowed=(TOP,)):
    fi = fake_install()
    home = temp_home()
    rt_src = make_fake_runtime(home / "download" / "os", os_version, automation)
    ctl = AppController(AppPaths(home / "tool"))
    p = Profile(name="t", mo2_dir=str(fi.mo2_dir))
    p.conversion.outfitstudio_dir = str(rt_src)
    p.conversion.allowed_projects = list(allowed)
    p.conversion.os_run_mode = run_mode
    ctl.detect(p)
    ctl.analyze(write=False, workers=1)
    return ctl, fi, home


def project(ctl, name):
    return next(p for p in ctl.report.projects if p.project == name)


class RuntimeTests(unittest.TestCase):
    def test_571_has_no_headless(self):
        d = make_fake_runtime(temp_home() / "os571", (5, 7, 1, 0), automation=False)
        info = detect_runtime(d)
        self.assertEqual(info.version, "5.7.1.0")
        self.assertFalse(info.headless_supported)
        self.assertEqual(info.headless_label, "NO")
        self.assertIn("5.8.0", info.reason)

    def test_582_supported_but_headless_weight_copy_broken(self):
        info = detect_runtime(make_fake_runtime(temp_home() / "os582", (5, 8, 2, 0)))
        self.assertTrue(info.headless_supported)
        self.assertEqual(info.headless_label, "YES")
        self.assertFalse(info.headless_weight_copy_ok)
        self.assertEqual(info.recommended_run_mode, "interactive")

    def test_newer_version_headless(self):
        info = detect_runtime(make_fake_runtime(temp_home() / "os583", (5, 8, 3, 0)))
        self.assertTrue(info.headless_supported)
        self.assertEqual(info.recommended_run_mode, "headless")

    def test_version_without_automation_option(self):
        info = detect_runtime(make_fake_runtime(temp_home() / "osx", (5, 8, 2, 0), automation=False))
        self.assertFalse(info.headless_supported)
        self.assertIn("automation", info.reason)

    def test_missing_folder_and_exe(self):
        self.assertFalse(detect_runtime("").headless_supported)
        empty = temp_home()
        info = detect_runtime(empty)
        self.assertFalse(info.headless_supported)
        self.assertIn("no Outfit Studio executable", info.reason)

    def test_private_copy_excludes_project_data(self):
        home = temp_home()
        src = make_fake_runtime(home / "dl", (5, 8, 2, 0))
        before = tree_digest(src)
        info = prepare_private_runtime(src, home / "runtime")
        self.assertTrue(info.private_copy)
        dest = Path(info.folder)
        self.assertTrue((dest / "OutfitStudio x64.exe").is_file())
        self.assertTrue((dest / "res" / "skeleton_fo4.nif").is_file())
        self.assertFalse((dest / "SliderSets").exists())
        self.assertFalse((dest / "Config.xml").exists())
        self.assertEqual(tree_digest(src), before)  # source only read
        cfg = write_runtime_config(dest, home / "ws" / "Data" / "Tools" / "BodySlide", home / "ws" / "Data")
        text = cfg.read_text(encoding="utf-8")
        for needle in ("<TargetGame>3</TargetGame>", "<ProjectPath>", "<BSATextureScan>false", "res/skeleton_fo4.nif", "<SkeletonRootName>Root"):
            self.assertIn(needle, text)
        self.assertEqual(prepare_private_runtime(src, home / "runtime").folder, info.folder)  # reused

    def test_config_refused_outside_private_copy(self):
        src = make_fake_runtime(temp_home() / "dl", (5, 8, 2, 0))
        with self.assertRaises(RuntimeError_):
            write_runtime_config(src, src, src)

    def test_prepare_refuses_unsupported(self):
        src = make_fake_runtime(temp_home() / "dl", (5, 7, 1, 0), automation=False)
        with self.assertRaises(RuntimeError_):
            prepare_private_runtime(src, temp_home() / "rt")


class ScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, cls.home = controller()
        cls.plan = cls.ctl.plan_conversion(project(cls.ctl, TOP))

    def test_plan_is_convertible(self):
        self.assertEqual(self.plan.blockers, [])
        self.assertTrue(self.plan.script_xml)

    def test_step_order_and_parameters(self):
        sc = A.parse_script(self.plan.script_xml)
        types = [s.type for s in sc.steps]
        self.assertEqual(types[:3], ["ClearProject", "LoadReference", "AddProject"])
        self.assertEqual(types[-2:], ["CopyBoneWeights", "SaveProject"])
        ref = sc.step("LoadReference")
        self.assertEqual(ref.params["Set"], "CBBE 3BBB Body")
        self.assertEqual(ref.params["Shape"], "CBBE-3BBB")
        cbw = sc.step("CopyBoneWeights")
        self.assertEqual(cbw.target_meshes, ["TopShape"])
        self.assertAlmostEqual(float(cbw.params["ProximityRadius"]), 5.0)
        self.assertEqual(cbw.params["MaxResults"], "4")
        self.assertNotIn("BoneList", cbw.params)  # full copy mode

    def test_autocopyref_false_invariant(self):
        save = A.parse_script(self.plan.script_xml).step("SaveProject")
        self.assertEqual(save.params["AutoCopyRef"], "false")
        self.assertIn("<AutoCopyRef>false</AutoCopyRef>", self.plan.script_xml)
        missing = self.plan.script_xml.replace("<AutoCopyRef>false</AutoCopyRef>", "")
        with self.assertRaises(A.ScriptInvariantError):
            A.check_invariants(missing)
        true = self.plan.script_xml.replace("<AutoCopyRef>false</AutoCopyRef>", "<AutoCopyRef>true</AutoCopyRef>")
        with self.assertRaises(A.ScriptInvariantError):
            A.check_invariants(true)

    def test_forbidden_steps_and_paths(self):
        sc = A.parse_script(self.plan.script_xml)
        sc.steps.insert(1, A.AutomationStep("ExportFile", {"FilePath": "x.nif"}))
        with self.assertRaises(A.ScriptInvariantError):
            A.check_invariants(sc)
        sc = A.parse_script(self.plan.script_xml)
        sc.step("AddProject").params["SourceFile"] = "C:/Games/Fallout 4/Data/Tools/BodySlide/SliderSets/x.osp"
        with self.assertRaises(A.ScriptInvariantError):
            A.check_invariants(sc)
        sc = A.parse_script(self.plan.script_xml)
        sc.step("SaveProject").params["SliderSetFile"] = "../../outside.osp"
        with self.assertRaises(A.ScriptInvariantError):
            A.check_invariants(sc)

    def test_xml_matches_outfit_studio_format(self):
        import xml.etree.ElementTree as ET

        root = ET.fromstring(self.plan.script_xml)
        self.assertEqual(root.tag, "AutomationScript")
        self.assertEqual(root.get("version"), "1")
        for st in root.findall("Step"):
            self.assertIn(st.get("active"), ("true", "false"))
        self.assertEqual(root.find("Step[@type='CopyBoneWeights']/TargetMeshes").text, "TopShape")

    def test_output_path_preserved_and_names(self):
        src = parse_osp(Path(project(self.ctl, TOP).osp_path)).sets[0]
        p = self.plan
        self.assertEqual(p.output_game_path, src.output_path)
        self.assertEqual(p.output_game_file, src.output_file)
        save = A.parse_script(p.script_xml).step("SaveProject")
        self.assertEqual(save.params["OutputDataPath"], src.output_path)
        self.assertEqual(save.params["OutputFileName"], src.output_file)
        self.assertEqual(p.output_project, TOP + " - 3BBB AUTO")
        self.assertNotEqual(p.output_osp_rel.lower(), p.source_osp_rel.lower())
        self.assertNotEqual(p.output_shapedata_folder, "Test Top")

    def test_region_and_bone_selection(self):
        p = self.plan
        self.assertEqual(p.regions, ["BREAST"])
        self.assertEqual(sorted(p.physics_bones), sorted(b for b in p.physics_bones if "Breast_0" in b))
        self.assertEqual(len(p.physics_bones), 6)
        self.assertEqual(p.shape_plans[0].bones_outside_regions, [])

    def test_regions_mode_bone_list(self):
        prof = self.ctl.installation.profile
        prof.conversion.weight_mode = "regions"
        try:
            p = self.ctl.plan_conversion(project(self.ctl, TOP))
        finally:
            prof.conversion.weight_mode = "auto"
        self.assertEqual(p.weight_mode, "regions")
        self.assertIn("LBreast_skin", p.bone_list)
        self.assertTrue(all("Butt" not in b and "Thigh" not in b for b in p.bone_list))
        self.assertIn("<BoneList>", p.script_xml)

    def test_generated_names_are_file_safe(self):
        n = generated_names('A/B: "C"?')
        self.assertEqual(n["display"], 'A/B: "C"? - 3BBB AUTO')
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, n["shapedata_folder"])


class PlanGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, cls.home = controller(allowed=(TOP, "Test Top - 3BBB", "Test Pants - CBBE"))

    def test_not_allowed_in_milestone(self):
        self.ctl.installation.profile.conversion.allowed_projects = ["Something Else"]
        try:
            p = self.ctl.plan_conversion(project(self.ctl, TOP))
        finally:
            self.ctl.installation.profile.conversion.allowed_projects = [TOP, "Test Top - 3BBB", "Test Pants - CBBE"]
        self.assertTrue(any("enabled only for" in b for b in p.blockers))

    def test_not_convert_is_blocked(self):
        p = self.ctl.plan_conversion(project(self.ctl, "Test Top - 3BBB"))
        self.assertTrue(any("KEEP" in b for b in p.blockers))
        self.assertEqual(p.script_xml, "")

    def test_runtime_without_headless_blocks(self):
        self.ctl.installation.profile.conversion.outfitstudio_dir = str(temp_home())
        try:
            p = self.ctl.plan_conversion(project(self.ctl, TOP))
        finally:
            self.ctl.installation.profile.conversion.outfitstudio_dir = str(self.home / "download" / "os")
        self.assertTrue(any("headless automation unavailable" in b for b in p.blockers))

    def test_existing_foreign_output_folder_blocks(self):
        d = temp_home() / "SomeMod"
        d.mkdir()
        (d / "readme.txt").write_text("not ours")
        p = self.ctl.plan_conversion(project(self.ctl, TOP), output_mod=str(d))
        self.assertTrue(any("not a CBBE3BBBConverter output mod" in b for b in p.blockers))

    def test_duplicate_generated_name_blocks(self):
        import copy

        dup = copy.deepcopy(project(self.ctl, TOP))
        dup.project = TOP + " - 3BBB AUTO"
        dup.mod = "Other Mod"
        self.ctl.report.projects.append(dup)
        try:
            p = self.ctl.plan_conversion(project(self.ctl, TOP))
        finally:
            self.ctl.report.projects.remove(dup)
        self.assertTrue(any("already exists" in b for b in p.blockers))

    def test_same_output_mesh_warning(self):
        p = self.ctl.plan_conversion(project(self.ctl, TOP))
        self.assertTrue(any("keeps building the same game mesh" in w for w in p.warnings))

    def test_dry_run_performs_no_writes(self):
        before_src = tree_digest(self.fi.root)
        tool = self.ctl.paths.root
        before_tool = {k: v for k, v in tree_digest(tool).items() if not k.startswith("logs")}
        p = self.ctl.plan_conversion(project(self.ctl, TOP))
        self.assertTrue(p.can_convert)
        self.assertEqual(tree_digest(self.fi.root), before_src)
        after_tool = {k: v for k, v in tree_digest(tool).items() if not k.startswith("logs")}
        self.assertEqual(after_tool, before_tool)
        self.assertFalse(Path(p.output_mod_path).exists())
        self.assertFalse((tool / "work").exists())
        self.assertFalse((tool / "runtime").exists())

    def test_run_mode_follows_runtime(self):
        ctl, _, _ = controller(os_version=(5, 8, 2, 0))
        p = ctl.plan_conversion(project(ctl, TOP))
        self.assertEqual(p.os_run_mode, "interactive")


class EngineTests(unittest.TestCase):
    def run_conv(self, mode="ok", os_version=(5, 8, 3, 0), tamper=None):
        ctl, fi, home = controller(os_version=os_version)
        out_mod = home / "mods" / "CBBE 3BBB Auto Conversions"
        plan = ctl.plan_conversion(project(ctl, TOP), output_mod=str(out_mod))
        self.assertTrue(plan.can_convert, plan.blockers)
        if tamper:
            tamper(plan)
        before = tree_digest(fi.root)
        fake = FakeOutfitStudio(mode)
        res = ctl.convert(plan, runner=fake)
        self.assertEqual(tree_digest(fi.root), before, "source installation changed")
        return res, plan, out_mod, fake

    def test_success(self):
        res, plan, out_mod, fake = self.run_conv()
        self.assertEqual(res.status, "SUCCESS", res.reasons)
        self.assertFalse(fake.calls[0][3])  # headless for a fixed version
        osp = out_mod / "Tools/BodySlide" / plan.output_osp_rel
        nif = out_mod / "Tools/BodySlide/ShapeData" / plan.output_shapedata_folder / plan.output_shapedata_file
        self.assertTrue(osp.is_file() and nif.is_file())
        shapes = [s.name for s in read_nif(nif).shapes]
        # the original's own embedded CBBE body stays; the CBBE-3BBB reference is not added
        self.assertEqual(sorted(shapes), ["CBBE", "TopShape"])
        self.assertNotIn("CBBE-3BBB", shapes)
        self.assertEqual(res.generated_classification["action"], "KEEP")
        self.assertEqual(res.source_hashes_before, res.source_hashes_after)
        man = json.loads((out_mod / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(man["tool"], "CBBE3BBBConverter")
        rec = man["conversions"][0]
        for key in ("source", "target", "generated_files", "outfit_studio", "settings", "reference", "timestamp", "tool_version"):
            self.assertIn(key, rec)
        self.assertEqual(rec["source"]["project"], TOP)
        self.assertTrue(all(f["sha256"] for f in rec["source"]["files"]))
        self.assertEqual({f["role"] for f in rec["source"]["files"]} >= {"source-osp", "source-nif", "source-osd"}, True)
        self.assertFalse(rec["settings"]["auto_copy_ref"])
        self.assertEqual(rec["target"]["game_output_file"], plan.output_game_file)
        self.assertTrue(all(len(f["sha256"]) == 64 for f in rec["generated_files"]))
        group = out_mod / "Tools/BodySlide/SliderGroups/CBBE 3BBB Auto Conversions.xml"
        self.assertIn(plan.output_project, group.read_text(encoding="utf-8"))
        # second conversion of the same project is refused (no silent overwrite)
        ctl2, _, _ = controller()
        p2 = ctl2.plan_conversion(project(ctl2, TOP), output_mod=str(out_mod))
        self.assertTrue(any("already exists" in b for b in p2.blockers))

    def test_interactive_mode_for_582(self):
        res, plan, out_mod, fake = self.run_conv(os_version=(5, 8, 2, 0))
        self.assertEqual(plan.os_run_mode, "interactive")
        self.assertTrue(fake.calls[0][3])
        self.assertEqual(res.status, "SUCCESS", res.reasons)
        self.assertEqual(res.run["mode"], "interactive")

    def test_interactive_closed_without_running(self):
        res, plan, out_mod, _ = self.run_conv("closed_without_running", os_version=(5, 8, 2, 0))
        self.assertEqual(res.status, "FAILED")
        self.assertTrue(any("without executing" in r for r in res.reasons))
        self.assertFalse(out_mod.exists())

    def assert_isolated_failure(self, res, out_mod):
        self.assertEqual(res.status, "FAILED")
        self.assertFalse(out_mod.exists(), "nothing may be written to the output mod on failure")
        ws = Path(res.workspace)
        self.assertTrue((ws / "result.json").is_file())
        self.assertTrue((ws / "plan.json").is_file())

    def test_embedded_reference_fails(self):
        res, plan, out_mod, _ = self.run_conv("embed_ref")
        self.assert_isolated_failure(res, out_mod)
        failed = [c["name"] for c in res.validation["checks"] if not c["ok"]]
        self.assertIn("CBBE-3BBB body NOT embedded", failed)
        self.assertIn("reference shape not listed in project", failed)
        self.assertTrue((Path(res.workspace) / "logs" / "validation.txt").is_file())

    def test_extra_slider_fails(self):
        res, _, out_mod, _ = self.run_conv("extra_slider")
        self.assert_isolated_failure(res, out_mod)
        self.assertTrue(any("sliders preserved" in r for r in res.reasons))

    def test_step_error_fails(self):
        res, _, out_mod, _ = self.run_conv("exit10")
        self.assert_isolated_failure(res, out_mod)
        self.assertTrue(any("exit code 10" in r for r in res.reasons))

    def test_crash_and_hang_fail(self):
        for mode, needle in (("crash", "crashed"), ("hang", "did not finish")):
            res, _, out_mod, _ = self.run_conv(mode)
            self.assert_isolated_failure(res, out_mod)
            self.assertTrue(any(needle in r for r in res.reasons), res.reasons)

    def test_source_changed_after_plan_fails_before_run(self):
        def tamper(plan):
            sf = next(s for s in plan.staged_files if s.role == "source-osp")
            sf.size += 1  # simulates the file changing after the dry run
        res, _, out_mod, fake = self.run_conv(tamper=tamper)
        self.assert_isolated_failure(res, out_mod)
        self.assertEqual(fake.calls, [])
        self.assertTrue(any("changed since the plan" in r for r in res.reasons))

    def test_commit_rollback(self):
        from cbbe3bbb.conversion import manifest as M

        with mock.patch.object(M.Manifest, "save", side_effect=OSError("disk full")):
            res, plan, out_mod, _ = self.run_conv()
        self.assertEqual(res.status, "FAILED")
        self.assertTrue(any("could not write the output mod" in r for r in res.reasons))
        left = [p for p in out_mod.rglob("*") if p.is_file()] if out_mod.exists() else []
        self.assertEqual(left, [])


class OsdTests(unittest.TestCase):
    def test_roundtrip(self):
        import numpy as np

        p = temp_home() / "a.osd"
        write_osd(p, {"ShapeBreasts": (np.array([1, 5]), np.array([[0.1, 0.2, 0.3], [1, 2, 3]]))})
        o = read_osd(p)
        self.assertEqual(o.names, ["ShapeBreasts"])
        d = o.dense("ShapeBreasts", 8)
        self.assertAlmostEqual(float(d[5, 2]), 3.0, places=5)
        with self.assertRaises(OsdError):
            read_osd_bytes(b"OSD\0garbage")



# ---------------------------------------------------------------------------
# Execution evidence: the Windows / Outfit Studio 5.8.2 interactive false FAILED
# ---------------------------------------------------------------------------

# Log_OS.txt of the real interactive run on Windows (tool 0.2.0 reported FAILED although the
# script ran): no "Automation: Step" lines, because AutomationDialog redirects wxLog to its window.
WINDOWS_582_INTERACTIVE_LOG = r"""[#Log Sun Sep 27 23:28:02 2026]
[23:28:02][3]	OutfitStudio.cpp(528): Initializing Outfit Studio...
[23:28:02][3]	OutfitStudio.cpp(547): Working directory: T:\Fallout 4 Tools\CBBE3BBBConverter\runtime\outfitstudio-5.8.2.0
[23:28:02][3]	OutfitStudio.cpp(548): Executable directory: T:\Fallout 4 Tools\CBBE3BBBConverter\runtime\outfitstudio-5.8.2.0
[23:28:02][3]	OutfitStudio.cpp(960): Game data path in config: T:\Fallout 4 Tools\CBBE3BBBConverter\work\conversions\20260927-232800_cbbe3bbb_MM69_Raider_Punk_Top_-_CBBE_-_3BBB_AUTO\Data\
[23:28:02][3]	OutfitStudio.cpp(967): Project path in config: T:\Fallout 4 Tools\CBBE3BBBConverter\work\conversions\20260927-232800_cbbe3bbb_MM69_Raider_Punk_Top_-_CBBE_-_3BBB_AUTO\Data\Tools\BodySlide
[23:28:02][3]	OutfitStudio.cpp(1193): Using language 'English'.
[23:28:02][3]	OutfitStudio.cpp(568): Target game: Fallout 4
[23:28:02][3]	OutfitStudio.cpp(1228): Loading Outfit Studio frame at X:100 Y:100 with W:1700 H:1125...
[23:28:02][2]	imagpng.cpp(194): iCCP: CRC error
[23:28:02][2]	imagpng.cpp(194): iCCP: CRC error
[23:28:02][2]	imagpng.cpp(194): iCCP: CRC error
[23:28:02][2]	imagpng.cpp(194): iCCP: CRC error
[23:28:02][2]	imagpng.cpp(194): iCCP: CRC error
[23:28:02][2]	imagpng.cpp(194): iCCP: CRC error
[23:28:02][3]	GLSurface.cpp(76): OpenGL attributes are supported.
[23:28:02][3]	Anim.cpp(891): Loaded skeleton 'T:\Fallout 4 Tools\CBBE3BBBConverter\runtime\outfitstudio-5.8.2.0\res/skeleton_fo4.nif' with root 'Root'.
[23:28:03][3]	OutfitStudio.cpp(1554): Outfit Studio frame loaded.
[23:28:03][3]	OutfitStudio.cpp(735): Outfit Studio initialized.
[23:28:03][3]	GLSurface.cpp(120): OpenGL Context Info:
[23:28:03][3]	GLSurface.cpp(121): -> Vendor:   'NVIDIA Corporation'
[23:28:03][3]	GLSurface.cpp(122): -> Renderer: 'NVIDIA GeForce RTX 5060 Ti/PCIe/SSE2'
[23:28:03][3]	GLSurface.cpp(123): -> Version:  '3.3.0 NVIDIA 610.62'
[23:31:03][3]	OutfitStudio.cpp(1630): Outfit Studio frame closed.
""".replace("\n", "\r\n")

ORIGINAL_CHECK_NAMES = None  # filled by the first successful headless run


class ExecutionEvidenceTests(unittest.TestCase):
    run_conv = EngineTests.run_conv

    def test_exact_windows_582_log_is_parsed(self):
        import time as _t

        from cbbe3bbb.conversion.evidence import parse_log_session

        s = parse_log_session(WINDOWS_582_INTERACTIVE_LOG)
        self.assertEqual(_t.localtime(s.start)[:6], (2026, 9, 27, 23, 28, 2))
        self.assertEqual(_t.localtime(s.end)[:6], (2026, 9, 27, 23, 31, 3))
        self.assertEqual(round(s.end - s.start), 181)  # the run took 181.75 s
        self.assertEqual(s.step_lines, [])  # the reason 0.2.0 reported FAILED
        self.assertEqual(s.error_lines, [])
        self.assertTrue(s.initialized and s.frame_closed)
        self.assertEqual(s.project_paths, [r"T:\Fallout 4 Tools\CBBE3BBBConverter\work\conversions"
                                           r"\20260927-232800_cbbe3bbb_MM69_Raider_Punk_Top_-_CBBE_-_3BBB_AUTO\Data\Tools\BodySlide"])

    def test_log_past_midnight(self):
        from cbbe3bbb.conversion.evidence import parse_log_session

        s = parse_log_session("[#Log Sun Sep 27 23:59:58 2026]\n[23:59:58][3]\ta\n[00:00:05][3]\tb\n")
        self.assertEqual(round(s.end - s.start), 7)

    def test_windows_interactive_run_is_accepted(self):
        """Regression: interactive 5.8.2 run with a Windows log without step lines → SUCCESS."""
        res, plan, out_mod, fake = self.run_conv("windows_interactive", os_version=(5, 8, 2, 0))
        self.assertTrue(fake.calls[0][3])
        self.assertEqual(res.status, "SUCCESS", res.reasons)
        self.assertEqual(res.run["steps_logged"], 0)
        ev = res.run["execution_evidence"]
        self.assertTrue(ev["ok"], ev)
        self.assertTrue(any("redirects wxLog" in n for n in ev["notes"]))
        names = [c["name"] for c in res.validation["checks"]]
        for n in ("execution: Log_OS.txt is from an Outfit Studio session on this workspace",
                  "execution: planned outputs created by this Outfit Studio session",
                  "execution: saved project matches the prepared SaveProject step"):
            self.assertIn(n, names)
        # the content checks are all still there and all required
        base, _, _, _ = self.run_conv()
        content = [c["name"] for c in base.validation["checks"] if not c["name"].startswith("execution:")]
        self.assertEqual([n for n in names if not n.startswith("execution:")], content)
        self.assertTrue(all(c["required"] for c in res.validation["checks"] if c["name"].startswith("execution:")))
        self.assertEqual([c["required"] for c in res.validation["checks"] if not c["name"].startswith("execution:")],
                         [c["required"] for c in base.validation["checks"] if not c["name"].startswith("execution:")])
        self.assertTrue((out_mod / "manifest.json").is_file())

    def test_interactive_rejections(self):
        cases = {
            "closed_without_running": "without executing",
            "stale_outputs": "outside the session",
            "wrong_project_log": "session on this workspace",
            "no_reference_info": "matches the prepared SaveProject step",
        }
        for mode, needle in cases.items():
            with self.subTest(mode=mode):
                res, _, out_mod, _ = self.run_conv(mode, os_version=(5, 8, 2, 0))
                self.assertEqual(res.status, "FAILED")
                self.assertTrue(any(needle in r for r in res.reasons), res.reasons)
                self.assertFalse(out_mod.exists())

    def test_headless_step_log_without_saveproject_rejected(self):
        res, _, out_mod, _ = self.run_conv("step_log_without_save")
        self.assertEqual(res.status, "FAILED")
        self.assertTrue(any("include SaveProject" in r for r in res.reasons), res.reasons)
        self.assertFalse(out_mod.exists())

    def test_preexisting_outputs_rejected(self):
        import time as _t

        from cbbe3bbb.conversion.evidence import collect_evidence, planned_outputs, snapshot
        from cbbe3bbb.conversion.plan import ConversionPlan

        home = temp_home()
        project = home / "Data" / "Tools" / "BodySlide"
        plan = ConversionPlan(output_project="X - 3BBB AUTO", output_osp_rel="SliderSets/X.osp",
                              output_shapedata_folder="X", output_shapedata_file="X.nif",
                              reference_osp_rel="SliderSets/R.osp", reference_set="R", reference_shape="S")
        outs = planned_outputs(plan, project)
        outs["ShapeData NIF"].parent.mkdir(parents=True)
        outs["project file"].parent.mkdir(parents=True)
        outs["ShapeData NIF"].write_bytes(b"x")
        outs["project file"].write_text('<SliderSetInfo><SliderSet name="X - 3BBB AUTO"><ReferenceInfo '
                                        'ProjectFile="SliderSets\\R.osp" ProjectName="R" ShapeName="S"/></SliderSet></SliderSetInfo>')
        before = snapshot(outs)
        t = _t.time()
        log = f"[#Log x]\n[00:00:00][3]\tOutfitStudio.cpp(967): Project path in config: {project}\n"
        ev = collect_evidence(plan, project, log, interactive=True, before=before, window=(t - 10, t + 10))
        self.assertFalse(ev.ok)
        self.assertTrue(any("already existed" in d for _, ok, d in ev.checks if not ok))
        ev2 = collect_evidence(plan, project, log, interactive=True, before=snapshot({}), window=(t - 10, t + 10))
        self.assertTrue(ev2.ok, ev2.checks)  # same files, created inside the window → accepted


class RevalidateTests(unittest.TestCase):
    """An existing workspace from the 0.2.0 false FAILED can be accepted without rerunning Outfit Studio."""

    def false_failed_workspace(self):
        from cbbe3bbb.conversion import engine as E
        from cbbe3bbb.conversion.evidence import ExecutionEvidence

        ctl, fi, home = controller(os_version=(5, 8, 2, 0))
        out_mod = home / "mods" / "CBBE 3BBB Auto Conversions"
        plan = ctl.plan_conversion(project(ctl, TOP), output_mod=str(out_mod))
        old = ExecutionEvidence()
        old.add("execution: logged automation steps include SaveProject", False, "no SaveProject step in its log")
        with mock.patch.object(E, "collect_evidence", return_value=old):  # the 0.2.0 detector
            res = ctl.convert(plan, runner=FakeOutfitStudio("windows_interactive"))
        self.assertEqual(res.status, "FAILED")
        ws = Path(res.workspace)
        r = json.loads((ws / "result.json").read_text(encoding="utf-8"))
        for k in ("started", "ended", "execution_evidence"):  # 0.2.0 did not record these
            r["run"].pop(k, None)
        r["reasons"] = ["Outfit Studio was closed without executing the conversion script (no SaveProject step in its log)"]
        (ws / "result.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
        self.assertFalse(out_mod.exists())
        return ctl, fi, ws, out_mod, plan

    def test_revalidate_then_commit(self):
        ctl, fi, ws, out_mod, plan = self.false_failed_workspace()
        keep = {p: p.read_bytes() for p in ws.rglob("*") if p.is_file()}
        before_src = tree_digest(fi.root)
        res = ctl.revalidate(ws)
        self.assertEqual(res.status, "VALIDATED", res.reasons)
        self.assertFalse(out_mod.exists(), "revalidation without --commit writes nothing")
        self.assertEqual(res.run["execution_evidence"]["window_source"], "Log_OS.txt timestamps")
        self.assertTrue(all(c["ok"] for c in res.validation["checks"]))
        self.assertIn("execution: Log_OS.txt session length matches the recorded Outfit Studio run",
                      [c["name"] for c in res.validation["checks"]])
        for p, b in keep.items():  # originals preserved byte for byte
            self.assertEqual(p.read_bytes(), b, p)
        self.assertTrue((ws / "result.revalidated.json").is_file())
        self.assertTrue((ws / "logs" / "validation.revalidated.txt").is_file())
        res2 = ctl.revalidate(ws.name, commit=True)  # an id relative to work/conversions works too
        self.assertEqual(res2.status, "SUCCESS", res2.reasons)
        osp = out_mod / "Tools/BodySlide" / plan.output_osp_rel
        self.assertTrue(osp.is_file())
        self.assertEqual(osp.read_bytes(), (ws / "Data/Tools/BodySlide" / plan.output_osp_rel).read_bytes())
        rec = json.loads((out_mod / "manifest.json").read_text(encoding="utf-8"))["conversions"][0]
        self.assertIn("revalidate", rec["accepted_by"])
        self.assertTrue(rec["execution_evidence"]["ok"])
        self.assertEqual(tree_digest(fi.root), before_src)
        res3 = ctl.revalidate(ws, commit=True)  # never a silent second commit
        self.assertEqual(res3.status, "FAILED")
        self.assertTrue(any("already exists" in r for r in res3.reasons), res3.reasons)

    def test_revalidate_rejects_modified_output(self):
        import os as _os
        import time as _t

        ctl, _, ws, out_mod, plan = self.false_failed_workspace()
        osp = ws / "Data/Tools/BodySlide" / plan.output_osp_rel
        later = _t.time() + 600  # edited after the Outfit Studio session
        _os.utime(osp, (later, later))
        res = ctl.revalidate(ws, commit=True)
        self.assertEqual(res.status, "FAILED")
        self.assertTrue(any("outside the session" in r for r in res.reasons), res.reasons)
        self.assertFalse(out_mod.exists())

    def test_revalidate_rejects_foreign_log_and_folders(self):
        ctl, _, ws, out_mod, _ = self.false_failed_workspace()
        lf = ws / "logs" / "Log_OS.txt"
        lf.write_text(lf.read_text(encoding="utf-8").replace("Project path in config: ", "Project path in config: X:\\other"),
                      encoding="utf-8")
        res = ctl.revalidate(ws, commit=True)
        self.assertEqual(res.status, "FAILED")
        self.assertTrue(any("session on this workspace" in r for r in res.reasons), res.reasons)
        self.assertFalse(out_mod.exists())
        other = temp_home()
        res = ctl.revalidate(other, commit=True)
        self.assertEqual(res.status, "FAILED")
        self.assertTrue(any("not a conversion workspace" in r for r in res.reasons))
        self.assertEqual(list(other.iterdir()), [])

    def test_revalidate_cli(self):
        import io
        from contextlib import redirect_stdout

        from cbbe3bbb.cli import main as cli_main
        from tests.test_app import _drop_handlers

        ctl, fi, ws, out_mod, _ = self.false_failed_workspace()
        prof = ctl.installation.profile
        ctl.save_profile(prof)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["--home", str(ctl.paths.root), "revalidate", "--profile", prof.name,
                             "--workspace", str(ws)])
        _drop_handlers()
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("RESULT: VALIDATED", buf.getvalue())
        self.assertFalse(out_mod.exists())

if __name__ == "__main__":
    unittest.main()


class CliConversionTests(unittest.TestCase):
    def test_plan_cli_is_dry_and_gated(self):
        import io
        from contextlib import redirect_stdout

        from cbbe3bbb.cli import main as cli_main
        from tests.test_app import _drop_handlers

        fi = fake_install()
        home = temp_home()
        rt = make_fake_runtime(home / "os", (5, 8, 2, 0))
        before = tree_digest(fi.root)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["--home", str(home / "tool"), "plan", "--mo2", str(fi.mo2_dir), "--project", TOP, "--os-dir", str(rt)])
        _drop_handlers()
        out = buf.getvalue()
        self.assertEqual(code, 3)  # the default milestone gate allows only the Raider Punk Top
        self.assertIn("enabled only for MM69 Raider Punk Top - CBBE", out)
        self.assertIn("DRY RUN: nothing was written.", out)
        self.assertEqual(tree_digest(fi.root), before)
        self.assertFalse((home / "tool" / "work").exists())
