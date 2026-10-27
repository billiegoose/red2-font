import contextlib
from collections import Counter
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from src.font import AutoKerning, GlyphCollection, layout_line, load_glyphs
from src.generate import build_parser, main
from src.pipeline import generate, prepare_font
from tests.test_export_web_font import shaped_origins, shaping_font


ROOT = Path(__file__).resolve().parents[1]


def sample_glyphs():
    images = {}
    patterns = {0: ["###", "#.#", "#.#", "###"],
                32: ["...", "...", "...", "..."],
                65: [".#.", "#.#", "###", "#.#"],
                66: ["##.", "#.#", "##.", "###"],
                0x2500: [".....", "#####", ".....", "....."]}
    for code, rows in patterns.items():
        image = Image.new("RGBA", (len(rows[0]), len(rows)))
        for y, row in enumerate(rows):
            for x, value in enumerate(row):
                if value == "#":
                    image.putpixel((x, y), (255, 255, 255, 255))
        images[code] = image
    return GlyphCollection(images, fixed_advances={0x2500: 5})


class GenerationTests(unittest.TestCase):
    def test_shared_preparation_and_round_trip_every_export_format(self):
        glyphs = sample_glyphs()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            with patch("src.pipeline.load_glyphs", return_value=glyphs) as load:
                with patch("src.pipeline.prepare_font", wraps=prepare_font) as prepare:
                    with patch("src.web_font.AutoKerning", side_effect=AssertionError("recomputed kerning")):
                        prepared, files = generate(ROOT / "RED2 Font.aseprite", directory,
                                                   ("woff", "woff2", "ttf", "bram", "v2"),
                                                   spacing=6, kerning_slack=3, widen_pairs=True)
                self.assertEqual(prepare.call_count, 1)
                self.assertEqual(load.call_count, 1)
            manifest = json.loads((directory / "font.json").read_text())
            self.assertEqual(manifest["settings"], {"spacing": 6, "kerning_slack": 3, "widen_pairs": True})
            self.assertTrue(all(path.exists() for path in files))
            self.assertEqual(prepared.statistics["history_glyphs"], 1)
            for extension, signature in (("woff", b"wOFF"), ("woff2", b"wOF2"), ("ttf", b"\0\1\0\0")):
                path = directory / f"RED2-Regular.{extension}"
                self.assertEqual(path.read_bytes()[:4], signature)
                font = shaping_font(path)
                for a in glyphs:
                    for b in glyphs:
                        text = ''.join(map(chr, (a, b)))
                        self.assertEqual(shaped_origins(font, text),
                                         [x for _, x in layout_line((a, b), prepared.kerning)])
                for text in ("AB BA", "A─BA", "ABAABBB A"):
                    self.assertEqual(shaped_origins(font, text),
                                     [x for _, x in layout_line(map(ord, text), prepared.kerning)])
            # Decode the actual .mem files using only the exported layout.
            bram = directory / "bram"
            layout = json.loads((bram / "layout.json").read_text())
            words = [int(line, 16) for line in (bram / "font.mem").read_text().splitlines()]
            meta = [int(line, 16) for line in (bram / "metadata.mem").read_text().splitlines()]
            kerns = [int(line, 16) for line in (bram / "kerning.mem").read_text().splitlines()]
            self.assertEqual(len(words), 2048)
            self.assertEqual(len(meta), 256)
            self.assertEqual(len(kerns), layout["kerning_rom"]["depth"])
            ids = {int(k): v for k, v in layout["glyph_ids"].items()}
            font_bytes = [byte for word in words for byte in (word & 255, word >> 8)]
            for a, glyph in glyphs.items():
                m = meta[ids[a]]
                width, height, top, base = m & 31, (m >> 9) & 63, (m >> 15) & 31, (m >> 20) & 2047
                self.assertEqual(width, glyph.width)
                for y in range(glyph.height):
                    bits = 0
                    if top <= y < top + height:
                        row_id = words[base + y - top]
                        bits = sum(font_bytes[3 * row_id + n] << (8 * n) for n in range(3))
                    for x in range(width):
                        self.assertEqual(bool(bits & 1 << x), bool(glyph.getpixel((x, y))[3]))
                for b in glyphs:
                    adjustment = kerns[((m >> 31) & 63) * layout["kerning_rom"]["stride"]
                                       + ((meta[ids[b]] >> 37) & 63)]
                    self.assertEqual(width + 6 - adjustment, prepared.kerning.advance(a, b))

    def test_only_requested_formats_are_written_and_css_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("src.pipeline.load_glyphs", return_value=sample_glyphs()):
                generate(ROOT / "RED2 Font.aseprite", directory, ("woff",))
            names = {path.name for path in Path(directory).iterdir()}
            self.assertIn("RED2-Regular.woff", names)
            self.assertNotIn("RED2-Regular.ttf", names)
            self.assertNotIn("RED2-Regular.woff2", names)
            self.assertNotIn("bram", names)
            css = (Path(directory) / "red2-font.css").read_text()
            self.assertIn('url("./RED2-Regular.woff") format("woff")', css)
            self.assertNotIn('.ttf', css)
            self.assertNotIn('.woff2', css)

    def test_bram_only_does_not_build_a_web_font(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("src.pipeline.load_glyphs", return_value=sample_glyphs()):
                with patch("src.web_font.build_font", side_effect=AssertionError("unrequested format")):
                    generate(ROOT / "RED2 Font.aseprite", directory, ("bram",))
            self.assertFalse((Path(directory) / "red2-font.css").exists())
            self.assertTrue((Path(directory) / "bram/font.mem").exists())

    def test_format_failure_preserves_previously_published_outputs(self):
        glyphs = GlyphCollection({65: Image.new("RGBA", (22, 32), "white")})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dist"
            output.mkdir()
            (output / "RED2-Regular.ttf").write_bytes(b"previous font")
            (output / "font.json").write_text("previous manifest")
            with patch("src.pipeline.load_glyphs", return_value=glyphs):
                with self.assertRaisesRegex(ValueError, "21 x 32"):
                    generate(ROOT / "RED2 Font.aseprite", output, ("ttf", "bram"))
            self.assertEqual((output / "RED2-Regular.ttf").read_bytes(), b"previous font")
            self.assertEqual((output / "font.json").read_text(), "previous manifest")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_bad_options_fail_before_loading_artwork(self):
        for argv in (("--spacing", "-1"), ("--kerning-slack", "-1"), ("--export", "xyz"), ("--export", "")):
            with self.subTest(argv=argv), patch("src.pipeline.load_glyphs") as load:
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as status:
                        main(list(argv))
                self.assertEqual(status.exception.code, 2)
                load.assert_not_called()

    def test_defaults_and_comma_separated_formats(self):
        parser = build_parser()
        defaults = parser.parse_args([])
        self.assertEqual(defaults.spacing, 4)
        self.assertEqual(defaults.kerning_slack, 2)
        self.assertFalse(defaults.widen_pairs)
        self.assertEqual(defaults.formats, ("woff", "ttf", "bram"))
        args = parser.parse_args(["--export", "woff,ttf,bram,woff"])
        self.assertEqual(args.formats, ("woff", "ttf", "bram"))


class RealFontGenerationTests(unittest.TestCase):
    def test_defaults_validate_current_artwork_within_slack_budget(self):
        glyphs = load_glyphs(ROOT / "RED2 Font.aseprite", "aseprite")
        prepared = prepare_font(glyphs)
        # Artwork is editable; class counts and error histograms change with
        # it. Check the optical-error bound rather than a past font snapshot.
        original = AutoKerning(glyphs, prepared.settings['spacing'])
        errors = Counter(prepared.kerning.advance(a, b)-original.advance(a, b)
                         for a in glyphs for b in glyphs)
        self.assertTrue(all(0 <= extra <= 2 for extra in errors))
        self.assertEqual(prepared.statistics["extra_pixel_histogram"],
                         {str(extra): count for extra, count in sorted(errors.items())})
        self.assertEqual(prepared.statistics["history_glyphs"], 2)
        self.assertEqual(prepared.statistics['history_combinations_checked'], len(glyphs)**4)


if __name__ == "__main__":
    unittest.main()
