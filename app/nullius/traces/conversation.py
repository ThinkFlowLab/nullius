"""Bounded, read-only views of already parsed trajectory and trace evidence.

Search examines full redacted text; only twelve index entries and one 6,000
character content window leave this layer. Native step/call ids are retained.
Positions are separate zero-based navigation keys so gappy or repeated source
ids never change order. This module has no Flask or filesystem dependency.
"""
from __future__ import annotations

import json
import math

from nullius.redact import redact

PAGE_SIZE = 12
CHUNK_SIZE = 6000
KINDS = ("all", "messages", "tools", "errors")


def _integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _text(value):
    return redact(str(value)) if value is not None else ""


def _preview(value, limit=135):
    value = " ".join(_text(value).split())
    return value[:limit] + ("…" if len(value) > limit else "")


def _sections(step):
    sections = []
    if step.message and step.message != "(tool use)":
        sections.append(("message", "Message", _text(step.message), {}))
    for i, call in enumerate(step.tool_calls):
        facts = {"tool_call_id": _text(call.get("tool_call_id")) if call.get("tool_call_id") is not None else None,
                 "tool": _text(call.get("name")),
                 "failure": _text(call.get("failure")),
                 "category": _text(call.get("category")),
                 "target": _preview(call.get("target"), 200),
                 "observation_characters": len(_text(call.get("observation")))}
        sections.append((f"call-{i}-arguments", "Arguments", _text(call.get("arguments")), facts))
        sections.append((f"call-{i}-observation", "Result", _text(call.get("observation")), facts))
    return sections


def _content(sections, wanted, chunk):
    """Redact before slicing, including secrets crossing a chunk boundary."""
    selected = next((s for s in sections if s[0] == wanted), sections[0] if sections else None)
    if selected is None:
        return None
    key, label, text, facts = selected
    pages = max(1, math.ceil(len(text) / CHUNK_SIZE))
    index = min(max(0, _integer(chunk)), pages - 1)
    start = index * CHUNK_SIZE
    return {"key": key, "label": label, **facts,
            "text": text[start:start + CHUNK_SIZE], "chunk": index,
            "chunks": pages, "start": start, "end": min(start + CHUNK_SIZE, len(text)),
            "characters": len(text)}


# A call's tool is the command it ran: the reader shows it whole, never cut.
FULL_FACTS = ("tool_call_id", "tool")


def _section_index(sections):
    return [{"key": key, "label": label, "characters": len(text),
             **{k: _preview(v, 100) if isinstance(v, str) and k not in FULL_FACTS else v
                for k, v in facts.items()}}
            for key, label, text, facts in sections]


def page(traj, *, query="", kind="all", page=1, position=None, step=None,
         section=None, chunk=0):
    query = _text(query).strip()[:500]
    kind = kind if kind in KINDS else "all"
    result = {"query": query, "kind": kind, "page_size": PAGE_SIZE,
              "page": 1, "pages": 1, "total": 0, "matches": 0,
              "rows": [], "selected": None, "notice": ""}
    if traj is None or traj.error:
        result["notice"] = ("Trajectory unreadable: " + _text(traj.error)) if traj else "No trajectory recorded for this trial."
        return result
    result["total"] = len(traj.steps)
    needle = query.casefold()
    matching = []
    for i, item in enumerate(traj.steps):
        has_message = bool(item.message and item.message != "(tool use)")
        has_tools = bool(item.tool_calls)
        # Reuse the established reader's failure signals; an error-looking
        # message is not a new failure classification or a benchmark verdict.
        has_errors = any(c.get("failure") for c in item.tool_calls)
        if not {"all": True, "messages": has_message, "tools": has_tools, "errors": has_errors}[kind]:
            continue
        texts = [str(item.step_id), _text(item.source), _text(item.message)]
        for call in item.tool_calls:
            texts.extend(_text(call.get(k)) for k in ("tool_call_id", "name", "arguments", "observation", "failure"))
        if needle and not any(needle in text.casefold() for text in texts):
            continue
        matching.append(i)
    result["matches"] = len(matching)
    result["pages"] = max(1, math.ceil(len(matching) / PAGE_SIZE))
    page_number = min(max(1, _integer(page, 1)), result["pages"])
    selected_position = None
    if step is not None:
        selected_position = next((i for i in matching if str(traj.steps[i].step_id) == str(step)), None)
        if selected_position is None:
            result["notice"] = "That source step id is not in the current search and filter."
    elif position is not None:
        wanted = _integer(position, -1)
        selected_position = wanted if wanted in matching else None
    if selected_position is not None:
        page_number = matching.index(selected_position) // PAGE_SIZE + 1
    elif matching:
        selected_position = matching[(page_number - 1) * PAGE_SIZE]
    result["page"] = page_number
    for i in matching[(page_number - 1) * PAGE_SIZE:page_number * PAGE_SIZE]:
        item = traj.steps[i]
        title = item.message if item.message and item.message != "(tool use)" else ", ".join(c.get("name", "Tool") for c in item.tool_calls)
        result["rows"].append({"position": i, "step_id": item.step_id,
            "source": _preview(item.source, 32), "preview": _preview(title) or "No message recorded",
            "tools": len(item.tool_calls), "errors": sum(bool(c.get("failure")) for c in item.tool_calls)})
    if selected_position is not None:
        item = traj.steps[selected_position]
        match_index = matching.index(selected_position)
        sections = _sections(item)
        result["selected"] = {"position": selected_position, "step_id": item.step_id,
            "source": _text(item.source), "timestamp": _text(item.timestamp), "duration": item.dur,
            "match_number": match_index + 1,
            "previous": matching[match_index - 1] if match_index else None,
            "next": matching[match_index + 1] if match_index + 1 < len(matching) else None,
            "sections": _section_index(sections), "content": _content(sections, section, chunk)}
    elif not result["notice"]:
        result["notice"] = "No steps match this search and filter." if traj.steps else "The trajectory contains no recorded steps."
    return result


def span_detail(trace, span_id, *, section=None, chunk=0):
    """Select only a visible, normalised span; never rebuild from raw JSON."""
    row = next((r for r in trace.rows if r.span.span_id == span_id), None) if trace else None
    if row is None:
        raise LookupError("Span not present in this trace view")
    span = row.span
    sections = [(f"content-{i}", _preview(label, 100), _text(text), {})
                for i, (label, text) in enumerate(span.content)]
    if span.tokens:
        sections.append(("tokens", "Tokens", _text(json.dumps(span.tokens, indent=2, ensure_ascii=False)), {}))
    if span.attributes:
        sections.append(("attributes", "Attributes", _text("\n\n".join(f"{k}\n{v}" for k, v in span.attributes)), {}))
    return {"span_id": span.span_id, "label": _preview(span.label, 200),
            "name": _preview(span.name, 200), "kind": span.kind,
            "timing_quality": span.timing_quality, "source": _preview(span.source, 100),
            "merged": span.merged, "offset_ns": row.offset_ns,
            "duration_ns": span.duration_ns, "sections": _section_index(sections),
            "content": _content(sections, section, chunk)}
