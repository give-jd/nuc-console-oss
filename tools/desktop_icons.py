#!/usr/bin/env python3
"""The icons of the desktop app (desktop/src-tauri/icons): drawn here, standard library only, so that they are code like the rest.

    python3 tools/desktop_icons.py [--out desktop/src-tauri/icons]

The picture: a monitor whose screen draws a pulse, on a dark rounded square. Every size is drawn on its own (4 x 4 samples a pixel), and
written as the files the Tauri bundler reads: 32x32.png, 128x128.png, 128x128@2x.png, icon.png (512, Linux), icon.ico (16 to 256, PNG
entries) and icon.icns (PNG entries). The output depends only on this file: the same bytes every time (no time in a PNG chunk)."""
import argparse
import math
import os
import struct
import sys
import zlib

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "desktop", "src-tauri", "icons")
SS = 4  # samples per pixel and axis

BG, EDGE, SCREEN, FRAME, PULSE = (0x0f, 0x17, 0x20), (0x30, 0x36, 0x3d), (0x16, 0x1b, 0x22), (0xc9, 0xd1, 0xd9), (0x3f, 0xb9, 0x50)
PULSE_PTS = ((0.27, 0.48), (0.38, 0.48), (0.43, 0.36), (0.50, 0.60), (0.56, 0.42), (0.60, 0.48), (0.73, 0.48))


def in_round_rect(x, y, x0, y0, x1, y1, r):
    """Is (x, y) inside the rectangle with corners of radius r?"""
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def near_polyline(x, y, pts, half):
    """Is (x, y) within `half` of the polyline?"""
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        if (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2 <= half * half:
            return True
    return False


def colour(x, y):
    """The colour at (x, y) of the unit square, or None (transparent): the topmost shape wins."""
    if not in_round_rect(x, y, 0.06, 0.06, 0.94, 0.94, 0.2):
        return None
    if not in_round_rect(x, y, 0.08, 0.08, 0.92, 0.92, 0.18):
        return EDGE
    if near_polyline(x, y, PULSE_PTS, 0.022):
        return PULSE
    if in_round_rect(x, y, 0.2, 0.24, 0.8, 0.68, 0.06):
        return SCREEN if in_round_rect(x, y, 0.235, 0.275, 0.765, 0.645, 0.03) else FRAME
    if 0.45 <= x <= 0.55 and 0.68 <= y <= 0.77 or in_round_rect(x, y, 0.33, 0.76, 0.67, 0.80, 0.02):
        return FRAME
    return BG


def draw(size):
    """-> the RGBA rows of the icon at size x size (bytes per row, filter byte first, as PNG wants them)."""
    rows = []
    n = SS * SS
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    c = colour((px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size)
                    if c is not None:
                        r, g, b, a = r + c[0], g + c[1], b + c[2], a + 1
            if a:
                row += bytes((round(r / a), round(g / a), round(b / a), round(255 * a / n)))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return rows


def png(size, rows=None):
    """-> the PNG bytes of the icon at size x size (8-bit RGBA, no other chunk)."""
    rows = rows if rows is not None else draw(size)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def ico(pngs):
    """-> an .ico holding PNG images: pngs is [(size, bytes)] (256 is written as 0, as the format says)."""
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset, entries, body = 6 + 16 * len(pngs), b"", b""
    for size, data in pngs:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset + len(body))
        body += data
    return head + entries + body


ICNS_TYPES = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08", 512: b"ic09"}


def icns(pngs):
    """-> an .icns holding PNG images: pngs is [(size, bytes)] of the sizes ICNS_TYPES names."""
    body = b"".join(ICNS_TYPES[size] + struct.pack(">I", 8 + len(data)) + data for size, data in pngs)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def build(out=OUT, log=print):
    """Draw every size and write the files the bundler reads into `out`."""
    os.makedirs(out, exist_ok=True)
    done = {}
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        done[size] = png(size)
        log("drew %d x %d" % (size, size))
    files = {"32x32.png": done[32], "128x128.png": done[128], "128x128@2x.png": done[256], "icon.png": done[512],
             "icon.ico": ico([(s, done[s]) for s in (16, 24, 32, 48, 64, 128, 256)]),
             "icon.icns": icns([(s, done[s]) for s in sorted(ICNS_TYPES)])}
    for name, data in files.items():
        with open(os.path.join(out, name), "wb") as f:
            f.write(data)
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=OUT, help="the folder to write into (default: desktop/src-tauri/icons)")
    a = ap.parse_args(argv)
    for name in sorted(build(a.out)):
        print(os.path.join(a.out, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
