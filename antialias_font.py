"""Turn a binary glyph into four-color anti-aliased artwork.

Scale2x three times -> BOX downsample -> perceptual quantization to
sRGB grays (0, 59, 198, 255). See ANTIALIASING.md for a minimal example.
"""

from PIL import Image, ImageOps


FITTED_PALETTE = (0, 59, 198, 255)  # sRGB codes, indexed by 00, 01, 10, 11
PADDING = 2
SCALE_PASSES = 3


def scale2x(source):
    """Refine bitmap corners using the center and four orthogonal neighbors."""
    width, height = source.size
    pixels = source.load()
    result = Image.new("L", (width * 2, height * 2))
    output = result.load()
    for y in range(height):
        for x in range(width):
            center = pixels[x, y]
            above = pixels[x, max(0, y - 1)]
            below = pixels[x, min(height - 1, y + 1)]
            left = pixels[max(0, x - 1), y]
            right = pixels[min(width - 1, x + 1), y]
            corners = [center] * 4
            if above != below and left != right:
                corners = [left if left == above else center,
                           right if above == right else center,
                           left if left == below else center,
                           right if below == right else center]
            for (dx, dy), value in zip(((0, 0), (1, 0), (0, 1), (1, 1)), corners):
                output[x * 2 + dx, y * 2 + dy] = value
    return result


def antialias_coverage(mask):
    """Refine binary L/1 artwork and sample back to its original dimensions.

    Black is background; white is ink. BOX sampling matches the chosen preview.
    """
    if mask.mode not in ("1", "L"):
        raise ValueError("Expected a binary mask in mode '1' or 'L'.")
    mask = mask.convert("L")
    if any(mask.histogram()[1:255]):
        raise ValueError("Input mask must contain only 0 and 255.")
    refined = ImageOps.expand(mask, border=PADDING, fill=0)
    padded_size = refined.size
    for _ in range(SCALE_PASSES):
        refined = scale2x(refined)
    sampled = refined.resize(padded_size, Image.Resampling.BOX)
    return sampled.crop((PADDING, PADDING,
                         mask.width + PADDING, mask.height + PADDING))


def srgb_lightness(value):
    """CIELAB L* of a neutral 8-bit sRGB gray, relative to white."""
    encoded = value / 255
    luminance = encoded / 12.92 if encoded <= 0.04045 else ((encoded + 0.055) / 1.055) ** 2.4
    return (116 * luminance ** (1 / 3) - 16
            if luminance > 216 / 24389 else (24389 / 27) * luminance)


def lightness_srgb(lightness):
    """Convert neutral CIELAB L* to an 8-bit sRGB gray (for comparison palettes)."""
    luminance = ((lightness + 16) / 116) ** 3 if lightness > 8 else lightness * 27 / 24389
    encoded = 12.92 * luminance if luminance <= 0.0031308 else 1.055 * luminance ** (1 / 2.4) - 0.055
    return round(max(0, min(1, encoded)) * 255)


def quantize_2bit(coverage, palette=FITTED_PALETTE, perceptual=True):
    """Return mode P pixels with numeric indices 0..3, without dithering.

    The default palette and metric reproduce the selected fitted version.
    Alternate palettes/metrics are only for the historical preview comparisons.
    """
    if coverage.mode != "L":
        raise ValueError("Expected grayscale coverage in mode 'L'.")
    if len(palette) != 4 or any(not isinstance(v, int) or not 0 <= v <= 255 for v in palette):
        raise ValueError("Palette must contain four integer sRGB codes in 0..255.")
    metric = srgb_lightness if perceptual else float
    palette_values = [metric(shade) for shade in palette]
    # Resolve any exact tie to the lower palette index.
    lookup = [min(range(4), key=lambda index: abs(metric(value) - palette_values[index]))
              for value in range(256)]
    indices = coverage.point(lookup)
    result = Image.frombytes("P", coverage.size, indices.tobytes())
    result.putpalette([channel for shade in palette for channel in (shade,) * 3] + [0] * 756)
    return result


def antialias_glyph(mask):
    """Convert a two-color L/1 glyph to four-color mode P artwork."""
    return quantize_2bit(antialias_coverage(mask))


def compile_glyph(glyph):
    """Compile a transparent RGBA/LA glyph using its alpha occupancy.

    RGB color does not determine occupancy: an opaque black source pixel is
    still ink. Nonzero alpha becomes binary white, matching load_glyphs().
    Dimensions, origin, and blank glyph widths remain unchanged.
    """
    if glyph.mode not in ("RGBA", "LA"):
        raise ValueError("Expected a glyph with an alpha channel (RGBA or LA).")
    mask = glyph.getchannel("A").point([0] + [255] * 255)
    return antialias_glyph(mask)
