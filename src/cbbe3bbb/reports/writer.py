"""Report writers: ``latest.json``, ``latest.csv`` and ``latest.html``.

Reports only describe what the analyzer found; writing them never touches the
scanned installation. Each run also keeps a timestamped JSON copy so that
earlier analyses can be compared.
"""
from __future__ import annotations

import csv
import html
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from ..analysis.analyzer import ScanReport
from ..analysis.models import ProjectResult

__all__ = [
    "TABLE_COLUMNS",
    "FILTERS",
    "row_for",
    "matches_filter",
    "write_reports",
    "render_html",
    "render_csv",
    "load_report",
]

# Column order for the GUI table and the HTML report (spec: Mod, Project, Body, Breast, Butt, 3BBB, Confidence, Action)
TABLE_COLUMNS = ["Mod", "Project", "Body", "Breast", "Butt", "3BBB", "Confidence", "Action"]

# GUI / HTML filters. The key is the label; the predicate works on a ProjectResult.
FILTERS = ["All", "Convert candidates", "Already 3BBB", "Review", "Unsupported", "Errors"]

CSV_COLUMNS = [
    "action", "mod", "project", "body_family", "physics", "breast", "butt", "thigh", "existing_3bbb",
    "confidence", "reasons", "warnings", "errors", "provider_kind", "osp_rel", "set_index", "shapedata_rel",
    "shapedata_provider", "output", "sliders", "zaps", "shapes", "superseded_by", "same_output_projects",
    "osp_also_provided_by", "osp_path",
]


def row_for(pr: ProjectResult) -> list[str]:
    return [pr.mod, pr.project, pr.family.label, pr.breast, pr.butt, pr.existing_3bbb, pr.geometry_confidence, pr.action]


def matches_filter(pr: ProjectResult, name: str) -> bool:
    if name in ("", "All"):
        return True
    if name == "Convert candidates":
        return pr.action == "CONVERT"
    if name == "Already 3BBB":
        return pr.family.physics == "3bbb" or pr.existing_3bbb in ("YES", "PARTIAL")
    if name == "Review":
        return pr.action == "REVIEW"
    if name == "Unsupported":
        return pr.action == "UNSUPPORTED"
    if name == "Errors":
        return pr.action == "ERROR" or bool(pr.errors)
    if name == "Skipped":
        return pr.action == "SKIP"
    if name == "Keep":
        return pr.action == "KEEP"
    return True


def _csv_record(pr: ProjectResult) -> dict[str, str]:
    output = "\\".join(x for x in (pr.output_path, pr.output_file) if x)
    return {
        "action": pr.action,
        "mod": pr.mod,
        "project": pr.project,
        "body_family": pr.family.label,
        "physics": pr.family.physics,
        "breast": pr.breast,
        "butt": pr.butt,
        "thigh": pr.thigh,
        "existing_3bbb": pr.existing_3bbb,
        "confidence": pr.geometry_confidence,
        "reasons": " | ".join(pr.reasons),
        "warnings": " | ".join(pr.warnings),
        "errors": " | ".join(pr.errors),
        "provider_kind": pr.provider_kind,
        "osp_rel": pr.osp_rel,
        "set_index": str(pr.set_index),
        "shapedata_rel": pr.shapedata_rel,
        "shapedata_provider": pr.shapedata_provider,
        "output": output,
        "sliders": str(pr.slider_count),
        "zaps": str(pr.zap_count),
        "shapes": "; ".join(pr.shape_names),
        "superseded_by": pr.superseded_by,
        "same_output_projects": "; ".join(pr.same_output_projects),
        "osp_also_provided_by": "; ".join(pr.osp_also_provided_by),
        "osp_path": pr.osp_path,
    }


def _sorted(projects: Iterable[ProjectResult]) -> list[ProjectResult]:
    return sorted(projects, key=lambda p: (p.mod.lower(), p.project.lower()))


def render_csv(rep: ScanReport) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    for pr in _sorted(rep.projects):
        w.writerow(_csv_record(pr))
    return buf.getvalue()


def _atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_reports(rep: ScanReport, out_dir: Path, keep_history: bool = True) -> dict[str, Path]:
    """Write latest.json/csv/html (and a timestamped JSON copy) into ``out_dir``."""
    out_dir = Path(out_dir)
    data = json.dumps(rep.to_dict(), indent=1, ensure_ascii=False)
    paths = {
        "json": out_dir / "latest.json",
        "csv": out_dir / "latest.csv",
        "html": out_dir / "latest.html",
    }
    _atomic_write(paths["json"], data)
    # utf-8-sig so Excel opens umlauts etc. correctly
    _atomic_write(paths["csv"], render_csv(rep), encoding="utf-8-sig")
    _atomic_write(paths["html"], render_html(rep))
    if keep_history:
        stamp = (rep.started or "run").replace(":", "").replace("-", "").replace("T", "_").replace(" ", "_")[:15]
        hist = out_dir / "history" / f"report_{stamp}.json"
        _atomic_write(hist, data)
        paths["history"] = hist
    return paths


def load_report(path: Path) -> ScanReport:
    return ScanReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1f;--muted:#666;--line:#ddd;--card:#fff;
--convert:#0b6bcb;--keep:#1d8a46;--review:#b26b00;--skip:#777;--unsupported:#7a4bb3;--error:#c62828}
@media (prefers-color-scheme: dark){:root{--bg:#18181a;--fg:#e8e8e8;--muted:#9a9a9a;--line:#333;--card:#222226;
--convert:#5aa9ff;--keep:#5bd08a;--review:#ffb74d;--skip:#9a9a9a;--unsupported:#b894f0;--error:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:14px/1.45 "Segoe UI",system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:20px 0 8px}
.muted{color:var(--muted)}.banner{border:1px solid var(--line);background:var(--card);padding:8px 12px;border-radius:6px;margin:10px 0}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.stat{background:var(--card);border:1px solid var(--line);
border-radius:6px;padding:6px 12px;min-width:90px}.stat b{display:block;font-size:18px}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0}
select,input{font:inherit;padding:4px 6px;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:4px}
.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--card)}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
th{position:sticky;top:0;background:var(--card);cursor:pointer;white-space:nowrap}
tr.main{cursor:pointer}tr.main:hover{background:rgba(127,127,127,.08)}
tr.det td{background:rgba(127,127,127,.05);font-size:13px}tr.det{display:none}tr.det.open{display:table-row}
.a{font-weight:600}.a-CONVERT{color:var(--convert)}.a-KEEP{color:var(--keep)}.a-REVIEW{color:var(--review)}
.a-SKIP{color:var(--skip)}.a-UNSUPPORTED{color:var(--unsupported)}.a-ERROR{color:var(--error)}
ul{margin:2px 0 6px 18px;padding:0}code{font-size:12px;word-break:break-all}
dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:0}dl.kv dt{color:var(--muted)}
"""

_JS = """
const q=document.getElementById('q'),f=document.getElementById('f'),rows=[...document.querySelectorAll('tr.main')];
function apply(){const s=q.value.toLowerCase(),fv=f.value;let n=0;for(const r of rows){
const ok=(fv==='All'||r.dataset.filters.split('|').includes(fv))&&(!s||r.dataset.text.includes(s));
r.style.display=ok?'':'none';const d=r.nextElementSibling;if(!ok)d.classList.remove('open');if(ok)n++}
document.getElementById('count').textContent=n+' shown'}
q.addEventListener('input',apply);f.addEventListener('change',apply);
for(const r of rows)r.addEventListener('click',()=>r.nextElementSibling.classList.toggle('open'));
document.querySelectorAll('th').forEach((th,i)=>th.addEventListener('click',()=>{const tb=th.closest('table').tBodies[0];
const pairs=rows.map(r=>[r,r.nextElementSibling]);const dir=th.dataset.dir==='a'?'d':'a';th.dataset.dir=dir;
pairs.sort((x,y)=>{const a=x[0].cells[i].textContent,b=y[0].cells[i].textContent;return dir==='a'?a.localeCompare(b):b.localeCompare(a)});
for(const [r,d] of pairs){tb.appendChild(r);tb.appendChild(d)}}));
apply();
"""


def _e(x: object) -> str:
    return html.escape(str(x), quote=True)


def _ul(items: list[str]) -> str:
    if not items:
        return ""
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"


def _details(pr: ProjectResult) -> str:
    parts = []
    parts.append("<b>Reasons</b>" + _ul(pr.reasons))
    if pr.warnings:
        parts.append("<b>Warnings</b>" + _ul(pr.warnings))
    if pr.errors:
        parts.append("<b>Errors</b>" + _ul(pr.errors))
    kv = [
        ("Project file", f"{pr.osp_rel} (set #{pr.set_index}) — {pr.provider_kind}"),
        ("ShapeData", f"{pr.shapedata_rel} [{pr.shapedata_provider}]" if pr.shapedata_rel else "—"),
        ("Output", "\\".join(x for x in (pr.output_path, pr.output_file) if x) or "—"),
        ("Family evidence", "; ".join(pr.family.evidence[:4]) or "—"),
        ("Sliders / zaps", f"{pr.slider_count} / {pr.zap_count}"),
        ("Thigh", pr.thigh),
    ]
    if pr.superseded_by:
        kv.append(("Superseded by", pr.superseded_by))
    if pr.same_output_projects:
        kv.append(("Same output as", "; ".join(pr.same_output_projects[:6])))
    if pr.osp_also_provided_by:
        kv.append(("Project file also in (overridden)", "; ".join(pr.osp_also_provided_by)))
    parts.append("<dl class='kv'>" + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in kv) + "</dl>")
    shp = []
    for s in pr.shapes:
        tag = " (body)" if s.is_body else ""
        regs = []
        for r, m in s.regions.items():
            if m.relevance in ("NONE",) and not m.existing_vertices:
                continue
            txt = f"{r.lower()}: relevance {m.relevance}, fit {m.fit}, predicted {m.predicted_vertices} v, coverage {m.reference_coverage:.0%}"
            if m.existing_vertices:
                txt += f", existing 3BBB {m.existing_vertices} v"
                if m.agreement is not None:
                    txt += f", agreement {m.agreement:.2f}"
            regs.append(txt)
        pb = f", 3BBB bones {len(s.physics_bones)}" if s.physics_bones else ""
        shp.append(
            f"<li><b>{_e(s.name)}</b>{_e(tag)} — {s.vertices} verts, median dist {s.distance.median:.2f}, "
            f"p90 {s.distance.p90:.2f}{_e(pb)}{_ul(regs)}</li>"
        )
    if shp:
        parts.append("<b>Shapes</b><ul>" + "".join(shp) + "</ul>")
    return "".join(parts)


def render_html(rep: ScanReport) -> str:
    inst = rep.installation or {}
    stats = rep.stats or {}
    by_action = stats.get("actions", {})
    order = ["CONVERT", "REVIEW", "KEEP", "SKIP", "UNSUPPORTED", "ERROR"]
    stat_html = "".join(
        f"<div class='stat'><span class='a a-{a}'>{a}</span><b>{by_action.get(a, 0)}</b></div>" for a in order
    )
    stat_html = f"<div class='stat'>Projects<b>{len(rep.projects)}</b></div>" + stat_html
    info = [
        ("Game", inst.get("game_dir")),
        ("MO2", f"{inst.get('mo2_dir')} (profile {inst.get('mo2_profile')})"),
        ("BodySlide", f"{inst.get('bodyslide_dir')} v{inst.get('bodyslide_version')}"),
        ("Reference", rep.reference.get("template") if rep.reference else inst.get("reference_template")),
        ("Scan", f"{rep.started} · {rep.elapsed_s:.1f}s{' · CANCELLED (partial)' if rep.cancelled else ''}"),
        ("Tool", f"{rep.tool_version} / analyzer {rep.analyzer_version}"),
    ]
    info_html = "<dl class='kv'>" + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in info) + "</dl>"
    notes = _ul(list(rep.notes) + list(inst.get("warnings", [])))
    head = "".join(f"<th>{_e(c)}</th>" for c in TABLE_COLUMNS)
    body = []
    for pr in _sorted(rep.projects):
        flt = "|".join(f for f in FILTERS if matches_filter(pr, f))
        text = " ".join([pr.mod, pr.project, pr.family.label, pr.action, *pr.reasons]).lower()
        cells = row_for(pr)
        tds = "".join(f"<td>{_e(c)}</td>" for c in cells[:-1]) + f"<td class='a a-{_e(pr.action)}'>{_e(pr.action)}</td>"
        body.append(f"<tr class='main' data-filters='{_e(flt)}' data-text='{_e(text)}'>{tds}</tr>")
        body.append(f"<tr class='det'><td colspan='{len(TABLE_COLUMNS)}'>{_details(pr)}</td></tr>")
    options = "".join(f"<option>{_e(f)}</option>" for f in FILTERS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CBBE3BBB Analysis Report</title><style>{_CSS}</style></head><body>
<h1>CBBE → 3BBB library analysis</h1>
<div class="banner"><b>PHASE 1 IS AN ANALYZER. IT DOES NOT MODIFY MODS.</b>
<span class="muted">Actions are recommendations only; nothing was converted.</span></div>
{info_html}
<div class="stats">{stat_html}</div>
{('<h2>Notes</h2>' + notes) if notes else ''}
<div class="controls"><label>Filter <select id="f">{options}</select></label>
<input id="q" type="search" placeholder="Search mod, project, reason…" size="36"><span id="count" class="muted"></span>
<span class="muted">Click a row for details · click a header to sort</span></div>
<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>
{''.join(body)}
</tbody></table></div>
<script>{_JS}</script></body></html>
"""
