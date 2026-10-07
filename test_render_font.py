import contextlib
import csv
import io
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from render_font import (
    AutoKerning, compose, horizontal_ink_metrics, layout_line, load_glyphs, render,
)


EXAMPLE_TEXT = """THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG.
PACK MY BOX WITH FIVE DOZEN LIQUOR JUGS!
LOOK! A QUIET KOALA WALKS ALONG THE QUAY.
AVATAR / WAVY / TYPOGRAPHY / MINIMUM / 0123456789
RED2 FONT: VERSION 1.0 | STATUS: READY
PRICE: $12.50 + 8% TAX = $13.50
(ROUND) [SQUARE] {CURLY} <ANGLE> @HOME #42
"HELLO, WORLD!" & 'GOODBYE'; A_B ~ C^D * E?"""


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
                error = abs(min(distances)**0.5 - 1 - spacing)
                for candidate in (advance - 1, advance + 1):
                    alternative = min((candidate + yy - y)**2 + (yy-y)**2
                                      for y in range(7) for yy in range(7))
                    self.assertLessEqual(error, abs(alternative**0.5 - 1 - spacing))

    def test_nearest_gap_may_be_below_target(self):
        diagonal = glyph(["#....", ".#...", "..#..", "...#.", "....#"])
        kern = AutoKerning({65: diagonal}, spacing=2)
        # Advance 4 gives sqrt(8)-1 = 1.828; advance 5 gives sqrt(13)-1 = 2.606.
        self.assertEqual(kern.advance(65, 65), 4)

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

    def test_small_right_glyph_tucks_flush_with_left_glyph(self):
        glyphs = {ord('P'): glyph(['###########'] + ['#..........'] * 11),
                  ord('.'): glyph(['.'] * 11 + ['#']),
                  ord('O'): glyph(['###'] * 12)}
        for spacing in (0, 1, 2, 4):
            with self.subTest(spacing=spacing):
                kern = AutoKerning(glyphs, spacing)
                direct = kern.advance(ord('P'), ord('O'))
                placements = layout_line(map(ord, 'P.O.'), kern)
                self.assertEqual(placements[1][1], kern.advance(ord('P'), ord('.')))
                self.assertEqual(placements[1][1], glyphs[ord('P')].width - glyphs[ord('.')].width)
                self.assertGreaterEqual(placements[2][1], direct)
                # Check the actual pixels, including the nonadjacent P and O.
                occupied = set()
                for code, x in placements:
                    image = glyphs[code]
                    pixels = {(x + xx, yy) for yy in range(image.height)
                              for xx in range(image.width) if image.getpixel((xx, yy))[3]}
                    self.assertFalse(occupied & pixels)
                    occupied.update(pixels)
                image = compose('P.O.', glyphs, kern)
                self.assertEqual(sum(image.getpixel((x, y))[:3] == (255, 255, 255)
                                     for y in range(image.height) for x in range(image.width)),
                                 len(occupied))

    def test_repeated_marks_advance_instead_of_stacking(self):
        glyphs = {ord('P'): glyph(['#####'] + ['#....'] * 11),
                  ord('.'): glyph(['.'] * 11 + ['#']),
                  ord('O'): glyph(['###'] * 12)}
        placements = layout_line(map(ord, 'P...O'), AutoKerning(glyphs, spacing=0))
        self.assertEqual([x for _, x in placements], [0, 4, 5, 6, 7])

    def test_horizontal_ink_metrics_count_pixels_not_box_width_or_rows(self):
        # Most of the right ink is in column zero, although the box is wide.
        right = glyph(['#.......'] * 9 + ['.......#'])
        self.assertEqual(horizontal_ink_metrics(right), (8, 0))

    def test_horizontal_ink_metrics_handle_odd_even_counts_and_margins(self):
        for rows, median in ((['#.......', '......##'], 6),
                             (['##......', '......##'], 6),
                             (['..#.....', '....##..'], 4)):
            with self.subTest(rows=rows):
                right = glyph(rows)
                self.assertEqual(horizontal_ink_metrics(right)[1], median)
        self.assertIsNone(horizontal_ink_metrics(glyph(['....'])))

    def test_layout_keeps_spaces_and_resets_at_line_breaks(self):
        glyphs = {ord('A'): glyph(['##']), ord(' '): glyph(['....'])}
        kern = AutoKerning(glyphs, spacing=3)
        self.assertEqual(layout_line(map(ord, 'A A'), kern), [(65, 0), (32, 5), (65, 12)])
        self.assertEqual(layout_line([], kern), [])
        self.assertEqual(compose('A A\nA', glyphs, kern).size, (14, 6))

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
                             [("!", b) for b in "!0A"])
            self.assertTrue(all(int(row["kerning_px"]) != 0 for row in rows))
            with Image.open(output) as image:
                self.assertEqual(image.height, 11)
                self.assertEqual(image.getchannel("A").getextrema(), (255, 255))

    def test_empty_output(self):
        image = compose("", {}, AutoKerning({}))
        self.assertEqual(image.size, (1, 1))
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 0, 255))


class ExampleRenderTest(unittest.TestCase):
    def test_real_font_matches_approved_punctuation_placements(self):
        directory = Path(__file__).resolve().parent
        glyphs = load_glyphs(directory / 'RED2 Font.aseprite', 'aseprite')
        kern = AutoKerning(glyphs)
        approved = {'P.': [0, 11], 'F.': [0, 11], 'L.': [0, 18],
                    "L'.": [0, 11, 18], 'P.O.': [0, 11, 18, 39], 'F.T': [0, 11, 18]}
        for text, expected in approved.items():
            with self.subTest(text=text):
                placements = layout_line(map(ord, text), kern)
                self.assertEqual([x for _, x in placements], expected)
        for left in glyphs:
            for right in glyphs:
                with self.subTest(left=chr(left), right=chr(right)):
                    self.assertGreaterEqual(kern.advance(left, right) + glyphs[right].width,
                                            glyphs[left].width)

    def test_repeated_and_partially_overlapping_marks_respect_all_pair_bounds(self):
        directory = Path(__file__).resolve().parent
        glyphs = load_glyphs(directory / 'RED2 Font.aseprite', 'aseprite')
        kern = AutoKerning(glyphs)
        for text in ('T.T', 'P...O', "L''.", "L'.T", 'F.-T', 'P. O', 'P.4', 'PO' * 100):
            with self.subTest(text=text):
                placements = layout_line(map(ord, text), kern)
                for index, (right, x) in enumerate(placements):
                    for left, left_x in placements[:index]:
                        self.assertGreaterEqual(x, left_x + kern.advance(left, right))

    def test_example_render(self):
        """Render the real font sample and leave example.png for the README."""
        directory = Path(__file__).resolve().parent
        output = directory / "example.png"
        warnings = io.StringIO()
        with contextlib.redirect_stderr(warnings):
            size = render(EXAMPLE_TEXT, directory / "RED2 Font.aseprite",
                          output, "aseprite")
        self.assertEqual(warnings.getvalue(), "", "Sample text has missing glyphs")
        with Image.open(output) as image:
            self.assertEqual(image.size, size)
            self.assertGreater(image.width, 1)
            self.assertGreater(image.height, 1)
            self.assertEqual(image.getchannel("A").getextrema(), (255, 255))
            self.assertIsNotNone(image.convert("RGB").getbbox(), "Sample has no glyph pixels")


if __name__ == "__main__":
    unittest.main()
