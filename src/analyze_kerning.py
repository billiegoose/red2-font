#!/usr/bin/env python3
"""Find history-dependent triplets and propose a wider, pair-only table.

The normal renderer/exporter are deliberately unchanged. Run this after an
artwork edit to regenerate the proof, candidate advances, and visual comparison.
"""

import argparse
from collections import Counter
import csv
from pathlib import Path
from .paths import ROOT, DEFAULT_SOURCE

from PIL import Image, ImageDraw, ImageFont

from .kerning import TableKerning, dependent_triplets, widen_pairs

from .font import AutoKerning, compose, load_glyphs


def label(code):
    return ".notdef" if code == 0 else chr(code)


def write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(header)
        writer.writerows(rows)


def write_comparison(path, glyphs, original, candidate):
    samples = [
        "THE QUICK BROWN FOX / AVATAR / 0123456789",
        "P.O.  F.T.  P,4  L`/  L'/  L'.",
        ".O  ,4  '/  `/  :T  _4",
        "PRICE: $12.50 + 8% TAX = $13.50",
        "RED2 FONT: VERSION 1.0 | STATUS: READY",
        "HELLO, WORLD!  A_B  C^D  *E?",
    ]
    rows = [(text, compose(text, glyphs, original),
             compose(text, glyphs, candidate)) for text in samples]
    width = max(max(a.width, b.width) for _, a, b in rows) + 36
    image = Image.new("RGB", (width, len(rows) * 128 + 48), "#101419")
    draw = ImageDraw.Draw(image)
    draw.text((18, 12), "RED2: CURRENT FULL-HISTORY LAYOUT / WIDER PAIR-ONLY CANDIDATE",
              fill="#d0d8e0")
    for i, (_, a, b) in enumerate(rows):
        y = 48 + i * 128
        draw.text((18, y), f"CURRENT: {a.width}px", fill="#aeb9c5")
        image.paste(a.convert("RGB"), (18, y + 16))
        draw.text((18, y + 56), f"PAIR ONLY: {b.width}px (+{b.width - a.width})",
                  fill="#83d6bc")
        image.paste(b.convert("RGB"), (18, y + 72))
    image.save(path)
    return [(text, a.width, b.width) for text, a, b in rows]


def write_triplet_comparison(path, glyphs, advances, widened, violations):
    """Render every dependent triplet, with three explicit placement policies.

    All three panels share an origin and use exact bitmap pixels at 2x scale.
    The first two differ only in C's origin. The third uses widened adjacent
    pairs, which can change both B and C. IDs refer to dependent-triplets.csv.
    """
    scale = 2
    columns = 6
    margin, gutter, panel_width, row_height = 24, 20, 412, 100
    header_height = 156
    per_column = max(1, (len(violations) + columns - 1) // columns)
    width = 2 * margin + columns * panel_width + (columns - 1) * gutter
    height = header_height + per_column * row_height + margin
    background, foreground = "#101419", "#e5edf5"
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=25)
    body_font = ImageFont.load_default(size=16)
    small_font = ImageFont.load_default(size=13)
    draw.text((margin, 18), f"RED2 / ALL {len(violations)} HISTORY-DEPENDENT TRIPLETS",
              font=title_font, fill=foreground)
    draw.text((margin, 54), "1  NO A CORRECTION: original adjacent pairs     "
              "2  WITH A CORRECTION: current full-history placement     "
              "3  WIDENED PAIRS: proposed adjacent-pair table",
              font=body_font, fill=foreground)
    draw.text((margin, 82), "C is colored; A and B are white. A+ = required correction to C. "
              "W+ = additional C displacement of widened pairs versus current placement. "
              "Exact bitmap pixels at 2x. Read down, then right.",
              font=small_font, fill="#b2c0cd")
    draw.text((margin, 104), "Sorted by A+ (largest first). Row IDs match dependent-triplets.csv. "
              "All views start at the same origin; the wider table can also move B.",
              font=small_font, fill="#b2c0cd")
    colors = ["#ffb86b", "#8de1b8", "#8fc7ff"]
    for column in range(columns):
        x = margin + column * (panel_width + gutter)
        for i, (heading, color) in enumerate(zip(
                ["NO A CORRECTION", "WITH A CORRECTION", "WIDENED PAIRS"], colors)):
            draw.text((x + 8 + i * 132, header_height - 24), heading,
                      font=small_font, fill=color)
    ordered = sorted(enumerate(violations, 1), key=lambda item: (-item[1][3], item[1][:3]))
    manifest = []
    for index, (row_id, (a, b, c, extra)) in enumerate(ordered):
        column, row = divmod(index, per_column)
        x = margin + column * (panel_width + gutter)
        y = header_height + row * row_height
        ab, bc = advances[a][b], advances[b][c]
        uncorrected = ab + bc
        corrected = max(advances[a][c], uncorrected)
        candidate_b = widened[a][b]
        candidate_c = candidate_b + widened[b][c]
        if corrected - uncorrected != extra or candidate_c < corrected:
            raise AssertionError("Triplet comparison displacement is inconsistent")
        if row % 2 == 0:
            draw.rectangle((x, y, x + panel_width - 1, y + row_height - 1), fill="#19212a")
        text = ''.join(label(code) for code in (a, b, c))
        draw.text((x + 8, y + 5), f"#{row_id:03}   {text}    A+{extra}px    W+{candidate_c - corrected}px",
                  font=body_font, fill=foreground)
        positions = [(0, ab, uncorrected), (0, ab, corrected), (0, candidate_b, candidate_c)]
        for i, (origins, color) in enumerate(zip(positions, colors)):
            for code, origin, fill in zip((a, b, c), origins, ("#ffffff", "#ffffff", color)):
                glyph = Image.new("RGBA", glyphs[code].size, fill)
                glyph.putalpha(glyphs[code].getchannel("A"))
                glyph = glyph.resize((glyph.width * scale, glyph.height * scale),
                                     Image.Resampling.NEAREST)
                image.paste(glyph, (x + 8 + i * 132 + origin * scale, y + 28), glyph)
        manifest.append((row_id, text, column + 1, row + 1, ab, uncorrected,
                         corrected, candidate_b, candidate_c, extra, candidate_c - corrected))
    image.save(path)
    # A small readable excerpt for inline display; the full sheet has every row.
    image.crop((margin, header_height - 26, margin + panel_width,
                header_height + min(6, len(ordered)) * row_height)).save(
                    path.with_name("triplets-comparison-detail.png"))
    write_csv(path.with_name("triplets-comparison-index.csv"),
              ["csv_row_id", "triplet", "sheet_column", "sheet_row", "original_B_x",
               "uncorrected_C_x", "corrected_C_x", "widened_B_x", "widened_C_x",
               "A_correction_px", "widening_beyond_current_px"], manifest)


def analyze(glyphs, spacing, output):
    original = AutoKerning(glyphs, spacing)
    codes = sorted(glyphs)
    advances = {a: {b: original.advance(a, b) for b in codes} for a in codes}
    violations = list(dependent_triplets(advances))
    widened = widen_pairs(advances, {a: original.nominal_advance(a) for a in codes})
    # Independent exhaustive postcondition, rather than relying on loop state.
    if any(dependent_triplets(widened)):
        raise AssertionError("Candidate still requires history")
    for a in codes:
        for b in codes:
            if a in original.fixed_advances or b in original.fixed_advances:
                if widened[a][b] != advances[a][b]:
                    raise AssertionError("Candidate changed a fixed-cell pair")
    changes = [(a, b, advances[a][b], widened[a][b]) for a in codes for b in codes
               if advances[a][b] != widened[a][b]]
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "dependent-triplets.csv",
              ["A", "B", "C", "A_code", "B_code", "C_code", "AB_px", "BC_px",
               "AC_px", "extra_px"],
              [(label(a), label(b), label(c), f"U+{a:04X}", f"U+{b:04X}",
                f"U+{c:04X}", advances[a][b], advances[b][c], advances[a][c], extra)
               for a, b, c, extra in violations])
    write_csv(output / "pair-changes.csv", ["left", "right", "old_px", "new_px", "added_px"],
              [(label(a), label(b), old, new, new - old) for a, b, old, new in changes])
    write_csv(output / "pair-advances.csv", ["left_code", "right_code", "left", "right", "advance_px"],
              [(f"U+{a:04X}", f"U+{b:04X}", label(a), label(b), widened[a][b])
               for a in codes for b in codes])
    widths = write_comparison(output / "comparison.png", glyphs, original,
                              TableKerning(original, widened))
    write_triplet_comparison(output / "triplets-comparison.png", glyphs, advances,
                            widened, violations)
    middle = Counter(b for _, b, _, _ in violations)
    report = [
        "# Pair-only kerning feasibility", "",
        f"Current artwork: {len(codes)} glyphs (including .notdef), spacing {spacing}px.", "",
        "A triplet ABC requires A exactly when `d(A,C) > d(A,B) + d(B,C)`.",
        f"**{len(violations):,} / {len(codes) ** 3:,} triplets "
        f"({100 * len(violations) / len(codes) ** 3:.3f}%) violate this.** "
        "All are listed in [dependent-triplets.csv](dependent-triplets.csv).", "",
        "[Comparison image of every triplet](triplets-comparison.png) · "
        "[Image index and exact positions](triplets-comparison-index.csv). "
        "Each row shows original pairs without A's correction, current placement "
        "with A's correction, and the proposed wider pairs. C is colored. "
        "Row IDs match the CSV; rows are sorted by correction, largest first.", "",
        "| Middle glyph B | Triplets | Largest correction |",
        "| --- | ---: | ---: |",
    ]
    for b, count in middle.most_common():
        example = max((v for v in violations if v[1] == b), key=lambda v: v[3])
        report.append(f"| U+{b:04X} ({label(b)!r}) | {count} | {example[3]}px |")
    report += ["", "## Wider candidate", "",
               "Resolve a violation by increasing B/C to at least `d(A,C) - d(A,B)`, "
               "then repeat and exhaustively verify the entire table. This preserves "
               "clearance and produces a feasible table; it is not a global optimum. "
               "Every sweep visits A, B, and C in ascending glyph-key order and "
               "updates in place, independently of dictionary insertion order.", "",
               f"- {len(changes)} of {len(codes) ** 2:,} pair entries change "
               f"({100 * len(changes) / len(codes) ** 2:.2f}%).",
               f"- Additions range from {min((new-old for _, _, old, new in changes), default=0)} "
               f"to {max((new-old for _, _, old, new in changes), default=0)} pixels; "
               f"{sum(new-old for _, _, old, new in changes)} pixels total across the table.",
               "- No advance exceeds its original nominal advance.",
               "- Fixed-cell pairs remain unchanged.",
               "- No triplets violate the inequality after widening. By induction, "
               "adjacent pairs now satisfy every earlier-glyph constraint for any string.", "",
               "[Changed pairs](pair-changes.csv) · [Full proposed pair table](pair-advances.csv)", "",
               "This changes the desired spacing: for example, a punctuation/letter pair "
               "widens even when no preceding glyph needs the extra room. Some triplets "
               "match their old contextual positions; others get wider. Existing default "
               "rendering and font exports are unchanged.", "",
               "## Visual comparison", "",
               "Each sample shows existing full-history placement above the wider candidate. "
               "Candidate placement uses the same full-history renderer; the exhaustive "
               "proof guarantees ordinary adjacent-pair layout gives identical origins.", "",
               "![Current and candidate layouts](comparison.png)", "",
               "| Sample | Current width | Pair-only width |",
               "| --- | ---: | ---: |"]
    for text, old, new in widths:
        report.append(f"| {text.replace('|', '&#124;')} | {old}px | {new}px |")
    report += ["", "Regenerate from the repository root:", "",
               "```sh", f"python -m src.analyze_kerning --spacing {spacing}", "```", ""]
    (output / "README.md").write_text("\n".join(report), encoding="utf-8")
    return len(violations), len(changes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--aseprite", default="aseprite")
    parser.add_argument("--layer", default="Glyphs")
    parser.add_argument("--spacing", type=int, default=4)
    parser.add_argument("--output-dir", type=Path, default=Path("previews/kerning-analysis"))
    args = parser.parse_args()
    if args.spacing < 0:
        parser.error("--spacing must be nonnegative")
    glyphs = load_glyphs(args.source, args.aseprite, args.layer)
    triplets, pairs = analyze(glyphs, args.spacing, args.output_dir)
    print(f"{triplets} dependent triplets; {pairs} widened pairs; candidate passes all triplets.")
    print(f"Report: {args.output_dir / 'README.md'}")


if __name__ == "__main__":
    main()
