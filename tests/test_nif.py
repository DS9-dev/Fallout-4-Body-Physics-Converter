import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tests import _util  # noqa: F401  (sys.path)
from cbbe3bbb.nif.reader import NifError, read_nif, read_nif_bytes
from synth import BONE_POS, SynthShape, make_body, outfit_patch, write_nif


class NifReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_roundtrip_skinned_shape(self):
        body = make_body(True)
        shp = outfit_patch(body, (90, 100), copy_weights=True, name="Top")
        p = self.tmp / "a.nif"
        write_nif(p, [shp], BONE_POS)
        nif = read_nif(p)
        self.assertTrue(nif.header.is_fallout4)
        self.assertEqual(len(nif.shapes), 1)
        s = nif.shapes[0]
        self.assertEqual(s.name, "Top")
        self.assertEqual(s.num_vertices, len(shp.vertices))
        self.assertEqual(len(s.triangles), len(shp.triangles))
        self.assertTrue(s.skinned and s.has_skin_block)
        self.assertEqual(s.bones, shp.bones)
        np.testing.assert_allclose(s.world_vertices(), shp.vertices, atol=1e-4)
        bones, w = s.weight_matrix()
        self.assertEqual(w.shape, (len(shp.vertices), len(bones)))
        np.testing.assert_allclose(w.sum(axis=1), 1.0, atol=2e-3)
        self.assertIn("Chest", nif.node_names)

    def test_skin_to_global_from_bones(self):
        # bones placed at non-zero positions with inverse bind data -> identity skin transform
        shp = SynthShape("S", np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]], float), np.array([[0, 1, 2]]),
                         ["Pelvis"], np.array([[1, 0, 0, 0]] * 3, float), np.zeros((3, 4), int), translation=(0, 0, 100))
        p = self.tmp / "b.nif"
        write_nif(p, [shp], {"Pelvis": (3, 4, 50)})
        s = read_nif(p).shapes[0]
        np.testing.assert_allclose(s.skin_to_global, np.eye(4), atol=1e-6)
        # the NiAVObject translation of a skinned shape is ignored
        np.testing.assert_allclose(s.world_vertices(), shp.vertices, atol=1e-5)

    def test_unskinned_shape_uses_node_transform(self):
        shp = SynthShape("U", np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float), np.array([[0, 1, 2]]), translation=(0, 0, 10))
        p = self.tmp / "c.nif"
        write_nif(p, [shp])
        s = read_nif(p).shapes[0]
        self.assertFalse(s.skinned)
        self.assertAlmostEqual(float(s.world_vertices()[0, 2]), 10.0, places=5)

    def test_not_a_nif(self):
        with self.assertRaises(NifError):
            read_nif_bytes(b"hello world\nxxxxxxxxxxxxxxxxxxxxxxxx")

    def test_truncated(self):
        body = make_body(False)
        p = self.tmp / "d.nif"
        write_nif(p, [outfit_patch(body, (0, 10), name="X")], BONE_POS)
        data = p.read_bytes()
        with self.assertRaises(NifError):
            read_nif_bytes(data[: len(data) // 2])

    def test_wrong_version(self):
        data = b"Gamebryo File Format, Version 20.0.0.5\n" + struct.pack("<I", 0x14000005) + b"\x01" + b"\x00" * 64
        with self.assertRaises(NifError):
            read_nif_bytes(data)

    def test_skyrim_header_flagged_not_fo4(self):
        body = make_body(False)
        p = self.tmp / "e.nif"
        write_nif(p, [outfit_patch(body, (0, 10), name="X")], BONE_POS, bs_version=100)
        nif = read_nif(p)
        self.assertFalse(nif.header.is_fallout4)

    def test_missing_file(self):
        with self.assertRaises(NifError):
            read_nif(self.tmp / "nope.nif")


if __name__ == "__main__":
    unittest.main()
