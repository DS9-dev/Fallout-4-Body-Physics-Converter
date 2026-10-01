"""0.6.0 Body Architecture Foundation: body identities and body detection results.

One central vocabulary for *which body* an outfit was made for, separate from *which physics* it carries:

* machine IDs (stable, serialised): ``unknown``, ``cbbe``, ``cbbe_3bbb``, ``fusion_girl``, ``twb``, ``twb_3bbb``,
  ``bodytalk_male`` — plus ``unknown_3bbb`` (the analyzer's "3BBB (body unknown)": 3BBB physics bones on a body it
  could not name), kept because the analyzer already distinguishes it;
* display labels (never serialised as identities): "CBBE", "CBBE 3BBB", "Fusion Girl", …

:func:`detect_body` does not detect anything new: it formalises the analyzer's existing deterministic body-family
detection (``classification/family.py``: weighted 3BBB/legacy bones, embedded body shapes, slider vocabulary, names)
into a :class:`BodyDetection` with the analyzer's confidence, its evidence, and conflicting evidence (other bodies
that scored too). Classification semantics are unchanged.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

__all__ = ["UNKNOWN", "CBBE", "CBBE_3BBB", "FUSION_GIRL", "TWB", "TWB_3BBB", "BODYTALK_MALE", "UNKNOWN_3BBB",
           "AUTO", "BODY_IDS", "BODY_LABELS", "SOURCE_CHOICES", "TARGET_CHOICES", "BodyDetection", "detect_body",
           "body_label", "normalize_body_id", "BodyIdError"]

UNKNOWN = "unknown"
CBBE = "cbbe"
CBBE_3BBB = "cbbe_3bbb"
FUSION_GIRL = "fusion_girl"
TWB = "twb"
TWB_3BBB = "twb_3bbb"
BODYTALK_MALE = "bodytalk_male"
UNKNOWN_3BBB = "unknown_3bbb"
AUTO = "auto"  # a source *request* ("detect it"), never a detected body

BODY_IDS = (UNKNOWN, CBBE, CBBE_3BBB, FUSION_GIRL, TWB, TWB_3BBB, BODYTALK_MALE, UNKNOWN_3BBB)
BODY_LABELS = {
    UNKNOWN: "Unknown", CBBE: "CBBE", CBBE_3BBB: "CBBE 3BBB", FUSION_GIRL: "Fusion Girl", TWB: "TWB",
    TWB_3BBB: "TWB 3BBB", BODYTALK_MALE: "BodyTalk / Male", UNKNOWN_3BBB: "3BBB (body unknown)", AUTO: "Auto Detect",
}
# what the user may request as the source (Auto Detect is the default) / the target
SOURCE_CHOICES = (AUTO, CBBE, FUSION_GIRL, TWB)
TARGET_CHOICES = (CBBE_3BBB,)

# analyzer family label (FamilyResult.family) → body when the outfit carries no 3BBB weights / when it does
_FAMILY_TO_BODY = {"CBBE": (CBBE, CBBE_3BBB), "TWB": (TWB, TWB_3BBB), "FUSION_GIRL": (FUSION_GIRL, FUSION_GIRL),
                   "BODYTALK": (BODYTALK_MALE, BODYTALK_MALE), "MALE": (BODYTALK_MALE, BODYTALK_MALE),
                   "UNKNOWN": (UNKNOWN, UNKNOWN_3BBB), "NON_FO4": (UNKNOWN, UNKNOWN)}
_SCORE_TO_BODY = {"CBBE": CBBE, "TWB": TWB, "FUSION_GIRL": FUSION_GIRL, "BODYTALK": BODYTALK_MALE,
                  "MALE": BODYTALK_MALE}
_ALIASES = {"auto-detect": AUTO, "autodetect": AUTO, "cbbe3bbb": CBBE_3BBB, "cbbe-3bbb": CBBE_3BBB,
            "cbbe 3bbb": CBBE_3BBB, "fusiongirl": FUSION_GIRL, "fusion-girl": FUSION_GIRL, "fusion girl": FUSION_GIRL,
            "fg": FUSION_GIRL, "twb3bbb": TWB_3BBB, "twb-3bbb": TWB_3BBB, "bodytalk": BODYTALK_MALE,
            "male": BODYTALK_MALE}


class BodyIdError(ValueError):
    pass


def normalize_body_id(value: Optional[str], default: str = AUTO) -> str:
    """A body id from user input / old records ('CBBE_3BBB', 'Fusion Girl', 'auto' …); raises for unknown names."""
    if value is None or str(value).strip() == "":
        return default
    v = str(value).strip().lower()
    if v in BODY_IDS or v == AUTO:
        return v
    v2 = v.replace(" ", "_")
    if v2 in BODY_IDS:
        return v2
    if v in _ALIASES:
        return _ALIASES[v]
    for k, lb in BODY_LABELS.items():
        if lb.lower() == v:
            return k
    raise BodyIdError(f"unknown body '{value}' (known: {', '.join(SOURCE_CHOICES + TARGET_CHOICES)})")


def body_label(body_id: str) -> str:
    return BODY_LABELS.get(body_id, body_id or "Unknown")


@dataclass
class BodyDetection:
    body: str = UNKNOWN
    confidence: str = "LOW"  # the analyzer's HIGH / MEDIUM / LOW
    physics: str = "none"  # none | standard | legacy_cbbe_physics | 3bbb (the analyzer's physics finding)
    evidence: list[str] = field(default_factory=list)
    conflicting_evidence: list[str] = field(default_factory=list)
    source: str = "analyzer.family"  # where the detection came from

    @property
    def label(self) -> str:
        return body_label(self.body)

    @property
    def base_body(self) -> str:
        """The geometry body without the physics suffix (cbbe_3bbb → cbbe)."""
        return {CBBE_3BBB: CBBE, TWB_3BBB: TWB, UNKNOWN_3BBB: UNKNOWN}.get(self.body, self.body)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "BodyDetection":
        d = dict(d or {})
        return cls(**{k: d[k] for k in ("body", "confidence", "physics", "evidence", "conflicting_evidence", "source")
                      if k in d})


def detect_body(family) -> BodyDetection:
    """The body detection of one analyzed project from its ``FamilyResult`` (object or dict). Deterministic."""
    if family is None:
        return BodyDetection(evidence=["no analysis"], source="none")
    get = (lambda k, dflt=None: family.get(k, dflt)) if isinstance(family, dict) else \
        (lambda k, dflt=None: getattr(family, k, dflt))
    fam = str(get("family", "UNKNOWN") or "UNKNOWN").upper()
    physics = str(get("physics", "none") or "none")
    plain, with_3bbb = _FAMILY_TO_BODY.get(fam, (UNKNOWN, UNKNOWN_3BBB))
    body = with_3bbb if physics == "3bbb" else plain
    scores = dict(get("scores", {}) or {})
    conflicting = []
    for k, v in sorted(scores.items(), key=lambda kv: -kv[1]):
        other = _SCORE_TO_BODY.get(k)
        if other is None or other == _FAMILY_TO_BODY.get(fam, (UNKNOWN,))[0] or v < 1.0:
            continue
        if fam == "TWB" and other == CBBE:
            continue  # TWB is CBBE-derived: CBBE slider overlap is expected, not a conflict
        conflicting.append(f"{body_label(other)} evidence score {v:g}")
    ev = list(get("evidence", []) or [])
    if fam == "NON_FO4":
        ev.insert(0, str(get("label", "non-Fallout 4 NIF")))
    return BodyDetection(body=body, confidence=str(get("confidence", "LOW") or "LOW"), physics=physics, evidence=ev,
                         conflicting_evidence=conflicting)
