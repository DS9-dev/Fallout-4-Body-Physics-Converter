"""Outfit Studio runtime detection and the tool-private runtime copy.

Phase 2 conversions run Outfit Studio's own headless automation
(``OutfitStudio -a <script>``, available from v5.8.0). The user's working
BodySlide/Outfit Studio installation (5.7.1 in MO2) is never modified or
replaced. Instead the user points the tool at a *separate* 5.8+ folder (for
example the extracted release download), and the tool copies the program files
(no project data) into ``<tool>/runtime/outfitstudio-<version>`` and gives that
copy its own ``Config.xml``. The copy is private to this tool, and each
conversion writes its own ``ProjectPath`` into it.

No BodySlide/Outfit Studio binaries are shipped with the tool.
"""
from __future__ import annotations

import json
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..bodyslide.xmlfiles import read_exe_version

__all__ = [
    "MIN_AUTOMATION_VERSION",
    "EXE_NAMES",
    "AUTOMATION_MARKERS",
    "RuntimeInfo",
    "RuntimeError_",
    "find_outfitstudio_exe",
    "parse_version",
    "detect_runtime",
    "prepare_private_runtime",
    "write_runtime_config",
    "DATA_DIRS",
]

MIN_AUTOMATION_VERSION = (5, 8, 0)
# Windows release names first; "OutfitStudio" is the Linux/CI build name.
EXE_NAMES = ("OutfitStudio x64.exe", "OutfitStudio.exe", "OutfitStudio")
# Strings compiled into Outfit Studio >= 5.8 for the "-a/--automation" option.
AUTOMATION_MARKERS = (b"Run an automation script by name", b"Automation: Running script")
# Folders that hold project data in a BodySlide install; never copied into the private runtime.
DATA_DIRS = {"slidersets", "shapedata", "automations", "posedata", "reftemplates", "slidercategories", "slidergroups", "sliderpresets", "backups"}
MARKER_FILE = "cbbe3bbb-runtime.json"
# Outfit Studio 5.8.0-5.8.2: in headless mode (-a) the main window is never shown, so the OpenGL
# panel never initializes and Outfit Studio never creates its per-shape meshes. CopyBoneWeights then
# computes the weights, but they are only applied through those meshes (ActiveShapesUpdated), so the
# step silently does nothing. Found by running 5.8.2 in this project's sandbox and confirmed in the
# source. Newer versions are treated as "unverified": the post-conversion validation catches a no-op.
HEADLESS_WEIGHT_COPY_BROKEN_UPTO = (5, 8, 2)


class RuntimeError_(RuntimeError):
    pass


@dataclass
class RuntimeInfo:
    path: str = ""  # executable
    folder: str = ""
    version: Optional[str] = None
    version_source: str = ""  # "pe" | "marker" | ""
    automation_marker_found: bool = False
    headless_supported: bool = False
    reason: str = ""
    private_copy: bool = False
    # None = unknown/unverified; False = known broken (weight copy is a no-op in headless mode)
    headless_weight_copy_ok: Optional[bool] = None
    recommended_run_mode: str = ""  # "headless" | "interactive"
    warnings: list[str] = field(default_factory=list)

    @property
    def headless_label(self) -> str:
        return "YES" if self.headless_supported else "NO"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["headless"] = self.headless_label
        return d


def parse_version(v: Optional[str]) -> Optional[tuple[int, ...]]:
    if not v:
        return None
    try:
        return tuple(int(x) for x in v.strip().split(".")[:3])
    except ValueError:
        return None


def find_outfitstudio_exe(folder: Path) -> Optional[Path]:
    folder = Path(folder)
    if folder.is_file():
        return folder
    for name in EXE_NAMES:
        p = folder / name
        if p.is_file():
            return p
    return None


def _scan_markers(exe: Path) -> bool:
    try:
        data = exe.read_bytes()
    except OSError:
        return False
    return any(m in data or m.decode().encode("utf-16-le") in data for m in AUTOMATION_MARKERS)


def detect_runtime(path: str | Path) -> RuntimeInfo:
    """Detect version and headless-automation support of an Outfit Studio folder or executable."""
    info = RuntimeInfo()
    if not path:
        info.reason = "no Outfit Studio 5.8+ folder configured"
        return info
    p = Path(path)
    exe = find_outfitstudio_exe(p)
    if exe is None:
        info.folder = str(p)
        info.reason = f"no Outfit Studio executable ({' / '.join(EXE_NAMES)}) in {p}"
        return info
    info.path = str(exe)
    info.folder = str(exe.parent)
    info.version = read_exe_version(exe)
    if info.version:
        info.version_source = "pe"
    marker = exe.parent / MARKER_FILE
    if marker.is_file():
        try:
            m = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            m = {}
        info.private_copy = bool(m.get("private_copy"))
        if not info.version and m.get("version"):
            info.version = str(m["version"])
            info.version_source = "marker"
    info.automation_marker_found = _scan_markers(exe)
    ver = parse_version(info.version)
    if ver is None:
        info.reason = "cannot determine the Outfit Studio version"
    elif ver < MIN_AUTOMATION_VERSION:
        info.reason = f"Outfit Studio {info.version} has no headless automation (needs 5.8.0 or newer)"
    elif not info.automation_marker_found:
        info.reason = "the executable does not contain the -a/--automation command-line option"
    else:
        info.headless_supported = True
        info.reason = "headless automation available (OutfitStudio -a <script>)"
    if ver is not None and ver >= MIN_AUTOMATION_VERSION:
        if ver <= HEADLESS_WEIGHT_COPY_BROKEN_UPTO:
            info.headless_weight_copy_ok = False
            info.recommended_run_mode = "interactive"
            info.warnings.append(
                f"Outfit Studio {info.version}: CopyBoneWeights does nothing in headless mode (the 3D view is never "
                "initialized). The script is run with Outfit Studio's window open instead: File > Open Automation > Execute."
            )
        else:
            info.recommended_run_mode = "headless"
    if (exe.parent / "SliderSets").is_dir() and not info.private_copy:
        info.warnings.append(
            "this folder contains SliderSets: Outfit Studio would use it as its project folder; "
            "the tool always runs a private copy without project data"
        )
    return info


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def prepare_private_runtime(source: str | Path, runtime_root: Path, protected: list[Path] = ()) -> RuntimeInfo:
    """Copy an Outfit Studio 5.8+ install (program files only) into the tool's runtime folder.

    Existing copies of the same version are reused. The source folder is only read.
    """
    src_info = detect_runtime(source)
    if not src_info.headless_supported:
        raise RuntimeError_(f"selected Outfit Studio cannot be used: {src_info.reason}")
    src = Path(src_info.folder)
    for p in protected:
        if p and _is_within(Path(runtime_root), Path(p)):
            raise RuntimeError_(f"runtime folder {runtime_root} is inside {p}")
    dest = Path(runtime_root) / f"outfitstudio-{src_info.version}"
    marker = dest / MARKER_FILE
    if marker.is_file() and find_outfitstudio_exe(dest):
        info = detect_runtime(dest)
        if info.headless_supported:
            return info
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    for entry in src.iterdir():
        if entry.name.lower() in DATA_DIRS or entry.name.lower() in ("config.xml", "outfitstudio.xml", "log_os.txt", "log_bs.txt"):
            continue
        if entry.is_dir():
            shutil.copytree(entry, tmp / entry.name)
        else:
            shutil.copy2(entry, tmp / entry.name)
    base_cfg = src / "Config.xml"
    if base_cfg.is_file():
        shutil.copy2(base_cfg, tmp / "Config.base.xml")
    (tmp / MARKER_FILE).write_text(
        json.dumps({"private_copy": True, "version": src_info.version, "source": str(src)}, indent=1), encoding="utf-8"
    )
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(tmp, dest)
    info = detect_runtime(dest)
    if not info.headless_supported:
        raise RuntimeError_(f"private runtime copy is not usable: {info.reason}")
    return info


def write_runtime_config(runtime_folder: Path, project_path: Path, game_data_path: Path, log_level: int = 4) -> Path:
    """Write the private runtime's Config.xml for one conversion run.

    ``TargetGame`` 3 = Fallout 4. ``GameDataPath`` points to an empty workspace
    folder, so Outfit Studio never scans the real game Data or its archives.
    """
    runtime_folder = Path(runtime_folder)
    if not (runtime_folder / MARKER_FILE).is_file():
        raise RuntimeError_("refusing to write Config.xml into a folder that is not the tool's private runtime copy")
    base = runtime_folder / "Config.base.xml"
    root = None
    if base.is_file():
        try:
            root = ET.parse(base).getroot()
        except ET.ParseError:
            root = None
    if root is None or root.tag != "Config":
        root = ET.Element("Config")

    def setv(tag: str, value: str) -> None:
        el = root.find(tag)
        if el is None:
            el = ET.SubElement(root, tag)
        el.text = value

    setv("TargetGame", "3")
    setv("GameDataPath", str(Path(game_data_path)) + os.sep)
    setv("ProjectPath", str(Path(project_path)))
    setv("WarnMissingGamePath", "false")
    setv("BSATextureScan", "false")
    setv("LogLevel", str(log_level))
    # 0.5.3: the unattended control identifies Outfit Studio's windows by their English titles and uses the standard
    # (light) native controls: Language -1 = English (OutfitStudio::InitLanguage), AppearanceMode Light (5.8.2 MSW)
    setv("Language", "-1")
    setv("UseSystemLanguage", "false")
    setv("AppearanceMode", "Light")
    anim = root.find("Anim")
    if anim is None:
        anim = ET.SubElement(root, "Anim")
    # the values Outfit Studio's own setup writes for Fallout 4
    for tag, value in (("DefaultSkeletonReference", "res/skeleton_fo4.nif"), ("SkeletonRootName", "Root")):
        el = anim.find(tag)
        if el is None:
            el = ET.SubElement(anim, tag)
        if not (el.text or "").strip():
            el.text = value
    out = runtime_folder / "Config.xml"
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    return out
