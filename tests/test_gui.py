"""GUI smoke test (skipped when PySide6 is not installed)."""
import os
import unittest

from tests._util import fake_install, temp_home

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # noqa: F401

    HAVE_QT = True
except Exception:  # noqa: BLE001
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class GuiSmokeTest(unittest.TestCase):
    def test_window_filters_and_details(self):
        from PySide6.QtWidgets import QApplication

        from cbbe3bbb.app.controller import AppController, AppPaths
        from cbbe3bbb.gui.main_window import MainWindow
        from cbbe3bbb.profiles.store import Profile

        app = QApplication.instance() or QApplication([])
        ctl = AppController(AppPaths(temp_home()))
        prof = Profile(name="Fake", mo2_dir=str(fake_install().mo2_dir))
        ctl.save_profile(prof)
        ctl.detect(prof)
        rep = ctl.analyze(workers=1)
        w = MainWindow(ctl)
        # Phase 2 UX: the output mod path is configurable (empty = default under the MO2 mods folder)
        self.assertTrue(w.output_mod_edit.isEnabled())
        self.assertEqual(w.output_mod_edit.text(), "")
        self.assertIn("CBBE 3BBB Auto Conversions", w.output_mod_edit.placeholderText())
        w.output_mod_edit.setText("X:/Some/Output Mod")
        self.assertEqual(w._profile_from_fields().conversion.output_mod_path, "X:/Some/Output Mod")
        w.output_mod_edit.setText("")
        # "Convert selected…" is disabled until a project row is selected
        self.assertFalse(w.convert_btn.isEnabled())
        w._show_report(rep)
        self.assertEqual(w.model.rowCount(), len(rep.projects))
        w.filter_combo.setCurrentText("Convert candidates")
        n = w.proxy.rowCount()
        self.assertTrue(0 < n < len(rep.projects))
        w.table.selectRow(0)
        w._on_row(w.proxy.index(0, 0), w.proxy.index(0, 0))
        self.assertIn("Reasons:", w.details.toPlainText())
        self.assertTrue(w.convert_btn.isEnabled())
        w.search_edit.setText("zzz-no-match")
        self.assertEqual(w.proxy.rowCount(), 0)
        w.search_edit.setText("")
        self.assertEqual(w.proxy.rowCount(), n)
        w.close()
        app.processEvents()

    def test_conversion_dialog_constructs(self):
        from PySide6.QtWidgets import QApplication

        from cbbe3bbb.app.controller import AppController, AppPaths
        from cbbe3bbb.gui.conversion_dialog import ConversionDialog
        from cbbe3bbb.profiles.store import Profile

        app = QApplication.instance() or QApplication([])
        ctl = AppController(AppPaths(temp_home()))
        prof = Profile(name="Fake", mo2_dir=str(fake_install().mo2_dir))
        ctl.detect(prof)
        rep = ctl.analyze(workers=1, write=False)
        pr = next(p for p in rep.projects if p.project == "Test Top - CBBE")
        dlg = ConversionDialog(ctl, prof, pr)
        self.assertFalse(dlg.conv_btn.isEnabled())  # only after a Dry Run
        self.assertTrue(dlg.dry_btn.isEnabled())
        dlg.close()
        app.processEvents()

    def test_package_dialog_preview_selection_dry_run(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        from cbbe3bbb.app.controller import AppController, AppPaths
        from cbbe3bbb.gui.main_window import MainWindow
        from cbbe3bbb.gui.package_dialog import COLUMNS, PackageDialog
        from cbbe3bbb.profiles.store import Profile
        from fake_os import make_fake_runtime
        from synth import PACKAGE, build_package_install

        app = QApplication.instance() or QApplication([])
        home = temp_home()
        fi = build_package_install(home / "inst")
        ctl = AppController(AppPaths(home / "tool"))
        prof = Profile(name="Pkg", mo2_dir=str(fi.mo2_dir))
        prof.conversion.outfitstudio_dir = str(make_fake_runtime(home / "os", (5, 8, 2, 0)))
        ctl.save_profile(prof)
        ctl.detect(prof)
        w = MainWindow(ctl)
        self.assertTrue(w.package_btn.isEnabled())
        dlg = PackageDialog(ctl, prof, package=PACKAGE)
        self.assertEqual(dlg.package.currentText(), PACKAGE)
        self.assertFalse(dlg.run_btn.isEnabled())
        self.assertFalse(dlg.commit_btn.isEnabled())
        # Phase 3.1: the embedded-body replacement option exists and is OFF by default
        self.assertTrue(hasattr(dlg, "body_opt"))
        self.assertFalse(dlg.body_opt.isChecked())
        self.assertFalse(dlg.direct_opt.isChecked())  # 0.4.0 Direct NIF analysis is opt-in
        dlg._analyzed(ctl.analyze_package(PACKAGE))  # what the worker thread delivers
        self.assertEqual(dlg.table.rowCount(), 9)
        self.assertEqual(dlg.table.columnCount(), len(COLUMNS))
        rows = {dlg.table.item(r, 1).text(): r for r in range(dlg.table.rowCount())}
        top = dlg.table.item(rows["Pkg Top - CBBE"], 0)
        self.assertEqual(top.checkState(), Qt.Checked)
        bt = dlg.table.item(rows["Pkg Shirt - BT3"], 0)
        self.assertFalse(bt.flags() & Qt.ItemIsUserCheckable)  # UNSUPPORTED cannot be selected
        self.assertTrue(dlg.run_btn.isEnabled())
        top.setCheckState(Qt.Unchecked)
        self.assertNotIn("Pkg Top - CBBE", [p.project for p in dlg.preview.selected()])
        dlg._show_details(rows["Pkg Suit - CBBE"])
        self.assertIn("SuitBoots", dlg.details.toPlainText())
        dlg._dry_run()
        self.assertIn("AutomationScript", dlg.script.toPlainText())
        self.assertFalse((fi.mods / "CBBE 3BBB Auto Conversions").exists())
        self.assertFalse(dlg.commit_btn.isEnabled())
        dlg.close()
        w.close()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SimpleWindowTest(unittest.TestCase):
    """Phase 3.2: the Normal-mode window driven by a real pipeline run (fake Outfit Studio)."""

    def test_progress_problem_names_and_summary(self):
        from PySide6.QtWidgets import QApplication

        from cbbe3bbb.gui.simple_window import MAX_PROBLEM_NAMES, BuildDialog, ProblemsDialog, SimpleWindow
        from test_mass import PACKAGE, fake, mass_setup

        app = QApplication.instance() or QApplication([])
        ctl, fi, home = mass_setup()
        w = SimpleWindow(ctl, auto_detect=False)
        w._detected((ctl.installation, ctl.runtime_status(), ctl.package_names()))
        self.assertIn(PACKAGE, [w.pkg_combo.itemText(i) for i in range(w.pkg_combo.count())])
        self.assertIn("Enabled", w.out_lbl.text())
        evs = []
        pl = ctl.pipeline(PACKAGE, mode="ALL_POSSIBLE", events=evs.append,
                          runner=fake(per_project={"Pkg Jacket - CBBE": "no_weights"}))
        prev = pl.analyze()
        w._analyzed(prev)
        self.assertEqual(w.an_counts["ready"].text(), "4")
        w._start_progress(PACKAGE)
        r = pl.run(prev)
        for e in evs:
            w._on_event(e)
        self.assertIn("Pkg Jacket - CBBE", w.pr_prob_list.text())  # the name, not only a counter
        self.assertEqual(w.pr_prob_head.text(), "Problems: 1")
        w._finished(r)
        attn = w.rs_attention.toPlainText()
        self.assertIn("Pkg Bodysuit - CBBE", attn)  # names, not only counters
        self.assertIn("contains its own body", attn)
        self.assertEqual(w.rs_attn_n.text(), str(len(r.needs_attention)))
        self.assertEqual(w.rs_completed.text(), str(len(r.completed)))
        self.assertIn("left unchanged", w.rs_problems.text())
        self.assertEqual(w.pages.currentIndex(), 3)
        d = ProblemsDialog(r.needs_attention, "Needs attention")
        self.assertIn("Original outfit was left unchanged.", d.text())
        d.tech.setChecked(True)
        self.assertIn("project:", d.text())
        b = BuildDialog(ctl, r)
        b._check()
        self.assertIn("Built correctly: 0 of", b.out.text())
        self.assertGreaterEqual(MAX_PROBLEM_NAMES, 3)
        for x in (d, b, w):
            x.close()
        app.processEvents()

