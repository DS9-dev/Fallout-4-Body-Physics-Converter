import json
import logging
import os
import unittest
from pathlib import Path

from tests._util import fake_install, temp_home, tree_digest
from cbbe3bbb.app.controller import AppController, AppPaths, UnsafeOutputLocation, filter_projects, project_details_text
from cbbe3bbb.app.logsetup import sanitize, setup_logging
from cbbe3bbb.cli import main as cli_main
from cbbe3bbb.profiles.store import Profile, ProfileStore


def _drop_handlers():
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_cbbe3bbb", False):
            root.removeHandler(h)
            h.close()
    logging.getLogger("cbbe3bbb").setLevel(logging.ERROR)


class ProfileStoreTests(unittest.TestCase):
    def test_roundtrip_and_phase1_guard(self):
        d = temp_home()
        st = ProfileStore(d)
        p = Profile(name="Main", game_dir="T:\\Fallout 4", mo2_dir="T:\\MO_Main _ baseCBBE", output_mod_enabled=True)
        p.conversion.search_radius = 3.5
        st.upsert(p)
        st.last_profile = "Main"
        st.save()
        st2 = ProfileStore(d)
        q = st2.get("Main")
        self.assertEqual(q.mo2_dir, "T:\\MO_Main _ baseCBBE")
        self.assertEqual(q.conversion.search_radius, 3.5)
        self.assertFalse(q.output_mod_enabled)  # forced off in Phase 1
        self.assertEqual(st2.last_profile, "Main")

    def test_corrupt_file_is_set_aside(self):
        d = temp_home()
        (d / "profiles.json").write_text("{not json", encoding="utf-8")
        st = ProfileStore(d)
        self.assertEqual(st.profiles, {})
        self.assertTrue((d / "profiles.json.corrupt").exists())

    def test_unknown_keys_ignored(self):
        p = Profile.from_dict({"name": "x", "future_field": 1, "conversion": {"search_radius": 2, "bogus": 1}})
        self.assertEqual(p.conversion.search_radius, 2)


class ControllerTests(unittest.TestCase):
    def test_analyze_writes_only_into_app_folder(self):
        fi = fake_install()
        before = tree_digest(fi.root)
        home = temp_home()
        ctl = AppController(AppPaths(home))
        prof = Profile(name="t", mo2_dir=str(fi.mo2_dir))
        self.assertEqual(ctl.validate_profile(prof), [])
        ctl.save_profile(prof)
        ctl.detect(prof)
        rep = ctl.analyze(workers=1)
        self.assertTrue((home / "reports" / "latest.html").is_file())
        self.assertTrue((home / "cache" / "analysis.sqlite").is_file())
        self.assertTrue((home / "config" / "profiles.json").is_file())
        self.assertEqual(tree_digest(fi.root), before)
        conv = filter_projects(rep.projects, "Convert candidates", "top")
        self.assertIn("Test Top - CBBE", [p.project for p in conv])
        txt = project_details_text(conv[0])
        self.assertIn("Reasons:", txt)
        self.assertIn("Shapes:", txt)

    def test_refuses_output_inside_mo2(self):
        fi = fake_install()
        ctl = AppController(AppPaths(fi.mo2_dir / "mods" / "SomeTool"))
        with self.assertRaises(UnsafeOutputLocation):
            ctl.detect(Profile(name="t", mo2_dir=str(fi.mo2_dir)))
        self.assertFalse((fi.mo2_dir / "mods" / "SomeTool").exists())

    def test_validate_profile(self):
        ctl = AppController(AppPaths(temp_home()))
        self.assertTrue(ctl.validate_profile(Profile(name="x")))
        self.assertTrue(ctl.validate_profile(Profile(name="x", mo2_dir="/definitely/not/here")))


class CliTests(unittest.TestCase):
    def test_detect_and_analyze(self):
        fi = fake_install()
        home = temp_home()
        self.assertEqual(cli_main(["--home", str(home), "detect", "--mo2", str(fi.mo2_dir)]), 0)
        self.assertEqual(cli_main(["--home", str(home), "analyze", "--mo2", str(fi.mo2_dir), "--save", "--name", "Fake", "--workers", "1"]), 0)
        data = json.loads((home / "reports" / "latest.json").read_text(encoding="utf-8"))
        self.assertTrue(data["projects"])
        self.assertIn("Fake", json.loads((home / "config" / "profiles.json").read_text(encoding="utf-8"))["profiles"][0]["name"])
        self.assertEqual(cli_main(["--home", str(home), "analyze", "--profile", "Fake", "--filter", "3bbb", "--workers", "1"]), 0)
        self.assertTrue((home / "logs" / "app.log").is_file())
        _drop_handlers()


class LogTests(unittest.TestCase):
    def test_sanitize_user_paths(self):
        self.assertEqual(sanitize("C:\\Users\\JaneDoe\\AppData\\x"), "C:\\Users\\<user>\\AppData\\x")
        home = os.path.expanduser("~")
        if home not in ("~", "/", "/root"):
            self.assertNotIn(home, sanitize(f"{home}/file"))

    def test_setup_logging(self):
        d = temp_home()
        p = setup_logging(d / "logs")
        logging.getLogger("cbbe3bbb").setLevel(logging.INFO)
        logging.getLogger("cbbe3bbb.test").warning("hello C:\\Users\\Someone\\x")
        for h in logging.getLogger().handlers:
            h.flush()
        text = Path(p).read_text(encoding="utf-8")
        self.assertIn("hello C:\\Users\\<user>\\x", text)
        _drop_handlers()


if __name__ == "__main__":
    unittest.main()
