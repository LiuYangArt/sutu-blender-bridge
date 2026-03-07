from __future__ import annotations

import binascii
import struct
import zlib

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _build_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)


def encode_rgba_png(width: int, height: int, pixels: bytes) -> bytes:
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")

    row_bytes = width * 4
    required_bytes = row_bytes * height
    if len(pixels) < required_bytes:
        raise ValueError(f"rgba payload is too short: {len(pixels)} < {required_bytes}")

    raw = bytearray()
    for row_index in range(height):
        start = row_index * row_bytes
        raw.append(0)
        raw.extend(pixels[start : start + row_bytes])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=6)
    return b"".join(
        (
            _PNG_SIGNATURE,
            _build_chunk(b"IHDR", ihdr),
            _build_chunk(b"IDAT", idat),
            _build_chunk(b"IEND", b""),
        )
    )
