# Font

I felt like designing a font, so I did.

![Example render](./example.png)

Render the sliced glyphs with Python and Aseprite:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python render_font.py "0123456789ABCD"
```

This runs `aseprite` from PATH, exports the first frame and slice metadata to
temporary files, maps slice userdata such as `c=65` to character code points,
and writes a black-background `output.png` in the current directory. It exports only
the `Glyphs` layer, excluding the Background guide lines. Transparent margins are
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
one pixel at a time until the closest Euclidean gap falls below the target,
then chooses that placement or the previous one, whichever is closer to the target.
Ties retain the wider placement. Actual gaps can be slightly below the target.
The threshold between pixel centers is `spacing + 1`, preserving the convention
that 3 means three empty pixels on a straight horizontal row. Blank glyphs and
pairs with no edges within the vertical radius keep their nominal advance.

```sh
python render_font.py --pairs --output kerning_pairs.png
python render_font.py "AVATAR" --spacing 2
python -m unittest test_render_font.py
```

`--pairs` renders every ordered pair of available glyphs, including digits,
punctuation, and spaces, ordered by code point. For N glyphs, it produces N² pairs
in N rows. Pair samples have a separate visual gap and do not require a space
slice. Text input also supports
newlines. `--spacing` controls the target gap and vertical search radius and `--layer` selects the artwork
layer. Advances are computed from the cropped widths and cached for repeated pairs.

`--spacing` accepts nonnegative integers. At `--spacing 0`, edge pixel centers
may be one pixel apart: glyphs can touch, but their pixels do not overlap.

`--pairs` also writes `kerning_pairs.csv` beside the PNG, omitting pairs whose
kerning adjustment is zero. The PNG still shows every pair. Columns contain the
left/right characters, requested spacing, advance, adjustment relative to cropped
width plus spacing, and the actual minimum pixel-center distance minus 1.
Distances are measured across all occupied pixels and rounded to six decimals.

CSV distances are blank for pairs containing an empty glyph such as space.

The example render test uses the real Aseprite font and writes `example.png` in
the project directory. Run it on its own with:

```sh
python -m unittest test_render_font.ExampleRenderTest
```
