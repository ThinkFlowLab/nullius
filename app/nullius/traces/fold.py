"""A measured LLM call given the log spans for the reply it produced.

It moves rows and no numbers, and runs before grouping: ``trace`` says why.
"""

from __future__ import annotations

from .spans import _BLOCK_KINDS, _int_or_none, _LOG_LLM_NAME, MAX_DEPTH, _MERGED_FROM, _OTEL_LLM_NAME

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

    The producer's own
    `totals.tokens` is the sum over every span it wrote, duplicated usage
    included: dropping a record's copy here would leave the recomputed breakdown
    disagreeing with the reported totals by a factor of three.
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
    and the producer's `totals` is untouched.

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
