"""Post-build verification (read-only): does the mesh the game loads carry the conversion?

The conversion checks (validate.py) prove the *generated project* is right. They
cannot see the BodySlide build or which in-game item uses the built mesh. This
check closes that gap after the user has built with BodySlide:

1. resolve ``meshes/<output path>/<file>.nif`` through the MO2 providers (the
   winner is what the game loads as a loose file) and list every competitor;
2. the winner must be newer than the commit (built after the conversion);
3. every shape of the generated ShapeData must be in the built mesh with the
   same bone list, the same physics bones weighted and the same weights
   (BodySlide applies slider morphs to positions, never to weights; zapped
   vertices may be missing, so rows are matched as a subset);
4. the plugins of the source package are searched for the in-game items that
   use the mesh, and a name mismatch (project "Outfit", item "Outfit V2") is
   flagged, because that item is the one to test in game.

It writes nothing into any mod. Reports go into the tool's reports/builds folder.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from ..nif.reader import NifError, read_nif
from .gamedata import item_name_mismatch, package_items_for_mesh, resolve_data_file

__all__ = ["BuildCheck", "verify_build", "WEIGHT_TOL"]

WEIGHT_TOL = 1e-4


@dataclass
class BuildCheck:
    generated_project: str
    source_project: str = ""
    status: str = "NOT_CHECKED"  # BUILD_OK | BUILD_MISMATCH | NOT_BUILT | BUILD_STALE | ERROR
    mesh: str = ""
    winner: dict = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    shapes: list[dict] = field(default_factory=list)
    game_items: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked_at: str = ""

    def add(self, name: str, ok: bool, detail: str = "", required: bool = True) -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required})

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.checks if c["required"] and not c["ok"]]

    def to_dict(self) -> dict:
        return asdict(self)

    def text(self) -> str:
        L = [f"BUILD CHECK  {self.generated_project}   →  {self.status}",
             f"  source project: {self.source_project}",
             f"  game mesh: {self.mesh}"]
        if self.winner:
            L.append(f"  loaded by the game (MO2 winner): {self.winner.get('provider')}: {self.winner.get('path')}  "
                     f"{self.winner.get('size')} bytes, modified {self.winner.get('modified')}, sha256 {self.winner.get('sha256', '')[:16]}…")
        for c in self.candidates[1:]:
            L.append(f"  overridden copy: {c.get('provider')}: {c.get('path')} ({c.get('size')} bytes, {c.get('modified')})")
        L.append("  checks:")
        for c in self.checks:
            L.append(f"    [{'OK' if c['ok'] else ('FAIL' if c['required'] else 'note')}] {c['name']}: {c['detail']}")
        if self.shapes:
            L.append("  shapes (built mesh):")
            for s in self.shapes:
                L.append(f"    {s['name']}: {s['vertices']} v (generated {s['generated_vertices']}), {s['bones']} bones, "
                         f"weight max diff {s['weight_max_diff']}")
                if s.get("physics"):
                    L.append("      3BBB: " + ", ".join(f"{b.replace('_skin', '')} {n}" for b, n in s["physics"].items()))
                if s.get("legacy"):
                    L.append("      legacy: " + ", ".join(f"{b} {n}" for b, n in s["legacy"].items()))
        L.append("  IN-GAME ITEMS using this mesh (test these):" if self.game_items else "  in-game items: none found in the package's plugins")
        for it in self.game_items:
            L.append(f"    '{it['armor_name'] or it['armor_editor_id']}'  (ARMO {it['armor_editor_id']} {it['armor_form_id']}, "
                     f"ARMA {it['addon_editor_id']} {it['addon_form_id']}, {it['role']}, {it['plugin']})")
        L += [f"  ! {w}" for w in self.warnings]
        L += [f"  note: {n}" for n in self.notes]
        return "\n".join(L)


LEGACY_BONES = ("LBreast_skin", "RBreast_skin", "LButtFat_skin", "RButtFat_skin")


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def _weighted(bones: list[str], W: np.ndarray, names) -> dict[str, int]:
    return {b: int((W[:, bones.index(b)] > 0.001).sum()) for b in names if b in bones and (W[:, bones.index(b)] > 0.001).any()}


def _weight_diff(gb: list[str], gw: np.ndarray, bb: list[str], bw: np.ndarray) -> float:
    """Max weight difference; rows matched by index when counts agree, else as a subset (zapped vertices)."""
    allb = sorted(set(gb) | set(bb))

    def full(B, W):
        M = np.zeros((W.shape[0], len(allb)), dtype=np.float64)
        for j, x in enumerate(allb):
            if x in B:
                M[:, j] = W[:, B.index(x)]
        return M

    G, Bm = full(gb, gw), full(bb, bw)
    if G.shape[0] == Bm.shape[0]:
        return float(np.abs(G - Bm).max()) if G.size else 0.0
    from scipy.spatial import cKDTree

    d, _ = cKDTree(G).query(Bm, k=1)
    return float(np.max(d)) if len(d) else 0.0


def verify_build(record: dict, out_mod: Path, providers, physics_bones: list[str],
                 package_root: Optional[Path] = None) -> BuildCheck:
    """Verify the BodySlide-built game mesh of one manifest record. Read-only."""
    tgt = record.get("target", {})
    res = BuildCheck(generated_project=tgt.get("project", "?"), source_project=record.get("source", {}).get("project", ""),
                     checked_at=_stamp(time.time()))
    mesh = "/".join(x.strip("\\/") for x in (tgt.get("game_output_path", ""), tgt.get("game_output_file", "")) if x).replace("\\", "/") + ".nif"
    res.mesh = mesh.replace("/", "\\")
    # 1. generated ShapeData, unchanged since the commit
    gen_rel = tgt.get("shapedata", "")
    gen_path = Path(out_mod) / gen_rel
    rec_hash = next((f.get("sha256") for f in record.get("generated_files", []) if f.get("path") == gen_rel), None)
    if not gen_path.is_file():
        res.status = "ERROR"
        res.add("generated ShapeData present", False, str(gen_path))
        return res
    gh = _sha(gen_path)
    res.add("generated ShapeData unchanged since the commit", rec_hash is None or gh == rec_hash,
            f"sha256 {gh[:16]}… (recorded {str(rec_hash)[:16]}…)")
    # 2. what the game loads
    cands = resolve_data_file(providers, mesh)
    res.candidates = [{"provider": n, "path": str(p), "size": s, "modified": _stamp(m), "mtime": m} for n, p, s, m in cands]
    if not cands:
        res.status = "NOT_BUILT"
        res.add("built game mesh present", False, f"no provider has {res.mesh}")
        return res
    wname, wpath, wsize, wmtime = cands[0]
    res.winner = dict(res.candidates[0], sha256=_sha(wpath))
    for c in res.candidates[1:]:
        c["sha256"] = _sha(Path(c["path"]))
    res.add("built game mesh present (MO2 winner)", True, f"{wname}: {wpath}")
    if len(cands) > 1:
        res.warnings.append(f"{len(cands) - 1} other mod(s) also provide {res.mesh}; the game loads the winner only "
                            f"({wname}). Overridden: " + ", ".join(n for n, *_ in cands[1:]))
    try:
        commit_t = time.mktime(time.strptime(record.get("timestamp", ""), "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        commit_t = None
    fresh = commit_t is None or wmtime >= commit_t
    res.add("built after the conversion was committed", fresh,
            f"mesh modified {_stamp(wmtime)}, commit {record.get('timestamp', '?')}")
    # 3. structural comparison
    try:
        g = read_nif(gen_path)
        b = read_nif(wpath)
    except (NifError, OSError, ValueError) as exc:
        res.status = "ERROR"
        res.add("meshes readable", False, str(exc))
        return res
    phys = list(physics_bones)
    missing_shapes = []
    for gs in g.shapes:
        bs = b.shape(gs.name)
        if bs is None:
            missing_shapes.append(gs.name)
            continue
        gb, gw = gs.weight_matrix()
        bb, bw = bs.weight_matrix()
        gp, bp = _weighted(gb, gw, phys), _weighted(bb, bw, phys)
        diff = _weight_diff(gb, gw, bb, bw)
        res.shapes.append({"name": gs.name, "vertices": bs.num_vertices, "generated_vertices": gs.num_vertices,
                           "bones": len(bb), "physics": bp, "legacy": _weighted(bb, bw, LEGACY_BONES),
                           "weight_max_diff": round(diff, 6)})
        if gp:
            res.add(f"{gs.name}: 3BBB physics bones survived the build", set(gp) <= set(bp) and all(bp[x] > 0 for x in gp),
                    f"generated {len(gp)} bones, built {len(bp)}: " + ", ".join(f"{k.replace('_skin', '')} {v}" for k, v in bp.items()))
        res.add(f"{gs.name}: bone list and weights identical to the generated ShapeData",
                set(gb) == set(bb) and diff <= WEIGHT_TOL,
                f"{len(bb)} bones (generated {len(gb)}), weight max diff {diff:.1e}, {bs.num_vertices}/{gs.num_vertices} v")
    res.add("every generated shape is in the built mesh", not missing_shapes,
            ("missing: " + ", ".join(missing_shapes)) if missing_shapes else f"{len(g.shapes)} shapes")
    extra = [s.name for s in b.shapes if g.shape(s.name) is None]
    if extra:
        res.warnings.append("the built mesh has shapes the generated project does not: " + ", ".join(extra))
    # 4. the in-game items
    items, notes = package_items_for_mesh(package_root, mesh)
    res.notes += notes
    res.game_items = [i.to_dict() for i in items]
    res.add("an in-game item of the package uses this mesh", bool(items),
            "; ".join(i.label() for i in items) or "no ARMA of the package's plugins references it", required=False)
    for i in items:
        if item_name_mismatch(res.source_project, i.armor_name):
            res.warnings.append(f"GAME_ITEM_NAME_MISMATCH: the project '{res.source_project}' builds {res.mesh}, which the "
                                f"game shows as '{i.armor_name}' ({i.armor_editor_id}). Test THAT item in game.")
    res.notes.append("plugins of other mods can override these ARMA/ARMO records; only the package's own plugins are read")
    if not fresh:
        res.status = "BUILD_STALE"
    elif res.failed:
        res.status = "BUILD_MISMATCH"
    else:
        res.status = "BUILD_OK"
    return res


def write_build_report(res: BuildCheck, folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    base = "".join(c if c.isalnum() or c in " -_." else "_" for c in res.generated_project).strip() or "build"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pj, pt = folder / f"{stamp}_{base}.json", folder / f"{stamp}_{base}.txt"
    pj.write_text(json.dumps(res.to_dict(), indent=2), encoding="utf-8")
    pt.write_text(res.text() + "\n", encoding="utf-8")
    return {"json": pj, "txt": pt}
