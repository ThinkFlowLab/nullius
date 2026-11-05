"""Load an agent trace (``agent/trace.json``) into rows for a waterfall.

A sidecar to the trajectory, not a replacement: where ``trajectory.json`` carries
one timestamp per step with no duration, a trace carries measured start *and*
end per LLM call, tool call and subagent, with causal parentage. It is the first
artefact in this tree that can say where the wall clock actually went.

Two rules shape everything here.

**One global time axis.** Bars are positioned against the whole trace, never
against their parent. Log-derived children routinely start *before* their OTel
parents, because the two come from different clocks; laying out relative to the
parent would force a clamp, and the clamp would hide exactly the discrepancy the
format exists to expose. A *zoom* (``Filter.zoom``) is not a per-parent layout:
it is one explicit window — a subtree's own extent — that the reader asks for,
and everything outside it is left out of the waterfall rather than re-scaled.

**Totals are the producer's job.** ``totals`` is displayed verbatim and never
recomputed from the tree — see :class:`Totals`. A filtered view is the one case
that needs its own arithmetic, because hiding a span has to move its time
somewhere and the producer cannot be asked again; that lives in
:class:`Breakdown`, is labelled as derived wherever it is shown, and never
overwrites what the file said.

Two normalisations happen on the way in, in that order, and they are the only
two. First, a call the exporter measured is given the log spans for the reply it
produced, which the producer writes as a second copy of the same call when it has
no request id to join them on. See :func:`fold_llm_requests`; it moves rows and
no numbers. Then a span that is a *namespace* for its children rather than an
event of its own is re-kinded ``group`` and reads ``Bash [group]``: Harbor's
Claude Code instrumentation opens a ``claude_code.tool`` span carrying
``tool_name: "Bash"`` and puts the permission wait and the actual ``Bash``
execution inside it. Counting the wrapper as work would double-count the call.
See :func:`is_group` for what qualifies and, more importantly, for the near
misses that deliberately do not.

The order is load-bearing. An ``llm_request`` holding a single ``reasoning`` or
``output`` block of byte-identical extent is the same
duplication :func:`fold_llm_requests` resolves, and it resolves it better: the
wrapper and its one block collapse into a single span carrying both the
request's tokens and the block's text, where re-kinding leaves two rows and
charges one of them nothing. Folding first leaves ``is_group`` only the wrappers
fold could not match to a call, which is the case it is still needed for.

Everything is redacted at load, so templates hold display text only. Stdlib
only, and no Flask: ``python3 -m unittest discover -s app/tests -t app`` needs no
install at all.
"""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field

from nullius.redact import redact

NS_PER_MS = 1_000_000

# A residual under this reads as float noise from the producer's own arithmetic,
# not as real unattributed time. Rendering it produces a zero-width segment with
# a legend entry saying "0ms".
UNACCOUNTED_EPSILON_NS = NS_PER_MS

# An exporter can emit thousands of spans for a trivial task. Every row is
# rendered server-side with its own detail pane, so the page has to be bounded.
MAX_ROWS = 2000

# Nesting past this is a malformed producer, not a deep agent. JSON cannot
# express a cycle, so `load` can never hit it — but `parse` takes a dict, and
# either way a runaway tree would exhaust the stack while building the spans,
# long before any of the guards downstream got a look.
MAX_DEPTH = 64

# Legend order. Not the enum's order — this groups the model's own production
# (llm/reasoning/output) together, then work, then structure.
KIND_ORDER = (
    "llm_request", "reasoning", "output", "tool", "subagent",
    "session", "user_message", "compaction", "group",
)

# What a namespace span becomes. A kind of its own rather than a flag, because
# every consumer already switches on `kind` — the swatch, the legend, the chart
# split, and `_KIND_CATEGORY` below, which by omitting it charges a group no time.
GROUP_KIND = "group"

# Said in the label, because the label is the only thing most of the page shows.
# Appended rather than substituted: "Bash [group]" and "Bash" sort together and
# read as the same call seen at two levels, which is what they are.
GROUP_SUFFIX = " [group]"

# How far a namespace span's children may fall short of covering it and still
# count as accounting for all of it.
#
# They never tile it exactly. Harbor stamps the wrapper to the millisecond and
# its children to the nanosecond, so each of the two boundaries is up to 1ms out,
# and children routinely overrun the parent they sit in — the same clock
# disagreement the global axis exists to show. Allow both boundaries' rounding.
GROUP_SLACK_NS = 2 * NS_PER_MS

# Kinds that already say they bracket their children. They are exempt from the
# normalisation below, which exists to stop a span *claiming to be work* when it
# is a container — a kind that never made the claim needs no correcting, and
# `_KIND_CATEGORY` already charges these nothing.
_BRACKET_KINDS = ("session", "subagent", GROUP_KIND)

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

# The OTel span for one API call, and the log-derived span for one *record* of
# the reply. Harbor merges the two itself when the exporter reports a request id
# (recorded in `harbor.merged_from`); when the provider returns none
# it cannot, and the same call is left drawn twice — once under the interaction,
# once under the parallel `session` subtree. `fold_llm_requests` finishes the job.
_OTEL_LLM_NAME = "claude_code.llm_request"
_LOG_LLM_NAME = "llm_request"

# What a call can be shown to have produced. A tool_use block is left untyped by
# the producer, and says nothing about what the call was for.
_BLOCK_KINDS = ("reasoning", "output")

# Provenance for a span this reader assembled, alongside the producer's own
# `harbor.merged_from`. The `app.` prefix is the whole point: it marks what was
# added here from what was measured there.
_MERGED_FROM = "app.merged_from"
_MERGED_KEYS = ("harbor.merged_from", _MERGED_FROM)

# Content fields, in reading order. `arguments` is a mapping; the rest are text.
_CONTENT_FIELDS = (
    ("input", "Input"),
    ("reasoning", "Reasoning"),
    ("output", "Output"),
    ("arguments", "Arguments"),
    ("result", "Result"),
)


def segments_of(source) -> list:
    """[{key, label, ns, pct}] for a stacked strip, zero-length categories dropped.

    Shared by :class:`Totals` and :class:`Breakdown` so the reported strip and a
    recomputed one are laid out and labelled identically — the only difference
    between them should be the numbers, never the presentation.

    Percentages are of the wall clock, so categories that do not close read as
    short of 100% instead of being silently rescaled to fill it; ``unaccounted``
    is what closes them. With no wall clock to divide by they fall back to the
    segments' own sum, which is the only honest denominator left.
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
        silently (the same reasoning as `Trial.dur_other`).
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
    (see :class:`Totals`). It exists because hiding spans has to take their time
    out of the picture, and the producer cannot be asked a second time.

    It is deliberately *not* the same arithmetic, and it does not reproduce the
    same numbers. Harbor's ``llm_ns`` is the plain sum of its LLM spans, while
    this partitions the timeline so an instant covered by both a tool call and
    the LLM call that asked for it is charged once, to the tool. The two are
    shown as separate strips because they measure different things.

    ``wall_ns`` is carried over from the source unchanged: hiding a span does not
    make the run shorter, it makes more of the run unattributed. That is what
    ``unaccounted_ns`` then absorbs, and it is the honest place for it to land.
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

        The comparison a filtered view actually needs. Both sides come from this
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


def clean(values) -> tuple:
    """Deduplicated, sorted, blank-free — so one view has exactly one URL."""
    return tuple(sorted({str(v) for v in values if str(v).strip()}))


def toggled(values: tuple, value: str) -> tuple:
    value = str(value)
    return tuple(v for v in values if v != value) if value in values else values + (value,)


@dataclass(frozen=True)
class Filter:
    """What a view is leaving out: span labels, and individual spans by id.

    Two axes, because they answer different questions — "never mind Bash" is a
    label, "not *that* call" is one span. Both take the span's subtree with them;
    see :func:`prune`.

    Immutable and canonical: every mutation returns a new Filter with its values
    sorted and deduplicated, so the same view always produces the same query
    string, the same saved file and the same provenance block.

    ``zoom`` rides along but is not a filter. It names the span whose subtree the
    waterfall's axis is re-based on (``?zoom=<span id>``), and it hides nothing:
    `active`, `hides` and :func:`filtered_document` are blind to it, and the
    breakdown, the coverage strip and the charts stay over the whole filtered
    view. It lives here because every control on the trial page links through
    `query`, and a link that dropped it would silently un-zoom the reader.
    """

    names: tuple = ()
    span_ids: tuple = ()
    zoom: str = ""

    @classmethod
    def of(cls, names=(), span_ids=(), zoom=None) -> "Filter":
        return cls(names=clean(names), span_ids=clean(span_ids), zoom=str(zoom or "").strip())

    @property
    def active(self) -> bool:
        return bool(self.names or self.span_ids)

    @property
    def query(self) -> dict:
        """Arguments for ``url_for``, so any filtered view is a plain link."""
        out = {"hide": list(self.names), "hide_span": list(self.span_ids)}
        if self.zoom:
            out["zoom"] = self.zoom
        return out

    def hides(self, raw: dict) -> bool:
        """Is this raw span filtered out?

        Matched on the *displayed* label rather than the instrumentation name, so
        what the chips offer, what the URL carries and what the waterfall row
        shows are all one string — and, since that label is already redacted, no
        raw job text ends up in a query string.
        """
        return (
            raw_label(raw) in self.names
            or str(raw.get("span_id") or "") in self.span_ids
        )

    def toggled_name(self, name: str) -> "Filter":
        return Filter.of(toggled(self.names, name), self.span_ids, self.zoom)

    def toggled_span(self, span_id: str) -> "Filter":
        return Filter.of(self.names, toggled(self.span_ids, span_id), self.zoom)

    def cleared(self) -> "Filter":
        """Nothing hidden. The zoom is a separate view and keeps its own reset."""
        return Filter(zoom=self.zoom)

    def zoomed_to(self, span_id: str) -> "Filter":
        return Filter.of(self.names, self.span_ids, span_id)

    def unzoomed(self) -> "Filter":
        return Filter.of(self.names, self.span_ids)


@dataclass
class Facet:
    """One filterable label, with how many spans carry it."""

    name: str
    kind: str = ""              # the commonest kind under this label, for the swatch
    count: int = 0
    hidden: bool = False


@dataclass
class Span:
    span_id: str = ""
    parent_span_id: str = ""    # "" on roots — the key is absent, not null
    name: str = ""              # the instrumentation name
    label: str = ""             # what to display; often the tool name instead
    kind: str = ""
    start_ns: int = 0
    end_ns: int | None = None
    duration_ns: int | None = None   # None means no known end — a marker, not a zero
    timing_quality: str = "unknown"
    source: str = ""
    attributes: list = field(default_factory=list)   # [(key, value_str)]
    tokens: dict = field(default_factory=dict)
    content: list = field(default_factory=list)      # [(label, text)]
    children: list = field(default_factory=list)

    @property
    def merged(self) -> bool:
        """Assembled from more than one source (OTLP span + agent log).

        Either the producer said so, or `fold_llm_requests` did it here — the
        badge means the same thing to a reader, so both keys light it.
        """
        return any(k in _MERGED_KEYS for k, _ in self.attributes)


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


@dataclass
class Trace:
    run_id: str = ""
    roots: list = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)
    extent: Extent = field(default_factory=Extent)
    axis: Extent = field(default_factory=Extent)   # what the bars are placed on: `extent`, or the zoomed subtree's
    rows: list = field(default_factory=list)
    ticks: list = field(default_factory=list)
    truncated: int = 0          # spans beyond MAX_ROWS, not rendered
    shown: int = 0              # spans in the filtered view, whatever the zoom leaves in the waterfall
    zoomed: "Span | None" = None    # the span the waterfall is re-based on
    zoom_lost: str = ""         # a `zoom` that names no span in this view — shown whole, with a note
    error: str = ""             # set if the file could not be read
    filter: Filter = field(default_factory=Filter)
    facets: list = field(default_factory=list)      # [Facet], from the unfiltered tree
    hidden_spans: list = field(default_factory=list)  # [(span_id, label)] hidden by id
    hidden_count: int = 0       # spans the filter removed, subtrees included
    view: Breakdown = field(default_factory=Breakdown)   # totals over what is left
    baseline: Breakdown = field(default_factory=Breakdown)  # ... and over all of them

    @property
    def has_spans(self) -> bool:
        return bool(self.rows)

    @property
    def visible(self) -> bool:
        """Should the trace panel render at all.

        Not just `has_spans`: a filter can legitimately hide every span, and a
        panel that vanished would take its own Clear control with it.
        """
        return self.has_spans or self.filter.active

    @property
    def zero_measured(self) -> bool:
        """Nothing was measured: every duration below is inferred.

        The signal that instrumentation captured nothing and the producer fell
        back to parsing the agent's own logs. Inferred timings are approximate
        and simply wrong under parallel tool calls, so the UI has to say so.
        """
        return bool(self.totals.span_count) and self.totals.measured_span_count == 0

    @property
    def instrumented_ns(self) -> int:
        """How much wall clock the spans actually cover.

        Off `extent`, never `axis`: a zoom re-bases the waterfall, and the
        coverage strip describes the capture, not the view.
        """
        return self.extent.span_ns if self.has_spans else 0

    @property
    def zoom_offset_ns(self) -> int:
        """Where the zoomed window starts, as an offset into the capture."""
        return self.axis.start_ns - self.extent.start_ns if self.zoomed else 0

    @property
    def coverage_pct(self) -> float:
        """Share of the run the waterfall can see, 0 when unknowable.

        The axis is derived from the spans, so a run where 1% was instrumented
        renders bars across the full width and looks completely captured. This
        is the number that says otherwise. It compares *durations* only — it
        never places a container-stamped instant on the host's clock.
        """
        wall = self.totals.wall_ns
        if not wall or not self.has_spans:
            return 0.0
        return min(100.0, self.instrumented_ns / wall * 100)

    @property
    def kinds_present(self) -> list:
        seen = {r.span.kind for r in self.rows}
        known = [k for k in KIND_ORDER if k in seen]
        return known + sorted(seen - set(KIND_ORDER))

    @property
    def qualities_present(self) -> list:
        seen = {r.span.timing_quality for r in self.rows}
        return [q for q in ("measured", "inferred", "unknown") if q in seen]


def block_types_of(raw: dict) -> tuple:
    """What kinds of block a request can be shown to have produced.

    Two producers say it two ways and both are read: Harbor emits a child span
    per block (`reasoning`, `output`), while another may keep the
    text on the call itself. Empty when nothing says — a call whose only block
    was a `tool_use` is not "an output call", it is just a call.
    """
    content = raw.get("content") or {}
    seen = {kind for kind in _BLOCK_KINDS if content.get(kind)}
    for child in raw.get("children") or []:
        if isinstance(child, dict) and str(child.get("kind") or "") in _BLOCK_KINDS:
            seen.add(str(child.get("kind")))
    return tuple(kind for kind in _BLOCK_KINDS if kind in seen)


def label_of(name: str, attributes: dict, kind: str = "", blocks: tuple = ()) -> str:
    """What to show for a span.

    Merged spans overwrite `name` with the log-derived tool name ("Write");
    unmerged ones keep the instrumentation name ("claude_code.tool") and put the
    tool name only in an attribute. Prefer the attribute, so both render as the
    tool. The raw name stays on the span for the detail pane.

    An LLM call reads the same way. `claude_code.llm_request` is the exporter's
    name for the same thing the agent log calls `llm_request`, so it is shown
    under the log's name; and when the call produced exactly one *kind* of block
    it is shown under that instead — `reasoning`, or `output`. Two kinds is not
    one type, so a call that both thought and answered stays `llm_request`.

    Both of those rules make a bracketing span and the call inside it share a
    label, which is why `mark_groups` re-kinds the outer one before this ever
    sees it: given `group`, the label says so. The suffix is added here rather
    than written onto the span so that one function decides what a span is
    called, and the raw name and `tool_name` reach the detail pane untouched.
    """
    tool = (attributes or {}).get("tool_name")
    if isinstance(tool, str) and tool.strip():
        label = tool.strip()
    elif name in (_OTEL_LLM_NAME, _LOG_LLM_NAME):
        label = blocks[0] if len(blocks) == 1 else _LOG_LLM_NAME
    else:
        label = name
    return label + GROUP_SUFFIX if kind == GROUP_KIND else label


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _content_of(raw: dict) -> list:
    out = []
    for key, label in _CONTENT_FIELDS:
        value = raw.get(key)
        if value is None or value == "" or value == {} or value == []:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, indent=2)
        out.append((label, redact(str(value))))
    return out


def _span(raw: dict, depth: int = 0) -> Span:
    # `.get(k) or {}` rather than `.get(k, {})` throughout: the producer writes
    # with exclude_none, so optional fields are absent — but a null would slip
    # straight past a default argument and reach the template as None.
    attributes = raw.get("attributes") or {}
    end_ns = _int_or_none(raw.get("end_ns"))
    start_ns = _int_or_none(raw.get("start_ns")) or 0
    duration_ns = _int_or_none(raw.get("duration_ns"))
    if duration_ns is None and end_ns is not None:
        duration_ns = end_ns - start_ns

    name = redact(str(raw.get("name") or ""))
    kind = str(raw.get("kind") or "")
    return Span(
        span_id=str(raw.get("span_id") or ""),
        parent_span_id=str(raw.get("parent_span_id") or ""),
        name=name,
        label=redact(
            label_of(str(raw.get("name") or ""), attributes, kind, block_types_of(raw))
        ),
        kind=kind,
        start_ns=start_ns,
        end_ns=end_ns,
        duration_ns=duration_ns,
        timing_quality=str(raw.get("timing_quality") or "unknown"),
        source=str(raw.get("source") or ""),
        # Keys are instrumentation enums (gen_ai.request.model), not job text, so
        # they are not redacted; every value is.
        attributes=sorted((str(k), redact(str(v))) for k, v in attributes.items()),
        tokens={k: v for k, v in (raw.get("tokens") or {}).items() if v is not None},
        content=_content_of(raw.get("content") or {}),
        children=(
            [_span(c, depth + 1) for c in raw.get("children") or []]
            if depth < MAX_DEPTH
            else []
        ),
    )


def all_spans(roots: list):
    """Depth-first walk. Guards against a parent cycle rather than hanging."""
    seen = set()

    def walk(span):
        if span.span_id and span.span_id in seen:
            return
        seen.add(span.span_id)
        yield span
        for child in span.children:
            yield from walk(child)

    for root in roots:
        yield from walk(root)


def extent_of(roots: list) -> Extent:
    spans = list(all_spans(roots))
    if not spans:
        return Extent()
    start = min(s.start_ns for s in spans)
    end = max((s.end_ns if s.end_ns is not None else s.start_ns) for s in spans)
    # Floored at 1: a trace whose spans are all instants has end == start, and
    # every percentage would come out NaN or Infinity.
    return Extent(start_ns=start, end_ns=end, span_ns=max(end - start, 1))


def find_span(roots: list, span_id: str):
    """The span called *span_id* in this tree, or None. Same walk as `all_spans`."""
    if not span_id:
        return None
    return next((s for s in all_spans(roots) if s.span_id == span_id), None)


def raw_spans_of(spans: list, depth: int = 0):
    """Depth-first walk of raw span dicts, straight from the document.

    The filter and everything that saves a file work on raw dicts rather than on
    parsed :class:`Span`s: a saved trace has to keep every field the producer
    wrote — including the content this reader redacts — and a round trip through
    the parser would quietly drop whatever it does not render.
    """
    for raw in spans:
        if not isinstance(raw, dict):
            continue
        yield raw
        if depth < MAX_DEPTH:
            yield from raw_spans_of(raw.get("children") or [], depth + 1)


def raw_label(raw: dict) -> str:
    """The label a raw span will be displayed under. Redacted, like the parsed one.

    Reads `kind` off the span, so it agrees with :func:`_span` only once
    :func:`mark_groups` has run. Everything that filters or counts by label goes
    through the normalised document for that reason.
    """
    return redact(label_of(
        str(raw.get("name") or ""), raw.get("attributes") or {},
        str(raw.get("kind") or ""), block_types_of(raw),
    ))


def _bare_label(raw: dict) -> str:
    """A raw span's label as if it were not a group. Its identity, not its role."""
    return label_of(
        str(raw.get("name") or ""), raw.get("attributes") or {}, blocks=block_types_of(raw)
    )


def _self_ns(raw: dict) -> int | None:
    """Time inside *raw* that none of its children covers, or None if unknowable.

    Overlapping children are merged and clipped to the parent first: a tool call
    nested in the LLM call that asked for it would otherwise be counted twice and
    make a span with real self-time look fully covered.
    """
    start = _int_or_none(raw.get("start_ns"))
    end = _int_or_none(raw.get("end_ns"))
    if start is None or end is None or end <= start:
        return None
    inside = []
    for child in raw.get("children") or []:
        if not isinstance(child, dict):
            continue
        low = _int_or_none(child.get("start_ns"))
        high = _int_or_none(child.get("end_ns"))
        if low is None or high is None:
            continue
        low, high = max(low, start), min(high, end)
        if high > low:
            inside.append((low, high))
    return (end - start) - sum(high - low for low, high in merge_intervals(inside))


def _same_extent(raw: dict, child: dict) -> bool:
    """Do these two spans start and end together, to within `GROUP_SLACK_NS`?

    Both ends, and that is the point. Coverage alone is satisfied by a child that
    overruns its parent — a background subagent hung under the launch that
    returned in 47ms runs for minutes. Clipped
    to the parent it covers all of it, but the launch is a real 47ms event.
    """
    for key in ("start_ns", "end_ns"):
        mine, theirs = _int_or_none(raw.get(key)), _int_or_none(child.get(key))
        if mine is None or theirs is None or abs(mine - theirs) > GROUP_SLACK_NS:
            return False
    return True


def is_group(raw: dict) -> bool:
    """Is this span a namespace for its children rather than an event itself?

    **Its children have to account for all of it**, to within `GROUP_SLACK_NS`.
    A span that also spent time of its own is doing something, whatever it is
    named. That is necessary but nowhere near sufficient, so one of two further
    signals has to say the span adds no distinction to what it contains:

    * **it shares its label with a child** — Harbor's `claude_code.tool` carries
      `tool_name: "Bash"` and holds the span that actually ran Bash, so one call
      would otherwise be counted twice under one name; or
    * **it has a single child of the same extent** — the same bar drawn twice.
      An `llm_request` may hold one `reasoning` or `output` block whose start
      and end are *byte-identical* to its own,
      because the producer splits a response into blocks and a one-block response
      is the request again. `fold_llm_requests` runs first and collapses that
      pair outright wherever it can match the request to the call it answered,
      which is the better answer — one span keeping both clocks and both sets of
      text. What reaches here is the remainder it could not match.

    Everything else with children stays what it is, and the near misses are worth
    knowing because each one looks like this from a distance. OpenCode's
    `ai.streamText` also brackets the stream call it is named for, but can spend
    time on its own transport; it is still a call. `claude_code.interaction`
    is covered by the calls inside it, and `session`/`subagent` are exempt
    outright: a kind that already announces itself as a bracket is not claiming
    to be work, and `_KIND_CATEGORY` already charges it nothing. A group is
    specifically a span pretending to be the event nested inside it.

    A call `fold_llm_requests` gave children to is exempt for a sharper reason:
    it would match, always, and the match would be this reader's own doing. The
    fold moves the reply's blocks under the call and then labels the call after
    what it produced, so the parent shares a label with a child by construction
    and the blocks span the call because they are the reply to it. Nothing was
    pretending to be anything — and the call is the one span in that subtree with
    a measured clock, so re-kinding it would charge the only real measurement to
    nothing and leave the inferred blocks holding the time.
    """
    if str(raw.get("kind") or "") in _BRACKET_KINDS:
        return False
    if _MERGED_FROM in (raw.get("attributes") or {}):
        return False
    children = [c for c in raw.get("children") or [] if isinstance(c, dict)]
    if not children:
        return False
    self_ns = _self_ns(raw)
    if self_ns is None or self_ns > GROUP_SLACK_NS:
        return False
    if len(children) == 1 and _same_extent(raw, children[0]):
        return True
    # Compared without the suffix on either side. `mark_groups` works children
    # first, so an inner span may already be a group by the time the outer one is
    # asked; matching on the displayed label would then miss a doubly-wrapped
    # call and leave the outer one claiming to be the tool.
    label = _bare_label(raw)
    return any(_bare_label(c) == label for c in children)


def mark_groups(spans: list, depth: int = 0) -> list:
    """*spans* with every namespace span re-kinded `group`, children first.

    Runs once, before anything filters or counts, so that the label a chip
    offers, the label in the query string and the label on the row are the same
    string — decided per span, never per surviving child. Filtering the `Bash`
    inside a group therefore leaves the group a group, still named for what it
    used to contain, which is the only answer that keeps a filter from renaming
    the rows it was not asked about.

    Copy-on-write like :func:`prune`: a subtree with no groups in it is passed
    through by reference, so the caller's document is never mutated.
    """
    out: list = []
    changed = False
    for raw in spans:
        if not isinstance(raw, dict):
            changed = True
            continue
        children = raw.get("children") or []
        if children and depth < MAX_DEPTH:
            marked = mark_groups(children, depth + 1)
            if marked is not children:
                raw, changed = dict(raw, children=marked), True
        if is_group(raw):
            raw, changed = dict(raw, kind=GROUP_KIND), True
        out.append(raw)
    return out if changed else spans


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


def inventory(spans: list, filt: "Filter | None" = None) -> list:
    """Every distinct span label in the document, with how many spans carry it.

    Built from the *unfiltered* tree: a hidden label still needs its chip, or
    there would be no way left to bring it back.
    """
    filt = filt or Filter()
    counts: dict = {}
    kinds: dict = {}
    for raw in raw_spans_of(spans):
        name = raw_label(raw)
        counts[name] = counts.get(name, 0) + 1
        seen = kinds.setdefault(name, {})
        kind = str(raw.get("kind") or "")
        seen[kind] = seen.get(kind, 0) + 1
    out = [
        Facet(
            name=name,
            # One label can span kinds (a merged parent and its child both read
            # "Write"); the commonest wins, since it only picks the swatch.
            kind=max(kinds[name].items(), key=lambda kv: (kv[1], kv[0]))[0],
            count=count,
            hidden=name in filt.names,
        )
        for name, count in counts.items()
    ]
    # Commonest first, then alphabetical — a stable order, and the labels worth
    # filtering are the ones there are a lot of.
    out.sort(key=lambda f: (-f.count, f.name))
    return out


def labels_by_id(spans: list) -> dict:
    """span_id → label, over the whole unfiltered document.

    So a span hidden by id can still be named in the UI that offers to unhide it.
    """
    return {
        str(raw.get("span_id") or ""): raw_label(raw)
        for raw in raw_spans_of(spans)
        if raw.get("span_id")
    }


def _window(raw: dict) -> tuple:
    """(start, end) in ns, an unknown end read as the start — same as `_span`."""
    start = _int_or_none(raw.get("start_ns")) or 0
    end = _int_or_none(raw.get("end_ns"))
    return start, start if end is None else end


def _tokens_key(raw: dict) -> tuple:
    """A call's usage, as something comparable. Empty when none was reported."""
    return tuple(
        sorted(
            (str(k), v)
            for k, v in (raw.get("tokens") or {}).items()
            if _int_or_none(v) is not None
        )
    )


def _request_groups(wrappers: list) -> list:
    """Log request spans grouped by the reply they came from.

    One API call arrives as several records — a thinking block, each tool_use,
    the final text — and the producer opens a span per record. They carry one
    `message.id` between them, which is exactly the grouping wanted; a producer
    that omits it leaves every record its own group, which folds them one by one
    and is no worse than not folding at all.
    """
    groups: dict = {}
    order: list = []
    for raw in wrappers:
        key = (raw.get("attributes") or {}).get("message.id")
        key = str(key) if key else "ungrouped:" + str(raw.get("span_id") or id(raw))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(raw)
    return [groups[key] for key in order]


def _match_requests(containers: list, groups: list) -> dict:
    """container span_id → the log request spans belonging to it.

    Harbor joins these on the request id the exporter reported. This runs where
    the provider returned none, so the join is on what is left: identical usage,
    and overlapping windows. Usage is a hard constraint when both sides report
    it and the overlap only ranks the candidates, which is what keeps two calls
    that happen to overlap from swapping their blocks.

    A wrong match moves a row under the wrong call; it cannot change a number,
    because folding re-parents spans and never touches a clock.
    """
    taken: set = set()
    out: dict = {}
    for container in sorted(containers, key=lambda raw: _window(raw)[0]):
        c_start, c_end = _window(container)
        c_tokens = _tokens_key(container)
        best = None
        for index, group in enumerate(groups):
            if index in taken:
                continue
            g_start = min(_window(raw)[0] for raw in group)
            g_end = max(_window(raw)[1] for raw in group)
            if g_start >= c_end or c_start >= g_end:
                continue
            g_tokens = next((t for t in map(_tokens_key, group) if t), ())
            if c_tokens and g_tokens and c_tokens != g_tokens:
                continue
            distance = abs(g_start - c_start)
            if best is None or distance < best[0]:
                best = (distance, index, group)
        if best is not None:
            taken.add(best[1])
            out[str(container.get("span_id") or "")] = best[2]
    return out


def _collect_requests(
    spans: list, parent: str, containers: list, wrappers: list, depth: int = 0
) -> None:
    """The calls to fold into, and the log request spans still to fold.

    A request already sitting under a call has been folded — by this function on
    an earlier read, or by Harbor itself — and must not be folded again: a saved
    view can be filtered again, which re-reads a document this has already seen,
    and a second pass would rewrite the provenance of the first.
    """
    for raw in spans:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        if name == _OTEL_LLM_NAME:
            containers.append(raw)
        elif (
            name == _LOG_LLM_NAME
            and str(raw.get("source") or "") == "log"
            and parent != _OTEL_LLM_NAME
        ):
            wrappers.append(raw)
        if depth < MAX_DEPTH:
            _collect_requests(raw.get("children") or [], name, containers, wrappers, depth + 1)


def _folded_child(raw: dict, parent: dict) -> dict:
    """One log request span, rewritten to sit under the call it belongs to.

    A record that produced a single typed block *is* that block — the two share
    a window exactly — so the two spans collapse into one rather than drawing the
    same instants twice more. The block keeps its identity and content; the
    request hands over its attributes and its tokens.

    Handing the tokens over matters more than it looks. The producer's own
    `totals.tokens` is the sum over every span it wrote, duplicated usage
    included: dropping a record's copy here would leave the recomputed breakdown
    disagreeing with the reported totals by a factor of three, and this reader
    would appear to have invented the defect.
    """
    children = [c for c in raw.get("children") or [] if isinstance(c, dict)]
    typed = [c for c in children if str(c.get("kind") or "") in _BLOCK_KINDS]
    out = dict(raw)
    if len(children) == 1 and len(typed) == 1 and _window(typed[0]) == _window(raw):
        block = typed[0]
        attributes = {**(raw.get("attributes") or {}), **(block.get("attributes") or {})}
        attributes[_MERGED_FROM] = str(raw.get("span_id") or "")
        out = dict(block, attributes=attributes)
        if raw.get("tokens") and not block.get("tokens"):
            out["tokens"] = raw["tokens"]
    out["parent_span_id"] = str(parent.get("span_id") or "")
    return out


def fold_llm_requests(spans: list) -> list:
    """Each measured LLM call given the log spans for the reply it produced.

    Without this the same call is drawn twice — as `claude_code.llm_request`
    under the interaction, and again as `llm_request` under the log's own
    `session` subtree — with the second copy carrying the reasoning and output
    text and the first carrying the only measured clock. Folding puts the blocks
    under the call, which is the shape Harbor itself writes when the exporter
    gives it a request id to join on.

    Nothing is invented and nothing is dropped. Every span survives with its own
    start, end and tokens; a request span that collapsed into its one block is
    that block, carrying what the request knew. The rows move, the clocks do not,
    and the producer's `totals` is untouched, as ever.

    Copy-on-write, like `prune`: a document with nothing to fold is returned by
    reference.
    """
    containers, wrappers = [], []
    _collect_requests(spans, "", containers, wrappers)
    if not containers or not wrappers:
        return spans

    folded = _match_requests(containers, _request_groups(wrappers))
    if not folded:
        return spans
    by_id = {str(c.get("span_id") or ""): c for c in containers}
    by_container = {
        parent_id: [_folded_child(raw, by_id[parent_id]) for raw in group]
        for parent_id, group in folded.items()
    }
    moved = {id(raw) for group in folded.values() for raw in group}
    return _fold_tree(spans, by_container, moved)[0]


def _fold_tree(spans: list, by_container: dict, moved: set, depth: int = 0) -> tuple:
    """(spans, changed) — the tree with the matched request spans relocated.

    Identity, not span id, is what marks a span as moved: ids are opaque strings
    from two producers here, and a collision would otherwise delete a span rather
    than move it.
    """
    out: list = []
    changed = False
    for raw in spans:
        if not isinstance(raw, dict):
            continue
        if id(raw) in moved:
            changed = True
            continue
        children = raw.get("children") or []
        if children and depth < MAX_DEPTH:
            children, dropped = _fold_tree(children, by_container, moved, depth + 1)
        else:
            dropped = False
        blocks = by_container.get(str(raw.get("span_id") or ""))
        if blocks:
            children = sorted(children + blocks, key=lambda s: _window(s)[0])
            attributes = dict(raw.get("attributes") or {})
            attributes[_MERGED_FROM] = ", ".join(
                str((b.get("attributes") or {}).get(_MERGED_FROM) or b.get("span_id") or "")
                for b in blocks
            )
            raw = dict(raw, children=children, attributes=attributes)
        elif dropped:
            raw = dict(raw, children=children)
        changed = changed or dropped or bool(blocks)
        out.append(raw)
    return out, changed


def prune(spans: list, filt: "Filter | None" = None, depth: int = 0) -> tuple:
    """(kept, removed) — hidden spans dropped, with their subtrees.

    A hidden span takes its children with it. Keeping them would leave a tool's
    inner spans floating with no call around them, and hiding a call is a request
    to take its whole cost out of the picture — including the cost of what it did.

    Copy-on-write: a subtree nothing was removed from is passed through by
    reference, so the caller's document is never mutated and saving a filtered
    view cannot corrupt the original.
    """
    filt = filt or Filter()
    kept: list = []
    removed = 0
    for raw in spans:
        if not isinstance(raw, dict):
            continue
        if filt.hides(raw):
            removed += 1 + sum(1 for _ in raw_spans_of(raw.get("children") or []))
            continue
        children = raw.get("children") or []
        if children and depth < MAX_DEPTH:
            children, dropped = prune(children, filt, depth + 1)
            if dropped:
                removed += dropped
                raw = dict(raw, children=children)
        kept.append(raw)
    return kept, removed


def merge_intervals(intervals: list) -> list:
    """Overlapping [start, end) runs collapsed into disjoint ones."""
    out: list = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


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


# Stamped into a saved view as both its producer and the hand that filtered it.
FILTER_PRODUCER = "app/nullius/trace.py (filtered view)"


def filtered_document(data: dict, filt: "Filter | None" = None) -> dict:
    """*data* with the hidden spans gone and ``totals`` recomputed over the rest.

    What gets written to disk, and the reason :func:`prune` works on raw dicts.
    Three things are load-bearing about the result:

    * it is still a ``harbor-trace-v1`` document, so this app can open it;
    * ``wall_ns`` is the source's, so removed time surfaces as ``unaccounted``
      rather than vanishing into a shorter run;
    * the producer's own ``totals`` survives verbatim under ``filtered``, since a
      derived number that has overwritten the measured one is unfalsifiable.

    Filtering a view that was itself saved from a filtered view works, and the
    chain is kept under ``filtered.previous`` rather than overwritten — otherwise
    the second save would present derived totals as if they were measured.
    """
    filt = filt or Filter()
    # The same two normalisations `parse` does, in the same order, and each for
    # its own reason. Folding first because hiding a call takes its subtree with
    # it and folding is what puts the reply's blocks in that subtree, so an
    # unfolded save would drop a different set of spans than the page showed;
    # `mark_groups` because `hide` carries the displayed label, and "Bash [group]"
    # only matches a span that has been re-kinded. Both are written out with the
    # spans — the document already names this tree as its producer, and a saved
    # view that reopened as a different shape than the one saved would be worse
    # than a changed enum.
    kept, removed = prune(mark_groups(fold_llm_requests(data.get("spans") or [])), filt)
    source_totals = data.get("totals") or {}
    view = breakdown(kept, _int_or_none(source_totals.get("wall_ns")) or 0)
    out = dict(data)
    out["spans"] = kept
    out["totals"] = view.as_totals()
    # This file was written here, whatever wrote the spans — `producer` naming a
    # script that never saw these totals is exactly the kind of plausible-looking
    # provenance that makes a derived artefact unfalsifiable. The original moves
    # into the block below rather than being dropped.
    out["producer"] = FILTER_PRODUCER
    out["filtered"] = {
        "by": FILTER_PRODUCER,
        "source_producer": str(data.get("producer") or ""),
        "hide": list(filt.names),
        "hide_span": list(filt.span_ids),
        "spans_removed": removed,
        "note": (
            "totals recomputed from the spans left, by charging every instant to one "
            "category at priority idle > tool > reasoning > llm; wall_ns is the source's, "
            "so the removed time reads as unaccounted; log request spans matched to the "
            "measured call they belong to were folded under it first (app.merged_from), "
            "and spans that only bracket a same-named child are re-kinded 'group' and "
            "charged no time"
        ),
        "source_totals": source_totals,
    }
    previous = data.get("filtered")
    if isinstance(previous, dict):
        out["filtered"]["previous"] = previous
    return out


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def place(span: Span, extent: Extent) -> tuple:
    """(left_pct, width_pct) on the global axis.

    All arithmetic is Python int until this line, so the nanosecond magnitudes
    that overflow a JS double are exact here — no BigInt problem to work around.
    """
    left = _clamp((span.start_ns - extent.start_ns) / extent.span_ns * 100)
    duration = span.duration_ns or 0
    width = _clamp(duration / extent.span_ns * 100, 0.0, 100.0 - left)
    return left, width


def is_instant(span: Span) -> bool:
    """No usable duration: one known instant, or an explicitly unknown clock."""
    return span.timing_quality == "unknown" or not (span.duration_ns and span.duration_ns > 0)


def flatten(roots: list, collapsed=frozenset(), extent: Extent | None = None) -> tuple:
    """Depth-first rows, skipping the children of collapsed spans.

    Returns (rows, truncated). Collapse is honoured here so it is testable, but
    the template renders every row and lets the browser hide descendants — which
    is what makes expanding a deep-linked span a no-round-trip operation.

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


def parse(data: dict, filt: "Filter | None" = None) -> Trace:
    """Build a Trace from already-decoded JSON. Pure — what the tests drive.

    *filt* hides spans from the view (see :class:`Filter`). Everything the filter
    controls needs the unfiltered document too, and for different reasons: the
    chips have to offer labels that are currently hidden, the axis has to stay
    where it was so the surviving bars do not move, and the producer's `totals`
    is reported whatever is on screen.
    """
    if not isinstance(data, dict):
        return Trace(error="trace.json is not an object")

    filt = filt or Filter()
    # Normalised before anything reads it, so `raw` is the unfiltered document as
    # the page will name it — the chips, the axis, the filter and the baseline
    # breakdown all come off this list, and a span has to be folded and named the
    # same way in every one of them. Same order, and the same list, as
    # `filtered_document` writes out.
    raw = mark_groups(fold_llm_requests(data.get("spans") or []))
    kept, hidden = prune(raw, filt)
    roots = [_span(s) for s in kept]
    extent = raw_extent(raw)
    # The zoom is a second, explicit window on top of the filter — never a
    # re-scaling of the filtered view. It is looked up in the *pruned* tree, so a
    # zoom on a span the filter hides falls back to the whole trace with a note,
    # and the axis it re-bases on is the subtree's own extent (`extent_of` over
    # the one span, the same walk the global axis uses — no second layout).
    # Spans outside the subtree are left out of the rows, not squeezed in.
    axis, zoomed, zoom_lost = extent, None, ""
    if filt.zoom:
        zoomed = find_span(roots, filt.zoom)
        if zoomed is None:
            zoom_lost = filt.zoom
        else:
            axis = extent_of([zoomed])
    rows, truncated = flatten([zoomed] if zoomed else roots, extent=axis)
    totals = _totals(data.get("totals") or {})
    names = labels_by_id(raw) if filt.span_ids else {}

    return Trace(
        # Real files emit `trial_name`; the format doc calls it `run_id`. Take
        # either, prefer what the producer actually writes.
        run_id=str(data.get("trial_name") or data.get("run_id") or ""),
        roots=roots,
        totals=totals,
        extent=extent,
        axis=axis,
        rows=rows,
        ticks=ticks_for(axis) if rows else [],
        truncated=truncated,
        shown=sum(1 for _ in all_spans(roots)),
        zoomed=zoomed,
        zoom_lost=zoom_lost,
        filter=filt,
        facets=inventory(raw, filt),
        hidden_spans=[(sid, names.get(sid, sid)) for sid in filt.span_ids],
        hidden_count=hidden,
        # The wall clock is the run's, not the view's — see `Breakdown`. With no
        # `totals` block to take it from, the instrumented window is the best
        # denominator available, and it is the one the axis already uses.
        view=breakdown(kept, totals.wall_ns or extent.span_ns),
        # The same arithmetic over the unfiltered tree, so the view can show what
        # the filter changed without the producer's own accounting in the way.
        baseline=breakdown(raw, totals.wall_ns or extent.span_ns),
    )


def load(path: str, filt: "Filter | None" = None) -> Trace:
    """Read *path*, or return a Trace carrying the error.

    A trace is an observability sidecar: a missing or broken one must never take
    the trial page down with it.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return Trace(error=str(exc))
    return parse(data, filt)
