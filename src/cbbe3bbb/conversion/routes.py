"""Conversion routes (0.5.3 preparation, 0.6.0 Body Architecture Foundation).

A route is a *supported conversion strategy*, not just two labels. It has two independent stages:

* the **geometry stage** fits the outfit's shape to the target body's geometry (Fusion Girl → CBBE, …);
* the **physics stage** gives the fitted outfit the target's physics (CBBE → CBBE 3BBB: Outfit Studio
  Copy Bone Weights from the installed CBBE 3BBB reference — the accepted 0.5.x backend).

A route may run only when *every* required stage is available (:attr:`ConversionRoute.executable`): the planner never
skips a required geometry stage because it knows how to add 3BBB weights.

Registry (honest status; nothing here claims more than the 0.5.x backend does):

=============================  ==================  ==============================================================
route_id                       status              behaviour
=============================  ==================  ==============================================================
CBBE_TO_CBBE_3BBB              PRODUCTION          the accepted backend (CP5, Raider Punk, Wardrobe 10; 0.5.3)
CBBE_3BBB_TO_CBBE_3BBB         ALREADY_COMPATIBLE  kept as is (analyzer KEEP when its 3BBB weights check out)
TWB_3BBB_TO_CBBE_3BBB          ALREADY_COMPATIBLE  kept as is: 3BBB weights present (analyzer KEEP, unchanged)
TWB_TO_CBBE_3BBB               LEGACY_SUPPORTED    0.5.x behaviour preserved: TWB is CBBE-derived; the analyzer
                                                   fits it against the CBBE 3BBB reference and the CBBE physics
                                                   stage runs; not separately accepted in game
UNKNOWN_3BBB_TO_CBBE_3BBB      ALREADY_COMPATIBLE  "3BBB (body unknown)": kept as is (analyzer KEEP / REVIEW)
FUSION_GIRL_TO_CBBE_3BBB       NOT_IMPLEMENTED     geometry stage Fusion Girl → CBBE not implemented
BODYTALK_MALE_TO_CBBE_3BBB     UNSUPPORTED         male bodies are out of scope
UNKNOWN_TO_CBBE_3BBB           REVIEW              body not determined: never converted automatically
=============================  ==================  ==============================================================
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import bodies as B

__all__ = ["Stage", "ConversionRoute", "BodyRoute", "ROUTES", "DEFAULT_ROUTE", "resolve_route", "route_for",
           "route_by_id", "RouteDecision", "decide_route", "resolve_request", "SOURCE_BODIES", "TARGET_BODIES",
           "RouteNotImplemented", "detect_source_family", "body_summary", "PRODUCTION", "LEGACY_SUPPORTED",
           "ALREADY_COMPATIBLE", "NOT_IMPLEMENTED", "UNSUPPORTED", "REVIEW", "SOURCE_OVERRIDE_CONFLICT",
           "SOURCE_UNVERIFIED", "NOT_REQUIRED"]

# route / stage statuses
PRODUCTION = "PRODUCTION"
LEGACY_SUPPORTED = "LEGACY_SUPPORTED"
ALREADY_COMPATIBLE = "ALREADY_COMPATIBLE"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
UNSUPPORTED = "UNSUPPORTED"
REVIEW = "REVIEW"
NOT_REQUIRED = "NOT_REQUIRED"
NOT_APPLICABLE = "NOT_APPLICABLE"
# decision statuses (a requested source that contradicts the detection)
SOURCE_OVERRIDE_CONFLICT = "SOURCE_OVERRIDE_CONFLICT"
SOURCE_UNVERIFIED = "SOURCE_UNVERIFIED"
_RUNNABLE = (PRODUCTION, LEGACY_SUPPORTED)

# legacy (0.5.3) display maps, kept for old callers
SOURCE_BODIES = {"CBBE": "CBBE", "FUSION_GIRL": "Fusion Girl", "BODYTALK": "BodyTalk"}
TARGET_BODIES = {"CBBE_3BBB": "CBBE 3BBB"}
_LEGACY_FAMILY = {B.CBBE: "CBBE", B.FUSION_GIRL: "FUSION_GIRL", B.TWB: "TWB", B.BODYTALK_MALE: "BODYTALK",
                  B.CBBE_3BBB: "CBBE_3BBB", B.TWB_3BBB: "TWB_3BBB", B.UNKNOWN: "UNKNOWN", B.UNKNOWN_3BBB: "UNKNOWN_3BBB"}


class RouteNotImplemented(ValueError):
    pass


@dataclass(frozen=True)
class Stage:
    id: str
    status: str  # NOT_REQUIRED | PRODUCTION | LEGACY_SUPPORTED | NOT_IMPLEMENTED | NOT_APPLICABLE
    strategy: str = ""
    description: str = ""

    @property
    def available(self) -> bool:
        return self.status in (NOT_REQUIRED, PRODUCTION, LEGACY_SUPPORTED)

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "strategy": self.strategy, "description": self.description}


GEOMETRY_IDENTITY = Stage("geometry.identity", NOT_REQUIRED, "none (same body)",
                          "the outfit already fits the target's CBBE geometry")
GEOMETRY_TWB_AS_CBBE = Stage("geometry.twb_as_cbbe", LEGACY_SUPPORTED, "none (TWB treated as CBBE-derived geometry)",
                             "0.5.x behaviour: the fit is measured against the CBBE 3BBB reference per region")
GEOMETRY_FG_TO_CBBE = Stage("geometry.fusion_girl_to_cbbe", NOT_IMPLEMENTED, "fusion_girl_to_cbbe (planned)",
                            "Fusion Girl geometry conversion is not implemented yet")
GEOMETRY_NONE = Stage("geometry.none", NOT_APPLICABLE, "", "no conversion")
PHYSICS_CBBE_3BBB = Stage("physics.cbbe_to_cbbe_3bbb", PRODUCTION, "outfit_studio_copy_bone_weights_from_reference",
                          "Outfit Studio Copy Bone Weights from the installed CBBE 3BBB reference (accepted 0.5.x backend)")
PHYSICS_KEEP = Stage("physics.keep", NOT_REQUIRED, "keep existing 3BBB weights", "already carries 3BBB physics")
PHYSICS_AFTER_GEOMETRY = Stage("physics.cbbe_to_cbbe_3bbb", PRODUCTION, "outfit_studio_copy_bone_weights_from_reference",
                               "available after the geometry stage (not reached while geometry is not implemented)")
PHYSICS_NONE = Stage("physics.none", NOT_APPLICABLE, "", "no conversion")

# validation stages per route (CBBE → CBBE 3BBB = the current strict validation, authoritative and unchanged)
VALIDATION_CBBE_3BBB = ("source immutability (SHA-256)", "expected 3BBB physics bones and weights",
                        "geometry/topology invariants", "normal/tangent invariants", "cloth data (BSClothExtraData) invariants",
                        "project/output invariants", "strict validation (validate.py, unchanged)")
VALIDATION_FG_PLANNED = ("source immutability (SHA-256)", "geometry conversion validation",
                         "CBBE intermediate validation", "physics conversion validation", "final CBBE 3BBB validation")


@dataclass(frozen=True)
class ConversionRoute:
    route_id: str
    source_body: str
    target_body: str
    status: str
    geometry_stage: Stage
    physics_stage: Stage
    validator: str = ""
    validation_stages: tuple = ()
    capabilities: tuple = ()
    limitations: tuple = ()
    legacy_id: str = ""  # 0.5.3 id ("cbbe-to-cbbe3bbb"), kept for old records/callers
    physics_profile: str = B.CBBE_3BBB

    @property
    def id(self) -> str:  # 0.5.3 compatibility: the old id where one existed
        return self.legacy_id or self.route_id

    @property
    def label(self) -> str:
        via = " → CBBE" if self.geometry_stage.id == GEOMETRY_FG_TO_CBBE.id else ""
        return f"{B.body_label(self.source_body)}{via} → {B.body_label(self.target_body)}"

    @property
    def implemented(self) -> bool:
        return self.executable

    @property
    def executable(self) -> bool:
        """May this route convert? Only when the route and EVERY stage it requires are available."""
        return (self.status in _RUNNABLE and self.geometry_stage.available
                and self.physics_stage.status in (PRODUCTION, LEGACY_SUPPORTED))

    # 0.5.3 field names (manifest/batch "route" records)
    @property
    def source_body_family(self) -> str:
        return _LEGACY_FAMILY.get(self.source_body, self.source_body.upper())

    @property
    def target_body_family(self) -> str:
        return _LEGACY_FAMILY.get(self.target_body, self.target_body.upper())

    @property
    def geometry_conversion_strategy(self) -> str:
        return self.geometry_stage.strategy

    @property
    def weight_transfer_strategy(self) -> str:
        return self.physics_stage.strategy

    def to_dict(self) -> dict:
        return {
            # 0.5.3 keys (readers of old records rely on them)
            "id": self.id, "source_body_family": self.source_body_family, "target_body_family": self.target_body_family,
            "physics_profile": self.physics_profile.upper(), "geometry_conversion_strategy": self.geometry_stage.strategy,
            "weight_transfer_strategy": self.physics_stage.strategy, "implemented": self.executable, "label": self.label,
            # 0.6.0 keys
            "route_id": self.route_id, "source_body": self.source_body, "target_body": self.target_body,
            "status": self.status, "geometry_stage": self.geometry_stage.to_dict(),
            "physics_stage": self.physics_stage.to_dict(), "validator": self.validator,
            "validation_stages": list(self.validation_stages), "capabilities": list(self.capabilities),
            "limitations": list(self.limitations),
        }


BodyRoute = ConversionRoute  # 0.5.3 name

ROUTES = (
    ConversionRoute("CBBE_TO_CBBE_3BBB", B.CBBE, B.CBBE_3BBB, PRODUCTION, GEOMETRY_IDENTITY, PHYSICS_CBBE_3BBB,
                    "cbbe_to_cbbe_3bbb_strict", VALIDATION_CBBE_3BBB,
                    ("unattended Outfit Studio", "cloth data (BSClothExtraData) preserved and verified",
                     "skin-weight renormalization", "embedded CBBE body replacement (opt-in)"),
                    ("loose/far garments, existing undeclared 3BBB weights and embedded bodies need review",),
                    legacy_id="cbbe-to-cbbe3bbb"),
    ConversionRoute("CBBE_3BBB_TO_CBBE_3BBB", B.CBBE_3BBB, B.CBBE_3BBB, ALREADY_COMPATIBLE, GEOMETRY_IDENTITY,
                    PHYSICS_KEEP, limitations=("kept as is; suspicious or partial 3BBB weights go to review",)),
    ConversionRoute("TWB_3BBB_TO_CBBE_3BBB", B.TWB_3BBB, B.CBBE_3BBB, ALREADY_COMPATIBLE, GEOMETRY_IDENTITY,
                    PHYSICS_KEEP, limitations=("kept as is (existing behaviour); its body stays TWB",)),
    ConversionRoute("TWB_TO_CBBE_3BBB", B.TWB, B.CBBE_3BBB, LEGACY_SUPPORTED, GEOMETRY_TWB_AS_CBBE, PHYSICS_CBBE_3BBB,
                    "cbbe_to_cbbe_3bbb_strict", VALIDATION_CBBE_3BBB,
                    limitations=("0.5.x behaviour preserved: TWB is treated as CBBE-derived geometry",
                                 "not separately accepted in game; validation is the CBBE route's, unchanged")),
    ConversionRoute("UNKNOWN_3BBB_TO_CBBE_3BBB", B.UNKNOWN_3BBB, B.CBBE_3BBB, ALREADY_COMPATIBLE, GEOMETRY_IDENTITY,
                    PHYSICS_KEEP, limitations=("3BBB weights on an unnamed body: kept as is, never converted",)),
    ConversionRoute("FUSION_GIRL_TO_CBBE_3BBB", B.FUSION_GIRL, B.CBBE_3BBB, NOT_IMPLEMENTED, GEOMETRY_FG_TO_CBBE,
                    PHYSICS_AFTER_GEOMETRY, "fusion_girl_to_cbbe_3bbb (planned)", VALIDATION_FG_PLANNED,
                    limitations=("Fusion Girl geometry conversion is not implemented yet",),
                    legacy_id="fusiongirl-to-cbbe3bbb"),
    ConversionRoute("BODYTALK_MALE_TO_CBBE_3BBB", B.BODYTALK_MALE, B.CBBE_3BBB, UNSUPPORTED, GEOMETRY_NONE, PHYSICS_NONE,
                    limitations=("male / BodyTalk bodies are out of scope",)),
    ConversionRoute("UNKNOWN_TO_CBBE_3BBB", B.UNKNOWN, B.CBBE_3BBB, REVIEW, GEOMETRY_NONE, PHYSICS_NONE,
                    limitations=("the body could not be determined: never converted automatically",)),
)
DEFAULT_ROUTE = ROUTES[0]
_BY_PAIR = {(r.source_body, r.target_body): r for r in ROUTES}


def route_by_id(route_id: str) -> Optional[ConversionRoute]:
    v = str(route_id or "")
    return next((r for r in ROUTES if v in (r.route_id, r.legacy_id) or v.upper() == r.route_id or v == r.label), None)


def route_for(source_body: str, target_body: str = B.CBBE_3BBB) -> ConversionRoute:
    """The registry route for a (detected) source body; an unknown pairing is the UNKNOWN review route."""
    return _BY_PAIR.get((source_body, target_body)) or _BY_PAIR[(B.UNKNOWN, B.CBBE_3BBB)]


def resolve_route(route: Optional[str] = None) -> ConversionRoute:
    """0.5.3 API: the route a package run uses ('' / None / 'auto' = CBBE → CBBE 3BBB). Raises unless executable."""
    if not route or str(route).lower() in ("auto", "auto-detect", B.AUTO):
        return DEFAULT_ROUTE
    r = route_by_id(route)
    if r is None:
        raise RouteNotImplemented(f"unknown conversion route '{route}'")
    if not r.executable:
        raise RouteNotImplemented(f"{r.label}: this conversion is not implemented yet" if r.status == NOT_IMPLEMENTED
                                  else f"{r.label}: {r.limitations[0] if r.limitations else r.status}")
    return r


def resolve_request(source_body: Optional[str] = None, target_body: Optional[str] = None) -> tuple[str, str]:
    """Validate a user request (GUI / CLI). Returns (source, target) ids; raises RouteNotImplemented for a pairing that
    cannot run (e.g. Fusion Girl → CBBE 3BBB: "Fusion Girl geometry conversion is not implemented yet")."""
    src = B.normalize_body_id(source_body, B.AUTO)
    dst = B.normalize_body_id(target_body, B.CBBE_3BBB)
    if dst not in B.TARGET_CHOICES:
        raise RouteNotImplemented(f"target body {B.body_label(dst)} is not available (only CBBE 3BBB)")
    if src not in B.SOURCE_CHOICES:
        raise RouteNotImplemented(f"{B.body_label(src)} cannot be chosen as a source body")
    if src != B.AUTO:
        r = route_for(src, dst)
        if not r.executable:
            msg = r.geometry_stage.description if r.status == NOT_IMPLEMENTED else (r.limitations or (r.status,))[0]
            raise RouteNotImplemented(f"{r.label}: {msg}")
    return src, dst


@dataclass
class RouteDecision:
    requested_source: str
    target_body: str
    detection: B.BodyDetection
    route: ConversionRoute
    status: str  # the route's status, or SOURCE_OVERRIDE_CONFLICT / SOURCE_UNVERIFIED
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def source_body(self) -> str:
        return self.detection.body

    @property
    def may_convert(self) -> bool:
        return self.status in _RUNNABLE and self.route.executable

    def to_dict(self) -> dict:
        return {"requested_source": self.requested_source, "source_body": self.detection.body,
                "target_body": self.target_body, "route_id": self.route.route_id, "route_status": self.status,
                "route_label": self.route.label, "geometry_stage": self.route.geometry_stage.to_dict(),
                "physics_stage": self.route.physics_stage.to_dict(), "reason": self.reason,
                "detection": self.detection.to_dict(), "validator": self.route.validator}


def decide_route(detection: B.BodyDetection, requested_source: str = B.AUTO,
                 target_body: str = B.CBBE_3BBB) -> RouteDecision:
    """The route of ONE project. Auto Detect follows the detection. An explicit source request only *confirms* the
    detection: it never reinterprets another body's geometry (strong contrary evidence → SOURCE_OVERRIDE_CONFLICT) and
    never upgrades an undetermined body (→ SOURCE_UNVERIFIED). There is no expert override in 0.6.0."""
    req = B.normalize_body_id(requested_source, B.AUTO)
    route = route_for(detection.body, target_body)
    d = RouteDecision(req, target_body, detection, route, route.status)
    if req != B.AUTO and req != detection.base_body:
        if detection.base_body == B.UNKNOWN:
            d.status = SOURCE_UNVERIFIED
            d.reason = (f"Source body {B.body_label(req)} was requested, but the body could not be determined "
                        "for this outfit; it is not converted on the request alone")
        else:
            d.status = SOURCE_OVERRIDE_CONFLICT
            d.reason = (f"Source body {B.body_label(req)} was requested, but this outfit was detected as "
                        f"{detection.label} ({detection.confidence}); it is not reinterpreted")
        return d
    if route.status == NOT_IMPLEMENTED:
        d.reason = (f"{route.label}: {route.geometry_stage.description}")
    elif route.status == UNSUPPORTED:
        d.reason = f"{route.label}: {(route.limitations or ('not supported',))[0]}"
    elif route.status == REVIEW:
        d.reason = f"{route.label}: {(route.limitations or ('needs review',))[0]}"
    return d


def detect_source_family(preview) -> str:
    """0.5.3 API: the analyzer family label most projects of a package were made for ('' when nothing detected)."""
    counts: dict[str, int] = {}
    for p in getattr(preview, "projects", []) or []:
        fam = getattr(p, "family", None)
        label = getattr(fam, "label", fam) if fam is not None else ""
        if isinstance(fam, dict):
            label = fam.get("label", "")
        if label and str(label).lower() != "unknown":
            counts[str(label)] = counts.get(str(label), 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def body_summary(preview) -> dict[str, int]:
    """Detected source bodies of a package: {body id: number of projects} (0.6.0 preview fields)."""
    out: dict[str, int] = {}
    for p in getattr(preview, "projects", []) or []:
        b = getattr(p, "source_body", "") or ""
        if b:
            out[b] = out.get(b, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
