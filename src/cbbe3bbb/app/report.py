"""Phase 3.2 (Parts I/J/K/L): the package report of a mass conversion (text + JSON, written to reports/packages)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from ..conversion.outcomes import BUCKETS
from .messages import OUTCOME_LABELS

__all__ = ["package_report_text", "write_package_report", "BUCKET_LABELS"]

BUCKET_LABELS = {
    "SAFE_AUTO_DONE": "converted automatically (validated; installed or already installed)",
    "MEDIUM_NOT_AUTO": "convertible, but confidence below HIGH: not converted automatically",
    "REVIEW_COMPLEX_GEOMETRY": "complex/loose geometry or a result that did not validate",
    "BLOCKED_WEIGHT_TRANSFER": "no safe weight-transfer radius for part of the outfit",
    "EXISTING_3BBB_AMBIGUOUS": "existing or ambiguous 3BBB weighting / earlier conversion: left as it is",
    "NO_PHYSICS_NEEDED": "no breast/butt/thigh region under the item: nothing to convert",
    "FAILED_TECHNICAL": "technical failure (files, Outfit Studio, validation infrastructure)",
    "ALREADY_COMPATIBLE": "already 3BBB (native)",
    "UNSUPPORTED_BODY": "made for another body",
}


def package_report_text(r, verify: Optional[dict] = None) -> str:
    L = [f"PACKAGE REPORT — {r.package}", f"mode {r.mode} · result {r.state} ({r.state_label}) · batch {r.batch_id or '—'} "
         f"({r.batch_state or '—'}) · {r.finished}", ""]
    L += [f"Completed: {len(r.completed)} · No conversion needed: {r.counts.get('NO_CONVERSION_NEEDED', 0)} · "
          f"Needs attention: {len(r.needs_attention)}"]
    if r.needs_attention:
        L += ["", "Needs attention / original kept:"] + [f"  {x}" for x in r.attention_lines()]
    rt = [i for i in r.items if i.get("runtime_issue")]
    if rt:
        L += ["", "In-game verification reports (the automated validation result is unchanged):"]
        L += [f"  {i['runtime_issue']['state']:<8} {i['name']} ({i['runtime_issue'].get('timestamp', '')})"
              + (f": {i['runtime_issue']['note']}" if i['runtime_issue'].get('note') else "")
              + f" — automated: {i['batch_status'] or i['status'] or i['outcome']}" for i in rt]
    L += [""]
    L.append("Outcomes (automated validation):")
    for k, v in sorted(r.counts.items(), key=lambda kv: -kv[1]):
        L.append(f"  {OUTCOME_LABELS.get(k, k):<28}{v:>5}")
    if r.percent is not None:
        L.append(f"  {r.percent}% of applicable outfits converted")
    if r.system_failure:
        L += ["", f"SYSTEM FAILURE: {r.system_failure}", f"  {r.system_message}"]
    for w in r.warnings:
        L += ["", f"WARNING {w['code']}: {w['message']}"]
    L += ["", "Buckets:"]
    for b in BUCKETS:
        names = r.buckets.get(b, [])
        if not names:
            continue
        L.append(f"  {b:<26}{len(names):>4}  {BUCKET_LABELS.get(b, '')}")
    for b in BUCKETS:
        items = [i for i in r.items if i["bucket"] == b]
        if not items or b == "NO_PHYSICS_NEEDED":
            continue
        L += ["", f"[{b}]"]
        for i in items:
            extra = f" — {i['message']}" if i["message"] else ""
            origin = f" (via {i['origin']})" if i.get("origin") not in ("", "SAFE", None) and i["outcome"] == "CONVERTED" else ""
            L.append(f"  {i['outcome']:<24} {i['name']}{origin}{extra}")
            if i["outcome"] in ("COULD_NOT_CONVERT_SAFELY", "FAILED", "UNFINISHED"):
                L += [f"      · {t[:220]}" for t in i["technical"][:3]]
    nn = [i["name"] for i in r.items if i["bucket"] == "NO_PHYSICS_NEEDED"]
    if nn:
        L += ["", f"[NO_PHYSICS_NEEDED] {', '.join(nn)}"]
    b = r.build
    if b.get("generated_projects"):
        L += ["", "BodySlide build plan (manual, separate stage):",
              f"  output mod state in MO2: {b.get('output_mod_state') or 'unknown'}"
              + ("  ← enable it first" if b.get("output_mod_state") not in ("ENABLED", "", None) else ""),
              f"  Build only: '{b['group']}' ({len(b['generated_projects'])} AUTO projects; not the main group "
              f"'{b['main_group']}', not a text filter)",
              f"  Preset: {b['preset']} · Build Morphs: {'ON' if b['build_morphs'] else 'OFF'}",
              f"  {b.get('warning') or b['order']}",
              "  Batch Build → Build; then run verify-package-build."]
    if r.representative:
        L += ["", "Test these in game (representative sample):"]
        L += [f"  • {x['name']} — {x['why']} ({', '.join(y.lower() for y in x['regions'])})" for x in r.representative]
    if verify:
        L += ["", f"Package build check: {'OK' if verify.get('ok') else 'NOT OK'} · "
              + ", ".join(f"{k} {v}" for k, v in sorted(verify.get("counts", {}).items()))
              + f" · output mod {verify.get('output_mod_state')}"]
        for g, d in verify.get("groups", {}).items():
            L.append(f"  group '{g}': {'present' if d['exists'] else 'MISSING'}"
                     + (f", missing {len(d['missing'])}: {', '.join(d['missing'][:5])}" if d["missing"] else ""))
        bad = [row for row in verify.get("rows", []) if row["status"] != "BUILD_OK"]
        for row in bad[:12]:
            L.append(f"  {row['status']:<14} {row['generated_project']}: {'; '.join(row['problems'][:2])}")
        if len(bad) > 12:
            L.append(f"  … {len(bad) - 12} more not OK (see the JSON report)")
    return "\n".join(L) + "\n"


def write_package_report(r, folder: Path, verify: Optional[dict] = None) -> dict[str, Path]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", r.package).strip("._")[:60] or "package"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    txt = folder / f"{safe}_{stamp}_package_report.txt"
    js = folder / f"{safe}_{stamp}_package_report.json"
    txt.write_text(package_report_text(r, verify), encoding="utf-8")
    d = r.to_dict()
    if verify:
        d["verify_build"] = verify
    js.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
    return {"txt": txt, "json": js}
