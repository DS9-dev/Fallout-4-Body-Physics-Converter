"""Parser for BodySlide slider set project files (``.osp``).

An OSP file contains one ``<SliderSetInfo>`` root with one or more
``<SliderSet>`` elements. The element/attribute names mirror what BodySlide
and Outfit Studio write (see docs/FILE_FORMATS.md). Parsing is read-only and
tolerant: a malformed file raises :class:`OspParseError` with a readable
message, a malformed individual slider set is reported and skipped.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Optional

__all__ = [
    "OspParseError",
    "SliderData",
    "Slider",
    "ShapeEntry",
    "SliderSet",
    "OspFile",
    "parse_osp",
    "parse_osp_bytes",
]


class OspParseError(Exception):
    pass


def _bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes")


def _norm_rel(p: str) -> str:
    """Normalise a Windows-style relative path to forward slashes."""
    return str(PureWindowsPath(p.strip())).replace("\\", "/") if p else ""


@dataclass
class SliderData:
    name: str
    target: str
    local: bool
    data_file: str  # e.g. "Outfit.osd\\ShapeSlider" or "Slider.bsd"


@dataclass
class Slider:
    name: str
    invert: bool = False
    default_small: float = 0.0
    default_big: float = 0.0
    zap: bool = False
    uv: bool = False
    hidden: bool = False
    clamp: bool = False
    zap_toggles: list[str] = field(default_factory=list)
    data: list[SliderData] = field(default_factory=list)

    def targets(self) -> set[str]:
        return {d.target for d in self.data}


@dataclass
class ShapeEntry:
    name: str  # shape name inside the ShapeData NIF
    target: str  # slider data target name
    data_folder: Optional[str] = None  # per-shape DataFolder attribute (reference shapes)
    lock_normals: bool = False


@dataclass
class SliderSet:
    name: str
    data_folder: str
    source_file: str
    output_path: str
    output_file: str
    gen_weights: bool = True
    prevent_morph_file: bool = False
    keep_zapped_shapes: bool = False
    shapes: list[ShapeEntry] = field(default_factory=list)
    sliders: list[Slider] = field(default_factory=list)
    extra_data_folders: list[str] = field(default_factory=list)

    @property
    def slider_names(self) -> list[str]:
        return [s.name for s in self.sliders]

    @property
    def zap_sliders(self) -> list[str]:
        return [s.name for s in self.sliders if s.zap]

    @property
    def output_game_path(self) -> str:
        """``meshes/foo/bar`` style relative output path (no extension)."""
        path = _norm_rel(self.output_path)
        return f"{path}/{self.output_file}" if path else self.output_file

    def shape_data_folder(self, shape: ShapeEntry) -> str:
        return _norm_rel(shape.data_folder) if shape.data_folder else _norm_rel(self.data_folder)

    def sliders_for_target(self, target: str) -> list[Slider]:
        return [s for s in self.sliders if target in s.targets()]


@dataclass
class OspFile:
    path: Optional[Path]
    version: Optional[int]
    sets: list[SliderSet]
    errors: list[str]


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parse_set(el: ET.Element) -> SliderSet:
    name = el.get("name")
    if not name:
        raise OspParseError("SliderSet without a name attribute")
    folders = [_text(f) for f in el.findall("DataFolder")]
    out_el = el.find("OutputFile")
    ss = SliderSet(
        name=name,
        data_folder=folders[0] if folders else "",
        extra_data_folders=folders[1:],
        source_file=_text(el.find("SourceFile")),
        output_path=_text(el.find("OutputPath")),
        output_file=_text(out_el),
        gen_weights=_bool(out_el.get("GenWeights") if out_el is not None else None, True),
        prevent_morph_file=_bool(out_el.get("PreventMorphFile") if out_el is not None else None),
        keep_zapped_shapes=_bool(out_el.get("KeepZappedShapes") if out_el is not None else None),
    )
    for sh in el.findall("Shape"):
        shape_name = _text(sh)
        if not shape_name:
            continue
        ss.shapes.append(
            ShapeEntry(
                name=shape_name,
                target=sh.get("target") or shape_name,
                data_folder=sh.get("DataFolder"),
                lock_normals=_bool(sh.get("LockNormals")),
            )
        )
    for sl in el.findall("Slider"):
        sname = sl.get("name")
        if sname is None:
            continue

        def num(attr: str) -> Optional[float]:
            v = sl.get(attr)
            try:
                return float(v) if v is not None else None
            except ValueError:
                return None

        big = num("big")
        small = num("small")
        default = num("default")
        slider = Slider(
            name=sname,
            invert=_bool(sl.get("invert")),
            default_small=small if small is not None else (default or 0.0),
            default_big=big if big is not None else (default or 0.0),
            zap=_bool(sl.get("zap")),
            uv=_bool(sl.get("uv")),
            hidden=_bool(sl.get("hidden")),
            clamp=_bool(sl.get("clamp")),
            zap_toggles=[t for t in re.split(r"[;,]", sl.get("zaptoggles") or "") if t],
        )
        for d in sl.findall("Data"):
            slider.data.append(
                SliderData(
                    name=d.get("name") or "",
                    target=d.get("target") or "",
                    local=_bool(d.get("local")),
                    data_file=_text(d),
                )
            )
        ss.sliders.append(slider)
    return ss


def parse_osp_bytes(data: bytes, path: Optional[Path] = None) -> OspFile:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if not data.strip():
        raise OspParseError("file is empty")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        # BodySlide itself is lenient about encodings; retry as cp1252 text.
        try:
            text = data.decode("cp1252")
            text = re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip())
            root = ET.fromstring(text)
        except (ET.ParseError, UnicodeDecodeError):
            raise OspParseError(f"malformed XML: {exc}") from exc
    if root.tag != "SliderSetInfo":
        raise OspParseError(f"unexpected root element <{root.tag}> (expected <SliderSetInfo>)")
    version = None
    if root.get("version"):
        try:
            version = int(root.get("version"))
        except ValueError:
            version = None
    sets: list[SliderSet] = []
    errors: list[str] = []
    for i, el in enumerate(root.findall("SliderSet")):
        try:
            sets.append(_parse_set(el))
        except OspParseError as exc:
            errors.append(f"slider set #{i + 1}: {exc}")
    if not sets and not errors:
        errors.append("file contains no slider sets")
    return OspFile(path=path, version=version, sets=sets, errors=errors)


def parse_osp(path: str | Path) -> OspFile:
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise OspParseError(f"cannot read file: {exc}") from exc
    return parse_osp_bytes(data, p)
