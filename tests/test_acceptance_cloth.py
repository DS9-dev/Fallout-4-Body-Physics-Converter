"""0.5.3 targeted acceptance: ``acceptance-test-cloth`` (app/acceptance.py) and the cloth-dialog evidence it reports.

One already converted + committed cloth project ("Pkg Shorts - CBBE") is converted again from its ORIGINAL source,
unattended, into a disposable output; nothing is installed and the normal "already compatible" detection is unchanged.
"""
import json
import unittest
from pathlib import Path

from tests._util import tree_digest
from cbbe3bbb.app.acceptance import ACCEPTED_TEXT, AcceptanceError, report_lines, run_cloth_acceptance
from cbbe3bbb.conversion.batch import BatchError
from fake_win_os import FakeWinOS
from synth import PACKAGE
from test_batch import out_mod
from test_mass import fake
from test_unattended import items, run_pkg

CLOTH = "Pkg Shorts - CBBE"


class ClothAcceptanceTests(unittest.TestCase):
    def setUp(self):
        # a normal one-click run first: Pkg Shorts is converted and committed (later: "already compatible")
        self.ctl, self.fi, _drv, r, _ = run_pkg()
        self.assertEqual(items(r)[CLOTH]["outcome"], "CONVERTED")
        self.prod_before = tree_digest(out_mod(self.fi))
        self.src_before = tree_digest(self.fi.mods / PACKAGE)

    def accept(self, faults=None, **kw):
        drv = FakeWinOS(conv=fake(), faults=faults)
        return drv, run_cloth_acceptance(self.ctl, PACKAGE, CLOTH, runner=drv, **kw)

    def test_committed_cloth_project_runs_again_into_disposable_output(self):
        drv, rep = self.accept()
        self.assertTrue(rep["passed"], "\n".join(report_lines(rep)))
        self.assertEqual(rep["verdict"], ACCEPTED_TEXT)
        # the real dialog evidence: title, hierarchy, origins, selection control, buttons, closed
        d = rep["dialog"]
        self.assertEqual(d["title"], "Choose cloth data")
        self.assertEqual({(b["id"], b["text"]) for b in d["buttons"]}, {(5100, "OK"), (5101, "Cancel")})
        self.assertEqual(len(d["selection_controls"]), 1)
        self.assertEqual(d["selection_controls"][0]["class"].lower(), "listbox")
        self.assertTrue(any("BSClothExtraData" in (c["text"] or "") for c in d["controls"]))
        self.assertTrue(rep["dialog_closed"])
        self.assertTrue(rep["zero_user_interaction"])
        self.assertEqual(drv.pressed.count(("Choose cloth data", 5100)) if hasattr(drv, "pressed") else 1, 1)
        cx = rep["cloth_extra_data"]
        self.assertEqual(cx["offered_origins"], 1)
        self.assertEqual(cx["selection_policy"], "single_verified_origin_preserved")
        self.assertTrue(cx["selected_origin"].endswith(".nif"))
        self.assertEqual(cx["source_payload_size"], cx["generated_payload_size"])
        self.assertEqual(cx["source_payload_sha256"], cx["generated_payload_sha256"])
        self.assertEqual(len(cx["source_payload_sha256"][0]), 64)
        self.assertTrue(cx["payload_match"])
        self.assertEqual(rep["project_status"], "VALIDATED")
        self.assertTrue(rep["sources_unchanged"])
        self.assertTrue(all(s["sha256_before"] == s["sha256_after"] for s in rep["source_files"]))
        # nothing reached the production output mod; the source mod is byte-for-byte unchanged
        self.assertTrue(rep["production_output_mod"]["untouched"])
        self.assertEqual(tree_digest(out_mod(self.fi)), self.prod_before)
        self.assertEqual(tree_digest(self.fi.mods / PACKAGE), self.src_before)
        # the result.json of the project records the payloads too (manifest/report material)
        b = json.loads((Path(rep["batch"]) / "batch.json").read_text())
        self.assertTrue(b["acceptance"]["no_commit"])
        res = json.loads((Path(rep["batch"]) / "projects" / b["projects"][0]["id"] / "result.json").read_text())
        rcx = res["run"]["cloth_extra_data"]
        self.assertTrue(rcx["payload_match"])
        self.assertEqual(rcx["offered_origins"], 1)
        self.assertIn("closed_after_confirmation", rcx["dialog"])
        # forced no-commit: an acceptance batch can never be installed
        with self.assertRaises(BatchError):
            self.ctl.commit_batch(rep["batch"])
        self.assertEqual(tree_digest(out_mod(self.fi)), self.prod_before)
        # the disposable output only holds the report
        self.assertEqual([p.name for p in Path(rep["disposable_output"]).iterdir()], ["acceptance_cloth.json"])
        # "already compatible" detection is unchanged for normal runs
        prev = self.ctl.analyze_package(PACKAGE, write=False)
        pp = next(p for p in prev.projects if p.project == CLOTH)
        self.assertEqual(pp.status, "ALREADY_CONVERTED")

    def test_altered_payload_never_passes(self):
        _drv, rep = self.accept(faults={CLOTH: "cloth_payload_altered"})
        self.assertFalse(rep["passed"])
        self.assertFalse(rep["cloth_extra_data"]["payload_match"])
        self.assertNotEqual(rep["project_status"], "VALIDATED")
        self.assertEqual(tree_digest(out_mod(self.fi)), self.prod_before)

    def test_relabelled_ok_button_fails_safely(self):
        drv, rep = self.accept(faults={CLOTH: "cloth_ok_relabelled"})
        self.assertFalse(rep["passed"])
        self.assertNotIn(("Choose cloth data", 5100), getattr(drv, "pressed", []))
        self.assertEqual(rep["cloth_extra_data"]["answer"], "stop")
        self.assertTrue(rep["dialog"]["buttons"])  # the real hierarchy is still reported
        self.assertEqual(tree_digest(out_mod(self.fi)), self.prod_before)

    def test_refusals(self):
        with self.assertRaises(AcceptanceError):  # no cloth data → not a cloth fixture
            run_cloth_acceptance(self.ctl, PACKAGE, "Pkg Top - CBBE", runner=FakeWinOS(conv=fake()))
        with self.assertRaises(AcceptanceError):  # not exact
            run_cloth_acceptance(self.ctl, PACKAGE, "Pkg Shorts", runner=FakeWinOS(conv=fake()))
        with self.assertRaises(AcceptanceError):  # the production output mod is never a disposable output
            run_cloth_acceptance(self.ctl, PACKAGE, CLOTH, output=str(out_mod(self.fi) / "x"), runner=FakeWinOS(conv=fake()))
        with self.assertRaises(AcceptanceError):  # nor anything MO2 would load
            run_cloth_acceptance(self.ctl, PACKAGE, CLOTH, output=str(self.fi.mods / "Acc"), runner=FakeWinOS(conv=fake()))
        self.assertEqual(tree_digest(out_mod(self.fi)), self.prod_before)

    def test_output_outside_work_acceptance_is_refused(self):
        import tempfile
        with self.assertRaises(AcceptanceError):
            run_cloth_acceptance(self.ctl, PACKAGE, CLOTH, output=tempfile.mkdtemp(), runner=FakeWinOS(conv=fake()))

    def test_isolation_object_verifies_its_scope(self):
        from cbbe3bbb.conversion.plan import AcceptanceIsolation, AcceptanceIsolationError
        root = self.ctl.paths.root / "work" / "acceptance"
        ok = dict(command="acceptance-test-cloth", project=CLOTH, source_mod=PACKAGE, output_dir=str(root / "x" / "OUT"),
                  acceptance_root=str(root), production_output_mod=str(out_mod(self.fi)),
                  mods_dir=str(self.fi.mods), no_commit=True)
        AcceptanceIsolation(**ok).verify()
        for bad in ({"command": "convert-package"}, {"no_commit": False}, {"output_dir": str(out_mod(self.fi))},
                    {"output_dir": str(self.fi.mods / "X")}, {"acceptance_root": str(self.fi.mods)}):
            with self.assertRaises(AcceptanceIsolationError, msg=bad):
                AcceptanceIsolation(**{**ok, **bad}).verify()


class UnknownAutoRealCaseTests(unittest.TestCase):
    """The real Latex Suit Top case (2026-10-01): the generated '... - 3BBB AUTO.osp' exists in the real output mod
    WITHOUT a manifest record → production says UNKNOWN_AUTO (unchanged); the acceptance uses the original source."""

    def test_unknown_auto_blocks_production_but_not_the_isolated_acceptance(self):
        ctl, fi, _drv, r, _ = run_pkg()
        self.assertEqual(items(r)[CLOTH]["outcome"], "CONVERTED")
        om = out_mod(fi)
        man = json.loads((om / "manifest.json").read_text())
        man["conversions"] = [c for c in man["conversions"] if c.get("source", {}).get("project") != CLOTH]
        (om / "manifest.json").write_text(json.dumps(man, indent=1))  # fixture: the record is unknown now
        ctl.detect(ctl.installation.profile)  # MO2 now serves the generated AUTO project (as on the real PC)
        self.assertIsNotNone(ctl.installation.vfs.resolve(f"SliderSets/{CLOTH} - 3BBB AUTO.osp"))
        auto = om / "Tools" / "BodySlide" / "SliderSets" / f"{CLOTH} - 3BBB AUTO.osp"
        self.assertTrue(auto.is_file())
        auto_bytes = auto.read_bytes()
        prod_before = tree_digest(om)
        # production: UNKNOWN_AUTO exactly as before (never overwritten, not selectable)
        prev = ctl.analyze_package(PACKAGE, write=False)
        pp = next(p for p in prev.projects if p.project == CLOTH)
        self.assertEqual(pp.status, "UNKNOWN_AUTO")
        self.assertFalse(pp.selected)
        r2 = ctl.pipeline(PACKAGE, "ALL_POSSIBLE", runner=FakeWinOS(conv=fake())).run()
        self.assertNotIn(CLOTH, [i["project"] for i in r2.items if i["outcome"] == "CONVERTED"])
        self.assertEqual(auto.read_bytes(), auto_bytes)
        prod_before = tree_digest(om)
        # acceptance: the original source project, disposable workspace, AUTO file byte-identical
        rep = run_cloth_acceptance(ctl, PACKAGE, CLOTH, runner=FakeWinOS(conv=fake()))
        self.assertTrue(rep["passed"], "\n".join(report_lines(rep)))
        self.assertTrue(rep["existing_auto_unchanged"])
        self.assertIn(str(auto), [f["path"] for f in rep["existing_auto_files"]])
        self.assertTrue(all(s["path"].startswith(str(fi.mods / PACKAGE)) for s in rep["source_files"]))
        self.assertEqual(auto.read_bytes(), auto_bytes)
        self.assertEqual(tree_digest(om), prod_before)
        b = json.loads((Path(rep["batch"]) / "batch.json").read_text())
        self.assertEqual(b["acceptance"]["source_mod"], PACKAGE)
        with self.assertRaises(BatchError):
            ctl.commit_batch(rep["batch"])
        # and production still refuses afterwards
        prev = ctl.analyze_package(PACKAGE, write=False)
        self.assertEqual(next(p for p in prev.projects if p.project == CLOTH).status, "UNKNOWN_AUTO")


if __name__ == "__main__":
    unittest.main()
