#!/usr/bin/env python3
"""Generate the app icons (PNG + ICO) for eQSL Card Downloader.

Run from anywhere:  python3 packaging/make_icons.py

Produces:
    assets/eqsl-card-downloader.png       256x256 app icon (AppImage / GUI window)
    assets/eqsl-card-downloader-512.png   512x512 hi-res
    assets/eqsl-card-downloader.ico       multi-size Windows icon
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (size, size), top)
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(size - 1, 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color)
    return img


def render(size: int) -> Image.Image:
    scale = size / 256.0
    base = _vertical_gradient(size, (18, 52, 110), (10, 24, 54)).convert("RGBA")

    # Rounded corners mask
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(52 * scale), fill=255
    )
    base.putalpha(mask)

    draw = ImageDraw.Draw(base)

    # QSL card body
    card = [int(52 * scale), int(86 * scale), int(204 * scale), int(184 * scale)]
    draw.rounded_rectangle(card, radius=int(14 * scale), fill=(245, 246, 250, 255))
    draw.rounded_rectangle(
        card, radius=int(14 * scale), outline=(255, 196, 80, 255), width=max(2, int(5 * scale))
    )
    # Card header stripe
    draw.rounded_rectangle(
        [card[0], card[1], card[2], card[1] + int(26 * scale)],
        radius=int(12 * scale), fill=(24, 92, 170, 255),
    )
    draw.rectangle(
        [card[0], card[1] + int(16 * scale), card[2], card[1] + int(26 * scale)],
        fill=(24, 92, 170, 255),
    )

    # Text on the card
    draw.text(
        (int(128 * scale), int(99 * scale)), "QSL",
        font=_font(int(20 * scale)), fill=(255, 255, 255, 255), anchor="mm",
    )
    draw.text(
        (int(128 * scale), int(148 * scale)), "eQSL",
        font=_font(int(34 * scale)), fill=(18, 52, 110, 255), anchor="mm",
    )

    # Radio waves top-left
    for i, r in enumerate((16, 27, 38)):
        bbox = [
            int((22 - r) * scale), int((30 - r) * scale),
            int((22 + r) * scale), int((30 + r) * scale),
        ]
        draw.arc(bbox, start=200, end=340, fill=(255, 196, 80, 255), width=max(2, int(4 * scale)))
    draw.ellipse(
        [int(18 * scale), int(26 * scale), int(26 * scale), int(34 * scale)],
        fill=(255, 196, 80, 255),
    )

    # Bottom accent bar
    draw.rounded_rectangle(
        [int(52 * scale), int(200 * scale), int(204 * scale), int(212 * scale)],
        radius=int(6 * scale), fill=(255, 196, 80, 255),
    )
    return base


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)

    icon256 = render(256)
    icon256.save(ASSETS / "eqsl-card-downloader.png")
    render(512).save(ASSETS / "eqsl-card-downloader-512.png")

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon256.save(ASSETS / "eqsl-card-downloader.ico", sizes=sizes)

    print("Icons written to", ASSETS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
