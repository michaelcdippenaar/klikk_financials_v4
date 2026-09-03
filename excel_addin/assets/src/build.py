#!/usr/bin/env python3
"""Klikk Journals ribbon icon set -- vector source + rasteriser.

Design system
-------------
* Every icon is a solid navy tile (brand primary #2B2D6E) carrying a white
  glyph and exactly one small pink (#FF3D7F) mark. The tile is what makes the
  set survive both Excel ribbon themes: on a light ribbon the navy square is
  the contrast, on a dark ribbon the white glyph is.
* Two hand-drawn masters per family, both on a pixel grid:
    - 16-unit master  -> icon-16 (heavier strokes, fewer shapes, every edge
      on a whole pixel).
    - 32-unit master  -> icon-32 (1x), icon-64 (2x), icon-80 (2.5x),
      icon-128 (4x).
  The 16 is never a downscale of the 32.
* Tile radius = 3/16 of the tile (3 @16, 6 @32) -- the smallest radius that
  still reads as rounded at 16px, and the same optical corner at every size.
* Glyph box ~ 12/16 and 20/32: the same ~62% fill at both masters.

Usage:  python3 build.py            (writes ../<family>-<size>.png)
Requires rsvg-convert (brew install librsvg).
"""
from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

NAVY = "#2B2D6E"
WHITE = "#FFFFFF"
PINK = "#FF3D7F"

HERE = Path(__file__).resolve().parent
OUT = HERE.parent

FAMILIES = ["icon", "filters", "detail", "cube", "sheet", "comments", "setup"]
SIZES_FROM_32 = {"icon": [32, 64, 80, 128]}
for _f in FAMILIES:
    SIZES_FROM_32.setdefault(_f, [32, 80, 128])


def rect(x, y, w, h, fill, rx=0):
    r = f' rx="{rx}"' if rx else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}"{r} fill="{fill}"/>'


def poly(points, fill):
    pts = " ".join(f"{x},{y}" for x, y in points)
    return f'<polygon points="{pts}" fill="{fill}"/>'


def line(x1, y1, x2, y2, stroke, w):
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{stroke}" stroke-width="{w}" stroke-linecap="butt"/>')


def hexagon(cx, cy, r):
    """Pointy-top hexagon -- the isometric-cube silhouette."""
    pts = []
    for i in range(6):
        a = math.radians(-90 + 60 * i)
        pts.append((round(cx + r * math.cos(a), 2), round(cy + r * math.sin(a), 2)))
    return pts


def tile(n, r):
    return rect(0, 0, n, n, NAVY, rx=r)


# --------------------------------------------------------------------------
# 32-unit masters. Glyph box is x/y 6..26.
# --------------------------------------------------------------------------
def g32(family):
    e = []
    if family == "icon":
        # Journal: white book, navy rules, pink bookmark.
        e.append(rect(8, 6, 16, 20, WHITE, rx=2))
        e.append(poly([(18, 6), (22, 6), (22, 15), (20, 13), (18, 15)], PINK))
        e.append(rect(12, 18, 8, 2, NAVY))
        e.append(rect(12, 22, 8, 2, NAVY))
    elif family == "filters":
        # Funnel: white mouth, pink stem (what comes through the filter).
        e.append(poly([(6, 7), (26, 7), (26, 9), (19, 16), (13, 16), (6, 9)], WHITE))
        e.append(poly([(13, 16), (19, 16), (19, 23), (13, 26)], PINK))
    elif family == "detail":
        # Raw rows: three lines, the middle one led by a pink marker.
        e.append(rect(6, 7, 20, 3, WHITE))
        e.append(rect(6, 14, 4, 3, PINK))
        e.append(rect(12, 14, 14, 3, WHITE))
        e.append(rect(6, 21, 20, 3, WHITE))
    elif family == "cube":
        # Cube: white hexagon, navy Y edges, pink measure at the intersection.
        e.append(poly(hexagon(16, 16, 10.5), WHITE))
        pts = hexagon(16, 16, 10.5)
        e.append(line(16, 16, 16, pts[3][1], NAVY, 2))          # down
        e.append(line(16, 16, pts[5][0], pts[5][1], NAVY, 2))   # upper-left
        e.append(line(16, 16, pts[1][0], pts[1][1], NAVY, 2))   # upper-right
        e.append(rect(14, 14, 4, 4, PINK, rx=1))
    elif family == "sheet":
        # This sheet: 3x3 grid, active (top-left) cell pink.
        e.append(rect(6, 6, 20, 20, WHITE, rx=2))
        e.append('<path d="M6,8 a2,2 0 0 1 2,-2 h4 v6 h-6 z" fill="%s"/>' % PINK)
        e.append(rect(12, 6, 2, 20, NAVY))
        e.append(rect(18, 6, 2, 20, NAVY))
        e.append(rect(6, 12, 20, 2, NAVY))
        e.append(rect(6, 18, 20, 2, NAVY))
    elif family == "comments":
        # Comment: white bubble with a pink pin.
        e.append(rect(6, 7, 20, 16, WHITE, rx=3))
        e.append(poly([(9, 22), (14, 22), (9, 27)], WHITE))
        e.append(rect(14, 13, 4, 4, PINK, rx=2))
    elif family == "setup":
        # Connection: plug with a pink live tip.
        e.append(rect(11, 5, 3, 7, WHITE))
        e.append(rect(18, 5, 3, 7, WHITE))
        e.append(rect(9, 11, 14, 11, WHITE, rx=2))
        e.append(rect(15, 22, 2, 3, WHITE))
        e.append(rect(14, 25, 4, 4, PINK, rx=2))
    return e


# --------------------------------------------------------------------------
# 16-unit masters, hand-tuned. Glyph box is x/y 2..14. Whole-pixel edges only.
# --------------------------------------------------------------------------
def g16(family):
    e = []
    if family == "icon":
        e.append(rect(4, 2, 8, 12, WHITE, rx=1))
        e.append(poly([(9, 2), (12, 2), (12, 7), (10.5, 5.5), (9, 7)], PINK))
        e.append(rect(6, 9, 4, 1, NAVY))
        e.append(rect(6, 11, 4, 1, NAVY))
    elif family == "filters":
        e.append(poly([(2, 3), (14, 3), (14, 5), (10, 9), (6, 9), (2, 5)], WHITE))
        e.append(poly([(6, 9), (10, 9), (10, 12), (6, 14)], PINK))
    elif family == "detail":
        e.append(rect(2, 3, 12, 2, WHITE))
        e.append(rect(2, 7, 3, 2, PINK))
        e.append(rect(6, 7, 8, 2, WHITE))
        e.append(rect(2, 11, 12, 2, WHITE))
    elif family == "cube":
        pts = hexagon(8, 8, 6.5)
        e.append(poly(pts, WHITE))
        e.append(line(8, 8, 8, pts[3][1], NAVY, 2))
        e.append(line(8, 8, pts[5][0], pts[5][1], NAVY, 2))
        e.append(line(8, 8, pts[1][0], pts[1][1], NAVY, 2))
        e.append(rect(7, 7, 2, 2, PINK))
    elif family == "sheet":
        e.append(rect(2, 2, 12, 12, WHITE, rx=1))
        e.append('<path d="M2,3 a1,1 0 0 1 1,-1 h3 v4 h-4 z" fill="%s"/>' % PINK)
        e.append(rect(6, 2, 1, 12, NAVY))
        e.append(rect(10, 2, 1, 12, NAVY))
        e.append(rect(2, 6, 12, 1, NAVY))
        e.append(rect(2, 10, 12, 1, NAVY))
    elif family == "comments":
        e.append(rect(2, 3, 12, 8, WHITE, rx=1.5))
        e.append(poly([(4, 10), (7, 10), (4, 13)], WHITE))
        e.append(rect(7, 6, 2, 2, PINK))
    elif family == "setup":
        e.append(rect(5, 2, 2, 3, WHITE))
        e.append(rect(9, 2, 2, 3, WHITE))
        e.append(rect(3, 5, 10, 6, WHITE, rx=1))
        e.append(rect(7, 11, 2, 1, WHITE))
        e.append(rect(7, 12, 2, 2, PINK))
    return e


def svg(n, radius, elements):
    body = "\n  ".join(elements)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{n}" height="{n}" '
            f'viewBox="0 0 {n} {n}" shape-rendering="geometricPrecision">\n'
            f'  {tile(n, radius)}\n  {body}\n</svg>\n')


def render(svg_path: Path, png_path: Path, size: int):
    subprocess.run(
        ["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(png_path), str(svg_path)],
        check=True,
    )


def main():
    for fam in FAMILIES:
        s16 = HERE / f"{fam}-16.svg"
        s32 = HERE / f"{fam}-32.svg"
        s16.write_text(svg(16, 3, g16(fam)))
        s32.write_text(svg(32, 6, g32(fam)))
        render(s16, OUT / f"{fam}-16.png", 16)
        for size in SIZES_FROM_32[fam]:
            render(s32, OUT / f"{fam}-{size}.png", size)
        print(fam, "ok")


if __name__ == "__main__":
    os.chdir(HERE)
    main()
