#!/usr/bin/env python3
"""Render RED2 bitmap text. Run --help for preview options."""

# Preserve the glyph-loader API used by external FPGA tooling.
from src.font import (AutoKerning, GlyphCollection, compose, compose_pairs, contours,
    export_kerning_csv, horizontal_ink_metrics, layout_line, load_glyphs, main,
    parse_slice_codepoint, parse_slice_userdata, render, validate_slices)

if __name__ == "__main__":
    main()
