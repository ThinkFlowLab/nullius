"""The span tree of an agent trace: the model, the walks over it, what a span is called."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from nullius.redact import redact

NS_PER_MS = 1_000_000

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
# every consumer already switches on `kind`, and `_KIND_CATEGORY` omits groups so they accrue no time.
GROUP_KIND = "group"

# Said in the label, because the label is the only thing most of the page shows.
# Appended rather than substituted: "Bash [group]" and "Bash" sort together and
# read as the same call seen at two levels.
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
# is a container — a kind that never made the claim needs no correcting.
_BRACKET_KINDS = ("session", "subagent", GROUP_KIND)

# The OTel span for one API call, and the log-derived span for one *record* of
# the reply. Without a request ID, Harbor can leave duplicate call spans for `fold_llm_requests` to join.
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

        Either the producer said so, or `fold_llm_requests` did it here.
        """
        return any(k in _MERGED_KEYS for k, _ in self.attributes)


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
    tool.

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


def find_span(roots: list, span_id: str):
    """The span called *span_id* in this tree, or None."""
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

    Coverage alone is satisfied by a child that
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
      is the request again. `fold_llm_requests` runs first; this sees what it could not match.

    Everything else with children stays what it is. OpenCode's
    `ai.streamText` also brackets the stream call it is named for, but can spend
    time on its own transport; it is still a call. `claude_code.interaction`
    is covered by the calls inside it, and `session`/`subagent` are exempt
    outright: they never claim to be work.

    A call `fold_llm_requests` gave children to is exempt for a sharper reason:
    it would match, always, and the match would be this reader's own doing. The
    fold moves the reply's blocks under the call and then labels the call after
    what it produced, so the parent shares a label with a child by construction
    and the blocks span the call because they are the reply to it. The call is the one span in that subtree with
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


def merge_intervals(intervals: list) -> list:
    """Overlapping [start, end) runs collapsed into disjoint ones."""
    out: list = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def is_instant(span: Span) -> bool:
    """No usable duration: one known instant, or an explicitly unknown clock."""
    return span.timing_quality == "unknown" or not (span.duration_ns and span.duration_ns > 0)
