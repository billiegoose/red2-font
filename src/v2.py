"""Font v2 compiler and little-endian flash image ABI.

The font compiler and production serializer live here. Hardware consumers keep
only their wire-format constants and fixture adapters. No FPGA runtime or
fpgatool checkout is needed to generate font binaries.
"""
import struct
from functools import lru_cache

MAGIC = 0x32564652  # b"RFV2"
VERSION = 2
MAX_PAGES = 8
HEADER_WORDS = 40
HEADER_BYTES = HEADER_WORDS * 4
PAYLOAD_WORDS = 1024
PAGE_WORD_BASE = 24
CHECKSUM_WORD = 16


def pack_streams(streams):
    unique = sorted(set(s for s in streams if s))
    parts = [s for s in unique if not any(s != t and s in t for t in unique)]
    @lru_cache(maxsize=None)
    def overlap(a, b):
        return next((n for n in range(min(len(a), len(b)), 0, -1)
                     if a[-n:] == b[:n]), 0)
    while len(parts) > 1:
        n, ai, bi = max((overlap(a, b), -i, -j)
                       for i, a in enumerate(parts) for j, b in enumerate(parts) if i != j)
        i, j = -ai, -bi
        joined = parts[i] + parts[j][n:]
        parts = [s for k, s in enumerate(parts) if k not in (i, j)] + [joined]
    return parts[0] if parts else b''


def compile_font(source):
    raw = bytes(b for w in source.FONT_WORDS[:source.FONT_INDEX_BASE]
                for b in (w & 255, w >> 8))
    patterns = [int.from_bytes(raw[3*i:3*i+3], 'little')
                for i in range(source.FONT_DICTIONARY_COUNT)]
    if any(p >= (1 << 21) for p in patterns):
        raise ValueError('Bit patterns must fit the lower 21 bits of each word')
    glyphs = []
    for code, meta in enumerate(source.METADATA):
        if meta & 256:
            base, height = (meta >> 20) & 2047, (meta >> 9) & 63
            ids = tuple(w & 511 for w in source.FONT_WORDS[base:base+height])
            cp = code if code < 128 else source.UNICODE_CODEPOINTS[code-128]
            glyphs.append((code, cp, meta, ids))
    return compile_rows(glyphs, patterns, source.KERNING,
                        source.KERNING_LEFT_COUNT, source.KERNING_RIGHT_COUNT, source.SPACING)


def partition_patterns(glyphs):
    """Find the minimum bank count supported by the one-bit glyph bank field.

    Keep existing single-glyph splits stable. For larger splits, try cheap,
    deterministic greedy searches before an exact two-bank feasibility search.
    A move groups all glyphs using a pattern, so it actually frees primary-bank
    space even when the pattern is shared. Shared rows may exist in both banks.
    """
    all_ids = sorted({i for g in glyphs for i in g[3]}) or [0]
    if len(all_ids) <= 256:
        return all_ids, [], set()
    if len(all_ids) > 512:
        raise ValueError(f'Font has {len(all_ids)} unique pixel-row patterns and needs '
                         f'at least {(len(all_ids)+255)//256} banks; font v2 supports '
                         'only two banks of 256 patterns each')
    singles = []
    for glyph in glyphs:
        first = sorted({i for g in glyphs if g != glyph for i in g[3]})
        second = sorted(set(glyph[3]))
        if len(first) <= 256 and len(second) <= 256:
            singles.append((len(second), glyph[0], first, second))
    if singles:
        _, code, first, second = min(singles)
        return first, second, {code}

    positions = {p: i for i, p in enumerate(all_ids)}
    masks = [sum(1 << positions[p] for p in set(g[3])) for g in glyphs]
    groups = sorted({sum(1 << i for i, mask in enumerate(masks) if mask & (1 << p))
                     for p in range(len(all_ids))})
    partitions = {}
    # Union eight glyph masks per lookup rather than scanning every glyph
    # for each candidate partition. Tables are local to this font build.
    chunks = []
    for start in range(0, len(masks), 8):
        chunk = masks[start:start+8]
        unions = [0] * (1 << len(chunk))
        for subset in range(1, len(unions)):
            bit = subset & -subset
            unions[subset] = unions[subset ^ bit] | chunk[bit.bit_length()-1]
        chunks.append((start, len(unions)-1, unions))

    def partition(selected):
        if selected not in partitions:
            first = second = 0
            for start, full, unions in chunks:
                subset = (selected >> start) & full
                second |= unions[subset]
                first |= unions[full ^ subset]
            partitions[selected] = first, second
        return partitions[selected]

    best = None
    for selected in [0] + groups:
        while True:
            first, second = partition(selected)
            if second.bit_count() > 256:
                break
            if first.bit_count() <= 256:
                candidate = second.bit_count(), selected
                if best is None or candidate < best:
                    best = candidate
                break
            options = []
            for group in groups:
                moved = selected | group
                a, b = partition(moved)
                if a.bit_count() >= first.bit_count() or b.bit_count() > 256:
                    continue
                freed = min(len(all_ids)-a.bit_count(), len(all_ids)-256)
                options.append((b.bit_count()/freed, b.bit_count(), moved))
            if not options:
                break
            selected = min(options)[2]
    if best is not None:
        selected = best[1]
        first, second = partition(selected)
    else:
        # Greedy failure is not proof that another bank is required. Remove
        # subset glyphs, then exhaust the remaining two-bank assignments.
        rows = sorted(set(masks), key=lambda m: (-m.bit_count(), m))
        rows = [m for m in rows if not any(m != n and m & n == m for n in rows)]
        tails = [0] * (len(rows)+1)
        for i in range(len(rows)-1, -1, -1):
            tails[i] = tails[i+1] | rows[i]

        @lru_cache(maxsize=None)
        def solve(index, a, b):
            if a > b:
                return solve(index, b, a)
            if (a.bit_count()+b.bit_count()
                    + (tails[index] & ~(a | b)).bit_count() > 512):
                return None
            if index == len(rows):
                return a, b
            row = rows[index]
            if row & a == row or row & b == row:
                return solve(index+1, a, b)
            choices = {(a | row, b), (a, b | row)}
            for aa, bb in sorted(choices, key=lambda pair:
                                 (pair[0].bit_count()+pair[1].bit_count(), pair)):
                if max(aa.bit_count(), bb.bit_count()) <= 256:
                    result = solve(index+1, aa, bb)
                    if result is not None:
                        return result
            return None

        result = solve(0, 0, 0)
        if result is None:
            raise ValueError(f'Font has {len(all_ids)} unique pixel-row patterns, but '
                             'requires more than two 256-pattern banks: each glyph '
                             'must use a single bank. Font v2 has a one-bit bank field')
        first, second = result
        if second.bit_count() > first.bit_count():
            first, second = second, first
        selected = sum(1 << i for i, mask in enumerate(masks) if mask & first != mask)
    return ([p for p in all_ids if first & (1 << positions[p])],
            [p for p in all_ids if second & (1 << positions[p])],
            {g[0] for i, g in enumerate(glyphs) if selected & (1 << i)})


def compile_rows(glyphs, patterns, kerning, left_count, right_count, spacing):
    """Compile (source ID, codepoint, legacy field bits, row IDs) records."""
    if not glyphs or glyphs[0][0] != 0:
        raise ValueError('Font v2 requires glyph 0 (.notdef)')
    if not 0 <= spacing <= 31:
        raise ValueError('Font v2 spacing must fit five unsigned bits')
    if max(left_count, right_count) > 32:
        raise ValueError('Font v2 requires at most 32 left and right kerning classes')
    if any(not 0 <= k <= 15 for k in kerning):
        raise ValueError('Font v2 kerning must fit four unsigned bits')
    glyphs.sort(key=lambda g: -1 if g[0] == 0 else g[1])
    if len(glyphs) > 256:
        raise ValueError('This profile uses eight-bit global glyph IDs')
    first, second, secondary = partition_patterns(glyphs)
    banks = [first, second]
    remap = [{old: new for new, old in enumerate(bank)} for bank in banks]
    overlay_ascii = any(code != 0 and cp >> 8 == 0 for code, cp, _, _ in glyphs)
    physical = [patterns[i] for i in first]
    # Bank 1 begins at ID 256. The overlaid map also reserves exactly 256
    # words even when a small font has fewer unique patterns.
    if second or overlay_ascii:
        physical.extend([0]*(256-len(first)))
    physical.extend(patterns[i] for i in second)
    unicode_maps = []
    for prefix in sorted({cp >> 8 for code, cp, _, _ in glyphs if code != 0}):
        entries = [(cp & 255, gid) for gid, (code, cp, _, _) in enumerate(glyphs)
                   if code != 0 and cp >> 8 == prefix]
        direct = bytearray([255]*256)
        for local, (low, gid) in enumerate(entries):
            direct[low] = local
        unicode_maps.append((prefix, direct, entries[0][1], len(entries)))
    ascii_map = next((data for prefix, data, _, _ in unicode_maps if prefix == 0), None)
    streams, crops = {}, []
    for code, cp, meta, ids in glyphs:
        mirror = len(ids) > 1 and ids == ids[::-1]
        stream = ids[:(len(ids)+1)//2] if mirror else ids
        streams[code] = bytes(remap[int(code in secondary)][i] for i in stream)
        crops.append(((meta >> 15) & 31, len(ids), mirror))
    classes = sorted(set(crops))
    if len(classes) > 32:
        raise ValueError('Font needs crop-class merging before it fits this profile')
    pool = pack_streams(list(streams.values()))
    if len(pool) > 2048:
        raise ValueError('Row-byte pool exceeds eleven-bit offsets')
    image = bytearray()
    def append(data):
        base = len(image)//4
        image.extend(data)
        image.extend(bytes((-len(image)) % 4))
        return base
    metadata = []
    for glyph, crop in zip(glyphs, crops):
        code, cp, meta, ids = glyph
        base = pool.find(streams[code])
        left, right = (meta >> 31) & 63, (meta >> 37) & 63
        if max(left, right) >= 32 or meta & 224:
            raise ValueError('Five-bit classes and zero backtrack required')
        metadata.append(base | (classes.index(crop) << 11) | (left << 16)
                        | (right << 21) | ((meta & 31) << 26) | (int(code in secondary) << 31))
    metadata_base = append(b''.join(w.to_bytes(4, 'little') for w in metadata))
    crop_base = append(b''.join((t | (h << 5) | (int(m) << 11)).to_bytes(2, 'little')
                               for t, h, m in classes))
    rows_base = append(pool)
    kern = [kerning[l*right_count+r]
            if l < left_count and r < right_count else 0
            for l in range(32) for r in range(32)]
    kerning_base = append(bytes(kern[i] | (kern[i+1] << 4) for i in range(0, 1024, 2)))
    # For this image the preceding tables end at word 384. Kerning first
    # therefore aligns its 128 words and starts patterns at word 512, removing
    # address adders without padding. Bases remain explicit format data.
    pattern_base = append(b''.join(
        (p | ((ascii_map[i] if ascii_map is not None and i < 256 else 0) << 21))
        .to_bytes(4, 'little') for i, p in enumerate(physical)))
    pages = []
    for prefix, direct, first_gid, count in unicode_maps:
        # Encoding 1: one map byte in bits [28:21] of each pattern word.
        # Encoding 0: four ordinary map bytes per word.
        overlay = int(prefix == 0)
        map_base = pattern_base if overlay else append(direct)
        pages.append((prefix, map_base, first_gid, count, overlay))
    if len(pages) > MAX_PAGES:
        raise ValueError(f'Font v2 supports at most {MAX_PAGES} Unicode pages')
    used = len(image)
    if used > 4096:
        raise ValueError(f'Font requires {used} bytes; one block holds 4096')
    image.extend(bytes(4096-used))
    return dict(FONT_FORMAT_VERSION=2, METADATA_BASE=metadata_base, CROP_BASE=crop_base, ROWS_BASE=rows_base,
                PATTERNS_BASE=pattern_base, KERNING_BASE=kerning_base, PAGES=tuple(pages),
                GLYPH_COUNT=len(glyphs), CROP_COUNT=len(classes), ROW_BYTE_COUNT=len(pool),
                PATTERN_COUNT=len(physical), USED_BYTES=used, SPACING=spacing,
                SOURCE_CODES=tuple(g[0] for g in glyphs),
                FONT_WORDS=tuple(int.from_bytes(image[i:i+4], 'little') for i in range(0,4096,4)))


def image_bytes(data):
    """160-byte header followed by all 1024 BRAM32 words, little endian.

    The sum of every uint32 word, including the checksum, is zero modulo 2^32.
    Header words 17..23 and unused page slots are reserved and must be zero.
    """
    header = [MAGIC, VERSION, HEADER_BYTES, PAYLOAD_WORDS, data['USED_BYTES'],
              data['METADATA_BASE'], data['CROP_BASE'], data['ROWS_BASE'],
              data['KERNING_BASE'], data['PATTERNS_BASE'], data['GLYPH_COUNT'],
              data['CROP_COUNT'], data['ROW_BYTE_COUNT'], data['PATTERN_COUNT'],
              data['SPACING'], len(data['PAGES'])]
    header.extend([0] * (HEADER_WORDS-len(header)))
    for i, (prefix, base, first, count, overlay) in enumerate(data['PAGES']):
        header[PAGE_WORD_BASE+2*i] = prefix | (base << 16) | (overlay << 26)
        header[PAGE_WORD_BASE+2*i+1] = first | (count << 8)
    words = header + list(data['FONT_WORDS'])
    words[CHECKSUM_WORD] = (-sum(words)) & 0xffffffff
    return struct.pack(f'<{len(words)}I', *words)


def validate_image(data):
    if len(data) != 4256:
        raise ValueError('font v2 image requires a 4256-byte font v2 image')
    words = struct.unpack('<1064I', data)
    if words[:4] != (0x32564652, 2, 160, 1024) or sum(words) & 0xffffffff:
        raise ValueError('invalid font v2 header or checksum')
    used, meta, crop, rows, kern, patterns = words[4:10]
    glyphs, crops, row_bytes, pattern_count, spacing, pages = words[10:16]
    limit = used // 4
    valid = (512 <= used <= 4096 and not used & 3
        and 1 <= glyphs <= 256 and meta + glyphs <= limit
        and 1 <= crops <= 32 and crop + (crops + 1)//2 <= limit
        and row_bytes <= 2048 and rows < limit and rows + (row_bytes + 3)//4 <= limit
        and kern + 128 <= limit and 1 <= pattern_count <= 512
        and patterns + pattern_count <= limit and spacing <= 31 and 1 <= pages <= 8
        and not any(words[17:24]))
    prefixes = set()
    for i in range(8):
        a, b = words[24 + 2*i:26 + 2*i]
        if i >= pages:
            valid = valid and not (a or b)
            continue
        prefix, base, overlay = a & 0xffff, (a >> 16) & 1023, (a >> 26) & 1
        first, count = b & 255, (b >> 8) & 511
        valid = (valid and not a >> 27 and prefix <= 0x10ff and prefix not in prefixes
            and base + (256 if overlay else 64) <= limit and not b >> 17
            and first > 0 and 1 <= count <= 255 and first + count <= glyphs)
        prefixes.add(prefix)
    if not valid:
        raise ValueError('invalid font v2 table bounds or Unicode directory')



def compile_prepared(prepared):
    """Use the shared glyphs and validated kerning without a legacy ROM export."""
    glyphs, classes = prepared.glyphs, prepared.classes
    left = {code: i for i, group in enumerate(classes.left) for code in group}
    right = {code: i for i, group in enumerate(classes.right) for code in group}
    dictionary, ids, records = [0], {0: 0}, []
    for code in sorted(glyphs):
        glyph = glyphs[code]
        if glyph.width > 21 or glyph.height > 32:
            raise ValueError('Font v2 glyphs must be at most 21 x 32 pixels')
        if not 0 <= code <= 0x10ffff or 0xd800 <= code <= 0xdfff:
            raise ValueError('Font v2 codepoints must be Unicode scalars')
        alpha = glyph.getchannel('A')
        bounds = alpha.getbbox()
        top, height = (bounds[1], bounds[3]-bounds[1]) if bounds else (0, 0)
        rows = []
        for y in range(top, top+height):
            bits = sum(bool(alpha.getpixel((x, y))) << x for x in range(glyph.width))
            if bits not in ids:
                ids[bits] = len(dictionary)
                dictionary.append(bits)
            rows.append(ids[bits])
        meta = glyph.width | (top << 15) | (left[code] << 31) | (right[code] << 37)
        records.append((code, code, meta, tuple(rows)))
    return compile_rows(records, dictionary, [v for row in classes.values for v in row],
                        len(classes.left), len(classes.right), prepared.settings['spacing'])


def export_v2(prepared, directory):
    from pathlib import Path
    directory = Path(directory)
    data = compile_prepared(prepared)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'RED2-Regular-v2.bin'
    binary = image_bytes(data)
    validate_image(binary)
    path.write_bytes(binary)
    return [path]
