"""Decode the actual flash binary independently of the exporter packing code."""
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from src.generate import build_parser
from src.pipeline import generate
from src.v2 import compile_prepared, compile_rows, image_bytes, partition_patterns
from tests.test_generate_font import sample_glyphs


class V2ExportTests(unittest.TestCase):
    def test_small_ascii_font_explicitly_reserves_overlay_words(self):
        from src.pipeline import prepare_font
        glyphs = sample_glyphs()
        for code in list(glyphs):
            if code not in (0, 65):
                del glyphs[code]
        data = compile_prepared(prepare_font(glyphs))
        self.assertEqual(data['PATTERN_COUNT'], 256)
        self.assertEqual(data['PAGES'], ((0, data['PATTERNS_BASE'], 1, 1, 1),))
        for low in range(256):
            self.assertEqual((data['FONT_WORDS'][data['PATTERNS_BASE']+low] >> 21) & 255,
                             0 if low == 65 else 255)

    def test_minimum_bank_count_and_multi_glyph_row_reconstruction(self):
        # Every row is shared by two glyphs: moving a single glyph frees no
        # dictionary space. Several complete groups must move together.
        records = [(i, i, 9, tuple(range((i//2)*10, (i//2+1)*10)))
                   for i in range(56)]
        first, second, secondary = partition_patterns(records)
        self.assertLessEqual(len(first), 256)
        self.assertLessEqual(len(second), 256)
        self.assertGreater(len(secondary), 1)
        self.assertEqual(set(first) | set(second), set(range(280)))
        self.assertTrue(second)  # 280 distinct rows cannot fit a single bank.
        self.assertEqual((first, second, secondary), partition_patterns(records))
        data = compile_rows(records, list(range(280)), [0], 1, 1, 4)
        payload = data['FONT_WORDS']
        for gid, (_, _, _, ids) in enumerate(records):
            meta = payload[data['METADATA_BASE']+gid]
            self.assertEqual(bool(meta >> 31), gid in secondary)
            for y, expected in enumerate(ids):
                byte = (meta & 2047) + y
                row_id = (payload[data['ROWS_BASE']+byte//4] >> (8*(byte%4))) & 255
                address = data['PATTERNS_BASE'] + (meta >> 31)*256 + row_id
                self.assertEqual(payload[address] & ((1 << 21)-1), expected)
        blob = image_bytes(data)
        self.assertEqual(len(blob), 4256)
        self.assertEqual(sum(struct.unpack('<1064I', blob)) & 0xffffffff, 0)

    def test_one_bank_boundary_and_shared_rows_in_both_banks(self):
        records = [(i, i, 9, tuple(range(i*32, (i+1)*32))) for i in range(8)]
        first, second, secondary = partition_patterns(records)
        self.assertEqual(len(first), 256)
        self.assertFalse(second or secondary)
        records = [(i, i, 9, (0, *range(i*10+1, (i+1)*10+1))) for i in range(27)]
        first, second, secondary = partition_patterns(records)
        self.assertIn(0, first)
        self.assertIn(0, second)
        for code, _, _, ids in records:
            self.assertTrue(set(ids) <= set(second if code in secondary else first))

    def test_capacity_errors_explain_pattern_count_and_glyph_bank_constraint(self):
        records = [(i, i, 9, tuple(range(i*129, (i+1)*129))) for i in range(3)]
        with self.assertRaisesRegex(ValueError, '387.*more than two.*single bank'):
            partition_patterns(records)
        records = [(0, 0, 9, tuple(range(513)))]
        with self.assertRaisesRegex(ValueError, '513.*at least 3 banks.*only two'):
            partition_patterns(records)

    def test_cli_and_standalone_v2_export(self):
        self.assertEqual(build_parser().parse_args(['--export', 'v2']).formats, ('v2',))
        glyphs = sample_glyphs()
        with tempfile.TemporaryDirectory() as directory:
            with patch('src.pipeline.load_glyphs', return_value=glyphs):
                with patch('src.web_font.build_font', side_effect=AssertionError('unrequested web font')):
                    prepared, files = generate('unused.aseprite', directory, ('v2',), spacing=6)
            path = Path(directory) / 'RED2-Regular-v2.bin'
            self.assertIn(path, files)
            blob = path.read_bytes()
            self.assertEqual(len(blob), 160+4096)
            self.assertEqual(blob[:4], b'RFV2')
            words = struct.unpack('<1064I', blob)
            self.assertEqual(words[1:4], (2, 160, 1024))
            self.assertEqual(sum(words) & 0xffffffff, 0)
            payload = words[40:]
            metadata, crop_base, rows, kern, patterns = words[5:10]
            self.assertEqual(words[14], 6)
            codes = sorted(glyphs)
            for gid, code in enumerate(codes):
                meta = payload[metadata+gid]
                cc = (meta >> 11) & 31
                crop = (payload[crop_base+cc//2] >> (16*(cc % 2))) & 65535
                top, height, mirror = crop & 31, (crop >> 5) & 63, bool(crop & 2048)
                for y in range(glyphs[code].height):
                    bits = 0
                    if top <= y < top+height:
                        row = y-top
                        if mirror:
                            row = min(row, height-1-row)
                        byte = (meta & 2047)+row
                        row_id = (payload[rows+byte//4] >> (8*(byte % 4))) & 255
                        bits = payload[patterns+(meta >> 31)*256+row_id] & ((1 << 21)-1)
                    for x in range(glyphs[code].width):
                        self.assertEqual(bool(bits & (1 << x)), bool(glyphs[code].getpixel((x, y))[3]))
                for other_gid, other in enumerate(codes):
                    rm = payload[metadata+other_gid]
                    cell = ((meta >> 16) & 31)*32 + ((rm >> 21) & 31)
                    adjustment = (payload[kern+cell//8] >> (4*(cell % 8))) & 15
                    self.assertEqual((meta >> 26) & 31, glyphs[code].width)
                    self.assertEqual(glyphs[code].width+6-adjustment, prepared.kerning.advance(code, other))
            for page in range(words[15]):
                a, b = words[24+2*page:26+2*page]
                prefix, base, overlay = a & 65535, (a >> 16) & 1023, (a >> 26) & 1
                first, count = b & 255, b >> 8
                for low in range(256):
                    local = ((payload[base+low] >> 21) & 255 if overlay else
                             (payload[base+low//4] >> (8*(low % 4))) & 255)
                    cp = (prefix << 8) | low
                    expected = codes.index(cp) if cp in glyphs and cp else 0
                    self.assertEqual(first+local if local < count else 0, expected)
            self.assertEqual(blob, image_bytes(compile_prepared(prepared)))

    def test_missing_notdef_and_unsupported_spacing_are_rejected(self):
        from src.pipeline import prepare_font
        glyphs = sample_glyphs()
        del glyphs[0]
        with self.assertRaisesRegex(ValueError, 'notdef'):
            compile_prepared(prepare_font(glyphs))
        with self.assertRaisesRegex(ValueError, 'spacing'):
            compile_prepared(prepare_font(sample_glyphs(), spacing=32))

    def test_blank_non_ascii_font_keeps_a_pattern_word(self):
        from PIL import Image
        from src.font import GlyphCollection
        from src.pipeline import prepare_font
        glyphs = GlyphCollection({cp: Image.new('RGBA', (3, 4)) for cp in (0, 0x2500)})
        data = compile_prepared(prepare_font(glyphs))
        self.assertEqual(data['ROW_BYTE_COUNT'], 0)
        self.assertEqual(data['PATTERN_COUNT'], 1)
        self.assertEqual(data['FONT_WORDS'][data['PATTERNS_BASE']], 0)
        self.assertEqual(len(image_bytes(data)), 4256)


if __name__ == '__main__':
    unittest.main()
