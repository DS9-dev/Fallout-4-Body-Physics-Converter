"""Phase 3.2 (Part R): translate internal diagnostics into short messages a normal mod user understands.

The engine keeps its exact reasons (validator check names, distances, hashes …); they stay available as "technical
details". This module only chooses what the user reads first. The first matching rule wins, so specific rules come
before general ones. Every problem message is followed by ``LEFT_UNCHANGED`` — the original outfit is never touched.
"""
from __future__ import annotations

import re
from typing import Iterable

__all__ = ["UNATTENDED_NOTE", "CONTROL_FAILURE_TEXT", "BUILD_STALE_TEXT", "humanize", "package_message", "system_message", "LEFT_UNCHANGED", "OUTCOME_LABELS", "RESULT_LABELS",
           "MESSAGES", "SHORT", "short_reason", "RUNTIME_STATES", "failed_check"]

LEFT_UNCHANGED = "Original outfit was left unchanged."

# 0.5.3: the unattended Outfit Studio control
UNATTENDED_NOTE = ("Outfit Studio may open during conversion. Do not interact with it unless the converter explicitly "
                   "asks you to continue manually.")
CONTROL_FAILURE_TEXT = ("Automatic Outfit Studio control could not continue.\nYour conversion progress is saved.\n"
                        "You can retry automatically or continue this session manually.")
BUILD_STALE_TEXT = ("{n} converted outfits have not been built from the generated 3BBB projects yet, or the original "
                    "BodySlide projects were built afterward.")

OUTCOME_LABELS = {
    "CONVERTED": "Converted successfully",
    "ALREADY_COMPATIBLE": "Already compatible",
    "NO_CONVERSION_NEEDED": "No conversion needed",
    "COULD_NOT_CONVERT_SAFELY": "Could not convert safely",
    "FAILED": "Failed",
    "SKIPPED": "Not tried in Safe mode",
    "UNFINISHED": "Not finished",
}
RESULT_LABELS = {
    "COMPLETE": "Conversion complete",
    "COMPLETE_WITH_WARNINGS": "Conversion complete (some outfits could not be converted)",
    "INTERRUPTED": "Conversion interrupted — Resume continues where it stopped",
    "SYSTEM_ERROR": "Conversion stopped — a problem affects the whole package",
}

# code -> message shown to the user
MESSAGES = {
    "PHYSICS_NOT_VERIFIED": "Could not verify the converted physics.",
    "GEOMETRY_NOT_VERIFIED": "The converted outfit geometry could not be verified.",
    "WEIGHT_TRANSFER_UNSAFE": "Automatic physics weights could not be safely transferred to part of this outfit.",
    "WEIGHTS_LOOK_WRONG": "The automatic physics weights looked wrong (left and right sides mixed).",
    "WEIGHTS_INCOMPLETE": "Automatic physics weights could not be stored completely for part of this outfit "
                          "(too many bones per vertex).",
    "PHYSICS_MISSING": "Part of the converted outfit did not receive all the expected physics bones; the result was "
                       "rejected by the strict validation.",
    "UNEXPECTED_SHAPE": "The converted project contained an outfit part that was not part of the conversion plan; the "
                        "result was rejected.",
    "SHAPE_CHANGED": "The outfit's shape changed during conversion.",
    "BODY_IN_OUTPUT": "The converted outfit contained the body mesh.",
    "NOT_CONVERTED_BY_OS": "Outfit Studio did not convert this outfit.",
    "OS_NOT_AVAILABLE": "Outfit Studio could not be used. Check the Outfit Studio folder in Settings.",
    "OS_STOPPED": "Outfit Studio stopped unexpectedly while converting this outfit.",
    "SOURCE_CHANGED": "The original outfit files changed during the conversion.",
    "EXISTING_3BBB": "This outfit already has 3BBB-style physics weights; they were left as they are.",
    "EMBEDDED_BODY": "This outfit contains its own body mesh and needs a manual conversion.",
    "COMPLEX_GEOMETRY": "Complex or loose geometry could not be verified.",
    "SAFE_MODE_UNCERTAIN": "Not converted in Safe mode: the fit could not be verified with high confidence. "
                           "Convert All Possible can try it.",
    "SAFE_MODE_COMPLEX": "Not converted in Safe mode: complex or loose geometry. Convert All Possible can try it.",
    "DUPLICATE_NAME": "Several outfits in this mod have the same name.",
    "EARLIER_CONVERSION_CHANGED": "An earlier conversion of this outfit was changed or is unknown; it was left as it is.",
    "OTHER_BODY": "Made for a different body (not CBBE).",
    "ROUTE_NOT_IMPLEMENTED": "Made for Fusion Girl. Fusion Girl → CBBE conversion is not implemented yet; the original is kept.",
    "SOURCE_BODY_MISMATCH": "Does not match the chosen source body; it was not converted (choose Auto Detect).",
    "UNREADABLE": "The outfit's files could not be read.",
    "VALIDATED_NOT_INSTALLED": "Converted, but could not be installed into the output mod.",
    "UNFINISHED": "The conversion stopped before this outfit.",
    "NOT_VERIFIED": "Could not verify the converted outfit.",
    # 0.5.3 (unattended Outfit Studio)
    "CLOTH_AMBIGUOUS": "Multiple cloth physics data sources require review.",
    "CLOTH_NOT_SELECTED": "Cloth physics data could not be selected automatically.",
    "CLOTH_LOST": "The outfit's cloth physics data was not kept by the conversion.",
    "OS_UNEXPECTED_MESSAGE": "Outfit Studio showed an unexpected message while converting this outfit.",
    "OS_STEP_ERROR": "Outfit Studio reported an error while converting this outfit.",
}

# (pattern over the technical reasons, code) — first match wins
_RULES: list[tuple[re.Pattern, str]] = [(re.compile(p, re.I), c) for p, c in (
    (r"headless automation unavailable|no Outfit Studio 5\.8\+ folder|Outfit Studio not usable", "OS_NOT_AVAILABLE"),
    (r"CLOTH_AMBIGUOUS|Multiple cloth physics data sources", "CLOTH_AMBIGUOUS"),
    (r"CLOTH_NOT_SELECTED|cloth physics data could not be selected", "CLOTH_NOT_SELECTED"),
    (r"OS_UNEXPECTED_MESSAGE|unexpected Outfit Studio message", "OS_UNEXPECTED_MESSAGE"),
    (r"OS_STEP_ERROR", "OS_STEP_ERROR"),
    (r"cloth physics data \(BSClothExtraData\) unchanged", "CLOTH_LOST"),
    (r"changed since the preview|source file changed|source files unchanged \(SHA-256\)", "SOURCE_CHANGED"),
    (r"crashed|did not finish in time|stopped unexpectedly", "OS_STOPPED"),
    (r"did not save this project|never started|closed without saving|not executed|automation exit code", "NOT_CONVERTED_BY_OS"),
    (r"only a body/reference shape", "GEOMETRY_NOT_VERIFIED"),
    (r"no reference vertex within radius|no safe radius|beyond the weight-transfer radius|beyond the search radius",
     "WEIGHT_TRANSFER_UNSAFE"),
    (r"NEW shapes \[", "UNEXPECTED_SHAPE"),
    (r"CBBE-3BBB body NOT embedded|reference shape not listed|reference body in the output", "BODY_IN_OUTPUT"),
    (r"geometry unchanged|vertex positions identical|triangles / topology|vertex/triangle counts", "SHAPE_CHANGED"),
    (r"cross-side / quality anomaly|opposite side of the body|mirrored", "WEIGHTS_LOOK_WRONG"),
    (r"predicted vs actual physics weights|matches the (Copy Bone Weights|full-copy) prediction", "PHYSICS_NOT_VERIFIED"),
    (r"weights normalized — weight sum range", "WEIGHTS_INCOMPLETE"),
    (r"3BBB bones present|physics bones outside planned regions|weights normalized", "PHYSICS_MISSING"),
    (r"existing 3BBB weights|not a declared 3BBB|partially converted|already builds the same (game )?mesh|MANUAL_3BBB",
     "EXISTING_3BBB"),
    (r"embed(s|ded)? (an? )?(non-3BBB )?body|EMBEDDED_BODY|MULTIPLE_BODIES", "EMBEDDED_BODY"),
    (r"share this name|DUPLICATE_NAME", "DUPLICATE_NAME"),
    (r"STALE|AUTO_CHANGED|UNKNOWN_AUTO|unknown existing conversion|changed after the conversion", "EARLIER_CONVERSION_CHANGED"),
    (r"SOURCE_OVERRIDE_CONFLICT|SOURCE_UNVERIFIED|was requested, but", "SOURCE_BODY_MISMATCH"),
    (r"geometry conversion is not implemented", "ROUTE_NOT_IMPLEMENTED"),
    (r"body family|BodyTalk|Fusion Girl|UNSUPPORTED", "OTHER_BODY"),
    (r"not found|cannot be read|cannot read|missing", "UNREADABLE"),
    (r"loose|far garment|garment|outerwear|bridging|custom \(non-skeleton\) bones|fit around physics regions", "COMPLEX_GEOMETRY"),
    (r"analyzer classifies generated project", "NOT_VERIFIED"),
)]


def humanize(outcome: str, reasons: Iterable[str] = (), status: str = "", attempted: bool = True,
             bucket: str = "") -> tuple[str, str]:
    """(code, message) for one project's outcome. Returns ('', '') for outcomes that are not problems."""
    if outcome in ("CONVERTED", "ALREADY_COMPATIBLE", "NO_CONVERSION_NEEDED"):
        return "", ""
    reasons = [r for r in reasons if r]
    if outcome == "SKIPPED":
        code = "SAFE_MODE_COMPLEX" if bucket == "REVIEW_COMPLEX_GEOMETRY" else "SAFE_MODE_UNCERTAIN"
        return code, MESSAGES[code]
    if outcome == "UNFINISHED":
        if any("could not write the output mod" in r or "commit" in r for r in reasons):
            return "VALIDATED_NOT_INSTALLED", MESSAGES["VALIDATED_NOT_INSTALLED"]
        return "UNFINISHED", MESSAGES["UNFINISHED"]
    text = " | ".join([status] + reasons)
    for pat, code in _RULES:
        if pat.search(text):
            if code == "COMPLEX_GEOMETRY" and not attempted and outcome == "COULD_NOT_CONVERT_SAFELY":
                return "SAFE_MODE_COMPLEX", MESSAGES["SAFE_MODE_COMPLEX"].replace(" Convert All Possible can try it.", "")
            return code, MESSAGES[code]
    if bucket == "REVIEW_COMPLEX_GEOMETRY":
        return "COMPLEX_GEOMETRY", MESSAGES["COMPLEX_GEOMETRY"]
    return "NOT_VERIFIED", MESSAGES["NOT_VERIFIED"]


def package_message(code: str, output_mod_name: str = "CBBE 3BBB Auto Conversions") -> str:
    """User message for a package-level warning code."""
    if code == "OUTPUT_MOD_DISABLED_IN_MO2":
        return (f"Generated conversions are disabled in Mod Organizer 2.\nEnable \"{output_mod_name}\" before building "
                "outfits.")
    if code == "OUTPUT_MOD_NOT_IN_PROFILE":
        return (f"\"{output_mod_name}\" is not in the Mod Organizer 2 mod list yet. Refresh Mod Organizer 2 (F5) and "
                "enable it before building outfits.")
    return code


def system_message(reason: str) -> str:
    """User message for a SYSTEM failure (the whole package stopped)."""
    r = reason.lower()
    if "source mod changed" in r or "reference changed" in r or "disappeared" in r:
        return ("Files of the outfit mod or of the CBBE 3BBB body changed while converting. Nothing more was converted. "
                "Analyze the mod again, then convert.")
    if "mo2 profile" in r:
        return ("The Mod Organizer 2 mod list changed while converting. Nothing more was converted. Analyze the mod "
                "again, then convert.")
    if "output" in r:
        return "The output mod cannot be written. Check that the drive is available, then press Resume."
    if "workspace" in r:
        return "The converter's work folder cannot be used. Check free disk space, then press Resume."
    if "outfit studio not usable" in r:
        return "Outfit Studio could not be started. Check the Outfit Studio folder in Settings."
    return "The conversion stopped because of a problem that affects the whole package."


# 0.5.2: one short phrase per problem for the "Needs attention / original kept" list of the normal window
SHORT = {
    "WEIGHT_TRANSFER_UNSAFE": "physics conversion unavailable",
    "WEIGHTS_INCOMPLETE": "physics conversion unavailable",
    "PHYSICS_NOT_VERIFIED": "physics could not be verified",
    "PHYSICS_MISSING": "expected physics bones missing (rejected)",
    "UNEXPECTED_SHAPE": "conversion result rejected (unplanned part)",
    "WEIGHTS_LOOK_WRONG": "physics could not be verified",
    "GEOMETRY_NOT_VERIFIED": "physics could not be verified",
    "COMPLEX_GEOMETRY": "physics could not be verified",
    "NOT_VERIFIED": "physics could not be verified",
    "SHAPE_CHANGED": "conversion result rejected",
    "BODY_IN_OUTPUT": "conversion result rejected",
    "EXISTING_3BBB": "already has its own physics weights (kept as is)",
    "EMBEDDED_BODY": "contains its own body (needs manual conversion)",
    "SAFE_MODE_UNCERTAIN": "not tried in Safe mode",
    "SAFE_MODE_COMPLEX": "not tried in Safe mode",
    "NOT_CONVERTED_BY_OS": "conversion did not finish",
    "OS_STOPPED": "conversion did not finish",
    "OS_NOT_AVAILABLE": "conversion did not finish (Outfit Studio not available)",
    "UNFINISHED": "conversion did not finish",
    "VALIDATED_NOT_INSTALLED": "converted but not installed",
    "SOURCE_CHANGED": "files changed during conversion",
    "DUPLICATE_NAME": "duplicate outfit name in the mod",
    "EARLIER_CONVERSION_CHANGED": "earlier conversion changed (kept as is)",
    "OTHER_BODY": "made for another body",
    "ROUTE_NOT_IMPLEMENTED": "Fusion Girl: conversion not implemented yet",
    "SOURCE_BODY_MISMATCH": "does not match the chosen source body",
    "UNREADABLE": "outfit files could not be read",
    "CLOTH_AMBIGUOUS": "multiple cloth physics data sources (needs review)",
    "CLOTH_NOT_SELECTED": "cloth physics data could not be selected automatically",
    "CLOTH_LOST": "cloth physics data not kept (rejected)",
    "OS_UNEXPECTED_MESSAGE": "Outfit Studio showed an unexpected message",
    "OS_STEP_ERROR": "Outfit Studio reported an error",
    "RUNTIME_FAIL": "converted, but no physics in game (reported)",
    "RUNTIME_PARTIAL": "converted, but physics problem in game (reported)",
}
# in-game verification states recorded by the user (manifest acceptance); PASS clears a report
RUNTIME_STATES = {"FAIL": "RUNTIME_FAIL", "PARTIAL": "RUNTIME_PARTIAL"}


def failed_check(reasons: Iterable[str]) -> str:
    """0.6.1: the first strict-validation check that failed, as the engine named it ('' when none)."""
    for r in reasons:
        if r.startswith("validation failed: "):
            return r[len("validation failed: "):].split(" — ", 1)[0].strip()
    return ""


def short_reason(code: str) -> str:
    return SHORT.get(code, "physics could not be verified")

