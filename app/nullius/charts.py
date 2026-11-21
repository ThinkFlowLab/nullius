"""Categorical charts: a few named values, one per item (a run), drawn as bars, dots or a scatter.

The trace distributions (:mod:`nullius.traces.charts`) bin nanosecond durations on a log axis; these place a
handful of plain values on a linear one, so the two share no scale and no code. They share the split: this module
computes the geometry, in SVG user units, and ``templates/_charts.html`` draws it without arithmetic.

A caller passes :class:`Metric` objects, each a label, a tick formatter and one :class:`Value` per item, already
computed and formatted as the caller shows them elsewhere: nothing here recomputes a value. A value of ``None`` is a
gap, never a zero. A ``check`` item is drawn hollow and named so; nothing here ranks items. An item's ``series``
gives its marks one colour in every chart, the class ``s1`` to ``s8`` (cycling), which the stylesheet maps to
``--series-1`` to ``--series-8``; the item's name always says which it is.

Each chart is one :class:`Panel`, drawn by a style of :data:`STYLES` from as many metrics as its ``Style.axes`` name:
one for bars and dots, x then y for a scatter. A caller reads the registry to offer the styles and, for each, a choice
of each axis's metric. Adding a style is one drawing function, taking those metrics and returning a :class:`Panel` of
:class:`Mark` and :class:`Text` that the template draws as they come, and one :data:`STYLES` entry. A new mark shape
also needs its one element in the template.

Stdlib only, and no Flask.
"""

from __future__ import annotations

import math
import textwrap
from dataclasses import dataclass
from typing import Callable

WIDTH = 360.0           # every panel's viewBox width; the page scales it to the panel
_LINE = 15.0            # a text line's height
_CHARS = 46             # a label's line length, at the template's font size; longer labels wrap, never clip
_BAR = 10.0             # a bar's thickness
_ROW_GAP = 9.0
_LEFT, _RIGHT = 8.0, 70.0   # room before the plot, and after it for the values
_AXIS = 20.0            # the tick labels under the rows
_DOT = 4.5
SERIES = 8              # the series colours, --series-1 to --series-8 in style.css
_CHAR_W = 7.0           # a character's width, at the template's font size


@dataclass(frozen=True)
class Value:
    """One item's value: ``value`` None for no measurement; ``text`` as the caller shows it, ``note`` beside it."""

    label: str
    value: float | None
    text: str = ""
    note: str = ""
    check: bool = False
    baseline: bool = False
    tag: str = ""           # a short name, where the full one does not fit (a scatter's dot)
    series: int = 0         # its colour, 1 on, the same in every chart; 0 for none

    def cls(self, shape: str) -> str:
        """A mark's classes: its shape, its series (``s1`` to ``s<SERIES>``, cycling) and whether it is a check."""
        return " ".join(filter(None, (shape, self.series and f"s{(self.series - 1) % SERIES + 1}", self.check and "check")))

    @property
    def name(self) -> str:
        """The label with what sets the item apart."""
        tags = [t for t, on in (("baseline", self.baseline), ("check", self.check)) if on]
        return f"{self.label} ({', '.join(tags)})" if tags else self.label

    @property
    def reading(self) -> str:
        return " ".join(filter(None, (self.text or "—", self.note)))


@dataclass(frozen=True)
class Metric:
    label: str
    values: tuple
    fmt: Callable = str     # a tick's label


@dataclass(frozen=True)
class Mark:
    """One shape: a rect (x, y, w, h), a circle (x, y, r) or a line (x, y to x2, y2); ``title`` its reading."""

    shape: str
    x: float
    y: float
    w: float = 0.0
    h: float = 0.0
    r: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    cls: str = ""
    title: str = ""


@dataclass(frozen=True)
class Text:
    x: float
    y: float                # the first line's baseline
    lines: tuple
    anchor: str = "start"
    cls: str = ""
    leading: float = _LINE


@dataclass(frozen=True)
class Panel:
    """One SVG: its ``title`` names it, ``table`` (head, rows) carries every value as text."""

    title: str
    height: float
    marks: tuple = ()
    texts: tuple = ()
    table: tuple = ((), ())
    width: float = WIDTH

    @property
    def viewbox(self) -> str:
        return f"0 0 {self.width:g} {self.height:g}"


def nice_ticks(lo: float, hi: float, count: int = 4, step_min: float = 0.0) -> tuple:
    """Round ticks enclosing ``[lo, hi]``, about ``count`` steps, none finer than ``step_min``; ints where whole."""
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        pad = abs(lo) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = max(next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), step_min)
    first, last = math.floor(lo / step + 1e-9), math.ceil(hi / step - 1e-9)
    return tuple(_clean(k * step) for k in range(first, last + 1))


def _clean(v: float):
    v = round(v, 10)
    return int(v) if v == int(v) else v


def _tick(fmt: Callable, v) -> str:
    return "0" if v == 0 else str(fmt(v))


class _Scale:
    """A linear map of ``ticks``' span onto [a, b] user units."""

    def __init__(self, ticks: tuple, a: float, b: float):
        self.ticks, self.lo, self.hi, self.a, self.b = ticks, ticks[0], ticks[-1], a, b

    def __call__(self, v: float) -> float:
        return round(self.a + (v - self.lo) / (self.hi - self.lo) * (self.b - self.a), 2)


def _ticks_for(values: list, with_zero: bool) -> tuple:
    """A metric's ticks: around its present values, from zero when its marks grow from zero."""
    if with_zero:
        values = values + [0]
    lo, hi = (min(values), max(values)) if values else (0, 1)
    if with_zero and lo == hi == 0:
        hi = 1
    whole = values and all(isinstance(v, int) for v in values)
    return nice_ticks(lo, hi, step_min=1 if whole else 0)


def _x_labels(scale: _Scale, fmt: Callable, y: float) -> list:
    """The labels of an x axis's ticks at ``y``, the end ones inside the panel. A label that would touch the one
    before it is left out, the last one never: the gridlines still mark every tick."""
    last = len(scale.ticks) - 1
    kept = []
    for i, t in enumerate(scale.ticks):
        text = _tick(fmt, t)
        anchor = "start" if i == 0 else "end" if i == last else "middle"
        x, w = scale(t), len(text) * _CHAR_W
        x0 = {"start": x, "end": x - w, "middle": x - w / 2}[anchor]
        while kept and kept[-1][1] + 6 > x0 and i == last and len(kept) > 1:
            kept.pop()
        if not kept or kept[-1][1] + 6 <= x0 or i == last:
            kept.append((Text(x, y, (text,), anchor, "tick"), x0 + w))
    return [t for t, _ in kept]


def _rows(metric: Metric, with_zero: bool, mark: Callable) -> Panel:
    """A row per item, its name over its mark, on one x axis: what bars and dots share. ``mark(v, scale, y)``
    gives the marks of ``v`` at the row's centre ``y``, none for a gap, and the x its reading starts at. The
    gridlines and the baseline's reference cross each row's band only, never a name."""
    present = [v.value for v in metric.values if v.value is not None]
    scale = _Scale(_ticks_for(present, with_zero), _LEFT, WIDTH - _RIGHT)
    base = next((v for v in metric.values if v.baseline and v.value is not None), None)
    base = base if len(present) > 1 else None
    marks, texts, y = [], [], 4.0
    for v in metric.values:
        lines = tuple(textwrap.wrap(v.name, _CHARS)) or ("",)
        texts.append(Text(_LEFT, y + _LINE - 3, lines, cls="label"))
        y += _LINE * len(lines)
        mid = y + 4 + _BAR / 2
        top, bottom = mid - _BAR / 2 - 3, mid + _BAR / 2 + 3
        marks += [Mark("line", scale(t), top, x2=scale(t), y2=bottom, cls="grid") for t in scale.ticks]
        if base:
            marks.append(Mark("line", scale(base.value), top, x2=scale(base.value), y2=bottom, cls="ref", title=_title(base)))
        drawn, at = mark(v, scale, mid)
        marks += drawn
        texts.append(Text(at, mid + 4, (v.text or "—",), cls="value" if drawn else "value none"))
        y += 4 + _BAR + _ROW_GAP
    bottom = y - _ROW_GAP + 2
    return Panel(metric.label, round(bottom + _AXIS, 2), tuple(marks), tuple(texts + _x_labels(scale, metric.fmt, bottom + 14)),
                 (("Name", metric.label), tuple((v.name, v.reading) for v in metric.values)))


def _title(v: Value) -> str:
    return f"{v.name}: {v.reading}"


def bars(metric: Metric) -> Panel:
    """A bar per item from zero; a present value always shows a mark, however small."""
    def bar(v, scale, mid):
        if v.value is None:
            return [], scale(0) + 4
        a, b = sorted((scale(0), scale(v.value)))
        w = max(b - a, 1.5)
        return [Mark("rect", a, mid - _BAR / 2, w=round(w, 2), h=_BAR, cls=v.cls("bar"), title=_title(v))], max(a + w, scale(0)) + 4
    return _rows(metric, True, bar)


def dots(metric: Metric) -> Panel:
    """A dot per item on a track, the axis around the values rather than from zero: close values stay apart."""
    def dot(v, scale, mid):
        if v.value is None:
            return [], scale.b + 10
        return [Mark("line", scale.a, mid, x2=scale.b, y2=mid, cls="track"),
                Mark("circle", scale(v.value), mid, r=_DOT, cls=v.cls("dot"), title=_title(v))], scale.b + 10
    return _rows(metric, False, dot)


_S_HEIGHT, _S_LEFT, _S_TOP, _S_BOTTOM = 270.0, 48.0, 22.0, 38.0
_TAG_W, _TAG_H = 7.5, 13.0     # a tag's width per character and its height, at the template's font size


def _tag_box(px: float, py: float, width: float, side: str) -> tuple:
    """Where a tag ``width`` wide goes on ``side`` of a dot at (px, py): (x, baseline, anchor, box)."""
    gap = _DOT + 3
    x, base, anchor = {"right": (px + gap, py + 4, "start"), "left": (px - gap, py + 4, "end"),
                       "above": (px, py - gap - 2, "middle"), "below": (px, py + gap + 10, "middle")}[side]
    x0 = {"start": x, "end": x - width, "middle": x - width / 2}[anchor]
    return x, base, anchor, (x0, base - _TAG_H + 3, x0 + width, base + 3)


def _overlaps(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def scatter(x: Metric, y: Metric) -> Panel:
    """A dot per item at (x, y), and its tag (``Value.tag``, else its place in the order) beside it, spelled out in a
    key under the plot. Every item keeps its own dot: items at one place are set in a ring around it, each tag outward
    from its dot. Any other tag takes the first side of its dot inside the plot and clear of the dots and tags before
    it."""
    pairs = list(zip(x.values, y.values))
    tags = [a.tag or str(i + 1) for i, (a, _) in enumerate(pairs)]
    shown = [(t, a, b) for t, (a, b) in zip(tags, pairs) if a.value is not None and b.value is not None]
    left, right, top, bottom = _S_LEFT, WIDTH - 8, _S_TOP, _S_HEIGHT - _S_BOTTOM
    # the scales inset from the frame, so a dot on the last tick is whole
    sx = _Scale(_ticks_for([a.value for _, a, _ in shown], False), left + 12, right - 12)
    sy = _Scale(_ticks_for([b.value for _, _, b in shown], False), bottom - 12, top + 12)
    marks = [Mark("line", sx(t), top, x2=sx(t), y2=bottom, cls="grid") for t in sx.ticks]
    marks += [Mark("line", left, sy(t), x2=right, y2=sy(t), cls="grid") for t in sy.ticks]
    texts = _x_labels(sx, x.fmt, bottom + 14)
    texts += [Text(left - 4, sy(t) + 4, (_tick(y.fmt, t),), "end", "tick") for t in sy.ticks]
    texts += [Text(left, 12, (y.label,), cls="axis-title"), Text(right, bottom + 30, (x.label,), "end", "axis-title")]
    places: dict = {}
    for item in shown:
        places.setdefault((sx(item[1].value), sy(item[2].value)), []).append(item)
    dots = []     # (x, y, the tag's place on its ring or None, tag, x value, y value)
    for (px, py), group in places.items():
        ring = max(_DOT * 2, len(group) * (2 * _DOT + 4) / (2 * math.pi)) if len(group) > 1 else 0.0
        for k, item in enumerate(group):
            turn = 2 * math.pi * k / len(group) - math.pi / 2
            at = (round(px + (ring + 11) * math.cos(turn), 2), round(py + (ring + 11) * math.sin(turn) + 4, 2)) if ring else None
            dots.append((round(px + ring * math.cos(turn), 2), round(py + ring * math.sin(turn), 2), at, *item))
    taken = [(px - _DOT, py - _DOT, px + _DOT, py + _DOT) for px, py, *_ in dots]
    taken += [(tx - _TAG_W, ty - _TAG_H + 3, tx + _TAG_W, ty + 3) for _, _, (tx, ty), *_ in (d for d in dots if d[2])]
    for px, py, at, tag, a, b in dots:
        marks.append(Mark("circle", px, py, r=_DOT, cls=a.cls("dot"),
                          title=f"{tag} {a.name}: {x.label} {a.reading}, {y.label} {b.reading}"))
        if at:      # on a ring, the tag sits outward from its dot
            texts.append(Text(*at, (tag,), "middle", "tag"))
            continue
        width = _TAG_W * len(tag)
        spots = [_tag_box(px, py, width, side) for side in ("right", "left", "above", "below")]
        inside = [s for s in spots if s[3][0] >= left and s[3][2] <= right and s[3][1] >= top and s[3][3] <= bottom]
        tx, base, anchor, box = next((s for s in inside if not any(_overlaps(s[3], t) for t in taken)), (inside or spots)[0])
        taken.append(box)
        texts.append(Text(tx, base, (tag,), anchor, "tag"))
    key_y = _S_HEIGHT + 8
    key = _LEFT + (12 if any(a.series for a, _ in pairs) else 0)
    drawn = {id(d[4]) for d in dots}
    for tag, (a, _) in zip(tags, pairs):
        # a name the caller numbered already (two runs of one agent) is not numbered twice
        lines = tuple(textwrap.wrap(a.name if a.name.startswith(f"{tag}. ") else f"{tag}. {a.name}", _CHARS))
        if a.series:        # its colour beside its name; the name says which it is
            marks.append(Mark("rect", _LEFT, key_y - 8, w=8, h=8, cls=a.cls("swatch")))
        texts.append(Text(key, key_y, lines, cls="label" if id(a) in drawn else "label none"))
        key_y += _LINE * len(lines)
    rows = tuple((a.name, a.reading, b.reading) for a, b in pairs)
    return Panel(f"{y.label} by {x.label}", round(key_y, 2), tuple(marks), tuple(texts), (("Name", x.label, y.label), rows))


@dataclass(frozen=True)
class Style:
    """A way to draw: its ``label``, the function, and the names of the metrics it takes, in its arguments' order."""

    label: str
    draw: Callable
    axes: tuple


STYLES = {
    "bars": Style("Bars", bars, ("Metric",)),
    "dots": Style("Dots", dots, ("Metric",)),
    "scatter": Style("Scatter", scatter, ("X", "Y")),
}
