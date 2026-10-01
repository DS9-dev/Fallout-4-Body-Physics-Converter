"""0.5.3: unattended Outfit Studio sessions (conversion/os_control.py + batch integration).

The Outfit Studio window is modelled by :class:`fake_win_os.FakeWinOS` (the 5.8.2 dialog flow); the conversions
are the same fake Outfit Studio as in the other tests. Letters refer to the 0.5.3 test list (A–Y).
"""
import json
import os
import shutil
import unittest
from pathlib import Path

from tests._util import tree_digest
from cbbe3bbb.cli import main as cli_main
from cbbe3bbb.conversion import os_control
from cbbe3bbb.conversion.batch import BatchEngine
from cbbe3bbb.conversion.manifest import Manifest
from cbbe3bbb.conversion.os_control import (AUTOMATION_FAILED, COMPLETED, CLOTH_AMBIGUOUS_MESSAGE, SessionController,
                                            SessionProject, SessionSpec, decide_cloth, session_script_name,
                                            verify_session_script, write_last_script)
from cbbe3bbb.conversion.routes import RouteNotImplemented, resolve_route
from cbbe3bbb.nif.reader import read_nif
from fake_os import FakeOutfitStudio
from fake_win_os import FakeWinOS
from synth import BS, PACKAGE
from test_batch import out_mod
from test_mass import SAFE, batch_json, fake, mass_setup


def run_pkg(faults=None, mode="SAFE", fail_first_n=None, control_failure=None, conv=None, settings=None, **setup_kw):
    ctl, fi, home = mass_setup(**setup_kw)
    for k, v in (settings or {}).items():
        setattr(ctl.installation.profile.conversion, k, v)
    src_before = tree_digest(fi.mods / PACKAGE)
    drv = FakeWinOS(conv=conv or fake(), faults=faults, fail_first_n=fail_first_n)
    r = ctl.pipeline(PACKAGE, mode, runner=drv, control_failure=control_failure).run()
    return ctl, fi, drv, r, src_before


def items(r):
    return {i["project"]: i for i in r.items}


def session_spec(bdir: Path, si: int, rt_folder: str) -> SessionSpec:
    """Rebuild the SessionSpec the batch used for session ``si`` (for controller-level tests)."""
    b = batch_json(bdir)
    s = b["sessions"][si - 1]
    project = bdir / "Data" / BS
    sps = []
    for pid in s["projects"]:
        e = next(x for x in b["projects"] if x["id"] == pid)
        plan = BatchEngine._plan(bdir, e)
        src = next(sf for sf in plan.staged_files if sf.role == "source-nif")
        sps.append(SessionProject(id=pid, project=e["project"], generated_project=plan.output_project,
                                  step_range=tuple(s["ranges"][pid]),
                                  outputs={"x": str(project / plan.output_osp_rel)}, cloth_blocks=plan.source_cloth_data,
                                  source_nif=str(project / plan.source_shapedata_rel),
                                  source_nif_sha256=e["source_hashes"][src.abs_path]))
    return SessionSpec(batch_id=bdir.name, session=si, script_name=s["script"], script_sha256=s["script_sha256"],
                       expected_steps=s["steps"], projects=sps, project_dir=str(project), runtime_folder=rt_folder,
                       exe=str(Path(rt_folder) / "OutfitStudio x64.exe"), ready_timeout_s=2, timeout_s=20)


class UnattendedSessionTests(unittest.TestCase):
    def test_D_E_L_M_R_T_one_click_conversion_with_cloth(self):
        ctl, fi, drv, r, src_before = run_pkg()
        it = items(r)
        for n in SAFE:
            self.assertEqual(it[n]["outcome"], "CONVERTED", (n, it[n]["technical"]))
        b = batch_json(r.batch_dir)
        # D: every session unattended, completed, closed gracefully; nobody was asked to press anything
        self.assertTrue(b["sessions"])
        for s in b["sessions"]:
            self.assertEqual(s["control"], "unattended")
            self.assertEqual(s["result"]["status"], COMPLETED, s["result"])
            self.assertEqual(s["result"]["close"], "graceful")
        self.assertEqual(drv.launches, len(b["sessions"]))
        self.assertEqual(getattr(drv, "manual_runs", 0), 0)
        # A: exactly each session's own script ran, in order
        self.assertEqual(drv.executed_scripts, [s["script"] for s in b["sessions"]])
        self.assertEqual(drv.executed_scripts, [session_script_name(b["id"], i) for i in range(1, len(b["sessions"]) + 1)])
        # E: the completion handshake
        for s in b["sessions"]:
            sr = json.loads((Path(r.batch_dir) / "logs" / f"session_result.session{s['id']}.json").read_text())
            for k in ("batch_id", "session_id", "script_id", "started_at", "finished_at", "planned_projects",
                      "processed_projects", "save_attempts", "automation_errors", "status"):
                self.assertIn(k, sr)
            self.assertEqual(sr["batch_id"], b["id"])
            self.assertEqual(sr["script_sha256"], s["script_sha256"])
            self.assertEqual(sorted(sr["processed_projects"]), sorted(sr["planned_projects"]))
            self.assertTrue(sr["output_captured"])
            self.assertGreater(sr["finished_at"], sr["started_at"])
            self.assertIn("Automation: Step", (Path(r.batch_dir) / "logs" / f"automation_output.session{s['id']}.txt").read_text())
        # L: the single verified cloth origin was kept automatically (Shorts and Suit carry cloth data)
        man = json.loads((out_mod(fi) / "manifest.json").read_text())
        recs = {c["target"]["project"]: c for c in man["conversions"]}
        for p in ("Pkg Shorts - CBBE - 3BBB AUTO", "Pkg Suit - CBBE - 3BBB AUTO"):
            cx = recs[p]["cloth_extra_data"]
            self.assertTrue(cx["encountered"])
            self.assertEqual(cx["policy"], "single_verified_origin_preserved")
            self.assertIn("equals the source", cx["verification"])
            self.assertEqual(set(cx["validated"].values()), {True})
            self.assertEqual(recs[p]["outfit_studio_control"]["control"], "unattended")
        self.assertEqual(len(drv.cloth_dialogs), 2)
        self.assertTrue(all(len(o) == 1 for o in drv.cloth_dialogs))
        # M: the generated NIFs carry exactly the source's cloth data
        for proj in ("Pkg Shorts", "Pkg Suit"):
            gen = read_nif(out_mod(fi) / BS / "ShapeData" / f"{proj} - CBBE - 3BBB AUTO" / f"{proj} - CBBE - 3BBB AUTO.nif")
            src = read_nif(fi.mods / PACKAGE / BS / "ShapeData" / proj / f"{proj}.nif")
            self.assertTrue(src.cloth_data)
            self.assertEqual(sorted(gen.cloth_data), sorted(src.cloth_data))
        # R: the source mod is byte-for-byte unchanged
        self.assertEqual(tree_digest(fi.mods / PACKAGE), src_before)
        # T: the package-specific AUTO group lists every converted project
        grp = (out_mod(fi) / BS / "SliderGroups" / f"CBBE 3BBB Auto Conversions - {PACKAGE}.xml").read_text()
        for n in SAFE:
            self.assertIn(f"{n} - 3BBB AUTO", grp)
        # the normal handoff names only the package group and warns about the originals
        self.assertIn(f"Build only: CBBE 3BBB Auto Conversions - {PACKAGE}", r.build["instructions"])
        self.assertIn("Do not batch-build the original", r.build["warning"])
        # the private runtime is English + light theme and preselects the session script
        cfg = (Path(b["runtime"]["folder"]) / "Config.xml").read_text()
        self.assertIn("<Language>-1</Language>", cfg)
        self.assertIn("<AppearanceMode>Light</AppearanceMode>", cfg)

    def test_A_exact_script_selected_when_another_is_preselected(self):
        ctl, fi, drv, r, _ = run_pkg(faults={"preselect_other": True})
        b = batch_json(r.batch_dir)
        self.assertEqual(drv.executed_scripts, [s["script"] for s in b["sessions"]])
        self.assertTrue(all(i["outcome"] == "CONVERTED" for n, i in items(r).items() if n in SAFE))
        ev = json.loads((Path(r.batch_dir) / "logs" / "session_result.session1.json").read_text())["events"]
        self.assertIn("exact text selection", [e.get("via") for e in ev if e["kind"] == "script_selected"])

    def test_A_B_wrong_script_is_never_executed_and_control_stops(self):
        asked = []
        ctl, fi, drv, r, src_before = run_pkg(faults={"preselect_other": True, "combo_select_fails": True},
                                              control_failure=lambda info: asked.append(info) or "stop")
        self.assertEqual(drv.executed_scripts, [])
        self.assertEqual(len(asked), 1)
        self.assertIn("does not show the session script exactly", asked[0]["reason"])
        self.assertIn("File > Open Automation", asked[0]["fallback"])
        b = batch_json(r.batch_dir)
        self.assertEqual(b["state"], "INTERRUPTED")
        self.assertEqual(len(b["sessions"]), 2)  # retried once automatically, then asked
        self.assertTrue(all(s["result"]["status"] == AUTOMATION_FAILED for s in b["sessions"]))
        # nothing ran: no attempt was used, nothing failed, everything is resumable
        for e in b["projects"]:
            self.assertEqual(e["status"], "PENDING")
            self.assertEqual(e["attempts"], 0)
        self.assertEqual(r.state, "INTERRUPTED")
        self.assertEqual(tree_digest(fi.mods / PACKAGE), src_before)

    def test_B_stale_or_foreign_script_is_rejected_before_launch(self):
        ctl, fi, drv, r, _ = run_pkg()
        rt = batch_json(r.batch_dir)["runtime"]["folder"]
        spec = session_spec(Path(r.batch_dir), 1, rt)
        self.assertEqual(verify_session_script(spec), [])
        d2 = FakeWinOS(conv=fake())
        # changed after the batch wrote it
        p = spec.script_path
        text = p.read_text()
        p.write_text(text.replace("<MaxResults>4</MaxResults>", "<MaxResults>5</MaxResults>", 1))
        res = SessionController(d2, poll_s=0.01).run(spec)
        self.assertEqual(res.status, AUTOMATION_FAILED)
        self.assertIn("SHA-256 mismatch", res.reason)
        self.assertEqual(d2.launches, 0)
        p.write_text(text)
        # another session's name / a project count that does not match the plan
        other = SessionSpec(**{**spec.__dict__, "session": spec.session + 1})
        self.assertTrue(any("not this batch/session's name" in x for x in verify_session_script(other)))
        fewer = SessionSpec(**{**spec.__dict__, "projects": spec.projects[:-1]})
        self.assertTrue(any("session planned" in x for x in verify_session_script(fewer)))
        steps = SessionSpec(**{**spec.__dict__, "expected_steps": spec.expected_steps + 1})
        self.assertTrue(any("steps" in x for x in verify_session_script(steps)))
        self.assertEqual(d2.launches, 0)

    def test_C_readiness_is_required_before_execution(self):
        ctl, fi, drv, r, _ = run_pkg(faults={"no_ready": True}, control_failure=lambda i: "stop",
                                     settings={"unattended_ready_timeout_s": 1})
        self.assertEqual(drv.executed_scripts, [])
        self.assertNotIn("Confirm Execution", [t for t, _ in drv.pressed])
        sr = json.loads((Path(r.batch_dir) / "logs" / "session_result.session1.json").read_text())
        self.assertEqual(sr["status"], AUTOMATION_FAILED)
        self.assertIn("did not become ready", sr["reason"])
        self.assertIsNone(sr["ready_at"])

    def test_F_I_crash_before_execution_then_resume(self):
        ctl, fi, drv, r, src_before = run_pkg(faults={"crash_before_execute": True}, control_failure=lambda i: "stop")
        b = batch_json(r.batch_dir)
        self.assertEqual(b["state"], "INTERRUPTED")
        self.assertTrue(all(e["status"] == "PENDING" and e["attempts"] == 0 for e in b["projects"]))
        # I: resume (Outfit Studio works again): continues with the unfinished projects, nothing repeated twice
        drv.faults.clear()
        r2 = ctl.pipeline("", runner=drv).resume(r.batch_id)
        for n in SAFE:
            self.assertEqual(items(r2)[n]["outcome"], "CONVERTED", items(r2)[n]["technical"])
        b = batch_json(r.batch_dir)
        self.assertTrue(all(e["attempts"] == 1 for e in b["projects"]))
        self.assertEqual(tree_digest(fi.mods / PACKAGE), src_before)

    def test_G_J_K_crash_mid_session_isolates_the_project(self):
        ctl, fi, drv, r, src_before = run_pkg(faults={"Pkg Shorts - CBBE": "crash"}, fail_first_n=1)
        b = batch_json(r.batch_dir)
        s1 = b["sessions"][0]
        self.assertEqual(s1["result"]["status"], "OS_CRASHED")
        self.assertEqual(s1["result"]["suspect_project"], "Pkg Shorts - CBBE")
        iso = [s for s in b["sessions"] if s.get("isolated")]
        self.assertEqual(len(iso), 1)
        pid = next(e["id"] for e in b["projects"] if e["project"] == "Pkg Shorts - CBBE")
        self.assertEqual(iso[0]["projects"], [pid])
        it = items(r)
        for n in SAFE:
            self.assertEqual(it[n]["outcome"], "CONVERTED", (n, it[n]["technical"]))
        # J: projects saved before the crash were validated in session 1 and never run again
        done_first = [e for e in b["projects"] if e["id"] in s1["projects"] and e["project"] != "Pkg Shorts - CBBE"]
        self.assertTrue(done_first)
        for e in done_first:
            self.assertEqual(e["attempts"], 1)
            self.assertEqual(sum(e["id"] in s["projects"] for s in b["sessions"]), 1)
        self.assertEqual(next(e for e in b["projects"] if e["id"] == pid)["attempts"], 2)
        self.assertEqual(tree_digest(fi.mods / PACKAGE), src_before)

    def test_H_timeout_isolates_the_hanging_project(self):
        ctl, fi, _ = mass_setup()
        ctl.installation.profile.conversion.unattended_stall_timeout_s = 1
        drv = FakeWinOS(conv=fake(), faults={"Pkg Top - CBBE": "hang"}, fail_first_n=1)
        r = ctl.pipeline(PACKAGE, "SAFE", runner=drv).run()
        b = batch_json(r.batch_dir)
        self.assertEqual(b["sessions"][0]["result"]["status"], "TIMEOUT")
        self.assertEqual(b["sessions"][0]["result"]["suspect_project"], "Pkg Top - CBBE")
        self.assertEqual(b["sessions"][0]["result"]["close"], "forced")
        self.assertEqual(items(r)["Pkg Top - CBBE"]["outcome"], "CONVERTED")

    def test_K_failing_step_fails_only_that_project(self):
        ctl, fi, drv, r, _ = run_pkg(faults={"Pkg Top - CBBE": "step_error"})
        it = items(r)
        self.assertEqual(it["Pkg Top - CBBE"]["outcome"], "FAILED")
        self.assertTrue(it["Pkg Top - CBBE"]["attention"])
        for n in SAFE - {"Pkg Top - CBBE"}:
            self.assertEqual(it[n]["outcome"], "CONVERTED", it[n]["technical"])
        s1 = batch_json(r.batch_dir)["sessions"][0]["result"]
        self.assertEqual(s1["status"], "COMPLETED_WITH_PROJECT_ERRORS")
        self.assertEqual([x["project"] for x in s1["automation_errors"]], ["Pkg Top - CBBE"])
        self.assertIn(("Automation Error", os_control.IDYES), drv.pressed)
        self.assertEqual(it["Pkg Top - CBBE"]["code"], "OS_STEP_ERROR")  # saved after "Yes", but never accepted
        self.assertTrue((Path(r.batch_dir) / "Data" / BS / "SliderSets" / "Pkg Top - CBBE - 3BBB AUTO.osp").exists())
        self.assertFalse((out_mod(fi) / BS / "SliderSets" / "Pkg Top - CBBE - 3BBB AUTO.osp").exists())

    def test_confirm_execution_real_hierarchy_and_strict_identity(self):
        """Real Windows (batch 20261001-002408): the task dialog's buttons all have control ID 0; identification is by
        exact title + exact button-text set, and the answer goes through the wx-defined task-dialog ID."""
        ctl, fi, drv, r, _ = run_pkg()
        self.assertIn(("Confirm Execution", os_control.IDYES), drv.pressed)
        self.assertIn(("Automation", os_control.IDCANCEL), drv.pressed)  # wx OK-only box = one IDCANCEL button "OK"
        sr = json.loads((Path(r.batch_dir) / "logs" / "session_result.session1.json").read_text())
        conf = next(d for d in sr["dialogs"] if d["title"] == "Confirm Execution")
        self.assertEqual((conf["answer"], conf["buttons"]), ("Yes", ["No", "Yes"]))
        self.assertEqual(os_control.KNOWN_BOXES["Automation"], (("OK",), "OK", os_control.IDCANCEL))

    def test_confirm_with_unexpected_buttons_is_never_pressed(self):
        class Odd(FakeWinOS):  # e.g. a third button, or a renamed one: not the dialog we verified
            def _modal(self, title, buttons, **kw):
                if title == "Confirm Execution":
                    buttons = {os_control.IDYES: "Yes", os_control.IDNO: "No", os_control.IDCANCEL: "Cancel"}
                return super()._modal(title, buttons, **kw)

        ctl, fi, _ = mass_setup()
        drv = Odd(conv=fake())
        r = ctl.pipeline(PACKAGE, "SAFE", runner=drv, control_failure=lambda i: "stop").run()
        self.assertEqual(drv.executed_scripts, [])
        self.assertEqual(getattr(drv, "posted_ids", []), [])  # nothing was pressed at all
        s1 = batch_json(r.batch_dir)["sessions"][0]["result"]
        self.assertEqual(s1["status"], AUTOMATION_FAILED)
        self.assertIn("unexpected buttons in 'Confirm Execution'", s1["reason"])

    def test_wrong_task_button_id_is_detected_not_assumed(self):
        """If a box ignored the pressed ID (e.g. IDOK sent to wx's OK-only box), it stays open → stop, never 'done'."""
        saved = os_control.KNOWN_BOXES["Automation"]
        os_control.KNOWN_BOXES["Automation"] = (("OK",), "OK", os_control.IDOK)
        try:
            ctl, fi, drv, r, _ = run_pkg(control_failure=lambda i: "stop")
        finally:
            os_control.KNOWN_BOXES["Automation"] = saved
        s1 = batch_json(r.batch_dir)["sessions"][0]["result"]
        self.assertEqual(s1["status"], AUTOMATION_FAILED)
        self.assertIn("did not close", s1["reason"])
        self.assertNotIn(("Automation", os_control.IDOK), drv.pressed)

    def test_failure_before_execute_never_loops(self):
        class StartupDialog(FakeWinOS):
            def launch(self, exe, cwd):
                p = super().launch(exe, cwd)
                self._modal("Some warning", {os_control.IDCANCEL: "OK"})
                return p

        ctl, fi, _ = mass_setup()
        drv = StartupDialog(conv=fake())
        asked = []
        r = ctl.pipeline(PACKAGE, "SAFE", runner=drv, control_failure=lambda i: asked.append(i) or "stop").run()
        self.assertEqual(drv.launches, 2)
        self.assertEqual(len(asked), 1)
        self.assertEqual(batch_json(r.batch_dir)["state"], "INTERRUPTED")
        self.assertTrue(all(e["attempts"] == 0 and e["status"] == "PENDING" for e in batch_json(r.batch_dir)["projects"]))

    def test_controller_error_never_leaves_outfit_studio_running(self):
        class Broken(FakeWinOS):
            def listview_count(self, hwnd):
                raise OSError("boom")

        ctl, fi, _ = mass_setup()
        drv = Broken(conv=fake())
        r = ctl.pipeline(PACKAGE, "SAFE", runner=drv, control_failure=lambda i: "stop").run()
        s1 = batch_json(r.batch_dir)["sessions"][0]["result"]
        self.assertEqual(s1["status"], AUTOMATION_FAILED)
        self.assertIn("session control error", s1["reason"])
        self.assertIsNotNone(drv.proc.exit_code)  # the process was ended
        self.assertEqual(drv.executed_scripts, [])

    def test_N_O_ambiguous_cloth_origins_are_not_guessed(self):
        ctl, fi, drv, r, _ = run_pkg(faults={"Pkg Shorts - CBBE": "extra_cloth_origin"})
        it = items(r)
        sh = it["Pkg Shorts - CBBE"]
        self.assertEqual(sh["outcome"], "FAILED")
        self.assertEqual(sh["code"], "CLOTH_AMBIGUOUS")
        self.assertEqual(sh["message"], CLOTH_AMBIGUOUS_MESSAGE)
        self.assertIn("Pkg Shorts - CBBE", "\n".join(r.attention_lines()))
        self.assertFalse((out_mod(fi) / BS / "SliderSets" / "Pkg Shorts - CBBE - 3BBB AUTO.osp").exists())
        self.assertIn(("Choose cloth data", 5101), drv.pressed)  # Cancel: no cloth data written, nothing guessed
        # O: the rest of the package continued
        for n in SAFE - {"Pkg Shorts - CBBE"}:
            self.assertEqual(it[n]["outcome"], "CONVERTED", it[n]["technical"])

    def test_equivalent_cloth_origins_keep_only_the_projects_own(self):
        ctl, fi, drv, r, _ = run_pkg(faults={"Pkg Shorts - CBBE": "equivalent_cloth_origin"})
        self.assertEqual(items(r)["Pkg Shorts - CBBE"]["outcome"], "CONVERTED")
        rec = next(c for c in json.loads((out_mod(fi) / "manifest.json").read_text())["conversions"]
                   if c["target"]["project"] == "Pkg Shorts - CBBE - 3BBB AUTO")
        self.assertEqual(rec["cloth_extra_data"]["policy"], "equivalent_origins_own_selected")
        self.assertEqual(len(rec["cloth_extra_data"]["origins"]), 2)

    def test_cloth_decision_rules(self):
        ctl, fi, drv, r, _ = run_pkg()
        rt = batch_json(r.batch_dir)["runtime"]["folder"]
        spec = session_spec(Path(r.batch_dir), 1, rt)
        cloth = next(p for p in spec.projects if p.cloth_blocks)
        plain = next(p for p in spec.projects if not p.cloth_blocks)
        self.assertEqual(decide_cloth([cloth.source_nif], spec, cloth).policy, "single_verified_origin_preserved")
        d = decide_cloth([cloth.source_nif], spec, plain)  # D: a project without cloth data of its own
        self.assertEqual((d.policy, d.action), ("unexpected", "stop"))
        d = decide_cloth(["C:/elsewhere/x.nif"], spec, cloth)  # D: outside the workspace
        self.assertEqual(d.action, "stop")
        self.assertEqual(decide_cloth([], spec, cloth).action, "stop")
        self.assertEqual(decide_cloth([cloth.source_nif], spec, None).action, "stop")
        bad = SessionProject(**{**cloth.__dict__, "source_nif_sha256": "0" * 64})  # the staged copy does not verify
        self.assertEqual(decide_cloth([cloth.source_nif], spec, bad).action, "stop")

    def test_P_unknown_dialog_is_not_answered_blindly(self):
        ctl, fi, drv, r, _ = run_pkg(faults={"Pkg Top - CBBE": "unknown_dialog"})
        self.assertNotIn("Warning", [t for t, _ in drv.pressed])
        b = batch_json(r.batch_dir)
        s1 = b["sessions"][0]["result"]
        self.assertEqual(s1["status"], "USER_INTERACTION_REQUIRED")
        self.assertEqual(s1["suspect_project"], "Pkg Top - CBBE")
        sr = json.loads((Path(r.batch_dir) / "logs" / "session_result.session1.json").read_text())
        cap = [d for d in sr["dialogs"] if d.get("capture")]
        self.assertEqual(cap[0]["title"], "Warning")
        self.assertIn("buttons", cap[0]["capture"])
        it = items(r)
        self.assertEqual(it["Pkg Top - CBBE"]["outcome"], "FAILED")
        self.assertEqual(it["Pkg Top - CBBE"]["code"], "OS_UNEXPECTED_MESSAGE")
        for n in SAFE - {"Pkg Top - CBBE"}:
            self.assertEqual(it[n]["outcome"], "CONVERTED", it[n]["technical"])
        top = next(e for e in b["projects"] if e["project"] == "Pkg Top - CBBE")
        self.assertEqual(top["attempts"], 2)  # isolated once, then failed on its own

    def test_Q_manual_fallback_continues_the_session(self):
        ctl, fi, drv, r, _ = run_pkg(faults={"menu_missing": True}, control_failure=lambda i: "manual")
        self.assertGreaterEqual(getattr(drv, "manual_runs", 0), 1)
        b = batch_json(r.batch_dir)
        self.assertIn("manual", [s["control"] for s in b["sessions"]])
        for n in SAFE:
            self.assertEqual(items(r)[n]["outcome"], "CONVERTED", items(r)[n]["technical"])

    def test_Q_cli_resume_manual_and_unattended(self):
        ctl, fi, drv, r, _ = run_pkg(faults={"menu_missing": True}, control_failure=lambda i: "stop")
        b = batch_json(r.batch_dir)
        self.assertEqual(b["state"], "INTERRUPTED")
        drv.faults.clear()
        eng = ctl._batch_engine(drv)
        eng.resume(r.batch_dir, control="manual")
        b = batch_json(r.batch_dir)
        self.assertEqual(b["control_override"], "manual")
        self.assertEqual(b["sessions"][-1]["control"], "manual")
        # the CLI offers the same choice
        rc = cli_main(["--home", str(ctl.paths.root), "resume-batch", "--mo2", str(fi.mo2_dir),
                       "--batch", r.batch_id, "--manual"])
        self.assertEqual(rc, 4)  # --yes is still required

    def test_cancel_mid_session_is_resumable(self):
        ctl, fi, _ = mass_setup()
        drv = FakeWinOS(conv=fake(), faults={"Pkg Top - CBBE": "hang"})
        calls = {"n": 0}
        orig = drv.top_windows

        def cancel_soon(pid):
            calls["n"] += 1
            if calls["n"] == 200:
                ctl.cancel()
            return orig(pid)

        drv.top_windows = cancel_soon
        r = ctl.pipeline(PACKAGE, "SAFE", runner=drv).run()
        b = batch_json(r.batch_dir)
        self.assertEqual(b["state"], "INTERRUPTED")
        self.assertEqual(b["sessions"][0]["result"]["status"], "CANCELLED")
        drv.faults.clear()
        drv.top_windows = orig
        r2 = ctl.pipeline("", runner=drv).resume(r.batch_id)
        for n in SAFE:
            self.assertEqual(items(r2)[n]["outcome"], "CONVERTED")

    def test_unsaved_changes_prompt_and_refused_close(self):
        ctl, fi, drv, r, _ = run_pkg(faults={"unsaved_prompt": True})
        self.assertIn(("Unsaved Changes", os_control.IDNO), drv.pressed)
        self.assertEqual(batch_json(r.batch_dir)["sessions"][0]["result"]["close"], "graceful")
        ctl, fi, drv, r, _ = run_pkg(faults={"refuse_close": True}, settings={"unattended_close_timeout_s": 1})
        s1 = batch_json(r.batch_dir)["sessions"][0]["result"]
        self.assertEqual(s1["status"], COMPLETED)  # the work was done and verified; only the close was forced
        self.assertEqual(s1["close"], "forced")
        self.assertTrue(all(items(r)[n]["outcome"] == "CONVERTED" for n in SAFE))


class ProbeTests(unittest.TestCase):
    def test_probe_opens_and_closes_without_executing(self):
        ctl, fi, _ = mass_setup()
        drv = FakeWinOS(conv=fake())
        res = ctl.probe_outfit_studio(driver=drv)
        self.assertEqual(res["status"], COMPLETED, res)
        self.assertEqual(drv.executed_scripts, [])
        self.assertEqual(res["close"], "graceful")
        self.assertTrue(res["completion"]["output_pane"])
        self.assertTrue(Path(res["report"]).is_file())
        drv = FakeWinOS(conv=fake(), faults={"menu_missing": True})
        res = ctl.probe_outfit_studio(driver=drv)
        self.assertEqual(res["status"], AUTOMATION_FAILED)
        self.assertIn("Open Automation", res["reason"])


class ValidationUnchangedTests(unittest.TestCase):
    def test_S_thresholds_and_rules_unchanged(self):
        from cbbe3bbb.conversion import renormalize

        src = Path(__file__).resolve().parents[1] / "src" / "cbbe3bbb" / "conversion" / "validate.py"
        text = src.read_text(encoding="utf-8")
        for rule in ("same_topo and dev < 1e-3", "bool(agreements) and min(agreements) >= 0.9", "mean_err < 0.005",
                     "float(np.abs(sums - 1).max()) < 0.05", "sorted(nif.cloth_data) == sorted(src_cloth)"):
            self.assertIn(rule, text)
        self.assertEqual(renormalize.SUM_TOLERANCE, 0.002)
        self.assertEqual(renormalize.MIN_REPAIRABLE_SUM, 0.5)

    def test_S_automation_success_is_not_conversion_success(self):
        ctl, fi, drv, r, _ = run_pkg(conv=fake(per_project={"Pkg Top - CBBE": "no_weights"}))
        s1 = batch_json(r.batch_dir)["sessions"][0]["result"]
        self.assertEqual(s1["status"], COMPLETED)  # Outfit Studio finished without any error
        self.assertNotEqual(items(r)["Pkg Top - CBBE"]["outcome"], "CONVERTED")  # but the result is not accepted
        self.assertFalse((out_mod(fi) / BS / "SliderSets" / "Pkg Top - CBBE - 3BBB AUTO.osp").exists())


class BuildAndCompatibilityTests(unittest.TestCase):
    def test_U_V_stale_build_detected_then_auto_group_build_ok(self):
        ctl, fi, drv, r, _ = run_pkg()
        om = out_mod(fi)
        meshes = {"Pkg Top - CBBE": "top", "Pkg Shorts - CBBE": "shorts", "Pkg Catsuit - CBBE": "catsuit",
                  "Pkg Suit - CBBE": "suit"}
        rec = json.loads((om / "manifest.json").read_text())["conversions"][-1]
        import time

        old = time.mktime(time.strptime(rec["timestamp"], "%Y-%m-%dT%H:%M:%S")) - 3600
        for proj, mesh in meshes.items():  # a build made before the conversions: the originals' old meshes
            gen = om / BS / "ShapeData" / f"{proj} - 3BBB AUTO" / f"{proj} - 3BBB AUTO.nif"
            dst = fi.mods / PACKAGE / "meshes" / "Pkg" / f"{mesh}.nif"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(gen, dst)
            os.utime(dst, (old, old))
        ctl.detect(ctl.installation.profile)
        v = ctl.verify_package_build(PACKAGE)
        self.assertGreaterEqual(v["counts"].get("BUILD_STALE", 0), 1, v["rows"])
        self.assertFalse(v["ok"])
        text = "\n".join(v["explanation"])
        self.assertIn("have not been built from the generated 3BBB projects yet, or the original BodySlide projects "
                      "were built afterward", text)
        self.assertIn(f"CBBE 3BBB Auto Conversions - {PACKAGE}", text)
        for proj, mesh in meshes.items():  # now the AUTO group is built
            os.utime(fi.mods / PACKAGE / "meshes" / "Pkg" / f"{mesh}.nif", None)
        v = ctl.verify_package_build(PACKAGE)
        self.assertEqual(v["counts"].get("BUILD_OK"), 4, v["rows"])
        self.assertTrue(v["ok"])

    def test_W_X_Y_older_manifests_and_batches_stay_readable(self):
        # a 0.5.2-style run (manual Outfit Studio, no control / route / cloth records)
        ctl, fi, _ = mass_setup()
        r = ctl.pipeline(PACKAGE, "SAFE", runner=fake()).run()
        bdir = r.batch_dir
        b = batch_json(bdir)
        for s in b["sessions"]:
            for k in ("control", "script_sha256", "steps", "isolated", "result"):
                s.pop(k, None)
        b.pop("route", None)
        (Path(bdir) / "batch.json").write_text(json.dumps(b), encoding="utf-8")
        om = out_mod(fi)
        man = json.loads((om / "manifest.json").read_text())
        for c in man["conversions"]:
            for k in ("route", "outfit_studio_control", "cloth_extra_data"):
                c.pop(k, None)
        (om / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
        self.assertTrue(Manifest.load(om).records)
        summary = ctl.validate_batch(r.batch_id)  # W/X: the old batch revalidates with the current code
        self.assertEqual(summary["counts"].get("COMMITTED"), 4)
        res = ctl.pipeline(PACKAGE, "SAFE").result(ctl.preview, Path(bdir))
        self.assertEqual(res.counts.get("CONVERTED"), 4)
        # Y: runtime issue reporting still works
        ctl.record_runtime_issue("Pkg Top - CBBE", "FAIL", "no physics in game")
        res = ctl.pipeline(PACKAGE, "SAFE").result(ctl.preview, Path(bdir))
        self.assertIn("Pkg Top - CBBE", "\n".join(res.attention_lines()))

    def test_retry_in_a_later_session_is_judged_by_that_session(self):
        """The real Wardrobe 10 case (sessions 7→8): the first session saved nothing, the retry saved everything."""
        ctl, fi, _ = mass_setup()
        calls = []
        ok = fake()
        closed = fake(mode="closed_without_running")

        def runner(rf, exe, name, t, cancel=None, interactive=False):
            calls.append(name)
            return (closed if len(calls) == 1 else ok)(rf, exe, name, t, cancel, interactive)

        r = ctl.pipeline(PACKAGE, "SAFE", runner=runner).run()
        b = batch_json(r.batch_dir)
        self.assertEqual(b["sessions"][0]["failure"], "SESSION_FAILURE")
        for n in SAFE:
            self.assertEqual(items(r)[n]["outcome"], "CONVERTED", items(r)[n]["technical"])

    def test_routes(self):
        self.assertEqual(resolve_route(None).id, "cbbe-to-cbbe3bbb")
        self.assertEqual(resolve_route("auto").source_body_family, "CBBE")
        with self.assertRaises(RouteNotImplemented):
            resolve_route("fusiongirl-to-cbbe3bbb")
        ctl, fi, _ = mass_setup()
        with self.assertRaises(RouteNotImplemented):
            ctl.pipeline(PACKAGE, "SAFE", route="fusiongirl-to-cbbe3bbb")
        r = ctl.pipeline(PACKAGE, "SAFE", runner=fake()).run()
        rec = json.loads((out_mod(fi) / "manifest.json").read_text())["conversions"][0]
        self.assertEqual(rec["route"]["source_body_family"], "CBBE")
        self.assertEqual(rec["route"]["target_body_family"], "CBBE_3BBB")

    def test_last_script_written_without_losing_settings(self):
        from tests._util import temp_home

        home = temp_home()
        (home / "OutfitStudio.xml").write_text('<OutfitStudioConfig><OutfitStudioFrame width="1700"/>'
                                                '<AutomationDialog lastScript="old"/></OutfitStudioConfig>')
        write_last_script(home, "cbbe3bbb_batch_X_3")
        text = (home / "OutfitStudio.xml").read_text()
        self.assertIn('lastScript="cbbe3bbb_batch_X_3"', text)
        self.assertIn('width="1700"', text)


if __name__ == "__main__":
    unittest.main()
