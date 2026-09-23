#!/usr/bin/env python3
"""Build NUPSON web/PWA raster assets with only the Python standard library."""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def read_png(path: Path) -> tuple[int, int, bytearray]:
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError(f"{path} is not a PNG file")

    offset = len(PNG_SIGNATURE)
    width = height = color_type = None
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if depth != 8 or color_type not in {2, 6} or compression or filtering or interlace:
                raise ValueError("Only non-interlaced 8-bit RGB/RGBA PNG files are supported")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break

    if width is None or height is None or color_type is None:
        raise ValueError("PNG header is missing")

    channels = 4 if color_type == 6 else 3
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    decoded = bytearray(height * stride)
    source_offset = 0
    previous = bytearray(stride)

    def paeth(a: int, b: int, c: int) -> int:
        estimate = a + b - c
        distances = abs(estimate - a), abs(estimate - b), abs(estimate - c)
        return (a, b, c)[distances.index(min(distances))]

    for y in range(height):
        filter_type = raw[source_offset]
        source_offset += 1
        scanline = bytearray(raw[source_offset : source_offset + stride])
        source_offset += stride
        for x in range(stride):
            left = scanline[x - channels] if x >= channels else 0
            above = previous[x]
            upper_left = previous[x - channels] if x >= channels else 0
            if filter_type == 1:
                scanline[x] = (scanline[x] + left) & 255
            elif filter_type == 2:
                scanline[x] = (scanline[x] + above) & 255
            elif filter_type == 3:
                scanline[x] = (scanline[x] + ((left + above) // 2)) & 255
            elif filter_type == 4:
                scanline[x] = (scanline[x] + paeth(left, above, upper_left)) & 255
            elif filter_type != 0:
                raise ValueError(f"Unsupported PNG filter: {filter_type}")
        decoded[y * stride : (y + 1) * stride] = scanline
        previous = scanline

    if channels == 4:
        return width, height, decoded

    rgba = bytearray(width * height * 4)
    for pixel in range(width * height):
        rgba[pixel * 4 : pixel * 4 + 3] = decoded[pixel * 3 : pixel * 3 + 3]
        rgba[pixel * 4 + 3] = 255
    return width, height, rgba


def resize(
    source_width: int,
    source_height: int,
    source: bytearray,
    width: int,
    height: int,
    *,
    scale: float = 1.0,
    background: tuple[int, int, int] | None = None,
) -> bytearray:
    destination = bytearray(width * height * 4)
    draw_width = width * scale
    draw_height = height * scale
    left = (width - draw_width) / 2
    top = (height - draw_height) / 2

    for y in range(height):
        for x in range(width):
            target = (y * width + x) * 4
            if x < left or x >= left + draw_width or y < top or y >= top + draw_height:
                rgba = (*background, 255) if background else (0, 0, 0, 0)
            else:
                sx = max(
                    0.0,
                    min(
                        source_width - 1.0,
                        (x - left + 0.5) * source_width / draw_width - 0.5,
                    ),
                )
                sy = max(
                    0.0,
                    min(
                        source_height - 1.0,
                        (y - top + 0.5) * source_height / draw_height - 0.5,
                    ),
                )
                x0, y0 = int(sx), int(sy)
                x1, y1 = min(x0 + 1, source_width - 1), min(y0 + 1, source_height - 1)
                fx, fy = sx - x0, sy - y0
                rgba = []
                for channel in range(4):
                    top_value = (
                        source[(y0 * source_width + x0) * 4 + channel] * (1 - fx)
                        + source[(y0 * source_width + x1) * 4 + channel] * fx
                    )
                    bottom_value = (
                        source[(y1 * source_width + x0) * 4 + channel] * (1 - fx)
                        + source[(y1 * source_width + x1) * 4 + channel] * fx
                    )
                    rgba.append(round(top_value * (1 - fy) + bottom_value * fy))
                if background and rgba[3] < 255:
                    alpha = rgba[3] / 255
                    rgba = [
                        round(rgba[i] * alpha + background[i] * (1 - alpha))
                        for i in range(3)
                    ] + [255]
            destination[target : target + 4] = bytes(rgba)
    return destination


def png_bytes(width: int, height: int, pixels: bytearray) -> bytes:
    raw = b"".join(b"\x00" + pixels[y * width * 4 : (y + 1) * width * 4] for y in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        return struct.pack(">I", len(payload)) + kind + payload + checksum

    header = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    return PNG_SIGNATURE + header + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def write_png(path: Path, width: int, height: int, pixels: bytearray) -> bytes:
    encoded = png_bytes(width, height, pixels)
    path.write_bytes(encoded)
    return encoded


def write_ico(path: Path, images: list[tuple[int, bytes]]) -> None:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for size, image in images:
        encoded_size = size if size < 256 else 0
        entries.extend(
            struct.pack(
                "<BBBBHHII", encoded_size, encoded_size, 0, 0, 1, 32, len(image), offset
            )
        )
        payload.extend(image)
        offset += len(image)
    path.write_bytes(header + entries + payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mark", type=Path, required=True, help="Square source image containing the brand mark"
    )
    parser.add_argument("--banner", type=Path, required=True, help="Wide login banner source")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    navy = (7, 23, 39)
    mark_width, mark_height, mark = read_png(args.mark)
    favicon_images: list[tuple[int, bytes]] = []
    for size in (16, 32, 48):
        pixels = resize(mark_width, mark_height, mark, size, size, background=navy)
        encoded = write_png(args.output / f"favicon-{size}x{size}.png", size, size, pixels)
        favicon_images.append((size, encoded))
    write_ico(args.output / "favicon.ico", favicon_images)

    regular_icons = (
        (120, "apple-touch-icon-120.png"),
        (152, "apple-touch-icon-152.png"),
        (167, "apple-touch-icon-167.png"),
        (180, "apple-touch-icon.png"),
        (192, "icon-192.png"),
        (512, "icon-512.png"),
    )
    for size, filename in regular_icons:
        pixels = resize(mark_width, mark_height, mark, size, size, background=navy)
        write_png(args.output / filename, size, size, pixels)
    for size in (192, 512):
        pixels = resize(mark_width, mark_height, mark, size, size, scale=0.8, background=navy)
        write_png(args.output / f"icon-maskable-{size}.png", size, size, pixels)
    logo = resize(mark_width, mark_height, mark, 256, 256)
    write_png(args.output / "logo-mark.png", 256, 256, logo)

    banner_width, banner_height, banner = read_png(args.banner)
    output_width = 960
    output_height = round(output_width * banner_height / banner_width)
    write_png(
        args.output / "login-banner.png",
        output_width,
        output_height,
        resize(banner_width, banner_height, banner, output_width, output_height),
    )


if __name__ == "__main__":
    main()
