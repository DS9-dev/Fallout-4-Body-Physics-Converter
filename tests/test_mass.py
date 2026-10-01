"""Phase 3.2: mass conversion (Parts C–N, P–R.1).

Deterministic safe selection, Convert All Possible, bounded sessions with cloth isolation, fault isolation
(project / session / system failures), checkpoint + resume, partial commit, output mod enabled in MO2, SliderGroups,
package build check, representative items, human-readable problems with item names, diagnostics export, CLI.
"""
import json
import shutil
import unittest
import zipfile
from pathlib import Path

import numpy as np

from tests._util import temp_home, tree_digest
from cbbe3bbb.app.messages import MESSAGES, humanize
from cbbe3bbb.app.pipeline import analysis_overview, problem_lines, representative_items, result_text
from cbbe3bbb.cli import main as cli_main
from cbbe3bbb.conversion.batch import BatchError, plan_sessions
from cbbe3bbb.conversion.outcomes import (applicable_percent, display_name, outcome_for, package_result,
                                          preview_bucket)
from cbbe3bbb.conversion.plan import ConversionPlan
from fake_os import FakeOutfitStudio
from synth import BS, CBBE_SLIDERS, PACKAGE, _project, bodysuit_package, outfit_patch
from test_batch import by_name, out_mod, setup

SAFE = {"Pkg Top - CBBE", "Pkg Shorts - CBBE", "Pkg Suit - CBBE", "Pkg Catsuit - CBBE"}


def _extra(mods, body, body_nophys):
    bodysuit_package(mods, body, body_nophys)
    # attemptable REVIEW projects: outerwear (garment risk) and a loose top (MEDIUM fit)
    _project(mods / PACKAGE, "Pkg Jacket.osp", [dict(name="Pkg Jacket - CBBE", folder="Pkg Jacket", source="Pkg Jacket.nif",
                                                     shapes=[("JacketShape", "JacketShape")], sliders=CBBE_SLIDERS,
                                                     output_path="meshes\\Pkg", output_file="jacket")],
             {"Pkg Jacket/Pkg Jacket.nif": [outfit_patch(body, (85, 115), name="JacketShape")]})
    _project(mods / PACKAGE, "Pkg Loose.osp", [dict(name="Pkg Loose - CBBE", folder="Pkg Loose", source="Pkg Loose.nif",
                                                    shapes=[("LooseTop", "LooseTop")], sliders=CBBE_SLIDERS,
                                                    output_path="meshes\\Pkg", output_file="loose")],
             {"Pkg Loose/Pkg Loose.nif": [outfit_patch(body, (85, 115), offset=3.0, name="LooseTop")]})


def mass_setup(**kw):
    return setup(extra={"__mass": _extra}, **kw)


def fake(**kw):
    return FakeOutfitStudio(kw.pop("mode", "windows_interactive"), source_slider_data=True, **kw)


def batch_json(bdir):
    return json.loads((Path(bdir) / "batch.json").read_text(encoding="utf-8"))


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, _ = mass_setup()
        cls.prev = cls.ctl.analyze_package(PACKAGE, write=False)

    def test_safe_selection_is_convert_high_only(self):
        sel = {p.project for p in self.prev.select_mode("SAFE")}
        self.assertEqual(sel, SAFE)
        b = by_name(self.prev)
        for name, excluded in (("Pkg Jacket - CBBE", "REVIEW attemptable"), ("Pkg Loose - CBBE", "REVIEW (MEDIUM fit)"),
                               ("Pkg Bodysuit - CBBE", "REVIEW blocked (embedded body)"),
                               ("Pkg Bra - CBBE", "REVIEW blocked (manual 3BBB variant)"),
                               ("Pkg Bracelet - CBBE", "SKIP"), ("Pkg Shirt - BT3", "UNSUPPORTED"),
                               ("Pkg Broken - CBBE", "ERROR"), ("Pkg Bra - 3BBB TEST", "KEEP")):
            self.assertFalse(b[name].selected, excluded)

    def test_medium_excluded_from_safe_included_in_all_possible(self):
        b = by_name(self.prev)
        top = b["Pkg Top - CBBE"]
        saved = (top.attempt_origin, top.confidence)
        try:
            top.attempt_origin, top.confidence = "MEDIUM", "MEDIUM"  # a CONVERT project below HIGH confidence
            self.assertNotIn("Pkg Top - CBBE", {p.project for p in self.prev.select_mode("SAFE")})
            self.assertIn("Pkg Top - CBBE", {p.project for p in self.prev.select_mode("ALL_POSSIBLE")})
        finally:
            top.attempt_origin, top.confidence = saved

    def test_review_policy(self):
        b = by_name(self.prev)
        self.assertEqual(b["Pkg Jacket - CBBE"].review_policy, "REVIEW_ATTEMPTABLE")
        self.assertEqual(b["Pkg Loose - CBBE"].review_policy, "REVIEW_ATTEMPTABLE")
        self.assertEqual(b["Pkg Bodysuit - CBBE"].review_policy, "REVIEW_BLOCKED")
        self.assertEqual(b["Pkg Bra - CBBE"].review_policy, "REVIEW_BLOCKED")
        self.assertIsNotNone(b["Pkg Jacket - CBBE"].plan)
        allp = {p.project for p in self.prev.select_mode("ALL_POSSIBLE")}
        self.assertEqual(allp, SAFE | {"Pkg Jacket - CBBE", "Pkg Loose - CBBE"})
        self.assertNotIn("Pkg Bodysuit - CBBE", allp)  # REVIEW_BLOCKED is never forced through
        self.assertNotIn("Pkg Bra - CBBE", allp)
        self.prev.select_mode("SAFE")

    def test_select_refuses_blocked_even_in_all_possible(self):
        self.prev.select_mode("ALL_POSSIBLE")
        problems = self.prev.select(["Pkg Bodysuit - CBBE", "Pkg Jacket - CBBE"])
        self.assertTrue(any("Pkg Bodysuit" in p for p in problems))
        self.assertEqual({p.project for p in self.prev.selected()}, {"Pkg Jacket - CBBE"})
        self.prev.select_mode("SAFE")

    def test_overview_counts(self):
        ov = analysis_overview(self.prev)
        self.assertEqual((ov["total"], ov["ready"], ov["try_all_possible"], ov["already_compatible"],
                          ov["no_conversion_needed"], ov["cannot_process"]), (12, 4, 2, 1, 1, 4))

    def test_buckets(self):
        b = by_name(self.prev)
        self.assertEqual(b["Pkg Top - CBBE"].bucket, "SAFE_AUTO_DONE")
        self.assertEqual(b["Pkg Bra - CBBE"].bucket, "EXISTING_3BBB_AMBIGUOUS")
        self.assertEqual(b["Pkg Bracelet - CBBE"].bucket, "NO_PHYSICS_NEEDED")
        self.assertEqual(b["Pkg Broken - CBBE"].bucket, "FAILED_TECHNICAL")
        self.assertEqual(preview_bucket("REVIEW", "BLOCKED", ["x: 196 vertices have no reference vertex within radius 5"]),
                         "BLOCKED_WEIGHT_TRANSFER")
        self.assertEqual(preview_bucket("CONVERT", "NEW", [], "MEDIUM"), "MEDIUM_NOT_AUTO")


class SessionPlanningTests(unittest.TestCase):
    def test_bounded_deterministic_cloth_last(self):
        plans = {n: ConversionPlan(source_project=n, source_cloth_data=c) for n, c in
                 (("a", 0), ("b", 1), ("c", 0), ("d", 2), ("e", 0), ("f", 0), ("g", 0))}
        order = list(plans)
        self.assertEqual(plan_sessions(order, plans, "single", 2), [["a", "c"], ["e", "f"], ["g", "b"], ["d"]])
        self.assertEqual(plan_sessions(order, plans, "single", 3), [["a", "c", "e"], ["f", "g", "b"], ["d"]])
        self.assertEqual(plan_sessions(order, plans, "single", 5), [["a", "c", "e", "f", "g"], ["b"], ["d"]])
        self.assertEqual(plan_sessions(order, plans, "single", 0), [["a", "c", "e", "f", "g", "b"], ["d"]])
        self.assertEqual(plan_sessions(order, plans, "single", 2), plan_sessions(order, plans, "single", 2))
        for g in plan_sessions(order, plans, "single", 2):  # at most one cloth project, and it is last
            cl = [x for x in g if plans[x].source_cloth_data]
            self.assertLessEqual(len(cl), 1)
            if cl:
                self.assertEqual(g[-1], cl[0])


class PipelineTests(unittest.TestCase):
    def test_safe_run_commits_only_validated_and_names_problems(self):
        ctl, fi, _ = mass_setup()
        src_before = tree_digest(fi.mods / PACKAGE)
        ctl.installation.profile.conversion.max_projects_per_session = 2
        evs = []
        runner = fake()
        r = ctl.pipeline(PACKAGE, "SAFE", events=evs.append, runner=runner).run()
        self.assertEqual(r.state, "COMPLETE_WITH_WARNINGS")
        self.assertEqual(r.counts.get("CONVERTED"), 4)
        self.assertEqual(r.counts.get("SKIPPED"), 2)  # attemptable REVIEW projects are not tried in Safe mode
        b = batch_json(r.batch_dir)
        self.assertEqual(b["state"], "COMMITTED")
        self.assertEqual(len(b["sessions"]), 3)  # 2 clean; each cloth project alone, last (bounded + cloth isolation)
        self.assertEqual([len(s["projects"]) for s in b["sessions"]], [2, 1, 1])
        self.assertEqual(len(runner.calls), 3)
        # problems carry names and human messages; never 'agreement n/a' style text first
        probs = {i["name"]: i for i in r.problems}
        self.assertEqual(set(probs), {"Pkg Bodysuit - CBBE", "Pkg Bra - CBBE", "Pkg Broken - CBBE", "Pkg Shirt - BT3"})
        self.assertEqual(probs["Pkg Bodysuit - CBBE"]["message"], MESSAGES["EMBEDDED_BODY"])
        self.assertEqual(problem_lines(r, 2)[-1], "+ 2 more…")
        txt = result_text(r)
        self.assertIn("Original versions of these outfits were left unchanged.", txt)
        self.assertIn("• Pkg Bodysuit - CBBE", txt)
        # SliderGroup: only the committed AUTO projects
        grp = (out_mod(fi) / BS / "SliderGroups" / f"CBBE 3BBB Auto Conversions - {PACKAGE}.xml").read_text()
        for n in SAFE:
            self.assertIn(f"{n} - 3BBB AUTO", grp)
        for n in ("Pkg Jacket - CBBE", "Pkg Loose - CBBE", "Pkg Bodysuit - CBBE", "Pkg Bra - CBBE"):
            self.assertNotIn(f"{n} - 3BBB AUTO", grp)
        self.assertEqual(tree_digest(fi.mods / PACKAGE), src_before, "source mod must stay byte-for-byte unchanged")
        # progress events: normal level stays short, every item result is available at detailed level
        normal = [e for e in evs if e["level"] == "NORMAL"]
        self.assertTrue(any(e["kind"] == "finished" for e in normal))
        self.assertEqual(sum(1 for e in evs if e["kind"] == "item_ok"), 4)
        # idempotent: a second run converts nothing new
        prev2 = ctl.analyze_package(PACKAGE, write=False)
        self.assertEqual({p.project for p in prev2.select_mode("SAFE")}, set())
        for n in SAFE:
            self.assertEqual((by_name(prev2)[n].action, by_name(prev2)[n].status), ("KEEP", "ALREADY_CONVERTED"))
        r2 = ctl.pipeline(PACKAGE, "SAFE", runner=fake()).run(prev2)
        self.assertEqual(r2.counts.get("ALREADY_COMPATIBLE"), 5)
        self.assertEqual(r2.batch_id, "")

    def test_all_possible_failed_attempt_is_not_committed_and_batch_continues(self):
        ctl, fi, _ = mass_setup()
        r = ctl.pipeline(PACKAGE, "ALL_POSSIBLE", runner=fake(per_project={"Pkg Jacket - CBBE": "no_weights"})).run()
        items = {i["project"]: i for i in r.items}
        self.assertIn(items["Pkg Jacket - CBBE"]["outcome"], ("COULD_NOT_CONVERT_SAFELY", "FAILED"))
        self.assertFalse((out_mod(fi) / BS / "SliderSets" / "Pkg Jacket - CBBE - 3BBB AUTO.osp").exists())
        for n in SAFE:
            self.assertEqual(items[n]["outcome"], "CONVERTED", items[n])
        self.assertIn(items["Pkg Loose - CBBE"]["batch_status"], ("COMMITTED", "REVIEW", "FAILED"))
        if items["Pkg Loose - CBBE"]["batch_status"] == "COMMITTED":
            self.assertEqual(items["Pkg Loose - CBBE"]["origin"], "REVIEW")

    def test_output_mod_disabled_warning(self):
        ctl, fi, _ = mass_setup()
        ml = fi.mo2_dir / "profiles" / "Default" / "modlist.txt"
        ml.write_text(ml.read_text().replace("+CBBE 3BBB Auto Conversions", "-CBBE 3BBB Auto Conversions"))
        (out_mod(fi)).mkdir(parents=True, exist_ok=True)
        ctl.detect(ctl.installation.profile)
        prev = ctl.analyze_package(PACKAGE, write=False)
        self.assertEqual(prev.output_mod_state, "DISABLED_IN_MO2")
        self.assertIn("OUTPUT_MOD_DISABLED_IN_MO2", prev.codes)
        r = ctl.pipeline(PACKAGE, "SAFE", runner=fake()).run(prev)
        self.assertEqual(r.warnings[0]["code"], "OUTPUT_MOD_DISABLED_IN_MO2")
        self.assertIn('Enable "CBBE 3BBB Auto Conversions"', r.warnings[0]["message"])


class FaultIsolationTests(unittest.TestCase):
    def test_session_crash_keeps_done_projects_and_retries_the_rest(self):
        ctl, fi, _ = mass_setup()
        ctl.installation.profile.conversion.max_projects_per_session = 10
        calls = []

        class Crash:
            """First session: saves the first project, then crashes. Later sessions work."""

            def __init__(self):
                self.ok = fake()

            def __call__(self, rf, exe, name, t, cancel=None, interactive=False):
                calls.append(name)
                out = self.ok(rf, exe, name, t, cancel, interactive)
                if len(calls) == 1:
                    b = sorted((ctl.paths.root / "work" / "batch_conversions").iterdir())[-1]
                    s = batch_json(b)["sessions"][0]
                    proj = b / "Data" / BS
                    for pid in s["projects"][1:]:  # only the first project survived the crash
                        e = next(x for x in batch_json(b)["projects"] if x["id"] == pid)
                        for f in (proj / "SliderSets" / f"{e['generated_project']}.osp",):
                            f.unlink(missing_ok=True)
                    out.crashed = "Fatal exception has occurred"
                return out

        r = ctl.pipeline(PACKAGE, "SAFE", runner=Crash()).run()
        b = batch_json(r.batch_dir)
        self.assertEqual(b["sessions"][0]["failure"], "SESSION_FAILURE")
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(r.counts.get("CONVERTED"), 4, r.problems)
        self.assertTrue(any(e.get("attempts", 0) == 2 for e in b["projects"]))
        self.assertEqual(max(e.get("attempts", 0) for e in b["projects"]), 2)

    def test_session_failure_exhausts_retries_then_fails_projects(self):
        ctl, fi, _ = mass_setup()
        runner = fake(mode="closed_without_running")
        r = ctl.pipeline(PACKAGE, "SAFE", runner=runner).run()
        self.assertEqual(r.counts.get("FAILED"), 5)  # 4 safe + Broken (analysis error)
        self.assertEqual(len(runner.calls), 4)  # 2 sessions (cloth), each tried once more
        self.assertFalse((out_mod(fi) / BS / "SliderSets").exists())
        self.assertTrue(all(i["message"] == MESSAGES["NOT_CONVERTED_BY_OS"] for i in r.problems
                            if i["project"] in SAFE))

    def test_system_failure_source_changed_stops_and_resume_continues(self):
        ctl, fi, _ = mass_setup()
        ctl.installation.profile.conversion.max_projects_per_session = 1
        ok = fake()
        n = []

        def runner(rf, exe, name, t, cancel=None, interactive=False):
            n.append(name)
            out = ok(rf, exe, name, t, cancel, interactive)
            if len(n) == 2:  # the source of a later project changes during the conversion
                src = fi.mods / PACKAGE / BS / "SliderSets/Pkg Suit.osp"
                src.write_text(src.read_text(encoding="utf-8") + " ", encoding="utf-8")
                src.touch()
            return out

        r = ctl.pipeline(PACKAGE, "SAFE", runner=runner).run()
        self.assertEqual(r.state, "SYSTEM_ERROR")
        self.assertIn("source mod changed", r.system_failure)
        self.assertEqual(r.counts.get("CONVERTED"), 2)  # installed before the stop, and kept
        b = batch_json(r.batch_dir)
        self.assertEqual(b["state"], "SYSTEM_ERROR")
        self.assertEqual(sorted(e["status"] for e in b["projects"]), ["COMMITTED", "COMMITTED", "PENDING", "PENDING"])
        # resume without fixing: stops again, nothing is run
        r2 = ctl.pipeline("", runner=runner).resume(r.batch_id)
        self.assertEqual(r2.state, "SYSTEM_ERROR")
        self.assertEqual(len(n), 2)

    def test_tool_interrupted_resume_does_not_repeat_finished_projects(self):
        ctl, fi, _ = mass_setup()
        ctl.installation.profile.conversion.max_projects_per_session = 2
        runner = fake()
        seen = []

        def stop_after_first(ev):
            seen.append(ev)
            if ev["kind"] == "session_done" and ev["session"] == 1:
                ctl._cancel.set()

        pl = ctl.pipeline(PACKAGE, "SAFE", runner=runner)
        prev = pl.analyze()
        prev.select_mode("SAFE")
        eng = ctl._batch_engine(runner)
        bdir = eng.create(prev)
        s = eng.run(bdir, cancel=ctl._cancel.is_set, events=stop_after_first)
        self.assertEqual(s["state"], "INTERRUPTED")
        b = batch_json(bdir)
        self.assertEqual(sorted(e["status"] for e in b["projects"]), ["PENDING", "PENDING", "VALIDATED", "VALIDATED"])
        # partial commit, then resume in the one-click pipeline
        ctl.commit_batch(bdir)
        self.assertEqual(batch_json(bdir)["state"], "PARTIALLY_COMMITTED")
        calls_before = len(runner.calls)
        r = ctl.pipeline("", runner=runner).resume(bdir.name)
        self.assertEqual(len(runner.calls), calls_before + 2)  # only the unfinished projects (clean + cloth session)
        self.assertEqual(r.counts.get("CONVERTED"), 4)
        self.assertEqual(batch_json(bdir)["state"], "COMMITTED")
        ran = [pid for sess in batch_json(bdir)["sessions"] for pid in sess["projects"]]
        self.assertEqual(len(ran), len(set(ran)), "no project converted twice")

    def test_resume_after_tool_crash(self):
        ctl, fi, _ = mass_setup()
        prev = ctl.analyze_package(PACKAGE, write=False)
        prev.select_mode("SAFE")
        runner = fake()
        eng = ctl._batch_engine(runner)
        bdir = eng.create(prev)

        class Killed(BaseException):
            pass

        def die(ev):  # the converter itself stops right after Outfit Studio saved the first session
            if ev["kind"] == "validating":
                raise Killed()

        with self.assertRaises(Killed):
            eng.run(bdir, events=die)
        b = batch_json(bdir)
        self.assertEqual(b["state"], "RUNNING")
        self.assertEqual(sorted(e["status"] for e in b["projects"]), ["GENERATED", "GENERATED", "GENERATED", "PENDING"])
        calls = len(runner.calls)
        s = ctl._batch_engine(runner).resume(bdir)
        self.assertEqual(s["counts"], {"VALIDATED": 4})
        self.assertEqual(len(runner.calls), calls + 1)  # saved projects are validated, not converted again
        with self.assertRaises(BatchError):
            ctl._batch_engine(runner).resume(bdir)


class OutcomeTests(unittest.TestCase):
    def test_outcomes_and_result(self):
        self.assertEqual(outcome_for("CONVERT", "NEW", "COMMITTED"), "CONVERTED")
        self.assertEqual(outcome_for("CONVERT", "NEW", "REVIEW"), "COULD_NOT_CONVERT_SAFELY")
        self.assertEqual(outcome_for("CONVERT", "NEW", "PENDING"), "UNFINISHED")
        self.assertEqual(outcome_for("CONVERT", "NEW"), "SKIPPED")
        self.assertEqual(outcome_for("KEEP", "ALREADY_CONVERTED"), "ALREADY_COMPATIBLE")
        self.assertEqual(outcome_for("SKIP", "NO_PHYSICS_REGION"), "NO_CONVERSION_NEEDED")
        self.assertEqual(outcome_for("REVIEW", "BLOCKED"), "COULD_NOT_CONVERT_SAFELY")
        self.assertEqual(package_result(["CONVERTED", "ALREADY_COMPATIBLE"]), "COMPLETE")
        self.assertEqual(package_result(["CONVERTED", "FAILED"]), "COMPLETE_WITH_WARNINGS")
        self.assertEqual(package_result(["CONVERTED", "UNFINISHED"]), "INTERRUPTED")
        self.assertEqual(package_result(["CONVERTED"], "SYSTEM_ERROR"), "SYSTEM_ERROR")
        self.assertEqual(applicable_percent(["CONVERTED"] * 3 + ["FAILED", "NO_CONVERSION_NEEDED"]), 75)

    def test_weight_sum_after_4_influence_truncation_is_review_not_failed(self):
        from cbbe3bbb.conversion.batch import project_status

        low = {"name": "top_v9: weights normalized", "ok": False,
               "detail": "weight sum range 0.946..1.000; 29 vertices off by >1% (Fallout 4 stores at most 4 influences per vertex)"}
        none = dict(low, detail=low["detail"] + "; 3 vertices WITHOUT any weight: 1, 2, 3")
        other = {"name": "top_v9: geometry unchanged", "ok": False, "detail": "x"}
        self.assertEqual(project_status([low]), "REVIEW")
        self.assertEqual(project_status([none]), "FAILED")
        self.assertEqual(project_status([low, other]), "FAILED")
        self.assertEqual(humanize("COULD_NOT_CONVERT_SAFELY", ["validation failed: " + low["name"] + " — " + low["detail"]])[0],
                         "WEIGHTS_INCOMPLETE")

    def test_display_name_priority(self):
        self.assertEqual(display_name("CP5 top v1", ["Summer Top"], "meshes\\x\\top.nif"), "Summer Top")
        self.assertEqual(display_name("CP5 top v1", [], "meshes\\x\\top.nif"), "CP5 top v1")
        self.assertEqual(display_name("", [], "meshes\\x\\top"), "top")

    def test_human_messages(self):
        self.assertEqual(humanize("COULD_NOT_CONVERT_SAFELY", [
            "validation failed: x: predicted vs actual physics weights consistent — agreement n/a (≥ 0.90 required)"])[1],
            "Could not verify the converted physics.")
        self.assertEqual(humanize("COULD_NOT_CONVERT_SAFELY", [
            "validation failed: analyzer classifies generated project as 3BBB KEEP — SKIP / CBBE 3BBB: project contains "
            "only a body/reference shape (no outfit geometry)"])[1], "The converted outfit geometry could not be verified.")
        self.assertEqual(humanize("COULD_NOT_CONVERT_SAFELY", ["x: 196 vertices have no reference vertex within radius 5"])[1],
                         "Automatic physics weights could not be safely transferred to part of this outfit.")
        self.assertEqual(humanize("CONVERTED", ["whatever"]), ("", ""))
        for code, msg in MESSAGES.items():  # no engineering vocabulary in normal messages
            for word in ("OSP", "NIF", "agreement", "radius", "SHA", "CopyBoneWeights", "manifest"):
                self.assertNotIn(word, msg, code)

    def test_representative_items(self):
        items = [dict(project=f"p{i}", name=f"n{i}", mesh="", outcome="CONVERTED", batch_status="COMMITTED",
                      regions=r, shapes=s, cloth=c, origin=o)
                 for i, (r, s, c, o) in enumerate([(["BREAST"], 1, False, "SAFE"), (["BUTT"], 1, False, "SAFE"),
                                                   (["THIGH"], 1, False, "SAFE"), (["BREAST", "BUTT", "THIGH"], 1, False, "SAFE"),
                                                   (["BREAST", "BUTT"], 2, False, "SAFE"), (["BUTT", "THIGH"], 1, True, "SAFE"),
                                                   (["BREAST"], 1, False, "REVIEW"), (["BREAST"], 1, False, "SAFE"),
                                                   (["BREAST"], 1, False, "SAFE")])]
        rep = representative_items(items)
        self.assertTrue(5 <= len(rep) <= 8)
        whys = {x["why"] for x in rep}
        self.assertTrue({"breast physics only", "full-length outfit (breast, butt and thigh)",
                         "outfit with cloth physics data", "converted by Convert All Possible (uncertain fit)"} <= whys)
        self.assertEqual(len({x["project"] for x in rep}), len(rep))


class ValidationStaysStrictTests(unittest.TestCase):
    """A generated project that only holds the reference, or whose outfit is analysed as a body, never validates."""

    def _one(self, tamper):
        ctl, fi, _ = mass_setup()
        prev = ctl.analyze_package(PACKAGE, write=False)
        prev.select(["Pkg Catsuit - CBBE"])
        bdir, s = ctl.convert_batch(prev, runner=fake())
        self.assertEqual(s["counts"], {"VALIDATED": 1})
        tamper(bdir / "Data" / BS)
        return ctl.validate_batch(bdir)["projects"][0]

    def test_outfit_analysed_as_body_cannot_validate(self):
        def body_folder(proj):
            f = proj / "SliderSets" / "Pkg Catsuit - CBBE - 3BBB AUTO.osp"
            f.write_text(f.read_text(encoding="utf-8").replace('DataFolder="Pkg Catsuit"', 'DataFolder="CBBE"'),
                         encoding="utf-8")
        row = self._one(body_folder)
        self.assertNotEqual(row["status"], "VALIDATED")
        self.assertTrue(any("agreement n/a" in r for r in row["reasons"]), row["reasons"])

    def test_reference_only_project_cannot_validate(self):
        from cbbe3bbb.nif.reader import read_nif
        from synth import BONE_POS, SynthShape, write_nif

        def ref_only(proj):
            ref = read_nif(proj / "ShapeData" / "CBBE 3BBB Body" / "CBBE 3BBB Body.nif").shapes[0] \
                if (proj / "ShapeData" / "CBBE 3BBB Body" / "CBBE 3BBB Body.nif").is_file() else None
            gen = proj / "ShapeData" / "Pkg Catsuit - CBBE - 3BBB AUTO" / "Pkg Catsuit - CBBE - 3BBB AUTO.nif"
            src = next((proj / "ShapeData").rglob("*.nif"))
            shp = ref or read_nif(src).shapes[0]
            bones, w = shp.weight_matrix()
            order = np.argsort(-w, axis=1)[:, :4]
            ww = np.take_along_axis(w, order, 1)
            write_nif(gen, [SynthShape("CBBE-3BBB", shp.world_vertices(), shp.triangles, bones, ww.astype(np.float32),
                                       order.astype(np.uint8))], BONE_POS)
        row = self._one(ref_only)
        self.assertEqual(row["status"], "FAILED")


class VerifyAndDiagnosticsTests(unittest.TestCase):
    def test_package_build_aggregate_and_diagnostics(self):
        ctl, fi, home = mass_setup()
        r = ctl.pipeline(PACKAGE, "SAFE", runner=fake()).run()
        v = ctl.verify_package_build(PACKAGE)
        self.assertEqual(v["committed"], 4)
        self.assertEqual(v["counts"], {"NOT_BUILT": 4})
        self.assertFalse(v["ok"])
        # simulate the BodySlide build of three of them (the built mesh keeps the generated weights)
        om = out_mod(fi)
        for proj, mesh in (("Pkg Top - CBBE", "top"), ("Pkg Shorts - CBBE", "shorts"), ("Pkg Catsuit - CBBE", "catsuit")):
            gen = om / BS / "ShapeData" / f"{proj} - 3BBB AUTO" / f"{proj} - 3BBB AUTO.nif"
            dst = fi.mods / PACKAGE / "meshes" / "Pkg" / f"{mesh}.nif"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(gen, dst)
        ctl.detect(ctl.installation.profile)
        v = ctl.verify_package_build(PACKAGE)
        self.assertEqual(v["counts"], {"BUILD_OK": 3, "NOT_BUILT": 1}, v["rows"])
        self.assertTrue(all(g["exists"] and not g["missing"] for g in v["groups"].values()))
        self.assertEqual(v["output_mod_state"], "ENABLED")
        rep = ctl.write_package_report(r, v)
        text = rep["txt"].read_text(encoding="utf-8")
        for b in ("SAFE_AUTO_DONE", "EXISTING_3BBB_AMBIGUOUS", "NO_PHYSICS_NEEDED", "FAILED_TECHNICAL"):
            self.assertIn(b, text)
        self.assertIn("Test these in game", text)
        z = ctl.export_diagnostics(result=r, preview=ctl.preview)
        with zipfile.ZipFile(z) as zz:
            names = zz.namelist()
            self.assertIn("about.json", names)
            self.assertIn("package_classification.json", names)
            blob = "".join(zz.read(n).decode("utf-8", "replace") for n in names)
        self.assertNotIn(str(fi.mo2_dir), blob)
        self.assertNotIn(str(ctl.paths.root), blob)


class NeedsAttentionTests(unittest.TestCase):
    """0.5.2: every outfit without the requested physics stays visible by name; a usable original is not a success;
    in-game problems reported by the user are listed without changing the automated validation result."""

    def test_attention_list_names_skipped_blocked_failed_and_runtime_reports(self):
        ctl, fi, home = mass_setup()
        r = ctl.pipeline(PACKAGE, "SAFE", runner=fake()).run()
        attn = {i["project"]: i for i in r.needs_attention}
        # blocked / failed / unsupported / not tried in Safe mode: all listed, none counted as completed
        for n in ("Pkg Bodysuit - CBBE", "Pkg Bra - CBBE", "Pkg Broken - CBBE", "Pkg Shirt - BT3", "Pkg Jacket - CBBE",
                  "Pkg Loose - CBBE"):
            self.assertIn(n, attn)
            self.assertTrue(attn[n]["short"])
        self.assertEqual(attn["Pkg Jacket - CBBE"]["short"], "not tried in Safe mode")
        completed = {i["project"] for i in r.completed}
        self.assertEqual(completed, SAFE | {"Pkg Bra - 3BBB TEST"})
        self.assertFalse(completed & set(attn))
        txt = result_text(r)
        self.assertIn(f"{'Completed:':<30}{len(completed):>5}", txt)
        self.assertIn("Needs attention / original kept:", txt)
        self.assertIn("- Pkg Bodysuit - CBBE — contains its own body (needs manual conversion)", txt)
        # an in-game report on a validated conversion: listed, not completed; validator result unchanged
        man_before = json.loads((out_mod(fi) / "manifest.json").read_text())
        rec_before = next(c for c in man_before["conversions"] if c["target"]["project"] == "Pkg Shorts - CBBE - 3BBB AUTO")
        d = ctl.record_runtime_issue("Pkg Shorts - CBBE", "FAIL", "no physics on the thighs")
        self.assertEqual(d["generated_project"], "Pkg Shorts - CBBE - 3BBB AUTO")
        r2 = ctl.pipeline(PACKAGE, "SAFE").result(ctl.preview, Path(r.batch_dir))
        it = next(i for i in r2.items if i["project"] == "Pkg Shorts - CBBE")
        self.assertEqual(it["outcome"], "CONVERTED")  # automated result unchanged
        self.assertTrue(it["attention"])
        self.assertEqual(it["short"], "converted, but no physics in game (reported)")
        self.assertIn("Pkg Shorts - CBBE", {i["project"] for i in r2.needs_attention})
        self.assertNotIn("Pkg Shorts - CBBE", {i["project"] for i in r2.completed})
        rec = next(c for c in json.loads((out_mod(fi) / "manifest.json").read_text())["conversions"]
                   if c["target"]["project"] == "Pkg Shorts - CBBE - 3BBB AUTO")
        self.assertEqual(rec["validation"], rec_before["validation"])
        self.assertEqual(rec["validation_result"], rec_before["validation_result"])
        self.assertEqual(rec["acceptance"]["state"], "FAIL")
        # later analysis still sees it as already converted (KEEP), and it stays listed
        prev2 = ctl.analyze_package(PACKAGE, write=False)
        r3 = ctl.pipeline(PACKAGE, "SAFE", runner=fake()).run(prev2)
        it3 = next(i for i in r3.items if i["project"] == "Pkg Shorts - CBBE")
        self.assertEqual((it3["outcome"], it3["attention"]), ("ALREADY_COMPATIBLE", True))
        # report and diagnostics carry it
        text = ctl.write_package_report(r3)["txt"].read_text(encoding="utf-8")
        self.assertIn("In-game verification reports", text)
        self.assertIn("no physics on the thighs", text)
        z = ctl.export_diagnostics(result=r3)
        with zipfile.ZipFile(z) as zz:
            self.assertIn("runtime_issues.json", zz.namelist())
        # clearing the report
        ctl.record_runtime_issue("Pkg Shorts - CBBE - 3BBB AUTO", "PASS")
        r4 = ctl.pipeline(PACKAGE, "SAFE").result(prev2, None)
        self.assertIn("Pkg Shorts - CBBE", {i["project"] for i in r4.completed})
        with self.assertRaises(KeyError):
            ctl.record_runtime_issue("Pkg Jacket - CBBE", "FAIL")  # never converted: nothing to report on
        # CLI
        self.assertEqual(cli_main(["--home", str(home / "tool"), "report-runtime-issue", "--mo2", str(fi.mo2_dir),
                                   "--project", "Pkg Top - CBBE", "--state", "PARTIAL", "--note", "clips"]), 0)


    def test_earlier_failed_attempt_stays_visible(self):
        ctl, fi, home = mass_setup()
        ctl.pipeline(PACKAGE, "ALL_POSSIBLE", runner=fake(per_project={"Pkg Jacket - CBBE": "no_weights"})).run()
        prev = ctl.analyze_package(PACKAGE, write=False)
        r = ctl.pipeline(PACKAGE, "SAFE").result(prev, None)  # a fresh view without a batch
        it = next(i for i in r.items if i["project"] == "Pkg Jacket - CBBE")
        self.assertIn(it["outcome"], ("COULD_NOT_CONVERT_SAFELY", "FAILED"))
        self.assertTrue(it["attention"])
        self.assertNotEqual(it["short"], "not tried in Safe mode")
        self.assertTrue(it["technical"][0].startswith("earlier attempt in batch"))


class CliTests(unittest.TestCase):
    def test_convert_package_cli(self):
        ctl, fi, home = mass_setup()
        import cbbe3bbb.app.controller as C

        orig = C.AppController._batch_engine
        C.AppController._batch_engine = lambda self, runner=None: orig(self, runner or fake())
        try:
            base = ["--home", str(home / "tool"), "convert-package", "--mo2", str(fi.mo2_dir), "--package", PACKAGE]
            # without a usable Outfit Studio the whole package stops (SYSTEM_ERROR), no outfit "fails"
            self.assertEqual(cli_main(base + ["--yes"]), 5)
            self.assertFalse(out_mod(fi).exists())
            args = base + ["--os-dir", str(home / "os")]
            self.assertEqual(cli_main(args), 4)  # overview only without --yes
            self.assertFalse(out_mod(fi).exists())
            self.assertEqual(cli_main(args + ["--yes", "--max-per-session", "3", "--level", "normal"]), 0)
            self.assertTrue((out_mod(fi) / "manifest.json").is_file())
            bid = sorted((home / "tool" / "work" / "batch_conversions").iterdir())[-1].name
            self.assertEqual(cli_main(["--home", str(home / "tool"), "package-report", "--mo2", str(fi.mo2_dir),
                                       "--batch", bid]), 0)
            self.assertEqual(cli_main(["--home", str(home / "tool"), "export-diagnostics", "--mo2", str(fi.mo2_dir),
                                       "--batch", bid]), 0)
            self.assertEqual(cli_main(["--home", str(home / "tool"), "verify-package-build", "--mo2", str(fi.mo2_dir),
                                       "--package", PACKAGE]), 5)  # nothing built yet
        finally:
            C.AppController._batch_engine = orig


if __name__ == "__main__":
    unittest.main()
