"""Phase 3: package analysis, Batch Preview / Dry Run, batch execution, per-project validation, commit."""
import json
import os
import time
import unittest
from pathlib import Path

from tests._util import temp_home, tree_digest
from cbbe3bbb.app.controller import AppController, AppPaths
from cbbe3bbb.conversion import automation as A
from cbbe3bbb.conversion.batch import BatchError, plan_sessions, project_status
from cbbe3bbb.conversion.plan import ConversionPlan, build_script
from cbbe3bbb.nif.reader import read_nif
from cbbe3bbb.profiles.store import Profile
from fake_os import FakeOutfitStudio, make_fake_runtime
from synth import PACKAGE, build_package_install, osp_xml

BS = Path("Tools/BodySlide")
DATA = Path(__file__).parent / "data"


def setup(os_version=(5, 8, 2, 0), extra=None, session_mode="single", allowed=None):
    home = temp_home()
    fi = build_package_install(home / "inst", extra=extra)
    rt = make_fake_runtime(home / "os", os_version)
    ctl = AppController(AppPaths(home / "tool"))
    p = Profile(name="t", mo2_dir=str(fi.mo2_dir))
    p.conversion.outfitstudio_dir = str(rt)
    p.conversion.batch_session_mode = session_mode
    if allowed is not None:
        p.conversion.allowed_projects = list(allowed)
    ctl.detect(p)
    return ctl, fi, home


def out_mod(fi):
    return fi.mods / "CBBE 3BBB Auto Conversions"


def by_name(prev):
    return {p.project: p for p in prev.projects}


def run(ctl, prev, **fake):
    fake_os = FakeOutfitStudio(fake.pop("mode", "windows_interactive"), **fake)
    bdir, summary = ctl.convert_batch(prev, runner=fake_os)
    return bdir, summary, fake_os, {p["project"]: p for p in summary["projects"]}


class PackagePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, cls.home = setup()
        cls.before = tree_digest(cls.fi.mods / PACKAGE)
        cls.prev = cls.ctl.analyze_package(PACKAGE)

    def test_package_discovery(self):
        self.assertIn(PACKAGE, self.ctl.package_names())
        self.assertIn("Other Outfits", self.ctl.package_names())
        names = set(by_name(self.prev))
        self.assertEqual(names, {"Pkg Top - CBBE", "Pkg Shorts - CBBE", "Pkg Suit - CBBE", "Pkg Bracelet - CBBE",
                                 "Pkg Shirt - BT3", "Pkg Bra - CBBE", "Pkg Bra - 3BBB TEST", "Pkg Bodysuit - CBBE",
                                 "Pkg Broken - CBBE"})
        self.assertNotIn("Other Top - CBBE", names)  # another mod is not part of the package
        with self.assertRaises(RuntimeError):
            self.ctl.analyze_package("No Such Mod", write=False)

    def test_classification_and_default_selection(self):
        b = by_name(self.prev)
        expect = {
            "Pkg Top - CBBE": ("CONVERT", "NEW"), "Pkg Shorts - CBBE": ("CONVERT", "NEW"), "Pkg Suit - CBBE": ("CONVERT", "NEW"),
            "Pkg Bracelet - CBBE": ("SKIP", "NO_PHYSICS_REGION"), "Pkg Shirt - BT3": ("UNSUPPORTED", "UNSUPPORTED_BODY"),
            "Pkg Bra - 3BBB TEST": ("KEEP", "ALREADY_3BBB"), "Pkg Bra - CBBE": ("REVIEW", "MANUAL_3BBB_VARIANT"),
            "Pkg Bodysuit - CBBE": ("REVIEW", "EMBEDDED_BODY"), "Pkg Broken - CBBE": ("ERROR", "ERROR"),
        }
        for name, (action, status) in expect.items():
            self.assertEqual((b[name].action, b[name].status), (action, status), (name, b[name].reasons))
        self.assertEqual({p.project for p in self.prev.selected()}, {"Pkg Top - CBBE", "Pkg Shorts - CBBE", "Pkg Suit - CBBE"})
        for name in ("Pkg Shirt - BT3", "Pkg Bra - CBBE", "Pkg Bodysuit - CBBE", "Pkg Broken - CBBE", "Pkg Bracelet - CBBE"):
            self.assertFalse(b[name].selectable, name)
        self.assertEqual(self.prev.counts, {"CONVERT": 3, "KEEP": 1, "REVIEW": 2, "SKIP": 1, "UNSUPPORTED": 1, "ERROR": 1,
                                            "selected": 3})

    def test_per_shape_regions_and_bones(self):
        b = by_name(self.prev)
        ref = self.ctl._knowledge_base().reference
        breast, butt, thigh = (ref.physics_regions[r] for r in ("BREAST", "BUTT", "THIGH"))
        top = {s.name: s for s in b["Pkg Top - CBBE"].shapes}["TopShape"]
        self.assertEqual((top.decision, top.regions, top.confidence), ("CONVERT", ["BREAST"], "HIGH"))
        self.assertEqual(sorted(top.allowed_bones), sorted(breast))
        shorts = {s.name: s for s in b["Pkg Shorts - CBBE"].shapes}["ShortsShape"]
        self.assertEqual(shorts.regions, ["BUTT", "THIGH"])  # butt-only/lower garment: no breast bones required
        self.assertEqual(sorted(shorts.allowed_bones), sorted(butt + thigh))
        suit = {s.name: s for s in b["Pkg Suit - CBBE"].shapes}
        self.assertEqual(suit["SuitTop"].regions, ["BREAST"])
        self.assertEqual(suit["SuitShorts"].regions, ["BUTT", "THIGH"])
        self.assertEqual((suit["SuitBoots"].decision, suit["SuitBoots"].allowed_bones), ("NONE", []))
        plan = b["Pkg Suit - CBBE"].conversion_plan()
        self.assertEqual(plan.untouched_shapes, ["SuitBoots"])
        sc = A.parse_script(plan.script_xml)
        cbw = [s for s in sc.steps if s.type == "CopyBoneWeights"]
        self.assertEqual(sorted(t for s in cbw for t in s.target_meshes), ["SuitShorts", "SuitTop"])
        self.assertTrue(all(sp.copy_mode == "full" for sp in plan.shape_plans))
        self.assertIn("SuitTop: no physics bones outside planned regions (BREAST)", b["Pkg Suit - CBBE"].expected_checks)
        self.assertEqual(plan.source_cloth_data, 1)
        # every diagnostic the preview explains is present
        d = top.region_details["BREAST"]
        for k in ("relevance", "fit", "predicted_vertices", "reference_coverage", "within_radius", "median_distance",
                  "predicted_lr_balance", "side_coverage", "predicted_cross_side_fraction"):
            self.assertIn(k, d)
        embedded = {s.name: s for s in b["Pkg Bodysuit - CBBE"].shapes}["CBBE"]
        self.assertEqual(embedded.decision, "BODY")

    def test_preview_reports_and_no_writes(self):
        paths = self.ctl.preview_paths
        for k in ("json", "txt", "html"):
            self.assertTrue(paths[k].is_file())
            self.assertTrue(paths[k].is_relative_to(self.ctl.paths.reports / "packages"))
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(data["package"], PACKAGE)
        html = paths["html"].read_text(encoding="utf-8")
        for col in ("Project", "Body family", "Shapes", "Regions", "3BBB", "Confidence", "Action", "Reason"):
            self.assertIn(f"<th>{col}</th>", html)
        self.assertIn("Nothing was converted", html)
        self.assertFalse(out_mod(self.fi).exists())
        self.assertFalse((self.ctl.paths.root / "work").exists())
        self.assertEqual(tree_digest(self.fi.mods / PACKAGE), self.before)

    def test_dry_run_and_selection(self):
        prev = self.ctl.analyze_package(PACKAGE, write=False)
        problems, scripts = self.ctl.plan_batch(prev, select=["Pkg Top - CBBE", "Pkg Shorts - CBBE", "Pkg Bra - CBBE"])
        self.assertEqual(len(problems), 1)
        self.assertIn("only CONVERT projects", problems[0])
        self.assertEqual({p.project for p in prev.selected()}, {"Pkg Top - CBBE", "Pkg Shorts - CBBE"})
        self.assertIn("cbbe3bbb_batch_DRYRUN", scripts)
        ranges = A.check_batch_invariants(scripts["cbbe3bbb_batch_DRYRUN"])
        self.assertEqual(len(ranges), 2)
        self.assertEqual(prev.dry_run[0]["projects"], ["Pkg Top - CBBE", "Pkg Shorts - CBBE"])  # cloth project last
        self.assertIn("Choose cloth data", prev.dry_run[0]["instructions"])
        self.assertTrue(self.ctl.preview_paths["scripts"].is_dir())
        prev.select(deselect=["Pkg Shorts - CBBE"])
        self.assertEqual([p.project for p in prev.selected()], ["Pkg Top - CBBE"])
        self.assertFalse(out_mod(self.fi).exists())
        self.assertFalse((self.ctl.paths.root / "work").exists())
        self.assertEqual(tree_digest(self.fi.mods / PACKAGE), self.before)

    def test_preview_roundtrip(self):
        from cbbe3bbb.conversion.package import PackagePreview

        d = json.loads(json.dumps(self.prev.to_dict()))
        p2 = PackagePreview.from_dict(d)
        self.assertEqual([p.project for p in p2.selected()], [p.project for p in self.prev.selected()])
        self.assertEqual(p2.projects[0].shapes[0].name, self.prev.projects[0].shapes[0].name)


class BatchRunTests(unittest.TestCase):
    def test_batch_convert_validate_commit_and_rerun(self):
        ctl, fi, home = setup()
        src_before = tree_digest(fi.mods / PACKAGE)
        prev = ctl.analyze_package(PACKAGE, write=False)
        bdir, summary, fake, rows = run(ctl, prev)
        self.assertEqual(summary["counts"], {"VALIDATED": 3})
        self.assertTrue(all(c[3] for c in fake.calls))  # interactive for 5.8.2
        self.assertEqual(len(fake.calls), 2)  # two sessions: two projects with cloth data
        self.assertEqual([d[1] for d in fake.cloth_dialogs][0], sorted([str(bdir / "Data" / BS / "ShapeData/Pkg Shorts/Pkg Shorts.nif")]))
        self.assertFalse(out_mod(fi).exists(), "nothing may reach the output mod before commit")
        for name in rows:
            pdir = bdir / "projects" / next(e["id"] for e in json.loads((bdir / "batch.json").read_text())["projects"]
                                            if e["project"] == name)
            for f in ("plan.json", "result.json", "logs/validation.txt", "logs/Log_OS.txt"):
                self.assertTrue((pdir / f).is_file(), (name, f))
            r = json.loads((pdir / "result.json").read_text())
            self.assertTrue(r["source_hashes_before"])
            names = [c["name"] for c in r["validation"]["checks"]]
            self.assertIn("execution: planned outputs created by this Outfit Studio session", names)
            self.assertIn("cloth physics data (BSClothExtraData) unchanged", names)
        for f in ("summary.json", "report.html", "report.txt", "batch.json"):
            self.assertTrue((bdir / f).is_file())
        # the interactive Windows log has no step lines; execution is proven by the artifacts
        self.assertEqual(json.loads((next((bdir / "projects").iterdir()) / "result.json").read_text())["run"]["steps_logged"], 0)
        # commit
        with self.assertRaises(BatchError):
            ctl.batch_status(home / "elsewhere")
        s = ctl.commit_batch(bdir.name)
        self.assertEqual(s["counts"], {"COMMITTED": 3})
        om = out_mod(fi)
        for gen in ("Pkg Top - CBBE - 3BBB AUTO", "Pkg Shorts - CBBE - 3BBB AUTO", "Pkg Suit - CBBE - 3BBB AUTO"):
            self.assertTrue((om / BS / "SliderSets" / f"{gen}.osp").is_file())
            self.assertTrue((om / BS / "ShapeData" / gen / f"{gen}.nif").is_file())
        suit = read_nif(om / BS / "ShapeData/Pkg Suit - CBBE - 3BBB AUTO/Pkg Suit - CBBE - 3BBB AUTO.nif")
        bones = {s.name: set(s.weight_matrix()[0]) for s in suit.shapes}
        self.assertTrue(any(b.startswith(("LBreast", "RBreast")) for b in bones["SuitTop"]))
        self.assertFalse(any("Butt" in b or "Thigh_01" in b for b in bones["SuitTop"]))
        self.assertFalse(any("Breast" in b for b in bones["SuitShorts"]))
        self.assertFalse(any(b.endswith("_skin") for b in bones["SuitBoots"]))
        self.assertEqual(len(suit.cloth_data), 1)
        man = json.loads((om / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(man["conversions"]), 3)
        rec = next(r for r in man["conversions"] if r["source"]["project"] == "Pkg Suit - CBBE")
        for k in ("source", "target", "timestamp", "tool_version", "outfit_studio", "reference", "settings", "validation",
                  "regions_by_shape", "confidence", "accepted_by", "batch", "execution_evidence", "package_group"):
            self.assertIn(k, rec)
        self.assertEqual(rec["regions_by_shape"]["SuitTop"]["regions"], ["BREAST"])
        self.assertTrue(rec["accepted_by"].startswith("batch "))
        self.assertTrue(all(f["sha256"] for f in rec["source"]["files"]))
        groups = om / BS / "SliderGroups"
        pkg_group = (groups / f"CBBE 3BBB Auto Conversions - {PACKAGE}.xml").read_text(encoding="utf-8")
        main_group = (groups / "CBBE 3BBB Auto Conversions.xml").read_text(encoding="utf-8")
        for gen in ("Pkg Top - CBBE - 3BBB AUTO", "Pkg Shorts - CBBE - 3BBB AUTO", "Pkg Suit - CBBE - 3BBB AUTO"):
            self.assertIn(gen, pkg_group)
            self.assertIn(gen, main_group)
        self.assertIn("Batch Build", (bdir / "report.txt").read_text(encoding="utf-8"))
        # idempotent: a second preview recognises the conversions and offers nothing
        prev2 = ctl.analyze_package(PACKAGE, write=False)
        b2 = by_name(prev2)
        for name in ("Pkg Top - CBBE", "Pkg Shorts - CBBE", "Pkg Suit - CBBE"):
            self.assertEqual((b2[name].action, b2[name].status), ("KEEP", "ALREADY_CONVERTED"), b2[name].reasons)
        self.assertEqual(prev2.selected(), [])
        self.assertEqual(ctl.plan_batch(prev2, select=["Pkg Top - CBBE"], write=False)[0][0][:14], "'Pkg Top - CBB")
        # committing again writes nothing new
        again = ctl.commit_batch(bdir.name)
        self.assertEqual(again["counts"], {"COMMITTED": 3})
        self.assertEqual(len(json.loads((om / "manifest.json").read_text())["conversions"]), 3)
        self.assertEqual(tree_digest(fi.mods / PACKAGE), src_before, "source files must stay byte-for-byte unchanged")

    def test_failure_isolation_partial_commit(self):
        ctl, fi, _ = setup()
        prev = ctl.analyze_package(PACKAGE, write=False)
        bdir, summary, _, rows = run(ctl, prev, per_project={"Pkg Shorts - CBBE": "embed_ref", "Pkg Top - CBBE": "not_saved"})
        self.assertEqual(rows["Pkg Shorts - CBBE"]["status"], "FAILED")
        self.assertIn("CBBE-3BBB body NOT embedded", rows["Pkg Shorts - CBBE"]["failed_checks"])
        self.assertEqual(rows["Pkg Top - CBBE"]["status"], "FAILED")
        self.assertTrue(any("did not save" in r for r in rows["Pkg Top - CBBE"]["reasons"]))
        self.assertEqual(rows["Pkg Suit - CBBE"]["status"], "VALIDATED")
        s = ctl.commit_batch(bdir)
        self.assertEqual(s["counts"], {"FAILED": 2, "COMMITTED": 1})
        om = out_mod(fi)
        self.assertTrue((om / BS / "SliderSets/Pkg Suit - CBBE - 3BBB AUTO.osp").is_file())
        self.assertFalse((om / BS / "SliderSets/Pkg Shorts - CBBE - 3BBB AUTO.osp").exists())
        self.assertFalse((om / BS / "SliderSets/Pkg Top - CBBE - 3BBB AUTO.osp").exists())
        self.assertEqual(json.loads((bdir / "batch.json").read_text())["state"], "PARTIALLY_COMMITTED")

    def test_review_after_validation_is_not_committed(self):
        ctl, fi, _ = setup()
        prev = ctl.analyze_package(PACKAGE, write=False)
        prev.select(["Pkg Top - CBBE", "Pkg Suit - CBBE"])
        bdir, summary, _, rows = run(ctl, prev, per_project={"Pkg Top - CBBE": "no_weights"})
        self.assertEqual(rows["Pkg Top - CBBE"]["status"], "REVIEW", rows["Pkg Top - CBBE"])
        self.assertEqual(rows["Pkg Suit - CBBE"]["status"], "VALIDATED")
        s = ctl.commit_batch(bdir)
        self.assertEqual(s["counts"], {"REVIEW": 1, "COMMITTED": 1})
        self.assertFalse((out_mod(fi) / BS / "SliderSets/Pkg Top - CBBE - 3BBB AUTO.osp").exists())

    def test_headless_step_error_is_attributed_to_its_project(self):
        ctl, fi, _ = setup(os_version=(5, 8, 3, 0))
        prev = ctl.analyze_package(PACKAGE, write=False)
        b = by_name(prev)
        # headless: projects with cloth data need the interactive cloth dialog → not convertible headless
        self.assertEqual(b["Pkg Shorts - CBBE"].status, "BLOCKED")
        self.assertEqual([p.project for p in prev.selected()], ["Pkg Top - CBBE"])
        bdir, summary, fake, rows = run(ctl, prev, mode="ok", per_project={"Pkg Top - CBBE": "step_error"})
        self.assertFalse(fake.calls[0][3])
        self.assertEqual(rows["Pkg Top - CBBE"]["status"], "FAILED")
        self.assertIn("execution: no automation step errors in Log_OS.txt", rows["Pkg Top - CBBE"]["failed_checks"])

    def test_headless_multi_project_success_and_step_ranges(self):
        ctl, fi, _ = setup(os_version=(5, 8, 3, 0), extra={"Extra Tops": _extra_tops})
        prev = ctl.analyze_package("Extra Tops", write=False)
        self.assertEqual(len(prev.selected()), 2)
        bdir, summary, fake, rows = run(ctl, prev, mode="ok", per_project={"Extra Top B - CBBE": "step_error"})
        self.assertEqual(len(fake.calls), 1)  # one session, one script
        self.assertEqual(rows["Extra Top A - CBBE"]["status"], "VALIDATED")
        self.assertEqual(rows["Extra Top B - CBBE"]["status"], "FAILED")
        r = json.loads(next(p / "result.json" for p in (bdir / "projects").iterdir() if "Top_A" in p.name).read_text())
        self.assertGreater(r["run"]["steps_logged"], 0)
        self.assertEqual(r["run"]["step_range"][0], 1)

    def test_cloth_dialog_wrong_answers_fail(self):
        for answer, status in (("none", "FAILED"), ("all", "FAILED")):
            with self.subTest(answer=answer):
                ctl, fi, _ = setup(session_mode="per_project" if answer == "all" else "single")
                prev = ctl.analyze_package(PACKAGE, write=False)
                prev.select(["Pkg Shorts - CBBE", "Pkg Top - CBBE"])
                if answer == "all":  # force a contaminated session: two cloth projects in one Outfit Studio run
                    from cbbe3bbb.conversion import batch as B

                    orig = B.plan_sessions
                    B.plan_sessions = lambda entries, plans, mode, *_: [list(entries)]
                    prev.select(["Pkg Shorts - CBBE", "Pkg Suit - CBBE"])
                    try:
                        _, _, _, rows = run(ctl, prev, cloth_answer="all")
                    finally:
                        B.plan_sessions = orig
                    self.assertEqual(rows["Pkg Suit - CBBE"]["status"], status)
                    self.assertIn("cloth physics data (BSClothExtraData) unchanged", rows["Pkg Suit - CBBE"]["failed_checks"])
                else:
                    _, _, _, rows = run(ctl, prev, cloth_answer="none")
                    self.assertEqual(rows["Pkg Shorts - CBBE"]["status"], status)
                    self.assertEqual(rows["Pkg Top - CBBE"]["status"], "VALIDATED")

    def test_session_planning(self):
        plans = {n: ConversionPlan(source_project=n, source_cloth_data=c) for n, c in
                 (("a", 0), ("b", 1), ("c", 0), ("d", 2), ("e", 0))}
        self.assertEqual(plan_sessions(list(plans), plans, "single"), [["a", "c", "e", "b"], ["d"]])
        self.assertEqual(plan_sessions(["a", "c"], plans, "single"), [["a", "c"]])
        self.assertEqual(plan_sessions(list(plans), plans, "per_project"), [[n] for n in plans])

    def test_interactive_closed_without_running(self):
        ctl, fi, _ = setup()
        prev = ctl.analyze_package(PACKAGE, write=False)
        bdir, summary, _, rows = run(ctl, prev, mode="closed_without_running")
        self.assertEqual(summary["counts"], {"FAILED": 3})
        self.assertEqual(ctl.commit_batch(bdir)["counts"], {"FAILED": 3})
        self.assertFalse(out_mod(fi).exists())

    def test_stale_changed_and_unknown_auto(self):
        ctl, fi, _ = setup(extra={"Someone Else": _unknown_auto})
        prev = ctl.analyze_package(PACKAGE, write=False)
        b = by_name(prev)
        self.assertEqual((b["Pkg Shorts - CBBE"].action, b["Pkg Shorts - CBBE"].status), ("REVIEW", "UNKNOWN_AUTO"))
        prev.select(["Pkg Top - CBBE", "Pkg Suit - CBBE"])
        bdir, _, _, _ = run(ctl, prev)
        ctl.commit_batch(bdir)
        # source changed after the conversion → STALE
        src = fi.mods / PACKAGE / BS / "SliderSets/Pkg Top.osp"
        src.write_text(src.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        # generated file edited after the conversion → AUTO_CHANGED
        gen = out_mod(fi) / BS / "ShapeData/Pkg Suit - CBBE - 3BBB AUTO/Pkg Suit - CBBE - 3BBB AUTO.nif"
        gen.write_bytes(gen.read_bytes() + b"\0")
        b = by_name(ctl.analyze_package(PACKAGE, write=False))
        self.assertEqual((b["Pkg Top - CBBE"].action, b["Pkg Top - CBBE"].status), ("REVIEW", "STALE"))
        self.assertEqual((b["Pkg Suit - CBBE"].action, b["Pkg Suit - CBBE"].status), ("REVIEW", "AUTO_CHANGED"))
        self.assertFalse(b["Pkg Top - CBBE"].selectable)

    def test_manifest_merge_keeps_phase2_history(self):
        ctl, fi, _ = setup(allowed=["Pkg Top - CBBE"])
        pr = next(p for p in ctl.analyze(write=False, workers=1).projects if p.project == "Pkg Top - CBBE")
        plan = ctl.plan_conversion(pr)
        self.assertTrue(plan.can_convert, plan.blockers)
        res = ctl.convert(plan, runner=FakeOutfitStudio("windows_interactive"))
        self.assertEqual(res.status, "SUCCESS", res.reasons)
        om = out_mod(fi)
        first = json.loads((om / "manifest.json").read_text())["conversions"][0]
        prev = ctl.analyze_package(PACKAGE, write=False)
        self.assertEqual(by_name(prev)["Pkg Top - CBBE"].status, "ALREADY_CONVERTED")
        self.assertEqual({p.project for p in prev.selected()}, {"Pkg Shorts - CBBE", "Pkg Suit - CBBE"})
        bdir, _, _, _ = run(ctl, prev)
        ctl.commit_batch(bdir)
        man = json.loads((om / "manifest.json").read_text())
        self.assertEqual(len(man["conversions"]), 3)
        self.assertEqual(man["conversions"][0], first, "the earlier (Phase 2) record must stay untouched")
        pkg_group = (om / BS / "SliderGroups" / f"CBBE 3BBB Auto Conversions - {PACKAGE}.xml").read_text()
        self.assertIn("Pkg Top - CBBE - 3BBB AUTO", pkg_group)  # built from the whole manifest history

    def test_commit_rechecks_hashes_and_recovery(self):
        ctl, fi, _ = setup()
        prev = ctl.analyze_package(PACKAGE, write=False)
        prev.select(["Pkg Top - CBBE", "Pkg Suit - CBBE"])
        bdir, _, _, _ = run(ctl, prev)
        # simulated crash: results and the copied session log are gone → validate-batch rebuilds them
        for p in (bdir / "projects").iterdir():
            (p / "result.json").unlink()
        (bdir / "logs" / "Log_OS.session1.txt").unlink()
        s = ctl.validate_batch(bdir)
        self.assertEqual(s["counts"], {"VALIDATED": 2}, s)
        # a generated file changed after validation → that project is not committed
        gen = bdir / "Data" / BS / "SliderSets/Pkg Top - CBBE - 3BBB AUTO.osp"
        gen.write_text(gen.read_text(encoding="utf-8") + " ", encoding="utf-8")
        os.utime(gen, (time.time() + 600, time.time() + 600))  # edited well after the Outfit Studio session
        s = ctl.commit_batch(bdir)
        rows = {p["project"]: p for p in s["projects"]}
        self.assertEqual(rows["Pkg Top - CBBE"]["status"], "VALIDATED")
        self.assertTrue(any("changed after validation" in e for e in rows["Pkg Top - CBBE"]["commit_error"]))
        self.assertEqual(rows["Pkg Suit - CBBE"]["status"], "COMMITTED")
        # validating again re-checks the edited file: now it fails and still is not committed
        s = ctl.validate_batch(bdir)
        rows = {p["project"]: p for p in s["projects"]}
        self.assertEqual(rows["Pkg Suit - CBBE"]["status"], "COMMITTED")
        self.assertEqual(rows["Pkg Top - CBBE"]["status"], "FAILED")  # edited after the session: evidence fails

    def test_create_refuses_non_convert_and_empty(self):
        ctl, fi, _ = setup()
        prev = ctl.analyze_package(PACKAGE, write=False)
        prev.select([])
        with self.assertRaises(BatchError):
            ctl.convert_batch(prev, runner=FakeOutfitStudio())
        b = by_name(prev)["Pkg Bra - CBBE"]
        b.selected = True  # forced past the selection rules
        with self.assertRaises(BatchError):
            ctl.convert_batch(prev, runner=FakeOutfitStudio())
        self.assertFalse(out_mod(fi).exists())

    def test_source_changed_after_preview(self):
        ctl, fi, _ = setup()
        prev = ctl.analyze_package(PACKAGE, write=False)
        prev.select(["Pkg Top - CBBE", "Pkg Suit - CBBE"])
        src = fi.mods / PACKAGE / BS / "SliderSets/Pkg Top.osp"
        src.write_text(src.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
        os.utime(src, (time.time() + 5, time.time() + 5))
        _, summary, _, rows = run(ctl, prev)
        self.assertEqual(rows["Pkg Top - CBBE"]["status"], "FAILED")
        self.assertTrue(any("changed since the preview" in r for r in rows["Pkg Top - CBBE"]["reasons"]))
        self.assertEqual(rows["Pkg Suit - CBBE"]["status"], "VALIDATED")


def _extra_tops(mods, body, body_nophys):
    from synth import CBBE_SLIDERS, _project, outfit_patch

    m = mods / "Extra Tops"
    for n in ("A", "B"):
        _project(m, f"Extra Top {n}.osp", [dict(name=f"Extra Top {n} - CBBE", folder=f"Extra Top {n}", source=f"Extra Top {n}.nif",
                                                shapes=[("TopShape", "TopShape")], sliders=CBBE_SLIDERS,
                                                output_path="meshes\\Extra", output_file=f"top{n}")],
                 {f"Extra Top {n}/Extra Top {n}.nif": [outfit_patch(body, (85, 115), name="TopShape")]})


def _unknown_auto(mods, body, body_nophys):
    p = mods / "Someone Else" / BS / "SliderSets" / "Pkg Shorts - CBBE - 3BBB AUTO.osp"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(osp_xml([dict(name="Pkg Shorts - CBBE - 3BBB AUTO", folder="x", source="x.nif", shapes=[("A", "A")])]),
                 encoding="utf-8")


class InvariantTests(unittest.TestCase):
    def _plan(self, name):
        ctl, fi, _ = setup()
        return ctl, by_name(ctl.analyze_package(PACKAGE, write=False))

    def test_batch_invariants(self):
        _, b = self._plan("x")
        p1 = b["Pkg Top - CBBE"].conversion_plan()
        p2 = b["Pkg Shorts - CBBE"].conversion_plan()
        from cbbe3bbb.conversion.batch import batch_script

        sc, ranges = batch_script([p1, p2], "t")
        self.assertEqual(ranges[0][0], 1)
        self.assertEqual(ranges[1][0], ranges[0][1] + 1)
        self.assertEqual(ranges[1][1], len(sc.steps))
        with self.assertRaises(A.ScriptInvariantError):
            batch_script([p1, p1], "dup")  # same output twice
        bad = A.AutomationScript("x", steps=build_script(p1).steps[1:])  # no ClearProject
        with self.assertRaises(A.ScriptInvariantError):
            A.check_batch_invariants(bad)
        steps = build_script(p2).steps
        next(s for s in steps if s.type == "SaveProject").params["AutoCopyRef"] = True
        with self.assertRaises(A.ScriptInvariantError):
            A.check_batch_invariants(A.AutomationScript("x", steps=build_script(p1).steps + steps))

    def test_project_status(self):
        ok = {"name": "valid OSP", "ok": True, "required": True}
        self.assertEqual(project_status([ok]), "VALIDATED")
        self.assertEqual(project_status([ok, {"name": "T: predicted vs actual physics weights consistent", "ok": False}]), "REVIEW")
        self.assertEqual(project_status([ok, {"name": "T: no physics bones outside planned regions", "ok": False}]), "FAILED")
        self.assertEqual(project_status([ok, {"name": "slider data location", "ok": False, "required": False}]), "VALIDATED")


class TopV1RegressionTests(unittest.TestCase):
    def test_accepted_top_v1_script_unchanged(self):
        """The plan of the real, in-game accepted Top V1 conversion (tool 0.2.1) rebuilds the identical script."""
        d = json.loads((DATA / "topv1_accepted_plan.json").read_text(encoding="utf-8"))
        plan = ConversionPlan.from_dict(d)
        self.assertEqual(build_script(plan).to_xml(), d["script_xml"])
        self.assertEqual(plan.shape_plans[0].regions, ["BREAST"])
        self.assertEqual(sorted(plan.physics_bones), sorted(f"{s}Breast_0{i}_skin" for s in "LR" for i in (1, 2, 3)))
        A.check_batch_invariants(d["script_xml"])  # a single project is a valid one-part batch


class BatchCliTests(unittest.TestCase):
    def test_cli_analyze_plan_convert_commit(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from cbbe3bbb.cli import main as cli_main
        from tests.test_app import _drop_handlers

        ctl, fi, home = setup()
        prof = ctl.installation.profile
        ctl.save_profile(prof)
        before = tree_digest(fi.mods / PACKAGE)

        def cli(*args):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli_main(["--home", str(ctl.paths.root), *args, "--profile", prof.name])
            _drop_handlers()
            return code, out.getvalue() + err.getvalue()

        code, out = cli("analyze-package", "--package", PACKAGE)
        self.assertEqual(code, 0, out)
        self.assertIn("CONVERT        3", out)
        self.assertIn("DRY RUN: nothing was converted", out)
        code, out = cli("plan-batch", "--package", PACKAGE, "--select", "Pkg Top - CBBE", "--details")
        self.assertEqual(code, 0, out)
        self.assertIn("Selected for conversion: 1", out)
        code, out = cli("plan-batch", "--package", PACKAGE, "--select", "Pkg Shirt - BT3")
        self.assertEqual(code, 3, out)
        code, out = cli("convert-batch", "--package", PACKAGE)
        self.assertEqual(code, 4, out)
        self.assertFalse((ctl.paths.root / "work").exists())
        self.assertFalse(out_mod(fi).exists())
        code, out = cli("batch-status")
        self.assertEqual(code, 0)
        self.assertEqual(tree_digest(fi.mods / PACKAGE), before)


if __name__ == "__main__":
    unittest.main()
