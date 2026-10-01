"""Typed result models for the analyzer (JSON-serialisable via ``to_dict``)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

__all__ = [
    "Action",
    "Level",
    "Region",
    "RegionMetrics",
    "DistanceStats",
    "ShapeAnalysis",
    "FamilyResult",
    "ProjectResult",
]


class Action(str, Enum):
    KEEP = "KEEP"
    CONVERT = "CONVERT"
    SKIP = "SKIP"
    REVIEW = "REVIEW"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"


class Level(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        return {"NONE": 0, "UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}[self.value]


class Region(str, Enum):
    BREAST = "BREAST"
    BUTT = "BUTT"  # butt / pelvis
    THIGH = "THIGH"
    BELLY = "BELLY"
    OTHER = "OTHER"


@dataclass
class DistanceStats:
    """Distances from outfit vertices to the nearest reference body vertex."""

    median: float = 0.0
    p90: float = 0.0
    max: float = 0.0
    frac_within_radius: float = 0.0
    frac_tight: float = 0.0


@dataclass
class RegionMetrics:
    region: str
    relevance: str = Level.NONE.value
    # --- geometry / reference correspondence (what a transfer would produce)
    predicted_vertices: int = 0  # outfit vertices that would receive >= threshold region weight
    predicted_fraction: float = 0.0  # ... as a fraction of the shape's vertices
    predicted_mass: float = 0.0
    reference_coverage: float = 0.0  # fraction of the reference region covered by this shape
    footprint_vertices: int = 0  # vertices whose nearest reference vertex is region-weighted
    footprint_within_radius: float = 0.0
    footprint_median_distance: float = 0.0
    footprint_p90_distance: float = 0.0
    fit: str = Level.UNKNOWN.value  # HIGH = tight, LOW = loose/far
    # --- existing weights in the outfit
    existing_bones: dict[str, int] = field(default_factory=dict)  # 3BBB region bone -> weighted vertices
    existing_vertices: int = 0
    existing_fraction: float = 0.0
    legacy_bones: dict[str, int] = field(default_factory=dict)
    # --- quality of existing weights (None when not applicable)
    agreement: Optional[float] = None  # 1 - mean |existing - predicted| over relevant vertices
    outside_region_fraction: Optional[float] = None  # existing weight where reference has none
    lr_balance: Optional[float] = None  # min(L,R)/max(L,R) of weighted vertex counts
    predicted_lr_balance: Optional[float] = None  # the same ratio for the predicted transfer
    side_coverage: dict[str, float] = field(default_factory=dict)  # 'L'/'R' -> reference coverage per side
    predicted_cross_side_fraction: Optional[float] = None  # predicted side weights landing on the other side
    cross_side_fraction: Optional[float] = None  # L weights on the right side of the body (or vice versa)
    weight_islands: Optional[int] = None
    predicted_weight_islands: Optional[int] = None
    abrupt_edge_fraction: Optional[float] = None


@dataclass
class ShapeAnalysis:
    name: str
    target: str
    block_type: str = ""
    vertices: int = 0
    triangles: int = 0
    skinned: bool = False
    is_body: bool = False
    body_match: str = ""  # e.g. "CBBE 3BBB Body", "CBBE", "TWB", "male"
    bone_count: int = 0
    physics_bones: dict[str, int] = field(default_factory=dict)  # 3BBB physics bone -> weighted vertices
    legacy_physics_bones: dict[str, int] = field(default_factory=dict)
    custom_bones: list[str] = field(default_factory=list)
    unweighted_fraction: float = 0.0
    distance: DistanceStats = field(default_factory=DistanceStats)
    regions: dict[str, RegionMetrics] = field(default_factory=dict)
    sliders: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class FamilyResult:
    family: str = "unknown"  # CBBE, TWB, FUSION_GIRL, BODYTALK, MALE, UNKNOWN
    physics: str = "none"  # none, standard, legacy_cbbe_physics, 3bbb
    label: str = "unknown"  # human readable, e.g. "CBBE 3BBB"
    confidence: str = Level.LOW.value
    evidence: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)


@dataclass
class ProjectResult:
    project: str
    mod: str
    provider_kind: str
    osp_rel: str
    osp_path: str
    set_index: int = 0
    shapedata_rel: str = ""
    shapedata_path: str = ""
    shapedata_provider: str = ""
    output_path: str = ""
    output_file: str = ""
    slider_count: int = 0
    zap_count: int = 0
    shape_names: list[str] = field(default_factory=list)
    family: FamilyResult = field(default_factory=FamilyResult)
    shapes: list[ShapeAnalysis] = field(default_factory=list)
    breast: str = Level.UNKNOWN.value
    butt: str = Level.UNKNOWN.value
    thigh: str = Level.UNKNOWN.value
    existing_3bbb: str = "NO"  # YES / PARTIAL / NO / UNKNOWN
    geometry_confidence: str = Level.UNKNOWN.value
    action: str = Action.ERROR.value
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    osp_also_provided_by: list[str] = field(default_factory=list)
    shapedata_also_provided_by: list[str] = field(default_factory=list)
    same_output_projects: list[str] = field(default_factory=list)
    superseded_by: str = ""
    includes_body_shape: bool = False
    fingerprint: str = ""
    elapsed_ms: float = 0.0
    cached: bool = False

    @property
    def key(self) -> str:
        return f"{self.osp_rel.lower()}::{self.project}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProjectResult":
        d = dict(d)
        d.pop("key", None)
        fam = FamilyResult(**d.pop("family", {}))
        shapes = []
        for s in d.pop("shapes", []):
            s = dict(s)
            dist = DistanceStats(**s.pop("distance", {}))
            regions = {k: RegionMetrics(**v) for k, v in s.pop("regions", {}).items()}
            shapes.append(ShapeAnalysis(distance=dist, regions=regions, **s))
        return cls(family=fam, shapes=shapes, **d)
