"""Regression tests for the Aseprite-to-OpenType web-font compiler."""

import io
import random
import tempfile
import unittest
from pathlib import Path

import uharfbuzz as hb
from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont

from src.web_font import (
    UNITS_PER_PIXEL, build_font, build_kerning_states, export_web_font,
    glyph_name, supported_characters_text, verify_two_glyph_history,
)
from src.font import AutoKerning, layout_line, load_glyphs


ROOT = Path(__file__).resolve().parents[1]


def sample_glyph(rows):
    image = Image.new("RGBA", (len(rows[0]), len(rows)))
    for y, row in enumerate(rows):
        for x, pixel in enumerate(row):
            if pixel == "#":
                image.putpixel((x, y), (255, 255, 255, 255))
    return image


def kerning_pairs(font):
    """Decode GPOS pair records, including those for stateful alternates."""
    pairs = {}
    if "GPOS" not in font:
        return pairs
    for lookup in font["GPOS"].table.LookupList.Lookup:
        assert lookup.LookupType == 2
        for subtable in lookup.SubTable:
            assert subtable.Format == 1
            for left, pair_set in zip(subtable.Coverage.glyphs, subtable.PairSet):
                for record in pair_set.PairValueRecord:
                    pairs[(left, record.SecondGlyph)] = record.Value1.XAdvance
    return pairs


def shaping_font(path):
    """Load a generated font for HarfBuzz, decoding WOFF2 to SFNT first.

    HarfBuzz's Python Face constructor expects ordinary SFNT bytes, whereas a
    browser supports WOFF2 through its own web-font decoder.
    """
    font = TTFont(path)
    font.flavor = None
    output = io.BytesIO()
    font.save(output)
    face = hb.Face(output.getvalue())
    result = hb.Font(face)
    result.scale = (face.upem, face.upem)
    return result


def shaped_origins(font, text):
    """Return source-pixel origins after real GSUB/GPOS shaping in HarfBuzz."""
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    hb.shape(font, buffer, {"kern": True, "calt": True})
    assert len(buffer.glyph_infos) == len(text), "Kerning must not merge glyphs"
    origins = []
    position = 0
    for item in buffer.glyph_positions:
        origins.append((position + item.x_offset) / UNITS_PER_PIXEL)
        position += item.x_advance
    return origins


class WebFontUnitTests(unittest.TestCase):
    def test_supported_character_manifest_is_sorted_and_excludes_notdef(self):
        glyphs = {
            0x25B2: sample_glyph(["#"]),
            0: sample_glyph(["#"]),
            65: sample_glyph(["#"]),
            32: sample_glyph(["."]),
        }
        lines = supported_characters_text(glyphs).splitlines()
        self.assertIn("(3 Unicode code points)", lines[0])
        self.assertEqual([line.split("\t", 1)[0] for line in lines[2:]],
                         ["U+0020", "U+0041", "U+25B2"])
        self.assertIn("' '\tSPACE", lines[2])
        self.assertIn("'▲'\tBLACK UP-POINTING TRIANGLE", lines[4])
        self.assertNotIn("U+0000", "\n".join(lines))

    def test_non_bmp_glyph_names(self):
        self.assertEqual(glyph_name(65), "uni0041")
        self.assertEqual(glyph_name(0x1F600), "u01F600")

    def test_rejects_bad_metrics(self):
        with self.assertRaisesRegex(ValueError, "no glyphs"):
            build_font({})
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            build_font({65: sample_glyph(["#"])}, spacing=-1)
        with self.assertRaisesRegex(ValueError, "same height"):
            build_font({65: sample_glyph(["#"]), 66: sample_glyph(["#", "#"])})

    def test_bitmap_outlines_include_holes_and_diagonally_touching_pixels(self):
        rows = [
            "###...",
            "#.#...",
            "###...",
            "...#..",
            "....#.",
            ".....#",
        ]
        glyphs = {ord("A"): sample_glyph(rows), ord(" "): sample_glyph(["......"] * 6)}
        with tempfile.TemporaryDirectory() as directory:
            ttf, woff2, rules = export_web_font(glyphs, directory)
            self.assertTrue(ttf.is_file())
            self.assertTrue(woff2.is_file())
            self.assertTrue((Path(directory) / "red2-font.css").is_file())
            self.assertTrue((Path(directory) / "demo.html").is_file())
            # The synthetic A/A pair still has a genuine tightening adjustment.
            self.assertEqual(rules, 1)
            for file in (ttf, woff2):
                with self.subTest(format=file.suffix):
                    font = TTFont(file)
                    self.assertEqual(font["head"].unitsPerEm, 6 * 64)
                    self.assertEqual(set(font.getBestCmap()), {32, 65})
                    rendered = Image.new("L", (10, 6))
                    ImageDraw.Draw(rendered).text(
                        (0, 6), "A", font=ImageFont.truetype(str(file), 6),
                        anchor="ls", fill=255)
                    expected = Image.new("L", rendered.size)
                    expected.paste(glyphs[65].getchannel("A"), (0, 0))
                    self.assertEqual(rendered.tobytes(), expected.tobytes())
                    self.assertEqual(font["hmtx"][glyph_name(32)][0], 10 * 64)
                    self.assertEqual(shaped_origins(shaping_font(file), "AA"), [0, 9])


class RealFontExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.glyphs = load_glyphs(ROOT / "RED2 Font.aseprite", "aseprite")
        cls.kerning = AutoKerning(cls.glyphs)
        cls.temp = tempfile.TemporaryDirectory()
        cls.ttf, cls.woff2, cls.rule_count = export_web_font(
            cls.glyphs, cls.temp.name)
        cls.shapers = [shaping_font(cls.ttf), shaping_font(cls.woff2)]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_supported_character_file_matches_ttf_and_woff2_cmaps(self):
        path = Path(self.temp.name) / "supported-characters.txt"
        contents = path.read_text(encoding="utf-8")
        lines = [line for line in contents.splitlines() if not line.startswith("#")]
        codes = [int(line.split("\t", 1)[0][2:], 16) for line in lines]
        self.assertEqual(codes, sorted(code for code in self.glyphs if code != 0))
        self.assertEqual(len(codes), len(set(codes)))
        self.assertIn("U+25B2\t'▲'", contents)
        self.assertIn("U+2500\t'─'\tBOX DRAWINGS LIGHT HORIZONTAL", contents)
        self.assertIn("U+253C\t'┼'\tBOX DRAWINGS LIGHT VERTICAL AND HORIZONTAL", contents)
        self.assertEqual(len(codes), len(self.glyphs)-1)
        self.assertNotIn("U+0000", contents)
        for file in (self.ttf, self.woff2):
            with self.subTest(format=file.suffix):
                self.assertEqual(codes, sorted(TTFont(file).getBestCmap()))

    def test_two_history_proof_rejects_insufficient_spacings(self):
        self.assertEqual(verify_two_glyph_history(self.kerning), len(self.glyphs) ** 4)
        for spacing in (0, 1, 2, 3):
            with self.subTest(spacing=spacing):
                with self.assertRaisesRegex(ValueError, "Two-glyph history is insufficient"):
                    build_font(self.glyphs, spacing=spacing)

    def test_every_glyph_is_pixel_perfect_at_source_size(self):
        for file in (self.ttf, self.woff2):
            font = ImageFont.truetype(str(file), 32)
            for code, glyph in sorted(self.glyphs.items()):
                with self.subTest(format=file.suffix, codepoint=code):
                    width = glyph.width + 8
                    rendered = Image.new("L", (width, 32))
                    ImageDraw.Draw(rendered).text(
                        (0, 32), chr(code), font=font, anchor="ls", fill=255)
                    expected = Image.new("L", rendered.size)
                    expected.paste(glyph.getchannel("A"), (0, 0))
                    self.assertEqual(rendered.tobytes(), expected.tobytes())

    def test_contextual_state_machine_is_compact_and_encoded(self):
        base, states, transitions = build_kerning_states(self.glyphs, self.kerning)
        self.assertEqual(set(base), set(self.glyphs))
        self.assertEqual(len(states), len(set(states)))
        self.assertTrue(set(base.values()) <= set(states))
        self.assertLessEqual(len(states), len(base)**2+len(base))
        self.assertEqual(len(transitions), len(states) * len(base))
        for (state, right), (target, step) in transitions.items():
            self.assertIn(target, states)
            self.assertEqual(target[0], right)
        expected_rules = sum(step != self.kerning.nominal_advance(state[0])
                             for (state, right), (_, step) in transitions.items())
        self.assertEqual(self.rule_count, expected_rules)
        for file in (self.ttf, self.woff2):
            with self.subTest(format=file.suffix):
                font = TTFont(file)
                self.assertEqual(font["head"].unitsPerEm, 2048)
                self.assertEqual(len(font.getBestCmap()), len(self.glyphs) - 1)
                self.assertNotIn(0, font.getBestCmap())
                for character in "▲▶▼◀":
                    self.assertEqual(font.getBestCmap()[ord(character)], glyph_name(ord(character)))
                self.assertEqual(font["maxp"].numGlyphs, len(states))
                self.assertEqual(len(kerning_pairs(font)), expected_rules)
                self.assertIn("GSUB", font)
                self.assertIn("GPOS", font)
                self.assertEqual(font["hhea"].ascent, 2048)
                self.assertEqual(font["hhea"].lineGap, 256)
                self.assertEqual(font["hmtx"][glyph_name(ord("P"))][0], 18 * 64)
                for code, advance in self.glyphs.fixed_advances.items():
                    name = font.getBestCmap()[code]
                    self.assertEqual(font["hmtx"][name][0], advance * UNITS_PER_PIXEL)
                    bbox = self.glyphs[code].getchannel("A").getbbox()
                    self.assertEqual(font["hmtx"][name][1], bbox[0] * UNITS_PER_PIXEL)

    def test_notdef_is_glyph_zero_and_renders_from_aseprite(self):
        self.assertIn(0, self.glyphs)
        fallback = self.glyphs[0]
        for file in (self.ttf, self.woff2):
            with self.subTest(format=file.suffix):
                font = TTFont(file)
                self.assertEqual(font.getGlyphOrder()[0], ".notdef")
                self.assertNotIn(0, font.getBestCmap())
                self.assertGreater(font["glyf"][".notdef"].numberOfContours, 0)
                self.assertEqual(font["hmtx"][".notdef"][0],
                                 (fallback.width + self.kerning.spacing) * UNITS_PER_PIXEL)
                rendered_font = ImageFont.truetype(str(file), 32)
                for missing in ("☃", "é", "\u0378"):
                    with self.subTest(missing=missing):
                        output = Image.new("L", (fallback.width + 8, 32))
                        ImageDraw.Draw(output).text(
                            (0, 32), missing, font=rendered_font, anchor="ls", fill=255)
                        expected = Image.new("L", output.size)
                        expected.paste(fallback.getchannel("A"), (0, 0))
                        self.assertEqual(output.tobytes(), expected.tobytes())

    def test_every_pair_matches_original_fpga_advance(self):
        codes = sorted(self.glyphs)
        for font in self.shapers:
            for left in codes:
                for right in codes:
                    with self.subTest(left=chr(left), right=chr(right)):
                        self.assertEqual(
                            shaped_origins(font, chr(left) + chr(right)),
                            [0, self.kerning.advance(left, right)],
                        )

    def test_contextual_shaping_matches_fpga_two_character_history(self):
        # The fpgatool proof establishes that two preceding glyphs suffice for
        # this specific font. Compare actual GSUB+GPOS output against the
        # independently implemented, all-recent-glyph Python renderer.
        examples = [
            "P.O.", "P.O.O.", "P...O", "F.T", "L'.", "L''.T", "#,,4",
            "/_\\`", "/\\`", "AVATAR", "HELLO, WORLD!", "PO" * 100,
            "P...O" * 100, "#,,4" * 100,
            "▲▶▼◀", "A▲B▶C▼D◀E", "P.▲O▼.", "◀▶▲▼" * 100,
            "☃", "A☃", "☃A", "A☃B", "☃☃", "AéA", "A☃éB",
            "☃P.O.", "P.☃O", "▲☃▼", "☃A☃B☃" * 25,
            "┌──────┬──────┐", "├──────┼──────┤", "└──────┴──────┘",
            "─│┌┐└┘├┤┬┴┼", "A┌B", "│P.O.│", "┌──┬──┐" * 40,
        ]
        rng = random.Random(34875)
        codes = sorted(self.glyphs)
        samples = examples + [
            "".join(map(chr, rng.choices(codes, k=rng.randint(3, 100))))
            for _ in range(2000)
        ]
        for text in samples:
            expected_codes = [ord(char) if ord(char) in self.glyphs else 0
                              for char in text]
            expected = [x for _, x in layout_line(expected_codes, self.kerning)]
            for font in self.shapers:
                self.assertEqual(shaped_origins(font, text), expected,
                                 f"HarfBuzz versus source renderer: {text!r}")


if __name__ == "__main__":
    unittest.main()
