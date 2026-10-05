# Font

I felt like designing a font, so I did.

Render the sliced glyphs (currently `0-9` and `A-Z`) with Python and Aseprite:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python render_font.py "0123456789ABCD"
```

This runs `aseprite` from PATH, exports the first frame and slice metadata to
temporary files, maps slice userdata such as `c=65` to character code points,
and writes a black-background `output.png` in the current directory. It exports only
the `Text` layer, excluding the Background guide lines. Transparent margins are
cropped horizontally; vertical slice metrics and blank space widths are preserved.
Use `--aseprite /path/to/aseprite`, `--source font.aseprite`, or
`--output preview.png` to override the defaults.

Every slice must have explicit `c=<decimal code point>` userdata, and all `c`
values must be unique. Rendering reports all validation errors before composing
the output.

Characters without a matching slice are skipped without adding spacing, with a
warning printed to the terminal for each distinct unrecognized character. Empty
text or text with no recognized characters produces a black 1×1 PNG.

Automatic kerning starts with the cropped glyph boxes `spacing` pixels apart
(default 4). It compares the facing edge pixels in each row against rows up to
`spacing` pixels above and below in the other glyph. The second glyph moves left
one pixel at a time until another step would violate the Euclidean clearance.
The threshold between pixel centers is `spacing + 1`, preserving the convention
that 3 means three empty pixels on a straight horizontal row. Blank glyphs and
pairs with no edges within the vertical radius keep their nominal advance.

```sh
python render_font.py --pairs --output kerning_pairs.png
python render_font.py "AVATAR" --spacing 2
python -m unittest test_render_font.py
```

`--pairs` renders all 676 uppercase pairs in 26 rows (`AA AB … AZ`,
`BA BB … BZ`, etc.), separated by the font's space glyph. Text input also supports
newlines. `--spacing` controls the minimum gap and vertical search radius and `--layer` selects the artwork
layer. Advances are computed from the cropped widths and cached for repeated pairs.

`--spacing` accepts nonnegative integers. At `--spacing 0`, edge pixel centers
may be one pixel apart: glyphs can touch, but their pixels do not overlap.

`--pairs` also writes `kerning_pairs.csv` beside the PNG. Columns contain the
left/right characters, requested spacing, advance, adjustment relative to cropped
width plus spacing, and the actual minimum pixel-center distance minus 1.
Distances are measured across all occupied pixels and rounded to six decimals.
