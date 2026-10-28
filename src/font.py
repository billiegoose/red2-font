#!/usr/bin/env python3
"""Export Aseprite slices and render text using available glyphs."""

import argparse
from collections import deque
import csv
import json
import math
from pathlib import Path
from .paths import ROOT, DEFAULT_SOURCE
import re
import subprocess
import sys
import tempfile

from PIL import Image
from .defaults import SPACING, KERNING_SLACK, WIDEN_PAIRS


def parse_slice_userdata(data):
    """Read a character mapping and optional k=0 from multiline slice userdata.

    The mapping can be decimal c=65, hexadecimal c=U+25B2, or bare U+25B2.
    k=0 disables optical kerning and selects a fixed cell advance. Reject
    unknown fields rather than silently misinterpreting Aseprite metadata.
    """
    code = None
    fixed = False
    seen_kerning = False
    for value in data.splitlines():
        value = value.strip()
        if not value:
            continue
        kerning_match = re.fullmatch(r"k\s*=\s*([01])", value)
        if kerning_match:
            if seen_kerning:
                raise ValueError("duplicate k= kerning field")
            seen_kerning = True
            fixed = kerning_match[1] == "0"
            continue
        hex_match = re.fullmatch(r"(?:c\s*=\s*)?U\+([0-9a-f]{1,6})", value, re.IGNORECASE)
        decimal_match = re.fullmatch(r"c\s*=\s*(\d+)", value)
        if not (hex_match or decimal_match):
            raise ValueError("expected c=<decimal>, c=U+<hex>, U+<hex>, or k=0 userdata")
        if code is not None:
            raise ValueError("duplicate character mapping")
        code = int(hex_match[1], 16) if hex_match else int(decimal_match[1], 10)
    if code is None:
        raise ValueError("missing character mapping (c=<decimal>, c=U+<hex>, or U+<hex>)")
    return code, fixed


def parse_slice_codepoint(data):
    """Return a Unicode code point from Aseprite slice userdata."""
    return parse_slice_userdata(data)[0]


class GlyphCollection(dict):
    """Codepoint-to-RGBA glyph dictionary carrying fixed-advance metadata."""

    def __init__(self, *args, fixed_advances=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixed_advances = dict(fixed_advances or {})


def validate_slices(slices):
    """Require explicit, unique Unicode scalar mappings on every slice."""
    codes = {}
    errors = []
    for item in slices:
        name = item["name"]
        try:
            code = parse_slice_codepoint(item.get("data", ""))
        except ValueError as error:
            errors.append(f"Slice {name!r}: {error}.")
            continue
        if not 0 <= code <= 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            errors.append(f"Slice {name!r} has invalid code point U+{code:X}.")
            continue
        if code in codes:
            errors.append(f"Duplicate U+{code:04X}: slices {codes[code]!r} and {name!r}.")
        else:
            codes[code] = name
    if errors:
        raise ValueError("Slice validation failed:\n" + "\n".join(errors))
    return codes


def load_glyphs(source, aseprite, layer="Glyphs"):
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
        glyphs = GlyphCollection()
        with Image.open(sheet) as exported:
            pixels = exported.convert("RGBA")
        for item in slices:
            code, fixed = parse_slice_userdata(item["data"])
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
            # Fixed-cell glyphs retain their transparent side margins: unlike
            # optically kerned letters, their stroke positions are relative
            # to the original Aseprite slice, not the ink bounding box.
            if ink and not fixed:
                glyph = glyph.crop((ink[0], 0, ink[2], h))
            glyphs[code] = glyph
            if fixed:
                glyphs.fixed_advances[code] = w
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


def horizontal_ink_metrics(glyph):
    """Return the exclusive right ink edge and upper median ink column."""
    alpha = glyph.getchannel("A")
    counts = [sum(bool(alpha.getpixel((x, y))) for y in range(glyph.height))
              for x in range(glyph.width)]
    total = sum(counts)
    if not total:
        return None
    remaining = total // 2
    for x, count in enumerate(counts):
        remaining -= count
        if remaining < 0:
            median = x
            break
    return max(x for x, count in enumerate(counts) if count) + 1, median


class AutoKerning:
    """Choose the nearest edge gap, keeping both glyph edges in reading order."""

    def __init__(self, glyphs, spacing=4):
        self.glyphs = glyphs
        self.spacing = spacing
        self.fixed_advances = dict(getattr(glyphs, "fixed_advances", {}))
        self.rows = {code: contours(glyph) for code, glyph in glyphs.items()}
        self.cache = {}

    def nominal_advance(self, left):
        """A fixed-cell glyph has no intercharacter gap, even before text."""
        return self.fixed_advances.get(left, self.glyphs[left].width + self.spacing)

    def advance(self, left, right):
        pair = (left, right)
        if pair not in self.cache:
            self.cache[pair] = self._advance(left, right)
        return self.cache[pair]

    def _advance(self, left, right):
        if left in self.fixed_advances or right in self.fixed_advances:
            return self.nominal_advance(left)
        a, b = self.rows[left], self.rows[right]
        nominal = self.glyphs[left].width + self.spacing
        if not a or not b:
            return nominal
        # Allow complete nesting only up to flush right alignment. A narrow
        # glyph can share the left glyph's width, but cannot end inside it or
        # place its left edge before the left glyph's left edge.
        minimum = max(0, self.glyphs[left].width - self.glyphs[right].width)
        # spacing empty pixels along a straight row means spacing + 1
        # between pixel centers. Compare facing edges within +/- spacing rows;
        # more distant rows cannot violate this clearance on the integer grid.
        # At a given vertical distance only the smallest horizontal gap
        # can constrain placement: all evaluated gaps are positive. Collapse
        # equivalent constraints before repeatedly measuring distances.
        gaps = {}
        for y, (_, edge) in a.items():
            for yy in range(y - self.spacing, y + self.spacing + 1):
                if yy in b:
                    dy = abs(yy - y)
                    dx = b[yy][0] - edge
                    if dy not in gaps or dx < gaps[dy]:
                        gaps[dy] = dx
        neighbors = [(dx, dy) for dy, dx in gaps.items()]
        if not neighbors:
            return nominal
        target_distance = self.spacing + 1

        def distance(x):
            return math.sqrt(min((x + dx)**2 + dy**2 for dx, dy in neighbors))

        advance = nominal
        previous_distance = distance(advance)
        while True:
            closer = advance - 1
            if closer < minimum:
                return advance
            # Preserve left-to-right contour order; spacing zero must not overlap.
            if any(closer + dx <= 0 for dx, _ in neighbors):
                return advance
            current_distance = distance(closer)
            if current_distance < target_distance:
                # Prefer the wider placement if both errors are exactly equal.
                if target_distance - current_distance < previous_distance - target_distance:
                    return closer
                return advance
            advance = closer
            previous_distance = current_distance


def layout_line(codes, kerning):
    """Place glyphs with pair clearance from every earlier glyph in the line.

    ``kerning`` supplies glyph widths, spacing, and pair advances satisfying
    max(0, left.width - right.width) <= advance <= left.width + spacing.
    Codes are source code points for AutoKerning or decoded-font glyph IDs.
    """
    positions = []
    # Both box edges advance monotonically, so expiry positions are ordered.
    # Retain only glyphs whose nominal advance can still affect a future origin.
    recent = deque()
    for code in codes:
        if positions:
            previous, previous_x = positions[-1]
            x = previous_x + kerning.advance(previous, code)
            while recent and recent[0][2] <= x:
                recent.popleft()
            for earlier, earlier_x, expiry in recent:
                if expiry > x:
                    x = max(x, earlier_x + kerning.advance(earlier, code))
        else:
            x = 0
        positions.append((code, x))
        # Other pair constraints may have moved x beyond more expiry positions.
        # Future origins cannot move left, so these entries can be forgotten.
        while recent and recent[0][2] <= x:
            recent.popleft()
        recent.append((code, x, x + kerning.nominal_advance(code)))
    return positions


def compose(text, glyphs, kerning):
    """Render text, substituting the reserved glyph 0 for missing characters.

    If no .notdef glyph was provided, preserve the old skip-with-warning
    behavior. Line breaks remain layout controls, not printable glyphs.
    """
    fallback = 0 if 0 in glyphs else None
    missing = sorted(set(text) - {chr(code) for code in glyphs} - {"\n"})
    for character in missing:
        action = "replacing" if fallback is not None else "skipping"
        print(f"Warning: {action} unrecognized character {character!r} "
              f"(U+{ord(character):04X}).", file=sys.stderr)
    lines = []
    for line in text.split("\n"):
        codes = []
        for character in line:
            code = ord(character)
            if code in glyphs:
                codes.append(code)
            elif fallback is not None:
                codes.append(fallback)
        lines.append(codes)
    if not any(lines):
        return Image.new("RGBA", (1, 1), (0, 0, 0, 255))
    height = max(glyph.height for glyph in glyphs.values())
    placements = []
    width = 1
    for line in lines:
        positions = layout_line(line, kerning)
        for code, x in positions:
            width = max(width, x + glyphs[code].width)
        placements.append(positions)
    line_advance = height + 4
    image = Image.new("RGBA", (width, (len(lines) - 1) * line_advance + height), (0, 0, 0, 255))
    for row, positions in enumerate(placements):
        for code, x in positions:
            image.alpha_composite(glyphs[code], (x, row * line_advance))
    return image


def export_kerning_csv(path, alphabet, glyphs, kerning):
    """Export nonzero kerning adjustments and actual closest-pixel gaps."""
    pixels = {}
    for character in alphabet:
        image = glyphs[ord(character)]
        alpha = image.getchannel("A")
        pixels[character] = [(x, y) for y in range(image.height)
                             for x in range(image.width) if alpha.getpixel((x, y))]
    count = 0
    with Path(path).open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["left", "right", "spacing_px", "advance_px", "kerning_px", "min_gap_px"])
        for left in alphabet:
            for right in alphabet:
                advance = kerning.advance(ord(left), ord(right))
                adjustment = advance - kerning.nominal_advance(ord(left))
                if adjustment == 0:
                    continue
                # Measure all pixel pairs, including rows beyond the search radius.
                squared = min(((advance + bx - ax)**2 + (by - ay)**2
                               for ax, ay in pixels[left] for bx, by in pixels[right]),
                              default=None)
                gap = "" if squared is None else f"{math.sqrt(squared) - 1:.6f}"
                writer.writerow([left, right, kerning.spacing, advance, adjustment, gap])
                count += 1
    return count


def compose_pairs(glyphs, kerning):
    """Render every glyph pair, ordered by code point, without a space dependency."""
    codes = sorted(glyphs)
    if not codes:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 255))
    height = max(glyph.height for glyph in glyphs.values())
    separator = glyphs[32].width if 32 in glyphs else 12
    separator += 2 * kerning.spacing
    rows = []
    width = 1
    for left in codes:
        x = 0
        placements = []
        for right in codes:
            advance = kerning.advance(left, right)
            placements.extend([(left, x), (right, x + advance)])
            pair_width = max(glyphs[left].width, advance + glyphs[right].width)
            width = max(width, x + pair_width)
            x += pair_width + separator
        rows.append(placements)
    image = Image.new("RGBA", (width, (len(rows)-1)*(height+4)+height), (0, 0, 0, 255))
    for row, placements in enumerate(rows):
        for code, x in placements:
            image.alpha_composite(glyphs[code], (x, row * (height + 4)))
    return image


def render(text, source, output, aseprite, spacing=SPACING, layer="Glyphs", pairs=False,
           kerning_slack=KERNING_SLACK, widen_pairs=WIDEN_PAIRS):
    glyphs = load_glyphs(source, aseprite, layer)
    if kerning_slack or widen_pairs:
        from .pipeline import prepare_font
        kerning = prepare_font(glyphs, spacing, kerning_slack, widen_pairs).kerning
    else:
        kerning = AutoKerning(glyphs, spacing)
    # c=0 is the reserved missing-glyph artwork, not a mapped text character.
    # Keep it available for normal rendering but omit it from pair samples/CSV.
    sample_glyphs = (GlyphCollection(
        {code: image for code, image in glyphs.items() if code != 0},
        fixed_advances=getattr(glyphs, "fixed_advances", {}),
    ) if pairs else glyphs)
    image = compose_pairs(sample_glyphs, kerning) if pairs else compose(text, glyphs, kerning)
    image.save(output, format="PNG")
    if pairs:
        alphabet = [chr(code) for code in sorted(sample_glyphs)]
        csv_path = Path(output).with_name("kerning_pairs.csv")
        count = export_kerning_csv(csv_path, alphabet, sample_glyphs, kerning)
        print(f"Saved {csv_path} ({count} nonzero kerning pairs)")
    return image.size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "text",
        nargs="?",
        default="",
        help="Text to render; unsupported characters use .notdef (or are skipped if absent)",
    )
    parser.add_argument(
        "--source", type=Path, default=DEFAULT_SOURCE
    )
    parser.add_argument("--output", type=Path, default=Path("output.png"))
    parser.add_argument(
        "--aseprite",
        default="aseprite",
        help="Aseprite executable (default: aseprite on PATH)",
    )
    parser.add_argument("--pairs", action="store_true", help="Render every ordered pair of available glyphs")
    parser.add_argument("--spacing", type=int, default=SPACING,
                        help=f"Target edge gap and vertical search radius in pixels (default: {SPACING})")
    parser.add_argument("--kerning-slack", type=int, default=KERNING_SLACK, metavar="N",
                        help=f"Allow up to N extra pixels per pair to reduce classes (default: {KERNING_SLACK})")
    parser.add_argument("--widen-pairs", action="store_true", default=WIDEN_PAIRS,
                        help="Require adjacent-pair placement (default: off)")
    parser.add_argument("--layer", default="Glyphs", help="Artwork layer to export (default: Glyphs)")
    args = parser.parse_args()
    if args.spacing < 0 or args.kerning_slack < 0:
        parser.error("--spacing and --kerning-slack must be nonnegative")
    try:
        width, height = render(args.text, args.source, args.output, args.aseprite,
                               args.spacing, args.layer, args.pairs, args.kerning_slack, args.widen_pairs)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(f"Saved {args.output} ({width}x{height})")


if __name__ == "__main__":
    main()
