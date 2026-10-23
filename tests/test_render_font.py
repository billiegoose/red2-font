import contextlib
import csv
import io
import random
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from src.specimen import EXAMPLE_TEXT

from src.font import (
    AutoKerning, compose, horizontal_ink_metrics, layout_line, load_glyphs,
    GlyphCollection, parse_slice_codepoint, parse_slice_userdata, render, validate_slices,
)




def glyph(rows):
    image = Image.new("RGBA", (len(rows[0]), len(rows)))
    for y, row in enumerate(rows):
        for x, pixel in enumerate(row):
            if pixel == "#":
                image.putpixel((x, y), (255, 255, 255, 255))
    return image


class UnicodeSliceTests(unittest.TestCase):
    def test_parse_decimal_and_u_plus_unicode_notation(self):
        cases = {
            "c=65": 65,
            "c = 65": 65,
            "c=U+25B2": 0x25B2,
            "c=U+25B6": 0x25B6,
            "c=U+25BC": 0x25BC,
            "c=U+25C0": 0x25C0,
            "U+25B2": 0x25B2,
            " c = u+25b2 ": 0x25B2,
            "U+1F600": 0x1F600,
        }
        for value, code in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_slice_codepoint(value), code)

    def test_multiline_kerning_userdata(self):
        self.assertEqual(parse_slice_userdata('c=U+2500\nk=0'), (0x2500, True))
        self.assertEqual(parse_slice_userdata('k=0\n U+253C '), (0x253C, True))
        self.assertEqual(parse_slice_userdata('c=65\nk=1'), (65, False))
        self.assertEqual(parse_slice_userdata('c=65'), (65, False))
        self.assertEqual(parse_slice_codepoint('c=U+251C\nk=0'), 0x251C)
        for value in ('c=65\nk=2', 'c=65\nk=off', 'c=65\nk=0\nk=1',
                      'c=65\nc=66', 'k=0', 'c=65\nunknown=1'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_slice_userdata(value)

    def test_rejects_malformed_out_of_range_surrogates_and_duplicates(self):
        for value in ("", "65", "c=", "c=0x25B2", "U+NOTHEX", "c=U+25B2garbage"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_slices([{"name": "invalid", "data": value}])
        for value in ("U+110000", "c=55296", "U+D800", "c=1114112"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "invalid code point"):
                    validate_slices([{"name": "invalid", "data": value}])
        with self.assertRaisesRegex(ValueError, "Duplicate U\\+25B2"):
            validate_slices([
                {"name": "first", "data": "U+25B2"},
                {"name": "second", "data": "c=U+25B2"},
            ])
        with self.assertRaisesRegex(ValueError, "Duplicate U\\+0041"):
            validate_slices([
                {"name": "ASCII", "data": "c=65"},
                {"name": "Unicode", "data": "c=U+0041"},
            ])

    def test_load_and_render_all_four_real_unicode_triangles(self):
        glyphs = load_glyphs(Path(__file__).resolve().parents[1] / "RED2 Font.aseprite", "aseprite")
        triangles = "▲▶▼◀"
        self.assertTrue(all(ord(character) in glyphs for character in triangles))
        self.assertEqual(len(glyphs), 85)  # 84 mapped characters plus .notdef (c=0).
        fixed_codes = {ord(char) for char in '─│┌┐└┘├┤┬┴┼▲▶▼◀'}
        self.assertEqual(set(glyphs.fixed_advances), fixed_codes)
        self.assertEqual({glyphs.fixed_advances[code] for code in fixed_codes}, {17})
        self.assertEqual({glyphs[code].width for code in fixed_codes}, {17})
        kerning = AutoKerning(glyphs)
        for left in fixed_codes:
            for right in fixed_codes:
                self.assertEqual(kerning.advance(left, right), 17)
        self.assertIn(0, glyphs)
        self.assertIsNotNone(glyphs[0].getchannel("A").getbbox())
        warnings = io.StringIO()
        with contextlib.redirect_stderr(warnings):
            image = compose("A▲▶▼◀Z", glyphs, AutoKerning(glyphs))
        self.assertEqual(warnings.getvalue(), "")
        self.assertEqual(image.height, 32)
        self.assertGreater(image.width, 0)
        self.assertTrue(image.getchannel("R").getextrema()[1] > 0)


class KerningTests(unittest.TestCase):
    def test_k0_fixed_cell_bypasses_optical_kerning_in_both_directions(self):
        # Keep cell margins as supplied by Aseprite; do not ink-trim them.
        glyphs = GlyphCollection({
            65: glyph(['####'] * 2),
            0x2500: glyph(['#####'] * 2),
            0x2502: glyph(['..#..'] * 2),
        }, fixed_advances={0x2500: 5, 0x2502: 5})
        kern = AutoKerning(glyphs)
        self.assertEqual(kern.advance(0x2500, 0x2502), 5)
        self.assertEqual(kern.advance(0x2502, 0x2500), 5)
        self.assertEqual(kern.advance(65, 0x2500), 8)
        self.assertEqual(kern.advance(0x2500, 65), 5)
        self.assertEqual(kern.advance(65, 0x2502), 8)
        self.assertEqual(kern.advance(0x2502, 65), 5)
        self.assertEqual([x for _, x in layout_line([0x2500, 0x2502, 0x2500], kern)],
                         [0, 5, 10])
        self.assertEqual([x for _, x in layout_line([65, 0x2500, 0x2502, 65], kern)],
                         [0, 8, 13, 18])

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

    def test_right_left_edge_cannot_precede_left_left_edge(self):
        comma = glyph(['...'] * 10 + ['###'] * 2)
        four = glyph(['#........'] + ['........#'] * 11)
        # The bottom ink allows geometric backtracking, while the right glyph's
        # top-left pixel makes that backtracking visibly reverse its left edge.
        for spacing in (0, 1, 2, 4):
            with self.subTest(spacing=spacing):
                kern = AutoKerning({44: comma, 52: four}, spacing)
                self.assertEqual(kern.advance(44, 52), 0)
                self.assertEqual(layout_line(map(ord, ',4'), kern), [(44, 0), (52, 0)])

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

    def test_notdef_replaces_missing_characters_with_normal_kerning(self):
        glyphs = {65: glyph(["##", "##"]), 0: glyph([".#", "##"])}
        kern = AutoKerning(glyphs)
        warnings = io.StringIO()
        with contextlib.redirect_stderr(warnings):
            image = compose("A?A\n☃", glyphs, kern)
        self.assertEqual(image.size, (14, 8))
        # The unknown '?' is placed at x=6, between the two A glyphs.
        self.assertEqual(image.getpixel((7, 0)), (255, 255, 255, 255))
        self.assertEqual(image.getpixel((6, 0)), (0, 0, 0, 255))
        self.assertEqual(image.getpixel((0, 6)), (0, 0, 0, 255))
        self.assertEqual(image.getpixel((1, 6)), (255, 255, 255, 255))
        self.assertEqual(compose("?", glyphs, kern).size, (2, 2))
        self.assertIn("replacing unrecognized character '?'", warnings.getvalue())
        self.assertIn("replacing unrecognized character '☃'", warnings.getvalue())
        self.assertNotIn("'\\n'", warnings.getvalue())

    def test_skip_warning_and_line_break(self):
        glyphs = {65: glyph(["##", "##"])}
        warnings = io.StringIO()
        with contextlib.redirect_stderr(warnings):
            image = compose("A?A\nA", glyphs, AutoKerning(glyphs, spacing=3))
        self.assertEqual(image.size, (7, 8))
        self.assertIn("'?'", warnings.getvalue())
        self.assertNotIn("'\\n'", warnings.getvalue())

    def test_notdef_is_excluded_from_unicode_pair_preview_and_csv(self):
        glyphs = {0: glyph(["###"]), 65: glyph(["##"]), 66: glyph(["#."])}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pairs.png"
            with patch("src.font.load_glyphs", return_value=glyphs):
                with contextlib.redirect_stdout(io.StringIO()):
                    render("", Path("font.aseprite"), output, "aseprite", pairs=True, kerning_slack=0)
            with output.with_name("kerning_pairs.csv").open() as file:
                csv_content = file.read()
            self.assertNotIn("\\x00", csv_content)
            self.assertNotIn(".notdef", csv_content)
            with Image.open(output) as image:
                self.assertEqual(image.height, 1 * (1 + 4) + 1)

    def test_pairs_include_all_glyphs_without_space_slice(self):
        glyphs = {65: glyph(["##"]), 48: glyph(["##"]), 33: glyph(["#."])}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pairs.png"
            with patch("src.font.load_glyphs", return_value=glyphs) as load:
                with contextlib.redirect_stdout(io.StringIO()):
                    render("", Path("font.aseprite"), output, "aseprite", pairs=True, kerning_slack=0)
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
    def test_recent_queue_preserves_constraints_beyond_two_glyphs(self):
        directory = Path(__file__).resolve().parents[1]
        glyphs = load_glyphs(directory / 'RED2 Font.aseprite', 'aseprite')
        examples = ((0, '"//_', [0, 0, 3, 7]),
                    (1, '#``@', [0, 7, 12, 13]),
                    (2, '+``+', [0, 6, 12, 13]),
                    (3, '/,<T', [0, 10, 11, 16]))
        for spacing, text, expected in examples:
            with self.subTest(spacing=spacing, text=text):
                placements = layout_line(map(ord, text), AutoKerning(glyphs, spacing))
                self.assertEqual([x for _, x in placements], expected)

    def test_recent_queue_matches_all_prior_glyphs_across_spacings(self):
        directory = Path(__file__).resolve().parents[1]
        glyphs = load_glyphs(directory / 'RED2 Font.aseprite', 'aseprite')
        rng = random.Random(0)
        samples = [list(map(ord, text)) for text in
                   ('P...O', "L''.T", '#,,,4', 'P. O', 'PO' * 100, EXAMPLE_TEXT.replace('\n', ''))]
        samples.extend(rng.choices(sorted(glyphs), k=60) for _ in range(20))
        for spacing in (0, 1, 2, 3, 4, 5, 8):
            kern = AutoKerning(glyphs, spacing)
            for codes in samples:
                with self.subTest(spacing=spacing, text=''.join(map(chr, codes))):
                    # Reference placement keeps all history, with no pruning.
                    expected = []
                    for code in codes:
                        x = max((left_x + kern.advance(left, code)
                                 for left, left_x in expected), default=0)
                        expected.append((code, x))
                    self.assertEqual(layout_line(iter(codes), kern), expected)

    def test_real_font_matches_approved_punctuation_placements(self):
        directory = Path(__file__).resolve().parents[1]
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
                    self.assertGreaterEqual(kern.advance(left, right), 0)
                    self.assertGreaterEqual(kern.advance(left, right) + glyphs[right].width,
                                            glyphs[left].width)

    def test_comma_sequences_keep_left_edges_in_reading_order(self):
        directory = Path(__file__).resolve().parents[1]
        glyphs = load_glyphs(directory / 'RED2 Font.aseprite', 'aseprite')
        kern = AutoKerning(glyphs)
        expected = {'#,4': [0, 12, 16], '#,,4': [0, 12, 19, 19],
                    '#,,,4': [0, 12, 19, 26, 26]}
        self.assertEqual(kern.advance(ord(','), ord('4')), 0)
        for text, origins in expected.items():
            with self.subTest(text=text):
                placements = layout_line(map(ord, text), kern)
                self.assertEqual([x for _, x in placements], origins)
                self.assertEqual(origins, sorted(origins))
                occupied = set()
                for code, origin in placements:
                    image = glyphs[code]
                    pixels = {(origin + x, y) for y in range(image.height)
                              for x in range(image.width) if image.getpixel((x, y))[3]}
                    self.assertFalse(occupied & pixels)
                    occupied.update(pixels)

    def test_repeated_and_partially_overlapping_marks_respect_all_pair_bounds(self):
        directory = Path(__file__).resolve().parents[1]
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
        directory = Path(__file__).resolve().parents[1]
        output = directory / "example.png"
        warnings = io.StringIO()
        with contextlib.redirect_stderr(warnings):
            size = render(EXAMPLE_TEXT, directory / "RED2 Font.aseprite",
                          output, "aseprite", kerning_slack=0)
        self.assertEqual(warnings.getvalue(), "", "Sample text has missing glyphs")
        with Image.open(output) as image:
            self.assertEqual(image.size, size)
            self.assertGreater(image.width, 1)
            self.assertGreater(image.height, 1)
            self.assertEqual(image.getchannel("A").getextrema(), (255, 255))
            self.assertIsNotNone(image.convert("RGB").getbbox(), "Sample has no glyph pixels")


if __name__ == "__main__":
    unittest.main()
