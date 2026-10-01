"""Installation profiles and conversion settings, stored as local JSON.

No registry access. The default location is ``<app>/config/profiles.json``
(portable); set ``CBBE3BBB_CONFIG_DIR`` to override.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

__all__ = ["ConversionSettings", "AnalysisSettings", "Profile", "ProfileStore", "app_root", "default_config_dir",
           "migrate_tool_root", "LEGACY_ROOT_NAMES"]


def app_root() -> Path:
    env = os.environ.get("CBBE3BBB_HOME")
    if env:
        return Path(env)
    # src/cbbe3bbb/profiles/store.py -> repository root
    return Path(__file__).resolve().parents[3]


def default_config_dir() -> Path:
    env = os.environ.get("CBBE3BBB_CONFIG_DIR")
    return Path(env) if env else app_root() / "config"


@dataclass
class ConversionSettings:
    """Weight-transfer settings (used from Phase 2 on; stored now so they are profile-bound)."""

    search_radius: float = 5.0  # Outfit Studio "Search Radius"
    max_vertex_targets: int = 4  # Outfit Studio "Max Vertex Targets"
    no_target_limit: bool = False  # Outfit Studio "No Target Limit"
    copy_reference_shape_into_output: bool = False
    build_morphs: bool = True
    preset: str = "CBBE Curvy"
    output_mod_name: str = "CBBE 3BBB Auto Conversions"
    project_suffix: str = " - 3BBB AUTO"
    # Phase 2
    output_mod_path: str = ""  # empty = <MO2 mods>/CBBE 3BBB Auto Conversions
    outfitstudio_dir: str = ""  # a separate Outfit Studio 5.8+ folder (never the MO2 BodySlide mod)
    weight_mode: str = "auto"  # auto | full | regions
    # Milestone gate: only these source projects may be converted (empty list = no gate)
    allowed_projects: list[str] = field(default_factory=lambda: ["MM69 Raider Punk Top - CBBE"])
    automation_timeout_s: int = 600
    # how Outfit Studio runs the script: auto | headless | interactive (window open, user clicks Execute)
    os_run_mode: str = "auto"
    interactive_timeout_s: int = 1800
    # Phase 3 (package/batch): "single" = one Outfit Studio run executes one script that converts every
    # selected project in turn (one Execute); "per_project" = one Outfit Studio run per project.
    batch_session_mode: str = "single"
    # Phase 3.2 (mass conversion): at most this many projects per Outfit Studio session (0 = no limit). Long sessions
    # are split so a crash or a closed window costs at most one session; cloth projects still go last, one per session.
    max_projects_per_session: int = 25
    # a session that failed as a whole (crash, timeout, window closed without Execute) is retried this many times for
    # the projects it did not finish, each time in a fresh Outfit Studio session
    session_retries: int = 1
    # 0.5.3: who presses the buttons in the Outfit Studio window: "auto" (unattended where the converter can drive
    # Outfit Studio's window itself, i.e. on Windows; otherwise manual), "unattended" or "manual" (the user runs the
    # script: File > Open Automation > Execute)
    os_control: str = "auto"
    # 0.5.3: an unattended session that shows no progress (no new step, no saved project, no dialog) for this long stops
    unattended_stall_timeout_s: int = 900
    unattended_ready_timeout_s: int = 180  # Outfit Studio window + 3D view ready after the start
    unattended_close_timeout_s: int = 60  # graceful close before Outfit Studio is ended


@dataclass
class AnalysisSettings:
    reference_template: str = ""  # empty = auto-detect the CBBE 3BBB reference
    include_disabled_mods: bool = False
    use_cache: bool = True
    # A reference body vertex is "physics weighted" for a region above this weight.
    region_weight_threshold: float = 0.05


@dataclass
class Profile:
    name: str
    game_dir: str = ""
    mo2_dir: str = ""
    mo2_profile: str = ""  # empty = the instance's selected profile
    mods_dir: str = ""  # empty = auto from ModOrganizer.ini
    bodyslide_dir: str = ""  # empty = auto (Data/Tools/BodySlide through MO2)
    extra_bodyslide_roots: list[str] = field(default_factory=list)
    output_mod_enabled: bool = False  # Phase 1: always False
    conversion: ConversionSettings = field(default_factory=ConversionSettings)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Profile":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known and k not in ("conversion", "analysis")}
        prof = cls(**kwargs)
        conv = d.get("conversion") or {}
        prof.conversion = ConversionSettings(**{k: v for k, v in conv.items() if k in {f.name for f in fields(ConversionSettings)}})
        ana = d.get("analysis") or {}
        prof.analysis = AnalysisSettings(**{k: v for k, v in ana.items() if k in {f.name for f in fields(AnalysisSettings)}})
        prof.output_mod_enabled = False  # enforced during Phase 1
        return prof

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


LEGACY_ROOT_NAMES = ("CBBE3BBBConverter",)  # 0.6.1: the folder was renamed to "Fallout 4 Body & Physics Converter"
_PATH_FIELDS = ("game_dir", "mo2_dir", "mods_dir", "bodyslide_dir")
_CONV_PATH_FIELDS = ("outfitstudio_dir", "output_mod_path")


def _under(path: str, root: Path) -> Optional[Path]:
    """``path`` relative to ``root`` when it lies inside it (Windows-style case-insensitive), else None."""
    a = str(path).replace("/", "\\").rstrip("\\")
    b = str(root).replace("/", "\\").rstrip("\\")
    if a.lower() == b.lower():
        return Path(".")
    if a.lower().startswith(b.lower() + "\\"):
        return Path(*a[len(b) + 1:].split("\\"))
    return None


def migrate_tool_root(profiles, current_root: Path, stored_root: Optional[str] = None) -> list[str]:
    """0.6.1: paths saved inside a FORMER application folder (the stored ``tool_root``, or a sibling folder with a legacy
    name such as ``CBBE3BBBConverter``) are moved to the current folder — only when the old path no longer exists and
    the same relative path exists under the current folder. Other user paths are never touched. Returns the changes."""
    current_root = Path(current_root)
    former = []
    if stored_root and Path(stored_root) != current_root:
        former.append(Path(stored_root))
    for name in LEGACY_ROOT_NAMES:
        cand = current_root.parent / name
        if current_root.name != name and cand not in former:
            former.append(cand)
    changes: list[str] = []

    def fix(value: str) -> str:
        if not value or Path(value).exists():
            return value
        for old in former:
            rel = _under(value, old)
            if rel is not None and (current_root / rel).exists():
                new = str(current_root / rel)
                changes.append(f"{value} -> {new}")
                return new
        return value

    for p in profiles:
        for f in _PATH_FIELDS:
            setattr(p, f, fix(getattr(p, f)))
        p.extra_bodyslide_roots = [fix(x) for x in p.extra_bodyslide_roots]
        for f in _CONV_PATH_FIELDS:
            setattr(p.conversion, f, fix(getattr(p.conversion, f)))
    return changes


class ProfileStore:
    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = Path(config_dir) if config_dir else default_config_dir()
        self.path = self.config_dir / "profiles.json"
        self.profiles: dict[str, Profile] = {}
        self.last_profile: str = ""
        self.migrated: list[str] = []
        self.load()

    def load(self) -> None:
        self.profiles = {}
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            backup = self.path.with_suffix(".json.corrupt")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return
        for item in data.get("profiles", []):
            try:
                p = Profile.from_dict(item)
            except TypeError:
                continue
            self.profiles[p.name] = p
        self.last_profile = data.get("last_profile", "")
        self.migrated = migrate_tool_root(self.profiles.values(), self.tool_root, data.get("tool_root"))
        if self.migrated:
            try:
                self.save()
            except OSError:
                pass

    @property
    def tool_root(self) -> Path:
        """The application folder this store belongs to (``<root>/config/profiles.json``)."""
        return self.config_dir.parent

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {"schema": 1, "last_profile": self.last_profile, "tool_root": str(self.tool_root),
                   "profiles": [p.to_dict() for p in self.profiles.values()]}
        fd, tmp = tempfile.mkstemp(dir=self.config_dir, prefix=".profiles-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def upsert(self, profile: Profile) -> None:
        self.profiles[profile.name] = profile
        self.last_profile = profile.name
        self.save()

    def delete(self, name: str) -> None:
        self.profiles.pop(name, None)
        if self.last_profile == name:
            self.last_profile = next(iter(self.profiles), "")
        self.save()

    def get(self, name: str) -> Optional[Profile]:
        return self.profiles.get(name)
