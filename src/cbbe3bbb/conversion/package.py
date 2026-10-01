"""Phase 3: package analysis and the Batch Preview (the batch Dry Run). Writes nothing.

A *package* is one MO2 mod (or other data provider). Every BodySlide project that
the package provides (the MO2 winner of its ``SliderSets`` file) is classified by the
Phase 1 analyzer, then checked against the output mod's ``manifest.json`` and planned
shape by shape with the same :func:`build_plan` that the single-project conversion
uses. The result is a :class:`PackagePreview`: per project an action
(CONVERT / KEEP / REVIEW / SKIP / UNSUPPORTED / ERROR), a status, the reasons, the
per-shape region decisions and, for CONVERT, the complete conversion plan.

Package-level rules on top of the analyzer (all conservative):

* A project that already has an AUTO conversion recorded in the manifest is
  ``KEEP / ALREADY_CONVERTED`` if the generated files and all source files still have
  the recorded hashes, otherwise ``REVIEW / STALE`` (sources changed) or
  ``REVIEW / AUTO_CHANGED`` (generated files missing or edited). It is never
  converted again automatically.
* A generated project name that already exists without a manifest record is
  ``REVIEW / UNKNOWN_AUTO``.
* A CONVERT project that builds an embedded non-3BBB body into its mesh is
  ``REVIEW / EMBEDDED_BODY``: converting the clothes would leave a body without
  physics under them, and embedding the CBBE-3BBB body is not done automatically.
* A CONVERT project whose output mesh is already built by a 3BBB project (a manual
  conversion) is ``REVIEW / MANUAL_3BBB_VARIANT``.
* Only CONVERT projects can be selected. They are selected by default only when
  every converted shape has HIGH confidence.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .. import PRODUCT_NAME, __version__
from ..analysis.models import Level, ProjectResult
from ..analysis.reference import KnowledgeBase
from ..discovery.install import BODYSLIDE_SUBTREE, Installation
from .manifest import Manifest
from .gamedata import item_name_mismatch, load_order_items_for_mesh, package_items_for_mesh
from .bodies import BodyDetection, detect_body
from .outcomes import POLICY_ATTEMPTABLE, POLICY_BLOCKED, analyzer_review_attemptable, preview_bucket
from .plan import ConversionPlan, build_plan, generated_names, sha256_file
from .runtime import RuntimeInfo

__all__ = [
    "MAIN_GROUP",
    "ShapeDecision",
    "PackageProject",
    "PackagePreview",
    "package_group_name",
    "package_names",
    "build_package_preview",
    "existing_conversion_status",
    "expected_checks",
    "write_preview_reports",
]

MAIN_GROUP = "CBBE 3BBB Auto Conversions"
ACTIONS = ("CONVERT", "KEEP", "REVIEW", "SKIP", "UNSUPPORTED", "ERROR")
_CONF = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0, "NONE": 0, "": 0}


def package_group_name(package: str) -> str:
    """BodySlide group with only this package's AUTO projects (for a package-only Batch Build)."""
    clean = re.sub(r'[\\/:*?"<>|]', "_", package).strip()
    return f"{MAIN_GROUP} - {clean}"


@dataclass
class ShapeDecision:
    name: str
    vertices: int = 0
    role: str = "outfit"  # outfit | body | unlisted
    decision: str = "NONE"  # CONVERT | NONE | UNKNOWN | BODY
    regions: list[str] = field(default_factory=list)  # planned regions (empty for NONE)
    allowed_bones: list[str] = field(default_factory=list)
    predicted_bones: list[str] = field(default_factory=list)  # physics bones a full copy would add
    copy_mode: str = ""
    confidence: str = ""
    reasons: list[str] = field(default_factory=list)
    region_details: dict = field(default_factory=dict)
    distance: dict = field(default_factory=dict)
    existing_physics_bones: dict = field(default_factory=dict)


@dataclass
class PackageProject:
    project: str
    osp_rel: str = ""
    set_index: int = 0
    shapedata_rel: str = ""
    output_mesh: str = ""
    family: str = ""
    physics: str = ""
    existing_3bbb: str = ""
    analyzer_action: str = ""
    analyzer_confidence: str = ""
    action: str = "ERROR"
    status: str = ""  # NEW | ALREADY_CONVERTED | STALE | AUTO_CHANGED | UNKNOWN_AUTO | EMBEDDED_BODY | MANUAL_3BBB_VARIANT | ...
    confidence: str = ""
    regions: list[str] = field(default_factory=list)
    shapes: list[ShapeDecision] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    selectable: bool = False
    selected: bool = False
    generated_project: str = ""
    existing_conversion: dict = field(default_factory=dict)
    expected_checks: list[str] = field(default_factory=list)
    game_items: list[dict] = field(default_factory=list)  # in-game items (package plugins) that use output_mesh
    plan: Optional[dict] = None  # ConversionPlan.to_dict() for CONVERT (and attemptable REVIEW projects)
    # Phase 3.2
    review_policy: str = ""  # REVIEW_ATTEMPTABLE | REVIEW_BLOCKED (REVIEW projects and non-default CONVERT ones)
    review_policy_reason: str = ""
    attempt_origin: str = ""  # SAFE | MEDIUM | REVIEW: why this project is (or would be) converted
    bucket: str = ""  # report bucket before any conversion (see outcomes.py)
    # 0.6.0 Body Architecture Foundation (empty in older previews: see apply_routing)
    source_body: str = ""  # detected body id (bodies.py)
    requested_source: str = ""  # auto | an explicit source body id
    target_body: str = ""
    route_id: str = ""
    route_status: str = ""  # PRODUCTION | ALREADY_COMPATIBLE | NOT_IMPLEMENTED | … | SOURCE_OVERRIDE_CONFLICT
    route_label: str = ""
    route_reason: str = ""
    geometry_stage: dict = field(default_factory=dict)
    physics_stage: dict = field(default_factory=dict)
    body_detection: dict = field(default_factory=dict)  # BodyDetection.to_dict(): body, confidence, evidence, …

    @property
    def display_name(self) -> str:
        from .outcomes import display_name

        return display_name(self.project, [g.get("armor_name", "") for g in self.game_items], self.output_mesh)

    @property
    def key(self) -> str:
        return f"{self.osp_rel.lower()}::{self.project}"

    def conversion_plan(self) -> Optional[ConversionPlan]:
        return ConversionPlan.from_dict(self.plan) if self.plan else None


@dataclass
class PackagePreview:
    package: str
    created: str = ""
    tool_version: str = __version__
    provider_kind: str = ""
    provider_path: str = ""
    output_mod: str = ""
    runtime: dict = field(default_factory=dict)
    run_mode: str = ""
    session_mode: str = ""
    reference: dict = field(default_factory=dict)
    regions_available: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    groups: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    projects: list[PackageProject] = field(default_factory=list)
    dry_run: list[dict] = field(default_factory=list)  # Outfit Studio sessions of the current selection (plan-batch)
    allow_body_replacement: bool = False  # Phase 3.1 opt-in (default off)
    source_mode: str = "BODYSLIDE_PROJECT"  # 0.4.0: BODYSLIDE_PROJECT / DIRECT_NIF / MIXED / NO_CONVERTIBLE_SOURCE
    direct_nif: dict = field(default_factory=dict)  # DirectNifReport.to_dict() when Direct-NIF analysis ran (read-only)
    # Phase 3.2
    conversion_mode: str = "SAFE"  # SAFE | ALL_POSSIBLE (the current selection)
    output_mod_state: str = ""  # ENABLED | DISABLED_IN_MO2 | NOT_IN_PROFILE | NOT_CREATED | UNKNOWN
    codes: list[str] = field(default_factory=list)  # machine-readable warnings, e.g. OUTPUT_MOD_DISABLED_IN_MO2
    # 0.6.0: the requested bodies of this preview (defaults reproduce 0.5.x: Auto Detect → CBBE 3BBB)
    requested_source: str = "auto"
    target_body: str = "cbbe_3bbb"
    body_counts: dict = field(default_factory=dict)  # detected source body id → number of projects

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PackagePreview":
        d = dict(d)
        projects = []
        for p in d.pop("projects", []):
            p = dict(p)
            shapes = [ShapeDecision(**s) for s in p.pop("shapes", [])]
            pk = set(PackageProject.__dataclass_fields__)
            projects.append(PackageProject(shapes=shapes, **{k: v for k, v in p.items() if k in pk}))
        known = set(cls.__dataclass_fields__)
        return cls(projects=projects, **{k: v for k, v in d.items() if k in known})

    def selected(self) -> list[PackageProject]:
        return [p for p in self.projects if p.selected]

    def select(self, names: Optional[list[str]] = None, deselect: Optional[list[str]] = None) -> list[str]:
        """Change the selection. Only selectable (CONVERT) projects can be selected; returns problems."""
        problems = []
        by = {p.project: p for p in self.projects}
        if names is not None:
            for p in self.projects:
                p.selected = False
            for n in names:
                p = by.get(n)
                if p is None:
                    problems.append(f"project '{n}' is not in package '{self.package}'")
                elif not p.selectable and not (self.conversion_mode == "ALL_POSSIBLE" and p.plan
                                               and p.review_policy == "REVIEW_ATTEMPTABLE"):
                    problems.append(f"'{n}' is {p.action} ({p.status}); only CONVERT projects (and, with "
                                    "--mode all-possible, attemptable REVIEW projects) can be converted")
                else:
                    p.selected = True
        for n in deselect or []:
            p = by.get(n)
            if p is None:
                problems.append(f"project '{n}' is not in package '{self.package}'")
            else:
                p.selected = False
        return problems

    def select_mode(self, mode: str) -> list[PackageProject]:
        """Deterministic selection for a conversion mode (Phase 3.2); returns the selected projects.

        ``SAFE``: CONVERT projects that are NEW and HIGH confidence in every converted shape (the preview default;
        never MEDIUM / REVIEW / BLOCKED / SKIP / UNSUPPORTED / ERROR / already converted, never embedded-body
        replacement). ``ALL_POSSIBLE``: SAFE plus CONVERT projects of lower confidence and REVIEW_ATTEMPTABLE projects.
        Validation is the same for every selected project."""
        from .outcomes import MODE_ALL_POSSIBLE, MODE_SAFE, POLICY_ATTEMPTABLE

        mode = (mode or MODE_SAFE).upper().replace("-", "_")
        if mode not in (MODE_SAFE, MODE_ALL_POSSIBLE):
            raise ValueError(f"unknown conversion mode {mode}")
        self.conversion_mode = mode
        for p in self.projects:
            safe = (p.action == "CONVERT" and p.selectable and p.status == "NEW" and p.attempt_origin == "SAFE")
            extra = mode == MODE_ALL_POSSIBLE and bool(p.plan) and (
                (p.action == "CONVERT" and p.selectable and p.status == "NEW" and p.attempt_origin == "MEDIUM")
                or p.review_policy == POLICY_ATTEMPTABLE)
            p.selected = safe or extra
        self.counts["selected"] = len(self.selected())
        return self.selected()

    def summary_lines(self) -> list[str]:
        c = self.counts
        L = [f"Package: {self.package}", f"Total projects: {sum(c.get(a, 0) for a in ACTIONS)}"]
        L += [f"  {a:<12}{c.get(a, 0):>4}" for a in ACTIONS]
        L.append(f"Selected for conversion: {len(self.selected())}")
        return L


# ---------------------------------------------------------------------------------------------
def package_names(inst: Installation) -> list[str]:
    """Data providers (mods) that ship at least one BodySlide project, highest priority first."""
    if inst.vfs is None:
        return []
    seen: dict[str, int] = {}
    for key, versions in inst.vfs.files.items():
        if not key.startswith("slidersets/") or not key.endswith(".osp"):
            continue
        for vf in versions:
            if vf.provider.kind in ("mod", "disabled-mod", "overwrite", "game", "extra"):
                seen[vf.provider.name] = vf.provider.priority
    return sorted(seen, key=lambda n: -seen[n])


def _hash_cache() -> Callable[[str], str]:
    memo: dict[str, str] = {}

    def h(path: str) -> str:
        if path not in memo:
            p = Path(path)
            memo[path] = sha256_file(p) if p.is_file() else "MISSING"
        return memo[path]
    return h


def existing_conversion_status(pr: ProjectResult, manifest: Optional[Manifest], out_mod: Optional[Path],
                               inst: Installation, suffix: str, hasher: Callable[[str], str]) -> Optional[tuple[str, str, list[str], dict]]:
    """(action, status, reasons, info) when an AUTO conversion of ``pr`` already exists, else None."""
    names = generated_names(pr.project, suffix)
    recs = [r for r in (manifest.records if manifest else [])
            if r.get("source", {}).get("project") == pr.project and r.get("source", {}).get("mod") == pr.mod
            and r.get("status") == "SUCCESS"]
    if recs:
        rec = recs[-1]
        info = {"id": rec.get("id"), "timestamp": rec.get("timestamp"), "generated_project": rec.get("target", {}).get("project"),
                "accepted_by": rec.get("accepted_by", "conversion"), "tool_version": rec.get("tool_version")}
        changed_gen = []
        for f in rec.get("generated_files", []):
            if f["path"].lower().endswith(".xml"):  # group files are shared and change with every commit
                continue
            p = Path(out_mod) / f["path"] if out_mod else None
            if p is None or not p.is_file():
                changed_gen.append(f"{f['path']} is missing")
            elif hasher(str(p)) != f.get("sha256"):
                changed_gen.append(f"{f['path']} was modified after the conversion")
        changed_src, changed_roles = [], set()
        for f in rec.get("source", {}).get("files", []):
            # compare with what MO2 provides *now* for that path (catches edits and a new winning mod)
            vf = inst.vfs.resolve(f.get("path", "")) if inst.vfs else None
            if vf is None:
                changed_src.append(f"{f.get('path')} ({f.get('role')}) is no longer provided")
                changed_roles.add(f.get("role", ""))
            elif hasher(str(vf.abs_path)) != f.get("sha256"):
                changed_src.append(f"{f.get('path')} ({f.get('role')})"
                                   + (f" now from '{vf.provider.name}'" if vf.provider.name != f.get("provider") else ""))
                changed_roles.add(f.get("role", ""))
        if changed_gen:
            return ("REVIEW", "AUTO_CHANGED",
                    [f"an AUTO conversion is recorded ({info['generated_project']}, {info['timestamp']}) but its files "
                     "do not match the manifest: " + "; ".join(changed_gen[:4]), "not converted again automatically"], info)
        if changed_src and all(r.startswith("reference") for r in changed_roles):
            mode = rec.get("conversion_mode", "outfit_only")
            return ("REVIEW", "TARGET_REFERENCE_CHANGED",
                    [f"converted as '{info['generated_project']}' ({info['timestamp']}), but the installed CBBE-3BBB target "
                     "reference changed since: " + ", ".join(changed_src[:4]),
                     ("the generated project contains a copy of the old target body weights" if mode == "embedded_body_replacement"
                      else "the generated weights were copied from the old target reference")
                     + "; regeneration may be required (never overwritten automatically)"], info)
        if changed_src:
            return ("REVIEW", "STALE",
                    [f"already converted as '{info['generated_project']}' ({info['timestamp']}), but source files changed since: "
                     + ", ".join(changed_src[:4]), "stale conversion: not overwritten automatically; decide whether to reconvert"], info)
        return ("KEEP", "ALREADY_CONVERTED",
                [f"already converted: '{info['generated_project']}' in the output mod ({info['timestamp']}, "
                 f"accepted by {info['accepted_by']})", "source and generated files unchanged (SHA-256 match the manifest)"], info)
    # a generated project with this name but no manifest record
    where = []
    if out_mod is not None and (Path(out_mod) / BODYSLIDE_SUBTREE / names["osp_rel"]).exists():
        where.append(f"output mod {Path(out_mod).name}")
    vf = inst.vfs.resolve(names["osp_rel"]) if inst.vfs else None
    if vf is not None and vf.provider.name not in where and not (out_mod and Path(vf.abs_path).is_relative_to(Path(out_mod))):
        where.append(vf.provider.name)
    if where:
        return ("REVIEW", "UNKNOWN_AUTO",
                [f"a project file {names['osp_rel']} already exists ({', '.join(where)}) without a manifest record",
                 "unknown existing conversion: never overwritten"], {})
    return None


def _shape_decisions(pr: ProjectResult, plan: Optional[ConversionPlan]) -> list[ShapeDecision]:
    sp_by = {sp.name: sp for sp in (plan.shape_plans if plan else [])}
    out = []
    for sa in pr.shapes:
        d = ShapeDecision(name=sa.name, vertices=sa.vertices,
                          distance={"median": sa.distance.median, "p90": sa.distance.p90,
                                    "within_radius": sa.distance.frac_within_radius},
                          existing_physics_bones=dict(sa.physics_bones))
        if sa.name not in pr.shape_names:
            d.role, d.decision = "unlisted", "NONE"
            d.reasons.append("not listed in the slider set")
        elif sa.is_body and plan is not None and sa.name in plan.replaced_body_shapes:
            br = plan.body_replacement
            d.role, d.decision = "body", "BODY_REPLACE"
            d.regions = []
            d.confidence = br.get("source_body_confidence", "")
            d.reasons.append(f"embedded body → {br.get('target_body_shape')} ({br.get('source_body_classification')}); "
                             "not a clothing weight-copy target; gets the reference weights by vertex identity")
            d.reasons += list(br.get("source_body_evidence", []))
        elif sa.is_body:
            d.role, d.decision = "body", "BODY"
            ident = next((x for x in (plan.body_identities if plan else []) if x.get("shape") == sa.name), None)
            d.reasons.append(f"body shape ({sa.body_match}); kept unchanged, never a weight-copy target"
                             + (f"; identity {ident.get('classification')}" if ident else ""))
        elif sa.name in sp_by:
            sp = sp_by[sa.name]
            d.decision, d.regions = "CONVERT", list(sp.regions)
            d.allowed_bones = list(sp.allowed_bones)
            d.predicted_bones = list(sp.predicted_full_copy_physics_bones)
            d.copy_mode, d.confidence = sp.copy_mode, sp.confidence
            d.reasons, d.region_details = list(sp.reasons), dict(sp.region_details)
        elif not sa.regions:
            d.decision = "UNKNOWN"
            d.reasons.append("no geometry/reference correspondence could be computed (not skinned or no reference)")
        else:
            d.decision = "NONE"
            near = [f"{r} {m.relevance} ({m.predicted_vertices} v)" for r, m in sa.regions.items() if m.predicted_vertices]
            d.reasons.append("no breast/butt/thigh physics region under this shape"
                             + (f"; only marginal: {', '.join(near)}" if near else ""))
            d.region_details = {r: {"planned": False, "relevance": m.relevance, "fit": m.fit,
                                    "predicted_vertices": m.predicted_vertices, "reference_coverage": m.reference_coverage,
                                    "median_distance": m.footprint_median_distance}
                                for r, m in sa.regions.items() if m.predicted_vertices}
        out.append(d)
    return out


def expected_checks(plan: ConversionPlan) -> list[str]:
    """The validation checks this project's result will have to pass (from its own plan)."""
    L = ["valid OSP", "original output path preserved", "original output filename preserved", "GenWeights preserved",
         "expected outfit shape(s) listed", "reference shape not listed in project", "original sliders preserved",
         "original zaps preserved", "slider data kept for outfit targets", "valid ShapeData NIF",
         "CBBE-3BBB body NOT embedded", "slider data (.osd) resolvable"]
    for sp in plan.shape_plans:
        L += [f"{sp.name}: geometry unchanged",
              f"{sp.name}: no physics bones outside planned regions ({', '.join(sp.regions)})"]
        L += [f"{sp.name}: {r.lower()} 3BBB bones present" for r in sp.regions]
        L += [f"{sp.name}: weights normalized", f"{sp.name}: cross-side / quality anomaly checks",
              f"{sp.name}: predicted vs actual physics weights consistent"]
    L += [f"{n}: weights unchanged" for n in plan.untouched_shapes]
    if plan.conversion_mode == "embedded_body_replacement":
        L += ["body replacement: source body positively identified",
              "body replacement: target reference unchanged since the plan (SHA-256)",
              "body replacement: exactly one body in the output",
              "body replacement: body geometry unchanged (deleted vertices stay deleted)",
              "body replacement: every body vertex coincides with the CBBE-3BBB reference",
              "body replacement: body weights identical to the CBBE-3BBB reference",
              "body replacement: no legacy physics bones weighted on the body",
              "body replacement: 3BBB physics bones of the reference present on the body",
              "body replacement: body slider morphs identical to the source and resolvable",
              "body replacement: body zaps preserved"]
    L += ["analyzer classifies generated project as 3BBB KEEP", "source files unchanged (SHA-256)",
          "execution: Log_OS.txt is from an Outfit Studio session on this workspace",
          "execution: planned outputs created by this Outfit Studio session",
          "execution: saved project matches the prepared SaveProject step",
          "execution: no automation step errors in Log_OS.txt"]
    return L


def _min_conf(values: list[str]) -> str:
    vals = [v for v in values if v]
    return min(vals, key=lambda v: _CONF.get(v, 0)) if vals else ""


_LEGACY_LABEL_BODY = {"cbbe": "cbbe", "cbbe physics (legacy)": "cbbe", "cbbe 3bbb": "cbbe_3bbb", "twb": "twb",
                      "twb 3bbb": "twb_3bbb", "fusion girl": "fusion_girl", "bodytalk": "bodytalk_male",
                      "male": "bodytalk_male", "3bbb (body unknown)": "unknown_3bbb"}


def detection_of(pp: "PackageProject") -> BodyDetection:
    """The body detection of a preview project; for a pre-0.6.0 preview only what its family label safely says."""
    if pp.body_detection:
        return BodyDetection.from_dict(pp.body_detection)
    body = _LEGACY_LABEL_BODY.get((pp.family or "").strip().lower(), "unknown")
    return BodyDetection(body=body, confidence="LOW", physics=pp.physics or "none",
                         evidence=[f"inferred from the analyzer family label '{pp.family}' of an older preview"],
                         source="legacy preview label")


def apply_routing(prev: "PackagePreview", requested_source: Optional[str] = None,
                  target_body: Optional[str] = None) -> list[str]:
    """0.6.0: decide every project's body route and gate conversion on it. Returns the demoted projects.

    The analyzer's action stays authoritative; the route is an additional gate: a CONVERT / attemptable project whose
    route cannot run (a required stage not implemented, a source request that contradicts the detection, an
    undetermined body) becomes REVIEW and is never selected. With Auto Detect → CBBE 3BBB every project the 0.5.x
    analyzer lets convert is CBBE (or TWB, preserved behaviour), so nothing changes for existing workflows."""
    from .outcomes import POLICY_ATTEMPTABLE, POLICY_BLOCKED, preview_bucket
    from .routes import NOT_IMPLEMENTED, SOURCE_OVERRIDE_CONFLICT, SOURCE_UNVERIFIED, decide_route, resolve_request

    req, dst = resolve_request(requested_source if requested_source is not None else (prev.requested_source or "auto"),
                               target_body if target_body is not None else (prev.target_body or "cbbe_3bbb"))
    prev.requested_source, prev.target_body = req, dst
    demoted: list[str] = []
    counts: dict[str, int] = {}
    for pp in prev.projects:
        dec = decide_route(detection_of(pp), req, dst)
        pp.source_body, pp.requested_source, pp.target_body = dec.source_body, req, dst
        pp.route_id, pp.route_status, pp.route_label = dec.route.route_id, dec.status, dec.route.label
        pp.route_reason = dec.reason
        pp.geometry_stage, pp.physics_stage = dec.route.geometry_stage.to_dict(), dec.route.physics_stage.to_dict()
        if not pp.body_detection:
            pp.body_detection = dec.detection.to_dict()
        counts[dec.source_body] = counts.get(dec.source_body, 0) + 1
        if dec.route.status == NOT_IMPLEMENTED and dec.reason and dec.reason not in pp.reasons:
            pp.reasons.insert(0, dec.reason)  # "Fusion Girl → CBBE → CBBE 3BBB: … not implemented yet"
        if pp.plan:
            rd = dec.to_dict()
            rd["detection"] = {**rd["detection"], "evidence": rd["detection"]["evidence"][:6]}
            pp.plan["route"] = rd
        wants = (pp.action == "CONVERT" and pp.selectable) or pp.review_policy == POLICY_ATTEMPTABLE
        if wants and not dec.may_convert:
            demoted.append(pp.project)
            pp.action = "REVIEW"
            pp.status = dec.status if dec.status in (SOURCE_OVERRIDE_CONFLICT, SOURCE_UNVERIFIED) else "ROUTE_NOT_EXECUTABLE"
            pp.selectable = pp.selected = False
            pp.review_policy, pp.review_policy_reason = POLICY_BLOCKED, dec.reason or dec.status
            pp.reasons.insert(0, dec.reason or f"{dec.route.label}: route {dec.status}")
            pp.plan = None
            pp.bucket = preview_bucket(pp.action, pp.status, pp.reasons, pp.confidence)
    prev.body_counts = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    prev.counts = {a: sum(1 for p in prev.projects if p.action == a) for a in ACTIONS}
    prev.counts["selected"] = len(prev.selected())
    return demoted


def build_package_preview(inst: Installation, kb: KnowledgeBase, package: str, projects: list[ProjectResult],
                          runtime: RuntimeInfo, output_mod: Optional[Path], library: Optional[list[ProjectResult]] = None,
                          progress: Optional[Callable[[int, int, str], None]] = None,
                          allow_body_replacement: bool = False, acceptance=None) -> PackagePreview:
    """Classify and plan every project of ``package``. Reads files, writes nothing.

    ``acceptance`` (:class:`~cbbe3bbb.conversion.plan.AcceptanceIsolation`, 0.5.3 ``acceptance-test-cloth`` only):
    for that one exact project an existing generated AUTO project is not an input (no ALREADY_CONVERTED /
    UNKNOWN_AUTO / name collision); it is verified first and never used by production commands."""
    if acceptance is not None:
        acceptance.verify()
    say = progress or (lambda *_: None)
    conv = inst.profile.conversion
    created = time.strftime("%Y-%m-%dT%H:%M:%S")
    prev = PackagePreview(package=package, created=created, output_mod=str(output_mod or ""),
                          runtime=runtime.to_dict(), session_mode=conv.batch_session_mode,
                          allow_body_replacement=bool(allow_body_replacement))
    prov = next((p for p in inst.providers if p.name == package), None)
    if prov is not None:
        prev.provider_kind, prev.provider_path = prov.kind, str(prov.root)
    ref = kb.reference if kb else None
    if ref is not None:
        prev.reference = {"template": inst.reference_template.display_name if inst.reference_template else ref.display_name,
                          "set": ref.set_name, "shape": ref.shape_name, "source": ref.source}
        prev.regions_available = {r: list(b) for r, b in ref.physics_regions.items()}
        if not any("pelvis" in b.lower() for b in ref.physics_bones):
            prev.notes.append("regions come from the installed reference: " + ", ".join(ref.physics_regions)
                              + ". It has no separate pelvis physics bones; the pelvis/butt area is covered by the butt bones")
    prev.settings = {"search_radius": conv.search_radius, "max_vertex_targets": conv.max_vertex_targets,
                     "no_target_limit": conv.no_target_limit, "weight_mode": conv.weight_mode,
                     "auto_copy_ref": False, "project_suffix": conv.project_suffix, "preset": conv.preset,
                     "build_morphs": conv.build_morphs}
    prev.groups = [MAIN_GROUP, package_group_name(package)]
    manifest = None
    if output_mod is not None and (Path(output_mod) / "manifest.json").is_file():
        try:
            manifest = Manifest.load(Path(output_mod))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            prev.notes.append(f"output mod manifest unreadable: {exc}; existing conversions cannot be recognised")
    hasher = _hash_cache()
    mine = [p for p in projects if p.mod == package]
    overridden = sorted({vf.rel for key, versions in (inst.vfs.files.items() if inst.vfs else [])
                         if key.startswith("slidersets/") for vf in versions[1:] if vf.provider.name == package})
    if overridden:
        prev.notes.append(f"{len(overridden)} project file(s) of this package are overridden by other mods and are not "
                          f"analyzed: {', '.join(overridden[:5])}")
    names_seen: dict[str, int] = {}
    for p in mine:
        names_seen[p.project] = names_seen.get(p.project, 0) + 1
    lib = library if library is not None else projects
    for i, pr in enumerate(sorted(mine, key=lambda p: p.project.lower())):
        say(i, len(mine), f"Planning {i + 1}/{len(mine)}: {pr.project}")
        pp = PackageProject(project=pr.project, osp_rel=pr.osp_rel, set_index=pr.set_index, shapedata_rel=pr.shapedata_rel,
                            output_mesh="\\".join(x for x in (pr.output_path, pr.output_file) if x),
                            family=pr.family.label, physics=pr.family.physics, existing_3bbb=pr.existing_3bbb,
                            analyzer_action=pr.action, analyzer_confidence=pr.geometry_confidence,
                            action=pr.action, status="", confidence=pr.geometry_confidence,
                            reasons=list(pr.reasons), warnings=list(pr.warnings),
                            generated_project=generated_names(pr.project, conv.project_suffix)["display"],
                            body_detection=detect_body(pr.family).to_dict())
        if pp.output_mesh and prov is not None:  # which in-game item shows this mesh (read-only plugin lookup)
            items, _notes = package_items_for_mesh(prov.root, pp.output_mesh)
            if not items and _notes and "ships no plugin" in _notes[0]:  # Phase 3.2: the plugin is in another mod
                items = load_order_items_for_mesh(inst.providers, pp.output_mesh)
            pp.game_items = [it.to_dict() for it in items]
            for it in items:
                if item_name_mismatch(pr.project, it.armor_name):
                    pp.warnings.append(f"GAME_ITEM_NAME_MISMATCH: this project builds {pp.output_mesh}.nif, which the game "
                                       f"shows as '{it.armor_name}' ({it.armor_editor_id}); test THAT item in game")
        plan: Optional[ConversionPlan] = None
        isolated = acceptance is not None and acceptance.applies_to(pr)
        existing = None if isolated else existing_conversion_status(pr, manifest, output_mod, inst, conv.project_suffix,
                                                                    hasher)
        if isolated:
            pp.warnings.append("ACCEPTANCE: planned from the original source project only; existing generated AUTO "
                               "projects are not inputs and are left untouched")
        if existing is not None:
            pp.action, pp.status, reasons, pp.existing_conversion = existing
            pp.reasons = reasons + [f"analyzer view of the source: {pr.action} — {pr.reasons[0]}" if pr.reasons else ""]
            pp.reasons = [r for r in pp.reasons if r]
        elif names_seen[pr.project] > 1:
            pp.action, pp.status = "REVIEW", "DUPLICATE_NAME"
            pp.reasons.insert(0, "several slider sets in this package share this name; generated names would collide")
        elif pr.action == "CONVERT":
            plan = build_plan(inst, kb, pr, runtime, output_mod_path=output_mod, library=lib, created=created,
                              milestone_gate=False, allow_body_replacement=allow_body_replacement,
                              acceptance=acceptance if isolated else None)
            pp.status = "NEW"
            bodies = [s for s in pr.shapes if s.is_body and s.name in pr.shape_names and "3bbb" not in f"{s.name} {s.body_match}".lower()]
            manual = [x for x in plan.same_output_projects if _is_3bbb_project(x, lib)]
            shape_conf = _min_conf([sp.confidence for sp in plan.shape_plans])
            pp.confidence = shape_conf or pp.confidence
            if plan.blockers:
                pp.action, pp.status = "REVIEW", "BLOCKED"
                pp.reasons = [f"conversion is blocked: {b}" for b in plan.blockers] + pp.reasons
            elif bodies and plan.conversion_mode == "embedded_body_replacement" and not manual:
                br = plan.body_replacement
                pp.action, pp.status = "CONVERT", "EMBEDDED_BODY_REPLACEMENT"
                pp.selectable = True
                pp.selected = False  # MODE B is always selected explicitly, one project at a time
                pp.confidence = _min_conf([shape_conf, br.get("source_body_confidence", "")])
                pp.reasons = [
                    f"the project intentionally embeds a compatible CBBE body ('{br['source_body_shape']}'); it can be "
                    f"replaced with the configured CBBE-3BBB reference ('{br['target_body_shape']}' from "
                    f"{br['target_body_template']}); physics-relevant outfit shapes are independently {shape_conf or 'unknown'} confidence",
                    "embedded-body replacement is opt-in and never selected by default: select this project explicitly",
                ] + list(br.get("source_body_reasons", [])) + pp.reasons
            elif bodies:
                ids = plan.body_identities
                klass = [d.get("classification") for d in ids]
                pp.action, pp.status = "REVIEW", "EMBEDDED_BODY"
                if allow_body_replacement and manual:
                    pp.status = "MANUAL_3BBB_VARIANT"
                elif allow_body_replacement and len(ids) > 1:
                    pp.status = "MULTIPLE_BODIES"
                elif allow_body_replacement and klass and klass[0] == "BODY_AMBIGUOUS":
                    pp.status = "EMBEDDED_BODY_AMBIGUOUS"
                elif allow_body_replacement and klass and klass[0] == "BODY_UNSUPPORTED":
                    pp.status = "EMBEDDED_BODY_UNSUPPORTED"
                pp.reasons = [
                    f"the project builds an embedded non-3BBB body into the outfit mesh ({', '.join(s.name for s in bodies)}): "
                    "converting only the clothes would leave that body without physics under physics-weighted clothes, and "
                    "embedding the CBBE-3BBB body is not done automatically",
                    "decide manually (e.g. a manual conversion that replaces the embedded body)"
                    if not allow_body_replacement else
                    "embedded-body replacement is enabled, but this body is not a safe replacement candidate",
                ] + [f"body '{d.get('shape')}': {d.get('classification')} — " + "; ".join(d.get("reasons", [])[:2]) for d in ids] + pp.reasons
            elif manual:
                pp.action, pp.status = "REVIEW", "MANUAL_3BBB_VARIANT"
                pp.reasons = [f"a 3BBB project already builds the same game mesh: {', '.join(manual)} "
                              "(manual conversion); a third project for the same mesh is not created automatically"] + pp.reasons
            else:
                pp.selectable = True
                pp.selected = shape_conf == "HIGH" and pr.geometry_confidence == Level.HIGH.value
                pp.attempt_origin = "SAFE" if pp.selected else "MEDIUM"
                if not pp.selected:
                    pp.reasons.insert(0, f"confidence {pp.confidence or 'unknown'}: not selected by default")
            pp.warnings = list(dict.fromkeys(pp.warnings + plan.warnings))
            pp.regions = list(plan.regions)
            pp.plan = plan.to_dict()
            pp.expected_checks = expected_checks(plan)
        else:
            pp.status = {"KEEP": "ALREADY_3BBB", "SKIP": "NO_PHYSICS_REGION" if not pr.superseded_by else "SUPERSEDED",
                         "UNSUPPORTED": "UNSUPPORTED_BODY", "ERROR": "ERROR", "REVIEW": "ANALYZER_REVIEW"}.get(pr.action, pr.action)
            if pr.action == "REVIEW":  # Phase 3.2: can Convert All Possible attempt it?
                ok, why = analyzer_review_attemptable(pr.action, pr.reasons)
                if ok:
                    plan = build_plan(inst, kb, pr, runtime, output_mod_path=output_mod, library=lib, created=created,
                                      milestone_gate=False, allow_body_replacement=False, attempt_review=True,
                                      acceptance=acceptance if isolated else None)
                    bodies = [s for s in pr.shapes if s.is_body and s.name in pr.shape_names
                              and "3bbb" not in f"{s.name} {s.body_match}".lower()]
                    manual = [x for x in plan.same_output_projects if _is_3bbb_project(x, lib)]
                    if plan.blockers or bodies or manual or not plan.shape_plans:
                        why = (plan.blockers[0] if plan.blockers else "the project embeds a body" if bodies
                               else "a 3BBB project already builds the same mesh" if manual else "nothing to convert")
                        plan = None
                    else:
                        pp.review_policy, pp.attempt_origin = POLICY_ATTEMPTABLE, "REVIEW"
                        pp.confidence = _min_conf([sp.confidence for sp in plan.shape_plans]) or pp.confidence
                        pp.warnings = list(dict.fromkeys(pp.warnings + plan.warnings))
                        pp.plan = plan.to_dict()
                        pp.expected_checks = expected_checks(plan)
                if not pp.review_policy:
                    pp.review_policy, pp.review_policy_reason = POLICY_BLOCKED, why
        if pp.action == "REVIEW" and not pp.review_policy:
            pp.review_policy = POLICY_BLOCKED
            pp.review_policy_reason = pp.reasons[0] if pp.reasons else pp.status
        pp.bucket = preview_bucket(pp.action, pp.status, pp.reasons + [pp.review_policy_reason], pp.confidence,
                                   selected_safe=pp.action == "CONVERT" and pp.attempt_origin == "SAFE")
        pp.shapes = _shape_decisions(pr, plan)
        if not pp.regions:
            pp.regions = sorted({r for s in pr.shapes if not s.is_body for r, m in s.regions.items()
                                 if Level(m.relevance).rank >= Level.MEDIUM.rank})
        prev.projects.append(pp)
    apply_routing(prev)  # 0.6.0: body routes (Auto Detect → CBBE 3BBB reproduces the 0.5.x classification)
    prev.counts = {a: sum(1 for p in prev.projects if p.action == a) for a in ACTIONS}
    prev.counts["selected"] = len(prev.selected())
    prev.run_mode = next((p.plan.get("os_run_mode", "") for p in prev.projects if p.plan), "")
    if runtime.recommended_run_mode == "interactive":
        prev.notes.append(
            f"Outfit Studio {runtime.version}: headless CopyBoneWeights is a no-op, so the batch runs interactively. "
            + ("One Outfit Studio window runs one prepared script that converts its projects in turn (one Execute). "
               "Projects with cloth physics data (BSClothExtraData) go last in a session, one per session, because "
               "Outfit Studio keeps cloth data across projects and asks at every save which to write."
               if conv.batch_session_mode == "single" else "One Outfit Studio window per project (one Execute each)."))
    if any(p.plan and p.plan.get("source_cloth_data") for p in prev.projects if p.selectable):
        prev.notes.append("some candidates contain cloth physics data (BSClothExtraData): each one keeps exactly its own "
                          "cloth data (answered automatically only when provable, and verified after saving)")
    prev.output_mod_state = output_mod_state(inst, output_mod)
    if prev.output_mod_state == "DISABLED_IN_MO2":
        prev.codes.append("OUTPUT_MOD_DISABLED_IN_MO2")
        prev.notes.append(f"OUTPUT_MOD_DISABLED_IN_MO2: '{Path(output_mod).name}' is disabled in the MO2 profile "
                          f"'{inst.mo2_profile}'. BodySlide shows its group '{package_group_name(package)}' empty until "
                          "the mod is enabled (the converter does not change the MO2 profile).")
    elif prev.output_mod_state == "NOT_IN_PROFILE":
        prev.codes.append("OUTPUT_MOD_NOT_IN_PROFILE")
        prev.notes.append(f"OUTPUT_MOD_NOT_IN_PROFILE: '{Path(output_mod).name}' is not in the MO2 profile "
                          f"'{inst.mo2_profile}' yet: after the first commit, refresh MO2 and enable it before building.")
    try:
        from .batch import dry_run_sessions

        dry_run_sessions(prev, conv.batch_session_mode, conv.max_projects_per_session)
    except Exception as exc:  # noqa: BLE001 - the preview must still show every decision
        prev.notes.append(f"batch script could not be prepared: {exc}")
    say(len(mine), len(mine), "Batch preview ready")
    return prev


def output_mod_state(inst: Installation, output_mod: Optional[Path]) -> str:
    """Is the output mod enabled in the active MO2 profile? (read-only; BodySlide only sees enabled mods)."""
    if output_mod is None:
        return "UNKNOWN"
    mods_dir = inst.mods_dir
    if inst.mo2 is None or mods_dir is None:
        return "UNKNOWN"
    try:
        inside = Path(output_mod).resolve().parent == Path(mods_dir).resolve()
    except OSError:
        inside = False
    if not inside:
        return "UNKNOWN"
    name = Path(output_mod).name
    entry = next((m for m in inst.mods if m.name.lower() == name.lower()), None)
    if entry is None:
        return "NOT_IN_PROFILE" if Path(output_mod).is_dir() else "NOT_CREATED"
    return "ENABLED" if entry.enabled else "DISABLED_IN_MO2"


def _is_3bbb_project(label: str, lib: list[ProjectResult]) -> bool:
    for p in lib:
        if f"{p.project} [{p.mod}]" == label and p.family.physics == "3bbb":
            return True
    return False


# ---------------------------------------------------------------------------------------------
def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")[:60] or "package"


def preview_text(prev: PackagePreview, detail: bool = True) -> str:
    L = prev.summary_lines()
    L.insert(1, f"Source mode: {prev.source_mode}")
    if prev.direct_nif:
        d = prev.direct_nif
        c = d.get("counts", {})
        L.insert(2, f"Direct NIF (read-only analysis): {d.get('unique_female_meshes', 0)} unique female game meshes of "
                    f"{d.get('female_items', 0)} female items; male-only meshes skipped {d.get('male_meshes_skipped', 0)}; "
                    + "  ".join(f"{a} {c.get(a, 0)}" for a in ("CONVERT", "KEEP", "SKIP", "REVIEW", "UNSUPPORTED", "ERROR")))
    L.append(f"Output mod: {prev.output_mod or '—'}")
    L += route_summary_lines(prev)
    L.append(f"Outfit Studio: {prev.runtime.get('version') or '—'} run mode {prev.run_mode or '—'}, sessions {prev.session_mode}")
    L.append(f"BodySlide groups: {', '.join(prev.groups)}")
    L += [f"note: {n}" for n in prev.notes]
    for sess in prev.dry_run:
        L.append(f"Outfit Studio session {sess['session']}: script {sess['script']}: {', '.join(sess['projects'])}")
        L.append(f"  {sess['instructions']}")
    L.append("")
    L.append(f"{'':2}{'Project':<44}{'Family':<12}{'Shapes':>6}  {'Regions':<20}{'3BBB':<6}{'Conf':<8}{'Action':<12}{'Status':<20}Reason")
    for p in prev.projects:
        mark = "[x]" if p.selected else ("[ ]" if p.selectable else "   ")
        reason = p.reasons[0] if p.reasons else ""
        L.append(f"{mark}{p.project[:43]:<44}{p.family[:11]:<12}{len(p.shapes):>6}  {','.join(r.lower() for r in p.regions)[:19]:<20}"
                 f"{p.existing_3bbb:<6}{p.confidence:<8}{p.action:<12}{p.status:<20}{reason}")
    if detail:
        for p in prev.projects:
            L += ["", "-" * 100, *project_detail_lines(p)]
    return "\n".join(L) + "\n"


def route_summary_lines(prev: PackagePreview) -> list[str]:
    """0.6.0: Source/Target body and the route of every detected body (one line each)."""
    from .bodies import body_label
    from .routes import route_by_id

    L = [f"Source body: {body_label(prev.requested_source or 'auto')}   Target body: {body_label(prev.target_body or 'cbbe_3bbb')}"]
    per: dict[str, int] = {}
    for p in prev.projects:
        if p.route_id:
            per[p.route_id] = per.get(p.route_id, 0) + 1
    for rid, n in sorted(per.items(), key=lambda kv: -kv[1]):
        r = route_by_id(rid)
        if r is not None:
            L.append(f"  route {r.label}: {n} project(s), {r.status}; geometry {r.geometry_stage.status}, "
                     f"physics {r.physics_stage.status}")
    return L


def project_detail_lines(p: PackageProject) -> list[str]:
    L = [f"{p.project}   →  {p.action} / {p.status}   confidence {p.confidence or '—'}"
         + ("   [selected]" if p.selected else ("   [selectable]" if p.selectable else ""))]
    L.append(f"  source: {p.osp_rel} (set #{p.set_index}); ShapeData {p.shapedata_rel or '—'}")
    L.append(f"  output mesh: {p.output_mesh or '—'}   family: {p.family} (physics {p.physics}, existing 3BBB {p.existing_3bbb})")
    L.append(f"  analyzer: {p.analyzer_action} ({p.analyzer_confidence})")
    if p.route_id:  # 0.6.0 body route
        det = p.body_detection or {}
        L.append(f"  body: detected {det.get('label') or p.source_body} ({det.get('confidence', '—')}); requested source "
                 f"{p.requested_source or 'auto'}; target {p.target_body}")
        L.append(f"  route: {p.route_label} [{p.route_id}] {p.route_status}; geometry stage "
                 f"{(p.geometry_stage or {}).get('status', '—')}; physics stage {(p.physics_stage or {}).get('status', '—')}"
                 + (f" — {p.route_reason}" if p.route_reason else ""))
        for x in (det.get("conflicting_evidence") or [])[:3]:
            L.append(f"  conflicting body evidence: {x}")
    if p.game_items:
        L.append("  in-game item(s) using this mesh: " + "; ".join(
            f"'{i.get('armor_name') or i.get('armor_editor_id')}' ({i.get('armor_editor_id')}, {i.get('role')}, {i.get('plugin')})"
            for i in p.game_items))
    L += [f"  - {r}" for r in p.reasons]
    for w in p.warnings[:8]:
        L.append(f"  ! {w}")
    if p.existing_conversion:
        L.append(f"  existing conversion: {p.existing_conversion}")
    L.append("  OUTFIT PHYSICS TRANSFER (per shape):" if p.plan else "  shapes:")
    for s in p.shapes:
        head = f"    {s.name} ({s.vertices} v, {s.role}): {s.decision}"
        if s.decision == "CONVERT":
            head += f" {'+'.join(s.regions)}  confidence {s.confidence}  copy {s.copy_mode}"
        L.append(head)
        if s.allowed_bones:
            L.append(f"      allowed physics bones: {', '.join(s.allowed_bones)}")
        if s.decision == "CONVERT":
            L.append(f"      predicted by a full copy: {', '.join(s.predicted_bones) or 'none'}")
        L += [f"      · {r}" for r in s.reasons]
    for ident in (p.plan or {}).get("body_identities", []):
        br = (p.plan or {}).get("body_replacement", {})
        replaced = br.get("source_body_shape") == ident.get("shape")
        L.append("  BODY REPLACEMENT:" if replaced else "  EMBEDDED BODY (not replaced):")
        L.append(f"    source body: {ident.get('shape')} ({ident.get('vertices')} v)   classification: {ident.get('classification')}"
                 f"   confidence: {ident.get('confidence')}")
        L += [f"      evidence: {e}" for e in ident.get("evidence", [])]
        L += [f"      · {r}" for r in ident.get("reasons", [])]
        if replaced:
            L.append(f"    target: {br.get('target_body_shape')} from {br.get('target_body_template')} "
                     f"({br.get('target_body_project')}, set {br.get('target_body_set')}; {br.get('target_body_source')} "
                     f"sha256 {str(br.get('target_body_sha256'))[:16]}…, {br.get('target_body_vertices')} v)")
            L.append(f"    method: {br.get('replacement_method')} — {br.get('method_details')}")
            L.append(f"    sliders: {br.get('slider_mapping')}")
            L.append(f"    zaps: {br.get('zap_mapping')}")
            L.append("    warning: the generated output intentionally contains the CBBE-3BBB body")
    if p.plan:
        pl = p.plan
        L.append(f"  conversion mode: {pl.get('conversion_mode', 'outfit_only')}")
        L.append(f"  generated project: {pl.get('output_project')}  →  {pl.get('output_osp_rel')}")
        L.append(f"  generated ShapeData: ShapeData/{pl.get('output_shapedata_folder')}/{pl.get('output_shapedata_file')}")
        L.append(f"  builds (preserved): {pl.get('output_game_path')}\\{pl.get('output_game_file')}  GenWeights={pl.get('source_gen_weights')}")
        if pl.get("source_cloth_data"):
            L.append(f"  cloth physics data (BSClothExtraData): {pl['source_cloth_data']} block(s), kept as in the original "
                     "(only this project's own verified cloth data is kept; checked after saving)")
        L.append(f"  settings: radius {pl.get('search_radius')}, max targets {pl.get('max_vertex_targets')}, "
                 f"no target limit {pl.get('no_target_limit')}, weight mode {pl.get('weight_mode')}, AutoCopyRef false")
        L.append("  automation steps:")
        L += [f"    {i}. {st}" for i, st in enumerate(pl.get("script_summary", []), 1)]
    return L


def _html(prev: PackagePreview) -> str:
    e = html.escape
    rows = []
    for i, p in enumerate(prev.projects):
        sel = "✔" if p.selected else ("○" if p.selectable else "")
        rows.append(
            f"<tr class='a-{e(p.action)}' onclick=\"d({i})\"><td>{sel}</td><td>{e(p.project)}</td><td>{e(p.family)}{(' / ' + e(p.route_label)) if p.route_label else ''}</td>"
            f"<td>{len(p.shapes)}</td><td>{e(', '.join(r.lower() for r in p.regions))}</td><td>{e(p.existing_3bbb)}</td>"
            f"<td>{e(p.confidence)}</td><td><b>{e(p.action)}</b></td><td>{e(p.status)}</td><td>{e(p.reasons[0] if p.reasons else '')}</td></tr>")
    details = [e("\n".join(project_detail_lines(p))) for p in prev.projects]
    counts = " · ".join(f"{a} {prev.counts.get(a, 0)}" for a in ACTIONS)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Batch Preview</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#ddd;--conv:#e6f4ea;--rev:#fff4e0;--keep:#e8f0fe;--err:#fde8e8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1b1b1d;--fg:#eee;--mut:#aaa;--line:#333;--conv:#1f3a28;--rev:#3d3120;--keep:#1f2d45;--err:#442224}}}}
body{{background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif;margin:16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}}
tr{{cursor:pointer}}.a-CONVERT{{background:var(--conv)}}.a-REVIEW{{background:var(--rev)}}.a-KEEP{{background:var(--keep)}}.a-ERROR{{background:var(--err)}}
pre{{white-space:pre-wrap;border:1px solid var(--line);padding:8px}}.mut{{color:var(--mut)}}
.wrap{{overflow-x:auto}}
</style></head><body>
<h1>Batch Preview: {e(prev.package)}</h1>
<p>{e(counts)} · selected {prev.counts.get('selected', 0)}</p>
<p>{'<br>'.join(e(x) for x in route_summary_lines(prev))}</p>
<p class="mut">Created {e(prev.created)} by {e(PRODUCT_NAME)} {e(prev.tool_version)}. Output mod {e(prev.output_mod)}.
Outfit Studio {e(str(prev.runtime.get('version')))} ({e(prev.run_mode)}). Groups: {e(', '.join(prev.groups))}.
Nothing was converted or written to the output mod.</p>
<ul>{''.join(f'<li>{e(n)}</li>' for n in prev.notes)}</ul>
<div class="wrap"><table><tr><th>Sel</th><th>Project</th><th>Body family</th><th>Shapes</th><th>Regions</th><th>3BBB</th><th>Confidence</th><th>Action</th><th>Status</th><th>Reason</th></tr>
{''.join(rows)}</table></div>
<h2>Details</h2><pre id="det">Click a row.</pre>
<script>const D={json.dumps(details)};function d(i){{const t=document.createElement('textarea');t.innerHTML=D[i];document.getElementById('det').textContent=t.value;}}</script>
</body></html>"""


def write_preview_reports(prev: PackagePreview, folder: Path, dry_run_scripts: Optional[dict[str, str]] = None) -> dict[str, Path]:
    """Write the preview (JSON, text, HTML) into the tool's own reports folder."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = Path(folder) / f"{stamp}_{_safe(prev.package)}"
    base.parent.mkdir(parents=True, exist_ok=True)
    out = {"json": base.with_suffix(".json"), "txt": base.with_suffix(".txt"), "html": base.with_suffix(".html")}
    out["json"].write_text(json.dumps(prev.to_dict(), indent=1), encoding="utf-8")
    out["txt"].write_text(preview_text(prev), encoding="utf-8")
    out["html"].write_text(_html(prev), encoding="utf-8")
    if dry_run_scripts:
        d = Path(str(base) + "_dryrun")
        d.mkdir(parents=True, exist_ok=True)
        for name, xml in dry_run_scripts.items():
            (d / f"{name}.xml").write_text(xml, encoding="utf-8")
        out["scripts"] = d
    return out


def preview_fingerprint(prev: PackagePreview) -> str:
    return hashlib.sha1(json.dumps([(p.project, p.action, p.plan and p.plan.get("script_xml")) for p in prev.projects]).encode()).hexdigest()
