#!/usr/bin/env python3
"""Export Aseprite slices and render text using available glyphs."""

import argparse
import csv
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from PIL import Image


def validate_slices(slices):
    """Require explicit, unique c=<decimal code point> userdata on every slice."""
    codes = {}
    errors = []
    for item in slices:
        name = item["name"]
        match = re.fullmatch(r"c\s*=\s*(\d+)", item.get("data", "").strip())
        if not match:
            errors.append(f"Slice {name!r} must have userdata c=<decimal code point>.")
            continue
        code = int(match[1])
        if not 0 <= code <= 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            errors.append(f"Slice {name!r} has invalid code point {code}.")
        if code in codes:
            errors.append(f"Duplicate c={code}: slices {codes[code]!r} and {name!r}.")
        else:
            codes[code] = name
    if errors:
        raise ValueError("Slice validation failed:\n" + "\n".join(errors))
    return codes


def load_glyphs(source, aseprite, layer="Text"):
    """Export only glyph artwork, then crop horizontal transparent margins."""
    with tempfile.TemporaryDirectory(prefix="red2-font-") as directory:
        sheet = Path(directory) / "sheet.png"
        data = Path(directory) / "slices.json"
        # One untrimmed frame keeps slice bounds in source-canvas coordinates.
        subprocess.run(
            [
                aseprite,
                "-b",
                "--list-slices",
                "--layer",
                layer,
                "--frame-range",
                "0,0",
                str(source.resolve()),
                "--sheet",
                str(sheet),
                "--data",
                str(data),
            ],
            check=True,
        )
        metadata = json.loads(data.read_text())
        slices = metadata["meta"].get("slices", [])
        validate_slices(slices)
        glyphs = {}
        with Image.open(sheet) as exported:
            pixels = exported.convert("RGBA")
        for item in slices:
            code = int(item["data"].split("=", 1)[1].strip())
            keys = [key for key in item["keys"] if key["frame"] == 0]
            if not keys:
                continue
            bounds = keys[0]["bounds"]
            x, y, w, h = (bounds[k] for k in ("x", "y", "w", "h"))
            if (
                w <= 0
                or h <= 0
                or x < 0
                or y < 0
                or x + w > pixels.width
                or y + h > pixels.height
            ):
                raise ValueError(f"Invalid bounds for slice {item['name']!r}")
            glyph = pixels.crop((x, y, x + w, y + h))
            ink = glyph.getchannel("A").getbbox()
            # Keep vertical metrics; blank glyphs (space) retain their slice width.
            if ink:
                glyph = glyph.crop((ink[0], 0, ink[2], h))
            glyphs[code] = glyph
        return glyphs


def contours(glyph):
    """Map occupied rows to their leftmost and rightmost alpha pixels."""
    alpha = glyph.getchannel("A")
    rows = {}
    for y in range(glyph.height):
        occupied = [x for x in range(glyph.width) if alpha.getpixel((x, y))]
        if occupied:
            rows[y] = (occupied[0], occupied[-1])
    return rows


class AutoKerning:
    """Tighten pairs while preserving Euclidean clearance between nearby edges."""

    def __init__(self, glyphs, spacing=4):
        self.glyphs = glyphs
        self.spacing = spacing
        self.rows = {code: contours(glyph) for code, glyph in glyphs.items()}
        self.cache = {}

    def advance(self, left, right):
        pair = (left, right)
        if pair not in self.cache:
            self.cache[pair] = self._advance(left, right)
        return self.cache[pair]

    def _advance(self, left, right):
        a, b = self.rows[left], self.rows[right]
        nominal = self.glyphs[left].width + self.spacing
        if not a or not b:
            return nominal
        # spacing empty pixels along a straight row means spacing + 1
        # between pixel centers. Compare facing edges within +/- spacing rows;
        # more distant rows cannot violate this clearance on the integer grid.
        neighbors = [(b[yy][0] - a[y][1], yy - y)
                     for y in a for yy in b if abs(yy - y) <= self.spacing]
        if not neighbors:
            return nominal
        minimum_squared = (self.spacing + 1) ** 2
        advance = nominal
        while all(
            advance - 1 + dx > 0
            and (advance - 1 + dx) ** 2 + dy ** 2 >= minimum_squared
            for dx, dy in neighbors
        ):
            advance -= 1
        return advance


def compose(text, glyphs, kerning):
    """Render multiple lines; unsupported characters have no advance."""
    missing = sorted(set(text) - {chr(code) for code in glyphs} - {"\n"})
    for character in missing:
        print(f"Warning: skipping unrecognized character {character!r} "
              f"(U+{ord(character):04X}).", file=sys.stderr)
    lines = [[ord(c) for c in line if ord(c) in glyphs] for line in text.split("\n")]
    if not any(lines):
        return Image.new("RGBA", (1, 1), (0, 0, 0, 255))
    height = max(glyph.height for glyph in glyphs.values())
    placements = []
    width = 1
    for line in lines:
        x = 0
        positions = []
        for index, code in enumerate(line):
            positions.append((code, x))
            width = max(width, x + glyphs[code].width)
            if index + 1 < len(line):
                x += kerning.advance(code, line[index + 1])
        placements.append(positions)
    line_advance = height + 4
    image = Image.new("RGBA", (width, (len(lines) - 1) * line_advance + height), (0, 0, 0, 255))
    for row, positions in enumerate(placements):
        for code, x in positions:
            image.alpha_composite(glyphs[code], (x, row * line_advance))
    return image


def export_kerning_csv(path, alphabet, glyphs, kerning):
    """Export advances and actual closest-pixel gaps for the pair specimen."""
    pixels = {}
    for character in alphabet:
        image = glyphs[ord(character)]
        alpha = image.getchannel("A")
        pixels[character] = [(x, y) for y in range(image.height)
                             for x in range(image.width) if alpha.getpixel((x, y))]
    with Path(path).open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["left", "right", "spacing_px", "advance_px", "kerning_px", "min_gap_px"])
        for left in alphabet:
            for right in alphabet:
                advance = kerning.advance(ord(left), ord(right))
                adjustment = advance - glyphs[ord(left)].width - kerning.spacing
                # Measure all pixel pairs, including rows beyond the search radius.
                squared = min(((advance + bx - ax)**2 + (by - ay)**2
                               for ax, ay in pixels[left] for bx, by in pixels[right]),
                              default=None)
                gap = "" if squared is None else f"{math.sqrt(squared) - 1:.6f}"
                writer.writerow([left, right, kerning.spacing, advance, adjustment, gap])


def render(text, source, output, aseprite, spacing=4, layer="Text", pairs=False):
    glyphs = load_glyphs(source, aseprite, layer)
    if pairs:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        missing = [c for c in alphabet if ord(c) not in glyphs]
        if missing or ord(" ") not in glyphs:
            raise ValueError("Pair specimen requires slices for A-Z and space.")
        text = "\n".join(" ".join(a + b for b in alphabet) for a in alphabet)
    kerning = AutoKerning(glyphs, spacing)
    image = compose(text, glyphs, kerning)
    image.save(output, format="PNG")
    if pairs:
        csv_path = Path(output).with_name("kerning_pairs.csv")
        export_kerning_csv(csv_path, alphabet, glyphs, kerning)
        print(f"Saved {csv_path} ({len(alphabet)**2} pairs)")
    return image.size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "text",
        nargs="?",
        default="",
        help="Text to render; characters without glyphs are skipped with a warning",
    )
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).with_name("RED2 Font.aseprite")
    )
    parser.add_argument("--output", type=Path, default=Path("output.png"))
    parser.add_argument(
        "--aseprite",
        default="aseprite",
        help="Aseprite executable (default: aseprite on PATH)",
    )
    parser.add_argument("--pairs", action="store_true", help="Render the 26×26 A-Z pair specimen")
    parser.add_argument("--spacing", type=int, default=4, help="Minimum edge gap and vertical search radius in pixels (default: 4)")
    parser.add_argument("--layer", default="Text", help="Artwork layer to export (default: Text)")
    args = parser.parse_args()
    if args.spacing < 0:
        parser.error("--spacing must be at least 0")
    try:
        width, height = render(args.text, args.source, args.output, args.aseprite,
                               args.spacing, args.layer, args.pairs)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(f"Saved {args.output} ({width}x{height})")


if __name__ == "__main__":
    main()
