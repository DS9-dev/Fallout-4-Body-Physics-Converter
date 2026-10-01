"""Post-conversion validation of an Outfit Studio-generated project.

Every check is independent and reported. A conversion counts as successful
only when all *required* checks pass. The generated files are compared with
the staged source copies and with the reference prediction. The Phase 1
analyzer must classify the result as a proper 3BBB ``KEEP``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from ..analysis.reference import KnowledgeBase
from ..analysis.shape_metrics import MetricSettings, analyze_shape
from ..bodyslide.osd import OsdError, read_osd
from ..bodyslide.osp import OspParseError, parse_osp
from ..classification.rules import quality_issues
from ..nif.reader import NifError, read_nif
from .plan import ConversionPlan, data_folders_for, shape_allowed_bones, shape_copy_mode, shape_radius

__all__ = ["Check", "ValidationReport", "validate_generated", "emulate_full_copy", "norm_game_path"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True


@dataclass
class ValidationReport:
    checks: list[Check] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if c.required)

    def add(self, name: str, ok: bool, detail: str = "", required: bool = True) -> Check:
        c = Check(name, bool(ok), detail, required)
        self.checks.append(c)
        return c

    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.required and not c.ok]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": [asdict(c) for c in self.checks], "metrics": self.metrics}

    def lines(self) -> list[str]:
        return [f"[{'PASS' if c.ok else ('FAIL' if c.required else 'WARN')}] {c.name}: {c.detail}" for c in self.checks]


def norm_game_path(p: str) -> str:
    return p.replace("/", "\\").strip("\\").lower()


def untouched_weight_change(gen_bones: list[str], gen_w: np.ndarray, src_bones: list[str], src_w: np.ndarray):
    """Semantic weight change of a shape that was not a copy target.

    Outfit Studio writes every shape's skin weights anew on save: nifly's ``NifFile::SetShapeVertWeights`` divides
    each vertex's weights by their sum and keeps the 4 largest (stored as half floats). A source vertex whose
    weights summed to 0.9916 therefore comes back scaled to 1.0 — the same skinning, because the game blends with
    the relative weights. So the source is compared *after* that per-vertex normalization; the bones, their order
    and which bones influence each vertex must still match, and any real change still fails (≤ 1e-3).
    Returns (semantic max change, raw max change, detail text).
    """
    allb = sorted(set(gen_bones) | set(src_bones))
    G = np.stack([_col(gen_bones, gen_w, x) for x in allb], 1) if allb else np.zeros((len(gen_w), 0))
    S = np.stack([_col(src_bones, src_w, x) for x in allb], 1) if allb else np.zeros((len(src_w), 0))
    if G.shape != S.shape:
        return float("inf"), float("inf"), f"vertex count changed: {G.shape[0]} (source {S.shape[0]})"
    raw = float(np.abs(G - S).max()) if G.size else 0.0
    ssum = S.sum(1, keepdims=True)
    Sn = np.where(ssum > 1e-6, S / np.maximum(ssum, 1e-9), S)
    sem = float(np.abs(G - Sn).max()) if G.size else 0.0
    support = bool(((G > 0.001) == (Sn > 0.001)).all())
    # bones that carry weight (a bone listed with no weight, e.g. dropped by Outfit Studio's CleanupBones, is harmless)
    g_used = {allb[j] for j in np.nonzero((G > 0.001).any(0))[0]} if G.size else set()
    s_used = {allb[j] for j in np.nonzero((S > 0.001).any(0))[0]} if S.size else set()
    same_bones = g_used == s_used
    if not (support and same_bones):
        sem = max(sem, raw, 1.0 if not same_bones else 0.0)
    renorm = int((np.abs(ssum[:, 0] - 1) > 0.001).sum())
    detail = (f"max weight change {sem:.4f} after Outfit Studio's per-vertex normalization "
              f"(raw {raw:.4f}; {renorm} source vertices did not sum to 1)"
              + ("" if same_bones else f"; weighted bones changed: {sorted(g_used ^ s_used)}")
              + ("" if support else "; the set of influencing bones changed"))
    return sem, raw, detail


def emulate_full_copy(kb: KnowledgeBase, pts: np.ndarray, src_bones: list[str], src_w: np.ndarray,
                      radius: float, k: int, no_limit: bool) -> tuple[list[str], np.ndarray]:
    """Python emulation of Outfit Studio's Copy Bone Weights for all reference bones.

    Used only to *check* Outfit Studio's result, never to write weights.
    """
    ref = kb.reference
    pred = ref.predict_weights(pts, ref.bones, radius, k, no_limit)
    m = np.where(pred < 0.001, 0.0, np.minimum(pred, 1.0))
    tot = m.sum(1)
    m = m * np.where(tot > 1, 1.0 / np.maximum(tot, 1e-9), 1.0)[:, None]
    tot = m.sum(1)
    extra = [b for b in src_bones if b not in ref.bones]
    e = src_w[:, [src_bones.index(b) for b in extra]] if extra else np.zeros((len(pts), 0))
    tn = e.sum(1)
    nf = np.where(tn > 0.001, (1 - tot) / np.maximum(tn, 1e-9), 0.0)
    return list(ref.bones) + extra, np.concatenate([m, e * nf[:, None]], 1)


def _col(names: list[str], w: np.ndarray, b: str) -> np.ndarray:
    return w[:, names.index(b)] if b in names else np.zeros(w.shape[0], dtype=np.float32)


def validate_generated(plan: ConversionPlan, project_root: Path, staged_root: Path, kb: KnowledgeBase,
                       settings: MetricSettings) -> ValidationReport:
    """``project_root`` = the run's Outfit Studio project folder (contains SliderSets/ShapeData);
    ``staged_root`` = where the staged *source* copies live (the same folder)."""
    rep = ValidationReport()
    root = Path(project_root)
    osp_path = root / plan.output_osp_rel
    nif_path = root / "ShapeData" / plan.output_shapedata_folder / plan.output_shapedata_file

    # ---- project file ----------------------------------------------------------------
    try:
        osp = parse_osp(osp_path)
        sets = osp.sets
    except (OspParseError, OSError) as exc:
        rep.add("valid OSP", False, f"{plan.output_osp_rel}: {exc}")
        return rep
    rep.add("valid OSP", len(sets) == 1 and sets[0].name == plan.output_project,
            f"{len(sets)} set(s): {', '.join(s.name for s in sets)}")
    if not sets:
        return rep
    out = sets[0]
    rep.add("original output path preserved", norm_game_path(out.output_path) == norm_game_path(plan.output_game_path),
            f"{out.output_path!r} (source {plan.output_game_path!r})")
    rep.add("original output filename preserved", out.output_file.lower() == plan.output_game_file.lower(),
            f"{out.output_file!r} (source {plan.output_game_file!r})")
    rep.add("GenWeights preserved", out.gen_weights == plan.source_gen_weights,
            f"{out.gen_weights} (source {plan.source_gen_weights})")
    out_shapes = [e.name for e in out.shapes]
    expected_shapes = list(plan.shapes_to_process) + list(plan.untouched_shapes) + list(plan.replaced_body_shapes)
    rep.add("expected outfit shape(s) listed", sorted(out_shapes) == sorted(expected_shapes),
            f"{out_shapes} (expected {expected_shapes})")
    rep.add("reference shape not listed in project", plan.reference_shape not in out_shapes,
            "project shapes: " + ", ".join(out_shapes))
    out_sl = [s.name for s in out.sliders if not s.zap]
    out_zap = list(out.zap_sliders)
    missing = [s for s in plan.source_slider_names if s not in out_sl]
    added = [s for s in out_sl if s not in plan.source_slider_names]
    rep.add("original sliders preserved", not missing and not added,
            f"{len(out_sl)} sliders (source {len(plan.source_slider_names)})"
            + (f"; missing {missing[:6]}" if missing else "") + (f"; added {added[:6]}" if added else ""))
    rep.add("original zaps preserved", sorted(out_zap) == sorted(plan.source_zap_names),
            f"{len(out_zap)} zaps (source {len(plan.source_zap_names)})")

    # ---- source for comparison ---------------------------------------------------------
    src_osp_path = Path(staged_root) / plan.source_osp_rel
    src_nif_path = Path(staged_root) / plan.source_shapedata_rel
    try:
        src_set = parse_osp(src_osp_path).sets[plan.source_set_index]
        src_nif = read_nif(src_nif_path)
    except (OspParseError, NifError, IndexError, OSError) as exc:
        rep.add("staged source readable", False, str(exc))
        return rep

    # slider data present for the same targets
    def data_targets(ss) -> dict[str, set[str]]:
        return {s.name: {d.target for d in s.data} for s in ss.sliders}
    st, ot = data_targets(src_set), data_targets(out)
    lost = [n for n, tg in st.items() if tg and not tg <= ot.get(n, set())]
    rep.add("slider data kept for outfit targets", not lost, "all source slider data present" if not lost else f"no data for {lost[:6]}")

    # ---- ShapeData NIF -----------------------------------------------------------------
    try:
        nif = read_nif(nif_path)
    except NifError as exc:
        rep.add("valid ShapeData NIF", False, str(exc))
        return rep
    rep.add("valid ShapeData NIF", nif.header.is_fallout4 and bool(nif.shapes),
            f"{nif_path.name}: {len(nif.shapes)} shape(s), Fallout 4 header {nif.header.is_fallout4}")
    names = [s.name for s in nif.shapes]
    ref = kb.reference
    # cloth physics data: exactly the source's blocks (Outfit Studio keeps cloth data of every file loaded in the
    # session and asks which to write; anything else would be another project's or a lost cloth setup)
    src_cloth, src_cloth256 = [], []
    try:
        _sn = read_nif(Path(staged_root) / plan.source_shapedata_rel)
        src_cloth, src_cloth256 = _sn.cloth_data, _sn.cloth_sha256
    except (NifError, OSError):
        pass
    if src_cloth or nif.cloth_data:  # 0.5.3: the payloads themselves, for the report/manifest (size + SHA-256)
        rep.metrics["cloth_extra_data"] = {
            "source_blocks": len(src_cloth), "generated_blocks": len(nif.cloth_data),
            "source_payloads": [{"size": n, "sha256": h} for n, h in src_cloth256],
            "generated_payloads": [{"size": n, "sha256": h} for n, h in nif.cloth_sha256],
            "payload_match": sorted(nif.cloth_data) == sorted(src_cloth)
            and sorted(nif.cloth_sha256) == sorted(src_cloth256)}
    rep.add("cloth physics data (BSClothExtraData) unchanged", sorted(nif.cloth_data) == sorted(src_cloth),
            f"{len(nif.cloth_data)} block(s) in the output, {len(src_cloth)} in the source"
            + ("" if sorted(nif.cloth_data) == sorted(src_cloth) else
               " — the output must carry exactly the source's cloth physics data (same blocks, same payloads)"))
    new_shapes = [n for n in names if n not in expected_shapes]
    embedded = [n for n in names if n == plan.reference_shape and n not in expected_shapes]
    body_like = [s.name for s in nif.shapes if s.name in new_shapes and ref is not None and s.num_vertices == ref.num_vertices]
    rep.add("CBBE-3BBB body NOT embedded", not embedded and not body_like and not new_shapes
            and sorted(names) == sorted(expected_shapes),
            f"NIF shapes: {names} (source shapes {expected_shapes}); file size {nif_path.stat().st_size} bytes"
            + (f"; NEW shapes {new_shapes}" if new_shapes else ""))

    # slider data: every data reference of the generated project must resolve to an existing
    # .osd entry. Outfit Studio keeps "AddProject" slider data where it was (the source's
    # ShapeData folder, as a DataFolder reference) instead of copying it.
    refs: dict[Path, set[str]] = {}
    for sl in out.sliders:
        for d in sl.data:
            f = d.data_file.replace("\\", "/")
            if "/" not in f or not f.split("/", 1)[0].lower().endswith(".osd"):
                continue
            osd_file, entry = f.split("/", 1)
            cands = [root / "ShapeData" / fo / osd_file for fo in data_folders_for(out, d.target, bool(getattr(d, "local", False)))]
            refs.setdefault(next((c for c in cands if c.is_file()), cands[0]), set()).add(entry)
    bad = []
    locations = []
    for fpath, entries in refs.items():
        try:
            o = read_osd(fpath)
        except OsdError as exc:
            bad.append(f"{fpath.parent.name}/{fpath.name}: {exc}")
            continue
        miss = sorted(entries - set(o.entries))
        if miss:
            bad.append(f"{fpath.name} lacks {len(miss)} entries (e.g. {miss[0]})")
        locations.append(f"ShapeData/{fpath.parent.name}/{fpath.name} ({len(entries)} entries)")
    rep.metrics["slider_data_files"] = locations
    rep.add("slider data (.osd) resolvable", not bad,
            ("; ".join(locations) or "no slider data") if not bad else "; ".join(bad))
    external = [loc for loc in locations if not loc.startswith(f"ShapeData/{plan.output_shapedata_folder}/")]
    if external:
        rep.add("slider data location", True,
                "slider morphs are read from the source project's ShapeData (unchanged, not copied): " + ", ".join(external),
                required=False)

    region_ok = True
    details = []
    agreement_notes = []
    for sp in plan.shape_plans:
        shp = nif.shape(sp.name)
        src = src_nif.shape(sp.name)
        if shp is None or src is None:
            rep.add(f"{sp.name}: shape present", False, "missing in generated or source NIF")
            region_ok = False
            continue
        same_topo = shp.num_vertices == src.num_vertices and len(shp.triangles) == len(src.triangles)
        dev = float(np.abs(shp.world_vertices() - src.world_vertices()).max()) if same_topo else float("inf")
        rep.add(f"{sp.name}: geometry unchanged", same_topo and dev < 1e-3,
                f"{shp.num_vertices} verts / {len(shp.triangles)} tris, max vertex deviation {dev:.2e}")
        bones, w = shp.weight_matrix()
        phys = {b: int((w[:, bones.index(b)] > 0.001).sum()) for b in (ref.physics_bones if ref else []) if b in bones}
        phys = {b: n for b, n in phys.items() if n > 0}
        allowed = shape_allowed_bones(sp, plan)
        outside = sorted(b for b in phys if b not in allowed)
        rep.add(f"{sp.name}: no physics bones outside planned regions", not outside,
                f"weighted physics bones: {', '.join(f'{b}({n})' for b, n in sorted(phys.items())) or 'none'}"
                + (f"; unexpected {outside}" if outside else ""))
        for region in sp.regions:
            rb = [b for b in (ref.physics_regions.get(region, []) if ref else [])]
            have = [b for b in rb if phys.get(b, 0) >= 5]
            ok = len(have) >= max(1, len(rb) // 2)
            region_ok &= ok
            details.append(f"{sp.name}/{region.lower()}: {len(have)}/{len(rb)} bones weighted")
            rep.add(f"{sp.name}: {region.lower()} 3BBB bones present", ok, ", ".join(f"{b}({phys.get(b, 0)})" for b in rb))
        # weights sum to 1 where skinned
        sums = w.sum(1)
        off = int((np.abs(sums - 1) > 0.01).sum())
        unweighted = np.nonzero(sums < 0.5)[0]
        rep.add(f"{sp.name}: weights normalized", float(np.abs(sums - 1).max()) < 0.05,
                f"weight sum range {sums.min():.3f}..{sums.max():.3f}; {off} vertices off by >1% "
                "(Fallout 4 stores at most 4 influences per vertex)"
                + (f"; {len(unweighted)} vertices WITHOUT any weight: "
                   + ", ".join(str(int(i)) for i in unweighted[:12]) + (" …" if len(unweighted) > 12 else "")
                   if len(unweighted) else ""))
        # analyzer view of the generated shape
        entry = next((e for e in out.shapes if e.name == sp.name), None)
        sa = analyze_shape(shp, entry, kb, settings, 0, {})
        issues = []
        for r, m in sa.regions.items():
            issues += quality_issues(sa, m)
            if m.agreement is not None:
                agreement_notes.append(f"{r.lower()} {m.agreement:.3f}")
                rep.metrics[f"{sp.name}/{r}/agreement"] = m.agreement
            if m.cross_side_fraction is not None:
                rep.metrics[f"{sp.name}/{r}/cross_side_fraction"] = m.cross_side_fraction
        rep.add(f"{sp.name}: cross-side / quality anomaly checks", not issues, "; ".join(issues) or "no anomalies")
        agreements = [rep.metrics[k] for k in rep.metrics if k.startswith(f"{sp.name}/") and k.endswith("/agreement")]
        rep.add(f"{sp.name}: predicted vs actual physics weights consistent",
                bool(agreements) and min(agreements) >= 0.9,
                "agreement " + (", ".join(agreement_notes) or "n/a") + " (≥ 0.90 required)")
        if shape_copy_mode(sp, plan) == "full" and ref is not None:
            sb, sw = src.weight_matrix()
            en, ew = emulate_full_copy(kb, src.world_vertices(), sb, sw, shape_radius(sp, plan), plan.max_vertex_targets, plan.no_target_limit)
            allb = sorted(set(en) | set(bones))
            errs = np.array([np.abs(_col(en, ew, b) - _col(bones, w, b)) for b in allb])
            mean_err = float(errs.mean()) if errs.size else 0.0
            p99 = float(np.percentile(errs.max(axis=0), 99)) if errs.size else 0.0
            rep.metrics[f"{sp.name}/full_copy_mean_abs_error"] = round(mean_err, 5)
            rep.metrics[f"{sp.name}/full_copy_p99_max_error"] = round(p99, 4)
            rep.add(f"{sp.name}: matches the full-copy prediction", mean_err < 0.005,
                    f"mean |Δw| {mean_err:.4f}, 99th pct per-vertex max |Δw| {p99:.3f}", required=False)
    for name in plan.untouched_shapes:
        a, b = nif.shape(name), src_nif.shape(name)
        if a is None or b is None:
            rep.add(f"{name}: untouched shape present", False, "missing")
            continue
        an, aw = a.weight_matrix()
        bn, bw = b.weight_matrix()
        diff, raw, detail = untouched_weight_change(an, aw, bn, bw)
        rep.add(f"{name}: weights unchanged", diff < 1e-3, detail)
    if plan.conversion_mode == "embedded_body_replacement":
        validate_body_replacement(rep, plan, root, Path(staged_root), out, src_set, nif, src_nif, kb)
    elif plan.replaced_body_shapes:
        rep.add("conversion mode consistent", False, "replaced body shapes listed but the plan is not MODE B")
    rep.metrics["regions"] = details
    return rep


def validate_body_replacement(rep: "ValidationReport", plan: ConversionPlan, root: Path, staged_root: Path, out, src_set,
                              nif, src_nif, kb: KnowledgeBase) -> None:
    """MODE B checks: the embedded body became exactly the planned CBBE-3BBB body (see ``body.py``)."""
    from .body import COINCIDE_TOL, LEGACY_BODY_BONES, WEIGHT_TOL, target_morphs, vertex_map
    from .plan import sha256_file

    br = plan.body_replacement
    name = br.get("source_body_shape", "")
    target = br.get("source_body_target", name)
    rep.add("body replacement: source body positively identified",
            br.get("source_body_classification") == "BODY_CBBE_COMPATIBLE" and br.get("source_body_confidence") == "HIGH",
            f"{name}: {br.get('source_body_classification')} ({br.get('source_body_confidence')})")
    ref_path = staged_root / br.get("target_body_source", "")
    try:
        ref_nif = read_nif(ref_path)
    except NifError as exc:
        rep.add("body replacement: target reference readable", False, str(exc))
        return
    ref = ref_nif.shape(br.get("target_body_shape", ""))
    if ref is None:
        rep.add("body replacement: target reference readable", False, f"shape {br.get('target_body_shape')} missing")
        return
    h = sha256_file(ref_path)
    rep.add("body replacement: target reference unchanged since the plan (SHA-256)", h == br.get("target_body_sha256"),
            f"{br.get('target_body_source')} {h[:16]}… (plan {str(br.get('target_body_sha256'))[:16]}…)")
    # exactly one body-like shape, and it is the planned one
    body_like = []
    for sh in nif.shapes:
        if sh.name == name:
            continue
        if sh.num_vertices >= 1000:
            m, _ = vertex_map(sh.world_vertices(), ref.world_vertices())
            if (m >= 0).mean() > 0.5:
                body_like.append(sh.name)
    b = nif.shape(name)
    rep.add("body replacement: exactly one body in the output", b is not None and not body_like,
            (f"body '{name}' present" if b is not None else f"planned body '{name}' MISSING")
            + (f"; other body-like shapes: {body_like}" if body_like else "; no duplicate or old body"))
    sb = src_nif.shape(name)
    if b is None or sb is None:
        return
    same = b.num_vertices == sb.num_vertices and len(b.triangles) == len(sb.triangles)
    dev = float(np.abs(b.world_vertices() - sb.world_vertices()).max()) if same else float("inf")
    tri_same = same and np.array_equal(np.asarray(b.triangles), np.asarray(sb.triangles))
    rep.add("body replacement: body geometry unchanged (deleted vertices stay deleted)", same and dev < 1e-3 and tri_same,
            f"{b.num_vertices} verts / {len(b.triangles)} tris (source {sb.num_vertices} / {len(sb.triangles)}), "
            f"max vertex deviation {dev:.1e}")
    m, d = vertex_map(b.world_vertices(), ref.world_vertices())
    rep.add("body replacement: every body vertex coincides with the CBBE-3BBB reference", bool((m >= 0).all()),
            f"{int((m >= 0).sum())}/{len(m)} vertices within {COINCIDE_TOL:g} (max {float(d.max()):.1e})")
    if not (m >= 0).all():
        return
    bb, bw = b.weight_matrix()
    rb, rw = ref.weight_matrix()
    rwm = rw[m]
    allb = sorted(set(bb) | set(rb))
    err = max((float(np.abs(_col(bb, bw, x) - _col(rb, rwm, x)).max()) for x in allb), default=0.0)
    rep.metrics[f"{name}/body_weight_max_error"] = round(err, 6)
    rep.add("body replacement: body weights identical to the CBBE-3BBB reference", err <= WEIGHT_TOL,
            f"max |Δw| {err:.5f} over {len(allb)} bones (≤ {WEIGHT_TOL} required; the body was not weight-smeared)")
    legacy = [x for x in LEGACY_BODY_BONES if x in bb and float(_col(bb, bw, x).max()) > 0.001]
    rep.add("body replacement: no legacy physics bones weighted on the body", not legacy,
            ", ".join(legacy) if legacy else "none")
    phys = set(kb.physics_bones) if kb else set()
    want = sorted(x for x in rb if x in phys and float(_col(rb, rwm, x).max()) > 0.001)
    have = sorted(x for x in bb if x in phys and float(_col(bb, bw, x).max()) > 0.001)
    rep.add("body replacement: 3BBB physics bones of the reference present on the body", want == have and bool(want),
            f"{len(have)} bones: {', '.join(have)}" + ("" if want == have else f"; expected {', '.join(want)}"))
    # slider morphs: the generated body data resolves (project-local) and equals the source body's morphs
    def res_in(base: Path):
        def resolve(fo: str, f: str):
            p = base / "ShapeData" / fo / f
            return p if p.is_file() else None
        return resolve
    gen_m = target_morphs(out, target, b.num_vertices, res_in(root))
    src_m = target_morphs(src_set, target, sb.num_vertices, res_in(staged_root))
    missing = sorted(set(src_m) - set(gen_m))
    worst = max((float(np.abs(gen_m[k] - src_m[k]).max()) for k in src_m if k in gen_m), default=0.0)
    rep.add("body replacement: body slider morphs identical to the source and resolvable",
            not missing and worst < 1e-3 and len(gen_m) == len(src_m),
            f"{len(gen_m)}/{len(src_m)} body morphs resolve, max offset change {worst:.1e}"
            + (f"; MISSING {missing[:5]}" if missing else ""))
    zaps_src = sorted(sl.name for sl in src_set.sliders if sl.zap and any(dd.target == target for dd in sl.data))
    zaps_out = sorted(sl.name for sl in out.sliders if sl.zap and any(dd.target == target for dd in sl.data))
    rep.add("body replacement: body zaps preserved", zaps_src == zaps_out,
            (", ".join(zaps_out) or "no zaps on the body") + ("" if zaps_src == zaps_out else f" (source {zaps_src})"))
