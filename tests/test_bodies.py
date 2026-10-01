"""0.6.0 Body Architecture Foundation: body identities, detection, routes, source/target requests (tests 1–15)."""
import json
import unittest
from pathlib import Path

from tests._util import tree_digest
from cbbe3bbb import PRODUCT_NAME, TOOL_ID, __version__
from cbbe3bbb.analysis.models import FamilyResult
from cbbe3bbb.app.pipeline import analysis_overview, route_lines
from cbbe3bbb.cli import main as cli_main
from cbbe3bbb.conversion import bodies as B
from cbbe3bbb.conversion.manifest import Manifest
from cbbe3bbb.conversion.package import PackagePreview, apply_routing, preview_text
from cbbe3bbb.conversion.plan import DEFAULT_OUTPUT_MOD, ConversionPlan
from cbbe3bbb.conversion.routes import (DEFAULT_ROUTE, ROUTES, RouteNotImplemented, decide_route, resolve_request,
                                        resolve_route, route_by_id, route_for)
from fake_win_os import FakeWinOS
from synth import BS, CBBE_SLIDERS, PACKAGE, _project, outfit_patch
from test_batch import out_mod, setup
from test_mass import SAFE, fake

FG_PKG = "FG Outfits"
FG_SLIDERS = ["FG Breasts", "FG Butt", "FG Hips", "FG Thighs", "FG Waist", "Fusion Belly", "FG Arms"]


def _fg(mods, body, body_nophys):
    _project(mods / FG_PKG, "FG Top.osp", [dict(name="FG Top", folder="FG Top", source="FG Top.nif",
                                                shapes=[("FGTop", "FGTop")], sliders=FG_SLIDERS,
                                                output_path="meshes\\FG", output_file="fgtop")],
             {"FG Top/FG Top.nif": [outfit_patch(body, (85, 115), name="FGTop")]})


def fg_setup():
    return setup(extra={FG_PKG: _fg})


class BodyModelTests(unittest.TestCase):
    def test_ids_labels_and_normalization(self):
        self.assertEqual(B.BODY_IDS[:7], ("unknown", "cbbe", "cbbe_3bbb", "fusion_girl", "twb", "twb_3bbb",
                                          "bodytalk_male"))
        self.assertEqual(B.body_label("cbbe_3bbb"), "CBBE 3BBB")
        for v, want in (("CBBE_3BBB", "cbbe_3bbb"), ("Fusion Girl", "fusion_girl"), ("auto", "auto"), (None, "auto"),
                        ("Auto Detect", "auto"), ("fg", "fusion_girl"), ("TWB", "twb")):
            self.assertEqual(B.normalize_body_id(v), want, v)
        with self.assertRaises(B.BodyIdError):
            B.normalize_body_id("UNP")

    def test_detection_formalises_the_analyzer(self):
        d = B.detect_body(FamilyResult(family="CBBE", physics="none", confidence="HIGH", evidence=["x"],
                                       scores={"CBBE": 4.0, "FUSION_GIRL": 1.5}))
        self.assertEqual((d.body, d.confidence, d.evidence), ("cbbe", "HIGH", ["x"]))
        self.assertEqual(d.conflicting_evidence, ["Fusion Girl evidence score 1.5"])
        self.assertEqual(B.detect_body(FamilyResult(family="CBBE", physics="3bbb")).body, "cbbe_3bbb")
        self.assertEqual(B.detect_body(FamilyResult(family="TWB", physics="3bbb")).body, "twb_3bbb")
        self.assertEqual(B.detect_body(FamilyResult(family="UNKNOWN", physics="3bbb")).body, "unknown_3bbb")
        self.assertEqual(B.detect_body(FamilyResult(family="MALE")).body, "bodytalk_male")
        self.assertEqual(B.detect_body({"family": "FUSION_GIRL", "confidence": "HIGH"}).body, "fusion_girl")
        twb = B.detect_body(FamilyResult(family="TWB", scores={"TWB": 4.0, "CBBE": 3.0}))
        self.assertEqual(twb.conflicting_evidence, [])  # CBBE overlap is expected for a CBBE-derived body

    def test_route_registry(self):
        ids = {r.route_id: r for r in ROUTES}
        self.assertEqual(ids["CBBE_TO_CBBE_3BBB"].status, "PRODUCTION")
        self.assertTrue(ids["CBBE_TO_CBBE_3BBB"].executable)
        self.assertEqual(ids["CBBE_TO_CBBE_3BBB"].geometry_stage.status, "NOT_REQUIRED")
        fg = ids["FUSION_GIRL_TO_CBBE_3BBB"]
        self.assertEqual((fg.status, fg.geometry_stage.status), ("NOT_IMPLEMENTED", "NOT_IMPLEMENTED"))
        self.assertFalse(fg.executable)  # never runs: the geometry stage it requires does not exist
        self.assertEqual(fg.label, "Fusion Girl → CBBE → CBBE 3BBB")
        self.assertEqual(ids["CBBE_3BBB_TO_CBBE_3BBB"].status, "ALREADY_COMPATIBLE")
        self.assertEqual(ids["BODYTALK_MALE_TO_CBBE_3BBB"].status, "UNSUPPORTED")
        self.assertEqual(ids["UNKNOWN_TO_CBBE_3BBB"].status, "REVIEW")
        self.assertEqual(ids["TWB_TO_CBBE_3BBB"].status, "LEGACY_SUPPORTED")
        self.assertEqual(route_for("unknown_3bbb").status, "ALREADY_COMPATIBLE")
        # 0.5.3 API and ids still resolve
        self.assertIs(resolve_route(None), DEFAULT_ROUTE)
        self.assertIs(route_by_id("cbbe-to-cbbe3bbb"), DEFAULT_ROUTE)
        with self.assertRaises(RouteNotImplemented):
            resolve_route("FUSION_GIRL_TO_CBBE_3BBB")
        d = DEFAULT_ROUTE.to_dict()  # 0.5.3 record keys unchanged
        self.assertEqual((d["id"], d["source_body_family"], d["target_body_family"], d["physics_profile"],
                          d["geometry_conversion_strategy"]),
                         ("cbbe-to-cbbe3bbb", "CBBE", "CBBE_3BBB", "CBBE_3BBB", "none (same body)"))

    def test_requests(self):
        self.assertEqual(resolve_request(None, None), ("auto", "cbbe_3bbb"))
        self.assertEqual(resolve_request("cbbe", "CBBE 3BBB"), ("cbbe", "cbbe_3bbb"))
        with self.assertRaises(RouteNotImplemented) as cm:
            resolve_request("fusion_girl", "cbbe_3bbb")
        self.assertIn("Fusion Girl geometry conversion is not implemented yet", str(cm.exception))
        with self.assertRaises(RouteNotImplemented):
            resolve_request("auto", "cbbe")  # CBBE 3BBB is the only target

    def test_6_override_never_reinterprets(self):
        fg = B.BodyDetection(body="fusion_girl", confidence="HIGH")
        d = decide_route(fg, "cbbe")
        self.assertEqual(d.status, "SOURCE_OVERRIDE_CONFLICT")
        self.assertFalse(d.may_convert)
        unk = B.BodyDetection(body="unknown", confidence="LOW")
        self.assertEqual(decide_route(unk, "cbbe").status, "SOURCE_UNVERIFIED")  # 5: never upgraded to CBBE
        self.assertFalse(decide_route(unk).may_convert)
        self.assertTrue(decide_route(B.BodyDetection(body="cbbe", confidence="HIGH"), "cbbe").may_convert)


class PackageRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, _ = fg_setup()

    def test_1_2_4_existing_fixture_routes(self):
        prev = self.ctl.analyze_package(PACKAGE, write=False)
        by = {p.project: p for p in prev.projects}
        for n in ("Pkg Top - CBBE", "Pkg Shorts - CBBE"):  # 1: the proven route, unchanged selection
            self.assertEqual((by[n].source_body, by[n].route_id, by[n].route_status, by[n].action),
                             ("cbbe", "CBBE_TO_CBBE_3BBB", "PRODUCTION", "CONVERT"))
            self.assertTrue(by[n].selected)
            self.assertEqual(by[n].conversion_plan().route["route_id"], "CBBE_TO_CBBE_3BBB")
        k = by["Pkg Bra - 3BBB TEST"]  # 2: native 3BBB stays KEEP / already compatible
        self.assertEqual((k.action, k.source_body, k.route_status), ("KEEP", "cbbe_3bbb", "ALREADY_COMPATIBLE"))
        bt = by["Pkg Shirt - BT3"]  # 4: BodyTalk stays unsupported
        self.assertEqual((bt.action, bt.route_id), ("UNSUPPORTED", "BODYTALK_MALE_TO_CBBE_3BBB"))
        self.assertEqual((prev.requested_source, prev.target_body), ("auto", "cbbe_3bbb"))
        self.assertTrue(any("Source body: Auto Detect" in x for x in preview_text(prev, detail=False).splitlines()))

    def test_auto_routing_changes_no_classification(self):
        """With Auto Detect → CBBE 3BBB the route gate changes nothing the 0.5.x analyzer decided."""
        from unittest import mock
        from cbbe3bbb.conversion import package as P

        from test_mass import mass_setup

        mctl, _fi, _h = mass_setup()
        for ctl, pkg in ((self.ctl, PACKAGE), (self.ctl, FG_PKG), (mctl, PACKAGE)):
            with mock.patch.object(P, "apply_routing", lambda prev, *a, **k: []):
                old = ctl.analyze_package(pkg, write=False)
            new = ctl.analyze_package(pkg, write=False)
            key = lambda prev: [(p.project, p.action, p.status, p.selectable, p.selected, bool(p.plan), p.review_policy,
                                 p.bucket) for p in prev.projects]
            self.assertEqual(key(old), key(new), pkg)

    def test_3_fusion_girl_recognised_not_converted(self):
        prev = self.ctl.analyze_package(FG_PKG, write=False)
        p = prev.projects[0]
        self.assertEqual((p.source_body, p.target_body, p.route_id, p.route_status),
                         ("fusion_girl", "cbbe_3bbb", "FUSION_GIRL_TO_CBBE_3BBB", "NOT_IMPLEMENTED"))
        self.assertEqual(p.geometry_stage["status"], "NOT_IMPLEMENTED")
        self.assertFalse(p.selectable or p.selected or p.plan)
        self.assertIn("Fusion Girl geometry conversion is not implemented yet", p.reasons[0])
        ov = analysis_overview(prev)
        self.assertEqual(ov["bodies"], {"Fusion Girl": 1})
        self.assertTrue(any("geometry conversion: not implemented; physics conversion: available after geometry "
                            "conversion" in x for x in route_lines(ov)))
        r = self.ctl.pipeline(FG_PKG, "ALL_POSSIBLE", runner=FakeWinOS(conv=fake())).run(prev)
        it = r.items[0]
        self.assertEqual((it["outcome"], it["code"]), ("COULD_NOT_CONVERT_SAFELY", "ROUTE_NOT_IMPLEMENTED"))
        self.assertIn("Fusion Girl", it["message"])
        self.assertFalse(r.batch_id)  # nothing started
        with self.assertRaises(RouteNotImplemented):  # an explicit Fusion Girl source is refused before anything runs
            self.ctl.pipeline(FG_PKG, source_body="fusion_girl")

    def test_6_explicit_cbbe_on_fusion_girl_and_twb_on_cbbe(self):
        prev = self.ctl.analyze_package(FG_PKG, write=False)
        apply_routing(prev, "cbbe")
        self.assertEqual(prev.projects[0].route_status, "SOURCE_OVERRIDE_CONFLICT")
        self.assertFalse(prev.projects[0].selected or prev.projects[0].plan)
        prev = self.ctl.analyze_package(PACKAGE, write=False)
        demoted = apply_routing(prev, "twb")
        self.assertIn("Pkg Top - CBBE", demoted)
        top = next(p for p in prev.projects if p.project == "Pkg Top - CBBE")
        self.assertEqual((top.action, top.status, top.selected, top.plan), ("REVIEW", "SOURCE_OVERRIDE_CONFLICT", False, None))
        self.assertEqual(prev.selected(), [])

    def test_legacy_preview_without_body_fields_loads(self):
        prev = self.ctl.analyze_package(PACKAGE, write=False)
        d = prev.to_dict()
        for k in ("requested_source", "target_body", "body_counts"):
            d.pop(k)
        for p in d["projects"]:
            for k in ("source_body", "requested_source", "target_body", "route_id", "route_status", "route_label",
                      "route_reason", "geometry_stage", "physics_stage", "body_detection"):
                p.pop(k)
            if p.get("plan"):
                p["plan"].pop("route")
        old = PackagePreview.from_dict(d)  # 7: 0.5.x preview
        self.assertTrue(old.conversion_plan if hasattr(old, "conversion_plan") else True)
        apply_routing(old)
        top = next(p for p in old.projects if p.project == "Pkg Top - CBBE")
        self.assertEqual((top.source_body, top.route_id, top.action), ("cbbe", "CBBE_TO_CBBE_3BBB", "CONVERT"))
        self.assertEqual(top.body_detection["source"], "legacy preview label")
        self.assertEqual(ConversionPlan.from_dict({"source_project": "x"}).route, {})
        d2 = prev.to_dict()  # forward-compat: unknown project keys are ignored
        d2["projects"][0]["future_field"] = 1
        self.assertEqual(len(PackagePreview.from_dict(d2).projects), len(prev.projects))


class EndToEndTests(unittest.TestCase):
    def test_8_9_10_15_default_run_unchanged_and_route_recorded(self):
        ctl, fi, _ = fg_setup()
        src_before = tree_digest(fi.mods / PACKAGE)
        r = ctl.pipeline(PACKAGE, "SAFE", runner=FakeWinOS(conv=fake())).run()  # 9: no body arguments
        conv = {i["project"] for i in r.items if i["outcome"] == "CONVERTED"}
        self.assertEqual(conv, {"Pkg Top - CBBE", "Pkg Shorts - CBBE", "Pkg Suit - CBBE"})
        self.assertTrue(all(i["route_id"] for i in r.items))
        man = json.loads((out_mod(fi) / "manifest.json").read_text())
        self.assertEqual(man["tool"], TOOL_ID)  # 15: serialised identities unchanged
        rec = next(c for c in man["conversions"] if c["target"]["project"] == "Pkg Top - CBBE - 3BBB AUTO")
        self.assertEqual(rec["route"]["id"], "cbbe-to-cbbe3bbb")  # 0.5.3 key kept
        self.assertEqual((rec["route"]["route_id"], rec["route"]["source_body"], rec["route"]["target_body"]),
                         ("CBBE_TO_CBBE_3BBB", "cbbe", "cbbe_3bbb"))  # 8: new fields round-trip
        self.assertEqual(rec["route"]["geometry_stage"]["status"], "NOT_REQUIRED")
        self.assertEqual(rec["body_detection"]["body"], "cbbe")
        self.assertEqual(Manifest.load(out_mod(fi)).records[0]["route"]["route_id"], "CBBE_TO_CBBE_3BBB")
        self.assertEqual(out_mod(fi).name, DEFAULT_OUTPUT_MOD)
        self.assertTrue((out_mod(fi) / BS / "SliderGroups" / f"CBBE 3BBB Auto Conversions - {PACKAGE}.xml").is_file())
        self.assertEqual(tree_digest(fi.mods / PACKAGE), src_before)
        # 10: UNKNOWN_AUTO unchanged (generated project without a manifest record)
        man["conversions"] = [c for c in man["conversions"] if c["source"]["project"] != "Pkg Top - CBBE"]
        (out_mod(fi) / "manifest.json").write_text(json.dumps(man))
        ctl.detect(ctl.installation.profile)
        prev = ctl.analyze_package(PACKAGE, write=False)
        self.assertEqual(next(p for p in prev.projects if p.project == "Pkg Top - CBBE").status, "UNKNOWN_AUTO")

    def test_legacy_manifest_record_without_route_loads(self):
        ctl, fi, _ = fg_setup()
        ctl.pipeline(PACKAGE, "SAFE", runner=FakeWinOS(conv=fake())).run()
        man = json.loads((out_mod(fi) / "manifest.json").read_text())
        for c in man["conversions"]:
            c.pop("route", None)
            c.pop("body_detection", None)
        (out_mod(fi) / "manifest.json").write_text(json.dumps(man))
        self.assertTrue(Manifest.load(out_mod(fi)).records)  # 7
        prev = ctl.analyze_package(PACKAGE, write=False)
        self.assertEqual(next(p for p in prev.projects if p.project == "Pkg Top - CBBE").status, "ALREADY_CONVERTED")

    def test_cli_body_arguments(self):
        ctl, fi, home = fg_setup()
        args = ["--mo2", str(fi.mo2_dir)]
        self.assertEqual(cli_main(["body-routes"]), 0)
        self.assertEqual(cli_main(["convert-package", *args, "--package", FG_PKG, "--source-body", "fusion_girl",
                                   "--yes"]), 2)  # refused before anything starts
        self.assertEqual(cli_main(["analyze-package", *args, "--package", PACKAGE]), 0)
        self.assertEqual(cli_main(["analyze-package", *args, "--package", PACKAGE, "--source-body", "cbbe",
                                   "--target-body", "cbbe_3bbb"]), 0)
        self.assertEqual(cli_main(["analyze-package", *args, "--package", PACKAGE, "--target-body", "cbbe"]), 2)

    def test_product_name(self):
        self.assertEqual(PRODUCT_NAME, "Fallout 4 Body & Physics Converter")
        self.assertRegex(__version__, r"^0\.6\.\d+$")
        self.assertEqual(TOOL_ID, "CBBE3BBBConverter")


if __name__ == "__main__":
    unittest.main()
