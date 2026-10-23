"""Command-line interface for generating RED2 font assets."""

import argparse
from pathlib import Path
import subprocess

from .paths import DEFAULT_SOURCE
from .defaults import SPACING, KERNING_SLACK, WIDEN_PAIRS, EXPORT_FORMATS
from .pipeline import generate


def export_formats(value):
    formats = tuple(dict.fromkeys(part.strip().lower() for part in value.split(",")))
    if not formats or any(name not in {"woff", "woff2", "ttf", "bram"} for name in formats):
        raise argparse.ArgumentTypeError("choose comma-separated formats: woff, woff2, ttf, bram")
    return formats


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--aseprite", default="aseprite")
    parser.add_argument("--layer", default="Glyphs")
    parser.add_argument("--spacing", type=int, default=SPACING,
                        help=f"Optical spacing in source pixels (default: {SPACING})")
    parser.add_argument("--kerning-slack", type=int, default=KERNING_SLACK, metavar="N",
                        help=f"Allow up to N extra pixels per pair to reduce classes (default: {KERNING_SLACK})")
    parser.add_argument("--widen-pairs", action="store_true", default=WIDEN_PAIRS,
                        help="Require adjacent-pair placement (default: off)")
    parser.add_argument("--export", dest="formats", type=export_formats, default=EXPORT_FORMATS,
                        metavar="FORMATS", help=f"Comma-separated woff,woff2,ttf,bram (default: {','.join(EXPORT_FORMATS)})")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.spacing < 0 or args.kerning_slack < 0:
        parser.error("--spacing and --kerning-slack must be nonnegative")
    try:
        print("Preparing glyphs and validating kerning...", flush=True)
        prepared, files = generate(args.source, args.output_dir, args.formats,
                                  spacing=args.spacing, kerning_slack=args.kerning_slack,
                                  widen_pairs=args.widen_pairs, aseprite=args.aseprite, layer=args.layer)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Error: {error}\n")
    stats = prepared.statistics
    print(f"Generated {len(prepared.glyphs)} glyphs; {stats['left']} x {stats['right']} kerning classes; "
          f"{stats['history_glyphs']}-glyph history verified.")
    for path in files:
        print(f"Saved {path} ({path.stat().st_size:,} bytes)")
