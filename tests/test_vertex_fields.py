"""0.4.2 (Phase 4.1R): per-attribute vertex-buffer validation of Direct-NIF outputs.

The first real Windows batch (20260929-201925) FAILED on "geometry and vertex attributes identical … vertex data
DIFFERENT". Forensics (RESEARCH.md §12): skin weights/indices were already excluded; the differing bytes were the
normals, tangents and bitangents of 12 / 20 / 6 UV-seam vertices, recomputed by Outfit Studio's interactive
Mesh::SmoothNormals (seam welding, 60°) and written back by ExportNIF. The validator now decodes every vertex field on
its own, so a report names exactly the attribute that changed, while skinning stays the only mutable part.
"""
import unittest
from pathlib import Path

import numpy as np

from tests._util import temp_home
from cbbe3bbb.nif.structure import compare_structures, read_structure, vertex_layout
from synth import BONE_POS, SynthShape, write_nif
from test_direct_batch import convert, failed, rows, setup


class LayoutTests(unittest.TestCase):
    def test_real_easygirl_vertex_desc(self):
        # BSSubIndexTriShape of the real EasyGirl meshes: desc 0x45b0007065040a, 40-byte vertices
        lay = vertex_layout(0x45b0007065040a, 40)
        self.assertEqual(lay, {"position": (0, 12), "bitangent_x": (12, 16), "uv": (16, 20), "normal": (20, 23),
                               "bitangent_y": (23, 24), "tangent": (24, 27), "bitangent_z": (27, 28),
                               "skin_weights": (28, 36), "bone_indices": (36, 40)})

    def _pair(self, mutate):
        home = temp_home()
        rng = np.random.default_rng(1)
        n = 30
        v = rng.random((n, 3)) * 10
        tris = np.array([[i, i + 1, i + 2] for i in range(n - 2)])
        va = {"uv": rng.random((n, 2)).astype(np.float16).view(np.uint8).reshape(n, 4),
              "normal": rng.integers(0, 256, (n, 3), dtype=np.uint8), "tangent": rng.integers(0, 256, (n, 3), dtype=np.uint8),
              "color": rng.integers(0, 256, (n, 4), dtype=np.uint8)}
        w = np.zeros((n, 4), np.float32)
        w[:, 0] = 1
        idx = np.zeros((n, 4), np.uint8)
        a = SynthShape("S", v, tris, ["Pelvis", "SPINE1"], w, idx, vattrs=va)
        b = SynthShape("S", v.copy(), tris.copy(), ["Pelvis", "SPINE1"], w.copy(), idx.copy(),
                       vattrs={k: x.copy() for k, x in va.items()})
        mutate(b)
        write_nif(home / "a.nif", [a], BONE_POS)
        write_nif(home / "b.nif", [b], BONE_POS)
        res = compare_structures(read_structure(home / "a.nif"), read_structure(home / "b.nif"), {"S"})
        return {n_: ok for n_, ok, _ in res}

    def test_01_09_skin_change_alone_passes(self):
        def skin(b):
            b.weights = b.weights.copy()
            b.weights[:, 0], b.weights[:, 1] = 0.6, 0.4
            b.indices = b.indices.copy()
            b.indices[:, 1] = 1
        r = self._pair(skin)
        self.assertTrue(all(r.values()), [k for k, ok in r.items() if not ok])

    def test_02_08_each_immutable_attribute_is_checked_on_its_own(self):
        cases = {
            "S: vertex positions identical": lambda b: setattr(b, "vertices", b.vertices + np.r_[0.01, 0, 0]),
            "S: normals identical": lambda b: b.vattrs["normal"].__setitem__((4, 1), b.vattrs["normal"][4, 1] ^ 1),
            "S: UVs identical": lambda b: b.vattrs["uv"].__setitem__((4, 0), b.vattrs["uv"][4, 0] ^ 1),
            "S: tangents and bitangents identical": lambda b: b.vattrs["tangent"].__setitem__((4, 2), b.vattrs["tangent"][4, 2] ^ 1),
            "S: vertex colors identical": lambda b: b.vattrs["color"].__setitem__((4, 3), b.vattrs["color"][4, 3] ^ 1),
            "S: triangles / topology identical": lambda b: setattr(b, "triangles", b.triangles[:, [0, 2, 1]]),
        }
        for name, mutate in cases.items():
            r = self._pair(mutate)
            self.assertFalse(r[name], name)
            self.assertEqual([k for k, ok in r.items() if not ok], [name], name)

    def test_08_bytes_outside_every_known_field(self):
        def pad(b):
            b.vattrs["pad"] = np.full((len(b.vertices), 4), 7, np.uint8)
        # a vertex format that differs is caught by the format check; the same padding with other content by 'other'
        r = self._pair(pad)
        self.assertFalse(r["S: vertex/triangle counts and vertex format identical"])


class RealConditionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl, cls.fi, cls.prof = setup()

    def _one(self, mode, mesh="attrs_F.nif"):
        bdir, s, _ = convert(self.ctl, [mesh], per={mesh: mode} if mode else {})
        return rows(s)[mesh]["status"], failed(bdir, mesh), bdir

    def test_01_09_full_attribute_mesh_validates(self):
        status, f, bdir = self._one("")
        self.assertEqual(status, "VALIDATED", f)
        # the skinning did change (intended), every other field is byte-identical
        src = read_structure(next(bdir.glob("meshes/*/source/attrs_F.nif"))).shape("AttrTop")
        gen = read_structure(next(bdir.glob("meshes/*/generated/attrs_F.nif"))).shape("AttrTop")
        self.assertFalse(np.array_equal(src.fields["bone_indices"], gen.fields["bone_indices"]))
        for k in ("position", "bitangent_x", "uv", "normal", "bitangent_y", "tangent", "bitangent_z", "color", "eye", "other"):
            self.assertTrue(np.array_equal(src.fields[k], gen.fields[k]), k)

    def test_windows_seam_normal_recomputation_is_caught_on_the_raw_export(self):
        # 0.4.2 found it; 0.4.3 restores the source normals, so the final file validates (see test_restore.py),
        # while the same change on ordinary vertices is not an exporter effect and still FAILS precisely
        status, f, _ = self._one("offseam_normals")
        self.assertEqual(status, "FAILED")
        self.assertEqual(sorted(x for x in f if x.startswith("structure:")),
                         ["structure: AttrTop: normals identical", "structure: AttrTop: tangents and bitangents identical"])
        status, f, _ = self._one("seam_normals")
        self.assertEqual(status, "VALIDATED", f)

    def test_02_08_single_attribute_defects_fail(self):
        for mode, check in (("move_vertex", "vertex positions identical"), ("change_normal", "normals identical"),
                            ("change_uv", "UVs identical"), ("change_tangent", "tangents and bitangents identical"),
                            ("change_color", "vertex colors identical"), ("change_triangles", "triangles / topology identical"),
                            ("change_eye", "eye vertex data identical"),
                            ("change_other", "other vertex bytes (not a known field) identical")):
            status, f, _ = self._one(mode)
            self.assertEqual(status, "FAILED", mode)
            self.assertIn(f"structure: AttrTop: {check}", f, mode)

    def test_10_unintended_skin_weight_change_fails_weight_validation(self):
        status, f, _ = self._one("skin_scrambled")
        self.assertNotEqual(status, "VALIDATED")
        self.assertIn("AttrTop: matches the Copy Bone Weights prediction", f)
        self.assertIn("AttrTop: predicted vs actual physics weights consistent", f)
        self.assertFalse(any(x.startswith("structure:") for x in f), f)  # geometry itself is intact

    def test_11_zero_weight_vertex_still_fails(self):
        status, f, _ = self._one("zero_weight")
        self.assertEqual(status, "FAILED")
        self.assertIn("AttrTop: weights normalized, no vertex without weight", f)

    def test_12_shape_segment_material_protections_remain(self):
        for mode, check in (("lose_segment", "structure: AttrTop: segment / tail data identical (BSSubIndexTriShape)"),
                            ("lose_material", "structure: AttrTop: material / shader and textures identical"),
                            ("rename_shape", "structure: shape set and order identical (no missing, added, renamed or reference shape)"),
                            ("embed_ref", "no CBBE-3BBB reference body in the output (WithRef=false)")):
            status, f, _ = self._one(mode)
            self.assertEqual(status, "FAILED", mode)
            self.assertIn(check, f, mode)


if __name__ == "__main__":
    unittest.main()
