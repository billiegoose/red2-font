#!/usr/bin/env python3
"""Export Aseprite slices and render text using available glyphs."""

import argparse
import json
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


def render(text, source, output, aseprite):
    with tempfile.TemporaryDirectory(prefix="red2-font-") as directory:
        sheet = Path(directory) / "sheet.png"
        data = Path(directory) / "slices.json"
        # One untrimmed frame keeps slice bounds in source-canvas coordinates.
        subprocess.run(
            [
                aseprite,
                "-b",
                "--list-slices",
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
            glyphs[code] = pixels.crop((x, y, x + w, y + h))
        missing = sorted(set(text) - {chr(code) for code in glyphs})
        if missing:
            for character in missing:
                print(
                    f"Warning: skipping unrecognized character {character!r} "
                    f"(U+{ord(character):04X}).",
                    file=sys.stderr,
                )
        characters = [glyphs[ord(c)] for c in text if ord(c) in glyphs]
        # PNGs cannot have zero width; an empty result is transparent and 1px wide.
        width = max(1, sum(glyph.width for glyph in characters))
        height = max((glyph.height for glyph in characters), default=1)
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        cursor = 0
        for glyph in characters:
            image.alpha_composite(glyph, (cursor, 0))
            cursor += glyph.width
        image.save(output, format="PNG")
        return width, height


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "text",
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
    args = parser.parse_args()
    try:
        width, height = render(args.text, args.source, args.output, args.aseprite)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(f"Saved {args.output} ({width}x{height})")


if __name__ == "__main__":
    main()
