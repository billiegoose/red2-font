#!/usr/bin/env python3
"""Measure existing and widened pair-only fonts, with recomputed GPOS classes.

This is an experiment: the production exporter and its defaults are unchanged.
"""

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from .paths import ROOT, DEFAULT_SOURCE
import random
from unittest.mock import patch

from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.misc.timeTools import timestampSinceEpoch
from fontTools.otlLib.optimize.gpos import compact
from fontTools.ttLib import TTFont
import uharfbuzz as hb

from .analyze_kerning import TableKerning, dependent_triplets, widen_pairs
from .web_font import UNITS_PER_PIXEL, build_font, build_kerning_states
from .font import AutoKerning, layout_line, load_glyphs


def class_groups(vectors):
    groups = defaultdict(list)
    for name, vector in vectors.items():
        groups[tuple(vector)].append(name)
    return list(groups.values())


def classify_matrix(matrix):
    rows = list(matrix)
    columns = list(matrix[rows[0]])
    left = class_groups({a: [matrix[a][b] for b in columns] for a in rows})
    right = class_groups({b: [matrix[a][b] for a in rows] for b in columns})
    return left, right


def class_font(font):
    """Replace explicit GPOS pairs with exact, freshly computed class pairs."""
    font = deepcopy(font)
    order = font.getGlyphOrder()
    matrix = {a: dict.fromkeys(order, 0) for a in order}
    for lookup in font["GPOS"].table.LookupList.Lookup:
        assert lookup.LookupType == 2
        for subtable in lookup.SubTable:
            assert subtable.Format == 1
            for left, pair_set in zip(subtable.Coverage.glyphs, subtable.PairSet):
                for record in pair_set.PairValueRecord:
                    matrix[left][record.SecondGlyph] = record.Value1.XAdvance
    left, right = classify_matrix(matrix)
    lines = [f"@L{i} = [{' '.join(group)}];" for i, group in enumerate(left)]
    lines += [f"@R{i} = [{' '.join(group)}];" for i, group in enumerate(right)]
    lines.append("feature kern {")
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            value = matrix[a[0]][b[0]]
            if value:
                lines.append(f"pos @L{i} @R{j} {value};")
    lines.append("} kern;")
    del font["GPOS"]
    # Existing GSUB state substitutions must survive rebuilding GPOS.
    gsub = deepcopy(font.get("GSUB"))
    addOpenTypeFeaturesFromString(font, "\n".join(lines))
    if gsub is not None:
        font["GSUB"] = gsub
    compact(font, 5)
    return font, (len(left), len(right))


def save_sizes(font, stem, output):
    font = deepcopy(font)
    font.recalcTimestamp = False
    font.flavor = None
    ttf = output / f"{stem}.ttf"
    woff = output / f"{stem}.woff2"
    font.save(ttf)
    font.flavor = "woff2"
    font.save(woff)
    decoded = TTFont(ttf)
    sizes = {tag: entry.length for tag, entry in decoded.reader.tables.items()}
    return {"ttf": ttf.stat().st_size, "woff2": woff.stat().st_size,
            "glyphs": len(decoded.getGlyphOrder()), "tables": sizes}


def shaper(font):
    font = deepcopy(font)
    font.flavor = None
    font.recalcTimestamp = False
    data = io.BytesIO()
    font.save(data)
    face = hb.Face(data.getvalue())
    result = hb.Font(face)
    result.scale = (face.upem, face.upem)
    return result


def verify_shaping(font, sequences, kerning):
    font = shaper(font)
    for codes in sequences:
        buffer = hb.Buffer()
        buffer.add_codepoints(codes)
        buffer.guess_segment_properties()
        hb.shape(font, buffer, {"kern": True, "calt": True})
        actual = []
        x = 0
        for position in buffer.glyph_positions:
            actual.append((x + position.x_offset) / UNITS_PER_PIXEL)
            x += position.x_advance
        expected = [x for _, x in layout_line(codes, kerning)]
        if actual != expected:
            raise AssertionError(f"Shaping mismatch: {codes}: {actual} != {expected}")


def main():
    root = ROOT
    output = root / "previews/kerning-analysis/font-sizes"
    output.mkdir(parents=True, exist_ok=True)
    glyphs = load_glyphs(root / "RED2 Font.aseprite", "aseprite")
    original = AutoKerning(glyphs)
    codes = sorted(glyphs)
    table = {a: {b: original.advance(a, b) for b in codes} for a in codes}
    widened = widen_pairs(table, {a: original.nominal_advance(a) for a in codes})
    candidate = TableKerning(original, widened)
    results = {"source_glyphs": len(codes), "spacing": 4, "pair_classes": {},
               "state_advance_classes": {}}
    for name, matrix in [("original", table), ("widened", widened)]:
        adjustments = {a: {b: value - original.nominal_advance(a)
                           for b, value in row.items()} for a, row in matrix.items()}
        results["pair_classes"][name] = {
            "advances": list(map(len, classify_matrix(matrix))),
            "adjustments": list(map(len, classify_matrix(adjustments))),
        }
        _, states, transitions = build_kerning_states(glyphs, TableKerning(original, matrix))
        state_matrix = {s: {b: transitions[s, b][1] for b in codes} for s in states}
        results["state_advance_classes"][name] = {
            "states": len(states),
            "advance_classes": list(map(len, classify_matrix(state_matrix))),
        }
    print("Building current font...", flush=True)
    current_font, current_rules = build_font(glyphs)
    print("Building widened pair-only font...", flush=True)
    with patch("src.web_font.AutoKerning", return_value=candidate):
        pair_font, pair_rules = build_font(glyphs)
    # Use identical timestamps in every variant for a fair compressed comparison.
    timestamp = timestampSinceEpoch(datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp())
    for font in (current_font, pair_font):
        font.recalcTimestamp = False
        font["head"].created = timestamp
        font["head"].modified = timestamp
    results["explicit_rules"] = {"current": current_rules, "widened": pair_rules}
    variants = {"current-explicit": current_font, "widened-explicit": pair_font}
    for name, font in list(variants.items()):
        grouped, groups = class_font(font)
        key = name.replace("explicit", "classes")
        variants[key] = grouped
        results[key] = {"gpos_equivalence_classes": groups}
    # All mapped pairs, all affected triples, plus mixed longer strings.
    sequences = [[a, b] for a in codes for b in codes]
    sequences += [[a, b, c] for a, b, c, _ in dependent_triplets(table)]
    rng = random.Random(0)
    sequences += [rng.choices(codes, k=rng.randrange(1, 80)) for _ in range(2000)]
    for name, font in variants.items():
        verify_shaping(font, sequences, candidate if name.startswith("widened") else original)
        results.setdefault(name, {}).update(save_sizes(font, name, output))
        print(name, json.dumps(results[name]), flush=True)
    results["shaping_checks_per_variant"] = len(sequences)
    (output / "sizes.json").write_text(json.dumps(results, indent=2) + "\n")
    report = ["# Measured kerning size comparison", "",
              f"Current artwork, {len(codes)} source glyphs, spacing 4. "
              "Every font is rebuilt from the same artwork with identical timestamps. "
              "Class versions recompute exact row/column equivalence and use "
              "FontTools GPOS class compaction at level 5.", "",
              "| Variant | TTF bytes | WOFF2 bytes | GPOS bytes | GSUB bytes | Glyphs |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name in ("current-explicit", "widened-explicit", "current-classes", "widened-classes"):
        result = results[name]
        report.append(f"| {name} | {result['ttf']:,} | {result['woff2']:,} | "
                      f"{result['tables'].get('GPOS', 0):,} | {result['tables'].get('GSUB', 0):,} | "
                      f"{result['glyphs']} |")
    report += ["", "## Pair-table classes", "",
               "These are exact equivalence classes over the complete 85-by-85 table. "
               "An advance row includes glyph width; an adjustment row subtracts the "
               "left glyph's nominal advance. Identical glyph artwork is not required "
               "to share a kerning class.", "",
               "| Table | Advance classes (left x right) | Adjustment classes (left x right) |",
               "| --- | ---: | ---: |"]
    for name, counts in results["pair_classes"].items():
        report.append(f"| {name} | {' x '.join(map(str, counts['advances']))} | "
                      f"{' x '.join(map(str, counts['adjustments']))} |")
    report += ["", "For a hardware table of absolute advances, 65 x 51 classes "
               "require 3,315 one-byte entries, plus 170 bytes for two one-byte "
               "glyph-to-class maps: 3,485 bytes total. Values fit in five bits; "
               "packing can reduce this further. This assumes the nominal glyph "
               "metrics and glyph bitmap storage are accounted for separately.", "",
               "If instead storing the current reachable-state advance matrix, "
               "its 176 x 85 entries group into 132 x 51 exact advance classes "
               "(6,732 cells). The pair-only version groups into 65 x 51 (3,315 cells), "
               "50.8% fewer cells. This comparison counts only advance values: "
               "the old scheme additionally needs state transitions and a way to "
               "identify the incoming state. It is not a measurement of an existing "
               "FPGA ROM binary."]
    report += ["", "The Python renderer already stores pair advances. Its pair-table "
               "storage does not shrink by removing history; the proposed table still "
               "has 85 x 85 entries before class grouping. The web-font savings come "
               "from removing contextual substitution rules, alternate glyph outlines, "
               "and state-dependent positioning. This repository has no FPGA ROM "
               "generator or hardware class-map encoding, so the font measurements "
               "are actual byte sizes; a hardware ROM size depends on that encoding.", "",
               f"Validation: each of four variants matches its intended Python layout "
               f"in HarfBuzz for {len(sequences):,} sequences: all {len(codes)**2:,} pairs, "
               f"all {len(list(dependent_triplets(table)))} dependent triplets, and 2,000 randomized strings. "
               "The widened table was also exhaustively proved pair-only safe before "
               "the size experiment. Exporter defaults are unchanged.", "",
               "Regenerate: `python -m src.measure_kerning_sizes`.", ""]
    (output / "README.md").write_text("\n".join(report))


if __name__ == "__main__":
    main()
