"""A trace laid out as a waterfall: one global axis, one row per span.

A bar is placed against the whole trace, never its parent: ``trace`` says why.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .spans import all_spans, _int_or_none, is_instant, raw_spans_of, Span

# An exporter can emit thousands of spans for a trivial task. Every row is
# rendered server-side with its own detail pane, so the page has to be bounded.
MAX_ROWS = 2000


@dataclass(frozen=True)
class Extent:
    """The window the axis covers, derived from the spans themselves.

    Never from the host wall clock: spans are stamped inside the container and
    ``totals.wall_ns`` on the host, so mixing them puts bars in the wrong place.
    """

    start_ns: int = 0
    end_ns: int = 1
    span_ns: int = 1        # floored at 1 — an all-instant trace would divide by zero


@dataclass
class Tick:
    pct: float
    ns: int                 # offset from the extent start, not an epoch value


@dataclass
class Row:
    """One rendered waterfall row: a span placed on the global axis."""

    span: Span
    depth: int = 0
    has_children: bool = False
    offset_ns: int = 0          # exact int; only the percentages become floats
    left_pct: float = 0.0
    width_pct: float = 0.0
    is_instant: bool = False


def extent_of(roots: list) -> Extent:
    spans = list(all_spans(roots))
    if not spans:
        return Extent()
    start = min(s.start_ns for s in spans)
    end = max((s.end_ns if s.end_ns is not None else s.start_ns) for s in spans)
    # Floored at 1: a trace whose spans are all instants has end == start, and
    # every percentage would come out NaN or Infinity.
    return Extent(start_ns=start, end_ns=end, span_ns=max(end - start, 1))


def raw_extent(spans: list) -> Extent:
    """The axis window, taken from the raw document.

    Called with the *unfiltered* spans on purpose: hiding one must not slide the
    remaining bars sideways, or a filter would look like it changed timings it
    was only asked to leave out. What it removes shows up as empty axis instead.
    Matches `extent_of` span for span, including its treatment of a span with no
    usable start (0, like `_span`) or no end (its own start).
    """
    starts, ends = [], []
    for raw in raw_spans_of(spans):
        start = _int_or_none(raw.get("start_ns")) or 0
        end = _int_or_none(raw.get("end_ns"))
        starts.append(start)
        ends.append(start if end is None else end)
    if not starts:
        return Extent()
    low, high = min(starts), max(ends)
    return Extent(start_ns=low, end_ns=high, span_ns=max(high - low, 1))


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def place(span: Span, extent: Extent) -> tuple:
    """(left_pct, width_pct) on the global axis.

    All arithmetic is Python int until this line, so the nanosecond magnitudes
    that overflow a JS double are exact here.
    """
    left = _clamp((span.start_ns - extent.start_ns) / extent.span_ns * 100)
    duration = span.duration_ns or 0
    width = _clamp(duration / extent.span_ns * 100, 0.0, 100.0 - left)
    return left, width


def flatten(roots: list, collapsed=frozenset(), extent: Extent | None = None) -> tuple:
    """Depth-first rows, skipping the children of collapsed spans.

    Returns (rows, truncated). The template renders every row so the browser can expand descendants locally.

    *extent* is the window the rows are placed on and their offsets counted
    from. A zoomed view passes the subtree's own window here (`extent_of` over
    the one span), so every offset it shows is from the zoomed span's start —
    the same origin its axis ticks use.
    """
    if extent is None:
        extent = extent_of(roots)

    rows: list = []
    truncated = 0
    seen: set = set()

    def walk(span, depth):
        nonlocal truncated
        if span.span_id and span.span_id in seen:
            return
        seen.add(span.span_id)
        if len(rows) >= MAX_ROWS:
            # Count what is being dropped, so the page can say so rather than
            # silently ending mid-tree.
            truncated += 1 + sum(1 for _ in all_spans(span.children))
            return
        left, width = place(span, extent)
        rows.append(
            Row(
                span=span,
                depth=depth,
                has_children=bool(span.children),
                offset_ns=span.start_ns - extent.start_ns,
                left_pct=left,
                width_pct=width,
                is_instant=is_instant(span),
            )
        )
        if span.children and span.span_id not in collapsed:
            for child in span.children:
                walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    return rows, truncated


def ticks_for(extent: Extent, n: int = 5) -> list:
    """Axis ticks on a 1/2/5×10^k step, labelled as offsets from the start."""
    span = extent.span_ns
    if span <= 1:
        return [Tick(pct=0.0, ns=0)]
    rough = span / max(n, 1)
    magnitude = 10 ** int(math.floor(math.log10(rough)))
    step = next(
        (m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= rough),
        10 * magnitude,
    )
    ticks = []
    value = 0
    while value <= span and len(ticks) <= n * 3:
        ticks.append(Tick(pct=value / span * 100, ns=int(value)))
        value += step
    return ticks
