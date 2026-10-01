"""A fake Outfit Studio for tests (no real binary needed).

It reads the private runtime's ``Config.xml`` (``ProjectPath``) and the
automation script, then does what Outfit Studio's steps do on the synthetic
fixtures: it loads the reference and the project, copies bone weights (the same
algorithm the analyzer's prediction uses) and saves a new project. Options
simulate failures: an embedded reference (AutoCopyRef ignored), a failing step
exit code, a crash marker, a hang, and touching files outside the workspace.

``Log_OS.txt`` is written in the format of Outfit Studio 5.8.2 on Windows. In the
interactive modes it has **no** ``Automation: Step`` lines, exactly as observed
on Windows: the Automation window redirects the log while a script runs.
"""
from __future__ import annotations

import os
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from cbbe3bbb.analysis.reference import ReferenceBody
from cbbe3bbb.bodyslide.osd import read_osd
from cbbe3bbb.bodyslide.osp import parse_osp
from cbbe3bbb.conversion.automation import parse_script
from cbbe3bbb.conversion.engine import RunOutcome
from cbbe3bbb.nif.reader import read_nif
from synth import BONE_POS, SynthShape, osp_xml, write_nif, write_osd

PE_SIG = struct.pack("<I", 0xFEEF04BD)


def fake_exe_bytes(version=(5, 8, 2, 0), automation=True) -> bytes:
    ms = (version[0] << 16) | version[1]
    ls = (version[2] << 16) | version[3]
    body = b"MZ" + b"\0" * 200 + PE_SIG + struct.pack("<IIII", 0x00010000, ms, ls, 0) + b"\0" * 64
    if automation:
        body += b"Run an automation script by name (no .xml extension); positional args are batch inputs\0"
    return body


def make_fake_runtime(folder: Path, version=(5, 8, 2, 0), automation=True, name="OutfitStudio x64.exe") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(fake_exe_bytes(version, automation))
    (folder / "res").mkdir(exist_ok=True)
    (folder / "res" / "skeleton_fo4.nif").write_bytes(b"x")
    (folder / "SliderSets").mkdir(exist_ok=True)  # like the release zip: must NOT be copied
    (folder / "SliderSets" / "junk.osp").write_text("x")
    (folder / "Config.xml").write_text("<Config><TargetGame>-1</TargetGame><Anim><DefaultSkeletonReference></DefaultSkeletonReference><SkeletonRootName></SkeletonRootName></Anim></Config>")
    return folder


def windows_session_log(runtime_folder: Path, project: Path, data: Path, t0: float, t1: float,
                        middle: list[str] = ()) -> str:
    """A Log_OS.txt as written by Outfit Studio 5.8.2 (Windows, interactive session)."""
    rf = str(runtime_folder).replace("/", "\\")
    hh = lambda t: time.strftime("[%H:%M:%S]", time.localtime(t))
    a = hh(t0)
    L = [time.strftime("[#Log %a %b %d %H:%M:%S %Y]", time.localtime(t0)),
         f"{a}[3]\tOutfitStudio.cpp(528): Initializing Outfit Studio...",
         f"{a}[3]\tOutfitStudio.cpp(547): Working directory: {rf}",
         f"{a}[3]\tOutfitStudio.cpp(548): Executable directory: {rf}",
         f"{a}[3]\tOutfitStudio.cpp(960): Game data path in config: {str(data).replace('/', chr(92))}" + "\\",
         f"{a}[3]\tOutfitStudio.cpp(967): Project path in config: {str(project).replace('/', chr(92))}",
         f"{a}[3]\tOutfitStudio.cpp(1193): Using language 'English'.",
         f"{a}[3]\tOutfitStudio.cpp(568): Target game: Fallout 4",
         f"{a}[3]\tOutfitStudio.cpp(1228): Loading Outfit Studio frame at X:100 Y:100 with W:1700 H:1125...",
         f"{a}[2]\timagpng.cpp(194): iCCP: CRC error",
         f"{a}[3]\tGLSurface.cpp(76): OpenGL attributes are supported.",
         f"{a}[3]\tAnim.cpp(891): Loaded skeleton '{rf}\\res/skeleton_fo4.nif' with root 'Root'.",
         f"{a}[3]\tOutfitStudio.cpp(1554): Outfit Studio frame loaded.",
         f"{a}[3]\tOutfitStudio.cpp(735): Outfit Studio initialized.",
         f"{a}[3]\tGLSurface.cpp(120): OpenGL Context Info:",
         f"{a}[3]\tGLSurface.cpp(121): -> Vendor:   'NVIDIA Corporation'"]
    L += [f"{a}[3]\t{m}" for m in middle]
    L.append(f"{hh(t1)}[3]\tOutfitStudio.cpp(1630): Outfit Studio frame closed.")
    return "\r\n".join(L) + "\r\n"


def cloth_payloads(path: Path) -> list[bytes]:
    """Raw BSClothExtraData payloads (without the name index) of a NIF."""
    from cbbe3bbb.nif.reader import _parse_header, _Stream

    data = Path(path).read_bytes()
    s = _Stream(data)
    hdr = _parse_header(s)
    pos, out = s.pos, []
    for i, size in enumerate(hdr.block_sizes):
        if hdr.block_types[hdr.block_type_index[i]] == "BSClothExtraData":
            out.append(data[pos + 4:pos + size])
        pos += size
    return out


def _blocks(raw: bytes):
    from cbbe3bbb.nif.reader import _parse_header, _Stream

    s = _Stream(raw)
    hdr = _parse_header(s)
    pos, out = s.pos, []
    for i, size in enumerate(hdr.block_sizes):
        out.append((hdr.block_types[hdr.block_type_index[i]], pos, size))
        pos += size
    return hdr, out


def _shape_block(raw: bytes, name: str):
    from cbbe3bbb.nif.reader import TRISHAPE_TYPES, _string

    hdr, blocks = _blocks(raw)
    for t, off, size in blocks:
        if t in TRISHAPE_TYPES and _string(hdr, struct.unpack_from("<i", raw, off)[0]) == name:
            return hdr, blocks, t, off, size
    raise KeyError(name)


def _shape_refs(raw: bytes, off: int) -> tuple[int, int]:
    """(shader ref, alpha ref) of a synthetic shape block."""
    n_extra = struct.unpack_from("<I", raw, off + 4)[0]
    p = off + 8 + 4 * n_extra + 4 + 4 + 12 + 36 + 4 + 4 + 16 + 4
    return struct.unpack_from("<ii", raw, p)


def _shader_params(raw: bytes, path, name: str) -> bytes:
    hdr, blocks, t, off, size = _shape_block(raw, name)
    sref, _ = _shape_refs(raw, off)
    _, so, ssz = blocks[sref]
    return raw[so + 4 + 4 + 4 + 4 + 24 + 4 + 16 + 4:so + ssz]


def _alpha_bytes(raw: bytes, path, name: str) -> bytes:
    hdr, blocks, t, off, size = _shape_block(raw, name)
    _, aref = _shape_refs(raw, off)
    _, ao, asz = blocks[aref]
    return raw[ao + 12:ao + asz]


def _tail_bytes(raw: bytes, path, name: str) -> bytes:
    from cbbe3bbb.nif.structure import read_structure

    hdr, blocks, t, off, size = _shape_block(raw, name)
    tail = read_structure(path).shape(name).tail_size
    return raw[off + size - tail + 4:off + size]  # the writer adds its own 4-byte field before the segments


def _string_extra(raw: bytes, path) -> list[tuple[str, str]]:
    from cbbe3bbb.nif.reader import _string

    hdr, blocks = _blocks(raw)
    return [(_string(hdr, struct.unpack_from("<i", raw, o)[0]), _string(hdr, struct.unpack_from("<i", raw, o + 4)[0]))
            for t, o, sz in blocks if t == "NiStringExtraData"]


class FakeOutfitStudio:
    """Modes: ok, windows_interactive, embed_ref, exit10, crash, hang, extra_slider,
    closed_without_running, stale_outputs, wrong_project_log, no_reference_info, step_log_without_save.

    A script may convert several projects (parts starting with ClearProject, as Phase 3
    batch scripts do). ``per_project`` injects a failure into one project only:
    ``{"<source set name>": "embed_ref" | "extra_slider" | "not_saved" | "step_error" | "no_weights" |
    "body_not_replaced" | "body_smeared" | "body_morphs_lost" | "untouched_changed"}`` (Phase 3.1 embedded body; the
    last one reassigns the bones of the shapes that are not copy targets). Like nifly's ``SetShapeVertWeights`` every
    saved shape's weights are divided by their per-vertex sum and truncated to the 4 largest.
    """

    INTERACTIVE_LIKE = ("windows_interactive", "closed_without_running", "stale_outputs", "wrong_project_log",
                        "no_reference_info")

    def __init__(self, mode: str = "ok", per_project: dict | None = None, cloth_answer: str = "own",
                 source_slider_data: bool = False, weight_deficit: float = 0.0):
        """``cloth_answer``: how the 'Choose cloth data' dialog is answered at each save when cloth data was
        loaded in the session: ``own`` (only the project's own file), ``all``, ``none``.

        ``source_slider_data`` (Phase 3.2): save like the real Outfit Studio 5.8.2 does — the outfit shapes keep
        reading their slider data from the SOURCE project's folder (``<Shape DataFolder="source folder">`` and slider
        ``Data`` entries naming the source .osd); no .osd is written to the new ShapeData folder."""
        self.mode = mode
        self.source_slider_data = source_slider_data
        # 0.5.1: emulate nifly's SetShapeVertWeights on >4 bones — every 4-influence vertex of a converted shape is
        # saved with this weight deficit (0.09 → sum 0.91)
        self.weight_deficit = weight_deficit
        self.per_project = dict(per_project or {})
        self.cloth_answer = cloth_answer
        self.calls = []
        self.cloth_dialogs = []  # (saved project, offered origins) per dialog
        self.direct_exports = []  # 0.4.1: (import path, export path, with_ref) per Direct-NIF part

    def __call__(self, runtime_folder: Path, exe: Path, script_name: str, timeout_s: int, cancel=None, interactive=False) -> RunOutcome:
        self.calls.append((runtime_folder, exe, script_name, interactive))
        cfg = ET.parse(Path(runtime_folder) / "Config.xml").getroot()
        project = Path(cfg.findtext("ProjectPath"))
        script_path = project / "Automations" / f"{script_name}.xml"
        t0 = time.time()
        out = RunOutcome(command=[str(exe)] if interactive else [str(exe), "-a", script_name], exit_code=0, seconds=0.1)
        # a real session log header (Windows format); headless mode adds the step lines
        steps_logged = self.mode not in self.INTERACTIVE_LIKE and not interactive
        log_project = project.parent / "Other" if self.mode == "wrong_project_log" else project
        log = [f"Automation: Running script '{script_name}' in headless mode."] if steps_logged else []
        if self.mode == "crash":
            (Path(runtime_folder) / "Log_OS.txt").write_text("Fatal exception has occurred, the program will terminate.")
            out.crashed = "Fatal exception has occurred"
            out.log_text = "Fatal exception has occurred"
            return out
        if self.mode == "hang":
            out.timed_out = True
            return out
        if self.mode == "exit10":
            out.exit_code = 10
            out.log_text = "Automation: Step 3 (AddProject) failed with error 2."
            return out
        sc = parse_script(script_path.read_text(encoding="utf-8"))
        self._cloth: dict[str, list[bytes]] = {}  # like OutfitProject::clothData: never cleared by ClearProject
        if self.mode == "closed_without_running":
            out.log_text = windows_session_log(runtime_folder, project, project.parent.parent, t0, time.time())
            (Path(runtime_folder) / "Log_OS.txt").write_text(out.log_text)
            return out
        parts, n = [], 0
        for st in sc.steps:
            if st.type == "ClearProject" or not parts:
                parts.append([])
            n += 1
            parts[-1].append((n, st))
        total = n
        for part in parts:
            steps = [st for _, st in part]
            imp = next((st for st in steps if st.type == "ImportFile"), None)
            if imp is not None:  # 0.4.1 Direct-NIF part: ImportFile → CopyBoneWeights → ExportFile
                pmode = self.per_project.get(Path(str(imp.params["FilePath"])).name, "")
                for i, st in part:
                    if steps_logged:
                        log.append(f"Automation: Step {i}/{total}: {st.type}")
                    if st.type == "CopyBoneWeights" and pmode == "step_error":
                        log.append(f"Automation: Step {i} (CopyBoneWeights) failed with error 1.")
                        out.exit_code = 10
                if pmode in ("no_output", "step_error"):
                    continue
                self.direct_exports.append(self._direct_part(project, steps, pmode))
                continue
            add = next(st for st in steps if st.type == "AddProject")
            pmode = self.per_project.get(add.params["Set"], "")
            for i, st in part:
                if steps_logged and not ((self.mode == "step_log_without_save" or pmode == "not_saved") and st.type == "SaveProject"):
                    log.append(f"Automation: Step {i}/{total}: {st.type}")
                if st.type == "CopyBoneWeights" and pmode == "step_error":
                    log.append(f"Automation: Step {i} (CopyBoneWeights) failed with error 1.")
                    out.exit_code = 10
            if pmode in ("not_saved", "step_error"):
                continue
            self._convert_part(project, steps, pmode)
        if steps_logged:
            log.append("Automation: complete")
        if self.mode == "stale_outputs":
            pass
        out.log_text = windows_session_log(runtime_folder, log_project, project.parent.parent, t0, time.time(), log)
        (Path(runtime_folder) / "Log_OS.txt").write_text(out.log_text)
        return out

    def _direct_part(self, project: Path, steps: list, pmode: str):
        """Direct-NIF: what Outfit Studio's ImportFile / CopyBoneWeights / ExportFile do, re-serializing the NIF
        (shaders, texture sets, alpha, segment data and extra data are carried over like nifly does).

        ``pmode`` failure injections: rename_shape, drop_shape, add_shape, embed_ref (WithRef ignored), move_vertex,
        lose_material, lose_segment, lose_extra_data, add_cloth, forbidden_region, zero_weight, untouched_changed,
        malformed, not_normalized; 0.4.2: change_normal, change_uv, change_tangent, change_color, change_eye,
        change_triangles, seam_normals (the real Windows 5.8.2 seam-normal recomputation on 4 duplicated seam
        vertices), offseam_normals (the same change on ordinary vertices), skin_scrambled."""
        from cbbe3bbb.nif.structure import read_structure

        by = {st.type: st for st in steps if st.type != "CopyBoneWeights"}
        cbws = [st for st in steps if st.type == "CopyBoneWeights"]
        ref_step, imp, exp = by["LoadReference"], by["ImportFile"], by["ExportFile"]
        ref_set = next(s for s in parse_osp(project / ref_step.params["SourceFile"]).sets if s.name == ref_step.params["Set"])
        ref_nif = read_nif(project / "ShapeData" / ref_set.data_folder / ref_set.source_file)
        ref_shape = ref_nif.shape(ref_step.params["Shape"])
        rb, rw = ref_shape.weight_matrix()
        ref = ReferenceBody(display_name="ref", set_name=ref_set.name, shape_name=ref_shape.name, source="fake",
                            vertices=ref_shape.world_vertices(), bones=rb, weights=rw, physics_bones=[],
                            physics_regions={}, side_sign={}, fingerprint="fake")
        fp = Path(str(imp.params["FilePath"]))
        src_path = fp if fp.is_absolute() else project / fp
        src = read_nif(src_path)
        st = read_structure(src_path)
        raw = src_path.read_bytes()
        cloth = cloth_payloads(src_path)
        shapes_out = []
        for shp in src.shapes:
            sb, sw = shp.weight_matrix()
            cbw = next((c for c in cbws if shp.name in c.target_meshes), None)
            if cbw is not None:
                radius, k = float(cbw.params["ProximityRadius"]), int(cbw.params["MaxResults"])
                pts = shp.world_vertices()
                pred = ref.predict_weights(pts, list(rb), radius, k)
                extra = [b for b in sb if b not in rb]
                E = sw[:, [sb.index(b) for b in extra]] if extra else np.zeros((len(pts), 0))
                M = np.where(pred < 0.001, 0.0, pred)
                tot = M.sum(1)
                tn = E.sum(1)
                E = E * np.where(tn > 0.001, (1 - tot) / np.maximum(tn, 1e-9), 0.0)[:, None]
                names, W = list(rb) + extra, np.concatenate([M, E], 1)
                if pmode == "forbidden_region":  # a thigh bone on a breast-only shape
                    j = names.index("LLeg_Thigh_01_F_skin")
                    W = W.copy()
                    W[:20, j] = 0.3
                if pmode == "zero_weight":
                    W = W.copy()
                    W[[3, 4], :] = 0.0
            else:
                names, W = list(sb), sw.copy()
                if pmode == "untouched_changed" and len(names) > 1:
                    names = names[::-1]
            if W.shape[1] < 4:
                W = np.concatenate([W, np.zeros((W.shape[0], 4 - W.shape[1]), dtype=W.dtype)], 1)
                names = list(names) + [f"__pad{i}" for i in range(4 - len(names))]
            order = np.argsort(-W, axis=1)[:, :4]
            ww = np.take_along_axis(W, order, 1).astype(np.float64)
            s_ = ww.sum(1, keepdims=True)
            ww = np.where(s_ > 1e-9, ww / np.maximum(s_, 1e-9), ww)  # nifly: divide by the per-vertex sum
            if pmode == "not_normalized" and cbw is not None:
                ww = ww * 0.9
            used = sorted({int(i) for i, v in zip(order.ravel(), ww.ravel()) if v > 0}) or [0]
            remap = {o: n for n, o in enumerate(used)}
            idx = np.vectorize(lambda x: remap.get(int(x), 0))(order)
            ww[np.isin(order, used, invert=True)] = 0
            ss = st.shape(shp.name)
            verts = shp.world_vertices().copy()
            if pmode == "move_vertex" and cbw is not None:
                verts[0, 2] += 0.5
            out = SynthShape(shp.name, verts, shp.triangles, [names[i] for i in used], ww.astype(np.float32),
                             idx.astype(np.uint8))
            if ss.shader and pmode != "lose_material":
                out.shader = {"name": ss.shader.get("name", ""), "textures": ss.shader.get("textures") or [""] * 10,
                              "shader_type": ss.shader.get("shader_type", 0), "wet_material": ss.shader.get("wet_material", ""),
                              "params": _shader_params(raw, src_path, shp.name)}
            if ss.alpha:
                out.alpha = _alpha_bytes(raw, src_path, shp.name)
            if ss.block_type == "BSSubIndexTriShape" and pmode != "lose_segment":
                out.segments = _tail_bytes(raw, src_path, shp.name)
            va = {k: ss.fields[k].copy() for k in ("bitangent_x", "uv", "normal", "bitangent_y", "tangent", "bitangent_z",
                                                     "color", "eye") if k in ss.fields}
            if ss.fields.get("other") is not None and ss.fields["other"].shape[1]:
                va["pad"] = ss.fields["other"].copy()
            if cbw is not None:  # 0.4.2 defect injections into single vertex attributes of the converted shape
                col = {"change_normal": "normal", "change_uv": "uv", "change_tangent": "tangent", "change_color": "color",
                       "change_eye": "eye", "change_other": "pad"}.get(pmode)
                pos = shp.vertices if hasattr(shp, "vertices") else None
                _, inv, cnt = np.unique(np.round(shp.world_vertices(), 5), axis=0, return_inverse=True, return_counts=True)
                welded = np.nonzero(cnt[inv.ravel()] > 1)[0]
                plain = np.nonzero(cnt[inv.ravel()] == 1)[0]
                if col and col in va:  # on an ordinary (not seam) vertex
                    va[col][plain[0], 0] ^= 0x10
                if pmode in ("seam_normals", "offseam_normals") and "normal" in va:
                    # the real Windows case: Outfit Studio's seam smoothing on a few duplicated seam vertices
                    rows = welded[:4] if pmode == "seam_normals" else plain[:4]
                    va["normal"][rows, 1] = (va["normal"][rows, 1].astype(int) + 9).clip(0, 255).astype(np.uint8)
                    va["tangent"][rows, 0] = (va["tangent"][rows, 0].astype(int) + 5).clip(0, 255).astype(np.uint8)
                    va["bitangent_x"][rows, 3] ^= 0x01
            out.vattrs = va or None
            if pmode == "change_triangles" and cbw is not None:
                tr = np.asarray(out.triangles).copy()
                tr[0] = tr[0][[0, 2, 1]]
                out.triangles = tr
            if pmode == "skin_scrambled" and cbw is not None:  # plausible-looking but wrong weights: another vertex's
                perm = np.roll(np.arange(len(out.weights)), len(out.weights) // 3)
                out.weights, out.indices = out.weights[perm], out.indices[perm]
            if pmode == "rename_shape" and cbw is not None:
                out.name = shp.name + "_1"
            shapes_out.append(out)
        if pmode == "drop_shape":
            shapes_out = shapes_out[:-1]
        if pmode == "add_shape":
            shapes_out.append(SynthShape("Extra", shapes_out[0].vertices[:3], np.array([[0, 1, 2]]), list(shapes_out[0].bones),
                                         shapes_out[0].weights[:3], shapes_out[0].indices[:3]))
        with_ref = str(exp.params.get("WithRef", "true")).lower() != "false"
        if with_ref or pmode == "embed_ref":
            shapes_out.append(SynthShape(ref_shape.name, ref_shape.world_vertices(), ref_shape.triangles, rb,
                                         ref_shape.bone_weights, ref_shape.bone_indices))
        extra_data = [] if pmode == "lose_extra_data" else _string_extra(raw, src_path)
        if pmode == "add_cloth":
            cloth = cloth + [b"CLOTH-FROM-NOWHERE" * 10]
        out_path = Path(str(exp.params["FilePath"]))
        write_nif(out_path, shapes_out, BONE_POS, root_name="Scene Root", cloth=cloth, extra_data=extra_data)
        if pmode == "malformed":
            out_path.write_bytes(out_path.read_bytes()[: len(out_path.read_bytes()) // 2])
        return (str(src_path), str(out_path), with_ref)

    def _convert_part(self, project: Path, steps: list, pmode: str) -> None:
        by = {}
        for st in steps:
            if st.type == "CopyBoneWeights":
                by.setdefault("cbw", []).append(st)
            else:
                by.setdefault(st.type, st)
        ref_step, add_step, save = by["LoadReference"], by["AddProject"], by["SaveProject"]
        cbws = by.get("cbw", [])
        t0 = time.time()
        # reference
        ref_set = next(s for s in parse_osp(project / ref_step.params["SourceFile"]).sets if s.name == ref_step.params["Set"])
        ref_nif = read_nif(project / "ShapeData" / ref_set.data_folder / ref_set.source_file)
        ref_shape = ref_nif.shape(ref_step.params["Shape"])
        rb, rw = ref_shape.weight_matrix()
        ref = ReferenceBody(display_name="ref", set_name=ref_set.name, shape_name=ref_shape.name, source="fake",
                            vertices=ref_shape.world_vertices(), bones=rb, weights=rw, physics_bones=[],
                            physics_regions={}, side_sign={}, fingerprint="fake")
        # project
        src_set = next(s for s in parse_osp(project / add_step.params["SourceFile"]).sets if s.name == add_step.params["Set"])
        src_nif_path = project / "ShapeData" / src_set.data_folder / src_set.source_file
        src_nif = read_nif(src_nif_path)
        for path in (project / "ShapeData" / ref_set.data_folder / ref_set.source_file, src_nif_path):
            payloads = cloth_payloads(path)
            if payloads:
                self._cloth[str(path)] = payloads
        shapes_out = []
        body_targets: list[str] = []
        for e in src_set.shapes:
            shp = src_nif.shape(e.name)
            if shp is None:
                continue
            sb, sw = shp.weight_matrix()
            cbw = next((c for c in cbws if e.name in c.target_meshes), None)
            identity = cbw is not None and float(cbw.params["ProximityRadius"]) < 0.5
            if identity:
                body_targets.append(e.target)
                if pmode == "body_not_replaced":
                    cbw = None
            if cbw is not None and pmode != "no_weights":
                radius = float(cbw.params["ProximityRadius"])
                k = int(cbw.params["MaxResults"])
                if identity and pmode == "body_smeared":  # a clothing-style transfer onto the body
                    radius, k = 25.0, 10
                bone_list = [b.strip() for b in str(cbw.params.get("BoneList", "")).split(",") if b.strip()]
                copy_bones = [b for b in rb if b in bone_list] if bone_list else list(rb)
                pts = shp.world_vertices()
                if identity and pmode == "body_smeared":  # weights sampled from the wrong place, averaged
                    pts = pts + np.array([0.0, 0.0, 6.0])
                pred = ref.predict_weights(pts, copy_bones, radius, k)
                if bone_list:  # BoneList copy: only the listed bones are replaced, the rest rescaled
                    keep = [b for b in sb if b not in copy_bones]
                    E = sw[:, [sb.index(b) for b in keep]] if keep else np.zeros((len(pts), 0))
                    M = np.where(pred < 0.001, 0.0, pred)
                    tot = M.sum(1)
                    tn = E.sum(1)
                    E = E * np.where(tn > 0.001, (1 - tot) / np.maximum(tn, 1e-9), 0.0)[:, None]
                    names, W = copy_bones + keep, np.concatenate([M, E], 1)
                else:
                    extra = [b for b in sb if b not in rb]
                    E = sw[:, [sb.index(b) for b in extra]] if extra else np.zeros((len(pts), 0))
                    M = np.where(pred < 0.001, 0.0, pred)
                    tot = M.sum(1)
                    tn = E.sum(1)
                    E = E * np.where(tn > 0.001, (1 - tot) / np.maximum(tn, 1e-9), 0.0)[:, None]
                    names, W = rb + extra, np.concatenate([M, E], 1)
            else:
                names, W = sb, sw
                if pmode == "untouched_changed" and not identity and len(names) > 1:  # a real change to a NONE shape
                    names = list(names)[::-1]
            if W.shape[1] < 4:  # pad to 4 influences (the NIF writer stores exactly 4)
                W = np.concatenate([W, np.zeros((W.shape[0], 4 - W.shape[1]), dtype=W.dtype)], 1)
                names = list(names) + [f"__pad{i}" for i in range(4 - len(names))]
            # keep the 4 largest influences, renormalize
            order = np.argsort(-W, axis=1)[:, :4]
            ww = np.take_along_axis(W, order, 1)
            ww = ww / np.maximum(ww.sum(1, keepdims=True), 1e-9)
            used = sorted({int(i) for i, v in zip(order.ravel(), ww.ravel()) if v > 0})
            remap = {o: n for n, o in enumerate(used)}
            idx = np.vectorize(lambda x: remap.get(int(x), 0))(order)
            ww[np.isin(order, used, invert=True)] = 0
            deficit = self.weight_deficit
            if deficit and cbw is not None and not identity:  # 0.5.1: real nifly — normalized over >4 bones, then 4 kept
                four = (ww > 0).sum(1) == 4
                ww[four] *= (1.0 - deficit)
            if pmode == "bad_weights" and cbw is not None and not identity:  # a defect the renormalization must refuse
                four = np.nonzero((ww > 0).sum(1) == 4)[0]
                ww[four, :] *= 0.9
                ww[0, 0] = -0.1
            shapes_out.append(SynthShape(e.name, shp.world_vertices(), shp.triangles, [names[i] for i in used],
                                         ww.astype(np.float32), idx.astype(np.uint8)))
        embed = self.mode == "embed_ref" or pmode == "embed_ref" or save.params.get("AutoCopyRef", "true") != "false"
        if embed:
            shapes_out.append(SynthShape(ref_shape.name, ref_shape.world_vertices(), ref_shape.triangles, rb,
                                         ref_shape.bone_weights, ref_shape.bone_indices))
        folder = save.params["ShapeDataFolder"]
        nif_name = save.params["ShapeDataFile"]
        osd_name = nif_name[:-4] + ".osd"
        cloth_out: list[bytes] = []
        if self._cloth:  # Outfit Studio's 'Choose cloth data' dialog
            self.cloth_dialogs.append((save.params["DisplayName"], sorted(self._cloth)))
            chooser = getattr(self, "cloth_chooser", None)
            if chooser is not None:  # 0.5.3: the modelled Windows dialog answers (None = Cancel = no cloth data)
                chosen = chooser(sorted(self._cloth)) or []
                for origin in chosen:
                    cloth_out += self._cloth[origin]
            else:
                for origin, payloads in self._cloth.items():
                    if self.cloth_answer == "all" or (self.cloth_answer == "own" and origin == str(src_nif_path)):
                        cloth_out += payloads
        write_nif(project / "ShapeData" / folder / nif_name, shapes_out, BONE_POS, cloth=cloth_out)
        # slider data: copy the source entries for the outfit targets
        src_osd = read_osd(project / "ShapeData" / src_set.data_folder / (src_set.source_file[:-4] + ".osd"))
        lost = tuple(body_targets) if pmode == "body_morphs_lost" else ()
        if not self.source_slider_data:
            write_osd(project / "ShapeData" / folder / osd_name,
                      {n: (r["index"], r["diff"]) for n, r in src_osd.entries.items() if not (lost and n.startswith(lost))})
        sliders = [s.name for s in src_set.sliders if not s.zap]
        if self.mode == "extra_slider" or pmode == "extra_slider":
            sliders.append("RefOnlySlider")
        st = dict(name=save.params["DisplayName"], folder=folder, source=nif_name,
                  output_path=save.params["OutputDataPath"], output_file=save.params["OutputFileName"],
                  gen_weights=save.params["GenWeights"] == "true",
                  shapes=[(e.name, e.target, src_set.data_folder if self.source_slider_data else "") for e in src_set.shapes]
                  + ([(ref_shape.name, ref_shape.name)] if embed else []),
                  sliders=sliders)
        if self.source_slider_data:
            st["osd_file"] = src_set.source_file[:-4] + ".osd"
        p = project / save.params["SliderSetFile"]
        p.parent.mkdir(parents=True, exist_ok=True)
        xml = osp_xml([st])
        if self.mode != "no_reference_info":  # SaveProject records the loaded reference
            ref_file = ref_step.params["SourceFile"].replace("/", "\\")
            xml = xml.replace(f'<SliderSet name="{st["name"]}">',
                              f'<SliderSet name="{st["name"]}">\n    <ReferenceInfo ProjectFile="{ref_file}" '
                              f'ProjectName="{ref_set.name}" ShapeName="{ref_shape.name}"/>', 1)
        p.write_text(xml, encoding="utf-8")
        if self.mode == "stale_outputs":  # files that look like they were written long before this session
            old = t0 - 3600
            for f in (p, project / "ShapeData" / folder / nif_name):
                os.utime(f, (old, old))
