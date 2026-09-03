#!/usr/bin/env python3
"""Contact sheet: all 7 families at 16/32/80 over a light (#FFFFFF) and a dark
(#2B2B2B) ribbon, plus the 16px row zoomed 4x (nearest-neighbour) so pixel
placement can be checked. Usage: python3 contact_sheet.py out.png"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent
FAMILIES = ["icon", "filters", "detail", "cube", "sheet", "comments", "setup"]
LIGHT, DARK = (255, 255, 255), (43, 43, 43)


def load(f, s):
    return Image.open(ASSETS / f"{f}-{s}.png").convert("RGBA")


def band(bg, zoom16=False):
    pad, gap = 24, 28
    rows = [(16, 4 if zoom16 else 1), (32, 1), (80, 1)]
    cell = 80 * 1 + gap
    w = pad * 2 + cell * len(FAMILIES)
    h = pad
    for s, z in rows:
        h += s * z + gap
    im = Image.new("RGBA", (w, h), bg + (255,))
    y = pad
    for s, z in rows:
        for i, f in enumerate(FAMILIES):
            ic = load(f, s)
            if z > 1:
                ic = ic.resize((s * z, s * z), Image.NEAREST)
            x = pad + i * cell + (80 - s * z) // 2
            im.alpha_composite(ic, (x, y))
        y += s * z + gap
    return im


def main(out):
    light, dark = band(LIGHT), band(DARK)
    zoom = band(DARK, zoom16=True).crop((0, 0, dark.width, 24 + 64 + 28))
    zoom_l = band(LIGHT, zoom16=True).crop((0, 0, dark.width, 24 + 64 + 28))
    sheet = Image.new("RGBA", (light.width, light.height + dark.height + zoom.height * 2), (245, 245, 248, 255))
    sheet.alpha_composite(light, (0, 0))
    sheet.alpha_composite(dark, (0, light.height))
    sheet.alpha_composite(zoom_l, (0, light.height + dark.height))
    sheet.alpha_composite(zoom, (0, light.height + dark.height + zoom.height))
    d = ImageDraw.Draw(sheet)
    d.text((6, light.height + dark.height + 4), "16px @4x (nearest) light / dark", fill=(107, 114, 128))
    sheet.convert("RGB").save(out)
    print(out, sheet.size)


if __name__ == "__main__":
    main(sys.argv[1])
