"""Detect a Fallout 4 / MO2 / BodySlide installation from a :class:`Profile`.

Nothing is written. Detection builds a virtual ``Data/Tools/BodySlide`` view
(game Data + enabled MO2 mods + overwrite, in MO2 priority order) and reads
BodySlide's own configuration, reference templates, presets and groups from it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..bodyslide.xmlfiles import (
    BodySlideConfig,
    RefTemplate,
    SliderGroup,
    SliderPreset,
    parse_config,
    parse_ref_templates,
    parse_slider_groups,
    parse_slider_presets,
    read_exe_version,
)
from ..profiles.store import Profile
from .mo2 import Mo2Error, Mo2Instance, ModEntry, load_mo2_instance
from .vfs import DataProvider, VirtualFS, VirtualFile, find_case_insensitive

log = logging.getLogger(__name__)

__all__ = ["Installation", "detect_installation", "choose_3bbb_template"]

BODYSLIDE_SUBTREE = "Tools/BodySlide"
OVERWRITE_PRIORITY = 10_000_000
GAME_PRIORITY = -1
DISABLED_PRIORITY_BASE = -1_000_000


@dataclass
class Installation:
    profile: Profile
    game_dir: Optional[Path] = None
    game_data_dir: Optional[Path] = None
    game_exe_found: bool = False
    mo2: Optional[Mo2Instance] = None
    mo2_profile: str = ""
    mods: list[ModEntry] = field(default_factory=list)
    providers: list[DataProvider] = field(default_factory=list)
    vfs: Optional[VirtualFS] = None
    bodyslide_exe: Optional[VirtualFile] = None
    outfitstudio_exe: Optional[VirtualFile] = None
    bodyslide_version: Optional[str] = None
    outfitstudio_version: Optional[str] = None
    bodyslide_config: Optional[BodySlideConfig] = None
    bodyslide_ui: dict[str, str] = field(default_factory=dict)
    ref_templates: list[RefTemplate] = field(default_factory=list)
    presets: list[SliderPreset] = field(default_factory=list)
    groups: list[SliderGroup] = field(default_factory=list)
    reference_template: Optional[RefTemplate] = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and self.vfs is not None

    @property
    def mods_dir(self) -> Optional[Path]:
        if self.profile.mods_dir:
            return Path(self.profile.mods_dir)
        return self.mo2.mods_dir if self.mo2 else None

    @property
    def automation_supported(self) -> Optional[bool]:
        """Outfit Studio gained a headless automation mode in v5.8.0."""
        v = self.outfitstudio_version or self.bodyslide_version
        if not v:
            return None
        try:
            parts = tuple(int(x) for x in v.split(".")[:3])
        except ValueError:
            return None
        return parts >= (5, 8, 0)

    def summary(self) -> dict:
        return {
            "profile": self.profile.name,
            "game_dir": str(self.game_dir) if self.game_dir else None,
            "game_exe_found": self.game_exe_found,
            "mo2_dir": str(self.mo2.root) if self.mo2 else None,
            "mo2_version": self.mo2.version if self.mo2 else None,
            "mo2_profile": self.mo2_profile or None,
            "mods_dir": str(self.mods_dir) if self.mods_dir else None,
            "mods_total": len([m for m in self.mods if not m.is_separator and m.kind != "unmanaged"]),
            "mods_enabled": len([m for m in self.mods if m.enabled and m.kind == "mod"]),
            "data_providers": len(self.providers),
            "bodyslide_dir": str(self.bodyslide_exe.abs_path.parent) if self.bodyslide_exe else None,
            "bodyslide_provider": self.bodyslide_exe.provider.name if self.bodyslide_exe else None,
            "bodyslide_version": self.bodyslide_version,
            "outfitstudio_version": self.outfitstudio_version,
            "outfitstudio_headless_automation": self.automation_supported,
            "bodyslide_target_game": self.bodyslide_config.target_game_name if self.bodyslide_config else None,
            "bodyslide_selected_preset": self.bodyslide_ui.get("SelectedPreset"),
            "ref_templates": [t.display_name for t in self.ref_templates],
            "reference_template": self.reference_template.display_name if self.reference_template else None,
            "presets": len(self.presets),
            "groups": len(self.groups),
            "vfs_files": len(self.vfs.files) if self.vfs else 0,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def choose_3bbb_template(templates: list[RefTemplate], preferred: str = "") -> Optional[RefTemplate]:
    """Pick the CBBE 3BBB reference template (full body, not "Reduced"/"Unique")."""
    if preferred:
        for t in templates:
            if t.display_name == preferred or t.set_name == preferred:
                return t
    scored = []
    for t in templates:
        text = f"{t.display_name} {t.set_name}".lower()
        if "3bbb" not in text or "cbbe" not in text:
            continue
        score = 0
        if t.set_name.lower() == "cbbe 3bbb body":
            score += 10
        if "reduced" in text:
            score -= 5
        if "unique" in text:
            score -= 3
        scored.append((score, t))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _game_providers(game_data: Optional[Path]) -> list[DataProvider]:
    return [DataProvider("<game data>", game_data, GAME_PRIORITY, "game")] if game_data else []


def detect_installation(profile: Profile, cancel: Optional[Callable[[], bool]] = None) -> Installation:
    inst = Installation(profile=profile)

    # --- game --------------------------------------------------------------
    if profile.game_dir:
        gd = Path(profile.game_dir)
        if gd.is_dir():
            inst.game_dir = gd
            inst.game_exe_found = find_case_insensitive(gd, "Fallout4.exe") is not None
            inst.game_data_dir = find_case_insensitive(gd, "Data")
            if not inst.game_exe_found:
                inst.warnings.append("Fallout4.exe not found in the game folder")
            if inst.game_data_dir is None:
                inst.warnings.append("game Data folder not found")
        else:
            inst.errors.append(f"game folder does not exist: {gd}")

    # --- MO2 ---------------------------------------------------------------
    providers: list[DataProvider] = []
    if profile.mo2_dir:
        try:
            inst.mo2 = load_mo2_instance(profile.mo2_dir)
        except Mo2Error as exc:
            inst.errors.append(str(exc))
        if inst.mo2:
            if inst.game_dir is None and inst.mo2.game_path and inst.mo2.game_path.is_dir():
                inst.game_dir = inst.mo2.game_path
                inst.game_exe_found = find_case_insensitive(inst.game_dir, "Fallout4.exe") is not None
                inst.game_data_dir = find_case_insensitive(inst.game_dir, "Data")
                inst.warnings.append("game folder taken from ModOrganizer.ini")
            if "fallout 4" not in inst.mo2.game_name.lower() and inst.mo2.game_name:
                inst.warnings.append(f"MO2 instance manages '{inst.mo2.game_name}', not Fallout 4")
            inst.mo2_profile = profile.mo2_profile or inst.mo2.selected_profile
            try:
                inst.mods = inst.mo2.load_modlist(inst.mo2_profile)
            except Mo2Error as exc:
                inst.errors.append(str(exc))
            mods_dir = inst.mods_dir
            if mods_dir and not mods_dir.is_dir():
                inst.errors.append(f"mods folder does not exist: {mods_dir}")
            elif mods_dir:
                for m in inst.mods:
                    if m.kind != "mod":
                        continue
                    if not m.enabled and not profile.analysis.include_disabled_mods:
                        continue
                    root = mods_dir / m.name
                    if root.is_dir():
                        if m.enabled:
                            providers.append(DataProvider(m.name, root, m.priority, "mod"))
                        else:
                            # Disabled mods are invisible to BodySlide; rank them below every
                            # real provider so they never change what enabled mods resolve to.
                            providers.append(DataProvider(m.name, root, DISABLED_PRIORITY_BASE + m.priority, "disabled-mod"))
                if inst.mo2.overwrite_dir.is_dir():
                    providers.append(DataProvider("<overwrite>", inst.mo2.overwrite_dir, OVERWRITE_PRIORITY, "overwrite"))
    providers.extend(_game_providers(inst.game_data_dir))

    # Explicit/extra BodySlide roots: each is a folder that *is* a BodySlide folder.
    extra_vfs_roots = []
    if profile.bodyslide_dir:
        extra_vfs_roots.append(Path(profile.bodyslide_dir))
    extra_vfs_roots.extend(Path(p) for p in profile.extra_bodyslide_roots)
    for i, root in enumerate(extra_vfs_roots):
        if not root.is_dir():
            inst.warnings.append(f"BodySlide folder does not exist: {root}")
            continue
        # A provider's root corresponds to Data/, so point it two levels up when the
        # user selected ".../Tools/BodySlide"; otherwise wrap it in a synthetic root.
        if root.name.lower() == "bodyslide" and root.parent.name.lower() == "tools":
            providers.append(DataProvider(f"<extra {i + 1}: {root}>", root.parent.parent, OVERWRITE_PRIORITY + 1 + i, "extra"))
        else:
            inst.warnings.append(f"extra BodySlide root must be a '...\\Tools\\BodySlide' folder: {root}")

    inst.providers = providers
    if not providers:
        inst.errors.append("no data folders to scan: set the game folder and/or the MO2 folder")
        return inst

    inst.vfs = VirtualFS.build(providers, BODYSLIDE_SUBTREE, cancel=cancel)
    for e in inst.vfs.scan_errors[:20]:
        inst.warnings.append(f"scan: {e}")

    # --- BodySlide program + config -----------------------------------------
    vfs = inst.vfs
    inst.bodyslide_exe = vfs.resolve("BodySlide x64.exe") or vfs.resolve("BodySlide.exe")
    inst.outfitstudio_exe = vfs.resolve("OutfitStudio x64.exe") or vfs.resolve("OutfitStudio.exe")
    if inst.bodyslide_exe is None:
        inst.warnings.append("BodySlide executable not found under Data/Tools/BodySlide")
    else:
        inst.bodyslide_version = read_exe_version(inst.bodyslide_exe.abs_path)
    if inst.outfitstudio_exe is not None:
        inst.outfitstudio_version = read_exe_version(inst.outfitstudio_exe.abs_path)
    cfg = vfs.resolve("Config.xml")
    if cfg:
        try:
            inst.bodyslide_config = parse_config(cfg.abs_path)
            if inst.bodyslide_config.target_game not in (3, 5):
                inst.warnings.append(f"BodySlide target game is {inst.bodyslide_config.target_game_name}, not Fallout 4")
        except Exception as exc:  # noqa: BLE001 - config is informational
            inst.warnings.append(f"cannot read BodySlide Config.xml: {exc}")
    ui = vfs.resolve("BodySlide.xml")
    if ui:
        try:
            from ..bodyslide.xmlfiles import parse_bodyslide_ui_config

            inst.bodyslide_ui = parse_bodyslide_ui_config(ui.abs_path)
        except Exception as exc:  # noqa: BLE001
            inst.warnings.append(f"cannot read BodySlide.xml: {exc}")

    for vf in list(vfs.iter_winners("", (".xml",))):
        key = vf.key
        try:
            if key == "reftemplates.xml" or key.startswith("reftemplates/"):
                inst.ref_templates.extend(parse_ref_templates(vf.abs_path))
            elif key.startswith("sliderpresets/"):
                inst.presets.extend(parse_slider_presets(vf.abs_path))
            elif key.startswith("slidergroups/"):
                inst.groups.extend(parse_slider_groups(vf.abs_path))
        except Exception as exc:  # noqa: BLE001 - tolerate broken side files
            inst.warnings.append(f"cannot parse {vf.rel} ({vf.provider.name}): {exc}")

    inst.reference_template = choose_3bbb_template(inst.ref_templates, profile.analysis.reference_template)
    if inst.reference_template is None:
        inst.warnings.append("no CBBE 3BBB reference template found; geometry analysis will be limited")
    log.info("detected installation: %s", {k: v for k, v in inst.summary().items() if k not in ("ref_templates", "warnings")})
    return inst
