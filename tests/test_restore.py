"""0.4.3 (Phase 4.1R2): post-export restoration of immutable vertex attributes (Direct-NIF).

FINAL vertex = SOURCE immutable bytes + RAW-EXPORT skin weights / bone indices, but only after a structural gate and a
raw-diff policy that allows nothing but the intended skinning and Outfit Studio's seam-normal regeneration.
Numbers in the test names refer to the Phase 4.1R2 test list.
"""
import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tests._util import temp_home, tree_digest
from cbbe3bbb.conversion import restore as restore_mod
from cbbe3bbb.conversion.restore import (REFUSED_UNEXPECTED, REFUSED_UNSAFE, restore_immutable_vertices,
                                         seam_vertices)
from cbbe3bbb.nif.structure import read_structure
from synth import BONE_POS, SynthShape, make_body, outfit_patch, seam_split, write_nif
from test_direct_batch import DIRECT_PACKAGE, OUT, convert, failed, mesh_path, out_path, rows, setup

FIELDS = {"bitangent_x": 4, "uv": 4, "uv2": 4, "normal": 3, "bitangent_y": 1, "tangent": 3, "bitangent_z": 1,
          "color": 4, "land": 4, "eye": 4, "pad": 4}


def source_shape(seed=3):
    body = make_body(False)
    s = seam_split(outfit_patch(body, (85, 95), name="Top", only_bones=["SPINE1", "SPINE2", "Chest", "Neck", "Pelvis"]))
    rng = np.random.default_rng(seed)
    n = len(s.vertices)
    s.vattrs = {k: rng.integers(0, 256, (n, w), dtype=np.uint8) for k, w in FIELDS.items()}
    s.vattrs["uv"] = rng.random((n, 2)).astype(np.float16).view(np.uint8).reshape(n, 4)
    s.shader = {"name": "Materials\\Test\\top.bgsm"}
    s.segments = b"SEGMENTS" * 3
    return s


def clone(s: SynthShape, name=None) -> SynthShape:
    c = SynthShape(name or s.name, np.array(s.vertices, copy=True), np.array(s.triangles, copy=True), list(s.bones),
                   np.array(s.weights, copy=True), np.array(s.indices, copy=True), shader=dict(s.shader or {}) or None,
                   segments=s.segments, vattrs={k: v.copy() for k, v in (s.vattrs or {}).items()})
    return c


class RestoreUnitTests(unittest.TestCase):
    def setUp(self):
        self.home = temp_home()
        self.src = source_shape()
        write_nif(self.home / "src.nif", [self.src], BONE_POS)
        seams = seam_vertices(read_structure(self.home / "src.nif").shape("Top"))
        self.seam = np.nonzero(seams)[0][:6]
        self.plain = np.nonzero(~seams)[0][:6]
        self.assertTrue(len(self.seam) and len(self.plain))

    def raw(self, mutate, extra=()):
        r = clone(self.src)
        mutate(r)
        write_nif(self.home / "raw.nif", [r] + list(extra), BONE_POS)
        return restore_immutable_vertices(self.home / "src.nif", self.home / "raw.nif", self.home / "final.nif", "CBBE-3BBB")

    def fields(self, name):
        return read_structure(self.home / name).shape("Top").fields

    def reskin(self, r):  # what Copy Bone Weights does: new weights and indices
        w = np.zeros_like(r.weights)
        w[:, 0], w[:, 1] = 0.7, 0.3
        r.weights = w
        r.indices = np.tile(np.array([2, 1, 0, 0], np.uint8), (len(w), 1))

    def assert_restored(self, res):
        self.assertTrue(res.ok, (res.code, res.reasons))
        src, fin, raw = self.fields("src.nif"), self.fields("final.nif"), self.fields("raw.nif")
        for k in src:
            if k in ("skin_weights", "bone_indices"):
                self.assertTrue(np.array_equal(fin[k], raw[k]), k)
            else:
                self.assertTrue(np.array_equal(fin[k], src[k]), k)
        return src, fin, raw

    def test_01_os_changed_normals_restored(self):
        def m(r):
            self.reskin(r)
            r.vattrs["normal"][self.seam, 1] ^= 0x20
        res = self.raw(m)
        src, fin, raw = self.assert_restored(res)
        self.assertFalse(np.array_equal(raw["normal"], src["normal"]))
        self.assertEqual(res.shapes["Top"]["os_regenerated_normals"], len(self.seam))

    def test_02_os_changed_tangent_basis_restored(self):
        def m(r):
            self.reskin(r)
            for k in ("tangent", "bitangent_x", "bitangent_y", "bitangent_z"):
                r.vattrs[k][self.seam, 0] ^= 0x08
        res = self.raw(m)
        self.assert_restored(res)
        self.assertEqual(res.shapes["Top"]["os_regenerated_tangent_basis"], len(self.seam))

    def test_03_04_new_skin_weights_and_indices_survive_byte_for_byte(self):
        res = self.raw(self.reskin)
        src, fin, raw = self.assert_restored(res)
        self.assertFalse(np.array_equal(src["skin_weights"], fin["skin_weights"]))
        self.assertFalse(np.array_equal(src["bone_indices"], fin["bone_indices"]))
        self.assertEqual(res.shapes["Top"]["skin_changed_vertices"], len(self.src.vertices))

    def test_05_10_every_immutable_field_is_restored_and_other_bytes_kept(self):
        def m(r):
            self.reskin(r)
            r.vattrs["normal"][self.seam, 0] ^= 0x01
        res = self.raw(m)
        src, fin, _ = self.assert_restored(res)
        for k in ("position", "uv", "uv2", "normal", "tangent", "bitangent_x", "bitangent_y", "bitangent_z", "color",
                  "land", "eye", "other"):
            self.assertIn(k, fin)
            self.assertTrue(np.array_equal(src[k], fin[k]), k)
        # outside the vertex buffers the final file is the raw export byte for byte
        a, b = (self.home / "raw.nif").read_bytes(), (self.home / "final.nif").read_bytes()
        self.assertEqual(len(a), len(b))
        self.assertEqual(sum(x != y for x, y in zip(a, b)), res.bytes_restored)

    def test_05_10_22_raw_changes_outside_the_policy_are_refused_not_hidden(self):
        cases = {
            "position": lambda r: r.vertices.__setitem__((self.plain[0], 2), r.vertices[self.plain[0], 2] + 0.25),
            "uv": lambda r: r.vattrs["uv"].__setitem__((self.plain[0], 0), r.vattrs["uv"][self.plain[0], 0] ^ 1),
            "uv2": lambda r: r.vattrs["uv2"].__setitem__((self.plain[0], 0), r.vattrs["uv2"][self.plain[0], 0] ^ 1),
            "color": lambda r: r.vattrs["color"].__setitem__((self.plain[0], 0), r.vattrs["color"][self.plain[0], 0] ^ 1),
            "land": lambda r: r.vattrs["land"].__setitem__((self.plain[0], 0), r.vattrs["land"][self.plain[0], 0] ^ 1),
            "eye": lambda r: r.vattrs["eye"].__setitem__((self.plain[0], 0), r.vattrs["eye"][self.plain[0], 0] ^ 1),
            "other": lambda r: r.vattrs["pad"].__setitem__((self.plain[0], 0), r.vattrs["pad"][self.plain[0], 0] ^ 1),
            "normal (not a seam vertex)": lambda r: r.vattrs["normal"].__setitem__((self.plain[0], 0), r.vattrs["normal"][self.plain[0], 0] ^ 1),
        }
        (self.home / "final.nif").write_bytes(b"stale candidate")
        for what, mutate in cases.items():
            res = self.raw(lambda r: (self.reskin(r), mutate(r)))
            self.assertEqual((res.status, res.code), ("REFUSED", REFUSED_UNEXPECTED), what)
            self.assertTrue(res.other_raw_differences or "seam" in res.reasons[0], what)
            self.assertFalse((self.home / "final.nif").exists(), what)  # no (stale) candidate survives a refusal

    def test_11_vertex_count_mismatch_refused(self):
        def m(r):
            keep = len(r.vertices) - 49
            r.vertices, r.weights, r.indices = r.vertices[:keep], r.weights[:keep], r.indices[:keep]
            r.vattrs = {k: v[:keep] for k, v in r.vattrs.items()}
            r.triangles = r.triangles[(r.triangles < keep).all(1)]
        res = self.raw(m)
        self.assertEqual(res.code, REFUSED_UNSAFE)
        self.assertTrue(any("counts" in x for x in res.reasons))

    def test_12_topology_mismatch_refused(self):
        res = self.raw(lambda r: setattr(r, "triangles", r.triangles[:, [0, 2, 1]]))
        self.assertEqual(res.code, REFUSED_UNSAFE)
        self.assertTrue(any("topology" in x for x in res.reasons))

    def test_13_incompatible_vertex_layout_refused(self):
        res = self.raw(lambda r: r.vattrs.pop("color"))
        self.assertEqual(res.code, REFUSED_UNSAFE)
        self.assertTrue(any("vertex layout" in x for x in res.reasons))

    def test_14_missing_shape_refused(self):
        home = self.home
        second = clone(self.src, "Second")
        write_nif(home / "src.nif", [self.src, second], BONE_POS)
        write_nif(home / "raw.nif", [clone(self.src)], BONE_POS)
        res = restore_immutable_vertices(home / "src.nif", home / "raw.nif", home / "final.nif", "CBBE-3BBB")
        self.assertEqual(res.code, REFUSED_UNSAFE)
        self.assertIn("shape set/order differs", res.reasons[0])

    def test_15_16_unexpected_shape_and_reference_leak_refused(self):
        extra = clone(self.src, "CBBE-3BBB")
        res = self.raw(self.reskin, extra=[extra])
        self.assertEqual(res.code, REFUSED_UNSAFE)
        self.assertTrue(any("reference body" in x for x in res.reasons))
        res = self.raw(self.reskin, extra=[clone(self.src, "Stray")])
        self.assertEqual(res.code, REFUSED_UNSAFE)

    def test_12b_segment_material_and_cloth_refused(self):
        self.assertEqual(self.raw(lambda r: setattr(r, "segments", b"OTHER" * 3)).code, REFUSED_UNSAFE)
        self.assertEqual(self.raw(lambda r: setattr(r, "shader", {"name": "Materials\\other.bgsm"})).code, REFUSED_UNSAFE)
        r = clone(self.src)
        write_nif(self.home / "raw.nif", [r], BONE_POS, cloth=[b"CLOTH" * 10])
        res = restore_immutable_vertices(self.home / "src.nif", self.home / "raw.nif", self.home / "final.nif")
        self.assertEqual(res.code, REFUSED_UNSAFE)
        self.assertTrue(any("cloth" in x for x in res.reasons))

    def test_19_verification_failure_leaves_no_candidate(self):
        def boom(path):
            raise ValueError("simulated crash while re-reading the candidate")
        with mock.patch.object(restore_mod, "read_nif", boom):
            res = self.raw(self.reskin)
        self.assertEqual((res.status, res.code), ("ERROR", "RESTORE_VERIFY_FAILED"))
        self.assertFalse((self.home / "final.nif").exists())
        self.assertFalse(list(self.home.glob("*.restore-tmp")))

    def test_raw_export_is_never_modified(self):
        def m(r):
            self.reskin(r)
            r.vattrs["normal"][self.seam, 1] ^= 0x20
        write_nif(self.home / "raw_probe.nif", [self.src], BONE_POS)
        res = self.raw(m)
        before = hashlib.sha256((self.home / "raw.nif").read_bytes()).hexdigest()
        self.assertEqual(res.raw_sha256, before)
        self.assertNotEqual(res.final_sha256, res.raw_sha256)


class RestorePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, cls.prof = setup()

    def test_20_21_seam_normal_export_is_restored_and_validated(self):
        bdir, s, fake = convert(self.ctl, ["attrs_F.nif"], per={"attrs_F.nif": "seam_normals"})
        r = rows(s)["attrs_F.nif"]
        self.assertEqual((r["status"], r["stage"]), ("VALIDATED", "RESTORED"))
        mdir = next(bdir.glob("meshes/*"))
        raw, fin = mdir / "generated_raw" / "attrs_F.nif", mdir / "generated" / "attrs_F.nif"
        self.assertTrue(raw.is_file() and fin.is_file())
        self.assertNotEqual(raw.read_bytes(), fin.read_bytes())  # 20: raw evidence and restored final both kept
        self.assertEqual([Path(x).name for _, x, _ in fake.direct_exports], ["attrs_F.nif"])
        self.assertIn("generated_raw", fake.direct_exports[0][1])
        res = json.loads((mdir / "result.json").read_text())
        rest = res["restoration"]
        self.assertEqual((rest["status"], rest["shapes"]["AttrTop"]["os_regenerated_normals"]), ("RESTORED", 4))
        self.assertEqual(res["generated_raw_sha256"], rest["raw_sha256"])
        checks = {c["name"]: c for c in res["validation"]["checks"]}
        for n in ("structure: AttrTop: normals identical", "structure: AttrTop: tangents and bitangents identical",
                  "post-export restoration: final immutable vertex bytes == source, skinning == raw export (byte-exact)"):
            self.assertTrue(checks[n]["ok"], (n, checks[n]))
        src = read_structure(mdir / "source" / "attrs_F.nif").shape("AttrTop").fields
        f = read_structure(fin).shape("AttrTop").fields
        rw = read_structure(raw).shape("AttrTop").fields
        self.assertTrue(np.array_equal(src["normal"], f["normal"]) and not np.array_equal(src["normal"], rw["normal"]))
        self.assertTrue(np.array_equal(rw["skin_weights"], f["skin_weights"]))
        # commit writes the restored final file and records the restoration
        self.ctl.commit_direct_batch(bdir.name)
        self.assertEqual(out_path(self.fi, "attrs_F.nif").read_bytes(), fin.read_bytes())
        man = json.loads((self.fi.mods / OUT / "manifest.json").read_text())
        rec = next(x for x in man["direct_conversions"] if x["game_key"] == "direct/attrs_f.nif")
        self.assertTrue(rec["restoration"]["applied"])
        self.assertEqual(rec["restoration"]["raw_export_sha256"], rest["raw_sha256"])
        self.assertEqual(rec["restoration"]["shapes"]["AttrTop"]["os_regenerated_normals"], 4)

    def test_19_22_geometry_mutation_is_not_hidden_and_never_committable(self):
        before = tree_digest(self.fi.mods / DIRECT_PACKAGE)
        for mode, check in (("move_vertex", "structure: TopShape: vertex positions identical"),
                            ("change_uv", None), ("offseam_normals", None), ("change_triangles", None)):
            mesh = "top_F.nif" if mode == "move_vertex" else "attrs_F.nif"
            bdir, s, _ = convert(self.ctl, [mesh], per={mesh: mode})
            r = rows(s)[mesh]
            self.assertEqual(r["status"], "FAILED", mode)
            self.assertEqual(r["stage"], "RAW_EXPORTED", mode)
            f = failed(bdir, mesh)
            self.assertTrue(any(x.startswith("post-export restoration:") for x in f), (mode, f))
            if check:
                self.assertIn(check, f)
            self.assertTrue(r["reasons"][0].startswith((REFUSED_UNEXPECTED, REFUSED_UNSAFE)), r["reasons"][0])
            mdir = next(bdir.glob("meshes/*"))
            self.assertFalse((mdir / "generated" / mesh).exists(), mode)  # nothing committable
            self.assertTrue((mdir / "generated_raw" / mesh).exists(), mode)  # the evidence is kept
            s2 = self.ctl.commit_direct_batch(bdir.name)
            self.assertEqual(rows(s2)[mesh]["status"], "FAILED")
        self.assertEqual(tree_digest(self.fi.mods / DIRECT_PACKAGE), before)

    def test_16_17_18_existing_protections_remain(self):
        for mode, check in (("embed_ref", "no CBBE-3BBB reference body in the output (WithRef=false)"),
                            ("rename_shape", "structure: shape set and order identical (no missing, added, renamed or reference shape)"),
                            ("add_cloth", "cloth physics data: none in source or output"),
                            ("lose_segment", "structure: TopShape: segment / tail data identical (BSSubIndexTriShape)")):
            bdir, s, _ = convert(self.ctl, ["top_F.nif"], per={"top_F.nif": mode})
            self.assertEqual(rows(s)["top_F.nif"]["status"], "FAILED", mode)
            f = failed(bdir, "top_F.nif")
            self.assertIn(check, f, mode)
            self.assertIn("post-export restoration: source ↔ export vertex correspondence proven (structural gate)", f, mode)
        plan = self.ctl.plan_direct_batch(DIRECT_PACKAGE, ["cloth_F.nif", "clash_F.nif"], write=False)
        self.assertEqual(len(plan.refused), 2)  # cloth REVIEW and the shape-name clash stay unselectable

    def test_legacy_batch_without_raw_split_is_revalidated_without_restoration(self):
        bdir, s, _ = convert(self.ctl, ["top_F.nif"])
        b = json.loads((bdir / "batch.json").read_text())
        mdir = next(bdir.glob("meshes/*"))
        # turn it into a 0.4.1-style workspace: the export in generated/, no raw folder, no restore flag
        (mdir / "generated" / "top_F.nif").unlink()
        (mdir / "generated_raw" / "top_F.nif").rename(mdir / "generated" / "top_F.nif")
        b.pop("restore")
        b["schema"] = 1
        (bdir / "batch.json").write_text(json.dumps(b))
        s2 = self.ctl.validate_direct_batch(bdir.name)
        self.assertEqual((rows(s2)["top_F.nif"]["status"], rows(s2)["top_F.nif"]["stage"]), ("VALIDATED", "LEGACY_NO_RESTORATION"))
        self.assertEqual((mdir / "generated" / "top_F.nif").stat().st_size > 0, True)


if __name__ == "__main__":
    unittest.main()
