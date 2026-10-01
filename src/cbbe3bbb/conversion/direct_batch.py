"""Phase 4.1: controlled Direct-NIF conversion (the first production write path for DIRECT_NIF packages).

Workflow (every stage explicit; there is no one-command analyze+convert+commit):

1. PLAN (``plan-direct-batch``, read-only): the fresh read-only Direct-NIF analysis (0.4.0) plus an explicit
   selection. A selector resolves to exactly one analyzed game mesh: a stable Direct-NIF ID (``dn-…``), the
   game-relative path, or a file name that is unique in the package (an ambiguous name is refused). Only
   ``CONVERT`` + ``HIGH`` + ``FEMALE_CBBE_COMPATIBLE`` + ``DIRECT_NIF_HIGH_CONFIDENCE`` meshes without cloth data,
   embedded body, body slot (33) or structural issues can be selected. There is no override.
2. CONVERT TO WORKSPACE (``convert-direct-batch``): ``work/direct_nif_batches/<stamp>_<package>/``. The current
   MO2 winner is resolved and hashed again (a different winner or file → REVIEW ``SOURCE_WINNER_CHANGED`` /
   ``SOURCE_CHANGED``), copied into the workspace, and only the copy is opened by Outfit Studio:
   ClearProject → LoadReference (CBBE-3BBB) → ImportFile (the copy) → CopyBoneWeights on the planned shapes only
   (radius 5 / 4 targets / no-target-limit off; the 0.3.3 per-shape adaptive radius) → ExportFile to an ABSOLUTE
   path inside the workspace with ``WithRef=false`` (the reference body is never exported). No SaveProject, no
   BodySlide project.
3. VALIDATE: every mesh on its own (VALIDATED / REVIEW / FAILED); the generated NIF is re-parsed independently
   and compared with the source copy: shape set and names, geometry and every vertex attribute but skinning,
   triangles, transforms, segments, materials/textures, alpha, extra data, block types, cloth data, and the
   weights (planned 3BBB bones only, no zero-weight vertex, normalized, ≤ 4 influences, no cross-side anomaly,
   the Copy Bone Weights prediction, untouched shapes normalization-aware as in 0.3.3).
4. COMMIT (``commit-direct-batch``, explicit): VALIDATED meshes only, after re-hashing the source winner and the
   generated file; written atomically to ``<output mod>/<same game-relative path>``; an existing file is never
   overwritten; a ``direct_conversions`` record is added to the output mod's manifest (BodySlide records kept).
5. VERIFY (``verify-direct-build``, read-only): the output mod must be the MO2 winner of the game path and carry
   exactly the committed file.

The source package and its plugin are only read.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Callable, Optional

import numpy as np

from .. import __version__
from ..analysis.reference import KnowledgeBase
from ..analysis.shape_metrics import MetricSettings, analyze_shape
from ..bodyslide.osp import OspParseError, parse_osp
from ..classification.rules import quality_issues
from ..discovery.install import BODYSLIDE_SUBTREE, Installation
from ..nif.reader import NifError, read_nif
from ..nif.structure import compare_structures, read_structure
from .automation import AutomationScript, AutomationStep, ScriptInvariantError, parse_script, safe_script_name
from .direct_nif import SEX_FEMALE_CBBE, DirectMesh, DirectNifReport, _reference_shape, analyze_mesh
from .engine import ConversionEngine, RunOutcome
from .evidence import MTIME_TOLERANCE_S, parse_log_session
from .gamedata import normalize_mesh_path, package_items_for_mesh, resolve_data_file
from .manifest import Manifest
from .plan import WEIGHT_ADDED_EPS, _osd_refs, _stage, check_output_mod, sha256_file
from .runtime import RuntimeInfo, write_runtime_config
from .validate import Check, ValidationReport, _col, emulate_full_copy, untouched_weight_change

__all__ = [
    "DirectBatchError", "DirectBatchPlan", "DirectBatchEngine", "direct_id", "resolve_selection", "plan_direct_batch",
    "build_direct_script", "check_direct_script", "validate_direct_mesh", "existing_direct_status",
    "verify_direct_build", "DirectBuildCheck", "LAYERING_NOTE", "plan_text", "direct_summary_text",
]

log = logging.getLogger(__name__)
ProgressFn = Callable[[str], None]

ELIGIBLE_STATUS = "DIRECT_NIF_HIGH_CONFIDENCE"
STATES = ("PLANNED", "CONVERTING", "VALIDATED", "REVIEW", "FAILED", "COMMITTED")
COPY_MODE = "full"  # Copy Bone Weights for all reference bones, as validated in game for the BodySlide path

# Failing one of these (and nothing else) makes a mesh REVIEW instead of FAILED.
QUALITY_MARKERS = (
    ": cross-side / quality anomaly checks",
    ": predicted vs actual physics weights consistent",
    ": matches the Copy Bone Weights prediction",
    " 3BBB bones present",
    ": expected 3BBB bones carry weight",
    "Direct-NIF analyzer classifies the generated mesh as 3BBB KEEP",
)

LAYERING_NOTE = (
    "IN-GAME TEST NOTE — do NOT test these converted clothes while wearing an unconverted body-slot (33) "
    "shoe/boot/item of this package that supplies its own legacy CBBE body (EasyGirl: its boots/shoes embed a plain "
    "CBBE body). For the first in-game acceptance use the normal CBBE-3BBB body and NO body-slot item of the "
    "package; otherwise the converted clothes move with 3BBB physics while that item's body does not (a false negative).")


class DirectBatchError(RuntimeError):
    pass


def _jload(p: Path) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _jsave(p: Path, data) -> None:
    p = Path(p)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def direct_id(game_path: str) -> str:
    """Stable Direct-NIF ID of a game mesh (independent of case and slash style)."""
    return "dn-" + hashlib.sha1(normalize_mesh_path(game_path).encode("utf-8")).hexdigest()[:10]


def _game_rel(mesh: str) -> str:
    """'meshes\\A\\b.nif' → 'meshes/A/b.nif' (the plugin's spelling, forward slashes)."""
    s = mesh.replace("\\", "/").strip("/")
    return s if s.lower().startswith("meshes/") else "meshes/" + s


def _same_path(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _inside(child, parent) -> bool:
    try:
        return Path(child).resolve().is_relative_to(Path(parent).resolve())
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------------------------------------------

def resolve_selection(rep: DirectNifReport, selectors: list[str]) -> tuple[list[tuple[str, DirectMesh]], list[str]]:
    """Map each selector to exactly one analyzed female game mesh; unknown or ambiguous selectors are problems.

    A selector is a Direct-NIF ID, a game-relative path (with or without ``meshes\\``) or a file name that only one
    analyzed mesh of the package has. File names are never used for sex or item mapping, only to find the record."""
    out: list[tuple[str, DirectMesh]] = []
    problems: list[str] = []
    seen: set[str] = set()
    for sel in selectors or []:
        s = sel.strip().strip('"')
        if not s:
            continue
        if s.lower().startswith("dn-"):
            hits = [m for m in rep.meshes if direct_id(m.mesh) == s.lower()]
            how = "Direct-NIF ID"
        elif "/" in s or "\\" in s:
            key = normalize_mesh_path(s)
            hits = [m for m in rep.meshes if normalize_mesh_path(m.mesh) == key]
            how = "game path"
        else:
            name = s.lower() if s.lower().endswith(".nif") else s.lower() + ".nif"
            hits = [m for m in rep.meshes if PureWindowsPath(m.mesh).name.lower() == name]
            how = "file name"
        if not hits:
            problems.append(f"'{s}': no female game mesh of package '{rep.package}' matches this {how} (male-only and "
                            "first-person meshes are never selectable; use the game-relative path or the Direct-NIF ID "
                            "from the report)")
            continue
        if len(hits) > 1:
            problems.append(f"'{s}': the {how} is ambiguous ({len(hits)} meshes: "
                            + ", ".join(f"{m.mesh} [{direct_id(m.mesh)}]" for m in hits[:4])
                            + "); select by game-relative path or Direct-NIF ID")
            continue
        m = hits[0]
        k = normalize_mesh_path(m.mesh)
        if k in seen:  # the same mesh selected twice (e.g. two items share it): converted once
            continue
        seen.add(k)
        out.append((s, m))
    return out, problems


def eligibility(dm: DirectMesh, package: str) -> list[str]:
    """Why a mesh may NOT be converted in Phase 4.1 (empty list = eligible). No override exists."""
    p = []
    if dm.action != "CONVERT":
        p.append(f"NOT_CONVERT: the analyzer classifies it {dm.action} / {dm.status}"
                 + (f" ({dm.reasons[0]})" if dm.reasons else ""))
    if dm.confidence != "HIGH":
        p.append(f"NOT_HIGH_CONFIDENCE: confidence {dm.confidence}")
    if dm.sex != SEX_FEMALE_CBBE:
        p.append(f"NOT_FEMALE_CBBE: sex/body {dm.sex}")
    if dm.action == "CONVERT" and dm.status != ELIGIBLE_STATUS:
        p.append(f"{dm.status}: only {ELIGIBLE_STATUS} meshes are converted in Phase 4.1")
    if dm.body_slot:
        p.append("BODY_SLOT_ITEM: used on biped slot 33 (body); body-slot items are not converted in Phase 4.1")
    if dm.embedded_body not in ("NONE", ""):
        p.append(f"EMBEDDED_BODY: {dm.embedded_body}")
    if dm.cloth_data:
        p.append(f"CLOTH_DATA_UNSUPPORTED: {dm.cloth_data} BSClothExtraData block(s)")
    if dm.unsupported_structures:
        p.append("UNSUPPORTED_NIF_STRUCTURE: " + ", ".join(dm.unsupported_structures[:3]))
    if dm.winner.get("provider") != package:
        p.append(f"NOT_PACKAGE_WINNER: the MO2 winner is '{dm.winner.get('provider', '—')}', not '{package}'")
    if not any(s.decision == "CONVERT" for s in dm.shapes):
        p.append("NO_CONVERT_SHAPE: no shape is planned for conversion")
    odd = [f"{s.name} ({s.decision})" for s in dm.shapes if s.decision not in ("CONVERT", "NONE")]
    if odd:
        p.append("SHAPE_DECISION_UNSUPPORTED: " + ", ".join(odd[:3]))
    return p


# ---------------------------------------------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------------------------------------------

@dataclass
class DirectBatchPlan:
    package: str
    created: str = ""
    tool_version: str = __version__
    output_mod: str = ""
    source_mode: str = ""
    selectors: list[str] = field(default_factory=list)
    reference: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    runtime: dict = field(default_factory=dict)
    run_mode: str = ""
    meshes: list[dict] = field(default_factory=list)  # eligible, selected meshes (converted)
    refused: list[dict] = field(default_factory=list)  # selected but not eligible
    problems: list[str] = field(default_factory=list)  # unresolved selectors, batch-level blockers
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.meshes) and not self.refused and not self.problems

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DirectBatchPlan":
        from dataclasses import fields

        return cls(**{k: v for k, v in d.items() if k in {f.name for f in fields(cls)}})


def _reference_identity(inst, kb) -> tuple[dict, list[str]]:
    """The CBBE-3BBB reference files Outfit Studio loads (staged into the workspace), with hashes."""
    problems = []
    tpl = getattr(inst, "reference_template", None)
    ref = kb.reference if kb else None
    if tpl is None or ref is None:
        return {}, ["CBBE-3BBB reference not found"]
    vfs = inst.vfs
    osp = _stage(vfs, "reference-osp", tpl.source_file)
    if osp is None:
        return {}, [f"reference project file not found: {tpl.source_file}"]
    try:
        ss = next(x for x in parse_osp(Path(osp.abs_path)).sets if x.name == tpl.set_name)
    except (StopIteration, OspParseError) as exc:
        return {}, [f"reference slider set '{tpl.set_name}' cannot be read: {exc}"]
    files = [osp]
    nif = _stage(vfs, "reference-nif", f"ShapeData/{ss.data_folder}/{ss.source_file}")
    if nif is None:
        problems.append("reference ShapeData NIF not found")
    else:
        files.append(nif)
    for folders, osd in sorted(_osd_refs(ss)):
        f = next((x for x in (_stage(vfs, "reference-osd", f"ShapeData/{fo}/{osd}") for fo in folders) if x), None)
        if f is not None:
            files.append(f)
    return {"template": tpl.display_name, "osp_rel": osp.rel, "set": tpl.set_name, "shape": tpl.shape or ref.shape_name,
            "vertices": ref.num_vertices,
            "files": [{"role": f.role, "rel": f.rel, "abs_path": f.abs_path, "provider": f.provider, "size": f.size,
                       "mtime": f.mtime, "sha256": sha256_file(Path(f.abs_path))} for f in files]}, problems


def _output_owner(target: Path, out_mod: Path, key: str) -> str:
    """Who owns an existing file at the output path: '' (no file) or a description."""
    if not target.exists():
        return ""
    try:
        man = Manifest.load(out_mod)
    except (ValueError, OSError, json.JSONDecodeError):
        return "an unknown file (the output mod has no readable manifest)"
    h = sha256_file(target)
    recs = [r for r in man.direct_records if r.get("game_key") == key]
    if recs:
        r = recs[-1]
        if r.get("generated", {}).get("sha256") == h:
            return f"the accepted Direct-NIF conversion of batch {r.get('batch', {}).get('id', '?')}"
        return (f"a stale or modified Direct-NIF conversion (batch {r.get('batch', {}).get('id', '?')}); the file no "
                "longer matches its manifest record")
    rel = str(target.relative_to(out_mod)).replace("\\", "/").lower()
    if any(f.get("path", "").lower() == rel for r in man.records for f in r.get("generated_files", [])):
        return "another generated conversion (a BodySlide-path record of the manifest)"
    return "a file that this tool did not create (manual user file)"


def plan_direct_batch(inst: Installation, kb: KnowledgeBase, rep: DirectNifReport, selectors: list[str],
                      output_mod: Optional[Path], runtime: Optional[RuntimeInfo] = None) -> DirectBatchPlan:
    """Read-only: resolve and check the selection and plan every selected mesh. Writes nothing."""
    conv = inst.profile.conversion
    plan = DirectBatchPlan(package=rep.package, created=_now(), output_mod=str(output_mod or ""),
                           source_mode=rep.source_mode, selectors=list(selectors or []))
    plan.settings = {"search_radius": conv.search_radius, "max_vertex_targets": conv.max_vertex_targets,
                     "no_target_limit": conv.no_target_limit, "copy_mode": COPY_MODE, "auto_copy_ref": False,
                     "export_with_ref": False}
    if runtime is not None:
        plan.runtime = runtime.to_dict()
        mode = (conv.os_run_mode or "auto").lower()
        plan.run_mode = (runtime.recommended_run_mode or "headless") if mode == "auto" else mode
    if not selectors:
        plan.problems.append("nothing selected: pass --select for every mesh (there is no default selection)")
    if not output_mod:
        plan.problems.append("no output mod")
    if conv.no_target_limit:
        plan.problems.append("the profile enables 'No Target Limit'; Direct-NIF conversion requires it OFF")
    plan.reference, ref_problems = _reference_identity(inst, kb)
    plan.problems += ref_problems
    chosen, sel_problems = resolve_selection(rep, selectors)
    plan.problems += sel_problems
    ref = kb.reference if kb else None
    for sel, dm in chosen:
        problems = eligibility(dm, rep.package)
        entry = _mesh_entry(dm, sel, output_mod, ref, plan.settings, problems)
        (plan.refused if problems else plan.meshes).append(entry)
    if output_mod:
        provs = {p.name: p for p in inst.providers}
        om, pk = provs.get(Path(output_mod).name), provs.get(rep.package)
        if om is None or not _same_path(om.root, output_mod):
            plan.notes.append(f"WARNING: the output mod '{Path(output_mod).name}' is not an enabled MO2 mod yet: after the "
                              "commit, enable it ABOVE the source package, or the game keeps loading the original meshes "
                              "(verify-direct-build checks this)")
        elif pk is not None and om.priority < pk.priority:
            plan.notes.append(f"WARNING: the output mod is ranked BELOW '{rep.package}' in MO2: the committed meshes would "
                              "not win; move the output mod above the package before testing")
    legacy = [n for n in rep.notes if n.startswith("LAYERING RISK")]
    plan.notes += legacy
    plan.notes.append("PLAN ONLY: nothing was copied, converted or written into any mod")
    return plan


def _mesh_entry(dm: DirectMesh, selector: str, output_mod: Optional[Path], ref, settings: dict, problems: list[str]) -> dict:
    game_rel = _game_rel(dm.mesh)
    key = normalize_mesh_path(dm.mesh)
    target = Path(output_mod) / game_rel if output_mod else None
    shapes = []
    warnings = list(dm.warnings)
    for s in dm.shapes:
        sd = {"name": s.name, "block_type": s.block_type, "vertices": s.vertices, "triangles": s.triangles, "kind": s.kind,
              "decision": s.decision, "bones": s.bones, "existing_bones": dict(s.weighted_bones),
              "existing_3bbb": dict(s.existing_3bbb), "regions": dict(s.regions), "planned_regions": list(s.planned_regions),
              "planned_bones": list(s.planned_bones), "planned_radius": s.planned_radius,
              "out_of_reach_vertices": list(s.out_of_reach_vertices), "reasons": list(s.reasons),
              "predicted_full_copy_physics_bones": []}
        shapes.append(sd)
    # a full Copy Bone Weights copies every reference bone: it must add only the planned 3BBB bones
    if not problems and ref is not None and dm.winner.get("path"):
        try:
            nif = read_nif(Path(dm.winner["path"]))
        except (NifError, OSError) as exc:
            problems.append(f"SOURCE_UNREADABLE: {exc}")
            nif = None
        for sd in shapes:
            if nif is None or sd["decision"] != "CONVERT":
                continue
            shp = nif.shape(sd["name"])
            pred = ref.predict_weights(shp.world_vertices(), ref.physics_bones, float(sd["planned_radius"]),
                                       int(settings["max_vertex_targets"]), bool(settings["no_target_limit"]))
            got = [b for j, b in enumerate(ref.physics_bones) if (pred[:, j] > WEIGHT_ADDED_EPS).any()]
            sd["predicted_full_copy_physics_bones"] = got
            outside = [b for b in got if b not in sd["planned_bones"]]
            if outside:
                problems.append(f"FULL_COPY_OUTSIDE_REGIONS: a full Copy Bone Weights would add {', '.join(outside)} to "
                                f"'{sd['name']}' (planned: {', '.join(sd['planned_bones'])}); region-restricted copies are "
                                "not validated for Direct-NIF")
    owner = _output_owner(target, Path(output_mod), key) if target is not None else ""
    if owner:
        warnings.append(f"the output path already exists: {owner}; commit will NOT overwrite it")
    return {
        "id": direct_id(dm.mesh), "selector": selector, "mesh": dm.mesh, "game_rel": game_rel, "game_key": key,
        "file_name": PureWindowsPath(dm.mesh).name,
        "items": dm.items, "item_names": dm.item_names, "plugins": sorted({i.get("plugin", "") for i in dm.items}),
        "roles": dm.roles,
        "source": {"provider": dm.winner.get("provider", ""), "path": dm.winner.get("path", ""), "size": dm.size,
                   "sha256": dm.sha256, "mtime": Path(dm.winner["path"]).stat().st_mtime if dm.winner.get("path") else 0},
        "winner": dict(dm.winner), "competitors": list(dm.competitors),
        "sex": dm.sex, "sex_confidence": dm.sex_confidence, "sex_evidence": list(dm.sex_evidence),
        "action": dm.action, "status": dm.status, "confidence": dm.confidence, "reasons": list(dm.reasons),
        "shapes": shapes,
        "convert_shapes": [s["name"] for s in shapes if s["decision"] == "CONVERT"],
        "untouched_shapes": [s["name"] for s in shapes if s["decision"] != "CONVERT"],
        "embedded_body": dm.embedded_body, "body_slot": dm.body_slot, "cloth_data": dm.cloth_data,
        "structures": list(dm.unsupported_structures),
        "name_collisions": [x for x in dm.unsupported_structures if "equal a bone" in x or "duplicate" in x],
        "output_rel": game_rel, "output_path": str(target) if target else "", "output_existing": owner,
        "warnings": warnings, "eligible": not problems, "problems": problems,
    }


def plan_text(plan: DirectBatchPlan, details: bool = True) -> str:
    L = [f"DIRECT-NIF BATCH PLAN — package {plan.package} ({plan.source_mode})",
         f"  output mod: {plan.output_mod}",
         f"  reference: {plan.reference.get('template', '—')} → {plan.reference.get('osp_rel', '—')} / "
         f"{plan.reference.get('set', '—')} / {plan.reference.get('shape', '—')}",
         f"  Copy Bone Weights: {plan.settings.get('copy_mode')} copy, radius {plan.settings.get('search_radius')}, "
         f"max targets {plan.settings.get('max_vertex_targets')}, no target limit "
         f"{'ON' if plan.settings.get('no_target_limit') else 'OFF'}; ExportFile WithRef=false (reference never exported)",
         f"  Outfit Studio: {plan.runtime.get('version') or '—'} ({plan.run_mode or '—'})",
         f"  selected: {len(plan.meshes) + len(plan.refused)}   convertible: {len(plan.meshes)}   refused: {len(plan.refused)}", ""]
    for e in plan.meshes + plan.refused:
        L.append(f"{'OK     ' if e['eligible'] else 'REFUSED'} {e['id']}  {e['game_rel']}")
        if details or not e["eligible"]:
            L += _entry_lines(e)
        L.append("")
    if plan.problems:
        L.append("PROBLEMS:")
        L += [f"  ✗ {p}" for p in plan.problems]
    L += [f"note: {n}" for n in plan.notes]
    L.append("RESULT: " + ("the plan can be converted (convert-direct-batch … --yes)" if plan.ok
                           else "NOT convertible as selected (fix the selection; there is no override)"))
    return "\n".join(L) + "\n"


def _entry_lines(e: dict) -> list[str]:
    s = e["source"]
    L = [f"    selector: {e['selector']}",
         f"    in-game items: " + ", ".join(e["item_names"]) + f"   (plugin {', '.join(e['plugins'])})",
         f"    source: {s['provider']}: {s['path']}",
         f"    MO2 winner: {e['winner'].get('provider', '—')}"
         + (f"   (overridden: {', '.join(c['provider'] for c in e['competitors'][1:])})" if len(e['competitors']) > 1 else ""),
         f"    SHA-256 {s['sha256']}   {s['size']} bytes",
         f"    sex/body: {e['sex']} ({e['sex_confidence']});  action {e['action']} / {e['status']} / {e['confidence']}",
         f"    embedded body: {e['embedded_body']};  body slot 33: {'YES' if e['body_slot'] else 'no'};  cloth data: "
         f"{e['cloth_data'] or 'none'};  shape-name collisions: {', '.join(e['name_collisions']) or 'none'}"]
    for sd in e["shapes"]:
        regs = ", ".join(f"{r.lower()} {d.get('relevance')}/{d.get('fit')}" for r, d in sd["regions"].items()) or "no region"
        L.append(f"    shape '{sd['name']}' [{sd['block_type']}, {sd['kind']}] {sd['vertices']} v / {sd['triangles']} t: "
                 f"{sd['decision']}  ({regs})")
        L.append(f"      existing bones ({len(sd['existing_bones'])}): " + ", ".join(sorted(sd["existing_bones"]))[:300])
        if sd["decision"] == "CONVERT":
            L.append(f"      planned 3BBB bones: {', '.join(sd['planned_bones'])};  Copy Bone Weights radius {sd['planned_radius']:g}"
                     + (f" (raised for vertices {sd['out_of_reach_vertices'][:8]})" if sd["out_of_reach_vertices"] else ""))
            L.append(f"      full-copy prediction adds: {', '.join(sd['predicted_full_copy_physics_bones']) or '—'}")
        else:
            L.append("      untouched (not a Copy Bone Weights target)")
    L.append(f"    output: <output mod>\\{e['output_rel'].replace('/', chr(92))}"
             + (f"   ! EXISTS: {e['output_existing']}" if e["output_existing"] else ""))
    L += [f"    ! {w}" for w in e["warnings"][:6]]
    L += [f"    ✗ {p}" for p in e["problems"]]
    return L


def write_plan_reports(plan: DirectBatchPlan, folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", plan.package).strip("._")[:40] or "package"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pj, pt = folder / f"{stamp}_{base}_direct_plan.json", folder / f"{stamp}_{base}_direct_plan.txt"
    pj.write_text(json.dumps(plan.to_dict(), indent=1), encoding="utf-8")
    pt.write_text(plan_text(plan), encoding="utf-8")
    return {"plan_json": pj, "plan_txt": pt}


def compare_with_saved_plan(fresh: DirectBatchPlan, saved: dict) -> list[str]:
    """Differences between the inspected plan file and the fresh plan (convert-direct-batch --plan)."""
    out = []
    old = {e["id"]: e for e in saved.get("meshes", [])}
    new = {e["id"]: e for e in fresh.meshes}
    if set(old) != set(new):
        out.append(f"the selection differs from the inspected plan ({sorted(old)} vs {sorted(new)})")
    for i in set(old) & set(new):
        a, b = old[i]["source"], new[i]["source"]
        if a.get("provider") != b.get("provider") or not _same_path(a.get("path", ""), b.get("path", "")):
            out.append(f"SOURCE_WINNER_CHANGED: {new[i]['game_rel']}: winner {a.get('provider')} → {b.get('provider')}")
        elif a.get("sha256") != b.get("sha256"):
            out.append(f"SOURCE_CHANGED: {new[i]['game_rel']}: the source file changed since the inspected plan")
        if [s.get("planned_radius") for s in old[i]["shapes"]] != [s.get("planned_radius") for s in new[i]["shapes"]] or \
                [s.get("planned_bones") for s in old[i]["shapes"]] != [s.get("planned_bones") for s in new[i]["shapes"]]:
            out.append(f"{new[i]['game_rel']}: the planned bones/radius differ from the inspected plan")
    if [f.get("sha256") for f in saved.get("reference", {}).get("files", [])] != \
            [f.get("sha256") for f in fresh.reference.get("files", [])]:
        out.append("TARGET_REFERENCE_CHANGED: the CBBE-3BBB reference files differ from the inspected plan")
    return out


# ---------------------------------------------------------------------------------------------------------------
# Outfit Studio script
# ---------------------------------------------------------------------------------------------------------------

def build_direct_script(name: str, parts: list[dict], reference: dict, settings: dict) -> AutomationScript:
    """One script converting several meshes in turn. ``parts``: {entry, source (abs), output (abs)}."""
    steps: list[AutomationStep] = []
    for i, p in enumerate(parts, 1):
        e = p["entry"]
        tag = f"[{i}/{len(parts)} {e['file_name']}]"
        steps.append(AutomationStep("ClearProject", note=f"{tag} start from an empty project"))
        steps.append(AutomationStep("LoadReference", {"SourceFile": reference["osp_rel"], "Set": reference["set"],
                                                      "Shape": reference["shape"], "LoadAll": False, "MergeSliders": True,
                                                      "MergeZaps": True, "AppendNewSliders": False},
                                    note=f"{tag} temporary reference: {reference.get('template', '')} (never exported)"))
        steps.append(AutomationStep("ImportFile", {"FilePath": str(p["source"]), "FromFolder": False},
                                    note=f"{tag} the WORKSPACE COPY of the game mesh (the source is never opened)"))
        groups: dict[float, list[str]] = {}
        for sd in e["shapes"]:
            if sd["decision"] == "CONVERT":
                groups.setdefault(float(sd["planned_radius"]), []).append(sd["name"])
        for radius, names in groups.items():
            steps.append(AutomationStep("CopyBoneWeights", {"ProximityRadius": radius,
                                                            "MaxResults": int(settings["max_vertex_targets"])},
                                        target_meshes=names,
                                        note=f"{tag} copy bone weights from the reference (full copy, radius {radius:g})"))
        steps.append(AutomationStep("ExportFile", {"FilePath": str(p["output"]), "WithRef": False, "UseOriginalPath": False},
                                    note=f"{tag} export the game mesh to the workspace; WithRef=false"))
    sc = AutomationScript(name=name, steps=steps)
    return sc


def check_direct_script(script: AutomationScript | str, workspace: Path) -> list[tuple[int, int]]:
    """Hard rules for Direct-NIF scripts; returns each mesh's (first, last) step numbers."""
    sc = parse_script(script.to_xml()) if isinstance(script, AutomationScript) else parse_script(script)
    ws = Path(workspace)
    segs: list[list] = []
    for st in sc.steps:
        if st.type == "ClearProject" or not segs:
            segs.append([])
        segs[-1].append(st)
    if not segs:
        raise ScriptInvariantError("empty Direct-NIF script")
    allowed = {"ClearProject", "LoadReference", "ImportFile", "CopyBoneWeights", "ExportFile"}
    ranges, outputs, n = [], set(), 0
    for seg in segs:
        types = [s.type for s in seg]
        if types[0] != "ClearProject":
            raise ScriptInvariantError("every mesh must start with ClearProject")
        bad = sorted(set(types) - allowed)
        if bad:
            raise ScriptInvariantError(f"step types not allowed in a Direct-NIF script: {bad} (no SaveProject, no fake project)")
        for t in ("LoadReference", "ImportFile", "ExportFile"):
            if types.count(t) != 1:
                raise ScriptInvariantError(f"each mesh needs exactly one {t} step")
        if types.index("LoadReference") > types.index("ImportFile") or types[-1] != "ExportFile":
            raise ScriptInvariantError("order must be ClearProject, LoadReference, ImportFile, CopyBoneWeights…, ExportFile")
        imp = next(s for s in seg if s.type == "ImportFile")
        exp = next(s for s in seg if s.type == "ExportFile")
        if str(imp.params.get("FromFolder", "false")).lower() != "false":
            raise ScriptInvariantError("ImportFile must import one file")
        for label, st in (("ImportFile", imp), ("ExportFile", exp)):
            fp = str(st.params.get("FilePath", ""))
            if not (Path(fp).is_absolute() or PureWindowsPath(fp).is_absolute()) or not _inside(fp, ws):
                raise ScriptInvariantError(f"{label}.FilePath must be an absolute path inside the workspace: {fp}")
        if str(exp.params.get("WithRef", "")).lower() != "false":
            raise ScriptInvariantError("ExportFile must set WithRef=false explicitly (Outfit Studio defaults to true)")
        if str(exp.params.get("UseOriginalPath", "false")).lower() != "false" or exp.params.get("Prefix") or exp.params.get("Suffix"):
            raise ScriptInvariantError("ExportFile must write exactly FilePath (no original path, prefix or suffix)")
        if not str(exp.params["FilePath"]).lower().endswith(".nif"):
            raise ScriptInvariantError("ExportFile must write a .nif")
        key = os.path.normcase(str(exp.params["FilePath"]))
        if key in outputs:
            raise ScriptInvariantError("two meshes export to the same file")
        outputs.add(key)
        for s in seg:
            if s.type == "CopyBoneWeights":
                if not s.target_meshes:
                    raise ScriptInvariantError("CopyBoneWeights must name its target shapes")
                if s.params.get("BoneList"):
                    raise ScriptInvariantError("region-restricted (BoneList) copies are not used for Direct-NIF")
                if int(float(s.params.get("MaxResults", 4))) > 64:
                    raise ScriptInvariantError("No Target Limit is not allowed for Direct-NIF")
        ranges.append((n + 1, n + len(seg)))
        n += len(seg)
    return ranges


# ---------------------------------------------------------------------------------------------------------------
# validation of one generated mesh
# ---------------------------------------------------------------------------------------------------------------

def mesh_status(checks: list[dict]) -> str:
    failed = [c for c in checks if c.get("required", True) and not c.get("ok")]
    if not failed:
        return "VALIDATED"
    if all(any(m in c["name"] for m in QUALITY_MARKERS) for c in failed):
        return "REVIEW"
    return "FAILED"


def validate_direct_mesh(entry: dict, source_copy: Path, generated: Path, original_source: Path, kb: KnowledgeBase,
                         settings: MetricSettings, reference: dict) -> tuple[ValidationReport, dict]:
    """Every check of one generated game mesh (the run evidence is added by the caller). Returns (report, facts)."""
    rep = ValidationReport()
    facts: dict = {"shapes": {}}
    ref = kb.reference
    # A. source immutability ---------------------------------------------------------------------------------
    h = sha256_file(original_source) if Path(original_source).is_file() else "MISSING"
    rep.add("source game mesh unchanged (SHA-256)", h == entry["source"]["sha256"],
            f"{Path(original_source).name} {h[:16]}… (planned {entry['source']['sha256'][:16]}…)")
    hc = sha256_file(source_copy) if Path(source_copy).is_file() else "MISSING"
    rep.add("workspace source copy unchanged (SHA-256)", hc == entry["source"]["sha256"], f"{hc[:16]}…")
    # round trip: independent re-parse ----------------------------------------------------------------------
    exists = Path(generated).is_file() and Path(generated).stat().st_size > 0
    rep.add("generated NIF exists at the explicit absolute workspace path", exists, str(generated))
    if not exists:
        return rep, facts
    facts["generated_sha256"] = sha256_file(generated)
    facts["generated_size"] = Path(generated).stat().st_size
    try:
        gen = read_nif(Path(generated))
        gst = read_structure(generated)
        ok = gen.header.is_fallout4 and bool(gen.shapes) and not gen.warnings and not gst.errors
        detail = (f"Fallout 4 {gen.header.is_fallout4}, {len(gen.shapes)} shape(s), {sum(gst.block_types.values())} blocks"
                  + (f"; reader warnings: {gen.warnings[:2]}" if gen.warnings else ""))
    except (NifError, OSError, ValueError, IndexError, KeyError) as exc:  # struct errors surface as NifError/ValueError
        rep.add("generated NIF independently re-parsed", False, f"not a readable NIF: {exc}")
        return rep, facts
    except Exception as exc:  # noqa: BLE001 - a malformed file must never crash validation
        rep.add("generated NIF independently re-parsed", False, f"not a readable NIF: {type(exc).__name__}: {exc}")
        return rep, facts
    rep.add("generated NIF independently re-parsed", ok, detail)
    src = read_nif(Path(source_copy))
    sst = read_structure(source_copy)
    convert = set(entry["convert_shapes"])
    # B–F. structure ------------------------------------------------------------------------------------------
    for name, ok_, det in compare_structures(sst, gst, convert):
        rep.add(f"structure: {name}", ok_, det)
    # no temporary reference body -------------------------------------------------------------------------------
    names = [s.name for s in gen.shapes]
    src_names = [s.name for s in src.shapes]
    ref_name = reference.get("shape", "")
    ref_n = int(reference.get("vertices") or (ref.num_vertices if ref else 0))
    leaked = [n for n in names if n not in src_names and (n == ref_name or n.startswith(ref_name)
                                                          or next(s for s in gen.shapes if s.name == n).num_vertices == ref_n)]
    rep.add("no CBBE-3BBB reference body in the output (WithRef=false)", not leaked and ref_name not in names,
            f"output shapes {names}" + (f"; REFERENCE LEAK {leaked}" if leaked else ""))
    # G. cloth
    rep.add("cloth physics data: none in source or output", not src.cloth_data and not gen.cloth_data,
            f"source {len(src.cloth_data)} block(s), output {len(gen.cloth_data)} block(s)")
    # 13. weights -------------------------------------------------------------------------------------------------
    phys_all = list(ref.physics_bones) if ref else []
    for sd in entry["shapes"]:
        g, s = gen.shape(sd["name"]), src.shape(sd["name"])
        if g is None or s is None:
            rep.add(f"{sd['name']}: shape present", False, "missing in the generated or source NIF")
            continue
        gb, gw = g.weight_matrix()
        sums = gw.sum(1) if len(gw) else np.zeros(0)
        phys = {b: int((gw[:, gb.index(b)] > 0.001).sum()) for b in phys_all if b in gb}
        phys = {b: n for b, n in phys.items() if n}
        infl = (gw > 1e-6).sum(1) if len(gw) else np.zeros(0)
        f = facts["shapes"][sd["name"]] = {"decision": sd["decision"], "physics_bones": phys, "bones": len(gb),
                                           "weight_sum_min": round(float(sums.min()), 4) if len(sums) else None,
                                           "weight_sum_max": round(float(sums.max()), 4) if len(sums) else None}
        if sd["decision"] != "CONVERT":
            sb, sw = s.weight_matrix()
            diff, raw, det = untouched_weight_change(gb, gw, sb, sw)
            f["max_weight_change"] = round(float(diff), 5)
            rep.add(f"{sd['name']}: untouched shape — weights unchanged (normalization-aware)", diff < 1e-3, det)
            continue
        allowed = list(sd["planned_bones"])
        outside = sorted(b for b in phys if b not in allowed)
        rep.add(f"{sd['name']}: no physics bones outside the planned regions (forbidden region)", not outside,
                "weighted 3BBB bones: " + (", ".join(f"{b}({n})" for b, n in sorted(phys.items())) or "none")
                + (f"; FORBIDDEN {outside}" if outside else ""))
        for region in sd["planned_regions"]:
            rb = list(ref.physics_regions.get(region, [])) if ref else []
            have = [b for b in rb if phys.get(b, 0) >= 5]
            rep.add(f"{sd['name']}: {region.lower()} 3BBB bones present", len(have) >= max(1, len(rb) // 2),
                    ", ".join(f"{b}({phys.get(b, 0)})" for b in rb))
        expected = list(sd.get("predicted_full_copy_physics_bones") or [])
        missing = [b for b in expected if not phys.get(b)]
        rep.add(f"{sd['name']}: expected 3BBB bones carry weight", bool(expected) and not missing,
                f"expected {len(expected)}: {', '.join(expected)}" + (f"; WITHOUT weight {missing}" if missing else ""))
        zero = np.nonzero(sums < 0.5)[0] if len(sums) else np.zeros(0, dtype=int)
        dev = float(np.abs(sums - 1).max()) if len(sums) else 1.0
        rep.add(f"{sd['name']}: weights normalized, no vertex without weight", dev < 0.05 and not len(zero),
                f"weight sum range {float(sums.min()) if len(sums) else 0:.3f}..{float(sums.max()) if len(sums) else 0:.3f}"
                + (f"; {len(zero)} vertices WITHOUT any weight: " + ", ".join(str(int(i)) for i in zero[:12]) if len(zero) else ""))
        rep.add(f"{sd['name']}: at most 4 influences per vertex (Fallout 4)", int(infl.max()) <= 4 if len(infl) else False,
                f"max {int(infl.max()) if len(infl) else 0} influences")
        sa = analyze_shape(g, None, kb, settings, 0, {})
        issues, agreements = [], []
        for r, m in sa.regions.items():
            issues += quality_issues(sa, m)
            if m.agreement is not None and r in sd["planned_regions"]:
                agreements.append((r, m.agreement))
                rep.metrics[f"{sd['name']}/{r}/agreement"] = m.agreement
            if m.cross_side_fraction is not None:
                rep.metrics[f"{sd['name']}/{r}/cross_side_fraction"] = m.cross_side_fraction
        rep.add(f"{sd['name']}: cross-side / quality anomaly checks", not issues, "; ".join(issues) or "no anomalies")
        rep.add(f"{sd['name']}: predicted vs actual physics weights consistent",
                bool(agreements) and min(a for _, a in agreements) >= 0.9,
                "agreement " + (", ".join(f"{r.lower()} {a:.3f}" for r, a in agreements) or "n/a") + " (≥ 0.90 required)")
        if ref is not None:
            sb, sw = s.weight_matrix()
            en, ew = emulate_full_copy(kb, s.world_vertices(), sb, sw, float(sd["planned_radius"]),
                                       int(settings.max_vertex_targets), bool(settings.no_target_limit))
            allb = sorted(set(en) | set(gb))
            errs = np.array([np.abs(_col(en, ew, b) - _col(gb, gw, b)) for b in allb]) if allb else np.zeros((0, 0))
            mean_err = float(errs.mean()) if errs.size else 0.0
            p99 = float(np.percentile(errs.max(axis=0), 99)) if errs.size else 0.0
            rep.metrics[f"{sd['name']}/full_copy_mean_abs_error"] = round(mean_err, 5)
            rep.metrics[f"{sd['name']}/full_copy_p99_max_error"] = round(p99, 4)
            f["prediction_mean_abs_error"], f["prediction_p99_error"] = round(mean_err, 5), round(p99, 4)
            rep.add(f"{sd['name']}: matches the Copy Bone Weights prediction", mean_err < 0.005,
                    f"mean |Δw| {mean_err:.4f} (< 0.005 required), 99th pct per-vertex max |Δw| {p99:.3f}")
    # the analyzer's own view of the generated mesh -------------------------------------------------------------
    try:
        dm = DirectMesh(mesh=entry["mesh"], items=list(entry["items"]), roles=list(entry["roles"]))
        ref_shape = _ReferenceShapeCache.get()
        if ref_shape is not None:
            analyze_mesh(dm, Path(generated), kb, ref_shape, settings, False, {})
            ok = dm.action == "KEEP" and dm.status == "ALREADY_3BBB"
            det = f"{dm.action} / {dm.status} / {dm.sex}" + (f": {dm.reasons[0]}" if dm.reasons and not ok else "")
        else:
            ok, det = False, "reference shape unavailable"
    except Exception as exc:  # noqa: BLE001
        ok, det = False, f"analysis failed: {exc}"
    rep.add("Direct-NIF analyzer classifies the generated mesh as 3BBB KEEP", ok, det)
    return rep, facts


def _restore_checks(val: ValidationReport, r: dict) -> None:
    """0.4.3: the post-export restoration's own evidence, as required checks."""
    shapes = r.get("shapes", {})
    if r.get("code") == "NO_RAW_EXPORT":
        val.add("post-export restoration: raw Outfit Studio export present", False, "no raw export")
        return
    other = {n: {k: v for k, v in s.get("raw_differences", {}).items()
                 if k not in ("skin_weights", "bone_indices", "normal", "tangent", "bitangent_x", "bitangent_y", "bitangent_z")}
             for n, s in shapes.items()}
    other = {n: d for n, d in other.items() if d}
    unsafe = r.get("code") == "POST_EXPORT_RESTORE_UNSAFE"
    val.add("post-export restoration: source ↔ export vertex correspondence proven (structural gate)", not unsafe,
            "; ".join(r.get("reasons", [])[:3]) if unsafe else "same shapes, counts, vertex layout, topology, segments, material")
    if unsafe:
        return
    val.add("post-export restoration: raw export differs only by skinning and OS-regenerated normals / tangent basis",
            not other and r.get("code") != "RAW_EXPORT_UNEXPECTED_CHANGE",
            ("; ".join(r.get("reasons", [])[:3]) if other or r.get("code") == "RAW_EXPORT_UNEXPECTED_CHANGE" else
             "; ".join(f"{n}: OS_REGENERATED_NORMALS {s.get('os_regenerated_normals', 0)}, OS_REGENERATED_TANGENT_BASIS "
                       f"{s.get('os_regenerated_tangent_basis', 0)}, skinning {s.get('skin_changed_vertices', 0)} vertices"
                       for n, s in shapes.items())))
    if r.get("code") == "RAW_EXPORT_UNEXPECTED_CHANGE":
        return
    ok = bool(r.get("ok")) and all(s.get("immutable_fields_equal_source") and s.get("skinning_equal_raw_export")
                                   for s in shapes.values())
    val.add("post-export restoration: final immutable vertex bytes == source, skinning == raw export (byte-exact)", ok,
            (f"{r.get('bytes_restored', 0)} bytes restored; raw {str(r.get('raw_sha256'))[:16]}… → final "
             f"{str(r.get('final_sha256'))[:16]}…") if ok else "; ".join(r.get("reasons", [])[:3]))


class _ReferenceShapeCache:
    shape = None

    @classmethod
    def set(cls, shape) -> None:
        cls.shape = shape

    @classmethod
    def get(cls):
        return cls.shape


# ---------------------------------------------------------------------------------------------------------------
# the batch engine
# ---------------------------------------------------------------------------------------------------------------

class DirectBatchEngine:
    def __init__(self, tool_root: Path, inst: Installation, kb: KnowledgeBase,
                 runner: Optional[Callable[..., RunOutcome]] = None):
        self.eng = ConversionEngine(tool_root, inst, kb, runner=runner)
        self.inst = inst
        self.kb = kb
        self.root = Path(tool_root) / "work" / "direct_nif_batches"
        self._lock = threading.Lock()
        _ReferenceShapeCache.set(_reference_shape(inst))

    def _settings(self) -> MetricSettings:
        s = self.inst.profile
        return MetricSettings(search_radius=s.conversion.search_radius, max_vertex_targets=s.conversion.max_vertex_targets,
                              no_target_limit=s.conversion.no_target_limit,
                              region_weight_threshold=s.analysis.region_weight_threshold)

    # ------------------------------------------------------------------ create
    def create(self, plan: DirectBatchPlan, say: Optional[ProgressFn] = None) -> Path:
        say = say or (lambda m: None)
        if not plan.ok:
            raise DirectBatchError("the plan is not convertible: " + "; ".join(
                plan.problems + [f"{e['game_rel']}: {e['problems'][0]}" for e in plan.refused]) or "nothing selected")
        pkg = re.sub(r"[^A-Za-z0-9_.-]+", "_", plan.package).strip("._")[:40] or "package"
        bid = f"{time.strftime('%Y%m%d-%H%M%S')}_{pkg}"
        bdir = self.root / bid
        n = 1
        while bdir.exists():
            n += 1
            bdir = self.root / f"{bid}-{n}"
        (bdir / "meshes").mkdir(parents=True)
        (bdir / "logs").mkdir()
        entries = []
        for i, e in enumerate(plan.meshes, 1):
            mid = f"{i:02d}_{e['id']}"
            mdir = bdir / "meshes" / mid
            for d in ("source", "generated_raw", "generated", "logs"):
                (mdir / d).mkdir(parents=True)
            _jsave(mdir / "plan.json", e)
            entries.append({"id": mid, "direct_id": e["id"], "mesh": e["game_rel"], "file_name": e["file_name"],
                            "items": e["item_names"], "status": "PLANNED"})
        batch = {"schema": 2, "restore": True, "kind": "DIRECT_NIF", "id": bdir.name, "tool_version": __version__, "created": _now(),
                 "package": plan.package, "output_mod": plan.output_mod, "run_mode": plan.run_mode,
                 "session_mode": self.inst.profile.conversion.batch_session_mode,
                 "meshes": entries, "sessions": [], "state": "CREATED", "notes": list(plan.notes)}
        _jsave(bdir / "batch.json", batch)
        _jsave(bdir / "plan.json", plan.to_dict())
        say(f"Direct-NIF batch workspace created: {bdir}")
        return bdir

    def _load(self, bdir: Path) -> dict:
        bdir = Path(bdir)
        if not _inside(bdir, self.root) or not (bdir / "batch.json").is_file():
            raise DirectBatchError(f"not a Direct-NIF batch workspace of this tool (expected batch.json under {self.root})")
        b = _jload(bdir / "batch.json")
        if b.get("kind") != "DIRECT_NIF":
            raise DirectBatchError("not a Direct-NIF batch")
        return b

    @staticmethod
    def _entry_plan(bdir: Path, m: dict) -> dict:
        return _jload(Path(bdir) / "meshes" / m["id"] / "plan.json")

    @staticmethod
    def latest_result(bdir: Path, m: dict) -> Optional[dict]:
        mdir = Path(bdir) / "meshes" / m["id"]
        for name in ("result.revalidated.json", "result.json"):
            if (mdir / name).is_file():
                return _jload(mdir / name)
        return None

    def _paths(self, bdir: Path, m: dict, e: dict) -> tuple[Path, Path, Path]:
        """(workspace source copy, final generated candidate, raw Outfit Studio export).

        0.4.3 batches (``restore``): Outfit Studio exports to ``generated_raw``; the post-export restoration writes the
        final candidate to ``generated``. Earlier batches exported straight to ``generated`` (no raw/final split)."""
        mdir = Path(bdir).resolve() / "meshes" / m["id"]
        restore = _jload(Path(bdir) / "batch.json").get("restore", False) if (Path(bdir) / "batch.json").is_file() else False
        raw = mdir / ("generated_raw" if restore else "generated") / e["file_name"]
        return mdir / "source" / e["file_name"], mdir / "generated" / e["file_name"], raw

    # ------------------------------------------------------------------ run
    def run(self, bdir: Path, progress: Optional[ProgressFn] = None, cancel: Optional[Callable[[], bool]] = None) -> dict:
        if not self._lock.acquire(blocking=False):
            raise DirectBatchError("another Direct-NIF batch is running")
        try:
            return self._run(Path(bdir), progress or (lambda m: None), cancel or (lambda: False))
        finally:
            self._lock.release()

    def _set(self, bdir: Path, batch: dict, m: dict, status: str, reasons: list[str], extra: Optional[dict] = None,
             name: str = "result.json") -> None:
        m["status"] = status
        res = {"id": m["id"], "direct_id": m["direct_id"], "mesh": m["mesh"], "status": status, "reasons": reasons,
               "at": _now(), "tool_version": __version__}
        res.update(extra or {})
        _jsave(Path(bdir) / "meshes" / m["id"] / name, res)
        _jsave(Path(bdir) / "batch.json", batch)

    def _run(self, bdir: Path, say: ProgressFn, cancel: Callable[[], bool]) -> dict:
        batch = self._load(bdir)
        if batch["state"] != "CREATED":
            raise DirectBatchError(f"batch already ran (state {batch['state']}); use validate-direct-batch / commit-direct-batch")
        plan = DirectBatchPlan.from_dict(_jload(bdir / "plan.json"))
        project = bdir / "Data" / BODYSLIDE_SUBTREE
        # 1. the reference, staged into the workspace (hash-checked against the plan)
        ref_problems = []
        for f in plan.reference.get("files", []):
            p = Path(f["abs_path"])
            if not p.is_file() or sha256_file(p) != f["sha256"]:
                ref_problems.append(f"TARGET_REFERENCE_CHANGED: {f['rel']} changed since the plan")
                continue
            dst = project / f["rel"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
        for d in ("SliderSets", "ShapeData", "Automations"):
            (project / d).mkdir(parents=True, exist_ok=True)
        # 2. per mesh: current MO2 winner, hash, copy
        todo = []
        for n, m in enumerate(batch["meshes"], 1):
            e = self._entry_plan(bdir, m)
            say(f"Preparing {n}/{len(batch['meshes'])}: {e['game_rel']}")
            if ref_problems:
                self._set(bdir, batch, m, "REVIEW", ref_problems)
                continue
            cands = resolve_data_file(self.inst.providers, e["game_rel"])
            win = cands[0] if cands else None
            if win is None:
                self._set(bdir, batch, m, "FAILED", ["the game mesh is no longer provided by any mod"])
                continue
            if win[0] != e["source"]["provider"] or not _same_path(win[1], e["source"]["path"]):
                self._set(bdir, batch, m, "REVIEW", [
                    f"SOURCE_WINNER_CHANGED: the MO2 winner is now '{win[0]}' ({win[1]}), not the analyzed "
                    f"'{e['source']['provider']}' ({e['source']['path']}); a stale or losing copy is never converted"])
                continue
            h = sha256_file(Path(win[1]))
            if h != e["source"]["sha256"]:
                self._set(bdir, batch, m, "REVIEW", [f"SOURCE_CHANGED: {e['game_rel']} changed since the plan "
                                                      f"({h[:16]}… vs {e['source']['sha256'][:16]}…)"])
                continue
            src_copy, gen, raw = self._paths(bdir, m, e)
            shutil.copy2(win[1], src_copy)
            if sha256_file(src_copy) != h:
                self._set(bdir, batch, m, "FAILED", ["the workspace copy does not match the source (copy failed)"])
                continue
            (src_copy.parent.parent / "source.sha256").write_text(f"{h}  {e['file_name']}\n", encoding="utf-8")
            for f in (gen, raw):
                if f.exists():
                    f.unlink()
            todo.append((m, e))
        if cancel():
            for m, _ in todo:
                self._set(bdir, batch, m, "FAILED", ["cancelled before Outfit Studio was started"])
            return self._finish(bdir, batch, "CANCELLED", say)
        if not todo:
            return self._finish(bdir, batch, "RAN", say)
        # 3. runtime
        rt = self.eng.resolve_runtime(prepare=True)
        batch["runtime"] = rt.to_dict()
        if not rt.headless_supported:
            for m, _ in todo:
                self._set(bdir, batch, m, "FAILED", [f"Outfit Studio not usable: {rt.reason}"])
            return self._finish(bdir, batch, "FAILED", say)
        write_runtime_config(Path(rt.folder), project, bdir / "Data")
        conv = self.inst.profile.conversion
        interactive = (batch.get("run_mode") or rt.recommended_run_mode) == "interactive"
        groups = [[x] for x in todo] if batch.get("session_mode") == "per_project" else [todo]
        # 4. Outfit Studio sessions
        for si, group in enumerate(groups, 1):
            name = safe_script_name(f"cbbe3bbb_direct_{bdir.name}" + (f"_{si}" if len(groups) > 1 else ""))
            parts = [{"entry": e, "source": self._paths(bdir, m, e)[0], "output": self._paths(bdir, m, e)[2]} for m, e in group]
            sc = build_direct_script(name, parts, plan.reference, plan.settings)
            try:
                ranges = check_direct_script(sc, bdir)
            except ScriptInvariantError as exc:
                for m, _ in group:
                    self._set(bdir, batch, m, "FAILED", [f"script safety check failed: {exc}"])
                continue
            (project / "Automations" / f"{name}.xml").write_text(sc.to_xml(), encoding="utf-8")
            (bdir / f"{name}.xml").write_text(sc.to_xml(), encoding="utf-8")
            sess = {"id": si, "script": name, "meshes": [m["id"] for m, _ in group],
                    "ranges": {m["id"]: list(r) for (m, _), r in zip(group, ranges)},
                    "mode": "interactive" if interactive else "headless", "runtime_folder": rt.folder}
            batch["sessions"].append(sess)
            for m, _ in group:
                m["status"] = "CONVERTING"
            if interactive:
                say(f"Waiting for Outfit Studio (session {si}/{len(groups)}). In Outfit Studio: File > Open Automation… "
                    f"(Ctrl+Shift+A), choose '{name}', press Execute ONCE (it converts {len(group)} mesh(es) in turn), confirm "
                    "the final message, close the Automation window and close Outfit Studio (answer 'No' if asked to save). "
                    "No 'Choose cloth data' dialog is expected; if one appears, press Cancel and report it.")
            else:
                say(f"Running Outfit Studio {rt.version} headless (session {si}/{len(groups)}, {len(group)} mesh(es))…")
            sess["started"] = time.time()
            _jsave(bdir / "batch.json", batch)
            timeout = (conv.interactive_timeout_s if interactive else conv.automation_timeout_s) + 60 * len(group)
            run = self.eng.runner(Path(rt.folder), Path(rt.path), name, timeout, cancel, interactive=interactive)
            sess["ended"] = time.time()
            sess.update({"exit_code": run.exit_code, "seconds": run.seconds, "timed_out": run.timed_out,
                         "cancelled": run.cancelled, "crashed": run.crashed, "command": run.command})
            (bdir / "logs" / f"Log_OS.session{si}.txt").write_text(run.log_text, encoding="utf-8")
            (bdir / "logs" / f"stdout.session{si}.txt").write_text(run.stdout + "\n--- stderr ---\n" + run.stderr, encoding="utf-8")
            _jsave(bdir / "batch.json", batch)
        # 5. validate every mesh on its own
        self._validate_all(bdir, batch, [m for m, _ in todo], say, "result.json")
        return self._finish(bdir, batch, "RAN", say)

    def _session_of(self, batch: dict, mid: str) -> Optional[dict]:
        # 0.5.3: the latest session that ran this mesh (a retry must be judged by its own session window)
        return next((s for s in reversed(batch.get("sessions", [])) if mid in s["meshes"]), None)

    def _evidence(self, bdir: Path, sess: dict, m: dict, gen: Path, log_text: str) -> list[tuple[str, bool, str]]:
        out = []
        project = str((bdir / "Data" / BODYSLIDE_SUBTREE))
        ls = parse_log_session(log_text)
        from .evidence import _norm

        out.append(("execution: Log_OS.txt is from an Outfit Studio session on this workspace",
                    bool(log_text.strip()) and _norm(project) in [_norm(p) for p in ls.project_paths],
                    f"project path in config: {ls.project_paths[-1] if ls.project_paths else 'not in log'}"))
        problems = []
        if sess.get("cancelled"):
            problems.append("cancelled while Outfit Studio was running")
        if sess.get("crashed"):
            problems.append(f"Outfit Studio crashed ({sess['crashed']})")
        if sess.get("timed_out"):
            problems.append("Outfit Studio did not finish in time (a dialog may have blocked it)")
        if sess.get("mode") == "headless" and sess.get("exit_code") not in (0, 10):
            problems.append(f"Outfit Studio automation exit code {sess.get('exit_code')}")
        out.append(("execution: Outfit Studio session finished normally", not problems, "; ".join(problems) or
                    f"{sess.get('mode')}, exit code {sess.get('exit_code')}, {sess.get('seconds')} s"))
        st, en = sess.get("started"), sess.get("ended")
        if gen.is_file():
            mt = gen.stat().st_mtime
            ok = st is not None and en is not None and st - MTIME_TOLERANCE_S <= mt <= en + MTIME_TOLERANCE_S
            det = f"written {time.strftime('%H:%M:%S', time.localtime(mt))}, session " + (
                f"{time.strftime('%H:%M:%S', time.localtime(st))}–{time.strftime('%H:%M:%S', time.localtime(en))}" if st and en else "?")
        else:
            ok, det = False, "the ExportFile step did not write the generated NIF (not executed, aborted or failed)"
        out.append(("execution: generated NIF written by this Outfit Studio session", ok, det))
        r = sess.get("ranges", {}).get(m["id"])
        errs = (ls.errors_in(*r) + ls.unattributed_errors()) if r else ls.error_lines
        out.append(("execution: no automation step errors in Log_OS.txt", not errs, " | ".join(errs[:3]) or "none"))
        return out

    def _validate_all(self, bdir: Path, batch: dict, metas: list[dict], say: ProgressFn, result_name: str) -> None:
        plan = DirectBatchPlan.from_dict(_jload(bdir / "plan.json"))
        settings = self._settings()
        restore = bool(batch.get("restore"))
        for n, m in enumerate(metas, 1):
            e = self._entry_plan(bdir, m)
            src_copy, gen, raw = self._paths(bdir, m, e)
            sess = self._session_of(batch, m["id"])
            reasons: list[str] = []
            extra: dict = {"source_sha256": e["source"]["sha256"], "session": sess["id"] if sess else None,
                           "source_copy": str(src_copy), "generated": str(gen), "generated_raw": str(raw)}
            try:
                if sess is None:
                    raise _Stop("Outfit Studio was never started for this mesh")
                lf = bdir / "logs" / f"Log_OS.session{sess['id']}.txt"
                log_text = lf.read_text(encoding="utf-8", errors="replace") if lf.is_file() else ""
                ev = self._evidence(bdir, sess, m, raw, log_text)
                target = gen
                rcheck = None
                if restore:
                    rest = self._restore(bdir, m, e, src_copy, raw, gen, plan, say, n, len(metas))
                    extra["restoration"] = rest
                    extra["stage"] = "RESTORED" if rest.get("ok") else ("RAW_EXPORTED" if raw.is_file() else "NOT_EXPORTED")
                    if not rest.get("ok"):
                        target = raw  # report exactly what the raw export breaks; it can never be committed
                    rcheck = rest
                else:
                    extra["stage"] = "LEGACY_NO_RESTORATION"
                say(f"Validating {'restored' if restore and rcheck and rcheck.get('ok') else ''} NIF {n}/{len(metas)}: "
                    f"{e['game_rel']}".replace("  ", " "))
                val, facts = validate_direct_mesh(e, src_copy, target, Path(e["source"]["path"]), self.kb, settings, plan.reference)
                if rcheck is not None:
                    _restore_checks(val, rcheck)
                for name, ok, det in ev:
                    val.add(name, ok, det)
                extra["validation"] = val.to_dict()
                extra["facts"] = facts
                (bdir / "meshes" / m["id"] / "logs" / result_name.replace("result", "validation").replace(".json", ".txt")
                 ).write_text("\n".join(val.lines()), encoding="utf-8")
                status = mesh_status(extra["validation"]["checks"])
                if any(not ok for name, ok, _ in ev):
                    status = "FAILED"  # not executed, crashed, cancelled, exit code: never REVIEW
                reasons = [f"validation failed: {c.name} — {c.detail}" for c in val.failures()]
                if rcheck is not None and not rcheck.get("ok"):
                    status = "FAILED"
                    reasons.insert(0, f"{rcheck.get('code')}: " + "; ".join(rcheck.get("reasons", [])[:3]))
                if status == "VALIDATED":
                    extra["generated_sha256"] = facts.get("generated_sha256")
                    if raw.is_file():
                        extra["generated_raw_sha256"] = sha256_file(raw)
            except _Stop as exc:
                status, reasons = "FAILED", [str(exc)]
            except Exception as exc:  # noqa: BLE001 - one mesh never breaks the batch
                log.exception("direct validation crashed for %s", m["mesh"])
                status, reasons = "FAILED", [f"validation crashed: {exc}"]
            if status != "VALIDATED" and restore and gen.exists():
                gen.unlink()  # a non-VALIDATED mesh keeps no committable candidate
            self._set(bdir, batch, m, status, reasons, extra, result_name)

    def _restore(self, bdir: Path, m: dict, e: dict, src_copy: Path, raw: Path, gen: Path, plan: "DirectBatchPlan",
                 say: ProgressFn, n: int, total: int) -> dict:
        from .restore import restore_immutable_vertices

        if not raw.is_file():
            if gen.exists():
                gen.unlink()
            return {"ok": False, "status": "REFUSED", "code": "NO_RAW_EXPORT",
                    "reasons": ["Outfit Studio did not export this mesh"], "shapes": {}}
        say(f"Inspecting raw export {n}/{total}: {e['game_rel']}")
        say(f"Restoring immutable vertex attributes {n}/{total}: {e['game_rel']}")
        res = restore_immutable_vertices(src_copy, raw, gen, plan.reference.get("shape", ""))
        d = res.to_dict()
        _jsave(Path(bdir) / "meshes" / m["id"] / "restoration.json", d)
        return d

    def _finish(self, bdir: Path, batch: dict, state: str, say: ProgressFn) -> dict:
        batch["state"] = state
        _jsave(bdir / "batch.json", batch)
        s = self.summary(bdir, batch)
        c = s["counts"]
        say(f"Direct-NIF batch {state.lower()}: validated {c.get('VALIDATED', 0)}, review {c.get('REVIEW', 0)}, "
            f"failed {c.get('FAILED', 0)}, committed {c.get('COMMITTED', 0)}")
        return s

    # ------------------------------------------------------------------ validate again (no Outfit Studio)
    def validate(self, bdir: Path, progress: Optional[ProgressFn] = None) -> dict:
        say = progress or (lambda m: None)
        bdir = Path(bdir)
        with self._lock:
            batch = self._load(bdir)
            metas = [m for m in batch["meshes"] if m["status"] != "COMMITTED" and self._session_of(batch, m["id"])]
            if not metas:
                raise DirectBatchError("Outfit Studio never ran for this batch (or everything is committed); nothing to validate")
            self._validate_all(bdir, batch, metas, say, "result.revalidated.json")
            return self._finish(bdir, batch, batch["state"], say)

    # ------------------------------------------------------------------ commit
    def commit(self, bdir: Path, only: Optional[list[str]] = None, progress: Optional[ProgressFn] = None) -> dict:
        say = progress or (lambda m: None)
        bdir = Path(bdir)
        with self._lock:
            batch = self._load(bdir)
            plan = DirectBatchPlan.from_dict(_jload(bdir / "plan.json"))
            out_mod = Path(batch["output_mod"])
            for m in batch["meshes"]:
                if only and not any(o.lower() in (m["id"].lower(), m["direct_id"].lower(), m["file_name"].lower(),
                                                  normalize_mesh_path(m["mesh"])) or normalize_mesh_path(o) == normalize_mesh_path(m["mesh"])
                                     for o in only):
                    continue
                if m["status"] != "VALIDATED":
                    if m["status"] != "COMMITTED":
                        say(f"Not committed ({m['status']}): {m['mesh']}")
                    continue
                problems = self._commit_one(bdir, batch, plan, m, out_mod, say)
                if problems:
                    m["commit_error"] = problems
                    say(f"Not committed: {m['mesh']}: {problems[0]}")
                _jsave(bdir / "batch.json", batch)
            done = [m for m in batch["meshes"] if m["status"] == "COMMITTED"]
            state = "COMMITTED" if done and len(done) == len(batch["meshes"]) else ("PARTIALLY_COMMITTED" if done else batch["state"])
            return self._finish(bdir, batch, state, say)

    def _commit_one(self, bdir: Path, batch: dict, plan: DirectBatchPlan, m: dict, out_mod: Path, say: ProgressFn) -> list[str]:
        e = self._entry_plan(bdir, m)
        r = self.latest_result(bdir, m) or {}
        src_copy, gen, raw = self._paths(bdir, m, e)
        problems = []
        if r.get("status") != "VALIDATED" or mesh_status(r.get("validation", {}).get("checks", [])) != "VALIDATED":
            return ["the latest validation result is not VALIDATED"]
        want = r.get("generated_sha256")
        if not gen.is_file() or sha256_file(gen) != want:
            problems.append("the generated NIF changed after validation; run validate-direct-batch again")
        rest = r.get("restoration")
        if batch.get("restore"):
            if not rest or not rest.get("ok") or rest.get("final_sha256") != want:
                problems.append("the post-export restoration did not produce this validated file")
            elif not raw.is_file() or sha256_file(raw) != r.get("generated_raw_sha256"):
                problems.append("the raw Outfit Studio export changed after validation; run validate-direct-batch again")
        # the source: still the analyzed MO2 winner with the analyzed content
        cands = resolve_data_file(self.inst.providers, e["game_rel"])
        win = cands[0] if cands else None
        if win is None or win[0] != e["source"]["provider"] or not _same_path(win[1], e["source"]["path"]):
            problems.append(f"SOURCE_WINNER_CHANGED: the MO2 winner is now {win[0] if win else 'nothing'}")
        elif sha256_file(Path(win[1])) != e["source"]["sha256"]:
            problems.append("SOURCE_CHANGED: the source game mesh changed since the conversion")
        # the target: the planned game-relative path inside the planned output mod
        if str(out_mod) != plan.output_mod or not out_mod.name:
            problems.append("the output mod differs from the plan")
        target = out_mod / e["game_rel"]
        if normalize_mesh_path(str(target.relative_to(out_mod))) != e["game_key"] or not _inside(target, out_mod):
            problems.append("the target is not the planned game-relative path")
        src_prov = next((p for p in self.inst.providers if p.name == e["source"]["provider"]), None)
        if _same_path(target, e["source"]["path"]) or (src_prov is not None and _inside(target, src_prov.root)):
            problems.append("the target would be inside the source package")
        problems += check_output_mod(out_mod, self.inst, [])
        owner = _output_owner(target, out_mod, e["game_key"])
        if owner:
            problems.append(f"{e['game_rel']} already exists in the output mod: {owner}. Not overwritten "
                            "(remove or move it yourself if it should be replaced)")
        if problems:
            return problems
        say(f"Committing {e['game_rel']}…")
        man_file = out_mod / "manifest.json"
        man_backup = man_file.read_bytes() if man_file.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".cbbe3bbb-tmp")
        written = False
        try:
            shutil.copy2(gen, tmp)
            if sha256_file(tmp) != want:
                raise OSError("the temporary copy does not match the validated file")
            if target.exists():  # appeared meanwhile
                raise FileExistsError(f"{target} appeared during the commit")
            os.replace(tmp, target)
            written = True
            back = sha256_file(target)
            if back != want:
                raise OSError("read-back SHA-256 of the written file differs")
            man = Manifest.load(out_mod)
            facts = r.get("facts", {})
            rec = {
                "id": f"{bdir.name}/{m['id']}", "kind": "DIRECT_NIF", "direct_nif_id": e["id"], "timestamp": _now(),
                "tool_version": __version__, "status": "SUCCESS", "package": batch["package"],
                "game_path": e["mesh"], "game_key": e["game_key"],
                "source": {"mod": e["source"]["provider"], "path": e["game_rel"], "abs_path": e["source"]["path"],
                           "sha256": e["source"]["sha256"], "size": e["source"]["size"]},
                "generated": {"path": e["game_rel"], "sha256": back, "size": target.stat().st_size},
                "reference": {k: v for k, v in plan.reference.items() if k != "files"} | {
                    "files": [{"role": f["role"], "rel": f["rel"], "provider": f["provider"], "sha256": f["sha256"]}
                              for f in plan.reference.get("files", [])]},
                "items": e["items"], "item_names": e["item_names"], "sex": e["sex"],
                "shapes": [{"name": sd["name"], "decision": sd["decision"], "regions": sd["planned_regions"],
                            "planned_bones": sd["planned_bones"], "radius": sd["planned_radius"],
                            "expected_bones": sd.get("predicted_full_copy_physics_bones", []),
                            "actual_physics_bones": facts.get("shapes", {}).get(sd["name"], {}).get("physics_bones", {})}
                           for sd in e["shapes"]],
                "untouched_shapes": e["untouched_shapes"],
                "restoration": ({"applied": True, "raw_export_sha256": rest.get("raw_sha256"),
                                 "final_sha256": rest.get("final_sha256"), "bytes_restored": rest.get("bytes_restored"),
                                 "other_raw_differences": rest.get("other_raw_differences"),
                                 "shapes": {n: {k: v for k, v in d.items() if k != "fields_restored"}
                                            for n, d in rest.get("shapes", {}).items()}}
                                if batch.get("restore") and rest else {"applied": False}),
                "settings": dict(plan.settings), "outfit_studio": batch.get("runtime", {}),
                "run_mode": (self._session_of(batch, m["id"]) or {}).get("mode", ""),
                "validation": {"status": "VALIDATED", "checks": len(r.get("validation", {}).get("checks", [])),
                               "failed_checks": [], "metrics": r.get("validation", {}).get("metrics", {})},
                "batch": {"id": bdir.name, "workspace": str(bdir / "meshes" / m["id"])},
                "acceptance": {"state": "committed, awaiting in-game test", "history": []},
            }
            man.add_direct_record(rec)
            man.save()
        except Exception as exc:  # noqa: BLE001 - one transaction per mesh
            try:
                if tmp.exists():
                    tmp.unlink()
                if written and target.exists():
                    target.unlink()
                if man_backup is not None:
                    man_file.write_bytes(man_backup)
            except OSError:
                pass
            return [f"could not write the output mod: {exc}"]
        m["status"] = "COMMITTED"
        m.pop("commit_error", None)
        m["committed"] = {"at": _now(), "file": str(target), "sha256": want}
        _jsave(bdir / "meshes" / m["id"] / "commit.json", m["committed"])
        return []

    # ------------------------------------------------------------------ summary
    def summary(self, bdir: Path, batch: Optional[dict] = None) -> dict:
        bdir = Path(bdir)
        batch = batch or self._load(bdir)
        rows = []
        for m in batch["meshes"]:
            r = self.latest_result(bdir, m) or {}
            checks = r.get("validation", {}).get("checks", [])
            failed = [c["name"] for c in checks if c.get("required", True) and not c.get("ok")]
            rows.append({"id": m["id"], "direct_id": m["direct_id"], "mesh": m["mesh"], "items": m.get("items", []),
                         "status": m["status"], "stage": r.get("stage", ""),
                         "restoration": {k: r.get("restoration", {}).get(k) for k in ("status", "code", "bytes_restored")}
                         if r.get("restoration") else {}, "reasons": r.get("reasons", [])[:6], "failed_checks": failed,
                         "checks": len(checks), "commit_error": m.get("commit_error", []), "committed": m.get("committed")})
        counts: dict[str, int] = {}
        for x in rows:
            counts[x["status"]] = counts.get(x["status"], 0) + 1
        s = {"id": batch["id"], "kind": "DIRECT_NIF", "package": batch["package"], "state": batch["state"],
             "output_mod": batch.get("output_mod"), "tool_version": __version__, "updated": _now(), "counts": counts,
             "meshes": rows, "sessions": batch.get("sessions", []), "notes": batch.get("notes", [])}
        _jsave(bdir / "summary.json", s)
        (bdir / "report.txt").write_text(direct_summary_text(s), encoding="utf-8")
        return s


class _Stop(Exception):
    pass


def direct_summary_text(s: dict) -> str:
    L = [f"Direct-NIF batch {s['id']} — package {s['package']} — state {s['state']}",
         "Counts: " + ", ".join(f"{k} {v}" for k, v in sorted(s["counts"].items())), ""]
    for x in s["meshes"]:
        L.append(f"{x['status']:<11} {x['direct_id']}  {x['mesh']}  →  {', '.join(x['items'][:4])}  ({x['checks']} checks)"
                 + (f"  [{x['stage']}]" if x.get("stage") else ""))
        rs = x.get("restoration") or {}
        if rs:
            L.append(f"    post-export restoration: {rs.get('status')}" + (f" {rs.get('code')}" if rs.get("code") else "")
                     + (f", {rs.get('bytes_restored')} bytes restored" if rs.get("status") == "RESTORED" else ""))
        L += [f"    - {r}" for r in x["reasons"]]
        L += [f"    ! commit: {r}" for r in x["commit_error"]]
        if x.get("committed"):
            L.append(f"    written: {x['committed']['file']}  sha256 {x['committed']['sha256'][:16]}…")
    if any(x["status"] == "COMMITTED" for x in s["meshes"]):
        L += ["", "NEXT: verify-direct-build (read-only) BEFORE starting the game.", "", LAYERING_NOTE]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------------------------------------------
# idempotency (used by the 0.4.0 analysis) and post-commit verification
# ---------------------------------------------------------------------------------------------------------------

def latest_direct_records(out_mod: Optional[Path]) -> dict[str, dict]:
    if not out_mod or not (Path(out_mod) / "manifest.json").is_file():
        return {}
    try:
        man = Manifest.load(Path(out_mod))
    except (ValueError, OSError, json.JSONDecodeError):
        return {}
    out = {}
    for r in man.direct_records:
        if r.get("status") == "SUCCESS":
            out[r.get("game_key", "")] = r
    return out


def existing_direct_status(rec: dict, cands: list, package: str, out_mod: Path, inst) -> tuple[str, str, list[str]]:
    """(action, status, reasons) of a game mesh with a committed Direct-NIF conversion."""
    info = f"batch {rec.get('batch', {}).get('id', '?')}, {rec.get('timestamp', '?')}"
    win = cands[0] if cands else None
    if win is None or not _inside(win[1], out_mod):
        return ("REVIEW", "OUTPUT_NOT_WINNER",
                [f"a Direct-NIF conversion is recorded ({info}) but the output mod is not the MO2 winner "
                 f"({win[0] if win else 'no provider'} wins): the game does not load it; check the mod order"])
    gh = sha256_file(Path(win[1]))
    if gh != rec.get("generated", {}).get("sha256"):
        return ("REVIEW", "OUTPUT_CHANGED", [f"the generated mesh in the output mod no longer matches the manifest ({info}); "
                                             "not converted again automatically"])
    pkg = next((c for c in cands if c[0] == package), None)
    if pkg is None:
        return ("REVIEW", "SOURCE_CHANGED", [f"converted ({info}), but the package no longer provides the source mesh"])
    if sha256_file(Path(pkg[1])) != rec.get("source", {}).get("sha256"):
        return ("REVIEW", "SOURCE_CHANGED", [f"converted ({info}), but the source game mesh changed since: the "
                                             "conversion is stale; never overwritten automatically — decide whether to reconvert"])
    changed = []
    for f in rec.get("reference", {}).get("files", []):
        vf = inst.vfs.resolve(f.get("rel", "")) if getattr(inst, "vfs", None) else None
        if vf is None or sha256_file(Path(vf.abs_path)) != f.get("sha256"):
            changed.append(f.get("rel"))
    if changed:
        return ("REVIEW", "TARGET_REFERENCE_CHANGED", [f"converted ({info}), but the installed CBBE-3BBB target reference "
                                                       "changed since: " + ", ".join(changed[:3]) + "; the generated weights "
                                                       "were copied from the old reference (regeneration may be required)"])
    return ("KEEP", "ALREADY_CONVERTED_DIRECT_NIF",
            [f"already converted by Direct-NIF ({info}); source, generated file and target reference unchanged (SHA-256)"])


@dataclass
class DirectBuildCheck:
    mesh: str
    direct_nif_id: str = ""
    status: str = "NOT_CHECKED"  # DIRECT_OK | NOT_COMMITTED | OUTPUT_NOT_WINNER | OUTPUT_MISMATCH | SOURCE_CHANGED | GENERATED_INVALID
    output_file: str = ""
    winner: dict = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)
    source_sha256: str = ""
    generated_sha256: str = ""
    winner_sha256: str = ""
    validation_status: str = ""
    game_items: list[dict] = field(default_factory=list)
    shapes: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_at: str = ""

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def to_dict(self) -> dict:
        return asdict(self)

    def text(self) -> str:
        L = [f"DIRECT-NIF VERIFY  {self.mesh}   →  {self.status}",
             f"  Direct-NIF ID: {self.direct_nif_id}   validation at commit: {self.validation_status or '—'}",
             f"  output-mod file: {self.output_file or '—'}"]
        if self.winner:
            L.append(f"  winning provider (what the game loads): {self.winner.get('provider')}: {self.winner.get('path')}")
        for c in self.candidates[1:]:
            L.append(f"  overridden: {c.get('provider')}: {c.get('path')}")
        L.append(f"  SHA-256 winner {self.winner_sha256[:16] or '—'}…  generated {self.generated_sha256[:16] or '—'}…  "
                 f"source {self.source_sha256[:16] or '—'}…")
        for s in self.shapes:
            L.append(f"  shape '{s['name']}' ({s['decision']}): expected 3BBB bones {', '.join(s['expected']) or '—'}")
            L.append(f"      actual 3BBB bones: " + (", ".join(f"{b} {n}" for b, n in s["actual"].items()) or "none"))
        L.append("  checks:")
        L += [f"    [{'OK' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}" for c in self.checks]
        L.append("  IN-GAME ITEMS using this mesh (test these):" if self.game_items else "  in-game items: none found")
        L += [f"    '{i.get('armor_name') or i.get('armor_editor_id')}'  (ARMO {i.get('armor_editor_id')} {i.get('armor_form_id')}, "
              f"ARMA {i.get('addon_editor_id')}, {i.get('plugin')})" for i in self.game_items]
        L += [f"  ! {w}" for w in self.warnings]
        return "\n".join(L)


def verify_direct_build(rec: Optional[dict], mesh: str, out_mod: Path, inst, physics_bones: list[str],
                        package_root: Optional[Path]) -> DirectBuildCheck:
    """Read-only: is the committed Direct-NIF mesh what the game loads?"""
    res = DirectBuildCheck(mesh=mesh, direct_nif_id=direct_id(mesh), checked_at=_now())
    if rec is None:
        res.status = "NOT_COMMITTED"
        res.add("committed Direct-NIF conversion recorded in the manifest", False, "no record")
        return res
    game_rel = rec.get("generated", {}).get("path") or _game_rel(mesh)
    res.output_file = str(Path(out_mod) / game_rel)
    res.source_sha256 = rec.get("source", {}).get("sha256", "")
    res.generated_sha256 = rec.get("generated", {}).get("sha256", "")
    res.validation_status = rec.get("validation", {}).get("status", "")
    items, _ = package_items_for_mesh(package_root, game_rel)
    res.game_items = [i.to_dict() for i in items] or list(rec.get("items", []))
    out_file = Path(res.output_file)
    res.add("committed file present in the output mod", out_file.is_file(), res.output_file)
    if not out_file.is_file():
        res.status = "NOT_COMMITTED"
        return res
    cands = resolve_data_file(inst.providers, game_rel)
    res.candidates = [{"provider": n, "path": str(p), "size": s} for n, p, s, _ in cands]
    if not cands:
        res.status = "OUTPUT_NOT_WINNER"
        res.add("output mod is the MO2 winner", False, "no enabled provider has the file (is the output mod enabled?)")
        return res
    wn, wp, _, _ = cands[0]
    res.winner = res.candidates[0]
    res.winner_sha256 = sha256_file(Path(wp))
    is_out = _inside(wp, out_mod)
    res.add("output mod is the MO2 winner", is_out, f"winner {wn}" + ("" if is_out else f" — the output mod must be ranked "
                                                                           "ABOVE the source package in MO2"))
    if not is_out:
        res.status = "OUTPUT_NOT_WINNER"
        return res
    same = res.winner_sha256 == res.generated_sha256
    res.add("winning file is exactly the committed generated file (SHA-256)", same,
            f"{res.winner_sha256[:16]}… vs {res.generated_sha256[:16]}…")
    if not same:
        res.status = "OUTPUT_MISMATCH"
        return res
    pkg = next((c for c in cands if c[0] == rec.get("source", {}).get("mod")), None)
    src_ok = pkg is not None and sha256_file(Path(pkg[1])) == res.source_sha256
    res.add("source game mesh unchanged since the conversion (SHA-256)", src_ok,
            (str(pkg[1]) if pkg else "source no longer provided") if not src_ok else f"{rec['source']['mod']}")
    if not src_ok:
        res.status = "SOURCE_CHANGED"
        return res
    try:
        nif = read_nif(Path(wp))
    except (NifError, OSError, ValueError) as exc:
        res.add("winning file parses", False, str(exc))
        res.status = "GENERATED_INVALID"
        return res
    phys = set(physics_bones)
    names = [s.name for s in nif.shapes]
    want = [s["name"] for s in rec.get("shapes", [])]
    res.add("expected shapes (and only those)", names == want, f"{names} (expected {want})")
    ok_w = names == want
    for sd in rec.get("shapes", []):
        shp = nif.shape(sd["name"])
        actual = {}
        if shp is not None:
            b, w = shp.weight_matrix()
            actual = {x: int((w[:, b.index(x)] > 0.001).sum()) for x in b if x in phys and (w[:, b.index(x)] > 0.001).any()}
        exp = list(sd.get("expected_bones") or [])
        res.shapes.append({"name": sd["name"], "decision": sd.get("decision"), "expected": exp, "actual": actual})
        if sd.get("decision") == "CONVERT":
            good = bool(exp) and all(actual.get(x) for x in exp) and set(actual) <= set(sd.get("planned_bones", exp))
            ok_w &= good
            res.add(f"{sd['name']}: expected 3BBB weights present", good,
                    f"{len(actual)} 3BBB bones: " + ", ".join(sorted(actual)))
    res.status = "DIRECT_OK" if ok_w else "GENERATED_INVALID"
    return res


def write_verify_report(results: list[DirectBuildCheck], folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pj, pt = folder / f"{stamp}_direct_verify.json", folder / f"{stamp}_direct_verify.txt"
    pj.write_text(json.dumps([r.to_dict() for r in results], indent=1), encoding="utf-8")
    pt.write_text("\n\n".join(r.text() for r in results) + "\n", encoding="utf-8")
    return {"json": pj, "txt": pt}
