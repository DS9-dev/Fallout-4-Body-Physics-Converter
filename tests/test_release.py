"""0.6.1 stabilization: one version source, clean window title, root-relative paths that survive the folder rename to
"Fallout 4 Body & Physics Converter" (an '&' and spaces in the path), profile path migration, user-facing messages."""
import json
import re
import shutil
import unittest
from pathlib import Path

from tests._util import ROOT, temp_home, tree_digest
from cbbe3bbb import PRODUCT_NAME, TOOL_ID, __version__, product_title
from cbbe3bbb.app.controller import AppController, AppPaths
from cbbe3bbb.app.messages import failed_check, humanize
from cbbe3bbb.app.pipeline import build_summary_lines
from cbbe3bbb.profiles.store import Profile, ProfileStore, migrate_tool_root
from fake_win_os import FakeWinOS
from synth import PACKAGE, build_package_install
from fake_os import make_fake_runtime
from test_mass import fake

NEW_ROOT_NAME = "Fallout 4 Body & Physics Converter"


class VersionAndTitleTests(unittest.TestCase):
    def test_one_version_source(self):
        py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotRegex(py, r'(?m)^version\s*=\s*"')  # no static version string
        self.assertIn('dynamic = ["version"]', py)
        self.assertIn('version = {attr = "cbbe3bbb.__version__"}', py)
        hits = [p for p in (ROOT / "src").rglob("*.py")
                if re.search(r'__version__\s*=\s*"', p.read_text(encoding="utf-8")) and p.name != "__init__.py"]
        self.assertEqual(hits, [])

    def test_clean_title(self):
        self.assertEqual(product_title(), f"{PRODUCT_NAME} {__version__}")
        self.assertEqual(product_title().count(PRODUCT_NAME), 1)
        for p in (ROOT / "src" / "cbbe3bbb" / "gui").glob("*.py"):
            t = p.read_text(encoding="utf-8")
            self.assertNotRegex(t, r"(?m)^\s*app\.setApplicationDisplayName", p.name)  # Qt would append it to titles
        self.assertEqual(TOOL_ID, "CBBE3BBBConverter")  # serialised identity unchanged


class LauncherTests(unittest.TestCase):
    def test_launchers_are_root_relative_and_ampersand_safe(self):
        for bat in ROOT.glob("*.bat"):
            t = bat.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("CBBE3BBBConverter", t.replace("CBBE3BBB_PYTHON", ""), bat.name)
            for line in t.splitlines():
                if line.lower().startswith("rem") or "%~dp0" not in line:
                    continue
                # every use of the script folder must be inside double quotes ('&' in the folder name)
                for m in re.finditer(r"%~dp0", line):
                    self.assertEqual(line[:m.start()].count('"') % 2, 1, f"{bat.name}: unquoted %~dp0: {line}")

    def test_no_code_depends_on_an_absolute_root(self):
        for p in (ROOT / "src").rglob("*.py"):
            t = p.read_text(encoding="utf-8")
            self.assertNotRegex(t, r"[A-Za-z]:\\\\Fallout 4 Tools\\\\CBBE3BBBConverter", p.name)


class RenamedRootTests(unittest.TestCase):
    """The whole one-click flow from a tool folder named like the new root (spaces and '&')."""

    def test_pipeline_from_new_root_name(self):
        home = temp_home()
        fi = build_package_install(home / "inst")
        rt = make_fake_runtime(home / "os", (5, 8, 2, 0))
        root = home / "Fallout 4 Tools" / NEW_ROOT_NAME
        ctl = AppController(AppPaths(root))
        p = Profile(name="Default", mo2_dir=str(fi.mo2_dir), mo2_profile="Default")
        p.conversion.outfitstudio_dir = str(rt)
        ctl.detect(p)
        src = tree_digest(fi.mods / PACKAGE)
        r = ctl.pipeline(PACKAGE, "SAFE", runner=FakeWinOS(conv=fake())).run()
        conv = [i for i in r.items if i["outcome"] == "CONVERTED"]
        self.assertTrue(conv)
        self.assertTrue(str(r.batch_dir).startswith(str(root)))
        cfg = (root / "runtime").rglob("Config.xml")
        cfgs = list(cfg)
        self.assertTrue(cfgs)
        from xml.etree import ElementTree as ET
        pp = ET.parse(cfgs[0]).getroot().findtext("ProjectPath")  # '&' escaped in the file, exact when parsed
        self.assertIn(NEW_ROOT_NAME, pp)
        self.assertIn("&amp;", cfgs[0].read_text(encoding="utf-8"))
        self.assertEqual(tree_digest(fi.mods / PACKAGE), src)
        man = json.loads((fi.mods / "CBBE 3BBB Auto Conversions" / "manifest.json").read_text())
        self.assertEqual(man["tool"], TOOL_ID)
        v = ctl.verify_package_build(PACKAGE)
        self.assertEqual(v["counts"].get("NOT_BUILT"), len(conv))
        lines = build_summary_lines(v)
        self.assertTrue(lines[0].startswith(f"Built correctly: 0 of {len(conv)}"))
        self.assertTrue(any("not built yet" in x for x in lines))
        self.assertIn(f"CBBE 3BBB Auto Conversions - {PACKAGE}", r.build["group"])


class ProfileMigrationTests(unittest.TestCase):
    def test_paths_inside_the_old_root_move_with_it(self):
        home = temp_home()
        old, new = home / "Fallout 4 Tools" / "CBBE3BBBConverter", home / "Fallout 4 Tools" / NEW_ROOT_NAME
        (new / "runtime" / "os").mkdir(parents=True)
        (home / "BodySlide").mkdir()
        (new / "config").mkdir()
        prof = {"name": "Default", "mo2_dir": str(home / "MO2"), "conversion": {
            "outfitstudio_dir": str(old / "runtime" / "os"), "output_mod_path": ""}}
        (new / "config" / "profiles.json").write_text(json.dumps({"schema": 1, "last_profile": "Default",
                                                                  "profiles": [prof]}), encoding="utf-8")
        st = ProfileStore(new / "config")
        self.assertEqual(st.profiles["Default"].conversion.outfitstudio_dir, str(new / "runtime" / "os"))
        self.assertEqual(st.profiles["Default"].mo2_dir, str(home / "MO2"))  # unrelated path untouched (even missing)
        self.assertEqual(len(st.migrated), 1)
        saved = json.loads((new / "config" / "profiles.json").read_text())
        self.assertEqual(saved["tool_root"], str(new))
        st2 = ProfileStore(new / "config")  # idempotent
        self.assertEqual(st2.migrated, [])

    def test_existing_or_foreign_paths_are_never_rewritten(self):
        home = temp_home()
        cur = home / NEW_ROOT_NAME
        (cur / "x").mkdir(parents=True)
        p = Profile(name="P", bodyslide_dir=str(home / "elsewhere" / "x"))
        p.conversion.outfitstudio_dir = str(home / "Other Tool" / "x")
        self.assertEqual(migrate_tool_root([p], cur, None), [])


class MessageTests(unittest.TestCase):
    def test_unplanned_part_and_failed_check(self):
        r = ["validation failed: expected outfit shape(s) listed — ['A', 'B'] (expected ['A'])",
             "validation failed: CBBE-3BBB body NOT embedded — NIF shapes: ['A', 'B ']; NEW shapes ['B ']"]
        code, _ = humanize("COULD_NOT_CONVERT_SAFELY", r)
        self.assertEqual(code, "UNEXPECTED_SHAPE")
        self.assertEqual(failed_check(r), "expected outfit shape(s) listed")
        code, msg = humanize("COULD_NOT_CONVERT_SAFELY",
                             ["validation failed: arm: breast 3BBB bones present — LBreast_01_skin(26)"])
        self.assertEqual(code, "PHYSICS_MISSING")
        self.assertIn("strict validation", msg)


if __name__ == "__main__":
    unittest.main()
