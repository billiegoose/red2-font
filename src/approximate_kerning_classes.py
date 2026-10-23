#!/usr/bin/env python3
"""Export compact kerning classes with bounded kerning slack.

--kerning-slack N allows up to N extra pixels per pair to reduce class count.
Slack defaults to two pixels and is measured against the original table by default.
--widen-pairs optionally enforces adjacent-pair placement before approximation.
The production font and FPGA exporters are unchanged.
"""

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
from .paths import ROOT, DEFAULT_SOURCE
from .defaults import KERNING_SLACK

from PIL import Image, ImageDraw

from .analyze_kerning import TableKerning, dependent_triplets, label, widen_pairs, write_csv
from .font import AutoKerning, compose, load_glyphs


from .kerning import (STRATEGIES, ClassTable, exact_classes, compress_classes,
    incompatibility_clique, prepare_baseline, verify_required_history, select_candidate)


def experiment(name, advances, nominal, tolerance, output, protected=(), *, history=2):
    codes = sorted(advances)
    adjustments = {a: {b: nominal[a] - advances[a][b] for b in codes} for a in codes}
    exact = list(map(len, exact_classes(adjustments)))
    variants, candidates, proofs = [], [], {}
    for strategy in STRATEGIES:
        classes = compress_classes(adjustments, tolerance, strategy, protected)
        expanded = classes.expand()
        proposed = {a: {b: nominal[a] - expanded[a][b] for b in codes} for a in codes}
        errors = [proposed[a][b] - advances[a][b] for a in codes for b in codes]
        if not all(0 <= extra <= tolerance for extra in errors):
            raise AssertionError("Spacing error exceeded the original per-pair budget")
        if any(proposed[a][b] != advances[a][b] for a, b in protected):
            raise AssertionError("Protected pair changed")
        # Approximation need not preserve the weaker two-predecessor property.
        # Verify the decoded table, and never select an invalid candidate.
        key = tuple(tuple(proposed[a][b] for b in codes) for a in codes)
        if key not in proofs:
            print(f"Checking {name}/{strategy}: {history}-glyph history...", flush=True)
            try:
                proofs[key] = {"history_valid": True,
                               "history_combinations_checked": verify_required_history(proposed, history)}
            except ValueError as error:
                proofs[key] = {"history_valid": False, "history_failure": str(error)}
        entry = {"strategy": strategy, "left": len(classes.left), "right": len(classes.right),
                 "cells": len(classes.left) * len(classes.right),
                 "padded_cells": 1 << ((len(classes.left) - 1).bit_length()
                                       + (len(classes.right) - 1).bit_length()),
                 "changed_pairs": sum(error > 0 for error in errors),
                 "added_pixels_total": sum(errors), "max_added_pixels": max(errors),
                 "extra_pixel_histogram": {str(extra): count for extra, count in sorted(Counter(errors).items())},
                 "dependent_triplets": sum(1 for _ in dependent_triplets(proposed)),
                 **proofs[key]}
        variants.append(entry)
        candidates.append((classes, proposed))
    chosen = select_candidate(variants)
    classes, proposed = candidates[chosen]
    certificates = {axis_name: incompatibility_clique(adjustments, tolerance, axis)
                    for axis, axis_name in enumerate(("left", "right"))}
    result = {"name": name, "glyphs": len(codes), "tolerance": tolerance,
              "kerning_slack": tolerance,
              "selection_policy": "padded-cells, descending pixel-error counts, cells, maximum dimension, strategy order",
              "required_history_glyphs": history, "widen_pairs": history == 1,
              "exact_classes": exact, "variants": variants, "chosen": variants[chosen],
              "lower_bounds": {axis: len(clique) for axis, clique in certificates.items()},
              "incompatible_glyphs": certificates,
              "classes": {"left": classes.left, "right": classes.right, "values": classes.values}}
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
    write_csv(output / f"{name}-pair-advances.csv",
              ["left_code", "right_code", "left", "right", "baseline_px", "approximate_px", "extra_px"],
              [(f"U+{a:04X}", f"U+{b:04X}", label(a), label(b), advances[a][b], proposed[a][b],
                proposed[a][b] - advances[a][b]) for a in codes for b in codes])
    return result, proposed


def read_fpga_rom(path):
    constants = {s.targets[0].id: ast.literal_eval(s.value)
                 for s in ast.parse(path.read_text()).body if isinstance(s, ast.Assign)}
    metadata = constants["METADATA"]
    codes = [c for c, m in enumerate(metadata) if m & 256]
    codepoints = {c: c if c < 128 else constants["UNICODE_CODEPOINTS"][c - 128] for c in codes}
    nominal = {a: (metadata[a] & 31) + 4 for a in codes}
    advances = {a: {b: nominal[a] - constants["KERNING"][
        ((metadata[a] >> 31) & 63) * constants["KERNING_RIGHT_COUNT"] + ((metadata[b] >> 37) & 63)]
        for b in codes} for a in codes}
    return ({codepoints[a]: {codepoints[b]: value for b, value in row.items()}
             for a, row in advances.items()}, {codepoints[a]: value for a, value in nominal.items()})


def write_preview(path, glyphs, kerning, baseline, proposed, tolerance, widened=False):
    samples = ["THE QUICK BROWN FOX / AVATAR / 0123456789",
               "PRICE: $12.50 + 8% TAX = $13.50",
               "RED2 FONT: VERSION 1.0 | STATUS: READY",
               "HELLO, WORLD!  A_B  C^D  *E?",
               "P.O.  F.T.  P,4  L`/  L'/  L'."]
    pairs = [(text, compose(text, glyphs, TableKerning(kerning, baseline)),
              compose(text, glyphs, TableKerning(kerning, proposed))) for text in samples]
    image = Image.new("RGB", (max(max(a.width, b.width) for _, a, b in pairs) + 36,
                              len(pairs) * 128 + 48), "#101419")
    draw = ImageDraw.Draw(image)
    baseline_name = "WIDENED PAIR-ONLY" if widened else "ORIGINAL KERNING"
    draw.text((18, 12), f"{baseline_name} / APPROXIMATE CLASSES (ALLOW UP TO {tolerance}px EXTRA PER PAIR)",
              fill="#d0d8e0")
    for i, (_, a, b) in enumerate(pairs):
        y = 48 + i * 128
        draw.text((18, y), f"BASELINE: {a.width}px", fill="#aeb9c5")
        image.paste(a.convert("RGB"), (18, y + 16))
        draw.text((18, y + 56), f"APPROXIMATE: {b.width}px (+{b.width - a.width})", fill="#83d6bc")
        image.paste(b.convert("RGB"), (18, y + 72))
    image.save(path)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kerning-slack", "--extra-space", dest="kerning_slack", type=int, default=KERNING_SLACK,
                        metavar="N", help=f"Allow 0..N extra pixels per pair to reduce kerning classes (default: {KERNING_SLACK})")
    parser.add_argument("--widen-pairs", action="store_true",
                        help="Enforce one-glyph history before approximation (default: retain original kerning)")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--aseprite", default="aseprite")
    parser.add_argument("--fpga-rom", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.kerning_slack < 0:
        parser.error("--kerning-slack must be nonnegative")
    if args.output_dir is None:
        directory = "approximate-classes" if args.widen_pairs else "approximate-classes-unwidened"
        args.output_dir = Path("previews/kerning-analysis") / directory
    glyphs = load_glyphs(args.source, args.aseprite)
    kerning = AutoKerning(glyphs)
    codes = sorted(glyphs)
    raw = {a: {b: kerning.advance(a, b) for b in codes} for a in codes}
    baseline = prepare_baseline(raw, {a: kerning.nominal_advance(a) for a in codes}, args.widen_pairs)
    history = 1 if args.widen_pairs else 2
    protected = {(a, b) for a in codes for b in codes
                 if a in glyphs.fixed_advances or b in glyphs.fixed_advances}
    result, proposed = experiment("current-font", baseline, {a: glyphs[a].width + 4 for a in codes},
                                  args.kerning_slack, args.output_dir, protected, history=history)
    results = [result]
    if args.fpga_rom:
        advances, nominal = read_fpga_rom(args.fpga_rom)
        hardware, _ = experiment("fpga-snapshot", prepare_baseline(advances, nominal, args.widen_pairs), nominal,
                                 args.kerning_slack, args.output_dir, history=history)
        results.append(hardware)
    write_preview(args.output_dir / "comparison.png", glyphs, kerning, baseline, proposed,
                  args.kerning_slack, args.widen_pairs)
    baseline_description = "widened pair-only baseline" if args.widen_pairs else "original, un-widened kerning table"
    report = ["# Approximate kerning classes", "",
              f"**Kerning slack: {args.kerning_slack}px.** Allow up to "
              f"{args.kerning_slack} extra pixel(s) per pair, relative to the "
              f"{baseline_description}. Optional triangle widening is "
              f"{'enabled' if args.widen_pairs else 'disabled'}. "
              f"The required placement history is {history} preceding glyph(s). "
              "Extra space can accumulate across a string. "
              "The current font includes .notdef and fixed-cell symbols. Every pair involving "
              "a fixed-cell glyph is protected from additional widening.", "",
              "For each class block, store the minimum original adjustment. Accept a merge "
              "only if its original maximum minus original minimum is <= N. Bounds refer "
              "to the original baseline, so repeated merges do not accumulate error beyond N. "
              "Glyph order, strategy order, and score tie breaks are deterministic.", "",
              "## Measurements", "",
              "| Source | Exact classes | Selected approximate classes | Changed pairs | Added pixels | Left lower bound | Right lower bound |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in results:
        s = r["chosen"]
        report.append(f"| {r['name']} ({r['glyphs']} glyphs) | "
                      f"{r['exact_classes'][0]} x {r['exact_classes'][1]} | "
                      f"{s['left']} x {s['right']} | {s['changed_pairs']} | {s['added_pixels_total']} | "
                      f"{r['lower_bounds']['left']} | {r['lower_bounds']['right']} |")
    report += ["", "Candidates are ranked first by power-of-two padded ROM capacity, "
               "then by the number of pairs at each pixel error, from largest to smallest. "
               "For slack 2 this prefers fewer +2px pairs, then fewer +1px pairs. "
               "Exact cell count, maximum dimension, and stable strategy order break "
               "remaining ties. Total widening is diagnostic, not the objective. "
               "Five deterministic greedy strategies are compared; this is not a proof "
               "of globally optimal class counts or error counts. The lower "
               "bounds are proven by explicit certificates: every pair of profiles in the corresponding "
               "JSON glyph list differs by more than N at some coordinate and therefore "
               "cannot share a class, independent of the heuristic or the opposite partition.", ""]
    for r in results:
        report += [f"## {r['name']} strategies", "",
                   "| Strategy | Left | Right | Cells | Padded cells | +1px pairs | +2px pairs | Added pixels | History valid | Dependent triplets |",
                   "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |"]
        for s in r["variants"]:
            report.append(f"| {s['strategy']} | {s['left']} | {s['right']} | {s['cells']} | "
                          f"{s['padded_cells']} | {s['extra_pixel_histogram'].get('1', 0)} | "
                          f"{s['extra_pixel_histogram'].get('2', 0)} | "
                          f"{s['added_pixels_total']} | {s['history_valid']} | {s['dependent_triplets']} |")
            if not s["history_valid"]:
                report += ["", f"Rejected {s['strategy']}: {s['history_failure']}", ""]
        report += ["", f"[Class definitions and lower-bound certificates]({r['name']}.json) · "
                   f"[Full decoded pair table]({r['name']}-pair-advances.csv)", ""]
    chosen = result["chosen"]
    left_bits = max(1, (chosen["left"] - 1).bit_length())
    right_bits = max(1, (chosen["right"] - 1).bit_length())
    before_cells = result["exact_classes"][0] * result["exact_classes"][1]
    report += ["## History invariant", "",
               "Without --widen-pairs, approximation starts directly from the original "
               "advances and is checked against the existing exhaustive four-glyph proof "
               "that two predecessors suffice. Remaining dependent triplets are allowed. "
               "Each distinct decoded candidate is checked; duplicate candidates share "
               "a proof result. Only valid candidates are eligible for selection. "
               "With --widen-pairs, the stronger exhaustive three-glyph triangle proof "
               "is required instead.", "",
               "Arbitrary positive per-pair errors do not preserve the triangle invariant. "
               "When the baseline already satisfies it, this aggregation does: "
               "a column merge replaces an advance by "
               "the maximum over the merged destination glyphs. The original triangle "
               "inequality then gives D'(A,C) <= D(A,B) + D'(B,C) <= D'(A,B) + D'(B,C). "
               "Row merging has the same proof after subtracting each source glyph's "
               "nominal advance. Both operations commute as minima of adjustment blocks. "
               "This does not imply preservation of the weaker two-predecessor invariant; "
               "that property is checked independently as described above.", "",
               "## BRAM implications", "",
               f"The selected current-font candidate needs {left_bits}-bit left IDs and "
               f"{right_bits}-bit right IDs. Actual class cells decrease from {before_cells:,} "
               f"to {chosen['cells']:,} ({100 * (1 - chosen['cells'] / before_cells):.1f}% fewer). "
               "The existing FPGA code still pads the table to 64 x 64 x 4 bits (one RAMB18). "
               "Tightly storing the actual class cells uses fewer bits, but needs a different ROM layout. "
               "There is no automatic physical BRAM reduction with the existing padded addressing.", "",
               "## Preview", "", "![Baseline and approximate classes](comparison.png)", "",
               "Production renderer and FPGA exporter defaults are unchanged.", "",
               "Regenerate from the font repository root:", "", "```sh",
               f"python -m src.approximate_kerning_classes --kerning-slack {args.kerning_slack}"
               + (" --widen-pairs" if args.widen_pairs else "")
               + (f" --fpga-rom {args.fpga_rom}" if args.fpga_rom else ""), "```", ""]
    (args.output_dir / "README.md").write_text("\n".join(report))
    for r in results:
        print(r["name"], json.dumps({"exact": r["exact_classes"], "selected": r["chosen"],
                                     "lower_bounds": r["lower_bounds"]}), flush=True)


if __name__ == "__main__":
    main()
