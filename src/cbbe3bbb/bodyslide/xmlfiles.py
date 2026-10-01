"""Readers for the small BodySlide XML side files and executable metadata.

* ``RefTemplates.xml`` / ``RefTemplates/*.xml`` - Outfit Studio reference templates
* ``SliderGroups/*.xml`` - group membership of slider sets
* ``SliderPresets/*.xml`` - body presets (e.g. "CBBE Curvy")
* ``Config.xml`` / ``BodySlide.xml`` - program settings
* PE version resource of ``BodySlide x64.exe`` / ``OutfitStudio x64.exe``
"""
from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "RefTemplate",
    "SliderGroup",
    "SliderPreset",
    "BodySlideConfig",
    "parse_ref_templates",
    "parse_slider_groups",
    "parse_slider_presets",
    "parse_config",
    "parse_bodyslide_ui_config",
    "read_exe_version",
]


def _load_xml(path: Path) -> ET.Element:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return ET.fromstring(data)


@dataclass
class RefTemplate:
    display_name: str
    source_file: str  # relative to the BodySlide folder, e.g. "SliderSets\\CBBE 3BBB Body.osp"
    set_name: str
    shape: str
    defined_in: Optional[Path] = None


@dataclass
class SliderGroup:
    name: str
    members: list[str]
    defined_in: Optional[Path] = None


@dataclass
class SliderPreset:
    name: str
    set_name: str
    groups: list[str]
    values: dict[str, dict[str, float]] = field(default_factory=dict)  # slider -> {"small": v, "big": v}
    defined_in: Optional[Path] = None


@dataclass
class BodySlideConfig:
    target_game: Optional[int] = None
    game_data_path: str = ""
    output_data_path: str = ""
    project_path: str = ""
    default_skeleton: str = ""
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def target_game_name(self) -> str:
        names = {0: "Fallout 3", 1: "Fallout New Vegas", 2: "Skyrim", 3: "Fallout 4", 4: "Skyrim SE", 5: "Fallout 4 VR", 6: "Skyrim VR", 7: "Oblivion", 8: "Starfield"}
        return names.get(self.target_game, "unknown") if self.target_game is not None else "unknown"


def parse_ref_templates(path: Path) -> list[RefTemplate]:
    root = _load_xml(path)
    out = []
    for t in root.iter("Template"):
        out.append(
            RefTemplate(
                display_name=(t.text or "").strip(),
                source_file=t.get("sourcefile") or "",
                set_name=t.get("set") or "",
                shape=t.get("shape") or "",
                defined_in=path,
            )
        )
    return out


def parse_slider_groups(path: Path) -> list[SliderGroup]:
    root = _load_xml(path)
    out = []
    for g in root.iter("Group"):
        out.append(SliderGroup(name=g.get("name") or "", members=[m.get("name") or "" for m in g.findall("Member")], defined_in=path))
    return out


def parse_slider_presets(path: Path) -> list[SliderPreset]:
    root = _load_xml(path)
    out = []
    for p in root.iter("Preset"):
        preset = SliderPreset(name=p.get("name") or "", set_name=p.get("set") or "", groups=[g.get("name") or "" for g in p.findall("Group")], defined_in=path)
        for s in p.findall("SetSlider"):
            try:
                val = float(s.get("value") or 0)
            except ValueError:
                continue
            preset.values.setdefault(s.get("name") or "", {})[s.get("size") or "big"] = val
        out.append(preset)
    return out


def parse_config(path: Path) -> BodySlideConfig:
    root = _load_xml(path)
    cfg = BodySlideConfig()
    for child in root:
        if len(child) == 0:
            cfg.raw[child.tag] = (child.text or "").strip()
    tg = cfg.raw.get("TargetGame")
    cfg.target_game = int(tg) if tg and tg.lstrip("-").isdigit() else None
    cfg.game_data_path = cfg.raw.get("GameDataPath", "")
    cfg.output_data_path = cfg.raw.get("OutputDataPath", "")
    cfg.project_path = cfg.raw.get("ProjectPath", "")
    anim = root.find("Anim")
    if anim is not None:
        cfg.default_skeleton = (anim.findtext("DefaultSkeletonReference") or "").strip()
    return cfg


def parse_bodyslide_ui_config(path: Path) -> dict[str, str]:
    root = _load_xml(path)
    return {c.tag: (c.text or "").strip() for c in root if len(c) == 0}


def read_exe_version(path: Path) -> Optional[str]:
    """Return the FileVersion from a PE file's VS_FIXEDFILEINFO, or None."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    sig = struct.pack("<I", 0xFEEF04BD)
    pos = data.rfind(sig)
    while pos >= 0:
        if pos + 52 <= len(data):
            (_sig, _struc, ms, ls) = struct.unpack_from("<IIII", data, pos)
            if _struc in (0x00010000, 0x00000000):
                return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        pos = data.rfind(sig, 0, pos)
    return None
