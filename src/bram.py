"""Dictionary-coded glyph rows and compact class ROMs for FPGA consumers.

The bitmap and 43-bit glyph metadata follow the VGA UART row format. Kerning
ROM addressing uses the exported stride, so five-bit class IDs need not be
padded to a 64-by-64 table. The Python constants also expose dense KERNING
values for consumers that do their own padding.
"""

import json
from pathlib import Path


def write_mem(path, values, width):
    digits = (width + 3) // 4
    if any(not 0 <= value < 1 << width for value in values):
        raise ValueError(f"{path.name}: a value exceeds {width} bits")
    path.write_text("".join(f"{value:0{digits}x}\n" for value in values))


def export_bram(prepared, directory):
    glyphs, classes = prepared.glyphs, prepared.classes
    codes = sorted(glyphs)
    extras = tuple(code for code in codes if code >= 128)
    if len(extras) > 128:
        raise ValueError("BRAM font supports at most 128 non-ASCII glyphs")
    ids = {code: code for code in codes if code < 128}
    ids.update({code: 128 + i for i, code in enumerate(extras)})
    left = {code: i for i, group in enumerate(classes.left) for code in group}
    right = {code: i for i, group in enumerate(classes.right) for code in group}
    left_count, right_count = len(classes.left), len(classes.right)
    if max(left_count, right_count) > 64:
        raise ValueError("BRAM metadata supports at most 64 left and 64 right classes; try more kerning slack")
    dense_kerns = [value for row in classes.values for value in row]
    if any(not 0 <= value <= 15 for value in dense_kerns):
        raise ValueError("BRAM kerning adjustments must fit four unsigned bits")
    dictionary, dictionary_ids, row_indices = [0], {0: 0}, []
    metadata = [0] * 256
    for code in codes:
        glyph = glyphs[code]
        if glyph.width > 21 or glyph.height > 32:
            raise ValueError("BRAM glyphs must be at most 21 x 32 pixels")
        alpha = glyph.getchannel("A")
        bounds = alpha.getbbox()
        top, height = (bounds[1], bounds[3] - bounds[1]) if bounds else (0, 0)
        base = len(row_indices)
        metadata[ids[code]] = (glyph.width | 1 << 8 | height << 9 | top << 15 | base << 20
                               | left[code] << 31 | right[code] << 37)
        for y in range(top, top + height):
            bits = sum(bool(alpha.getpixel((x, y))) << x for x in range(glyph.width))
            if bits not in dictionary_ids:
                dictionary_ids[bits] = len(dictionary)
                dictionary.append(bits)
            row_indices.append(dictionary_ids[bits])
    if len(dictionary) > 512:
        raise ValueError("BRAM row dictionary exceeds nine-bit indices")
    font_bytes = [byte for bits in dictionary for byte in
                  (bits & 255, (bits >> 8) & 255, (bits >> 16) & 255)]
    if len(font_bytes) & 1:
        font_bytes.append(0)
    index_base = len(font_bytes) // 2
    font_words = [font_bytes[i] | font_bytes[i + 1] << 8 for i in range(0, len(font_bytes), 2)]
    font_words.extend(row_indices)
    used_words = len(font_words)
    if used_words > 2048:
        raise ValueError("BRAM dictionary and cropped row indices exceed 2048 sixteen-bit words")
    for code in codes:
        metadata[ids[code]] += index_base << 20
    font_words.extend([0] * (2048 - len(font_words)))
    left_bits, right_bits = (left_count - 1).bit_length(), (right_count - 1).bit_length()
    stride = 1 << right_bits
    padded_kerns = [classes.values[a][b] if a < left_count and b < right_count else 0
                    for a in range(1 << left_bits) for b in range(stride)]
    # Check the actual decoded format, including byte-sized glyph remapping.
    spacing = prepared.settings["spacing"]
    for a in codes:
        ma = metadata[ids[a]]
        for b in codes:
            mb = metadata[ids[b]]
            kern = padded_kerns[((ma >> 31) & 63) * stride + ((mb >> 37) & 63)]
            if (ma & 31) + spacing - kern != prepared.kerning.advance(a, b):
                raise ValueError("Packed BRAM classes changed a pair advance")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_mem(directory / "font.mem", font_words, 16)
    write_mem(directory / "metadata.mem", metadata, 43)
    write_mem(directory / "kerning.mem", padded_kerns, 4)
    constants = {"SPACING": spacing, "KERNING_SLACK": prepared.settings["kerning_slack"],
                 "WIDEN_PAIRS": prepared.settings["widen_pairs"],
                 "KERNING_LEFT_COUNT": left_count, "KERNING_RIGHT_COUNT": right_count,
                 "KERNING_LEFT_BITS": left_bits, "KERNING_RIGHT_BITS": right_bits,
                 "KERNING_STRIDE": stride, "FONT_DICTIONARY_COUNT": len(dictionary),
                 "FONT_INDEX_BASE": index_base, "FONT_ROW_COUNT": len(row_indices),
                 "UNICODE_CODEPOINTS": extras}
    text = ['"""Generated RED2 BRAM constants; see layout.json for ROM addressing."""', ""]
    text.extend(f"{name} = {value!r}" for name, value in constants.items())
    for name, values in (("FONT_WORDS", font_words), ("METADATA", metadata), ("KERNING", dense_kerns)):
        text += ["", f"{name} = ("]
        for start in range(0, len(values), 16):
            text.append("    " + ", ".join(map(str, values[start:start + 16])) + ",")
        text.append(")")
    (directory / "red2_bram.py").write_text("\n".join(text) + "\n")
    layout = {"settings": prepared.settings, "glyph_ids": {str(code): ids[code] for code in codes},
              "history_glyphs": prepared.statistics["history_glyphs"],
              "font_rom": {"file": "font.mem", "depth": 2048, "width": 16, "used_words": used_words,
                           "dictionary_count": len(dictionary), "index_base": index_base,
                           "row_count": len(row_indices), "dictionary_bytes_per_pattern": 3},
              "metadata_rom": {"file": "metadata.mem", "depth": 256, "width": 43,
                               "fields": {"width": [0, 5], "backtrack": [5, 3], "valid": [8, 1],
                                          "height": [9, 6], "top": [15, 5], "base": [20, 11],
                                          "left_class": [31, 6], "right_class": [37, 6]}},
              "kerning_rom": {"file": "kerning.mem", "depth": len(padded_kerns), "width": 4,
                              "left_count": left_count, "right_count": right_count,
                              "left_bits": left_bits, "right_bits": right_bits, "stride": stride,
                              "address": "(left_class << right_bits) | right_class",
                              "advance": "previous_width + spacing - adjustment"}}
    (directory / "layout.json").write_text(json.dumps(layout, indent=2) + "\n")
    return [directory / name for name in ("font.mem", "metadata.mem", "kerning.mem", "red2_bram.py", "layout.json")]
