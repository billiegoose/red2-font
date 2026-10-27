"""Font-v2 reference exports, validation and frame checks for generate_font.py."""
import codecs
import importlib.util
from pathlib import Path
import struct

from .v2 import compile_font, image_bytes, validate_image, HEADER_WORDS


def add_commands(parser):
    commands = parser.add_subparsers(dest='action')
    validate = commands.add_parser('validate-v2', help='validate an existing font-v2 binary')
    validate.add_argument('binary', type=Path)
    reference = commands.add_parser('v2-reference', help='convert legacy BRAM constants into a v2 reference fixture')
    reference.add_argument('reference', type=Path)
    reference.add_argument('--output', type=Path, required=True, help='Python constants fixture')
    reference.add_argument('--binary', type=Path, help='also write the header + BRAM32 binary')
    verify = commands.add_parser('verify-v2-reload', help='check captures contain only complete old/new font frames')
    verify.add_argument('capture_directory', type=Path)
    verify.add_argument('text', type=Path)
    verify.add_argument('--font', type=Path, required=True, help='original font-v2 binary used by the capture')
    verify.add_argument('--new-spacing', type=int, default=9)


class FontImage:
    """Independent binary decoder used to validate pixels, maps and row bounds."""
    def __init__(self, binary):
        validate_image(binary)
        words = struct.unpack('<1064I', binary)
        self.header, payload = words[:HEADER_WORDS], words[HEADER_WORDS:]
        meta, crop_base, rows, kern, patterns = words[5:10]
        self.spacing = words[14]
        self.metadata = payload[meta:meta+words[10]]
        self.rows = []
        for descriptor in self.metadata:
            crop_id = (descriptor >> 11) & 31
            if crop_id >= words[11] or ((descriptor >> 26) & 31) > 21:
                raise ValueError('font v2 glyph has an invalid crop class or width')
            crop = (payload[crop_base+crop_id//2] >> (16*(crop_id%2))) & 65535
            top, height, mirror = crop & 31, (crop >> 5) & 63, bool(crop & 2048)
            stored = (height+1)//2 if mirror else height
            base, bank = descriptor & 2047, descriptor >> 31
            if crop >> 12 or top+height > 32 or base+stored > words[12]:
                raise ValueError('font v2 glyph has invalid vertical or row-pool bounds')
            decoded = [0]*32
            for y in range(height):
                offset = base + (min(y, height-1-y) if mirror else y)
                row_id = (payload[rows+offset//4] >> (8*(offset%4))) & 255
                index = bank*256+row_id
                if index >= words[13]:
                    raise ValueError('font v2 row references a pattern outside its dictionary')
                decoded[top+y] = payload[patterns+index] & ((1 << 21)-1)
            self.rows.append(tuple(decoded))
        self.kerning = tuple((payload[kern+i//8] >> (4*(i%8))) & 15 for i in range(1024))
        self.mapping = {}
        for page in range(words[15]):
            a, b = words[24+2*page:26+2*page]
            prefix, base, overlay = a & 65535, (a >> 16) & 1023, (a >> 26) & 1
            first, count = b & 255, (b >> 8) & 511
            for low in range(256):
                local = ((payload[base+low] >> 21) & 255 if overlay else
                         (payload[base+low//4] >> (8*(low%4))) & 255)
                if local < count:
                    self.mapping[prefix << 8 | low] = first+local

    def advance(self, left, right, spacing):
        a, b = self.metadata[left], self.metadata[right]
        return ((a >> 26) & 31) + spacing - self.kerning[((a >> 16) & 31)*32+((b >> 21) & 31)]


def expected_pixels(font, data, *, spacing=None, width=1920, height=1080):
    """Render UTF-8/editing with full layout history, independent of FPGA logic."""
    spacing = font.spacing if spacing is None else spacing
    if not 0 <= spacing <= 31:
        raise ValueError('font v2 spacing must be between 0 and 31')
    decoder = codecs.getincrementaldecoder('utf-8')('ignore')
    codes = []
    for byte in data:
        if byte in (8, 127):
            decoder.reset()
            if codes:
                codes.pop()
        else:
            for character in decoder.decode(bytes((byte,))):
                if len(codes) < 2048:
                    codes.append(ord(character))
    pixels = bytearray(width*height)
    margin_x, margin_y = 5, 2
    right, bottom = width-margin_x, height-margin_y
    y, history = margin_y, []
    for code in codes:
        if code == 10:
            y += 36
            history = []
            continue
        if code < 32:
            continue
        gid = font.mapping.get(code, 0)
        glyph_width = (font.metadata[gid] >> 26) & 31
        x = max((left_x+font.advance(left, gid, spacing) for left, left_x in history),
                default=margin_x)
        if x+glyph_width > right:
            y += 36
            x, history = margin_x, []
        if y >= bottom:
            break
        for dy, row in enumerate(font.rows[gid][:max(0, min(32, bottom-y))]):
            for dx in range(glyph_width):
                if margin_x <= x+dx < right and row & (1 << dx):
                    pixels[(y+dy)*width+x+dx] = 255
        history.append((gid, x))
    return bytes(pixels)


def verify_reload(font, capture_directory, text, new_spacing=9):
    from PIL import Image
    old = expected_pixels(font, text)
    new = expected_pixels(font, text, spacing=new_spacing)
    if old == new:
        raise ValueError('reload reference images are identical; use text/spacing that changes placement')
    labels = []
    for path in sorted(Path(capture_directory).glob('frame-[0-9][0-9][0-9][0-9].png')):
        with Image.open(path) as frame:
            if frame.size != (1920, 1080):
                raise ValueError(f'Wrong dimensions: {path}')
            pixels = frame.convert('L').tobytes()
        label = 'old' if pixels == old else 'new' if pixels == new else None
        if label is None:
            raise ValueError(f'Frame matches neither complete font layout: {path}')
        labels.append(label)
    if not ('old' in labels and 'new' in labels) or labels != sorted(labels, key=lambda x: x == 'new'):
        raise ValueError(f'Expected one transition from old to new: {labels}')
    return f'Exact 1920x1080 match across {len(labels)} frames: {", ".join(labels)}'


def run_command(args):
    if args.action == 'v2-reference':
        spec = importlib.util.spec_from_file_location('font_reference', args.reference)
        if spec is None or spec.loader is None:
            raise ValueError('reference source must be a Python constants file')
        source = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(source)
        data = compile_font(source)
        binary = image_bytes(data)
        FontImage(binary)
        lines = ['"""Generated reference fixture; use red2-font generate_font.py v2-reference."""', '']
        constants = dict(data, FONT_HEADER_WORDS=struct.unpack('<40I', binary[:160]))
        for name, value in constants.items():
            if name == 'FONT_WORDS':
                lines += ['FONT_WORDS = (']
                lines += ['    '+', '.join(str(w) for w in value[i:i+8])+',' for i in range(0,1024,8)]
                lines += [')']
            else:
                lines.append(f'{name} = {value!r}')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text('\n'.join(lines)+'\n')
        if args.binary:
            args.binary.parent.mkdir(parents=True, exist_ok=True)
            args.binary.write_bytes(binary)
        return f'{data["USED_BYTES"]}/4096 bytes; {data["GLYPH_COUNT"]} glyphs; {data["PATTERN_COUNT"]} patterns'
    if args.action == 'validate-v2':
        font = FontImage(args.binary.read_bytes())
        return f'Valid font v2: {font.header[10]} glyphs; {font.header[4]}/4096 payload bytes'
    font = FontImage(args.font.read_bytes())
    return verify_reload(font, args.capture_directory, args.text.read_bytes(), args.new_spacing)
