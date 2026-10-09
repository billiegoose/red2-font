#!/usr/bin/env python3
"""Make an anti-aliasing study from the existing pixel-font specimen.

Scale2x infers diagonal contours from neighboring pixels; three passes produce
an 8x contour approximation. Area sampling then gives partial pixel coverage.
This is a visual approximation, not a replacement for vector glyph outlines.
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from antialias_font import (
    antialias_coverage, lightness_srgb,
    quantize_2bit as quantize_coverage,
)


# Compatibility helpers for the existing preview workflow. Compiler code uses
# antialias_font.py directly, whose default is the selected fitted palette.
def simulate(source):
    return antialias_coverage(source.convert("L")).convert("RGB")


def quantize_2bit(source, perceptual=False):
    palette = tuple(lightness_srgb(100 * i / 3) for i in range(4)) if perceptual else (0, 85, 170, 255)
    return quantize_coverage(source.convert("L"), palette, perceptual)


def comparison(source, smoothed, labels=("ORIGINAL", "SIMULATED ANTI-ALIASING")):
    font_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
    font = ImageFont.truetype(str(font_path), 18) if font_path.exists() else ImageFont.load_default()
    title_font = ImageFont.truetype(str(font_path), 28) if font_path.exists() else font
    margin = 32
    detail_box = (0, 0, min(175, source.width), min(26, source.height))
    details = [image.crop(detail_box).resize((detail_box[2] * 5, detail_box[3] * 5),
                                           Image.Resampling.NEAREST)
               for image in (source, smoothed)]
    width = max(source.width, details[0].width) + 2 * margin
    height = 142 + 2 * (source.height + 64) + 2 * (details[0].height + 64) + 48
    canvas = Image.new("RGB", (width, height), "#11151a")
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 25), "RED2 / ANTI-ALIASING STUDY", font=title_font, fill="white")
    draw.text((margin, 70), "Same artwork and spacing. Gray pixels approximate smoother edge coverage.",
              font=font, fill="#aab4c2")
    y = 126
    panels = [(f"{labels[0]} / NATIVE SIZE", source),
              (f"{labels[1]} / NATIVE SIZE", smoothed),
              (f"{labels[0]} / 5x PIXEL DETAIL", details[0]),
              (f"{labels[1]} / 5x PIXEL DETAIL", details[1])]
    for label, panel in panels:
        draw.text((margin, y), label, font=font, fill="#aab4c2")
        y += 32
        draw.rectangle((margin - 8, y - 8, width - margin + 8, y + panel.height + 8), fill="black")
        canvas.paste(panel, (margin, y))
        y += panel.height + 32
    draw.text((margin, y + 4), "Preview only: contours inferred from pixels; vector outlines would give finer control.",
              font=font, fill="#aab4c2")
    return canvas.crop((0, 0, width, y + 54))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(__file__).with_name("example.png"))
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).with_name("previews"))
    args = parser.parse_args()
    with Image.open(args.input) as image:
        source = image.convert("RGB")
    smoothed = simulate(source)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    smoothed.save(args.output_dir / "antialiasing.png")
    comparison(source, smoothed).save(args.output_dir / "antialiasing-comparison.png")
    linear = quantize_2bit(smoothed)
    perceptual = quantize_2bit(smoothed, perceptual=True)
    linear.save(args.output_dir / "antialiasing-2bit.png", bits=2)
    perceptual.save(args.output_dir / "antialiasing-2bit-perceptual.png", bits=2)
    fitted = quantize_coverage(smoothed.convert("L"))
    fitted.save(args.output_dir / "antialiasing-2bit-fitted.png", bits=2)
    comparison(linear.convert("RGB"), perceptual.convert("RGB"),
               labels=("LINEAR RGB / 0, 85, 170, 255", "PERCEPTUAL L* / 0, 78, 162, 255")).save(
                   args.output_dir / "antialiasing-perceptual-comparison.png")
    print(args.output_dir / "antialiasing-comparison.png")


if __name__ == "__main__":
    main()
