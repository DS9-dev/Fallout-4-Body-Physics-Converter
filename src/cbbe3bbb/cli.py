"""Command line interface.

    python -m cbbe3bbb gui                      # start the GUI (default)
    python -m cbbe3bbb detect  --mo2 "T:\\MO_Main _ baseCBBE" [--game "T:\\Fallout 4"]
    python -m cbbe3bbb analyze --profile NAME   # or --mo2/--game
    python -m cbbe3bbb profiles                 # list saved profiles
    python -m cbbe3bbb runtime --os-dir "T:\\Fallout 4 Tools\\BodySlide 5.8.2"
    python -m cbbe3bbb plan    --profile NAME --project "MM69 Raider Punk Top - CBBE"   # dry run
    python -m cbbe3bbb convert --profile NAME --project "MM69 Raider Punk Top - CBBE" --yes
    python -m cbbe3bbb revalidate --profile NAME --workspace <work\\conversions\\id> [--commit]
    python -m cbbe3bbb analyze-package --profile NAME --package "<MO2 mod>"          # Batch Preview
    python -m cbbe3bbb plan-batch      --profile NAME --package "<MO2 mod>" [--select P]... # Dry Run
    python -m cbbe3bbb convert-batch   --profile NAME --package "<MO2 mod>" [--select P]... --yes
    python -m cbbe3bbb validate-batch  --profile NAME --batch <id>
    python -m cbbe3bbb commit-batch    --profile NAME --batch <id> --yes
    python -m cbbe3bbb plan-direct-batch    --profile NAME --package "<MO2 mod>" --allow-direct-nif --select X [--select Y]...
    python -m cbbe3bbb convert-direct-batch --profile NAME --package "<MO2 mod>" --allow-direct-nif --select X ... --yes
    python -m cbbe3bbb commit-direct-batch  --profile NAME --batch <id> --yes
    python -m cbbe3bbb verify-direct-build  --profile NAME [--mesh X]

detect/analyze/runtime/plan are read-only. ``convert`` writes only into the
tool folder (work/, runtime/) and, after successful validation, into the
generated output mod. ``revalidate`` re-checks an existing workspace without
starting Outfit Studio; it writes result.revalidated.json into the workspace and,
only with ``--commit`` and all checks passed, the output mod.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from collections import Counter
from typing import Optional

from . import PRODUCT_NAME, __version__
from .app.controller import AppController, AppPaths
from .app.logsetup import setup_logging
from .profiles.store import Profile

log = logging.getLogger("cbbe3bbb.cli")


def _profile_from_args(ctl: AppController, a: argparse.Namespace) -> Profile:
    if a.profile:
        p = ctl.store.get(a.profile)
        if p is None:
            raise SystemExit(f"unknown profile '{a.profile}' (see: python -m cbbe3bbb profiles)")
    elif a.mo2 or a.game:
        p = Profile(name=a.name or "CLI", game_dir=a.game or "", mo2_dir=a.mo2 or "", mo2_profile=a.mo2_profile or "")
    else:
        p = ctl.current_profile()
        if not (p.game_dir or p.mo2_dir):
            raise SystemExit("no profile configured: pass --mo2/--game or --profile")
    if getattr(a, "include_disabled", False):
        p.analysis.include_disabled_mods = True
    if getattr(a, "no_cache", False):
        p.analysis.use_cache = False
    if a.save and (a.mo2 or a.game):
        ctl.save_profile(p)
    return p


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--profile", help="saved profile name")
    sp.add_argument("--mo2", help="MO2 instance folder (contains ModOrganizer.ini)")
    sp.add_argument("--game", help="Fallout 4 folder (contains Fallout4.exe)")
    sp.add_argument("--mo2-profile", help="MO2 profile name (default: the instance's selected profile)")
    sp.add_argument("--name", help="profile name when saving (--save)")
    sp.add_argument("--save", action="store_true", help="save --mo2/--game as a profile")


def _add_bodies(sp: argparse.ArgumentParser) -> None:
    """0.6.0: optional Source/Target body (defaults = the 0.5.x behaviour: auto → cbbe_3bbb)."""
    sp.add_argument("--source-body", default="auto",
                    help="auto (default: detect per outfit) | cbbe | fusion_girl | twb — an explicit source only "
                         "confirms the detection; outfits detected as another body are not converted")
    sp.add_argument("--target-body", default="cbbe_3bbb", help="cbbe_3bbb (default; the only target)")


def _progress(done: int, total: int, msg: str) -> None:
    if sys.stderr.isatty():
        sys.stderr.write(f"\r{msg:<70}")
        sys.stderr.flush()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="run_batch.bat", description=f"{PRODUCT_NAME} {__version__} — command line "
                                 "(--profile = converter profile, --mo2-profile = Mod Organizer 2 profile)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--home", help="application folder for config/reports/logs/cache (default: repository root)")
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("gui", help="start the GUI (simplified window; --developer for the engineering window)")
    g.add_argument("--developer", action="store_true", help="open the Developer / Diagnostics window")
    sub.add_parser("profiles", help="list saved profiles")
    d = sub.add_parser("detect", help="detect installation and print a summary")
    _add_common(d)
    an = sub.add_parser("analyze", help="analyze the BodySlide library and write reports/latest.*")
    _add_common(an)
    an.add_argument("--include-disabled", action="store_true", help="also scan mods disabled in the MO2 profile")
    an.add_argument("--no-cache", action="store_true", help="ignore the analysis cache")
    an.add_argument("--filter", default="", help="only scan project files whose mod or path contains this text")
    an.add_argument("--workers", type=int, default=None)
    rt = sub.add_parser("runtime", help="detect an Outfit Studio folder (version, headless automation)")
    rt.add_argument("--os-dir", help="Outfit Studio 5.8+ folder (default: the profile's)")
    _add_common(rt)
    for name, hlp in (("plan", "dry run: show the planned conversion and automation script (writes nothing)"),
                      ("convert", "convert one project with Outfit Studio headless automation")):
        c = sub.add_parser(name, help=hlp)
        _add_common(c)
        c.add_argument("--project", required=True, help="source slider set name")
        c.add_argument("--os-dir", help="Outfit Studio 5.8+ folder (stored in the profile with --save)")
        c.add_argument("--output-mod", help="output mod folder (default <MO2 mods>/CBBE 3BBB Auto Conversions)")
        c.add_argument("--include-disabled", action="store_true")
        c.add_argument("--no-cache", action="store_true")
        c.add_argument("--script-out", help="also write the automation script XML here (plan only)")
        if name == "convert":
            c.add_argument("--yes", action="store_true", help="required: confirm the conversion")
    # Phase 3: package / batch
    pk = sub.add_parser("packages", help="list the mods that provide BodySlide projects")
    _add_common(pk)
    for name, hlp in (("analyze-package", "Batch Preview of one package (writes only reports/packages)"),
                      ("plan-batch", "batch Dry Run: preview + selection + automation scripts (writes only reports/packages)"),
                      ("convert-batch", "convert the selected projects of a package (never commits)")):
        c = sub.add_parser(name, help=hlp)
        _add_common(c)
        c.add_argument("--package", required=True, help="exact MO2 mod name")
        c.add_argument("--output-mod", help="output mod folder (default <MO2 mods>/CBBE 3BBB Auto Conversions)")
        c.add_argument("--os-dir", help="Outfit Studio 5.8+ folder")
        c.add_argument("--details", action="store_true", help="print per-project/per-shape details")
        _add_bodies(c)
        c.add_argument("--allow-embedded-body-replacement", action="store_true",
                       help="Phase 3.1 opt-in: allow a safe embedded CBBE → CBBE-3BBB body replacement (default off)")
        if name == "analyze-package":
            c.add_argument("--allow-direct-nif", action="store_true",
                           help="0.4.0 opt-in: also analyze the package's plugin-referenced female game meshes "
                                "(Direct NIF, read-only; nothing is converted)")
        if name != "analyze-package":
            c.add_argument("--select", action="append", help="convert only these projects (repeat); default = CONVERT + HIGH")
            c.add_argument("--deselect", action="append", default=[], help="leave out this project (repeat)")
            c.add_argument("--mode", choices=["safe", "all-possible"], default="safe",
                           help="safe (default): CONVERT + HIGH only; all-possible: also try MEDIUM and attemptable "
                                "REVIEW projects (validation unchanged)")
            c.add_argument("--max-per-session", type=int, default=None,
                           help="at most this many projects per Outfit Studio session (default: profile, 25)")
        if name == "convert-batch":
            c.add_argument("--yes", action="store_true", help="required: confirm the batch conversion")
    # Phase 3.2: one-click package conversion, resume, report, diagnostics
    cp = sub.add_parser("convert-package", help="one step: analyze + convert + validate + install a whole package "
                                                "(what the GUI's Convert button does)")
    _add_common(cp)
    cp.add_argument("--package", required=True, help="exact MO2 mod name")
    cp.add_argument("--mode", choices=["safe", "all-possible"], default="safe")
    cp.add_argument("--no-commit", action="store_true", help="convert and validate only; install later with commit-batch")
    cp.add_argument("--max-per-session", type=int, default=None)
    cp.add_argument("--output-mod", help="output mod folder (default <MO2 mods>/CBBE 3BBB Auto Conversions)")
    cp.add_argument("--os-dir", help="Outfit Studio 5.8+ folder")
    cp.add_argument("--level", choices=["normal", "detailed", "debug"], default="detailed", help="how much to print")
    cp.add_argument("--yes", action="store_true", help="required: confirm the conversion (and the install)")
    _add_bodies(cp)
    br = sub.add_parser("body-routes", help="0.6.0: list the body conversion routes and their status")
    _add_common(br)
    at = sub.add_parser("acceptance-test-cloth", help="DEVELOPMENT/ACCEPTANCE (0.5.3): convert ONE cloth-data project "
                                                       "unattended into a disposable output; never installs anything")
    _add_common(at)
    at.add_argument("--package", required=True, help="exact MO2 mod name")
    at.add_argument("--project", required=True, help="exact BodySlide project (slider set) name")
    at.add_argument("--output", help="disposable output folder (new/empty; default work/acceptance/cloth_<time>)")
    rb = sub.add_parser("resume-batch", help="continue an interrupted batch from its unfinished projects")
    _add_common(rb)
    rb.add_argument("--batch", required=True)
    rb.add_argument("--commit", action="store_true", help="also install every validated project (one-click behaviour)")
    rb.add_argument("--level", choices=["normal", "detailed", "debug"], default="detailed")
    ctl_grp = rb.add_mutually_exclusive_group()
    ctl_grp.add_argument("--manual", action="store_true",
                         help="0.5.3: run the remaining Outfit Studio sessions manually (you press Execute)")
    ctl_grp.add_argument("--unattended", action="store_true",
                         help="0.5.3: run them automatically again (the default where supported)")
    rb.add_argument("--yes", action="store_true", help="required")
    pr_ = sub.add_parser("package-report", help="the package report of a batch (buckets, problems, build plan, test list)")
    _add_common(pr_)
    pr_.add_argument("--batch", required=True)
    pr_.add_argument("--verify", action="store_true", help="also check the BodySlide build (after Batch Build)")
    vp = sub.add_parser("verify-package-build", help="after BodySlide: check every committed AUTO project of a package")
    _add_common(vp)
    vp.add_argument("--package", required=True)
    vp.add_argument("--output-mod", help="output mod folder (default: the profile's)")
    ed = sub.add_parser("export-diagnostics", help="write one anonymised ZIP for a bug report")
    _add_common(ed)
    ed.add_argument("--batch", help="include this batch (default: none)")
    ed.add_argument("--out", help="ZIP path (default reports/diagnostics/...)")
    for name, hlp in (("validate-batch", "validate a batch again from its workspace (no Outfit Studio)"),
                      ("commit-batch", "write the VALIDATED projects of a batch into the output mod"),
                      ("batch-status", "show a batch (or list batches)")):
        c = sub.add_parser(name, help=hlp)
        _add_common(c)
        c.add_argument("--batch", required=name != "batch-status", help="work\\batch_conversions\\<id> (or just <id>)")
        if name == "commit-batch":
            c.add_argument("--only", action="append", help="commit only these projects (repeat)")
            c.add_argument("--yes", action="store_true", help="required: confirm the commit")
    # 0.4.1: Direct-NIF production workflow (PLAN → CONVERT TO WORKSPACE → VALIDATE → COMMIT → VERIFY)
    for name, hlp in (("plan-direct-batch", "Direct-NIF PLAN of explicitly selected game meshes (read-only)"),
                      ("convert-direct-batch", "convert the selected Direct-NIF meshes in a workspace and validate them "
                                               "(never commits)")):
        c = sub.add_parser(name, help=hlp)
        _add_common(c)
        c.add_argument("--package", required=True, help="exact MO2 mod name")
        c.add_argument("--allow-direct-nif", action="store_true", help="required: confirm the Direct-NIF source mode")
        c.add_argument("--select", action="append", default=[],
                       help="game mesh to convert (repeat): Direct-NIF ID, game-relative path, or a file name that is "
                            "unique in the package")
        c.add_argument("--output-mod", help="output mod folder (default <MO2 mods>/CBBE 3BBB Auto Conversions)")
        c.add_argument("--os-dir", help="Outfit Studio 5.8+ folder")
        c.add_argument("--details", action="store_true", help="print every planned shape (default for plan-direct-batch)")
        if name == "convert-direct-batch":
            c.add_argument("--plan", help="the inspected *_direct_plan.json: refuse if the fresh plan differs from it")
            c.add_argument("--yes", action="store_true", help="required: confirm the conversion into the workspace")
    for name, hlp in (("validate-direct-batch", "validate a Direct-NIF batch again from its workspace (no Outfit Studio)"),
                      ("commit-direct-batch", "write the VALIDATED meshes of a Direct-NIF batch into the output mod"),
                      ("direct-batch-status", "show a Direct-NIF batch (or list them)")):
        c = sub.add_parser(name, help=hlp)
        _add_common(c)
        c.add_argument("--batch", required=name != "direct-batch-status",
                       help="work\\direct_nif_batches\\<id> (or just <id>)")
        if name == "commit-direct-batch":
            c.add_argument("--only", action="append", help="commit only this mesh (ID, file name or game path; repeat)")
            c.add_argument("--yes", action="store_true", help="required: confirm the commit")
    vd = sub.add_parser("verify-direct-build", help="after a Direct-NIF commit: check the game mesh MO2 serves (read-only)")
    _add_common(vd)
    vd.add_argument("--mesh", help="only this mesh (ID, file name or game path); default: every committed Direct-NIF mesh")
    vd.add_argument("--output-mod", help="output mod folder (default: the profile's)")
    ra = sub.add_parser("record-acceptance", help="record an in-game test result for a generated project in manifest.json")
    _add_common(ra)
    ra.add_argument("--generated", required=True, help="generated project name, e.g. 'X - 3BBB AUTO'")
    ra.add_argument("--state", required=True, choices=["PASS", "FAIL", "PARTIAL"])
    ra.add_argument("--note", default="")
    ra.add_argument("--output-mod", help="output mod folder (default: the profile's)")
    po = sub.add_parser("probe-outfit-studio", help="0.5.3: check that the converter can drive Outfit Studio's window "
                                                    "(starts it, opens the Automation window, closes it; runs nothing)")
    _add_common(po)
    rr = sub.add_parser("report-runtime-issue", help="record an in-game problem of a converted outfit (FAIL = no physics, "
                                                       "PARTIAL = physics problem, PASS = works / clear the report)")
    _add_common(rr)
    rr.add_argument("--project", required=True, help="source project or generated project name")
    rr.add_argument("--state", choices=["FAIL", "PARTIAL", "PASS"], default="FAIL")
    rr.add_argument("--note", default="")
    rr.add_argument("--output-mod", help="output mod folder (default: the profile's)")
    vb = sub.add_parser("verify-build", help="after a BodySlide build: check the game mesh the game loads (read-only)")
    _add_common(vb)
    vb.add_argument("--generated", help="generated project name (default: every committed conversion)")
    vb.add_argument("--output-mod", help="output mod folder (default: the profile's)")
    rv = sub.add_parser("revalidate", help="re-check an existing conversion workspace without rerunning Outfit Studio")
    _add_common(rv)
    rv.add_argument("--workspace", required=True, help="work\\conversions\\<id> folder (or just <id>)")
    rv.add_argument("--commit", action="store_true", help="if every check passes, write the result to the output mod")
    a = ap.parse_args(argv)

    paths = AppPaths(__import__("pathlib").Path(a.home)) if a.home else AppPaths.default()
    setup_logging(paths.logs, console=a.cmd != "gui")
    log.info("cbbe3bbb %s: %s", __version__, a.cmd or "gui")
    ctl = AppController(paths)

    if a.cmd in (None, "gui"):
        from .gui.main_window import run_gui

        return run_gui(ctl, developer=bool(getattr(a, "developer", False)))
    if a.cmd == "body-routes":
        from .conversion.routes import ROUTES

        for r in ROUTES:
            print(f"{r.route_id:<28} {r.status:<19} {r.label}")
            print(f"{'':28} geometry: {r.geometry_stage.status} ({r.geometry_stage.strategy or '—'}); "
                  f"physics: {r.physics_stage.status} ({r.physics_stage.strategy or '—'})")
            for x in r.limitations:
                print(f"{'':28} - {x}")
        return 0
    if a.cmd == "profiles":
        for name, p in ctl.store.profiles.items():
            mark = "*" if name == ctl.store.last_profile else " "
            print(f"{mark} {name}: game={p.game_dir or '-'} mo2={p.mo2_dir or '-'}")
        return 0
    prof = _profile_from_args(ctl, a) if a.cmd != "runtime" or a.profile or a.mo2 or a.game else ctl.current_profile()
    if getattr(a, "os_dir", None):
        prof.conversion.outfitstudio_dir = a.os_dir
        if a.save:
            ctl.save_profile(prof)
    if a.cmd == "probe-outfit-studio":
        ctl.detect(prof)
        try:
            res = ctl.probe_outfit_studio()
        except RuntimeError as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        print(json.dumps({k: res.get(k) for k in ("status", "reason", "completion", "close", "exe_sha256", "report")},
                         indent=1, default=str))
        return 0 if res.get("status") == "COMPLETED" else 5
    if a.cmd == "runtime":
        info = ctl.runtime_status(prof)
        print(json.dumps(info.to_dict(), indent=2))
        return 0 if info.headless_supported else 1
    problems = ctl.validate_profile(prof)
    if problems:
        for p in problems:
            print("error:", p, file=sys.stderr)
        return 2
    inst = ctl.detect(prof)
    if a.cmd == "detect":
        print(json.dumps(inst.summary(), indent=2))
        return 0 if inst.ok else 1
    if not inst.ok:
        print("installation problems:", *inst.errors, sep="\n  ", file=sys.stderr)
        return 1
    if a.cmd in ("plan", "convert"):
        return _plan_or_convert(ctl, a)
    if a.cmd == "revalidate":
        return _revalidate(ctl, a)
    if a.cmd == "report-runtime-issue":
        try:
            d = ctl.record_runtime_issue(a.project, a.state, a.note, a.output_mod)
        except (KeyError, RuntimeError, ValueError) as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        print(f"recorded in-game result {d['state']} for {d['generated_project']} ({d['entries']} entries); the automated "
              "validation result is unchanged")
        return 0
    if a.cmd == "record-acceptance":
        from .conversion.manifest import Manifest

        om = ctl.output_mod_path(a.output_mod)
        if om is None or not (om / "manifest.json").is_file():
            print(f"error: no manifest.json in {om}", file=sys.stderr)
            return 2
        man = Manifest.load(om)
        try:
            rec = man.record_acceptance(a.generated, a.state, a.note)
        except KeyError as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        man.save()
        print(f"recorded {a.state} for {a.generated} ({len(rec['acceptance']['history'])} entries)")
        return 0
    if a.cmd == "verify-build":
        prof = _profile_from_args(ctl, a)
        ctl.detect(prof)
        try:
            results = ctl.verify_build(a.generated, a.output_mod)
        except (KeyError, RuntimeError) as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        for res in results:
            print(res.text())
            print()
        bad = [r for r in results if r.status != "BUILD_OK"]
        print(f"{len(results) - len(bad)}/{len(results)} BUILD_OK; reports in {ctl.paths.reports / 'builds'}")
        return 0 if not bad else 1
    if a.cmd in ("plan-direct-batch", "convert-direct-batch", "validate-direct-batch", "commit-direct-batch",
                 "direct-batch-status", "verify-direct-build"):
        return _direct_cli(ctl, a)
    if a.cmd in ("convert-package", "resume-batch", "package-report", "verify-package-build", "export-diagnostics"):
        return _mass_cli(ctl, a)
    if a.cmd == "acceptance-test-cloth":
        from .app.acceptance import AcceptanceError, report_lines, run_cloth_acceptance

        print("ACCEPTANCE TEST (development): one project, disposable output, forced no-commit, unattended only")
        try:
            rep = run_cloth_acceptance(ctl, a.package, a.project, output=a.output, say=lambda m: print(f"  {m}"))
        except AcceptanceError as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        print()
        for line in report_lines(rep):
            print(line)
        print(f"\nreport: {Path(rep['batch']) / 'acceptance_cloth.json'}")
        return 0 if rep["passed"] else 1
    if a.cmd in ("packages", "analyze-package", "plan-batch", "convert-batch", "validate-batch", "commit-batch", "batch-status"):
        return _batch_cli(ctl, a)
    flt = a.filter.lower()
    pf = (lambda mod, rel: flt in mod.lower() or flt in rel.lower()) if flt else None
    t = time.time()
    try:
        rep = ctl.analyze(progress=_progress, workers=a.workers, project_filter=pf)
    except KeyboardInterrupt:
        ctl.cancel()
        print("\ncancelled", file=sys.stderr)
        return 130
    if sys.stderr.isatty():
        sys.stderr.write("\n")
    c = Counter(p.action for p in rep.projects)
    print(f"analyzed {len(rep.projects)} projects in {time.time() - t:.1f}s")
    for k in ("CONVERT", "REVIEW", "KEEP", "SKIP", "UNSUPPORTED", "ERROR"):
        print(f"  {k:<12}{c.get(k, 0):>6}")
    for k, v in ctl.report_paths.items():
        print(f"  {k}: {v}")
    return 0


def _print_result(res) -> None:
    print(f"\nRESULT: {res.status}")
    for r in res.reasons:
        print("  -", r)
    for c in res.validation.get("checks", []):
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    for n in res.validation.get("notes", []):
        print(f"  note: {n}")
    print(f"  workspace: {res.workspace}")
    for f in res.files_written:
        print(f"  wrote: {Path(res.output_mod) / f}")


def _print_summary(summary: dict) -> None:
    from .conversion.batch import summary_text

    print(summary_text(summary))


def _batch_cli(ctl: AppController, a: argparse.Namespace) -> int:
    from .conversion.package import preview_text

    if a.cmd == "packages":
        for n in ctl.package_names():
            print(n)
        return 0
    if a.cmd in ("analyze-package", "plan-batch", "convert-batch"):
        from .conversion.package import apply_routing
        from .conversion.routes import RouteNotImplemented, resolve_request

        try:
            src, dst = resolve_request(a.source_body, a.target_body)
        except (RouteNotImplemented, ValueError) as exc:
            print("error:", exc, "(nothing was started)", file=sys.stderr)
            return 2
        try:
            prev = ctl.analyze_package(a.package, output_mod=a.output_mod, write=a.cmd == "analyze-package",
                                       allow_body_replacement=a.allow_embedded_body_replacement,
                                       allow_direct_nif=getattr(a, "allow_direct_nif", False))
        except RuntimeError as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        if (src, dst) != ("auto", "cbbe_3bbb") and prev.projects:
            apply_routing(prev, src, dst)
        problems = []
        if a.cmd != "analyze-package":
            if getattr(a, "max_per_session", None) is not None:
                ctl.installation.profile.conversion.max_projects_per_session = a.max_per_session
            prev.select_mode("ALL_POSSIBLE" if a.mode == "all-possible" else "SAFE")
            problems, scripts = ctl.plan_batch(prev, select=a.select, deselect=a.deselect, write=a.cmd == "plan-batch")
        print(preview_text(prev, detail=a.details))
        if getattr(ctl, "direct_report", None) is not None:
            from .conversion.direct_nif import direct_report_text

            print("=" * 100)
            print("DIRECT NIF (read-only analysis)")
            print(direct_report_text(ctl.direct_report, details=a.details))
        for p in problems:
            print("error:", p, file=sys.stderr)
        for k, v in ctl.preview_paths.items():
            print(f"  {k}: {v}")
        if problems:
            return 3
        if a.cmd != "convert-batch":
            print("\nDRY RUN: nothing was converted and nothing was written to the output mod.")
            return 0
        if not prev.selected():
            print("nothing selected", file=sys.stderr)
            return 3
        if not a.yes:
            print("\nadd --yes to run the batch conversion (it never commits; use commit-batch afterwards)", file=sys.stderr)
            return 4
        bdir, summary = ctl.convert_batch(prev, progress=lambda m: print("  ..", m))
        _print_summary(summary)
        print(f"batch workspace: {bdir}")
        print(f"next: commit-batch --batch {bdir.name} --yes   (writes only the VALIDATED projects)")
        return 0 if summary["counts"].get("VALIDATED", 0) else 5
    if a.cmd == "batch-status" and not a.batch:
        for b in ctl.list_batches():
            print(b.name)
        return 0
    from .conversion.batch import BatchError

    try:
        if a.cmd == "validate-batch":
            summary = ctl.validate_batch(a.batch, progress=lambda m: print("  ..", m))
        elif a.cmd == "commit-batch":
            if not a.yes:
                print("add --yes to commit the VALIDATED projects of this batch into the output mod", file=sys.stderr)
                return 4
            summary = ctl.commit_batch(a.batch, only=a.only, progress=lambda m: print("  ..", m))
        else:
            summary = ctl.batch_status(a.batch)
    except BatchError as exc:
        print("error:", exc, file=sys.stderr)
        return 2
    _print_summary(summary)
    return 0


def _mass_cli(ctl: AppController, a: argparse.Namespace) -> int:
    """Phase 3.2 commands (one-click conversion, resume, package report, package build check, diagnostics)."""
    from .app.pipeline import analysis_overview, result_text
    from .app.report import package_report_text
    from .conversion.batch import BatchError

    level = getattr(a, "level", "detailed").upper()

    def show(ev: dict) -> None:
        order = {"NORMAL": 0, "DETAILED": 1, "DEBUG": 2}
        if ev.get("text") and order.get(ev.get("level", "NORMAL"), 0) <= order.get(level, 1):
            print(f"  {ev['time']} {ev['text']}")

    if a.cmd == "export-diagnostics":
        out = ctl.export_diagnostics(a.out, batch=a.batch)
        print(f"diagnostic report: {out}")
        return 0
    if a.cmd == "verify-package-build":
        try:
            v = ctl.verify_package_build(a.package, output_mod=a.output_mod)
        except RuntimeError as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        print(json.dumps({k: v[k] for k in ("package", "output_mod_state", "committed", "counts", "ok")}, indent=1))
        for g, d in v["groups"].items():
            print(f"group '{g}': {'present' if d['exists'] else 'MISSING'}; missing members: {len(d['missing'])}")
        for r in v["rows"]:
            if r["status"] != "BUILD_OK":
                print(f"  {r['status']:<14} {r['generated_project']}: {'; '.join(r['problems'][:2])}")
        for line in v.get("explanation", []):
            print(line)
        return 0 if v["ok"] else 5
    if a.cmd == "package-report":
        from .conversion.package import PackagePreview

        bdir = ctl.resolve_batch(a.batch)
        try:
            prev = PackagePreview.from_dict(json.loads((bdir / "preview.json").read_text(encoding="utf-8")))
        except OSError as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        pl = ctl.pipeline(prev.package, mode=prev.conversion_mode or "SAFE")
        b = json.loads((bdir / "batch.json").read_text(encoding="utf-8"))
        pl.commit = any(e["status"] == "COMMITTED" for e in b["projects"])
        res = pl.result(prev, bdir)
        verify = ctl.verify_package_build(prev.package) if a.verify else None
        print(package_report_text(res, verify))
        for k, p in ctl.write_package_report(res, verify).items():
            print(f"  {k}: {p}")
        return 0
    if a.cmd == "resume-batch":
        if not a.yes:
            print("add --yes to continue the batch (Outfit Studio runs again for the unfinished projects)", file=sys.stderr)
            return 4
        control = "manual" if a.manual else ("unattended" if a.unattended else None)
        try:
            if a.commit:
                res = ctl.pipeline("", commit=True, events=show, control_failure=_cli_control_failure).resume(
                    a.batch, control=control)
                print(result_text(res, level))
                for k, p in ctl.write_package_report(res).items():
                    print(f"  {k}: {p}")
                return 0 if res.state in ("COMPLETE", "COMPLETE_WITH_WARNINGS") else 5
            summary = ctl.resume_batch(a.batch, progress=lambda m: print("  ..", m), control=control,
                                       control_failure=_cli_control_failure)
        except BatchError as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        _print_summary(summary)
        return 0
    # convert-package
    if a.max_per_session is not None:
        ctl.installation.profile.conversion.max_projects_per_session = a.max_per_session
    mode = "ALL_POSSIBLE" if a.mode == "all-possible" else "SAFE"
    from .app.pipeline import route_lines
    from .conversion.routes import RouteNotImplemented

    try:
        pl = ctl.pipeline(a.package, mode=mode, commit=not a.no_commit, events=show, output_mod=a.output_mod,
                          control_failure=_cli_control_failure, source_body=a.source_body, target_body=a.target_body)
    except (RouteNotImplemented, ValueError) as exc:
        print("error:", exc, "(nothing was started)", file=sys.stderr)
        return 2
    try:
        prev = pl.analyze()
    except RuntimeError as exc:
        print("error:", exc, file=sys.stderr)
        return 2
    ov = analysis_overview(prev)
    print(f"\n{ov['package']}: {ov['total']} outfits/projects found")
    print(f"  Ready to convert:           {ov['ready']:>4}")
    print(f"  Will try in All Possible:   {ov['try_all_possible']:>4}")
    print(f"  Already compatible:         {ov['already_compatible']:>4}")
    print(f"  No conversion needed:       {ov['no_conversion_needed']:>4}")
    print(f"  Cannot safely process:      {ov['cannot_process']:>4}")
    for line in route_lines(ov):
        print(f"  {line}")
    if not a.yes:
        print(f"\nadd --yes to convert in mode {mode} ({'convert + validate only' if a.no_commit else 'validated results are installed into the output mod'})",
              file=sys.stderr)
        return 4
    res = pl.run(prev)
    print()
    print(result_text(res, level))
    for k, p in ctl.write_package_report(res).items():
        print(f"  {k}: {p}")
    if res.batch_id:
        print(f"batch: {res.batch_id}")
        if res.state == "INTERRUPTED":
            print(f"next: resume-batch --batch {res.batch_id} --commit --yes   (add --manual to press Execute yourself)")
    return 0 if res.state in ("COMPLETE", "COMPLETE_WITH_WARNINGS") else 5


def _cli_control_failure(info: dict) -> str:
    """0.5.3: the automatic Outfit Studio control could not continue (twice in a row): stop, print how to go on."""
    from .app.messages import CONTROL_FAILURE_TEXT

    print()
    print(CONTROL_FAILURE_TEXT)
    print(f"  session {info.get('session')}: {info.get('reason', '')}")
    print(f"  retry automatically:  run_batch.bat resume-batch --batch {info.get('batch')} --commit --yes")
    print(f"  continue manually:    run_batch.bat resume-batch --batch {info.get('batch')} --commit --yes --manual")
    print(f"      (Outfit Studio opens: File > Open Automation, choose '{info.get('script')}', press Execute)")
    return "stop"


def _direct_cli(ctl: AppController, a: argparse.Namespace) -> int:
    from .conversion.direct_batch import (LAYERING_NOTE, DirectBatchError, compare_with_saved_plan, direct_summary_text,
                                          plan_text)

    if a.cmd in ("plan-direct-batch", "convert-direct-batch"):
        if not a.allow_direct_nif:
            print("error: Direct-NIF conversion needs --allow-direct-nif", file=sys.stderr)
            return 2
        if not a.select:
            print("error: select every mesh explicitly with --select (there is no default selection)", file=sys.stderr)
            return 2
        try:
            plan = ctl.plan_direct_batch(a.package, a.select, output_mod=a.output_mod, write=a.cmd == "plan-direct-batch")
        except RuntimeError as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        print(plan_text(plan, details=a.details or a.cmd == "plan-direct-batch"))
        for k, v in ctl.direct_plan_paths.items():
            print(f"  {k}: {v}")
        if not plan.ok:
            return 3
        if a.cmd == "plan-direct-batch":
            print("\nPLAN ONLY: nothing was copied, converted or written. Next (after inspecting the plan):\n"
                  "  convert-direct-batch with the same --package/--select, --allow-direct-nif, --plan <plan json> --yes")
            return 0
        if getattr(a, "plan", None):
            try:
                saved = json.loads(Path(a.plan).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"error: cannot read the plan file: {exc}", file=sys.stderr)
                return 2
            diff = compare_with_saved_plan(plan, saved)
            if diff:
                for d in diff:
                    print("error:", d, file=sys.stderr)
                print("the situation changed since the inspected plan: run plan-direct-batch again", file=sys.stderr)
                return 3
        if not a.yes:
            print("\nadd --yes to convert into the workspace (it never commits; use commit-direct-batch afterwards)",
                  file=sys.stderr)
            return 4
        try:
            bdir, summary = ctl.convert_direct_batch(plan, progress=lambda m: print("  ..", m))
        except DirectBatchError as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        print(direct_summary_text(summary))
        print(f"batch workspace: {bdir}")
        print(f"next: inspect the validation, then commit-direct-batch --batch {bdir.name} --yes   "
              "(writes only the VALIDATED meshes)")
        return 0 if summary["counts"].get("VALIDATED", 0) else 5
    if a.cmd == "verify-direct-build":
        try:
            results = ctl.verify_direct_build(a.mesh, a.output_mod)
        except (KeyError, RuntimeError) as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        for r in results:
            print(r.text())
            print()
        bad = [r for r in results if r.status != "DIRECT_OK"]
        print(f"{len(results) - len(bad)}/{len(results)} DIRECT_OK; reports in {ctl.paths.reports / 'builds'}")
        if not bad:
            print("\n" + LAYERING_NOTE)
        return 0 if not bad else 1
    if a.cmd == "direct-batch-status" and not a.batch:
        for b in ctl.list_direct_batches():
            print(b.name)
        return 0
    try:
        if a.cmd == "validate-direct-batch":
            summary = ctl.validate_direct_batch(a.batch, progress=lambda m: print("  ..", m))
        elif a.cmd == "commit-direct-batch":
            if not a.yes:
                print("add --yes to commit the VALIDATED meshes of this batch into the output mod", file=sys.stderr)
                return 4
            summary = ctl.commit_direct_batch(a.batch, only=a.only, progress=lambda m: print("  ..", m))
        else:
            summary = ctl.direct_batch_status(a.batch)
    except DirectBatchError as exc:
        print("error:", exc, file=sys.stderr)
        return 2
    print(direct_summary_text(summary))
    if a.cmd == "commit-direct-batch" and summary["counts"].get("COMMITTED"):
        print("=" * 100)
        print(LAYERING_NOTE)
        print("=" * 100)
        print("next: verify-direct-build (read-only) BEFORE starting Fallout 4")
    return 0


def _revalidate(ctl: AppController, a: argparse.Namespace) -> int:
    res = ctl.revalidate(a.workspace, commit=a.commit, progress=lambda m: print("  ..", m))
    _print_result(res)
    if res.status == "VALIDATED":
        print("\nNothing was written. Re-run with --commit to write the project into the output mod.")
        return 0
    return 0 if res.ok else 5


def _plan_or_convert(ctl: AppController, a: argparse.Namespace) -> int:
    from .reports.writer import load_report

    latest = ctl.paths.reports / "latest.json"
    if latest.is_file():
        ctl.report = load_report(latest)
    matches = [p for p in (ctl.report.projects if ctl.report else []) if p.project == a.project]
    if not matches:
        # analyze just the matching project file(s)
        rep = ctl.analyze(write=False, workers=1)
        matches = [p for p in rep.projects if p.project == a.project]
    if not matches:
        print(f"project '{a.project}' not found", file=sys.stderr)
        return 2
    plan = ctl.plan_conversion(matches[0], output_mod=a.output_mod)
    print("\n".join(plan.preview_lines()))
    if plan.script_summary:
        print("\nAutomation steps:")
        for i, st in enumerate(plan.script_summary, 1):
            print(f"  {i}. {st}")
    if a.cmd == "plan":
        if getattr(a, "script_out", None) and plan.script_xml:
            Path(a.script_out).write_text(plan.script_xml, encoding="utf-8")
            print(f"\nscript written to {a.script_out}")
        print("\nDRY RUN: nothing was written.")
        return 0 if plan.can_convert else 3
    if not plan.can_convert:
        return 3
    if not a.yes:
        print("\nadd --yes to run the conversion", file=sys.stderr)
        return 4
    res = ctl.convert(plan, progress=lambda m: print("  ..", m))
    _print_result(res)
    return 0 if res.ok else 5


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
