"""Compatibility checks for the selected glyph transformation."""

from pathlib import Path
import unittest

from PIL import Image

from antialias_font import (
    FITTED_PALETTE, antialias_coverage, antialias_glyph, compile_glyph, quantize_2bit,
)


class AntialiasFontTests(unittest.TestCase):
    def test_matches_selected_preview(self):
        root = Path(__file__).resolve().parent
        # Isolated first phrase with black separation from other text; exercising
        # contour refinement as well as quantization against the chosen artifact.
        box = (0, 0, 175, 26)
        with Image.open(root / "example.png") as source:
            actual = antialias_glyph(source.convert("L").crop(box))
        with Image.open(root / "previews/antialiasing-2bit-fitted.png") as selected:
            expected = selected.convert("RGB").crop(box)
        self.assertEqual(actual.convert("RGB").tobytes(), expected.tobytes())

    def test_blank_rgba_glyph_preserves_width_and_height(self):
        blank = Image.new("RGBA", (9, 32), (255, 255, 255, 0))
        compiled = compile_glyph(blank)
        self.assertEqual(compiled.size, (9, 32))
        self.assertEqual(compiled.getextrema(), (0, 0))

    def test_occupancy_uses_alpha_including_opaque_black(self):
        glyph = Image.new("RGBA", (8, 8), (255, 255, 255, 0))
        for y in range(2, 6):
            for x in range(2, 6):
                glyph.putpixel((x, y), (0, 0, 0, 1))
        compiled = compile_glyph(glyph)
        self.assertEqual(compiled.getpixel((3, 3)), 3)
        self.assertEqual(compiled.getpixel((0, 0)), 0)
        self.assertEqual(compiled.size, glyph.size)

    def test_straight_stroke_is_solid_away_from_corners(self):
        mask = Image.new("L", (12, 12), 0)
        mask.paste(255, (4, 0, 8, 12))
        coverage = antialias_coverage(mask)
        self.assertEqual([coverage.getpixel((x, 6)) for x in range(12)],
                         [0] * 4 + [255] * 4 + [0] * 4)

    def test_selected_quantization(self):
        coverage = Image.new("L", (10, 1))
        coverage.putdata([0, 44, 60, 64, 84, 191, 211, 223, 239, 255])
        compiled = quantize_2bit(coverage)
        self.assertEqual(list(compiled.tobytes()), [0, 1, 1, 1, 1, 2, 2, 2, 3, 3])
        self.assertEqual(compiled.getpalette()[:12],
                         [value for gray in FITTED_PALETTE for value in (gray,) * 3])

    def test_grayscale_input_is_rejected_before_smoothing(self):
        with self.assertRaises(ValueError):
            antialias_coverage(Image.new("L", (4, 4), 128))


if __name__ == "__main__":
    unittest.main()
