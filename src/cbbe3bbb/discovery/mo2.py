"""Mod Organizer 2 instance reader (read-only).

Understands portable instances (``ModOrganizer.ini`` next to the executable)
and the settings MO2 stores there: game path, selected profile and the
optional custom ``base_directory`` / ``mod_directory`` / ``profiles_directory``
/ ``overwrite_directory`` overrides.
"""
from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = ["Mo2Error", "ModEntry", "Mo2Instance", "load_mo2_instance", "parse_modlist", "decode_qt_value"]


class Mo2Error(Exception):
    pass


def decode_qt_value(value: str) -> str:
    """Decode a QSettings ini value (``@ByteArray(...)``, escaped backslashes, quotes)."""
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    m = re.fullmatch(r"@ByteArray\((.*)\)", v, re.S)
    if m:
        v = m.group(1)
    return v.replace("\\\\", "\\")


@dataclass
class ModEntry:
    name: str
    enabled: bool
    priority: int  # 0 = lowest priority (loaded first); higher wins conflicts
    kind: str = "mod"  # "mod", "separator", "unmanaged"

    @property
    def is_separator(self) -> bool:
        return self.kind == "separator"


@dataclass
class Mo2Instance:
    root: Path
    ini_path: Path
    game_name: str
    game_path: Optional[Path]
    selected_profile: str
    base_dir: Path
    mods_dir: Path
    profiles_dir: Path
    overwrite_dir: Path
    version: str = ""
    profiles: list[str] = field(default_factory=list)
    executables: dict[str, str] = field(default_factory=dict)  # title -> binary

    def modlist_path(self, profile: Optional[str] = None) -> Path:
        return self.profiles_dir / (profile or self.selected_profile) / "modlist.txt"

    def load_modlist(self, profile: Optional[str] = None) -> list[ModEntry]:
        p = self.modlist_path(profile)
        if not p.is_file():
            raise Mo2Error(f"modlist.txt not found for profile '{profile or self.selected_profile}'")
        return parse_modlist(p.read_text(encoding="utf-8-sig", errors="replace"))


def parse_modlist(text: str) -> list[ModEntry]:
    """Parse MO2's modlist.txt. The file lists the HIGHEST priority mod first."""
    raw: list[tuple[str, bool, str]] = []
    for line in text.splitlines():
        line = line.rstrip("\r\n")
        if not line or line.startswith("#"):
            continue
        flag, name = line[0], line[1:]
        if flag == "*":
            raw.append((name, True, "unmanaged"))
            continue
        if flag not in "+-":
            continue
        kind = "separator" if name.endswith("_separator") else "mod"
        raw.append((name, flag == "+", kind))
    n = len(raw)
    return [ModEntry(name=nm, enabled=en, priority=n - 1 - i, kind=kind) for i, (nm, en, kind) in enumerate(raw)]


def _resolve_dir(value: Optional[str], base: Path, default: Path) -> Path:
    if not value:
        return default
    v = decode_qt_value(value).replace("%BASE_DIR%", str(base))
    return Path(v)


def load_mo2_instance(root: str | Path) -> Mo2Instance:
    r = Path(root)
    ini = r / "ModOrganizer.ini"
    if not ini.is_file():
        raise Mo2Error(f"ModOrganizer.ini not found in {r} (only portable MO2 instances are auto-detected; set folders manually otherwise)")
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # type: ignore[assignment]
    try:
        cp.read_string(ini.read_text(encoding="utf-8-sig", errors="replace"))
    except configparser.Error as exc:
        raise Mo2Error(f"cannot parse ModOrganizer.ini: {exc}") from exc

    def get(section: str, key: str) -> Optional[str]:
        if cp.has_section(section) and cp.has_option(section, key):
            return cp.get(section, key)
        return None

    game_path = get("General", "gamePath")
    settings = "Settings"
    base = _resolve_dir(get(settings, "base_directory"), r, r)
    inst = Mo2Instance(
        root=r,
        ini_path=ini,
        game_name=decode_qt_value(get("General", "gameName") or ""),
        game_path=Path(decode_qt_value(game_path)) if game_path else None,
        selected_profile=decode_qt_value(get("General", "selected_profile") or "Default"),
        base_dir=base,
        mods_dir=_resolve_dir(get(settings, "mod_directory"), base, base / "mods"),
        profiles_dir=_resolve_dir(get(settings, "profiles_directory"), base, base / "profiles"),
        overwrite_dir=_resolve_dir(get(settings, "overwrite_directory"), base, base / "overwrite"),
        version=decode_qt_value(get("General", "version") or ""),
    )
    if inst.profiles_dir.is_dir():
        inst.profiles = sorted(p.name for p in inst.profiles_dir.iterdir() if (p / "modlist.txt").is_file())
    if cp.has_section("customExecutables"):
        items = dict(cp.items("customExecutables"))
        for key, val in items.items():
            m = re.fullmatch(r"(\d+)\\title", key)
            if m:
                binary = items.get(f"{m.group(1)}\\binary", "")
                inst.executables[decode_qt_value(val)] = decode_qt_value(binary)
    return inst
