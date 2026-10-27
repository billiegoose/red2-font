"""The generator owns binary validation, reference export and capture checks."""
import contextlib
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from src.bram import export_bram
from src.generate import main
from src.pipeline import prepare_font
from src.v2 import compile_prepared, image_bytes, validate_image
from src.v2_tools import FontImage, expected_pixels, verify_reload
from tests.test_generate_font import sample_glyphs


class V2ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.prepared = prepare_font(sample_glyphs())
        self.binary = image_bytes(compile_prepared(self.prepared))
        self.path = self.directory / 'font.bin'
        self.path.write_bytes(self.binary)
        self.font = FontImage(self.binary)

    def test_decoder_matches_source_pixels_kerning_and_fallback(self):
        codes = sorted(self.prepared.glyphs)
        self.assertNotIn(0, self.font.mapping)
        for gid, code in enumerate(codes):
            image = self.prepared.glyphs[code].getchannel('A')
            self.assertEqual(self.font.mapping.get(code, 0), gid)
            for y in range(32):
                expected = (sum(bool(image.getpixel((x, y))) << x for x in range(image.width))
                            if y < image.height else 0)
                self.assertEqual(self.font.rows[gid][y], expected)
            for other_gid, other in enumerate(codes):
                self.assertEqual(self.font.advance(gid, other_gid, self.font.spacing),
                                 self.prepared.kerning.advance(code, other))
        expected = Image.new('L', (1920, 1080))
        a = self.prepared.glyphs[65].getchannel('A')
        expected.paste(a, (5, 2))
        expected.paste(a, (5+self.prepared.kerning.advance(65, 65), 2))
        self.assertEqual(expected_pixels(self.font, b'AA'), expected.tobytes())
        self.assertEqual(expected_pixels(self.font, b'AB\bA'), expected.tobytes())

    def test_header_checks_reject_bad_checksum_bounds_and_duplicate_pages(self):
        corrupt = bytearray(self.binary)
        corrupt[-1] ^= 1
        with self.assertRaisesRegex(ValueError, 'checksum'):
            validate_image(corrupt)
        original = list(struct.unpack('<1064I', self.binary))
        for index, value in ((5, 1024), (10, 0), (14, 32),
                             (26, original[26] & ~65535 | original[24] & 65535)):
            words = original.copy()
            words[index] = value
            words[16] = 0
            words[16] = -sum(words) & 0xffffffff
            with self.assertRaisesRegex(ValueError, 'bounds|directory'):
                validate_image(struct.pack('<1064I', *words))

    def test_descriptor_checks_reject_bad_crop_and_row_pool_bounds(self):
        original = list(struct.unpack('<1064I', self.binary))
        for descriptor in ((original[40] & ~(31 << 11)) | (31 << 11),
                           (original[40] & ~2047) | 2047):
            words = original.copy()
            words[40] = descriptor
            words[16] = 0
            words[16] = -sum(words) & 0xffffffff
            with self.assertRaisesRegex(ValueError, 'crop|row-pool'):
                FontImage(struct.pack('<1064I', *words))

    def test_validate_cli_does_not_load_aseprite(self):
        output = io.StringIO()
        with patch('src.pipeline.load_glyphs', side_effect=AssertionError('unneeded artwork')), \
             contextlib.redirect_stdout(output):
            main(['validate-v2', str(self.path)])
        self.assertIn('Valid font v2: 5 glyphs', output.getvalue())

    def test_reference_command_uses_shared_compiler_without_fpgatool(self):
        export_bram(self.prepared, self.directory)
        output, binary = self.directory / 'reference.py', self.directory / 'reference.bin'
        with contextlib.redirect_stdout(io.StringIO()):
            main(['v2-reference', str(self.directory/'red2_bram.py'),
                  '--output', str(output), '--binary', str(binary)])
        self.assertTrue(output.is_file())
        self.assertEqual(binary.read_bytes(), self.binary)

    def test_reload_verifier_rejects_mixed_frames_and_reversed_transitions(self):
        text = b'AA'
        old = expected_pixels(self.font, text)
        new = expected_pixels(self.font, text, spacing=9)
        def save(index, pixels):
            Image.frombytes('L', (1920, 1080), pixels).save(self.directory/f'frame-{index:04d}.png')
        save(1, old)
        save(2, new)
        self.assertIn('old, new', verify_reload(self.font, self.directory, text))
        text_path = self.directory/'text.txt'
        text_path.write_bytes(text)
        with contextlib.redirect_stdout(io.StringIO()):
            main(['verify-v2-reload', str(self.directory), str(text_path), '--font', str(self.path)])
        save(3, old)
        with self.assertRaisesRegex(ValueError, 'one transition'):
            verify_reload(self.font, self.directory, text)
        corrupt = bytearray(new)
        corrupt[0] = 255
        save(2, bytes(corrupt))
        with self.assertRaisesRegex(ValueError, 'neither complete'):
            verify_reload(self.font, self.directory, text)
