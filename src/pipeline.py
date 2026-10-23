"""Prepare one validated font and share it across all output formats."""

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile

from .font import AutoKerning, load_glyphs
from .defaults import SPACING, KERNING_SLACK, WIDEN_PAIRS, EXPORT_FORMATS
from .kerning import (STRATEGIES, TableKerning, compress_classes, prepare_baseline,
                      select_candidate, verify_required_history)


@dataclass
class PreparedFont:
    glyphs: dict
    kerning: TableKerning
    classes: object
    settings: dict
    statistics: dict


def prepare_font(glyphs, spacing=SPACING, kerning_slack=KERNING_SLACK, widen_pairs=WIDEN_PAIRS):
    """Apply shared options once, selecting by padded storage and worst errors.

    Candidate generation is deterministic. Check candidates in preference order
    and return the first one that passes the required history proof. Lower-ranked
    candidates cannot win, so their expensive proofs are unnecessary.
    """
    if spacing < 0 or kerning_slack < 0:
        raise ValueError("Spacing and kerning slack must be nonnegative")
    if not glyphs:
        raise ValueError("Cannot generate a font with no glyphs")
    if not any(code != 0 for code in glyphs):
        raise ValueError("Cannot generate a font with no mapped glyphs")
    original = AutoKerning(glyphs, spacing)
    codes = sorted(glyphs)
    raw = {a: {b: original.advance(a, b) for b in codes} for a in codes}
    baseline = prepare_baseline(raw, {a: original.nominal_advance(a) for a in codes}, widen_pairs)
    # Hardware derives advances from width + spacing, including fixed-cell
    # glyphs (whose spacing is canceled by their stored adjustment).
    nominal = {a: glyphs[a].width + spacing for a in codes}
    adjustments = {a: {b: nominal[a] - baseline[a][b] for b in codes} for a in codes}
    protected = {(a, b) for a in codes for b in codes
                 if a in original.fixed_advances or b in original.fixed_advances}
    candidates, variants = [], []
    for strategy in STRATEGIES if kerning_slack else STRATEGIES[:1]:
        classes = compress_classes(adjustments, kerning_slack, strategy, protected)
        expanded = classes.expand()
        advances = {a: {b: nominal[a] - expanded[a][b] for b in codes} for a in codes}
        errors = [advances[a][b] - baseline[a][b] for a in codes for b in codes]
        if not all(0 <= extra <= kerning_slack for extra in errors):
            raise ValueError("Kerning slack exceeded its per-pair bound")
        if any(advances[a][b] != baseline[a][b] for a, b in protected):
            raise ValueError("Kerning processing changed a fixed-cell pair")
        variants.append({"strategy": strategy, "left": len(classes.left), "right": len(classes.right),
                         "cells": len(classes.left) * len(classes.right),
                         "max_added_pixels": max(errors),
                         "extra_pixel_histogram": {str(k): v for k, v in sorted(Counter(errors).items())},
                         "changed_pairs": sum(e > 0 for e in errors),
                         # Eligible for ranking; the proof is run before return.
                         "history_valid": True})
        candidates.append((classes, advances))
    history = 1 if widen_pairs else 2
    failures = []
    while any(v["history_valid"] for v in variants):
        chosen = select_candidate(variants)
        classes, advances = candidates[chosen]
        try:
            checked = verify_required_history(advances, history, spacing=spacing)
        except ValueError as error:
            variants[chosen].update(history_valid=False, history_failure=str(error))
            failures.append(str(error))
            continue
        kerning = TableKerning(original, advances)
        kerning.verified_history = history
        return PreparedFont(glyphs, kerning, classes,
                            {"spacing": spacing, "kerning_slack": kerning_slack,
                             "widen_pairs": widen_pairs},
                            {**variants[chosen], "history_glyphs": history,
                             "history_combinations_checked": checked})
    raise ValueError("No candidate passes the required kerning history invariant: " + "; ".join(failures))


def generate(source, directory, formats=EXPORT_FORMATS, *, spacing=SPACING,
             kerning_slack=KERNING_SLACK, widen_pairs=WIDEN_PAIRS, aseprite="aseprite", layer="Glyphs"):
    """Load once, prepare once, and export only the requested formats."""
    supported = {"woff", "woff2", "ttf", "bram"}
    formats = tuple(dict.fromkeys(formats))
    if not formats or set(formats) - supported:
        raise ValueError("Export formats must be selected from woff, woff2, ttf, bram")
    glyphs = load_glyphs(Path(source), aseprite, layer)
    prepared = prepare_font(glyphs, spacing, kerning_slack, widen_pairs)
    directory = Path(directory)
    # Finish every selected format before replacing any published output. A
    # BRAM capacity error must not leave new web fonts beside an old manifest.
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".red2-build-", dir=directory.parent) as temporary:
        staging = Path(temporary)
        files = _export(prepared, source, staging, formats, layer)
        directory.mkdir(parents=True, exist_ok=True)
        published = []
        for path in files:
            target = directory / path.relative_to(staging)
            target.parent.mkdir(parents=True, exist_ok=True)
            path.replace(target)
            published.append(target)
    return prepared, published


def _export(prepared, source, directory, formats, layer):
    glyphs = prepared.glyphs
    spacing = prepared.settings["spacing"]
    files = []
    web_formats = [name for name in formats if name != "bram"]
    if web_formats:
        from .web_font import build_font, write_web_fonts
        font, _ = build_font(glyphs, spacing, kerning=prepared.kerning)
        files.extend(write_web_fonts(font, glyphs, directory, web_formats))
    if "bram" in formats:
        from .bram import export_bram
        files.extend(export_bram(prepared, directory / "bram"))
    manifest = {"source": str(Path(source).resolve()), "layer": layer, "formats": list(formats),
                "settings": prepared.settings, "glyph_count": len(glyphs),
                "kerning": prepared.statistics,
                "classes": {"left": prepared.classes.left, "right": prepared.classes.right,
                            "values": prepared.classes.values},
                "files": [str(path.relative_to(directory)) for path in files]}
    path = directory / "font.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    files.append(path)
    return files
