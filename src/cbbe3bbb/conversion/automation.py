"""Outfit Studio 5.8 automation scripts (``<ProjectPath>/Automations/<name>.xml``).

The XML layout mirrors ``AutomationScript::Save``/``Load`` in Outfit Studio
5.8.2 (``src/components/Automation.cpp``)::

    <AutomationScript version="1">
      <Step type="LoadReference" active="true">
        <Note>...</Note>
        <TargetMeshes>a, b</TargetMeshes>
        <SourceFile>SliderSets/X.osp</SourceFile> ...
      </Step>
    </AutomationScript>

Outfit Studio only writes non-default values, and defaults some values to
``true`` when they are missing (``SaveProject/AutoCopyRef`` defaults to
**true**). This writer therefore always writes every parameter explicitly, and
:func:`check_invariants` refuses any script whose ``SaveProject`` step does not
contain ``<AutoCopyRef>false</AutoCopyRef>``.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "AutomationStep",
    "AutomationScript",
    "ScriptInvariantError",
    "check_invariants",
    "check_batch_invariants",
    "split_segments",
    "parse_script",
    "safe_script_name",
]

NO_TARGET_LIMIT = 2147483647  # what Outfit Studio's dialogs pass for "No Target Limit"


class ScriptInvariantError(ValueError):
    pass


def _fmt(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.6f}"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


@dataclass
class AutomationStep:
    type: str
    params: dict[str, object] = field(default_factory=dict)  # child element -> value (ordered)
    note: str = ""
    target_meshes: list[str] = field(default_factory=list)
    active: bool = True

    def to_element(self) -> ET.Element:
        el = ET.Element("Step", {"type": self.type, "active": _fmt(self.active)})
        if self.note:
            ET.SubElement(el, "Note").text = self.note
        if self.target_meshes:
            ET.SubElement(el, "TargetMeshes").text = ", ".join(self.target_meshes)
            ET.SubElement(el, "TargetRegex").text = "false"
        for k, v in self.params.items():
            ET.SubElement(el, k).text = _fmt(v)
        return el

    def summary(self) -> str:
        bits = [f"{k}={_fmt(v)}" for k, v in self.params.items()]
        if self.target_meshes:
            bits.insert(0, f"TargetMeshes={', '.join(self.target_meshes)}")
        return f"{self.type}(" + "; ".join(bits) + ")"


@dataclass
class AutomationScript:
    name: str
    steps: list[AutomationStep] = field(default_factory=list)

    def to_xml(self) -> str:
        root = ET.Element("AutomationScript", {"version": "1"})
        for s in self.steps:
            root.append(s.to_element())
        ET.indent(root, space="    ")
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"

    def write(self, automations_dir: Path) -> Path:
        check_invariants(self)
        automations_dir.mkdir(parents=True, exist_ok=True)
        p = automations_dir / f"{self.name}.xml"
        p.write_text(self.to_xml(), encoding="utf-8")
        return p

    def step(self, type_: str) -> Optional[AutomationStep]:
        return next((s for s in self.steps if s.type == type_), None)


def safe_script_name(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    return (s or "conversion")[:80]


def parse_script(xml_text: str) -> AutomationScript:
    root = ET.fromstring(xml_text)
    if root.tag != "AutomationScript":
        raise ValueError("not an AutomationScript")
    sc = AutomationScript(name="")
    for st in root.findall("Step"):
        step = AutomationStep(type=st.get("type", ""), active=st.get("active", "true") in ("true", "1"))
        for ch in st:
            if ch.tag == "Note":
                step.note = ch.text or ""
            elif ch.tag == "TargetMeshes":
                step.target_meshes = [x.strip() for x in (ch.text or "").split(",") if x.strip()]
            elif ch.tag == "TargetRegex":
                continue
            else:
                step.params[ch.tag] = ch.text or ""
        sc.steps.append(step)
    return sc


def check_invariants(script: AutomationScript | str) -> None:
    """Hard safety rules for conversion scripts (applied to the XML that will be run)."""
    sc = parse_script(script.to_xml()) if isinstance(script, AutomationScript) else parse_script(script)
    saves = [s for s in sc.steps if s.type == "SaveProject" and s.active]
    if len(saves) != 1:
        raise ScriptInvariantError(f"conversion script must contain exactly one SaveProject step (found {len(saves)})")
    save = saves[0]
    if str(save.params.get("AutoCopyRef", "")).strip().lower() != "false":
        raise ScriptInvariantError("SaveProject must set AutoCopyRef=false explicitly (Outfit Studio defaults to true)")
    if str(save.params.get("CopyRefFromProject", "false")).strip().lower() != "false":
        raise ScriptInvariantError("SaveProject must not copy the reference from the project")
    for key in ("SliderSetFile", "DisplayName", "ShapeDataFolder", "ShapeDataFile", "OutputDataPath", "OutputFileName"):
        if not str(save.params.get(key, "")).strip():
            raise ScriptInvariantError(f"SaveProject.{key} must be set")
    for s in sc.steps:
        if s.type == "CopyBoneWeights":
            try:
                radius, results = float(s.params.get("ProximityRadius", 5)), int(float(s.params.get("MaxResults", 4)))
            except ValueError as exc:
                raise ScriptInvariantError(f"CopyBoneWeights parameters are not numeric: {exc}") from exc
            if radius < 0.5 and results != 1:
                raise ScriptInvariantError("a CopyBoneWeights step with a tiny radius must be a vertex-identity copy (MaxResults 1)")
        if s.type == "ImportSliderData" and str(s.params.get("FromFolder", "false")).lower() != "false":
            raise ScriptInvariantError("ImportSliderData must import one named file, not a folder")
        if s.type in ("ExportFile", "ImportFile", "DeleteShape", "RemoveSkinning", "ApplyPose", "TransformShape", "RefineMesh",
                      "ConformSliders", "SetBaseShape", "MirrorShape", "SymmetrizeVertices", "DeleteVertices", "MergeGeometry"):
            raise ScriptInvariantError(f"step type {s.type} is not allowed in conversion scripts")
        for key, val in s.params.items():
            v = str(val)
            from pathlib import PurePosixPath, PureWindowsPath

            if key in ("SourceFile", "SliderSetFile", "SliderDataFile") and (
                PurePosixPath(v).is_absolute() or PureWindowsPath(v).is_absolute() or PureWindowsPath(v).drive
                or ".." in PurePosixPath(v.replace("\\", "/")).parts
            ):
                raise ScriptInvariantError(f"{s.type}.{key} must be a path inside the workspace project folder: {v}")


def split_segments(sc: AutomationScript) -> list[list[AutomationStep]]:
    """Split a batch script into its per-project parts (each starts with ClearProject)."""
    segs: list[list[AutomationStep]] = []
    for st in sc.steps:
        if st.type == "ClearProject" or not segs:
            segs.append([])
        segs[-1].append(st)
    return segs


def check_batch_invariants(script: AutomationScript | str) -> list[tuple[int, int]]:
    """Safety rules for a script that converts several projects in turn.

    Every part must start with ClearProject (so no state carries over between projects)
    and must satisfy :func:`check_invariants` on its own (one SaveProject with
    AutoCopyRef=false, workspace-relative paths, no geometry-changing steps). Output
    project files and ShapeData folders must be unique. Returns the 1-based
    (first, last) step numbers of each part, as Outfit Studio numbers them in its log.
    """
    sc = parse_script(script.to_xml()) if isinstance(script, AutomationScript) else parse_script(script)
    segs = split_segments(sc)
    if not segs:
        raise ScriptInvariantError("empty batch script")
    ranges = []
    outputs: set[str] = set()
    folders: set[str] = set()
    n = 0
    for seg in segs:
        if seg[0].type != "ClearProject":
            raise ScriptInvariantError("every project in a batch script must start with ClearProject")
        check_invariants(AutomationScript(name="part", steps=seg))
        save = next(s for s in seg if s.type == "SaveProject")
        key = str(save.params.get("SliderSetFile", "")).replace("\\", "/").lower()
        folder = str(save.params.get("ShapeDataFolder", "")).lower()
        if key in outputs or folder in folders:
            raise ScriptInvariantError(f"two projects in one batch save to the same file/folder: {key} / {folder}")
        outputs.add(key)
        folders.add(folder)
        ranges.append((n + 1, n + len(seg)))
        n += len(seg)
    return ranges
