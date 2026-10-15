#!/usr/bin/env python3
"""Compile the Aseprite RED2 bitmap glyphs and pair kerning into web fonts.

A 32-pixel-high source becomes a 2048-unit-em TrueType font with 64 font
units per source pixel. Each occupied pixel contributes to the exterior
outline, so the glyphs stay blocky and can be colored with CSS currentColor.
"""

import argparse
from collections import defaultdict
from pathlib import Path
import subprocess

from fontTools.fontBuilder import FontBuilder
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.pens.ttGlyphPen import TTGlyphPen

from render_font import AutoKerning, load_glyphs


UNITS_PER_PIXEL = 64
LINE_GAP_PIXELS = 4


def glyph_name(codepoint):
    """Give each Unicode scalar a stable, legal PostScript glyph name."""
    if codepoint <= 0xFFFF:
        return f"uni{codepoint:04X}"
    return f"u{codepoint:06X}"


def outline_contours(image):
    """Trace the boundary of the union of all nontransparent bitmap pixels.

    Edges are clockwise in image (Y-down) coordinates. At diagonal contacts,
    a right turn keeps touching pixel components as separate contours rather
    than joining them across a zero-width corner. Holes wind oppositely.
    """
    alpha = image.getchannel("A")
    pixels = {(x, y) for y in range(image.height) for x in range(image.width)
              if alpha.getpixel((x, y)) != 0}
    edges = set()
    for x, y in pixels:
        if (x, y - 1) not in pixels:
            edges.add(((x, y), (x + 1, y)))
        if (x + 1, y) not in pixels:
            edges.add(((x + 1, y), (x + 1, y + 1)))
        if (x, y + 1) not in pixels:
            edges.add(((x + 1, y + 1), (x, y + 1)))
        if (x - 1, y) not in pixels:
            edges.add(((x, y + 1), (x, y)))

    outgoing = defaultdict(list)
    for start, end in edges:
        outgoing[start].append(end)
    remaining = set(edges)
    contours = []
    while remaining:
        start, end = min(remaining)
        points = [start]
        previous = start
        current = end
        while True:
            remaining.remove((previous, current))
            if current == start:
                break
            points.append(current)
            choices = [candidate for candidate in outgoing[current]
                       if (current, candidate) in remaining]
            if not choices:
                raise ValueError("Unclosed bitmap boundary")

            dx, dy = current[0] - previous[0], current[1] - previous[1]

            def turn_preference(candidate):
                ex, ey = candidate[0] - current[0], candidate[1] - current[1]
                cross = dx * ey - dy * ex
                return (2 if cross > 0 else 1 if cross == 0 else 0, candidate)

            following = max(choices, key=turn_preference)
            previous, current = current, following

        # Discard intermediate points on straight runs, including the seam.
        simplified = []
        for i, point in enumerate(points):
            before = points[i - 1]
            after = points[(i + 1) % len(points)]
            if (point[0] - before[0]) * (after[1] - point[1]) != (
                    point[1] - before[1]) * (after[0] - point[0]):
                simplified.append(point)
        if len(simplified) < 4:
            raise ValueError("Degenerate bitmap contour")
        contours.append(simplified)
    return contours


def glyph_from_bitmap(image):
    """Convert pixel outlines into a TrueType quadratic-outline glyph."""
    pen = TTGlyphPen(None)
    for contour in outline_contours(image):
        points = [(x * UNITS_PER_PIXEL, (image.height - y) * UNITS_PER_PIXEL)
                  for x, y in contour]
        pen.moveTo(points[0])
        for point in points[1:]:
            pen.lineTo(point)
        pen.closePath()
    return pen.glyph()


def verify_two_glyph_history(kerning):
    """Verify the FPGA's two-history recurrence reproduces full layout.

    For any four characters A B C D, the earliest A must never constrain D
    more strongly than the last two glyphs B and C already do. This local
    property proves the recurrence works for arbitrarily long strings.
    Return the number of ordered quadruples checked, or raise ValueError with
    an actual counterexample. It is deliberately checked for every export:
    different artwork and spacing values can invalidate the property.
    """
    codes = sorted(kerning.glyphs)
    advances = {left: {right: kerning.advance(left, right) for right in codes}
                for left in codes}
    for a in codes:
        row_a = advances[a]
        for b in codes:
            row_b = advances[b]
            xb = row_a[b]
            for c in codes:
                row_c = advances[c]
                xc = max(row_a[c], xb + row_b[c])
                for d in codes:
                    if row_a[d] > max(xb + row_b[d], xc + row_c[d]):
                        example = ''.join(chr(code) for code in (a, b, c, d))
                        raise ValueError(
                            f"Two-glyph history is insufficient for {example!r} "
                            f"at spacing {kerning.spacing}; exact FPGA/Python "
                            "layout equivalence cannot be guaranteed."
                        )
    return len(codes) ** 4


def build_kerning_states(glyphs, kerning):
    """Compile the FPGA's two-glyph lookbehind into a finite-state machine.

    The FPGA places C at max(x_B + advance(B,C), x_A + advance(A,C)),
    where A,B are the previous two glyphs. A state stores the current glyph
    and the vector of required advances to every possible next character.
    States with identical vectors are interchangeable, even if their actual
    preceding glyphs differ. This merges 69*69*possible-gap histories into
    a much smaller deterministic machine.

    Return (base states by codepoint, reachable states, transition table).
    Each transition returns (next state, required origin advance in pixels).
    """
    codes = sorted(glyphs)
    advances = {(left, right): kerning.advance(left, right)
                for left in codes for right in codes}

    def state(older, previous, step):
        if older is None:
            return previous, tuple(advances[previous, right] for right in codes)
        return previous, tuple(max(advances[previous, right],
                                   advances[older, right] - step)
                               for right in codes)

    base = {code: state(None, code, 0) for code in codes}
    states = list(base.values())
    known = set(states)
    transitions = {}
    # Breadth-first enumeration: include every state reachable from a first
    # glyph, even those that appear only after arbitrarily long strings.
    for current_state in states:
        previous, steps = current_state
        for right, step in zip(codes, steps):
            target = state(previous, right, step)
            transitions[(current_state, right)] = (target, step)
            if target not in known:
                known.add(target)
                states.append(target)
    return base, states, transitions


def build_font(glyphs, spacing=4):
    """Return a TrueType font implementing FPGA two-history placement.

    GSUB contextual alternates encode the lookbehind state invisibly in glyph
    identities. GPOS then adjusts the adjacent pair based on that state.
    Unlike enumerated triple corrections, this remains correct over long text.
    """
    if not glyphs:
        raise ValueError("Cannot export a font with no glyphs")
    if spacing < 0:
        raise ValueError("spacing must be nonnegative")
    height = max(image.height for image in glyphs.values())
    if any(image.height != height for image in glyphs.values()):
        raise ValueError("All glyph slices must have the same height")
    units_per_em = height * UNITS_PER_PIXEL
    if not 16 <= units_per_em <= 16384:
        raise ValueError("Glyph height must produce a valid TrueType units-per-em")
    if any(image.width < 1 for image in glyphs.values()):
        raise ValueError("Glyph widths must be positive")

    codes = sorted(glyphs)
    kerning = AutoKerning(glyphs, spacing)
    verify_two_glyph_history(kerning)
    base, states, transitions = build_kerning_states(glyphs, kerning)
    names = {}
    variant_counts = defaultdict(int)
    for state in states:
        code = state[0]
        if state == base[code]:
            names[state] = glyph_name(code)
        else:
            variant_counts[code] += 1
            names[state] = f"{glyph_name(code)}.s{variant_counts[code]:02d}"

    order = [".notdef"] + [names[state] for state in states]
    builder = FontBuilder(units_per_em, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({code: names[base[code]] for code in codes})
    blank = TTGlyphPen(None).glyph()
    outlines = {".notdef": blank}
    metrics = {".notdef": (16 * UNITS_PER_PIXEL, 0)}
    for state in states:
        code = state[0]
        outlines[names[state]] = glyph_from_bitmap(glyphs[code])
        metrics[names[state]] = ((glyphs[code].width + spacing) * UNITS_PER_PIXEL, 0)
    builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics(metrics)
    ascent = height * UNITS_PER_PIXEL
    line_gap = LINE_GAP_PIXELS * UNITS_PER_PIXEL
    builder.setupHorizontalHeader(ascent=ascent, descent=0, lineGap=line_gap)
    builder.setupNameTable({
        "familyName": "RED2",
        "styleName": "Regular",
        "uniqueFontIdentifier": "RED2-Regular-1.000",
        "fullName": "RED2 Regular",
        "psName": "RED2-Regular",
        "version": "Version 1.000",
    })
    builder.setupOS2(
        version=4,
        sTypoAscender=ascent,
        sTypoDescender=0,
        sTypoLineGap=line_gap,
        usWinAscent=ascent,
        usWinDescent=0,
        fsSelection=0xC0,  # REGULAR | USE_TYPO_METRICS
    )
    builder.setupPost()
    builder.setupMaxp()
    builder.setupHead(unitsPerEm=units_per_em, fontRevision=1.0)

    substitutions = []
    positions = []
    for previous_state in states:
        previous_code = previous_state[0]
        for right in codes:
            target, step = transitions[(previous_state, right)]
            if target != base[right]:
                # The right-hand glyph is still its original cmap glyph when
                # scanned left-to-right; assign its new machine state.
                substitutions.append(
                    f"  sub {names[previous_state]} {names[base[right]]}' "
                    f"by {names[target]};")
            adjustment = step - glyphs[previous_code].width - spacing
            if adjustment:
                # GSUB runs before GPOS: match the state glyph actually
                # produced by the transition, not the original Unicode glyph.
                positions.append(
                    f"  pos {names[previous_state]} {names[target]} "
                    f"{adjustment * UNITS_PER_PIXEL};")
    features = ""
    if substitutions:
        features += "feature calt {\n" + "\n".join(substitutions) + "\n} calt;\n"
    if positions:
        features += "feature kern {\n" + "\n".join(positions) + "\n} kern;\n"
    if features:
        addOpenTypeFeaturesFromString(builder.font, features)
    return builder.font, len(positions)


CSS = '''/* 1 artwork pixel = 1 CSS pixel at font-size: 32px. */
@font-face {
  font-family: "RED2";
  src: url("./RED2-Regular.woff2") format("woff2"),
       url("./RED2-Regular.ttf") format("truetype");
  font-style: normal;
  font-weight: 400;
  font-display: swap;
}

.red2 {
  font-family: "RED2", monospace;
  font-size: 32px;
  line-height: 1.125; /* Source height 32px + renderer line gap 4px. */
  font-kerning: normal;
  font-variant-ligatures: contextual;
  font-feature-settings: "kern" 1, "calt" 1;
  font-synthesis: none;
  white-space: pre-wrap;
}
'''

DEMO = '''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RED2 web font</title>
  <link rel="stylesheet" href="./red2-font.css">
  <style>
    body { background: #10151a; color: #ecf9ff; padding: 2rem; }
    p { margin: 0 0 1rem; }
    .sample { overflow-wrap: anywhere; }
  </style>
</head>
<body>
  <p>RED2 font test — change color and size with CSS:</p>
  <p class="red2 sample">THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG.
0123456789  AVATAR  WAVY
P.O.  L'.  #,,4
HELLO, WORLD!</p>
  <p style="color: #ffaf5f" class="red2">PIXEL OUTLINES</p>
</body>
</html>
'''


def export_web_font(glyphs, directory, spacing=4):
    """Write the WOFF2, TTF, stylesheet, and sample page."""
    font, pairs = build_font(glyphs, spacing)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ttf = directory / "RED2-Regular.ttf"
    woff2 = directory / "RED2-Regular.woff2"
    font.save(ttf)
    font.flavor = "woff2"
    font.save(woff2)
    (directory / "red2-font.css").write_text(CSS, encoding="utf-8")
    (directory / "demo.html").write_text(DEMO, encoding="utf-8")
    return ttf, woff2, pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).with_name("RED2 Font.aseprite"))
    parser.add_argument("--aseprite", default="aseprite")
    parser.add_argument("--layer", default="Glyphs")
    parser.add_argument("--spacing", type=int, default=4)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.spacing < 0:
        parser.error("--spacing must be nonnegative")
    try:
        glyphs = load_glyphs(args.source, args.aseprite, args.layer)
        ttf, woff2, pairs = export_web_font(glyphs, args.output_dir, args.spacing)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(f"Exported {len(glyphs)} source glyphs and {pairs} state-dependent kerning rules")
    print(f"Saved {ttf} ({ttf.stat().st_size:,} bytes)")
    print(f"Saved {woff2} ({woff2.stat().st_size:,} bytes)")
    print(f"CSS: {args.output_dir / 'red2-font.css'}")
    print(f"Demo: {args.output_dir / 'demo.html'}")


if __name__ == "__main__":
    main()
