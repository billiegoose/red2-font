# VGA UART BRAM comparison

The widened pair-only kerning saves **zero BRAM blocks in the existing VGA UART
layout**. The renderer already stores a pair table, not a triplet table. It reads
that same table through two ports to enforce the previous two glyphs' bounds.

Source inspected:

- `../fpgatool/scripts/generate_uart_font.py`: independently group adjustment
  rows and columns, with adjustment = glyph width + 4 - advance.
- `../fpgatool/examples/hardware/vga_uart_pixels.py` and
  `vga_uart_aa_pixels.py`: pad to a 64 x 64 table, four bits per value, dual read.
- `../fpgatool/examples/hardware/red2_row_data.py` and `red2_aa_row_data.py`:
  decode the actual checked-in tables through their metadata class IDs.
- Both examples' existing `build/basys3/*/pipelinec/top/top.json` netlists:
  count RAMB36E1 and RAMB18E1 cells.

## Actual checked-in ROMs

These ROMs contain an older **73-glyph** snapshot. AA and non-AA have identical
kerning values. Their bitmap address metadata differs.

| Property | Existing | Widened pair-only |
| --- | ---: | ---: |
| Left adjustment classes | 59 | 62 |
| Right adjustment classes | 55 | 55 |
| Used four-bit entries | 3,245 | 3,410 |
| Used value bits | 12,980 | 13,640 |
| Allocated ROM | 4096 x 4 | 4096 x 4 |
| Logical ROM capacity | 16,384 bits | 16,384 bits |
| Physical kerning BRAM | 1 RAMB18E1 | 1 RAMB18E1 |
| Left/right metadata class IDs | 6 + 6 bits | 6 + 6 bits |

Reconstructed every supported advance from the stored class table and metadata.
Applying the same deterministic widening algorithm changes 144 pair advances;
all 989 dependent triplets in this snapshot disappear. Recomputed classes
reconstruct every widened advance exactly. Values still require four bits
(adjustments range from 0 to 11).

The existing non-AA netlist has 3 RAMB36E1 + 1 RAMB18E1 (15.75 KiB of physical
BRAM), while the AA netlist has 3 RAMB36E1 + 2 RAMB18E1 (18 KiB). Keeping the
current memory layout gives the same BRAM allocation after widening. A modified
renderer was not synthesized; the counts above are the existing netlists, and
the unchanged proposed allocation follows from keeping the same ROM instances.

## Current 85-glyph source

The current source artwork differs from the checked-in FPGA snapshot. Grouping
the current source with the FPGA's `width + 4 - advance` encoding gives
**56 x 51 classes before widening and 59 x 51 afterward**, preserving the
font's fixed-cell metadata. The candidate changes 120 pairs and eliminates the
627 triplets from the earlier font analysis. These counts also fit the existing
64 x 64, four-bit ROM and six-bit metadata class fields.

This calculation retains fixed-cell metadata when building the pair table.
The FPGA generator's current `assign_glyph_ids` returns a plain dictionary and
does not carry that metadata. The current artwork also has 1,091 bitmap columns,
above the column-export path's 1,024-column limit. Thus these current-source
counts describe the intended font behavior, not a successful unmodified run of
that older generator. Neither issue changes the zero-BRAM-saving conclusion
for the existing class-ROM layout.

## What the spacing change does save

The loader can use one pair lookup instead of two, removing the older glyph's
history registers and the second candidate-origin arithmetic/comparison path.
That saves logic/registers and frees one kerning read port; exact LUT/FF and
timing improvements require implementation and synthesis. It does not remove
the existing RAMB18E1 instance automatically.

Saving a BRAM block requires another storage-layout change, such as combining
font and kerning data with tighter packing and rescheduling accesses. The
earlier web-font/state-table savings are not BRAM savings in this architecture.
