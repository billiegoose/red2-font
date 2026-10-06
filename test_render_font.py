import contextlib
import csv
import io
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from render_font import AutoKerning, compose, render


def glyph(rows):
    image = Image.new("RGBA", (len(rows[0]), len(rows)))
    for y, row in enumerate(rows):
        for x, pixel in enumerate(row):
            if pixel == "#":
                image.putpixel((x, y), (255, 255, 255, 255))
    return image


class KerningTests(unittest.TestCase):
    def test_default_spacing_is_four(self):
        glyphs = {65: glyph(["##"] * 3)}
        self.assertEqual(AutoKerning(glyphs).spacing, 4)
        self.assertEqual(AutoKerning(glyphs).advance(65, 65), 6)

    def test_straight_walls_keep_target_gap(self):
        glyphs = {65: glyph(["##"] * 8)}
        self.assertEqual(AutoKerning(glyphs, spacing=3).advance(65, 65), 5)

    def test_sloping_edges_tighten_without_touching(self):
        a = glyph(["#....", ".#...", "..#..", "...#.", "....#"])
        b = a
        kern = AutoKerning({65: a, 66: b}, spacing=3)
        advance = kern.advance(65, 66)
        self.assertEqual(advance, 6)
        left = {(x, y) for y in range(a.height) for x in range(a.width)
                if a.getpixel((x, y))[3]}
        right = {(x + advance, y) for y in range(b.height) for x in range(b.width)
                 if b.getpixel((x, y))[3]}
        self.assertTrue(all((x-u)**2 + (y-v)**2 >= 16
                            for x, y in left for u, v in right))

    def test_no_shared_rows_keep_nominal_spacing(self):
        glyphs = {65: glyph(["##", ".."]), 66: glyph(["..", "##"])}
        self.assertEqual(AutoKerning(glyphs, spacing=3).advance(65, 66), 5)

    def test_configurable_gap_and_radius(self):
        diagonal = glyph(["#......", ".#.....", "..#....", "...#...",
                          "....#..", ".....#.", "......#"])
        for spacing in (2, 3, 4):
            with self.subTest(spacing=spacing):
                kern = AutoKerning({65: diagonal}, spacing)
                advance = kern.advance(65, 65)
                distances = [(advance + yy - y)**2 + (yy - y)**2
                             for y in range(7) for yy in range(7)]
                self.assertGreaterEqual(min(distances), (spacing + 1)**2)
                closer = [(advance - 1 + yy - y)**2 + (yy - y)**2
                          for y in range(7) for yy in range(7)]
                self.assertLess(min(closer), (spacing + 1)**2)

    def test_zero_spacing_allows_adjacent_pixels(self):
        glyphs = {65: glyph(["##"] * 3)}
        kern = AutoKerning(glyphs, spacing=0)
        self.assertEqual(kern.advance(65, 65), 2)
        image = compose("AA", glyphs, kern)
        self.assertEqual(image.size, (4, 3))
        self.assertTrue(all(image.getpixel((x, 0)) == (255, 255, 255, 255)
                            for x in range(4)))

    def test_space_keeps_width(self):
        glyphs = {65: glyph(["##"]), 32: glyph(["...."])}
        kern = AutoKerning(glyphs, spacing=3)
        self.assertEqual(kern.advance(32, 65), 7)

    def test_skip_warning_and_line_break(self):
        glyphs = {65: glyph(["##", "##"])}
        warnings = io.StringIO()
        with contextlib.redirect_stderr(warnings):
            image = compose("A?A\nA", glyphs, AutoKerning(glyphs, spacing=3))
        self.assertEqual(image.size, (7, 8))
        self.assertIn("'?'", warnings.getvalue())
        self.assertNotIn("'\\n'", warnings.getvalue())

    def test_pairs_include_all_glyphs_without_space_slice(self):
        glyphs = {65: glyph(["##"]), 48: glyph(["##"]), 33: glyph(["#."])}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pairs.png"
            with patch("render_font.load_glyphs", return_value=glyphs) as load:
                with contextlib.redirect_stdout(io.StringIO()):
                    render("", Path("font.aseprite"), output, "aseprite", pairs=True)
                self.assertEqual(load.call_args.args[2], "Glyphs")
            with output.with_name("kerning_pairs.csv").open() as file:
                rows = list(csv.DictReader(file))
            self.assertEqual([(row["left"], row["right"]) for row in rows],
                             [(a, b) for a in "!0A" for b in "!0A"])
            with Image.open(output) as image:
                self.assertEqual(image.height, 11)
                self.assertEqual(image.getchannel("A").getextrema(), (255, 255))

    def test_empty_output(self):
        image = compose("", {}, AutoKerning({}))
        self.assertEqual(image.size, (1, 1))
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 0, 255))


if __name__ == "__main__":
    unittest.main()
