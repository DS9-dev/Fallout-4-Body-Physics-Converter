"""Deterministic KEEP / CONVERT / SKIP / REVIEW / UNSUPPORTED / ERROR rules.

The rules are intentionally conservative: CONVERT is only given to projects
whose relevant garment surfaces sit tightly on the 3BBB reference and that
show no complicating factor. Everything uncertain becomes REVIEW with the
reason spelled out. Having 3BBB bones is *not* taken as proof of good
weights: existing 3BBB weighting is checked for asymmetry, cross-body
transfer and weight far outside the reference's physics regions.
See docs/CLASSIFICATION.md.
"""
from __future__ import annotations

import re
from typing import Optional

from ..analysis.models import Action, Level, ProjectResult, RegionMetrics, ShapeAnalysis

__all__ = [
    "classify_project",
    "RELEVANT_REGIONS",
    "CORE_REGIONS",
    "OUTERWEAR_WORDS",
    "LOOSE_WORDS",
    "RIGID_WORDS",
    "ACCESSORY_WORDS",
    "FOOTWEAR_WORDS",
    "name_tokens",
    "quality_issues",
    "garment_risks",
]

RELEVANT_REGIONS = ("BREAST", "BUTT", "THIGH")
# Regions whose absence makes an existing conversion "partial" (thigh physics is optional in 3BBB setups).
CORE_REGIONS = ("BREAST", "BUTT")
# Name hints are matched against *word tokens* (CamelCase and separators split),
# so "Wardrobe" does not match "robe" and "BlackMambaJacket" matches "jacket".
# Outerwear: stiff/layered garments where skin-like physics weighting is a known risk
# (the historical DonEb14n Clothes Pack 5 jacket failed in game despite 3BBB bones).
OUTERWEAR_WORDS = {
    "jacket", "jackets", "coat", "coats", "overcoat", "trenchcoat", "cape", "capes", "cloak", "robe", "robes",
    "poncho", "hoodie", "hoody", "duster", "trench", "kimono", "parka", "cardigan", "blazer", "shawl", "scarf", "vest",
}
LOOSE_WORDS = {"skirt", "miniskirt", "dress", "gown", "apron", "open", "loose", "baggy", "tail", "backpack", "bag"}
FOOTWEAR_WORDS = {"boot", "boots", "heel", "heels", "shoe", "shoes", "sneaker", "sneakers", "sandal", "sandals"}
RIGID_WORDS = {"armor", "armour", "plate", "helmet", "gauntlet", "gauntlets", "pauldron", "pauldrons", "carrier"}
ACCESSORY_WORDS = {
    "zip", "zipper", "zippers", "button", "buttons", "buckle", "buckles", "pocket", "pockets", "belt", "belts",
    "strap", "straps", "strapon", "chain", "chains", "badge", "holster", "holsters", "pouch", "pouches", "ring", "rings",
    "rivet", "rivets", "stud", "studs", "cuff", "cuffs", "harness", "clamp", "clamps", "collar", "shackle", "shackles", "plug", "plugs", "gag", "piercing", "piercings", "lock", "padlock",
    "radio", "electronics", "gun", "knife", "sword",
}
DECLARED_3BBB = re.compile(r"3\s*bbb|\b3b\b|3b[-_ ]|[-_ ]3b\b", re.I)


def name_tokens(text: str) -> set[str]:
    """Lower-case word tokens of a name: splits separators, digits and CamelCase."""
    parts = re.split(r"[^A-Za-z]+", text)
    out: set[str] = set()
    for part in parts:
        for tok in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", part):
            out.add(tok.lower())
        if part:
            out.add(part.lower())
    return out


def _hit(words: set[str], text: str) -> str:
    toks = name_tokens(text)
    found = sorted(toks & words)
    return found[0] if found else ""


MAX_CLEAN_RELEVANT_SHAPES = 2


def _lvl(v: str) -> Level:
    try:
        return Level(v)
    except ValueError:
        return Level.UNKNOWN


def quality_issues(shape: ShapeAnalysis, region: RegionMetrics) -> list[str]:
    """Signals that existing 3BBB weights on a shape are suspicious."""
    out = []
    if not region.existing_vertices:
        return out
    tag = f"{shape.name}/{region.region.lower()}"
    if region.lr_balance is not None and region.predicted_lr_balance is not None:
        if region.predicted_lr_balance >= 0.75 and region.lr_balance < 0.6 * region.predicted_lr_balance:
            out.append(f"{tag}: left/right imbalance (L/R ratio {region.lr_balance:.2f}, reference predicts {region.predicted_lr_balance:.2f})")
    if region.cross_side_fraction is not None and region.cross_side_fraction > 0.05:
        out.append(f"{tag}: {region.cross_side_fraction:.0%} of side-specific weights sit on the opposite side of the body")
    if region.outside_region_fraction is not None and region.outside_region_fraction > 0.25 and region.existing_vertices >= 20:
        out.append(f"{tag}: {region.outside_region_fraction:.0%} of physics-weighted vertices are where the reference has no {region.region.lower()} weight")
    if region.agreement is not None and region.agreement < 0.6 and region.existing_vertices >= 20:
        out.append(f"{tag}: weights differ strongly from a reference transfer (agreement {region.agreement:.2f})")
    if region.weight_islands is not None and region.predicted_weight_islands is not None:
        exp = region.predicted_weight_islands
        if region.weight_islands > max(2 * exp, exp + 4):
            out.append(f"{tag}: physics weights split into {region.weight_islands} disconnected islands (a reference transfer gives {exp})")
    return out


def garment_risks(project: str, outfit: list[ShapeAnalysis], relevant_shape_names: set[str], fit_high: bool) -> tuple[list[str], list[str]]:
    """(review reasons, informational warnings) derived from garment type and structure."""
    review: list[str] = []
    info: list[str] = []
    names = " ".join([project] + [s.name for s in outfit])
    w = _hit(OUTERWEAR_WORDS, names)
    if w:
        review.append(f"outerwear ('{w}'): stiff/layered garments often behave badly with skin-like physics weights")
    w = _hit(RIGID_WORDS, names)
    if w:
        review.append(f"name suggests rigid armor/equipment ('{w}')")
    w = _hit(LOOSE_WORDS, names)
    if w and not fit_high:
        review.append(f"name suggests loose/open clothing ('{w}') and the fit is not tight")
    elif w:
        info.append(f"name suggests loose/open clothing ('{w}'), but the geometry is tight")
    feet = sorted(n for n in relevant_shape_names if _hit(FOOTWEAR_WORDS, n))
    if not feet and len(outfit) == 1 and relevant_shape_names and _hit(FOOTWEAR_WORDS, project):
        feet = [outfit[0].name]
    if feet:
        review.append(f"footwear shapes reaching a physics region ({', '.join(feet[:3])}): check whether thigh/butt physics is wanted")
    acc = sorted(n for n in relevant_shape_names if _hit(ACCESSORY_WORDS, n))
    if not acc and _hit(ACCESSORY_WORDS, project):
        acc = [f"project '{project}' ({_hit(ACCESSORY_WORDS, project)})"]
    if acc:
        review.append(f"rigid accessory shapes over physics regions would move like skin: {', '.join(acc[:5])}")
    if len(relevant_shape_names) > MAX_CLEAN_RELEVANT_SHAPES:
        review.append(f"layered outfit: {len(relevant_shape_names)} shapes overlap physics regions ({', '.join(sorted(relevant_shape_names)[:5])})")
    return review, info


def _relevance(shapes: list[ShapeAnalysis], region: str) -> Level:
    best = Level.NONE
    for s in shapes:
        rm = s.regions.get(region)
        if rm and _lvl(rm.relevance).rank > best.rank:
            best = _lvl(rm.relevance)
    return best


def classify_project(pr: ProjectResult, reference_available: bool) -> ProjectResult:
    """Fill ``action``, ``reasons``, ``geometry_confidence`` and region summaries in place."""
    reasons: list[str] = []
    warns = pr.warnings

    def done(action: Action, conf: Optional[Level] = None) -> ProjectResult:
        pr.action = action.value
        pr.reasons = reasons
        if conf is not None:
            pr.geometry_confidence = conf.value
        return pr

    if pr.errors and not pr.shapes:
        reasons.extend(pr.errors[:3])
        return done(Action.ERROR)

    outfit = [s for s in pr.shapes if not s.is_body]
    bodies = [s for s in pr.shapes if s.is_body]
    pr.includes_body_shape = bool(bodies)
    fam = pr.family

    if reference_available:
        pr.breast = _relevance(outfit, "BREAST").value
        pr.butt = _relevance(outfit, "BUTT").value
        pr.thigh = _relevance(outfit, "THIGH").value
    has_3bbb_outfit = any(s.physics_bones for s in outfit)

    # existing 3BBB summary over relevant shapes
    relevant_pairs = [
        (s, s.regions[r]) for s in outfit for r in RELEVANT_REGIONS if r in s.regions and _lvl(s.regions[r].relevance).rank >= Level.MEDIUM.rank
    ]
    weighted_pairs = [(s, rm) for s, rm in relevant_pairs if rm.existing_vertices > 0]
    core_pairs = [(s, rm) for s, rm in relevant_pairs if rm.region in CORE_REGIONS]
    missing_core = [(s, rm) for s, rm in core_pairs if rm.existing_vertices == 0]
    if fam.physics == "3bbb":
        missing_thigh = [f"{s.name}/thigh" for s, rm in relevant_pairs if rm.region == "THIGH" and rm.existing_vertices == 0]
        if missing_thigh and not missing_core:
            warns.append(f"optional thigh physics bones not weighted on: {', '.join(missing_thigh[:4])}")
        if missing_core:
            pr.existing_3bbb = "PARTIAL"
        else:
            pr.existing_3bbb = "YES"
    else:
        pr.existing_3bbb = "NO" if reference_available or pr.shapes else "UNKNOWN"

    # --- unsupported bodies --------------------------------------------------
    if fam.family == "FUSION_GIRL":
        reasons.append("Fusion Girl body family: not a CBBE project, CBBE 3BBB conversion does not apply")
        reasons.extend(f"evidence: {e}" for e in fam.evidence[:3])
        return done(Action.UNSUPPORTED)
    if fam.family in ("BODYTALK", "MALE"):
        reasons.append(f"{fam.label} body family: male/BodyTalk projects are out of scope for CBBE 3BBB conversion")
        reasons.extend(f"evidence: {e}" for e in fam.evidence[:2])
        return done(Action.UNSUPPORTED)

    if not outfit:
        if bodies:
            reasons.append("project contains only a body/reference shape (no outfit geometry)")
            return done(Action.SKIP)
        reasons.append("no readable shapes in project")
        return done(Action.ERROR)

    # --- already 3BBB -------------------------------------------------------
    if fam.physics == "3bbb":
        issues = [q for s, rm in weighted_pairs for q in quality_issues(s, rm)]
        # also check shapes that carry 3BBB weights although the region is not relevant
        for s in outfit:
            for r in RELEVANT_REGIONS:
                rm = s.regions.get(r)
                if rm and rm.existing_vertices >= 20 and _lvl(rm.relevance).rank < Level.MEDIUM.rank and rm.predicted_vertices < 5:
                    issues.append(f"{s.name}/{r.lower()}: {rm.existing_vertices} vertices carry {r.lower()} physics weight but the geometry is not over that reference region")
        if issues:
            reasons.append("existing 3BBB weights present but look suspicious (bones present does not mean good weights)")
            reasons.extend(issues[:6])
            return done(Action.REVIEW, Level.LOW)
        declared = bool(DECLARED_3BBB.search(" ".join([pr.project, pr.mod, pr.osp_rel, pr.shapedata_rel])))
        if not declared:
            risks, _ = garment_risks(pr.project, outfit, {s.name for s, _ in weighted_pairs}, True)
            if risks:
                reasons.append(
                    "3BBB weights present, but the project is not a declared 3BBB conversion "
                    "(no '3BBB' in project/mod names; possibly an in-place manual edit)"
                )
                reasons.extend(risks[:5])
                reasons.append("known risk: a faithful weight copy can still behave badly on this garment type (see CP5 jacket case)")
                return done(Action.REVIEW, Level.MEDIUM)
            warns.append("3BBB weights present but no project/mod name declares a 3BBB conversion")
        if pr.existing_3bbb == "PARTIAL":
            missing = [f"{s.name}/{rm.region.lower()}" for s, rm in missing_core]
            reasons.append("partially converted: some physics-relevant shapes have no 3BBB weights")
            reasons.append("missing: " + ", ".join(missing[:6]))
            return done(Action.REVIEW, Level.MEDIUM)
        if bodies:
            warns.append(
                f"project includes body shape(s) {', '.join(s.name for s in bodies[:3])}: BodySlide builds them into the "
                "output mesh (intended only if the outfit needs the body, e.g. 'copy reference shape into output')"
            )
        reasons.append(f"already converted ({fam.label}): 3BBB physics bones are weighted")
        if weighted_pairs:
            regs = sorted({rm.region.lower() for _, rm in weighted_pairs})
            reasons.append(f"relevant regions carry 3BBB weights: {', '.join(regs)}")
        elif not has_3bbb_outfit:
            reasons.append("3BBB weighting comes from the embedded body shape")
        return done(Action.KEEP, Level.HIGH)

    if fam.family == "UNKNOWN":
        if reference_available and all(_relevance(outfit, r).rank < Level.MEDIUM.rank for r in RELEVANT_REGIONS):
            reasons.append("no breast/butt/thigh physics region under this item")
            reasons.append("body family could not be determined (no body sliders); irrelevant for physics conversion")
            return done(Action.SKIP, Level.MEDIUM)
        reasons.append("body family could not be determined")
        reasons.extend(f"evidence: {e}" for e in fam.evidence[:3])
        return done(Action.REVIEW, Level.LOW)

    if not reference_available:
        reasons.append("CBBE 3BBB reference not available: geometry analysis was skipped")
        return done(Action.REVIEW, Level.UNKNOWN)

    # --- superseded by another project producing the same output ---------
    if pr.superseded_by:
        reasons.append(f"superseded: 3BBB project '{pr.superseded_by}' (another project file) builds the same output mesh")
        return done(Action.SKIP, Level.HIGH)

    # --- no physics region under the garment ------------------------------
    rel = {r: _relevance(outfit, r) for r in RELEVANT_REGIONS}
    relevant_regions = [r for r, lvl in rel.items() if lvl.rank >= Level.MEDIUM.rank]
    if not relevant_regions:
        # geometry above a physics region but beyond the search radius: a weight copy
        # would silently produce nothing, which deserves a human look.
        far_over = [
            (s, rm) for s in outfit for r in RELEVANT_REGIONS
            if (rm := s.regions.get(r)) is not None and rm.footprint_vertices >= 30
            and rm.footprint_within_radius < 0.5
        ]
        if far_over:
            s, rm = max(far_over, key=lambda x: x[1].footprint_vertices)
            reasons.append(
                f"{s.name} lies over the {rm.region.lower()} region but mostly beyond the search radius "
                f"(median {rm.footprint_median_distance:.1f}, {rm.footprint_within_radius:.0%} within radius): "
                "a weight transfer would add little or no physics weight"
            )
            reasons.append("loose/far garment: decide manually (larger radius, manual weights, or leave without physics)")
            return done(Action.REVIEW, Level.LOW)
        touched = [r.lower() for r, lvl in rel.items() if lvl == Level.LOW]
        reasons.append("no breast/butt/thigh physics region under this outfit")
        if touched:
            reasons.append(f"only marginal contact with: {', '.join(touched)}")
        d = min((s.distance.median for s in outfit), default=0.0)
        reasons.append(f"closest outfit shape median distance to body: {d:.1f}")
        return done(Action.SKIP, Level.HIGH)

    # --- conversion candidate: judge confidence ---------------------------
    review: list[str] = []
    fits: list[Level] = []
    rel_shapes = {s.name for s, _ in relevant_pairs}
    for s, rm in relevant_pairs:
        fit = _lvl(rm.fit)
        fits.append(fit)
        if fit == Level.LOW:
            review.append(
                f"{s.name}: loose/far geometry over the {rm.region.lower()} region "
                f"(median {rm.footprint_median_distance:.1f}, {rm.footprint_within_radius:.0%} within search radius)"
            )
        if rm.predicted_cross_side_fraction is not None and rm.predicted_cross_side_fraction > 0.05:
            review.append(
                f"{s.name}: transfer would put {rm.predicted_cross_side_fraction:.0%} of {rm.region.lower()} side weights "
                "on the opposite side of the body (bridging/cross-body geometry)"
            )
        cov = rm.side_coverage
        if len(cov) == 2 and max(cov.values()) >= 0.3 and min(cov.values()) < 0.5 * max(cov.values()):
            warns.append(f"{s.name}: covers the {rm.region.lower()} region asymmetrically (L {cov.get('L', 0):.0%} / R {cov.get('R', 0):.0%})")
    conf = min(fits, key=lambda x: x.rank) if fits else Level.UNKNOWN
    if conf == Level.UNKNOWN:
        conf = Level.MEDIUM

    custom = sorted({b for s in outfit for b in s.custom_bones})
    if custom:
        review.append(f"custom (non-skeleton) bones present: {', '.join(custom[:5])}{'...' if len(custom) > 5 else ''}")
    if fam.physics == "legacy_cbbe_physics":
        review.append("legacy CBBE Physics cloth-bone weights present; they must be replaced, not merged")
    if pr.slider_count == 0:
        review.append("project has no sliders (not conformed to a body)")
    risks, info = garment_risks(pr.project, outfit, rel_shapes, conf == Level.HIGH)
    review.extend(risks)
    warns.extend(info)
    body_cbbe = [s.name for s in bodies if "3bbb" not in f"{s.name} {s.body_match}".lower()]
    if body_cbbe:
        warns.append(f"embedded non-3BBB body shape ({', '.join(body_cbbe)}) must be swapped for the 3BBB body during conversion")
    far = [s for s in outfit if s.name in rel_shapes and s.distance.p90 > 8.0]
    for s in far:
        review.append(f"{s.name}: 10% of vertices are more than {s.distance.p90:.1f} units from the body")
        conf = Level.LOW if conf.rank > Level.LOW.rank else conf

    if review or conf != Level.HIGH:
        if not review:
            review.append(f"fit around physics regions is only {conf.value.lower()} confidence")
        reasons.append(f"physics-relevant regions: {', '.join(r.lower() for r in relevant_regions)}")
        reasons.extend(review[:8])
        return done(Action.REVIEW, conf)

    # --- CONVERT ---------------------------------------------------------------
    for r in relevant_regions:
        best = max(
            (s.regions[r] for s in outfit if r in s.regions and _lvl(s.regions[r].relevance).rank >= Level.MEDIUM.rank),
            key=lambda m: m.reference_coverage,
        )
        reasons.append(
            f"close-fitting geometry over the {r.lower()} region (median {best.footprint_median_distance:.2f}, "
            f"{best.footprint_within_radius:.0%} within search radius)"
        )
        reasons.append(f"covers {best.reference_coverage:.0%} of the reference's {r.lower()}-weighted surface")
    reasons.append("no existing 3BBB weighting detected")
    return done(Action.CONVERT, Level.HIGH)
