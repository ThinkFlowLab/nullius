"""Distribution charts over a trace's spans: histograms and CDFs.

A sibling to the waterfall, answering a different question. The waterfall and the
breakdown strips are *timeline* views — when things happened, and how the wall
clock splits. This module describes the *population* of calls: how long a typical
tool call runs, whether the LLM calls are bimodal, whether a handful of spans
hold most of the time.

**Summed span duration is not wall clock, and nothing here may imply it is.**
Spans nest and overlap — a tool call sits inside the LLM call that asked for it,
and background subagents run alongside the main thread — so adding durations up
overshoots the run, sometimes by a lot. That is exactly why the strips next to
these charts never sum spans: ``Totals`` is the producer's, displayed verbatim,
and ``Breakdown`` *partitions* the timeline instead (see :func:`trace.partition`).
So the weighted axis here is a share of *summed span time*, never a share of the
run, :class:`Charts` carries no ``wall_ns``, and no percentage is ever taken
against one. A chart that looked like the breakdown strip and disagreed with it
would be the worst outcome available.

:func:`charts_of` takes a population of samples rather than a span tree, so
the same four charts can describe several traces at once. Pooling
would not weaken the rule above; it strengthens it. Within one trace a sum at
least overshoots a real clock. Across traces there is no single clock to
overshoot: two trials never shared one, so their summed span time measures
nothing at all. Which is why the only totals here count spans.

Two more things are load-bearing:

* **Bins are log-spaced.** Durations here run from microseconds to minutes.
  Linear bins put every span in the first bar and say nothing.
* **A span with no usable duration is excluded and counted**, never binned at
  zero — :func:`trace.is_instant` is the same predicate the waterfall uses to
  decide a row gets a marker instead of a bar. The count is surfaced so the
  charts cannot quietly describe fewer spans than the filter panel claims.
* **A span that only wraps others is not a call**, so it is left out and counted
  the same way (:data:`_WRAPPERS`). A session's duration is the whole run: drawn,
  it would be the heaviest bar and the slowest kind of every trace, and say so in
  every headline.

Geometry is computed here, in SVG user units against :data:`BOX`, for the same
reason :func:`trace.place` computes the waterfall's: the arithmetic runs on
nanosecond magnitudes that overflow a JS double, and staying in Python int/float
keeps it exact. Values that need formatting leave as integer nanoseconds and get
``|nsdur`` in the template — this module emits numbers, not presentation. The
page stretches the plot to its column, so the template draws lines from these
units and places bars and labels by the percentages :class:`Box` gives: text and
rounded bar ends then keep their size at any width.

Each chart leads with the one number it is for (:class:`Headline`), computed from
the same samples it draws: never a figure the plot could not show.

The legend on a per-kind chart is a control, not a key. Clicking a kind drops it
from *that* chart, and that chart's axes rescale to what is left. This is not
:class:`trace.Filter`, which prunes spans out of the whole view — the waterfall,
the breakdown strip and the saved file all follow that one and none of them
follows this one; the spans are still in the trace, one chart is simply not
drawing them. It lives in the query string all the same
(``chart_off=<chart key>:<kind>``), for the reason the trace filter does: the
page renders from the URL, so the view can be linked and cannot drift from what
it claims to show.

Rescaling on a toggle is deliberate, and the opposite of the waterfall's rule
that its axis never moves under a filter. There the axis is a shared timeline,
and moving it would make a filter look like it had changed the timings it was
only asked to leave out. Here each chart's axis is derived from its own
population, and tool calls squeezed into the corner of an axis defined by LLM
calls that are no longer drawn show nothing at all. The cost is real, though: a
rescaled chart stops sharing its duration axis with the other three, so it says
so underneath itself.

Stdlib only, and no Flask.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from nullius.traces.trace import clean as _clean
from nullius.traces.trace import toggled as _toggled
from nullius.traces.trace import KIND_ORDER, all_spans, is_instant

# The SVG coordinate system every chart is drawn in. One viewBox for all four so
# they line up visually across the grid, with room on the left for value labels
# and below for the duration axis.
VIEW_W = 320.0
VIEW_H = 170.0
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 40.0, 8.0, 10.0, 26.0


@dataclass(frozen=True)
class Box:
    """The plot rectangle inside the viewBox — axes and labels live outside it."""

    x0: float = _PAD_L
    y0: float = _PAD_T
    x1: float = VIEW_W - _PAD_R
    y1: float = VIEW_H - _PAD_B

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    def left(self, x: float) -> float:
        """A user-unit x as a percentage across the plot."""
        return round((x - self.x0) / self.w * 100, 3)

    def top(self, y: float) -> float:
        """A user-unit y as a percentage down the plot."""
        return round((y - self.y0) / self.h * 100, 3)

    def width(self, w: float) -> float:
        return round(w / self.w * 100, 3)

    def height(self, h: float) -> float:
        return round(h / self.h * 100, 3)


BOX = Box()

# How to split a decade, finest first, so a tie takes the finer one. Round steps,
# since a bin is read as a range: "2s to 5s", never "3.16s to 10s".
_SUBDIVISIONS = (
    (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8),
    (1, 1.5, 2, 3, 5, 7),
    (1, 2, 5),
    (1,),
)

# Aim for about this many bins. Fewer hides structure; many more turns a 300px
# wide plot into noise.
_TARGET_BINS = 12

# At most this many labelled ticks on the duration axis, thinned by decade.
_MAX_XTICKS = 6

# Kinds that bracket what ran inside them: a session, a turn, a namespace. Their time
# is their children's. A subagent stays, as a call the agent made and waited for.
_WRAPPERS = ("session", "group", "user_message")

# Left as a gap between neighbouring bars, in user units. Enough to read the bins
# apart, small enough that a 20-bin chart still reads as a distribution.
_BAR_GAP = 1.0

# The most durations one line turns at. Past it a line keeps every n-th, which a plot
# this wide cannot tell apart, so a trace with thousands of spans stays a small page.
_MAX_STEPS = 240

# Round tops for a share axis, in percent.
_SHARE_TOPS = (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100)


@dataclass(frozen=True)
class ChartFilter:
    """Which kinds each chart is not drawing, keyed ``"<chart key>:<kind>"``.

    Per chart, not per trace, and that is the whole distinction from
    :class:`trace.Filter`: that one prunes spans and everything downstream
    follows it, this one only decides what a single chart draws. Nothing here
    ever reaches ``save-trace``, and hiding a kind in one chart leaves the other
    alone — they answer different questions, and a shared toggle would redraw a
    chart the reader never touched.

    Immutable and canonical like its sibling, so one view has one query string.
    Values it cannot parse are dropped rather than raising: this arrives from a
    URL, and anyone can hand-edit one.
    """

    off: tuple = ()

    @classmethod
    def of(cls, values=()) -> "ChartFilter":
        return cls(off=_clean(v for v in values if ":" in str(v)))

    @property
    def active(self) -> bool:
        return bool(self.off)

    @property
    def query(self) -> dict:
        """Arguments for ``url_for``, so a toggled chart is a plain link."""
        return {"chart_off": list(self.off)}

    def hidden(self, key: str) -> frozenset:
        """The kinds switched off on the chart called ``key``."""
        prefix = f"{key}:"
        return frozenset(v[len(prefix):] for v in self.off if v.startswith(prefix))

    def toggled(self, key: str, kind: str) -> "ChartFilter":
        return ChartFilter.of(_toggled(self.off, f"{key}:{kind}"))

    def cleared(self, key: str = "") -> "ChartFilter":
        """Every kind drawn again — on one chart, or on all of them."""
        if not key:
            return ChartFilter()
        prefix = f"{key}:"
        return ChartFilter(off=tuple(v for v in self.off if not v.startswith(prefix)))


@dataclass(frozen=True)
class KindFacet:
    """One legend chip: a kind, how many spans carry it, and whether it is drawn.

    ``n`` counts the kind across the whole view rather than across what the chart
    is currently drawing. A chip that read ``0`` once switched off would be a
    control that hides the reason to click it again.
    """

    kind: str = ""
    n: int = 0
    off: bool = False


@dataclass(frozen=True)
class Sample:
    """One span's contribution: a duration and the kind it is charged to."""

    ns: int
    kind: str


@dataclass(frozen=True)
class Bar:
    """One histogram rectangle, already placed in user units.

    In a stacked chart there is one of these per (bin, kind); ``y`` is the top of
    this segment, not of the bar.
    """

    lo_ns: int
    hi_ns: int
    kind: str = ""              # "" when the chart is not split by kind
    count: int = 0
    ns: int = 0
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0


@dataclass(frozen=True)
class Column:
    """One bin of a histogram: its segments, and what hovering it reads out."""

    lo_ns: int
    hi_ns: int
    x: float
    w: float
    count: int = 0
    ns: int = 0
    share: str = ""             # of summed span time; "" when the bars count spans
    segs: tuple = ()            # Bars, from the baseline up


@dataclass(frozen=True)
class Series:
    """One CDF polyline, ready for an SVG ``points`` attribute."""

    kind: str = ""              # "" for a single pooled series
    label: str = ""
    points: str = ""
    n: int = 0
    area: str = ""              # the same line closed along the baseline
    values: tuple = ()          # its share at each bin edge, 0..1
    lo_ns: int = 0              # the line runs from its shortest span ...
    hi_ns: int = 0              # ... to its longest: nothing happens outside them


@dataclass(frozen=True)
class ReadoutRow:
    kind: str = ""
    label: str = ""
    share: str = ""
    y: float = 0.0
    on_line: bool = True        # the edge is within the series' drawn line


@dataclass(frozen=True)
class Readout:
    """Every series at one bin edge, and the stretch of the plot that shows it."""

    ns: int
    x: float
    left: float
    right: float
    rows: tuple = ()

    @property
    def at(self) -> float:
        """Where the edge sits inside its stretch, as a percentage of it."""
        return round((self.x - self.left) / (self.right - self.left) * 100, 3) if self.right > self.left else 0.0


@dataclass(frozen=True)
class Headline:
    """The chart's one number: a duration (``ns``) or a share (``text``), and what it is."""

    unit: str = ""
    ns: int | None = None
    text: str = ""
    lo_ns: int | None = None    # the bin it names, when it names one
    hi_ns: int | None = None


@dataclass(frozen=True)
class Marker:
    """A reference line at a duration, named beside it."""

    at: float
    ns: int
    word: str = ""


@dataclass(frozen=True)
class Tick:
    """An axis tick. ``ns`` is formatted by the template; ``text`` is not a number."""

    at: float                   # user units along the axis
    ns: int | None = None
    text: str = ""
    anchor: str = "middle"      # SVG text-anchor; the end ticks would overhang


@dataclass(frozen=True)
class Chart:
    key: str = ""
    title: str = ""
    subtitle: str = ""
    note: str = ""
    form: str = "histogram"     # "histogram" | "cdf"
    bars: tuple = ()
    series: tuple = ()
    xticks: tuple = ()
    yticks: tuple = ()
    kinds: tuple = ()           # a KindFacet per kind present, in KIND_ORDER
    columns: tuple = ()
    readouts: tuple = ()
    key_kinds: tuple = ()       # a key to the bars' colours, where the legend is not a control
    head: Headline | None = None
    marker: Marker | None = None

    @property
    def x_step(self) -> int | None:
        """How many times longer each labelled duration is than the one before it."""
        if len(self.xticks) < 2 or not self.xticks[0].ns:
            return None
        return round(self.xticks[1].ns / self.xticks[0].ns)

    @property
    def hidden_kinds(self) -> tuple:
        """Kinds this chart is leaving out — what the "all ×" control undoes."""
        return tuple(f.kind for f in self.kinds if f.off)


@dataclass(frozen=True)
class Charts:
    items: tuple = ()
    total: int = 0              # spans in the view
    plotted: int = 0            # ... with a usable duration
    excluded: int = 0           # ... without one
    summed_ns: int = 0          # sum of plotted durations — NOT wall clock
    edges: tuple = ()
    box: Box = BOX

    @property
    def viewbox(self) -> str:
        """The SVG ``viewBox``: the plot rectangle. Axis labels sit outside it, as text on the page."""
        return f"{self.box.x0:g} {self.box.y0:g} {self.box.w:g} {self.box.h:g}"

    @property
    def has_spans(self) -> bool:
        return bool(self.plotted)

    @property
    def wrapped(self) -> int:
        """Spans left out because they only wrap others (:data:`_WRAPPERS`)."""
        return self.total - self.plotted - self.excluded

    @property
    def note(self) -> str:
        """What the charts leave out, in words, or "" when they leave out nothing."""
        parts = []
        if self.excluded:
            one = self.excluded == 1
            parts.append(
                f"{self.excluded} of {self.total} spans "
                f"{'has' if one else 'have'} no known duration and "
                f"{'is' if one else 'are'} left out — a span with one known instant "
                "is not a zero-length one."
            )
        if self.wrapped:
            parts.append(f"{self.wrapped} of {self.total} only wrap{'s' if self.wrapped == 1 else ''} others and "
                         f"{'is' if self.wrapped == 1 else 'are'} left out.")
        return " ".join(parts)


# The four charts, in grid order: the two time-weighted ones fill the first row,
# the two per-kind ones the second. One definition of the order, the titles and
# the axis wording — in particular "summed span time", which is the phrase that
# keeps these from reading as an accounting of the run.
_SPECS = (
    ("time-hist", "Where the time is", "share of summed span time per duration bin"),
    ("time-cdf", "Time concentration", "share of summed span time in spans no longer than x"),
    ("kind-hist", "How many calls", "calls per duration bin, stacked by kind"),
    ("kind-cdf", "Duration by kind", "share of each kind's spans no longer than x"),
)


def log_edges(lo_ns: int, hi_ns: int, target: int = _TARGET_BINS) -> tuple:
    """Log-spaced bin edges covering ``[lo_ns, hi_ns]``, snapped out to decades.

    Snapping outward is what makes the axis labels read ``1ms``, ``10ms``,
    ``100ms`` instead of arbitrary quantiles, and it guarantees the range encloses
    every sample rather than clipping the extremes.

    Never degenerate. A trace whose spans all took the same time still gets a
    decade of width — the same stance :func:`trace.extent_of` takes when every
    span is an instant, and for the same reason: a zero-width range makes every
    position NaN.
    """
    lo = max(int(lo_ns), 1)
    hi = max(int(hi_ns), lo)
    low_dec = int(math.floor(math.log10(lo)))
    high_dec = int(math.ceil(math.log10(hi)))
    if high_dec <= low_dec:
        # One value, or a range inside a single decade boundary: widen rather
        # than emit a single edge.
        high_dec = low_dec + 1
    decades = high_dec - low_dec

    # Pick the subdivision landing nearest the target bin count. Ties go to the
    # finer one, since `_SUBDIVISIONS` is ordered and `min` is stable.
    steps = min(_SUBDIVISIONS, key=lambda split: abs(decades * len(split) - target))
    edges = [round(step * 10 ** decade) for decade in range(low_dec, high_dec) for step in steps]
    # Sorted and once each: at nanoseconds, 1 and 1.2 are one whole number.
    return tuple(sorted({max(int(edge), 1) for edge in edges} | {10 ** high_dec}))


def bin_index(value: int, edges: tuple) -> int:
    """Which bin ``value`` falls in — the last bin is closed on the right.

    Without that, the single longest span in the trace falls off the end of the
    axis it defined. Values outside the range clamp instead of raising: the edges
    are built to enclose the data, so an out-of-range value means a caller mixed
    two sample sets, and losing it silently would be worse than showing it at the
    edge.
    """
    if len(edges) < 2:
        return 0
    last = len(edges) - 2
    for i in range(last + 1):
        if value < edges[i + 1]:
            return max(i, 0)
    return last


def _x(value: float, edges: tuple, box: Box = BOX) -> float:
    """Position a duration on the log axis, in user units."""
    lo, hi = math.log10(max(edges[0], 1)), math.log10(max(edges[-1], 1))
    if hi <= lo:
        return box.x0
    frac = (math.log10(max(value, 1)) - lo) / (hi - lo)
    return round(box.x0 + max(0.0, min(1.0, frac)) * box.w, 2)


def _y(frac: float, box: Box = BOX) -> float:
    """A 0..1 share, measured up from the baseline, as a user-unit y."""
    return round(box.y1 - max(0.0, min(1.0, frac)) * box.h, 2)


def _kinds_present(samples: list) -> tuple:
    """Kinds in the samples, in legend order, unknown enum values sorted after."""
    seen = {s.kind for s in samples}
    known = [k for k in KIND_ORDER if k in seen]
    return tuple(known + sorted(seen - set(KIND_ORDER)))


def _xticks(edges: tuple, box: Box = BOX) -> tuple:
    """Ticks at decade boundaries, thinned so at most `_MAX_XTICKS` are labelled."""
    decades = [
        10 ** d
        for d in range(
            int(math.floor(math.log10(max(edges[0], 1)))),
            int(math.floor(math.log10(max(edges[-1], 1)))) + 1,
        )
        if edges[0] <= 10 ** d <= edges[-1]
    ]
    if not decades:
        decades = [edges[0], edges[-1]]
    stride = max(1, math.ceil(len(decades) / _MAX_XTICKS))
    shown = decades[::stride]
    return tuple(
        Tick(
            at=_x(value, edges, box),
            ns=int(value),
            # A centred label on the first or last tick hangs outside the
            # viewBox and gets clipped, which reads as a truncated number.
            anchor="start" if i == 0 else ("end" if i == len(shown) - 1 else "middle"),
        )
        for i, value in enumerate(shown)
    )


def _share_yticks(box: Box = BOX) -> tuple:
    """0/50/100% — for a cumulative axis, where the top really is everything."""
    return tuple(
        Tick(at=_y(frac, box), text=f"{int(frac * 100)}%")
        for frac in (0.0, 0.5, 1.0)
    )


def _pct(value: float) -> str:
    """A share, at the precision the number deserves. Never a bare 0% for real time."""
    if value <= 0:
        return "0%"
    if value < 0.1:
        return "<0.1%"
    return f"{value:.1f}%" if value < 10 else f"{value:.0f}%"


def _share_top(fullest: int, total: int) -> float:
    """The round percentage a share axis tops out at: the fullest bin's, rounded up."""
    share = fullest / total * 100 if total > 0 else 0.0
    return next((top for top in _SHARE_TOPS if share <= top), 100)


def _scaled_yticks(top_pct: float, box: Box = BOX) -> tuple:
    """Ticks for a chart whose bars are real shares on an axis of ``top_pct``.

    Tall enough to read the shape, and round: a bar holding 14% of the time stands
    at 14 on an axis to 20, so the top gridline is a value on the scale and never
    "the tallest bar", and "100%" there would mean what it says.
    """
    return tuple(
        Tick(at=_y(frac, box), text=_pct(top_pct * frac))
        for frac in (0.0, 0.5, 1.0)
    )


def _count_yticks(top: int, box: Box = BOX) -> tuple:
    """0 and the tallest bin. Integers only — half a span is not a thing."""
    if top <= 1:
        return (Tick(at=_y(0.0, box), text="0"), Tick(at=_y(1.0, box), text=str(max(top, 1))))
    mid = top // 2
    ticks = [Tick(at=_y(0.0, box), text="0")]
    if mid:
        ticks.append(Tick(at=_y(mid / top, box), text=str(mid)))
    ticks.append(Tick(at=_y(1.0, box), text=str(top)))
    return tuple(ticks)


def histogram(samples: list, edges: tuple, weight: str = "count",
              split: bool = False, box: Box = BOX, ceiling: float = 0.0) -> tuple:
    """Bars for ``samples``, binned on ``edges``.

    ``weight`` is ``"count"`` (bar height is a number of spans) or ``"time"``
    (summed nanoseconds). ``split`` stacks one segment per kind within each bin.

    Heights are shares of ``ceiling``, by default the tallest bin, so an empty bin
    is a visible gap rather than a bar of some minimum height — the distinction
    between "nothing here" and "a little here" is most of what a histogram is for.
    """
    n = max(len(edges) - 1, 0)
    if not n or not samples:
        return ()

    counts = [0] * n
    times = [0] * n
    per_kind: list = [{} for _ in range(n)]
    for sample in samples:
        i = bin_index(sample.ns, edges)
        counts[i] += 1
        times[i] += sample.ns
        cell = per_kind[i].setdefault(sample.kind, [0, 0])
        cell[0] += 1
        cell[1] += sample.ns

    totals = times if weight == "time" else counts
    top = ceiling or max(totals) or 1

    bars = []
    for i in range(n):
        lo, hi = edges[i], edges[i + 1]
        x0, x1 = _x(lo, edges, box), _x(hi, edges, box)
        # The gap comes out of the bar, never off the axis: a bin's left edge is
        # where its bin starts, or the bars stop lining up with the ticks.
        width = max(x1 - x0 - _BAR_GAP, 0.5)
        if not totals[i]:
            continue
        if not split:
            frac = totals[i] / top
            y = _y(frac, box)
            bars.append(Bar(lo_ns=lo, hi_ns=hi, count=counts[i], ns=times[i],
                            x=x0, y=y, w=round(width, 2), h=round(box.y1 - y, 2)))
            continue
        # Stacked: walk the kinds in legend order so the colours do not reshuffle
        # between bins, accumulating from the baseline upward.
        below = 0.0
        for kind in _kinds_present([Sample(ns=0, kind=k) for k in per_kind[i]]):
            count, ns = per_kind[i][kind]
            value = ns if weight == "time" else count
            if not value:
                continue
            top_frac = below + value / top
            y = _y(top_frac, box)
            bars.append(Bar(lo_ns=lo, hi_ns=hi, kind=kind, count=count, ns=ns,
                            x=x0, y=y, w=round(width, 2),
                            h=round(_y(below, box) - y, 2)))
            below = top_frac
    return tuple(bars)


def _columns(bars: tuple, summed_ns: int = 0) -> tuple:
    """The bars gathered into their bins. ``summed_ns``: the bars weigh time, so each
    says its share of it."""
    bins: dict = {}
    for bar in bars:
        bins.setdefault(bar.lo_ns, []).append(bar)
    out = []
    for segs in bins.values():
        first = segs[0]
        ns = sum(b.ns for b in segs)
        out.append(Column(lo_ns=first.lo_ns, hi_ns=first.hi_ns, x=first.x, w=first.w, count=sum(b.count for b in segs), ns=ns,
                          share=_pct(ns / summed_ns * 100) if summed_ns else "", segs=tuple(segs)))
    return tuple(out)


def _readouts(series: tuple, edges: tuple, box: Box = BOX) -> tuple:
    """Every series at each edge, the plot split halfway between edges so the pointer need not land on a line."""
    if not series:
        return ()
    xs = [_x(edge, edges, box) for edge in edges]
    out = []
    for i, edge in enumerate(edges):
        left = box.x0 if i == 0 else (xs[i - 1] + xs[i]) / 2
        right = box.x1 if i == len(edges) - 1 else (xs[i] + xs[i + 1]) / 2
        rows = tuple(ReadoutRow(kind=s.kind, label=s.label, share=_pct(s.values[i] * 100), y=_y(s.values[i], box),
                                on_line=s.lo_ns <= edge <= s.hi_ns) for s in series)
        out.append(Readout(ns=int(edge), x=xs[i], left=round(left, 2), right=round(right, 2), rows=rows))
    return tuple(out)


def _median(samples: tuple) -> int:
    """The middle span's duration (the lower one of an even count): one a span really had."""
    ordered = sorted(s.ns for s in samples)
    return ordered[(len(ordered) - 1) // 2]


def _half_time(samples: tuple) -> int:
    """The shortest duration such that spans no longer than it hold half the summed time."""
    half, held = sum(s.ns for s in samples) / 2, 0
    for ns in sorted(s.ns for s in samples):
        held += ns
        if held >= half:
            return ns
    return 0


def _slowest_kind(samples: tuple) -> tuple:
    """(kind, its median) for the kind whose middle span runs longest."""
    grouped: dict = {}
    for sample in samples:
        grouped.setdefault(sample.kind, []).append(sample)
    medians = {kind: _median(tuple(group)) for kind, group in grouped.items()}
    kind = max(_kinds_present(samples), key=lambda k: medians[k])
    return kind, medians[kind]


def _steps(ordered: list, total: float, edges: tuple, box: Box = BOX) -> str:
    """The real curve, as an SVG ``points`` string: flat, then straight up at each
    duration a span really took. A slope between two durations would be a share no
    span ever had. It runs from the shortest span to the longest and stops: a line
    along the axis on either side would be the loudest thing on a plot saying nothing.
    """
    turns: dict = {}
    held = 0.0
    for ns, weight in ordered:
        held += weight
        turns[ns] = held
    every = max(1, math.ceil(len(turns) / _MAX_STEPS))
    kept = [(ns, turns[ns]) for i, ns in enumerate(turns) if i % every == 0 or i == len(turns) - 1]
    points = [f"{_x(kept[0][0], edges, box)},{_y(0.0, box)}"]
    below = 0.0
    for ns, reached in kept:
        x = _x(ns, edges, box)
        points += [f"{x},{_y(below / total, box)}", f"{x},{_y(reached / total, box)}"]
        below = reached
    return " ".join(points)


def _cdf_series(pairs: list, total: float, edges: tuple, box: Box = BOX, **fields) -> Series | None:
    """One CDF as a :class:`Series`: its line through every span, and its share at
    each bin edge, where the readout asks. ``pairs`` is [(ns, weight)]."""
    if not pairs or total <= 0:
        return None
    ordered = sorted(pairs)
    values = []
    cumulative = 0.0
    i = 0
    for edge in edges:
        while i < len(ordered) and ordered[i][0] <= edge:
            cumulative += ordered[i][1]
            i += 1
        values.append(cumulative / total)
    points = _steps(ordered, total, edges, box)
    lo_ns, hi_ns = ordered[0][0], ordered[-1][0]
    return Series(points=points, area=f"{points} {_x(hi_ns, edges, box)},{_y(0.0, box)}", values=tuple(values), lo_ns=lo_ns, hi_ns=hi_ns, **fields)


def cdf(samples: list, edges: tuple, weight: str = "count", box: Box = BOX) -> tuple:
    """One pooled CDF series over every sample."""
    if not samples:
        return ()
    pairs = [(s.ns, float(s.ns) if weight == "time" else 1.0) for s in samples]
    series = _cdf_series(pairs, sum(w for _, w in pairs), edges, box, label="all spans", n=len(samples))
    return (series,) if series else ()


def cdf_by_kind(samples: list, edges: tuple, box: Box = BOX) -> tuple:
    """One CDF per kind, each normalised **within its kind**.

    Normalising within the kind is what makes the lines comparable: every one
    reaches 100%, so their horizontal separation is the difference in how long
    that kind's calls run, not in how many of them there were. The counts are on
    the legend, because that information has to survive somewhere.
    """
    grouped: dict = {}
    for sample in samples:
        grouped.setdefault(sample.kind, []).append(sample)
    out = []
    for kind in _kinds_present(samples):
        group = grouped.get(kind) or []
        series = _cdf_series([(s.ns, 1.0) for s in group], float(len(group)), edges, box, kind=kind, label=kind, n=len(group))
        if series:
            out.append(series)
    return tuple(out)


def _visible(samples: tuple, off: frozenset) -> tuple:
    """The samples a chart still draws, given the kinds switched off on it."""
    return tuple(s for s in samples if s.kind not in off)


def _facets(samples: tuple, off: frozenset) -> tuple:
    """One legend chip per kind in the view, counted over all of it."""
    counts: dict = {}
    for sample in samples:
        counts[sample.kind] = counts.get(sample.kind, 0) + 1
    return tuple(
        KindFacet(kind=kind, n=counts.get(kind, 0), off=kind in off)
        for kind in _kinds_present(samples)
    )


def _edges_for(samples: tuple, fallback: tuple) -> tuple:
    """Bins for a subset of the samples — the point of the legend toggle.

    A chart drawing only tool calls wants the axis those calls need, not the one
    the LLM calls defined before they were switched off. With nothing left to
    bin it falls back to the whole range, so the empty plot still has an axis
    rather than a degenerate one.
    """
    if not samples:
        return fallback
    return log_edges(min(s.ns for s in samples), max(s.ns for s in samples))


def _kind_note(off: frozenset, visible: tuple) -> str:
    """What the legend has done to this chart, said underneath it; "" when nothing."""
    if not visible:
        return "Every kind is switched off — click a chip above to draw one again."
    if not off:
        return ""
    names = ", ".join(sorted(off))
    return (f"{names} switched off; the axes are rescaled to the rest, so "
            "this one no longer shares its duration axis with the others.")


def samples_of(roots: list) -> tuple:
    """(samples, total, excluded) over the spans in ``roots``.

    ``roots`` is the *filtered* tree — `trace.parse` prunes before building it —
    and ``all_spans`` walks all of it, not the `MAX_ROWS`-capped row list. A
    chart built off the truncated set would describe a subset of the trace while
    claiming to describe the view.
    """
    samples = []
    total = 0
    excluded = 0
    for span in all_spans(roots):
        total += 1
        if is_instant(span):
            excluded += 1
            continue
        if span.kind not in _WRAPPERS:
            samples.append(Sample(ns=int(span.duration_ns), kind=span.kind))
    return tuple(samples), total, excluded


def charts_for(roots: list, filt: "ChartFilter | None" = None) -> Charts:
    """The four distribution charts over a filtered trace's spans."""
    return charts_of(*samples_of(roots), filt=filt)


def charts_of(samples: tuple, total: int, excluded: int,
              filt: "ChartFilter | None" = None) -> Charts:
    """The same four charts over an already-extracted population.

    Split out from :func:`charts_for` so that several traces could be pooled
    without merging their span *trees*. :func:`trace.all_spans` deduplicates by
    span id to guard against a parent cycle — right within one document, and
    quietly wrong across several, where two traces reusing an id would lose the
    duplicates without saying so. Extract each trace's samples on its own, then
    concatenate them here; ids never meet.

    Everything downstream of this point only ever sees durations and kinds, which
    is the honest reading of what these charts describe.

    ``filt`` is the legend rather than a filter on the trace: it says which kinds
    the per-kind charts draw, and those charts rescale to what is left.
    """
    samples = tuple(Sample(ns=s.ns, kind=s.kind or "unknown") for s in samples)
    filt = filt or ChartFilter()
    if not samples:
        return Charts(
            items=tuple(Chart(key=k, title=t, subtitle=s) for k, t, s in _SPECS),
            total=total, excluded=excluded,
        )

    edges = log_edges(min(s.ns for s in samples), max(s.ns for s in samples))
    summed = sum(s.ns for s in samples)
    xticks = _xticks(edges)
    shares = _share_yticks()   # cumulative charts only

    # Coloured by kind, as the other per-kind charts and the waterfall are, on one round axis.
    fullest_bin = max((b.ns for b in histogram(samples, edges, weight="time")), default=0)
    top_pct = _share_top(fullest_bin, summed)
    time_bars = histogram(samples, edges, weight="time", split=True, ceiling=summed * top_pct / 100)
    time_columns = _columns(time_bars, summed)
    fullest = max(time_columns, key=lambda c: c.ns, default=None)
    half_ns = _half_time(samples)

    # Each per-kind chart carries its own legend state and its own bins. The two
    # ask different questions, so switching a kind off in one must leave the
    # other exactly as it was.
    hist_off, cdf_off = filt.hidden("kind-hist"), filt.hidden("kind-cdf")
    hist_kinds, cdf_kinds = _visible(samples, hist_off), _visible(samples, cdf_off)
    hist_edges, cdf_edges = _edges_for(hist_kinds, edges), _edges_for(cdf_kinds, edges)
    kind_bars = histogram(hist_kinds, hist_edges, weight="count", split=True)
    tallest = max(
        (b.count for b in histogram(hist_kinds, hist_edges, weight="count")), default=0
    )

    specs = dict(zip((s[0] for s in _SPECS), _SPECS))
    time_series, kind_series = cdf(samples, edges, weight="time"), cdf_by_kind(cdf_kinds, cdf_edges)
    slowest = _slowest_kind(cdf_kinds) if cdf_kinds else None
    built = {
        "time-hist": Chart(
            form="histogram", bars=time_bars, xticks=xticks,
            yticks=_scaled_yticks(top_pct),
            columns=time_columns,
            key_kinds=_facets(samples, frozenset()),
            head=Headline(text=fullest.share, unit="in spans of", lo_ns=fullest.lo_ns, hi_ns=fullest.hi_ns) if fullest else None,
        ),
        "time-cdf": Chart(
            form="cdf", series=time_series,
            xticks=xticks, yticks=shares, readouts=_readouts(time_series, edges),
            head=Headline(ns=half_ns, unit="or shorter holds half the span time"),
            marker=Marker(at=_x(half_ns, edges), ns=half_ns, word="half"),
        ),
        # Ticks and gridlines come from the *drawn* samples, not the whole set —
        # an axis left at the unfiltered range is the bug the toggle exists to
        # avoid. With nothing drawn there are no value labels either: a "0/1"
        # count axis over an empty plot claims a measurement nobody made.
        "kind-hist": Chart(
            form="histogram", bars=kind_bars, xticks=_xticks(hist_edges),
            yticks=_count_yticks(tallest) if hist_kinds else (),
            kinds=_facets(samples, hist_off),
            columns=_columns(kind_bars),
            head=Headline(text=str(len(hist_kinds)), unit="call" if len(hist_kinds) == 1 else "calls") if hist_kinds else None,
            note=_kind_note(hist_off, hist_kinds),
        ),
        "kind-cdf": Chart(
            form="cdf", series=kind_series,
            xticks=_xticks(cdf_edges), yticks=shares if cdf_kinds else (),
            kinds=_facets(samples, cdf_off),
            readouts=_readouts(kind_series, cdf_edges),
            head=Headline(ns=slowest[1], unit=f"median {slowest[0]}" + (", the slowest kind" if len(kind_series) > 1 else "")) if slowest else None,
            note=_kind_note(cdf_off, cdf_kinds),
        ),
    }
    items = tuple(replace(built[key], key=key, title=specs[key][1], subtitle=specs[key][2]) for key, _, _ in _SPECS)
    return Charts(items=items, total=total, plotted=len(samples), excluded=excluded,
                  summed_ns=summed, edges=edges)
