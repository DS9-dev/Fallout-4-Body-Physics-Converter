"""0.5.3 targeted real-Windows acceptance: Outfit Studio's *Choose cloth data* (BSClothExtraData) dialog.

DEVELOPMENT / ACCEPTANCE ONLY — never part of a normal conversion. ``acceptance-test-cloth`` converts exactly ONE named
project of a package that carries cloth physics data, in a disposable batch, fully unattended, and never installs
anything:

* the project is planned from its ORIGINAL source project (only the package's own projects are analyzed, against an
  empty disposable output folder), so a project that is already converted/committed can be exercised again WITHOUT
  changing the normal "already compatible" detection, and without touching or hiding the existing 3BBB project;
* forced no-commit: the batch is marked ``acceptance`` and :meth:`BatchEngine.commit` refuses such a batch;
* forced disposable output: the plan's output mod is a new, empty folder under ``work/acceptance`` (or ``--output``,
  which must be new/empty and may not be — or be inside — the configured output mod or the MO2 mods folder);
* unattended only: the Win32 control must be available; there is no manual fallback (a control failure stops);
* evidence: source SHA-256 before/after, a size+mtime fingerprint of the real output mod before/after, the real
  dialog's control hierarchy, the offered/selected origin, and the source vs generated cloth payloads (size+SHA-256).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

from ..conversion.plan import sha256_file

ACCEPTED_TEXT = "0.5.3 UNATTENDED OUTFIT STUDIO: ACCEPTED"


class AcceptanceError(RuntimeError):
    pass


def _inside(p: Path, root: Path) -> bool:
    try:
        return Path(os.path.normcase(str(p.resolve()))).is_relative_to(Path(os.path.normcase(str(root.resolve()))))
    except (OSError, ValueError):
        return False


def tree_fingerprint(root: Optional[Path]) -> dict:
    """Every file under ``root`` as (size, mtime_ns) plus the manifest's SHA-256: proves a folder was not written."""
    out = {"root": str(root or ""), "exists": bool(root and Path(root).is_dir()), "files": 0, "manifest_sha256": ""}
    if not out["exists"]:
        return out
    entries = []
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            p = Path(dp) / f
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append(f"{p.relative_to(root)}|{st.st_size}|{st.st_mtime_ns}")
    entries.sort()
    import hashlib

    out["files"] = len(entries)
    out["listing_sha256"] = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    m = Path(root) / "manifest.json"
    if m.is_file():
        out["manifest_sha256"] = sha256_file(m)
        out["manifest_mtime_ns"] = m.stat().st_mtime_ns
    return out


def acceptance_root(ctl) -> Path:
    return ctl.paths.root / "work" / "acceptance"


def isolation_for(ctl, package: str, project: str, out_dir: Path):
    """The scoped acceptance exception (verified before use; production commands never build one)."""
    from ..conversion.plan import ACCEPTANCE_COMMAND, AcceptanceIsolation, AcceptanceIsolationError

    prod = ctl.output_mod_path(None)
    iso = AcceptanceIsolation(command=ACCEPTANCE_COMMAND, project=project, source_mod=package, output_dir=str(out_dir),
                              acceptance_root=str(acceptance_root(ctl)), production_output_mod=str(prod or ""),
                              mods_dir=str(ctl.installation.mods_dir or ""), no_commit=True)
    try:
        iso.verify()
    except AcceptanceIsolationError as exc:
        raise AcceptanceError(str(exc)) from exc
    return iso


def existing_auto_files(ctl, project: str) -> list[str]:
    """The already existing generated AUTO project files of ``project`` (wherever MO2 or the output mod has them):
    never inputs of the acceptance; hashed before and after to prove they stay byte-identical."""
    from ..conversion.plan import generated_names
    from ..discovery.install import BODYSLIDE_SUBTREE

    inst = ctl.installation
    names = generated_names(project, inst.profile.conversion.project_suffix)
    rels = [names["osp_rel"], f"ShapeData/{names['shapedata_folder']}/{names['shapedata_file']}"]
    found: list[str] = []
    for rel in rels:
        vf = inst.vfs.resolve(rel) if inst.vfs else None
        if vf is not None:
            found.append(str(vf.abs_path))
        prod = ctl.output_mod_path(None)
        if prod is not None and (Path(prod) / BODYSLIDE_SUBTREE / rel).is_file():
            found.append(str(Path(prod) / BODYSLIDE_SUBTREE / rel))
    return sorted({str(Path(f)) for f in found})


def disposable_output(ctl, output: Optional[str]) -> Path:
    """The disposable output folder (created empty). Never the configured output mod, never inside MO2's mods."""
    inst = ctl.installation
    prod = ctl.output_mod_path(None)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = acceptance_root(ctl)
    p = Path(output) if output else root / f"cloth_{stamp}" / "DISPOSABLE_OUTPUT_MOD"
    if not _inside(p, root) or _inside(root, p):
        raise AcceptanceError(f"--output must be inside the converter-owned {root}")
    if prod is not None and (_inside(p, prod) or _inside(prod, p)):
        raise AcceptanceError(f"--output may not be (or contain, or be inside) the production output mod {prod}")
    if inst.mods_dir is not None and _inside(p, Path(inst.mods_dir)):
        raise AcceptanceError(f"--output may not be inside the MO2 mods folder {inst.mods_dir} (MO2 would load it)")
    if p.exists() and (not p.is_dir() or any(p.iterdir())):
        raise AcceptanceError(f"--output must be a new or empty folder: {p}")
    p.mkdir(parents=True, exist_ok=True)
    return p


def plan_one_project(ctl, package: str, project: str, out_dir: Path, iso=None):
    """A package preview holding exactly ``project``, planned from its original source against ``out_dir``."""
    from ..analysis.analyzer import LibraryAnalyzer
    from ..conversion.package import build_package_preview

    inst = ctl.installation
    an = LibraryAnalyzer(inst, cache=None, workers=None, project_filter=lambda mod, rel: mod == package)
    rep = an.run()
    mine = [p for p in rep.projects if p.mod == package]
    if not mine:
        raise AcceptanceError(f"package '{package}' provides no BodySlide project (exact MO2 mod name?)")
    hits = [p for p in mine if p.project == project]
    if not hits:
        near = sorted(p.project for p in mine if project.lower() in p.project.lower())[:10]
        raise AcceptanceError(f"no project named exactly '{project}' in '{package}'"
                              + (f"; similar: {', '.join(near)}" if near else ""))
    if len(hits) > 1:
        raise AcceptanceError(f"'{project}' names {len(hits)} slider sets in '{package}'; it is not exact")
    iso = iso or isolation_for(ctl, package, project, out_dir)
    # the input is bound to this ORIGINAL source project (hits[0]: package's own OSP); existing generated AUTO projects
    # are not inputs (scoped AcceptanceIsolation, verified again inside the planner)
    prev = build_package_preview(inst, ctl._knowledge_base(), package, hits, ctl.runtime_status(), out_dir,
                                 library=mine, acceptance=iso)
    pp = next(p for p in prev.projects if p.project == project)
    if not pp.plan:
        raise AcceptanceError(f"'{project}' cannot be planned from its source: {pp.status} — "
                              + "; ".join((pp.reasons or [pp.review_policy_reason or ""])[:2]))
    if not ((pp.selectable and pp.action == "CONVERT") or pp.review_policy == "REVIEW_ATTEMPTABLE"):
        raise AcceptanceError(f"'{project}' is not convertible from its source ({pp.action} {pp.status})")
    if not int(pp.plan.get("source_cloth_data") or 0):
        raise AcceptanceError(f"'{project}' has no BSClothExtraData in its source ShapeData: not a cloth acceptance fixture")
    for p in prev.projects:
        p.selected = p is pp
    prev.conversion_mode = "ALL_POSSIBLE"
    src = hits[0]
    prev.acceptance = {"kind": "cloth_extra_data", "command": iso.command, "project": project, "source_mod": src.mod,
                       "source_osp": src.osp_rel, "no_commit": True, "disposable_output": str(out_dir),
                       "production_output_mod": iso.production_output_mod}
    prev.notes.insert(0, "ACCEPTANCE (cloth data): one project, disposable output, never installed")
    return prev, pp


def _source_files(plan) -> list[dict]:
    return [{"role": sf.role, "rel": sf.rel, "path": sf.abs_path} for sf in plan.staged_files
            if not sf.role.startswith("reference")]


def _payloads(path: Path) -> list[dict]:
    from ..nif.reader import read_nif

    return [{"size": n, "sha256": h} for n, h in read_nif(Path(path)).cloth_sha256]


def run_cloth_acceptance(ctl, package: str, project: str, output: Optional[str] = None,
                         say: Optional[Callable[[str], None]] = None, runner=None) -> dict:
    """Run the targeted acceptance; returns the report (also written as acceptance_cloth.json/.txt)."""
    from ..conversion import win32_ui

    say = say or (lambda m: None)
    if ctl.installation is None:
        raise AcceptanceError("no installation detected")
    if runner is None and not win32_ui.available():
        raise AcceptanceError("the unattended Outfit Studio control (Win32) is not available here: acceptance needs Windows")
    rt = ctl.runtime_status()
    if not rt.headless_supported:
        raise AcceptanceError(f"Outfit Studio not usable: {rt.reason}")
    out_dir = disposable_output(ctl, output)
    prod = ctl.output_mod_path(None)
    say(f"Disposable output: {out_dir}")
    say(f"Planning '{project}' from its original source project…")
    iso = isolation_for(ctl, package, project, out_dir)
    autos = existing_auto_files(ctl, project)
    autos_before = {f: sha256_file(Path(f)) for f in autos}
    for f in autos:
        say(f"Existing generated project file (not an input, left untouched): {f}")
    prod_before = tree_fingerprint(prod)
    prev, pp = plan_one_project(ctl, package, project, out_dir, iso)
    plan = pp.conversion_plan()
    if not _inside(Path(plan.output_mod_path), out_dir) or plan.source_mod != package or plan.source_project != project:
        raise AcceptanceError("the acceptance plan is not bound to the original source and the disposable output")
    srcs = _source_files(plan)
    before = {s["path"]: sha256_file(Path(s["path"])) for s in srcs}
    src_nif = next((s["path"] for s in srcs if s["role"] == "source-nif"), "")
    eng = ctl._batch_engine(runner)
    bdir = eng.create(prev, say)  # batch.json carries "acceptance" from the start: commit() refuses it
    b = json.loads((bdir / "batch.json").read_text(encoding="utf-8"))
    if not b.get("acceptance"):
        raise AcceptanceError("the acceptance batch is not marked as never-installable")
    b["control_override"] = "unattended"
    (bdir / "batch.json").write_text(json.dumps(b, indent=1), encoding="utf-8")
    say(f"Acceptance batch: {bdir}")
    ctl._cancel.clear()
    stopped: list[dict] = []

    def no_manual(info: dict) -> str:  # acceptance = zero user interaction: never fall back to manual
        stopped.append(info)
        return "stop"

    eng.run(bdir, progress=say, cancel=ctl._cancel.is_set, on_session_done=None, control_failure=no_manual)
    after = {p: (sha256_file(Path(p)) if Path(p).is_file() else "MISSING") for p in before}
    prod_after = tree_fingerprint(prod)
    autos_after = {f: (sha256_file(Path(f)) if Path(f).is_file() else "MISSING") for f in autos}
    rep = build_report(bdir, project, srcs, before, after, prod_before, prod_after, src_nif, out_dir, stopped)
    rep["existing_auto_files"] = [{"path": f, "sha256_before": autos_before[f], "sha256_after": autos_after[f]}
                                  for f in autos]
    rep["existing_auto_unchanged"] = all(autos_before[f] == autos_after[f] for f in autos)
    if not rep["existing_auto_unchanged"]:
        rep["passed"] = False
        rep["verdict"] = "0.5.3 cloth acceptance: NOT PASSED (see report)"
    (bdir / "acceptance_cloth.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (bdir / "acceptance_cloth.txt").write_text("\n".join(report_lines(rep)) + "\n", encoding="utf-8")
    (out_dir / "acceptance_cloth.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    return rep


def build_report(bdir: Path, project: str, srcs, before, after, prod_before, prod_after, src_nif, out_dir,
                 stopped) -> dict:
    b = json.loads((bdir / "batch.json").read_text(encoding="utf-8"))
    e = next(x for x in b["projects"] if x["project"] == project)
    pdir = bdir / "projects" / e["id"]
    res = json.loads((pdir / "result.json").read_text(encoding="utf-8")) if (pdir / "result.json").is_file() else {}
    sessions = [s for s in b.get("sessions", []) if e["id"] in s.get("projects", [])]
    sr: dict = {}
    if sessions:
        f = bdir / "logs" / f"session_result.session{sessions[-1]['id']}.json"
        if f.is_file():
            sr = json.loads(f.read_text(encoding="utf-8"))
    cloth_recs = [c for c in sr.get("cloth", []) if c.get("project") == project]
    c = cloth_recs[-1] if cloth_recs else {}
    plan = json.loads((pdir / "plan.json").read_text(encoding="utf-8"))
    from ..discovery.install import BODYSLIDE_SUBTREE

    gen = bdir / "Data" / BODYSLIDE_SUBTREE / "ShapeData" / plan.get("output_shapedata_folder", "") / \
        plan.get("output_shapedata_file", "")
    try:
        src_pl = _payloads(Path(src_nif)) if src_nif else []
    except Exception as exc:  # noqa: BLE001
        src_pl = [{"error": str(exc)}]
    try:
        gen_pl = _payloads(gen) if gen.is_file() else []
    except Exception as exc:  # noqa: BLE001
        gen_pl = [{"error": str(exc)}]
    checks = (res.get("validation") or {}).get("checks", [])
    unchanged = all(before[p] == after.get(p) for p in before) and bool(before)
    prod_same = {k: v for k, v in prod_before.items()} == {k: v for k, v in prod_after.items()}
    dialogs = sr.get("dialogs", [])
    manual = any(x.get("choice") in ("manual",) for x in stopped) or sr.get("control") == "manual"
    payload_match = bool(src_pl) and src_pl == gen_pl and not any("error" in x for x in src_pl + gen_pl)
    cx = {
        "encountered": bool(c),
        "offered_origins": c.get("offered_origins", len(c.get("origins") or [])) if c else 0,
        "origins": c.get("origins", []),
        "selected_origin": (c.get("selected_origins") or [""])[0] if c else "",
        "selection_policy": c.get("policy", ""),
        "verification": c.get("verification", ""),
        "answer": c.get("action", ""),
        "source_payload_size": [x.get("size") for x in src_pl],
        "generated_payload_size": [x.get("size") for x in gen_pl],
        "source_payload_sha256": [x.get("sha256") for x in src_pl],
        "generated_payload_sha256": [x.get("sha256") for x in gen_pl],
        "payload_match": payload_match,
    }
    rep = {
        "acceptance": "0.5.3 cloth data (Choose cloth data / BSClothExtraData)",
        "project": project, "batch": str(bdir), "disposable_output": str(out_dir),
        "session_status": sr.get("status", ""), "session_reason": sr.get("reason", ""),
        "control": sr.get("control", b.get("sessions", [{}])[-1].get("control", "") if b.get("sessions") else ""),
        "dialog_appeared": bool(c),
        "dialog": c.get("dialog", {}),
        "dialog_closed": bool((c.get("dialog") or {}).get("closed_after_confirmation")),
        "dialogs_answered": [{k: d.get(k) for k in ("title", "answer", "buttons", "phase") if k in d} for d in dialogs],
        "zero_user_interaction": bool(c) and not manual and not stopped and sr.get("status") == "COMPLETED",
        "cloth_extra_data": cx,
        "project_status": res.get("status", e.get("status", "")),
        "validation_passed": bool((res.get("validation") or {}).get("passed")),
        "validation_checks": [{"name": k["name"], "ok": k["ok"], "detail": k.get("detail", "")} for k in checks],
        "reasons": res.get("reasons", []),
        "generated_nif": str(gen),
        "source_files": [{**s, "sha256_before": before.get(s["path"]), "sha256_after": after.get(s["path"])}
                         for s in srcs],
        "sources_unchanged": unchanged,
        "production_output_mod": {"before": prod_before, "after": prod_after, "untouched": prod_same},
        "committed": False,
        "control_failures": stopped,
    }
    rep["passed"] = bool(rep["dialog_appeared"] and rep["dialog_closed"] and rep["zero_user_interaction"]
                         and cx["selection_policy"] in ("single_verified_origin_preserved", "equivalent_origins_own_selected")
                         and cx["answer"] == "ok" and payload_match and rep["project_status"] == "VALIDATED"
                         and rep["validation_passed"] and unchanged and prod_same)
    rep["verdict"] = ACCEPTED_TEXT if rep["passed"] else "0.5.3 cloth acceptance: NOT PASSED (see report)"
    return rep


def report_lines(rep: dict) -> list[str]:
    cx = rep["cloth_extra_data"]
    d = rep.get("dialog") or {}
    L = [f"=== {rep['acceptance']} ===", f"project: {rep['project']}", f"batch: {rep['batch']}",
         f"disposable output: {rep['disposable_output']}", "",
         f" 1. Choose cloth data appeared: {rep['dialog_appeared']}",
         f" 2. dialog: title '{d.get('title', '')}', class {d.get('class', '')}"]
    for k in d.get("controls", []):
        L.append(f"      {k.get('class')} id={k.get('id')} parent={k.get('parent')} "
                 f"{'visible' if k.get('visible') else 'hidden'} {'enabled' if k.get('enabled') else 'disabled'} "
                 f"text='{(k.get('text') or '')[:120]}'")
    for s in d.get("selection_controls", []):
        L.append(f"      selection control: {s.get('class')} id={s.get('id')} style={s.get('style')} items={s.get('items')}")
    for btn in d.get("buttons", []):
        L.append(f"      button id={btn.get('id')} text='{btn.get('text')}' enabled={btn.get('enabled')}")
    L += [f" 3. origins offered ({cx['offered_origins']}): " + "; ".join(cx["origins"]),
          f" 4. origin selected: {cx['selected_origin']}",
          f" 5. policy: {cx['selection_policy']} — {cx['verification']}",
          f" 6. zero user interaction: {rep['zero_user_interaction']} (session {rep['session_status']}, "
          f"control {rep['control']}); dialog closed after confirmation: {rep['dialog_closed']}",
          f" 7. source payload: {cx['source_payload_size']} sha256 {cx['source_payload_sha256']}",
          f" 8. generated payload: {cx['generated_payload_size']} sha256 {cx['generated_payload_sha256']}",
          f" 9. payload_match: {cx['payload_match']}",
          f"10. validation: {rep['project_status']} (passed {rep['validation_passed']}, "
          f"{sum(1 for c in rep['validation_checks'] if c['ok'])}/{len(rep['validation_checks'])} checks)"]
    for c in rep["validation_checks"]:
        if not c["ok"] or c["name"].startswith("cloth"):
            L.append(f"      [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    L.append(f"11. sources unchanged: {rep['sources_unchanged']}")
    for s in rep["source_files"]:
        L.append(f"      {s['role']} {s['rel']}: {s['sha256_before']} -> {s['sha256_after']}")
    po = rep["production_output_mod"]
    L += [f"12. disposable output: {rep['disposable_output']} (generated files in {rep['batch']}\\Data)",
          f"13. production output mod untouched: {po['untouched']} ({po['before'].get('files')} files, manifest "
          f"sha256 {po['before'].get('manifest_sha256', '')[:16]}… before = after); committed: {rep['committed']}",
          f"    existing generated AUTO files unchanged: {rep.get('existing_auto_unchanged', True)}"]
    for f in rep.get("existing_auto_files", []):
        L.append(f"      {f['path']}: {f['sha256_before']} -> {f['sha256_after']}")
    L += ["", f"RESULT: {rep['verdict']}"]
    if rep["reasons"]:
        L.append("reasons: " + "; ".join(rep["reasons"][:4]))
    if rep.get("session_reason"):
        L.append("session: " + rep["session_reason"])
    return L
