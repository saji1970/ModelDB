"""Real image dimension extraction (CLAUDE-STORAGE.md section 19).

No image library is available (the project has zero third-party
dependencies beyond duckdb/pydantic/typer/rich/pyyaml), so this parses
each container format's own fixed-offset header fields directly -
genuine values read from the file, never guessed. WebP and TIFF are
left unparsed (documented gap: WebP has three sub-formats and TIFF is
a full tag-based container, both non-trivial without a library) -
`extract_image_dimensions` returns `None` for them rather than a
fabricated guess.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageDimensions:
    width: int
    height: int


def _png_dimensions(content: bytes) -> ImageDimensions | None:
    # Signature (8 bytes) + IHDR chunk: 4-byte length, 4-byte "IHDR",
    # then big-endian width/height (4 bytes each).
    if len(content) < 24 or content[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", content[16:24])
    return ImageDimensions(width=width, height=height)


def _bmp_dimensions(content: bytes) -> ImageDimensions | None:
    # 14-byte BITMAPFILEHEADER, then the DIB header's width/height as
    # little-endian *signed* int32 (a negative height means top-down).
    if len(content) < 26:
        return None
    width, height = struct.unpack("<ii", content[18:26])
    return ImageDimensions(width=width, height=abs(height))


def _gif_dimensions(content: bytes) -> ImageDimensions | None:
    # 6-byte signature, then the Logical Screen Descriptor's
    # little-endian unsigned width/height (2 bytes each).
    if len(content) < 10:
        return None
    width, height = struct.unpack("<HH", content[6:10])
    return ImageDimensions(width=width, height=height)


# SOF (Start Of Frame) markers - excludes 0xC4 (DHT), 0xC8 (JPG), 0xCC
# (DAC), which share the 0xC0-0xCF range but aren't frame headers.
_JPEG_SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
_JPEG_NO_LENGTH_MARKERS = frozenset({0xD8, 0xD9}) | frozenset(range(0xD0, 0xD8))


def _jpeg_dimensions(content: bytes) -> ImageDimensions | None:
    if not content.startswith(b"\xff\xd8"):
        return None
    i, n = 2, len(content)
    while i < n - 1:
        if content[i] != 0xFF:
            i += 1
            continue
        marker = content[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in _JPEG_NO_LENGTH_MARKERS:
            i += 2
            continue
        if i + 4 > n:
            return None
        length = struct.unpack(">H", content[i + 2 : i + 4])[0]
        if marker in _JPEG_SOF_MARKERS:
            payload = i + 4
            if payload + 5 > n:
                return None
            height, width = struct.unpack(">HH", content[payload + 1 : payload + 5])
            return ImageDimensions(width=width, height=height)
        i += 2 + length
    return None


_PARSERS = {"png": _png_dimensions, "bmp": _bmp_dimensions, "gif": _gif_dimensions, "jpeg": _jpeg_dimensions}


def extract_image_dimensions(content: bytes, image_format: str) -> ImageDimensions | None:
    parser = _PARSERS.get(image_format)
    if parser is None:
        return None
    try:
        return parser(content)
    except (struct.error, IndexError):
        return None
