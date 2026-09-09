"""Generate the font-independent MBU/SL multi-frame Windows icon."""

from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
import struct
import sys

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path(__file__).with_name("mbu_sl_laboratory_tile.ico")
SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
TEAL = (8, 126, 114, 255)
WHITE = (255, 255, 255, 255)
MINT = (220, 239, 235, 255)

SMALL_GLYPHS = {
    "M": ("101", "111", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "U": ("101", "101", "101", "101", "111"),
    "S": ("111", "100", "111", "001", "111"),
    "L": ("100", "100", "100", "100", "111"),
}
LARGE_GLYPHS = {
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
}


def _bitmap_word(draw: ImageDraw.ImageDraw, word: str, glyphs: dict,
                 x: int, y: int, scale: int, color, bold: bool = False) -> None:
    cursor = x
    for letter in word:
        glyph = glyphs[letter]
        width = len(glyph[0])
        for row, pattern in enumerate(glyph):
            for column, pixel in enumerate(pattern):
                if pixel == "1":
                    left = cursor + column * scale
                    top = y + row * scale
                    right = left + scale - 1 + (1 if bold and scale > 1 else 0)
                    bottom = top + scale - 1
                    draw.rectangle((left, top, right, bottom), fill=color)
        cursor += (width + 1) * scale


def _small_frame(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = max(2, size // 6)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=TEAL)
    glyphs = SMALL_GLYPHS if size <= 20 else LARGE_GLYPHS
    glyph_width = 3 if size <= 20 else 5
    glyph_height = 5 if size <= 20 else 7
    top_width = glyph_width * 3 + 2
    bottom_width = glyph_width * 2 + 1
    top_y = 2 if size <= 24 else 3
    bottom_y = size - glyph_height - 2
    _bitmap_word(draw, "MBU", glyphs, (size - top_width) // 2, top_y, 1, WHITE)
    _bitmap_word(draw, "SL", glyphs, (size - bottom_width) // 2,
                 bottom_y, 1, WHITE)
    rung_y = (top_y + glyph_height + bottom_y) // 2
    draw.line((size // 2, rung_y - 1, size // 2, rung_y + 1), fill=MINT, width=1)
    return image


def _large_frame(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(2, round(size * .03))
    draw.rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=round(size * .16), fill=TEAL)
    scale = max(1, size // 48)
    glyph_w = 5 * scale
    top_width = glyph_w * 3 + 2 * scale * 2
    bottom_width = glyph_w * 2 + scale * 3
    _bitmap_word(draw, "MBU", LARGE_GLYPHS, (size - top_width) // 2,
                 round(size * .17), scale, WHITE)
    _bitmap_word(draw, "SL", LARGE_GLYPHS, (size - bottom_width) // 2,
                 round(size * .60), scale, WHITE, bold=True)
    rung_y = size // 2
    rung_width = max(2, scale)
    draw.line((size // 2, rung_y - 2 * scale, size // 2, rung_y + 2 * scale),
              fill=MINT, width=rung_width)
    return image


def render_ico() -> bytes:
    payloads: list[bytes] = []
    for size in SIZES:
        frame = _small_frame(size) if size <= 32 else _large_frame(size)
        stream = BytesIO()
        frame.save(stream, format="PNG", optimize=False, compress_level=9)
        payloads.append(stream.getvalue())
    header_size = 6 + 16 * len(SIZES)
    offset = header_size
    directory = bytearray(struct.pack("<HHH", 0, 1, len(SIZES)))
    for size, payload in zip(SIZES, payloads):
        encoded_size = 0 if size == 256 else size
        directory.extend(struct.pack(
            "<BBBBHHII", encoded_size, encoded_size, 0, 0, 1, 32,
            len(payload), offset))
        offset += len(payload)
    return bytes(directory) + b"".join(payloads)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    expected = render_ico()
    output = args.output.resolve()
    if args.check:
        if not output.is_file() or output.read_bytes() != expected:
            print(f"stale generated application icon: {output}", file=sys.stderr)
            return 1
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
