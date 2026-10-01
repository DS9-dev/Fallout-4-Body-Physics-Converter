import unittest

from tests import _util  # noqa: F401
from cbbe3bbb.analysis.models import ShapeAnalysis
from cbbe3bbb.classification.rules import (
    ACCESSORY_WORDS, DECLARED_3BBB, LOOSE_WORDS, OUTERWEAR_WORDS, _hit, garment_risks, name_tokens,
)


class NameTokenTests(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(name_tokens("CP5_DonEb14n jacket v3") >= {"cp", "don", "eb", "jacket", "v"}, True)
        self.assertIn("boots", name_tokens("RestrictiveBootsBlack"))
        self.assertIn("strap", name_tokens("Strap-on"))

    def test_whole_word_matching(self):
        self.assertEqual(_hit(OUTERWEAR_WORDS, "Vtaw Wardrobe 5 Bodysuit"), "")  # not 'robe'
        self.assertEqual(_hit(OUTERWEAR_WORDS, "Leather Robe"), "robe")
        self.assertEqual(_hit(LOOSE_WORDS, "Miniskirt"), "miniskirt")
        self.assertEqual(_hit(ACCESSORY_WORDS, "NippleClampBlack"), "clamp")

    def test_declared_3bbb(self):
        for s in ("Top - 3BBB", "3BBB TWB Vtaw", "VtawWardrobe8TWB-3B-Conversion", "Outfit 3B"):
            self.assertTrue(DECLARED_3BBB.search(s), s)
        for s in ("CP5_DonEb14n jacket v3", "Raider Punk Top V2 - CBBE", "BB3 thing"):
            self.assertFalse(DECLARED_3BBB.search(s), s)

    def test_garment_risks(self):
        shapes = [ShapeAnalysis(name="JacketShape", target="JacketShape")]
        review, info = garment_risks("Test Jacket", shapes, {"JacketShape"}, True)
        self.assertTrue(any("outerwear" in r for r in review))
        review, info = garment_risks("Tight Skirt", [ShapeAnalysis(name="Skirt", target="Skirt")], {"Skirt"}, True)
        self.assertEqual(review, [])
        self.assertTrue(info)
        review, _ = garment_risks("Loose Skirt", [ShapeAnalysis(name="Skirt", target="Skirt")], {"Skirt"}, False)
        self.assertTrue(review)
        many = [ShapeAnalysis(name=f"L{i}", target=f"L{i}") for i in range(4)]
        review, _ = garment_risks("Layers", many, {s.name for s in many}, True)
        self.assertTrue(any("layered" in r for r in review))


if __name__ == "__main__":
    unittest.main()
