"""Phase 3.2 (Part R): one portable diagnostic report for bug reports (``Export Diagnostic Report``).

The ZIP holds what a developer needs to understand a conversion problem without access to the user's PC: tool
version, relevant settings, the package classification, the batch state and sessions, and for every project that
did not convert its result, validation log and Outfit Studio log, plus the end of the tool log.

Privacy: no plugin, mesh or texture file is included (only JSON/text written by the tool), and every path is
anonymised: the MO2 folder, the game folder, the tool folder and the user's home become ``<MO2>``, ``<GAME>``,
``<TOOL>`` and ``<HOME>``; any remaining ``…\\Users\\<name>`` / ``/home/<name>`` is replaced by ``<USER>``.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

from .. import __version__

__all__ = ["export_diagnostic_report", "Anonymizer"]

MAX_LOG_LINES = 3000
_PROBLEM_STATES = ("REVIEW", "FAILED", "PENDING", "ATTEMPTING", "GENERATED", "PLANNED")


class Anonymizer:
    def __init__(self, roots: dict[str, Optional[str | Path]]):
        pairs = []
        for label, p in roots.items():
            if not p:
                continue
            s = str(p).rstrip("\\/")
            if len(s) < 4:
                continue
            for v in {s, s.replace("\\", "/"), s.replace("/", "\\"), s.replace("\\", "\\\\")}:
                pairs.append((v, f"<{label}>"))
        self.pairs = sorted(pairs, key=lambda x: -len(x[0]))
        self._user = [re.compile(r"(?i)([a-z]:[\\/]+users[\\/]+)[^\\/\"'<>|\r\n]+"),
                      re.compile(r"(/home/|/Users/)[^/\"'<>|\r\n]+")]

    def __call__(self, text: str) -> str:
        for a, b in self.pairs:
            text = re.sub(re.escape(a), lambda _m, _b=b: _b, text, flags=re.I)
        for rx in self._user:
            text = rx.sub(lambda m: m.group(1) + "<USER>", text)
        return text


def _read(p: Path) -> Optional[str]:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def export_diagnostic_report(ctl, out_path: Optional[Path] = None, batch: Optional[str | Path] = None,
                             result=None, preview=None) -> Path:
    """Write the ZIP and return its path (default ``reports/diagnostics/cbbe3bbb_diagnostics_<stamp>.zip``)."""
    inst = getattr(ctl, "installation", None)
    prof = inst.profile if inst else None
    anon = Anonymizer({"MO2": getattr(prof, "mo2_dir", "") if prof else "",
                       "GAME": str(inst.game_dir) if inst and inst.game_dir else "",
                       "TOOL": str(ctl.paths.root), "HOME": os.path.expanduser("~")})
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(out_path) if out_path else ctl.paths.reports / "diagnostics" / f"cbbe3bbb_diagnostics_{stamp}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    def put(name: str, text: Optional[str]) -> None:
        if text is not None:
            files[name] = anon(text)

    about = {"tool": "CBBE3BBBConverter", "version": __version__, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "python": sys.version.split()[0], "platform": platform.platform(terse=True),
             "outfit_studio": {}, "profile": {}}
    if prof is not None:
        from dataclasses import asdict

        d = asdict(prof)
        about["profile"] = {"conversion": d.get("conversion", {}), "analysis": d.get("analysis", {}),
                            "mo2_profile": d.get("mo2_profile", ""), "mods_dir_set": bool(d.get("mods_dir"))}
    try:
        about["outfit_studio"] = ctl.runtime_status().to_dict()
    except Exception as exc:  # noqa: BLE001
        about["outfit_studio"] = {"error": str(exc)}
    if inst is not None:
        about["installation"] = {"mo2_profile": inst.mo2_profile, "mods": len(inst.mods),
                                 "reference": inst.reference_template.display_name if inst.reference_template else "",
                                 "bodyslide_version": inst.bodyslide_version, "warnings": inst.warnings[:20],
                                 "errors": inst.errors[:20]}
    put("about.json", json.dumps(about, indent=1, default=str))
    prev = preview if preview is not None else getattr(ctl, "preview", None)
    if prev is not None:
        rows = [{"project": p.project, "action": p.action, "status": p.status, "confidence": p.confidence,
                 "review_policy": p.review_policy, "bucket": p.bucket, "selected": p.selected, "regions": p.regions,
                 "reasons": p.reasons[:8], "warnings": p.warnings[:6], "review_policy_reason": p.review_policy_reason}
                for p in prev.projects]
        put("package_classification.json", json.dumps({"package": prev.package, "counts": prev.counts,
                                                       "output_mod_state": prev.output_mod_state, "codes": prev.codes,
                                                       "notes": prev.notes, "projects": rows}, indent=1))
    if result is not None:
        put("result.json", json.dumps(result.to_dict(), indent=1, default=str))
    try:  # 0.5.2: the user's in-game verification reports (manifest acceptance FAIL / PARTIAL)
        from .pipeline import runtime_reports

        om = ctl.output_mod_path() if inst is not None else None
        rt = runtime_reports(str(om)) if om else {}
        if rt:
            put("runtime_issues.json", json.dumps(rt, indent=1))
    except Exception:  # noqa: BLE001 - diagnostics must still be written
        pass
    bdir = ctl.resolve_batch(batch) if batch else (Path(result.batch_dir) if result is not None and result.batch_dir else None)
    if bdir and (bdir / "batch.json").is_file():
        b = json.loads((bdir / "batch.json").read_text(encoding="utf-8"))
        for s in b.get("sessions", []):
            s.pop("before", None)
        put(f"batch/{bdir.name}/batch.json", json.dumps(b, indent=1))
        for n in ("summary.json", "report.txt"):
            put(f"batch/{bdir.name}/{n}", _read(bdir / n))
        for f in sorted((bdir / "logs").glob("*.txt")) if (bdir / "logs").is_dir() else []:
            put(f"batch/{bdir.name}/logs/{f.name}", _read(f))
        # 0.5.3: the unattended Outfit Studio control: one completion record per session (script hash, process,
        # timestamps, dialogs and how they were answered, cloth-data decisions, crashes/forced closes, fallbacks).
        # Screenshots of unexpected dialogs stay in the batch folder (logs/os_control): images are not anonymised.
        for f in sorted((bdir / "logs").glob("session_result.session*.json")) if (bdir / "logs").is_dir() else []:
            put(f"batch/{bdir.name}/logs/{f.name}", _read(f))
        shots = sorted((bdir / "logs" / "os_control").glob("*.png")) if (bdir / "logs" / "os_control").is_dir() else []
        if shots:
            put(f"batch/{bdir.name}/logs/os_control_screenshots.txt",
                "\n".join(f"{x.name} ({x.stat().st_size} bytes, kept in the batch folder, not included)" for x in shots))
        for e in b.get("projects", []):
            if e.get("status") not in _PROBLEM_STATES and not e.get("commit_error"):
                continue
            pdir = bdir / "projects" / e["id"]
            for rel in ("result.json", "result.revalidated.json", "plan.json", "logs/validation.txt",
                        "logs/validation.revalidated.txt", "logs/Log_OS.txt"):
                put(f"batch/{bdir.name}/projects/{e['id']}/{rel}", _read(pdir / rel))
    logf = ctl.paths.logs / "app.log"
    text = _read(logf)
    if text is not None:
        put("tool_log_tail.txt", "\n".join(text.splitlines()[-MAX_LOG_LINES:]))
    files["README.txt"] = ("CBBE3BBBConverter diagnostic report\n\nAttach this ZIP to a bug report. It contains only "
                           "text written by the converter (settings, classifications, validation logs, Outfit Studio "
                           "logs). Paths are anonymised (<MO2>, <GAME>, <TOOL>, <HOME>, <USER>); no mod files are "
                           "included.\n")
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files.items()):
            z.writestr(name, data)
    return out
