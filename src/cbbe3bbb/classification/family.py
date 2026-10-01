"""Body family detection from several independent signals.

Signals (strongest first):
  1. bones actually carrying weight on outfit shapes (3BBB physics bones,
     legacy CBBE-Physics cloth bones, vanilla breast/butt bones)
  2. embedded body/reference shapes (matched against the installed bodies)
  3. slider vocabulary (overlap with the installed CBBE body's sliders,
     TWB / BodyTalk / Fusion Girl specific slider names)
  4. project / data-folder / mod names (weak supporting evidence only)
"""
from __future__ import annotations

import re
from typing import Iterable

from ..analysis.models import FamilyResult, Level, ShapeAnalysis
from ..analysis.reference import KnowledgeBase

__all__ = ["detect_family", "TWB_SIGNATURE_SLIDERS", "FALLBACK_CBBE_SLIDERS"]

# Slider names that the TWB body adds on top of CBBE's (observed in installed TWB projects).
TWB_SIGNATURE_SLIDERS = {
    "Tone", "Athlete", "FFB Fitness Less", "FFB Fitness More", "FFB Fitness Very", "CBBE to FFB",
    "Body Clamp", "Body Keep", "Muscle Definition", "Atomic Beauty", "Real Atomic Beauty", "Dibella",
    "Tigersan", "BodyBuilder", "Fitness", "Explosion", "BBW", "AB Template", "New Atomic",
    "Body aTightTiger", "Body a Soul Body", "Body Vanilla Coke", "Big Soft Belly", "Boob Concave",
}
# A compact subset of CBBE slider names, used only if no CBBE body is installed.
FALLBACK_CBBE_SLIDERS = {
    "Breasts", "BreastsSmall", "BreastsSmall2", "BreastsNewSH", "BreastsCleavage", "BreastsTogether",
    "BreastTopSlope", "BreastWidth", "BreastHeight", "BreastPerkiness", "BreastGravity2", "BreastCenter",
    "BreastCenterBig", "BreastFantasy", "BreastFlatness2", "DoubleMelon", "Butt", "ButtSmall", "ButtShape2",
    "ButtNew", "BigButt", "ChubbyButt", "Waist", "WideWaistLine", "Hips", "HipBack", "HipUpperWidth",
    "ChestDepth", "ChestWidth", "7B Upper", "7B Lower", "NipplePerkiness", "NippleDistance", "ChubbyArms",
    "ChubbyLegs", "ChubbyWaist", "Belly", "BigBelly", "Arms", "Back", "BackArch", "BigTorso", "ShoulderWidth",
}
FG_NAME = re.compile(r"fusion\s*girl|fusiongirl|(^|[\s_\-\[(])fg([\s_\-\])]|$)", re.I)
BT_NAME = re.compile(r"bodytalk|(^|[\s_\-\[(])bt[1-4]?([\s_\-\])]|$)", re.I)
TWB_NAME = re.compile(r"(^|[\s_\-\[(])twb([\s_\-\])]|$)", re.I)
BBB_NAME = re.compile(r"3\s*bbb|3b\b|3-b\b", re.I)
PHYSICS_NAME = re.compile(r"physics|cbbe\s*phy|\bcbp\b", re.I)
BT_SLIDER = re.compile(r"^BT([A-Z0-9]|\d)")
FG_SLIDER = re.compile(r"^(FG[ _]|Fusion)", re.I)


def _count(names: Iterable[str], vocab: set[str]) -> int:
    return sum(1 for n in names if n in vocab)


def detect_family(
    project_name: str,
    mod_name: str,
    data_folder: str,
    slider_names: list[str],
    shapes: list[ShapeAnalysis],
    kb: KnowledgeBase,
) -> FamilyResult:
    fr = FamilyResult()
    ev = fr.evidence
    scores = {"CBBE": 0.0, "TWB": 0.0, "BODYTALK": 0.0, "FUSION_GIRL": 0.0, "MALE": 0.0}
    sliders = set(slider_names)
    cbbe_vocab = kb.cbbe_sliders or FALLBACK_CBBE_SLIDERS

    # --- sliders -------------------------------------------------------------
    n_cbbe = _count(sliders, cbbe_vocab)
    n_twb = _count(sliders, TWB_SIGNATURE_SLIDERS)
    n_bt = sum(1 for s in sliders if BT_SLIDER.match(s))
    n_fg = sum(1 for s in sliders if FG_SLIDER.match(s))
    if sliders:
        frac = n_cbbe / len(sliders)
        if n_cbbe >= 8 and frac >= 0.4:
            scores["CBBE"] += 3.0
            ev.append(f"{n_cbbe}/{len(sliders)} sliders match the installed CBBE body's sliders")
        elif n_cbbe >= 3:
            scores["CBBE"] += 1.0
            ev.append(f"{n_cbbe} CBBE slider names present")
        if n_twb >= 5:
            scores["TWB"] += 4.0
            ev.append(f"{n_twb} TWB-specific sliders (e.g. {', '.join(sorted(sliders & TWB_SIGNATURE_SLIDERS)[:3])})")
        if n_bt >= 5:
            scores["BODYTALK"] += 4.0
            ev.append(f"{n_bt} BodyTalk 'BT*' sliders")
        if n_fg >= 3:
            scores["FUSION_GIRL"] += 4.0
            ev.append(f"{n_fg} Fusion Girl sliders")
    else:
        ev.append("project has no sliders")

    # --- embedded body shapes -----------------------------------------------------
    body_shapes = [s for s in shapes if s.is_body]
    for s in body_shapes:
        nm = f"{s.name} {s.body_match}".lower()
        if "cbbe" in nm and "3bbb" in nm:
            scores["CBBE"] += 2.0
            ev.append(f"embedded body shape '{s.name}' matches {s.body_match or 'CBBE 3BBB'}")
        elif "cbbe" in nm:
            scores["CBBE"] += 2.0
            ev.append(f"embedded body shape '{s.name}' ({s.body_match})")
        elif "twb" in nm:
            scores["TWB"] += 3.0
            ev.append(f"embedded TWB body shape '{s.name}'")
        elif "male" in nm and "female" not in nm:
            scores["MALE"] += 3.0
            ev.append(f"embedded male body shape '{s.name}'")
        elif FG_NAME.search(nm):
            scores["FUSION_GIRL"] += 3.0
            ev.append(f"embedded Fusion Girl body shape '{s.name}'")
        elif BT_NAME.search(nm):
            scores["BODYTALK"] += 3.0
            ev.append(f"embedded BodyTalk body shape '{s.name}'")

    # --- names (weak) -------------------------------------------------------------
    names = f"{project_name} | {data_folder} | {mod_name}"
    if TWB_NAME.search(names):
        scores["TWB"] += 0.5
        ev.append("name mentions TWB (weak)")
    if FG_NAME.search(names):
        scores["FUSION_GIRL"] += 0.5
        ev.append("name mentions Fusion Girl / FG (weak)")
    if BT_NAME.search(project_name) or BT_NAME.search(data_folder):
        scores["BODYTALK"] += 0.5
        ev.append("project name mentions BodyTalk / BT (weak)")
    if re.search(r"\bcbbe\b", names, re.I):
        scores["CBBE"] += 0.5
        ev.append("name mentions CBBE (weak)")

    # --- bones -------------------------------------------------------------------
    outfit = [s for s in shapes if not s.is_body]
    phys_outfit = {b: c for s in outfit for b, c in s.physics_bones.items() if c >= 5}
    phys_body = {b: c for s in body_shapes for b, c in s.physics_bones.items() if c >= 5}
    legacy_cloth = {b: c for s in outfit for b, c in s.legacy_physics_bones.items() if b.startswith("CLOTH_") and c >= 5}
    legacy_std = {b: c for s in outfit for b, c in s.legacy_physics_bones.items() if not b.startswith("CLOTH_") and c >= 5}
    if phys_outfit or phys_body:
        fr.physics = "3bbb"
        where = "outfit shapes" if phys_outfit else "embedded body"
        ev.append(f"3BBB physics bones weighted on {where}: {', '.join(sorted(phys_outfit or phys_body)[:6])}")
        scores["CBBE"] += 1.0 if scores["TWB"] < 2 else 0.0
    elif legacy_cloth:
        fr.physics = "legacy_cbbe_physics"
        ev.append(f"legacy CBBE Physics cloth bones weighted: {', '.join(sorted(legacy_cloth))}")
        scores["CBBE"] += 1.0
    elif legacy_std:
        fr.physics = "standard"
        ev.append(f"vanilla breast/butt bones weighted: {', '.join(sorted(legacy_std))}")
    else:
        fr.physics = "none"
    if BBB_NAME.search(names) and fr.physics != "3bbb":
        ev.append("name mentions 3BBB but no 3BBB bone weights were found")
    if PHYSICS_NAME.search(names) and fr.physics == "none":
        ev.append("name mentions physics but no physics bone weights were found")

    fr.scores = {k: round(v, 2) for k, v in scores.items()}
    best = max(scores, key=lambda k: scores[k])
    top = scores[best]
    second = sorted(scores.values())[-2]
    # TWB is a CBBE-derived body: TWB signature wins over CBBE overlap.
    if scores["TWB"] >= 3.0:
        best, top = "TWB", scores["TWB"]
    if top < 1.0:
        fr.family = "UNKNOWN"
        fr.confidence = Level.LOW.value
    else:
        fr.family = best
        margin = top - (second if best != "TWB" else max(v for k, v in scores.items() if k not in ("TWB", "CBBE")))
        fr.confidence = (Level.HIGH if top >= 3.0 and margin >= 2.0 else Level.MEDIUM if top >= 2.0 else Level.LOW).value

    base = {"CBBE": "CBBE", "TWB": "TWB", "BODYTALK": "BodyTalk", "FUSION_GIRL": "Fusion Girl", "MALE": "Male", "UNKNOWN": "unknown"}[fr.family]
    if fr.physics == "3bbb" and fr.family in ("CBBE", "TWB", "UNKNOWN"):
        fr.label = f"{base} 3BBB" if fr.family != "UNKNOWN" else "3BBB (body unknown)"
    elif fr.physics == "legacy_cbbe_physics" and fr.family == "CBBE":
        fr.label = "CBBE Physics (legacy)"
    else:
        fr.label = base
    return fr
