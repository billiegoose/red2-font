# Two-color glyph → four-color anti-aliasing

The example implementation is [antialias_font.py](./antialias_font.py).
It refines the binary glyph with three Scale2x passes, samples it back to its
original size with Pillow BOX, then selects the nearest perceptual shade from
**0, 59, 198, 255**. No dithering.

```python
from PIL import Image
from antialias_font import antialias_glyph

# Two-color input: black background (0), white ink (255).
with Image.open("glyph.png") as image:
    glyph = image.convert("L")

antialiased = antialias_glyph(glyph)
antialiased.save("glyph-aa.png", bits=2)
```

The result has the same dimensions and four palette indices (0–3), mapped to
those four gray values. This reproduces the selected fitted preview.

For a transparent glyph returned by `load_glyphs()`, use its alpha occupancy:

```python
from antialias_font import compile_glyph

antialiased = compile_glyph(glyph_rgba)
```
