"""Colour maths for auditing the stylesheet's palettes.

`style.css` states the bar its palettes were chosen against — WCAG AA against
`--bg`, >=25 CIE76 between outcome colours, >=17 ΔE2000 between tool categories
with dichromacy simulated — but the script that checked it is not in the tree,
so nothing stops a later edit from quietly breaking it. This is that script.

It reads the values out of `style.css` rather than taking a hardcoded copy: an
audit against numbers that have drifted from the stylesheet is worse than no
audit, because it reports success.

Stdlib only. No colour library, so the transforms are written out: sRGB ->
linear -> XYZ -> Lab, plus ΔE2000 and the Machado dichromacy matrices.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

STYLESHEET = Path(__file__).resolve().parents[1] / "static" / "style.css"

# D65, the white point sRGB is defined against.
_WHITE = (0.95047, 1.00000, 1.08883)

# Machado, Oliveira & Fernandes (2009), severity 1.0, applied to linear RGB.
_CVD = {
    "protanopia": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deuteranopia": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
}


def parse_scopes(path: Path = STYLESHEET) -> dict:
    """{scope_name: {token: hex}} for the two theme scopes.

    Only literal hex values are returned; a token defined as `var(--other)` is
    skipped rather than resolved, so a caller asking for it gets a KeyError
    instead of a silently wrong colour.
    """
    css = path.read_text(encoding="utf-8")
    scopes = {}
    for name, selector in (("dark", r":root\s*\{"), ("light", r':root\[data-theme="light"\]\s*\{')):
        match = re.search(selector, css)
        if not match:
            raise AssertionError(f"no {name} scope in {path}")
        depth, i = 1, match.end()
        while depth and i < len(css):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        body = css[match.end():i - 1]
        scopes[name] = {
            token: value
            for token, value in re.findall(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", body)
        }
    return scopes


def to_rgb(value: str) -> tuple:
    """#rgb or #rrggbb -> (r, g, b) floats in 0..1."""
    text = value.lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) not in (6, 8):
        raise ValueError(f"unsupported colour: {value}")
    return tuple(int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def to_linear(rgb: tuple) -> tuple:
    return tuple(_linear(c) for c in rgb)


def luminance(rgb: tuple) -> float:
    r, g, b = to_linear(rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple, b: tuple) -> float:
    """WCAG contrast ratio, 1..21."""
    la, lb = luminance(a), luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def to_lab(rgb: tuple) -> tuple:
    r, g, b = to_linear(rgb)
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b

    def f(t):
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = f(x / _WHITE[0]), f(y / _WHITE[1]), f(z / _WHITE[2])
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e_76(a: tuple, b: tuple) -> float:
    """CIE76. What style.css's >=25 outcome threshold is stated in."""
    return math.dist(to_lab(a), to_lab(b))


def delta_e_2000(c1: tuple, c2: tuple) -> float:
    """CIE ΔE2000. Perceptually saner than CIE76 at high chroma."""
    l1, a1, b1 = to_lab(c1)
    l2, a2, b2 = to_lab(c2)

    avg_l = (l1 + l2) / 2
    c_1, c_2 = math.hypot(a1, b1), math.hypot(a2, b2)
    avg_c = (c_1 + c_2) / 2
    g = 0.5 * (1 - math.sqrt(avg_c ** 7 / (avg_c ** 7 + 25 ** 7))) if avg_c else 0.0

    a1p, a2p = a1 * (1 + g), a2 * (1 + g)
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    avg_cp = (c1p + c2p) / 2

    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0

    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360

    dlp = l2 - l1
    dcp = c2p - c1p
    dhp_big = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp) / 2)

    if c1p * c2p == 0:
        avg_hp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        avg_hp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        avg_hp = (h1p + h2p + 360) / 2
    else:
        avg_hp = (h1p + h2p - 360) / 2

    t = (1
         - 0.17 * math.cos(math.radians(avg_hp - 30))
         + 0.24 * math.cos(math.radians(2 * avg_hp))
         + 0.32 * math.cos(math.radians(3 * avg_hp + 6))
         - 0.20 * math.cos(math.radians(4 * avg_hp - 63)))

    sl = 1 + (0.015 * (avg_l - 50) ** 2) / math.sqrt(20 + (avg_l - 50) ** 2)
    sc = 1 + 0.045 * avg_cp
    sh = 1 + 0.015 * avg_cp * t
    rt = (-2 * math.sqrt(avg_cp ** 7 / (avg_cp ** 7 + 25 ** 7))
          * math.sin(math.radians(60 * math.exp(-(((avg_hp - 275) / 25) ** 2)))))

    return math.sqrt((dlp / sl) ** 2 + (dcp / sc) ** 2 + (dhp_big / sh) ** 2
                     + rt * (dcp / sc) * (dhp_big / sh))


def simulate(rgb: tuple, kind: str) -> tuple:
    """Dichromatic appearance of *rgb*, back in gamma-encoded sRGB."""
    matrix = _CVD[kind]
    lin = to_linear(rgb)
    out = []
    for row in matrix:
        value = max(0.0, min(1.0, sum(m * c for m, c in zip(row, lin))))
        out.append(12.92 * value if value <= 0.0031308
                   else 1.055 * value ** (1 / 2.4) - 0.055)
    return tuple(out)


def lightness(rgb: tuple) -> float:
    return to_lab(rgb)[0]
