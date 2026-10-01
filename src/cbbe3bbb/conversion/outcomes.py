"""Phase 3.2: package-level policy and outcomes for mass conversion (no I/O except the report writer).

* **Selection modes.** ``SAFE`` converts CONVERT + HIGH projects only (the default selection of the Batch Preview).
  ``ALL_POSSIBLE`` also *attempts* CONVERT projects of MEDIUM/LOW confidence and REVIEW projects whose review reason is
  attemptable. It never disables a check: an attempted project is committed only if it validates.
* **REVIEW policy.** ``REVIEW_ATTEMPTABLE``: Outfit Studio can technically convert it and the validator can judge the
  result (loose or unusual geometry, garment-type risks, uncertain fit). ``REVIEW_BLOCKED``: attempting it would
  knowingly produce invalid output or overwrite something unresolved (no safe weight-transfer radius, embedded body,
  existing or ambiguous 3BBB weights, generated-name collisions, changed or unknown earlier conversions, missing
  reference data).
* **Outcomes** (what the user sees per project): ``CONVERTED``, ``ALREADY_COMPATIBLE``, ``NO_CONVERSION_NEEDED``,
  ``COULD_NOT_CONVERT_SAFELY``, ``FAILED``, ``SKIPPED`` (not attempted in this mode) and ``UNFINISHED`` (the batch was
  interrupted before it). The original outfit is never changed, whatever the outcome.
* **Report buckets** (the package report): ``SAFE_AUTO_DONE``, ``MEDIUM_NOT_AUTO``, ``REVIEW_COMPLEX_GEOMETRY``,
  ``BLOCKED_WEIGHT_TRANSFER``, ``EXISTING_3BBB_AMBIGUOUS``, ``NO_PHYSICS_NEEDED``, ``FAILED_TECHNICAL``, plus
  ``ALREADY_COMPATIBLE`` (native 3BBB projects) and ``UNSUPPORTED_BODY`` (not CBBE).
* **Package result**: ``COMPLETE``, ``COMPLETE_WITH_WARNINGS`` (some outfits could not be converted), ``INTERRUPTED``
  (unfinished projects remain; resumable), ``SYSTEM_ERROR`` (stopped for a reason that concerns the whole package).
"""
from __future__ import annotations

from typing import Iterable, Optional

__all__ = [
    "MODE_SAFE", "MODE_ALL_POSSIBLE", "POLICY_ATTEMPTABLE", "POLICY_BLOCKED", "OUTCOMES", "BUCKETS",
    "analyzer_review_attemptable", "preview_bucket", "final_bucket", "outcome_for", "package_result", "display_name",
    "applicable_percent",
]

MODE_SAFE, MODE_ALL_POSSIBLE = "SAFE", "ALL_POSSIBLE"
POLICY_ATTEMPTABLE, POLICY_BLOCKED = "REVIEW_ATTEMPTABLE", "REVIEW_BLOCKED"
OUTCOMES = ("CONVERTED", "ALREADY_COMPATIBLE", "NO_CONVERSION_NEEDED", "COULD_NOT_CONVERT_SAFELY", "FAILED", "SKIPPED",
            "UNFINISHED")
BUCKETS = ("SAFE_AUTO_DONE", "MEDIUM_NOT_AUTO", "REVIEW_COMPLEX_GEOMETRY", "BLOCKED_WEIGHT_TRANSFER",
           "EXISTING_3BBB_AMBIGUOUS", "NO_PHYSICS_NEEDED", "FAILED_TECHNICAL", "ALREADY_COMPATIBLE", "UNSUPPORTED_BODY")

# analyzer REVIEW reasons that are never attempted automatically, even in Convert All Possible
_HARD_REVIEW = (
    ("existing 3BBB weights present", "existing 3BBB weights would be overwritten"),
    ("not a declared 3BBB conversion", "existing 3BBB weights would be overwritten"),
    ("partially converted", "existing partial 3BBB weighting would be overwritten"),
    ("legacy CBBE Physics cloth-bone weights", "legacy physics weights must be replaced by hand, not merged"),
    ("body family could not be determined", "the body family is unknown"),
    ("reference not available", "the CBBE 3BBB reference is not available"),
    ("loose/far garment: decide manually", "the garment lies beyond the weight-transfer radius"),
)
_EXISTING_3BBB = ("existing 3BBB weights present", "not a declared 3BBB conversion", "partially converted")
_ROUTE_STATUSES = {"ROUTE_NOT_EXECUTABLE", "SOURCE_OVERRIDE_CONFLICT", "SOURCE_UNVERIFIED"}  # 0.6.0 body-route gate
_BLOCKED_STATUSES = {"ROUTE_NOT_EXECUTABLE", "SOURCE_OVERRIDE_CONFLICT", "SOURCE_UNVERIFIED", "BLOCKED", "EMBEDDED_BODY", "EMBEDDED_BODY_AMBIGUOUS", "EMBEDDED_BODY_UNSUPPORTED", "MULTIPLE_BODIES",
                     "MANUAL_3BBB_VARIANT", "DUPLICATE_NAME", "STALE", "AUTO_CHANGED", "UNKNOWN_AUTO"}


def analyzer_review_attemptable(action: str, reasons: list[str]) -> tuple[bool, str]:
    """Is an analyzer-REVIEW project a candidate for Convert All Possible? Returns (attemptable, why not)."""
    if action != "REVIEW":
        return False, f"analyzer action {action}"
    text = " ".join(reasons)
    for marker, why in _HARD_REVIEW:
        if marker in text:
            return False, why
    if not reasons or not reasons[0].startswith("physics-relevant regions:"):
        return False, "not a conversion candidate"
    return True, ""


def preview_bucket(action: str, status: str, reasons: Iterable[str], confidence: str = "", selected_safe: bool = False) -> str:
    """Report bucket of a project from the Batch Preview alone (before any conversion)."""
    text = " ".join(reasons)
    if action == "KEEP":
        return "SAFE_AUTO_DONE" if status == "ALREADY_CONVERTED" else "ALREADY_COMPATIBLE"
    if action == "SKIP":
        return "NO_PHYSICS_NEEDED"
    if action == "UNSUPPORTED" or status in _ROUTE_STATUSES:
        return "UNSUPPORTED_BODY"
    if action == "ERROR":
        return "FAILED_TECHNICAL"
    if action == "CONVERT":
        if status == "EMBEDDED_BODY_REPLACEMENT":
            return "REVIEW_COMPLEX_GEOMETRY"
        return "SAFE_AUTO_DONE" if selected_safe else "MEDIUM_NOT_AUTO"
    # REVIEW
    if "no reference vertex within radius" in text or "no safe radius" in text:
        return "BLOCKED_WEIGHT_TRANSFER"
    if status == "BLOCKED":
        return "REVIEW_COMPLEX_GEOMETRY"
    if status in ("MANUAL_3BBB_VARIANT", "STALE", "AUTO_CHANGED", "UNKNOWN_AUTO"):
        return "EXISTING_3BBB_AMBIGUOUS"
    if status == "DUPLICATE_NAME":
        return "FAILED_TECHNICAL"
    if any(m in text for m in _EXISTING_3BBB):
        return "EXISTING_3BBB_AMBIGUOUS"
    return "REVIEW_COMPLEX_GEOMETRY"


def outcome_for(action: str, status: str, batch_status: Optional[str] = None, attempted: bool = False,
                committing: bool = True) -> str:
    """User-facing outcome of one project. ``batch_status`` is its status in the batch when it was attempted."""
    if batch_status:
        if batch_status == "COMMITTED" or (batch_status == "VALIDATED" and not committing):
            return "CONVERTED"
        if batch_status == "VALIDATED":
            return "UNFINISHED"  # validated but not installed (commit pending or refused)
        if batch_status == "REVIEW":
            return "COULD_NOT_CONVERT_SAFELY"
        if batch_status == "FAILED":
            return "FAILED"
        return "UNFINISHED"  # PENDING / ATTEMPTING / GENERATED
    if action == "KEEP":
        return "ALREADY_COMPATIBLE"
    if action == "SKIP":
        return "NO_CONVERSION_NEEDED"
    if action == "ERROR":
        return "FAILED"
    if action == "UNSUPPORTED":
        return "COULD_NOT_CONVERT_SAFELY"
    if action == "CONVERT" and status not in _BLOCKED_STATUSES:
        return "UNFINISHED" if attempted else "SKIPPED"
    return "COULD_NOT_CONVERT_SAFELY"


def final_bucket(preview_b: str, batch_status: Optional[str], reasons: Iterable[str] = ()) -> str:
    """Report bucket after the conversion: what happened overrides what was expected."""
    if batch_status in ("COMMITTED", "VALIDATED"):
        return "SAFE_AUTO_DONE"
    if batch_status == "REVIEW":
        text = " ".join(reasons)
        if "weights normalized — weight sum range" in text or "no reference vertex within radius" in text:
            return "BLOCKED_WEIGHT_TRANSFER"
        return "REVIEW_COMPLEX_GEOMETRY"
    if batch_status == "FAILED":
        return "FAILED_TECHNICAL"
    return preview_b


def package_result(outcomes: Iterable[str], batch_state: str = "") -> str:
    outs = list(outcomes)
    if batch_state == "SYSTEM_ERROR":
        return "SYSTEM_ERROR"
    if batch_state == "INTERRUPTED" or "UNFINISHED" in outs:
        return "INTERRUPTED"
    if any(o in ("COULD_NOT_CONVERT_SAFELY", "FAILED") for o in outs):
        return "COMPLETE_WITH_WARNINGS"
    return "COMPLETE"


def applicable_percent(outcomes: Iterable[str]) -> Optional[int]:
    """Share of the outfits that needed a conversion and got one (already compatible / not needed excluded)."""
    outs = list(outcomes)
    applicable = [o for o in outs if o not in ("ALREADY_COMPATIBLE", "NO_CONVERSION_NEEDED")]
    if not applicable:
        return None
    return round(100 * sum(1 for o in applicable if o == "CONVERTED") / len(applicable))


def display_name(project: str, item_names: Iterable[str] = (), mesh: str = "") -> str:
    """Name a normal user recognises: the in-game item name, else the BodySlide project name, else the mesh id."""
    names = [n for n in dict.fromkeys(x.strip() for x in item_names if x and x.strip())]
    if names:
        return names[0] if len(names) == 1 else f"{names[0]} (+{len(names) - 1})"
    if project:
        return project
    return (mesh.replace("/", "\\").rsplit("\\", 1)[-1]) if mesh else "?"
