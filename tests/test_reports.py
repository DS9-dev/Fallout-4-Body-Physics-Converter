import csv
import io
import json
import unittest

from tests._util import fake_install, temp_home
from cbbe3bbb.analysis.analyzer import LibraryAnalyzer, ScanReport
from cbbe3bbb.discovery.install import detect_installation
from cbbe3bbb.profiles.store import Profile
from cbbe3bbb.reports.writer import CSV_COLUMNS, FILTERS, TABLE_COLUMNS, load_report, matches_filter, write_reports


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        inst = detect_installation(Profile(name="t", mo2_dir=str(fake_install().mo2_dir)))
        cls.rep = LibraryAnalyzer(inst, workers=1).run()
        cls.out = temp_home() / "reports"
        cls.paths = write_reports(cls.rep, cls.out)

    def test_files_exist(self):
        for k in ("json", "csv", "html"):
            self.assertTrue(self.paths[k].is_file(), k)
        self.assertEqual(self.paths["json"].name, "latest.json")
        self.assertTrue(self.paths["history"].is_file())

    def test_json_roundtrip(self):
        rep = load_report(self.paths["json"])
        self.assertIsInstance(rep, ScanReport)
        self.assertEqual(len(rep.projects), len(self.rep.projects))
        a = {p.key: p.action for p in rep.projects}
        self.assertEqual(a, {p.key: p.action for p in self.rep.projects})
        self.assertIn("installation", json.loads(self.paths["json"].read_text(encoding="utf-8")))

    def test_csv(self):
        text = self.paths["csv"].read_text(encoding="utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(list(rows[0].keys()), CSV_COLUMNS)
        self.assertEqual(len(rows), len(self.rep.projects))
        self.assertTrue(all(r["reasons"] for r in rows))

    def test_html(self):
        html = self.paths["html"].read_text(encoding="utf-8")
        self.assertIn("PHASE 1 IS AN ANALYZER. IT DOES NOT MODIFY MODS.", html)
        for col in TABLE_COLUMNS:
            self.assertIn(f"<th>{col}</th>", html)
        for f in FILTERS:
            self.assertIn(f"<option>{f}</option>", html)
        self.assertIn("Test Top - CBBE", html)
        self.assertNotIn("<script src", html)  # self-contained

    def test_filters(self):
        conv = [p for p in self.rep.projects if matches_filter(p, "Convert candidates")]
        self.assertTrue(conv and all(p.action == "CONVERT" for p in conv))
        already = [p.project for p in self.rep.projects if matches_filter(p, "Already 3BBB")]
        self.assertIn("Test Top - 3BBB", already)
        self.assertTrue(all(matches_filter(p, "All") for p in self.rep.projects))
        errs = [p for p in self.rep.projects if matches_filter(p, "Errors")]
        self.assertEqual({p.project for p in errs}, {"Broken", "Test Missing - CBBE"})


if __name__ == "__main__":
    unittest.main()
