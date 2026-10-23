# Measured kerning size comparison

Current artwork, 85 source glyphs, spacing 4. Every font is rebuilt from the same artwork with identical timestamps. Class versions recompute exact row/column equivalence and use FontTools GPOS class compaction at level 5.

| Variant | TTF bytes | WOFF2 bytes | GPOS bytes | GSUB bytes | Glyphs |
| --- | ---: | ---: | ---: | ---: | ---: |
| current-explicit | 33,112 | 5,980 | 12,174 | 3,044 | 176 |
| widened-explicit | 17,052 | 3,628 | 5,814 | 0 | 85 |
| current-classes | 32,228 | 6,404 | 11,292 | 3,044 | 176 |
| widened-classes | 17,444 | 3,408 | 6,206 | 0 | 85 |

## Pair-table classes

These are exact equivalence classes over the complete 85-by-85 table. An advance row includes glyph width; an adjustment row subtracts the left glyph's nominal advance. Identical glyph artwork is not required to share a kerning class.

| Table | Advance classes (left x right) | Adjustment classes (left x right) |
| --- | ---: | ---: |
| original | 65 x 51 | 55 x 51 |
| widened | 65 x 51 | 58 x 51 |

For a hardware table of absolute advances, 65 x 51 classes require 3,315 one-byte entries, plus 170 bytes for two one-byte glyph-to-class maps: 3,485 bytes total. Values fit in five bits; packing can reduce this further. This assumes the nominal glyph metrics and glyph bitmap storage are accounted for separately.

If instead storing the current reachable-state advance matrix, its 176 x 85 entries group into 132 x 51 exact advance classes (6,732 cells). The pair-only version groups into 65 x 51 (3,315 cells), 50.8% fewer cells. This comparison counts only advance values: the old scheme additionally needs state transitions and a way to identify the incoming state. It is not a measurement of an existing FPGA ROM binary.

The Python renderer already stores pair advances. Its pair-table storage does not shrink by removing history; the proposed table still has 85 x 85 entries before class grouping. The web-font savings come from removing contextual substitution rules, alternate glyph outlines, and state-dependent positioning. This repository has no FPGA ROM generator or hardware class-map encoding, so the font measurements are actual byte sizes; a hardware ROM size depends on that encoding.

Validation: each of four variants matches its intended Python layout in HarfBuzz for 9,852 sequences: all 7,225 pairs, all 627 dependent triplets, and 2,000 randomized strings. The widened table was also exhaustively proved pair-only safe before the size experiment. Exporter defaults are unchanged.

Regenerate: `python -m src.measure_kerning_sizes`.
