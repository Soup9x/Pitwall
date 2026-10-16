"""Draw Pitwall's PNG app icons from the same shapes as static/icons/logo.svg (Pillow can't
render SVG). Run after changing the logo: python scripts/make_icons.py"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "pitwall" / "web" / "static" / "icons"
# (x, y, w, h, colour) on a 64x64 grid, as in logo.svg
BARS = [
    (10, 12, 6, 40, "#E8322F"),
    (22, 14, 32, 8, "#E8EAED"),
    (22, 28, 24, 8, "#9AA3AF"),
    (22, 42, 16, 8, "#5D6573"),
]


def draw(size: int, maskable: bool = False) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:  # full-bleed background, artwork inside the 80% safe zone
        d.rectangle((0, 0, size, size), fill="#171A21")
        scale, off = size * 0.7 / 64, size * 0.15
    else:
        scale, off = size / 64, 0.0
        d.rounded_rectangle(
            (2 * scale, 2 * scale, 62 * scale, 62 * scale),
            radius=12 * scale,
            fill="#171A21",
            outline="#262B36",
            width=max(1, round(2 * scale)),
        )
    for x, y, w, h, colour in BARS:
        d.rounded_rectangle(
            (off + x * scale, off + y * scale, off + (x + w) * scale, off + (y + h) * scale),
            radius=2 * scale,
            fill=colour,
        )
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        draw(size).save(OUT / f"icon-{size}.png")
        draw(size, maskable=True).save(OUT / f"icon-maskable-{size}.png")
    draw(180, maskable=True).convert("RGB").save(OUT / "apple-touch-icon.png")
    draw(32).save(OUT / "favicon-32.png")


if __name__ == "__main__":
    main()
