"""Where a trace's time went: the producer's totals, and the same accounting done here.

``Totals`` is shown verbatim; ``Breakdown`` is derived, and never overwrites it.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from .spans import _int_or_none, merge_intervals, NS_PER_MS, raw_spans_of

# A residual under this reads as float noise from the producer's own arithmetic,
# not as real unattributed time. Rendering it produces a zero-width segment with
# a legend entry saying "0ms".
UNACCOUNTED_EPSILON_NS = NS_PER_MS

# Breakdown segments, in a fixed left-to-right order. The order carries no
# meaning — these are categories, not a timeline — but a stable one keeps the
# strip from reshuffling between trials.
_SEGMENTS = (
    ("llm", "LLM", "llm_ns"),
    ("tool", "Tool", "tool_ns"),
    ("reasoning", "Reasoning", "reasoning_ns"),
    ("idle", "Idle", "idle_ns"),
)

# The residual is a segment like any other once it reaches a strip, and both
# `segments_of` and `Breakdown.compared_to` need to name and order it.
_UNACCOUNTED = ("unaccounted", "Unaccounted")
_SEGMENT_LABELS = {key: label for key, label, _ in _SEGMENTS} | {_UNACCOUNTED[0]: _UNACCOUNTED[1]}
_SEGMENT_ORDER = [key for key, _, _ in _SEGMENTS] + [_UNACCOUNTED[0]]

# What a span's own time counts toward when a breakdown has to be recomputed for
# a filtered view. The structural kinds are absent on purpose: `session`,
# `subagent` and `group` bracket their children, so charging them would claim the
# children's work as their own, and `output` is text produced inside a call that
# is already counted as `llm`.
_KIND_CATEGORY = {"llm_request": "llm", "reasoning": "reasoning", "tool": "tool"}

# Priority for `partition`. A tool that only waits for a human beats real work,
# and both beat the LLM call they happened inside of.
_PRIORITY = ("idle", "tool", "reasoning", "llm")

# A tool whose whole job is to wait for the human. Harbor opens a span with this
# name; another producer may set an attribute instead. Either way the time
# is not the agent working, so it lands in `idle` rather than `tool`.
_BLOCKED_NAME = "claude_code.tool.blocked_on_user"
_BLOCKED_ATTR = "blocked_on_user"


def segments_of(source) -> list:
    """[{key, label, ns, pct}] for a stacked strip, zero-length categories dropped.

    Shared by :class:`Totals` and :class:`Breakdown` so the reported strip and a
    recomputed one are laid out and labelled identically.

    Percentages are of the wall clock, so categories that do not close read as
    short of 100% instead of being silently rescaled to fill it; ``unaccounted``
    is what closes them. With no wall clock to divide by they fall back to the
    segments' own sum.
    """
    out = [
        {"key": key, "label": label, "ns": int(getattr(source, attr))}
        for key, label, attr in _SEGMENTS
        if getattr(source, attr)
    ]
    if source.unaccounted_ns:
        out.append({"key": _UNACCOUNTED[0], "label": _UNACCOUNTED[1], "ns": source.unaccounted_ns})
    denominator = source.wall_ns or sum(seg["ns"] for seg in out)
    for seg in out:
        seg["pct"] = seg["ns"] / denominator * 100 if denominator else 0.0
    return out


@dataclass
class Totals:
    """Run-level time, exactly as the producer reported it.

    **Never recomputed from the tree.** Summing nested spans double-counts
    their shared time. The producer already resolved that (it skips tools nested
    inside tools, and subtracts tool time nested inside LLM spans). The only
    arithmetic this class does is the residual, which is a display concern.
    """

    wall_ns: int | None = None
    llm_ns: int | None = None
    tool_ns: int | None = None
    reasoning_ns: int | None = None
    idle_ns: int | None = None
    span_count: int | None = None
    measured_span_count: int | None = None

    @property
    def unaccounted_ns(self) -> int:
        """Wall clock the categories don't account for.

        The categories are not expected to close: the session span brackets more
        than its instrumented children, so a stacked bar that just leaves a gap
        reads as a rendering bug. Name the gap instead. Clamped at both ends —
        under an epsilon it is noise, and a negative would corrupt the geometry
        silently.
        """
        if not self.wall_ns:
            return 0
        rest = sum(v or 0 for v in (self.llm_ns, self.tool_ns, self.reasoning_ns, self.idle_ns))
        residual = self.wall_ns - rest
        return int(residual) if residual >= UNACCOUNTED_EPSILON_NS else 0

    @property
    def segments(self) -> list:
        """[{key, label, ns, pct}] for the breakdown strip."""
        return segments_of(self)


@dataclass
class Breakdown:
    """Category time recomputed from a set of spans — for a filtered view only.

    The producer's ``totals`` stays authoritative and is never replaced by this
    (see :class:`Totals`).

    It is deliberately *not* the same arithmetic, and it does not reproduce the
    same numbers. Harbor's ``llm_ns`` is the plain sum of its LLM spans, while
    this partitions the timeline so an instant covered by both a tool call and
    the LLM call that asked for it is charged once, to the tool.

    ``wall_ns`` is carried over from the source unchanged: hiding a span does not
    make the run shorter, it makes more of the run unattributed. That is what
    ``unaccounted_ns`` then absorbs.
    """

    wall_ns: int = 0
    llm_ns: int = 0
    tool_ns: int = 0
    reasoning_ns: int = 0
    idle_ns: int = 0
    span_count: int = 0
    measured_span_count: int = 0
    tokens: dict = field(default_factory=dict)

    @property
    def attributed_ns(self) -> int:
        """Wall clock the categories account for. Disjoint, so a plain sum."""
        return self.llm_ns + self.tool_ns + self.reasoning_ns + self.idle_ns

    @property
    def unaccounted_ns(self) -> int:
        residual = (self.wall_ns or 0) - self.attributed_ns
        return int(residual) if residual >= UNACCOUNTED_EPSILON_NS else 0

    @property
    def segments(self) -> list:
        return segments_of(self)

    def compared_to(self, other: "Breakdown") -> list:
        """`segments`, each carrying `delta_ns` against *other*.

        Both sides come from this
        same partition, so every delta is the filter's doing and nothing else —
        whereas the difference against the producer's reported strip mixes the
        filter up with two different ways of accounting for the same run, and on
        a log-derived trace those disagree by minutes, not milliseconds.

        A category the filter emptied keeps its entry at 0ns: dropping it would
        hide the largest thing the filter did.
        """
        was = {seg["key"]: seg["ns"] for seg in other.segments}
        out = self.segments
        here = {seg["key"] for seg in out}
        out.extend(
            {"key": key, "label": _SEGMENT_LABELS[key], "ns": 0, "pct": 0.0}
            for key in was
            if key not in here
        )
        out.sort(key=lambda seg: _SEGMENT_ORDER.index(seg["key"]))
        for seg in out:
            seg["delta_ns"] = seg["ns"] - was.get(seg["key"], 0)
        return out

    def as_totals(self) -> dict:
        """The ``totals`` block to write into a saved filtered trace."""
        return {
            "wall_ns": self.wall_ns,
            "llm_ns": self.llm_ns,
            "tool_ns": self.tool_ns,
            "reasoning_ns": self.reasoning_ns,
            "idle_ns": self.idle_ns,
            "span_count": self.span_count,
            "measured_span_count": self.measured_span_count,
            "tokens": dict(self.tokens),
        }


def category_of(raw: dict) -> str | None:
    """Which breakdown category a span's own time belongs to, or None.

    None means the span contributes no time of its own — see `_KIND_CATEGORY`.
    """
    kind = str(raw.get("kind") or "")
    if kind == "tool" and (
        str(raw.get("name") or "") == _BLOCKED_NAME
        or (raw.get("attributes") or {}).get(_BLOCKED_ATTR)
    ):
        return "idle"
    return _KIND_CATEGORY.get(kind)


def partition(by_category: dict, priority: tuple = _PRIORITY) -> dict:
    """Split the timeline so every instant is charged to exactly one category.

    Summing durations exceeds the wall clock as soon as spans nest or overlap,
    and both happen here: a tool call sits inside the LLM call that asked for it,
    and background subagents run alongside the main thread. Each elementary
    segment goes to the highest-priority category covering it, which keeps the
    categories disjoint by construction and the stacked strip honest.
    """
    merged = {cat: merge_intervals(by_category.get(cat) or []) for cat in priority}
    starts = {cat: [start for start, _ in ivs] for cat, ivs in merged.items()}
    edges = sorted({point for ivs in merged.values() for iv in ivs for point in iv})
    totals = {cat: 0 for cat in priority}

    def covers(cat: str, low: int) -> bool:
        ivs = merged[cat]
        i = bisect.bisect_right(starts[cat], low) - 1
        return i >= 0 and ivs[i][1] > low

    for low, high in zip(edges, edges[1:]):
        for cat in priority:
            if covers(cat, low):
                totals[cat] += high - low
                break
    return totals


def breakdown(spans: list, wall_ns: int = 0) -> Breakdown:
    """Recompute category time, span counts and tokens over *spans*.

    See :class:`Breakdown` for why this is not the producer's arithmetic and must
    not be presented as it. Spans with no usable end contribute no time — an
    unknown clock is not a zero one — but they are still counted and their tokens
    still add up.
    """
    by_category: dict = {}
    tokens: dict = {}
    count = 0
    measured = 0
    for raw in raw_spans_of(spans):
        count += 1
        if str(raw.get("timing_quality") or "") == "measured":
            measured += 1
        for key, value in (raw.get("tokens") or {}).items():
            if _int_or_none(value) is not None:
                tokens[key] = tokens.get(key, 0) + value
        category = category_of(raw)
        start = _int_or_none(raw.get("start_ns"))
        end = _int_or_none(raw.get("end_ns"))
        if category and start is not None and end is not None and end > start:
            by_category.setdefault(category, []).append((start, end))
    split = partition(by_category)
    return Breakdown(
        wall_ns=int(wall_ns or 0),
        llm_ns=split.get("llm", 0),
        tool_ns=split.get("tool", 0),
        reasoning_ns=split.get("reasoning", 0),
        idle_ns=split.get("idle", 0),
        span_count=count,
        measured_span_count=measured,
        tokens=tokens,
    )


def _totals(raw: dict) -> Totals:
    if not raw:
        return Totals()
    return Totals(
        wall_ns=_int_or_none(raw.get("wall_ns")),
        llm_ns=_int_or_none(raw.get("llm_ns")),
        tool_ns=_int_or_none(raw.get("tool_ns")),
        reasoning_ns=_int_or_none(raw.get("reasoning_ns")),
        idle_ns=_int_or_none(raw.get("idle_ns")),
        span_count=_int_or_none(raw.get("span_count")),
        measured_span_count=_int_or_none(raw.get("measured_span_count")),
    )
