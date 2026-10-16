"""Theme tokens: both themes pass WCAG AA contrast for text, and the CSS uses no colour that
isn't a token (outside the token blocks)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "pitwall" / "web" / "static" / "style.css").read_text()


def _block(selector: str) -> dict[str, str]:
    start = CSS.index(selector)
    body = CSS[CSS.index("{", start) + 1 : CSS.index("}", start)]
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", body))


DARK = _block(":root {")
LIGHT = {**DARK, **_block(':root[data-theme="light"] {')}
SYSTEM_LIGHT = {**DARK, **_block(':root[data-theme="system"] {')}


def _lum(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def ch(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_PAIRS = [
    (fg, bg)
    for fg in ("text", "muted", "link", "good", "warn", "bad")
    for bg in ("bg", "surface", "surface-2")
] + [
    ("on-accent", "accent-strong"),
    ("badge-ok-fg", "badge-ok-bg"),
    ("badge-warn-fg", "badge-warn-bg"),
    ("badge-bad-fg", "badge-bad-bg"),
]


@pytest.mark.parametrize("theme", [DARK, LIGHT], ids=["dark", "light"])
@pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
def test_text_contrast_is_aa(theme, fg, bg):
    ratio = contrast(theme[fg], theme[bg])
    assert ratio >= 4.5, f"{fg} on {bg}: {ratio:.2f}"


def test_spec_colours_are_used():
    assert (DARK["bg"], DARK["surface"], DARK["surface-2"], DARK["border"]) == (
        "#0E1014",
        "#171A21",
        "#1E222B",
        "#262B36",
    )
    assert (DARK["text"], DARK["muted"], DARK["accent"]) == ("#E8EAED", "#9AA3AF", "#E8322F")
    assert (LIGHT["bg"], LIGHT["surface"], LIGHT["text"]) == ("#F6F7F9", "#FFFFFF", "#14161A")
    flags = ("flag-green", "flag-yellow", "flag-red", "flag-blue")
    assert [DARK[f] for f in flags] == ["#22C55E", "#FACC15", "#EF4444", "#3B82F6"]
    assert (DARK["timing-purple"], DARK["timing-green"]) == ("#A855F7", "#22C55E")
    tyres = ("tyre-soft", "tyre-medium", "tyre-hard", "tyre-inter", "tyre-wet")
    assert [DARK[t] for t in tyres] == ["#E8322F", "#FACC15", "#E8EAED", "#22C55E", "#3B82F6"]


def test_system_theme_matches_light():
    assert SYSTEM_LIGHT == LIGHT


def test_no_stray_colours_outside_tokens():
    # Strip the three token blocks, then look for hex colours (the tyre chip's fixed dark
    # inner disc is the one allowed literal).
    rest = CSS
    for sel in (":root {", ':root[data-theme="light"] {', ':root[data-theme="system"] {'):
        i = rest.index(sel)
        rest = rest[:i] + rest[rest.index("}", i) + 1 :]
    stray = set(re.findall(r"#[0-9A-Fa-f]{3,6}\b", rest)) - {"#111", "#fff"}
    assert not stray, stray
