"""Load an agent trace (``agent/trace.json``) into rows for a waterfall.

A sidecar to the trajectory, not a replacement: where ``trajectory.json`` carries
one timestamp per step with no duration, a trace carries measured start *and*
end per LLM call, tool call and subagent, with causal parentage.

Two rules shape everything here.

**One global time axis.** Bars are positioned against the whole trace, never
against their parent. Log-derived children routinely start *before* their OTel
parents, because the two come from different clocks; laying out relative to the
parent would force a clamp, and the clamp would hide exactly the discrepancy the
format exists to expose. A *zoom* (``Filter.zoom``) is not a per-parent layout:
it is one explicit window — a subtree's own extent — that the reader asks for,
and everything outside it is left out of the waterfall rather than re-scaled.

**Totals are the producer's job.** ``totals`` is displayed verbatim and never
recomputed from the tree — see :class:`breakdown.Totals`. A filtered view is the one case
that needs its own arithmetic, because hiding a span has to move its time
somewhere and the producer cannot be asked again; that lives in
:class:`breakdown.Breakdown`, is labelled as derived wherever it is shown, and never
overwrites what the file said.

Two normalisations happen on the way in, in that order, and they are the only
two. First, a call the exporter measured is given the log spans for the reply it
produced, which the producer writes as a second copy of the same call when it has
no request id to join them on. See :func:`fold.fold_llm_requests`; it moves rows and
no numbers. Then a span that is a *namespace* for its children rather than an
event of its own is re-kinded ``group`` and reads ``Bash [group]``: Harbor's
Claude Code instrumentation opens a ``claude_code.tool`` span carrying
``tool_name: "Bash"`` and puts the permission wait and the actual ``Bash``
execution inside it. Counting the wrapper as work would double-count the call.
See :func:`spans.is_group`.

The order is load-bearing. An ``llm_request`` holding a single ``reasoning`` or
``output`` block of byte-identical extent is the same
duplication :func:`fold.fold_llm_requests` resolves, and it resolves it better: the
wrapper and its one block collapse into a single span carrying both the
request's tokens and the block's text, where re-kinding leaves two rows and
charges one of them nothing. Folding first leaves ``is_group`` only the wrappers
fold could not match to a call, which is the case it is still needed for.

The span tree is ``spans``, the fold ``fold``, the accounting ``breakdown``, the layout ``waterfall``.

Everything is redacted at load, so templates hold display text only. Stdlib only, no Flask.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .spans import (
    all_spans, find_span, _int_or_none, KIND_ORDER, mark_groups, MAX_DEPTH, raw_label, raw_spans_of,
    Span, _span,
)
from .fold import fold_llm_requests
from .breakdown import Breakdown, breakdown, Totals, _totals
from .waterfall import Extent, extent_of, flatten, raw_extent, ticks_for

def clean(values) -> tuple:
    """Deduplicated, sorted, blank-free — so one view has exactly one URL."""
    return tuple(sorted({str(v) for v in values if str(v).strip()}))


def toggled(values: tuple, value: str) -> tuple:
    value = str(value)
    return tuple(v for v in values if v != value) if value in values else values + (value,)


@dataclass(frozen=True)
class Filter:
    """What a view is leaving out: span labels, and individual spans by id.

    Both take the span's subtree with them;
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
    # Commonest first, then alphabetical — a stable order.
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
    # spans.
    kept, removed = prune(mark_groups(fold_llm_requests(data.get("spans") or [])), filt)
    source_totals = data.get("totals") or {}
    view = breakdown(kept, _int_or_none(source_totals.get("wall_ns")) or 0)
    out = dict(data)
    out["spans"] = kept
    out["totals"] = view.as_totals()
    # This file was written here, whatever wrote the spans — `producer` naming a
    # script that never saw these totals would misattribute them. The original moves
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
    # and the axis it re-bases on is the subtree's own extent.
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
