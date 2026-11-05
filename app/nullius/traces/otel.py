"""OpenTelemetry span exports (OTLP/JSON), read as ``harbor-trace-v1`` traces.

A production trace export and Harbor's ``agent/trace.json`` describe the same
thing — spans with clocks and parentage — in two encodings. This reads the
first and writes the second, so a trace that never went through a benchmark
opens on the trial page and pools on ``/charts`` exactly as a recorded trial
does. Nothing here measures anything: every clock, id and count comes off the
wire document, and what the document does not say stays absent.

**Supported input format.** OTLP/JSON as the OpenTelemetry protocol 1.x JSON
encoding of ``ExportTraceServiceRequest`` — a top-level ``resourceSpans`` list,
each with ``resource.attributes``, ``scopeSpans[].scope`` and
``scopeSpans[].spans[]`` carrying ``traceId``, ``spanId``, ``parentSpanId``,
``name``, ``kind``, ``startTimeUnixNano``, ``endTimeUnixNano``, ``attributes``,
``events`` and ``status``. Ids are the hex strings OTLP/JSON specifies and are
kept opaque; a base64 id would be kept opaque too. A file holds one request as
a JSON object, or one per line (what the collector's ``file`` exporter writes),
and nothing else. A ``schemaUrl`` is accepted when it is
an ``opentelemetry.io/schemas/1.*`` URL and refused otherwise. OTLP protobuf,
Jaeger and Zipkin JSON, and live collectors are out of scope.

**Semantic conventions.** OpenInference 0.1.x attributes are read where present,
and so are the Langfuse observation attributes (``langfuse.observation.*``) a
Langfuse-instrumented application's spans carry. Every other attribute is kept
verbatim, which is what keeps a generic span — no LLM attributes at all —
inspectable: its name, clock, parent, status, events, resource and attributes
all reach the detail pane.

Mapping, input → the fields the trial page reads
    spanId / parentSpanId / traceId      span_id / parent_span_id / trace_id
    name                                 name (and the label, unless tool.name)
    startTimeUnixNano                    start_ns; nanoseconds, kept exact
    endTimeUnixNano                      end_ns and duration_ns; a missing or
                                         zero end is *incomplete*: no end, no
                                         duration, timing_quality ``unknown``
                                         and ``otel.incomplete: true``
    parent not in this trace             kept as a root with ``otel.orphan:
                                         true``; parent_span_id stays as recorded
    status.code ERROR                    ``otel.status: ERROR`` plus
                                         ``otel.status_message``; never an
                                         outcome — see below
    events[]                             kept under ``events`` (name, time_ns,
                                         attributes), summarised in
                                         ``otel.events``; the first ``exception``
                                         event's type and message are copied to
                                         ``otel.exception.type`` / ``.message``
    kind (OTLP SpanKind)                 ``otel.span_kind`` (INTERNAL, CLIENT, …)
    resource.attributes                  merged into every span's attributes
                                         (span attributes win on a clash)
    scope.name / scope.version           ``otel.scope.name`` / ``otel.scope.version``
    openinference.span.kind              LLM → llm_request, TOOL → tool, AGENT →
                                         session at a root and subagent below;
                                         CHAIN, RETRIEVER, EMBEDDING, RERANKER,
                                         GUARDRAIL, EVALUATOR keep their
                                         lower-cased name; no kind → ``span``
    langfuse.observation.type            generation → llm_request, tool → tool,
                                         agent → as AGENT above; others as-is
    llm.model_name                       kept; the importer reads the first one
                                         as the trial's model
    llm.token_count.prompt / completion  tokens.input_tokens / output_tokens
    llm.token_count.total                tokens.total_tokens
    llm.token_count.prompt_details.cache_read / cache_write
                                         tokens.cache_read_tokens / cache_write_tokens
    llm.token_count.completion_details.reasoning
                                         tokens.reasoning_tokens
    langfuse.observation.usage_details   input / output / input_cached_tokens /
                                         total → the same four token fields
    input.value / output.value           content.input / content.output on an LLM
                                         or generic span, content.arguments /
                                         content.result on a tool span; clipped
                                         as clip() cuts it
    tool.parameters                      content.arguments when a tool span has
                                         no input.value
    langfuse.observation.input / output  as input.value / output.value
    tool.name                            ``tool_name``, which is what the label
                                         prefers over the span name

Usage is read **per span, on LLM spans only** (``llm_request`` after the kind
mapping), and ``totals.tokens`` is the sum over those spans. A container that
carries aggregated counts of its own — an AGENT or CHAIN span some
instrumentations stamp with the sum of their children — keeps them as
attributes, verbatim, and they are not read as usage: reading them would count
every call twice. A count the span does not carry is absent, never zero.

``totals`` is computed once, here, by the trial page's own partition
(:func:`trace.breakdown`): every instant charged to one category at priority
idle > tool > reasoning > llm, so nested and concurrent spans never sum past
the clock. ``wall_ns`` is the trace's own window — first start to last end —
which is what the coverage strip then compares the spans against. Spans are
``timing_quality: measured``: these are the producer's clocks, not this
reader's inference. Spans are ordered by start, out-of-order input included.

**What is not mapped, and what is lost.** OTLP ``links``, ``droppedAttributesCount``
and its siblings, ``traceState`` and ``flags`` are not read. The OpenTelemetry
GenAI conventions (``gen_ai.*``), OpenInference message lists
(``llm.input_messages.*``), embeddings, retrieval documents and
``llm.invocation_parameters`` are kept as attributes and not interpreted. Usage
on a non-LLM span is not totalled (above). Content is clipped, and an export
that was itself clipped is clipped again. Nothing here says whether the trace *succeeded*: a benchmark outcome is
the verifier's to record, so a span in ERROR marks that span and the trial's
outcome stays unknown.

Redaction applies on the way in — attribute values, event attributes, status
messages, content and this reader's own error messages — because the importer
writes the result into a workspace. Stdlib only; no Flask.
"""

from __future__ import annotations

import base64
import json
import os
import re

from nullius.redact import redact
from nullius.traces import trace

DEFAULT_MAX_CONTENT = 2000


def clip(text, limit: int) -> str:
    """A span's text, cut at ``limit`` characters with the rest counted."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    if limit and len(text) > limit:
        return text[:limit] + f"\n… [{len(text) - limit} more characters]"
    return text

SCHEMA = "harbor-trace-v1"
PRODUCER = "app/nullius/traces/otel.py"

# What this reader is written against. Named in the document it writes and in
# the fixtures' README, so a claim of support can be checked against a version.
FORMAT = "OTLP/JSON ExportTraceServiceRequest, opentelemetry-proto 1.x"
CONVENTIONS = "OpenInference semantic conventions 0.1.x"

# The schema URL OTLP lets a resource or scope carry. Only the 1.x line is
# claimed; anything else names a convention set this reader was not written for.
_SCHEMA_URL_RE = re.compile(r"^https://opentelemetry\.io/schemas/1\.\d+(\.\d+)?$")

# The span kinds the trial page renders (`trace.KIND_ORDER`) for the kinds the
# two conventions name. A kind absent here keeps its lower-cased name — the
# page paints an unknown kind neutral rather than dropping it.
_OPENINFERENCE_KINDS = {"LLM": "llm_request", "TOOL": "tool"}
_LANGFUSE_TYPES = {"generation": "llm_request", "tool": "tool"}
_AGENT_KINDS = ("AGENT", "agent")
GENERIC_KIND = "span"

# OTLP SpanKind, by number and by enum name; both spellings are legal JSON.
_SPAN_KINDS = ("UNSPECIFIED", "INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER")
_STATUS_CODES = ("UNSET", "OK", "ERROR")

# OpenInference token attributes → the token fields the views read.
_TOKEN_ATTRS = (
    ("llm.token_count.prompt", "input_tokens"),
    ("llm.token_count.completion", "output_tokens"),
    ("llm.token_count.total", "total_tokens"),
    ("llm.token_count.prompt_details.cache_read", "cache_read_tokens"),
    ("llm.token_count.prompt_details.cache_write", "cache_write_tokens"),
    ("llm.token_count.completion_details.reasoning", "reasoning_tokens"),
)
# Langfuse `usage_details` keys → the same fields.
_LANGFUSE_USAGE = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("input_cached_tokens", "cache_read_tokens"),
    ("total", "total_tokens"),
)


class ReadError(ValueError):
    """A file this reader refuses. The message is already redacted."""



def _int_or_none(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def any_value(wire):
    """One OTLP ``AnyValue`` as a plain JSON value; strings redacted."""
    if not isinstance(wire, dict):
        return None
    if "stringValue" in wire:
        return redact(str(wire["stringValue"]))
    if "intValue" in wire:
        parsed = _int_or_none(wire["intValue"])
        return parsed if parsed is not None else redact(str(wire["intValue"]))
    if "doubleValue" in wire:
        try:
            return float(wire["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "boolValue" in wire:
        return bool(wire["boolValue"])
    if "arrayValue" in wire:
        values = (wire["arrayValue"] or {}).get("values") or []
        return [any_value(v) for v in values if isinstance(v, dict)]
    if "kvlistValue" in wire:
        return attributes_of((wire["kvlistValue"] or {}).get("values") or [])
    if "bytesValue" in wire:
        try:
            raw = base64.b64decode(str(wire["bytesValue"]), validate=True)
        except (ValueError, TypeError):
            return redact(str(wire["bytesValue"]))
        return f"<{len(raw)} bytes>"
    return None


def attributes_of(wire) -> dict:
    """An OTLP ``KeyValue`` list as a dict. Keys are instrumentation names."""
    out: dict = {}
    for item in wire or []:
        if not isinstance(item, dict) or not item.get("key"):
            continue
        out[str(item["key"])] = any_value(item.get("value"))
    return out



def _requests_in(doc, where: str) -> list:
    """The ``ExportTraceServiceRequest`` objects one decoded document holds."""
    if isinstance(doc, dict) and isinstance(doc.get("resourceSpans"), list):
        return [doc]
    keys = sorted(doc)[:8] if isinstance(doc, dict) else type(doc).__name__
    raise ReadError(redact(f"{where}: not an OTLP/JSON trace export "
                           f"(no resourceSpans; top level: {keys})"))


def _check_schema(request: dict, where: str) -> None:
    for block in request.get("resourceSpans") or []:
        if not isinstance(block, dict):
            continue
        urls = [block.get("schemaUrl")] + [
            s.get("schemaUrl") for s in block.get("scopeSpans") or [] if isinstance(s, dict)]
        for url in urls:
            if url and not _SCHEMA_URL_RE.match(str(url)):
                raise ReadError(redact(
                    f"{where}: unsupported schema {url!s}; this reader is written "
                    f"against {FORMAT}"))


def read_file(path: str) -> list:
    """Every request in one file — a JSON object, or one object per line.

    Refuses, with a redacted message, what it cannot read as OTLP/JSON: bad
    JSON, a document without ``resourceSpans``, a schema URL outside the 1.x
    line. Refusing is the whole contract — a file this half-reads would render
    as a trace with spans missing and nothing saying so.
    """
    where = os.path.basename(path)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ReadError(redact(f"{where}: {exc.strerror or exc}")) from None
    try:
        docs = [json.loads(text)]
    except ValueError as whole:
        lines = [line for line in text.splitlines() if line.strip()]
        try:
            docs = [json.loads(line) for line in lines] if lines else []
        except ValueError:
            raise ReadError(redact(f"{where}: not JSON ({whole})")) from None
        if not docs:
            raise ReadError(redact(f"{where}: not JSON ({whole})")) from None
    requests = [req for doc in docs for req in _requests_in(doc, where)]
    for req in requests:
        _check_schema(req, where)
    return requests


def looks_like_export(path: str) -> bool:
    """Cheap first look: the key has to be in the file's head for it to be one."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return b"resourceSpans" in head



def flat_spans(request: dict) -> list:
    """Every span in a request as a flat record carrying its resource and scope.

    The record keeps the wire span under ``span`` and the decoded, redacted
    resource and scope beside it; :func:`build` does the mapping.
    """
    out = []
    for block in request.get("resourceSpans") or []:
        if not isinstance(block, dict):
            continue
        resource = attributes_of((block.get("resource") or {}).get("attributes"))
        for scoped in block.get("scopeSpans") or []:
            if not isinstance(scoped, dict):
                continue
            scope = scoped.get("scope") or {}
            for span in scoped.get("spans") or []:
                if isinstance(span, dict):
                    out.append({"span": span, "resource": resource,
                                "scope": {"name": str(scope.get("name") or ""),
                                          "version": str(scope.get("version") or "")}})
    return out


def traces_of(records: list) -> dict:
    """Flat records grouped by trace id, in first-seen order."""
    out: dict = {}
    for rec in records:
        tid = str(rec["span"].get("traceId") or "")
        out.setdefault(tid, []).append(rec)
    return out



def _enum(value, names: tuple) -> str:
    """An OTLP enum, given as its number or its ``PREFIX_NAME`` string."""
    number = _int_or_none(value)
    if number is not None:
        return names[number] if 0 <= number < len(names) else str(number)
    text = str(value or "").upper()
    for prefix in ("SPAN_KIND_", "STATUS_CODE_"):
        text = text[len(prefix):] if text.startswith(prefix) else text
    return text or names[0]


def _kind_of(attributes: dict, is_root: bool) -> str:
    """The trial page's kind for a span, from whichever convention names one."""
    oi = attributes.get("openinference.span.kind")
    lf = attributes.get("langfuse.observation.type")
    for raw, table in ((oi, _OPENINFERENCE_KINDS), (lf, _LANGFUSE_TYPES)):
        if not isinstance(raw, str) or not raw.strip():
            continue
        raw = raw.strip()
        if raw in _AGENT_KINDS:
            return "session" if is_root else "subagent"
        return table.get(raw, raw.lower())
    return GENERIC_KIND


def _tokens_of(attributes: dict) -> dict:
    """Usage from the OpenInference counts, else from Langfuse's usage_details."""
    tokens = {}
    for key, field in _TOKEN_ATTRS:
        value = _int_or_none(attributes.get(key))
        if value is not None:
            tokens[field] = value
    if tokens:
        return tokens
    usage = attributes.get("langfuse.observation.usage_details")
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except ValueError:
            usage = None
    if isinstance(usage, dict):
        for key, field in _LANGFUSE_USAGE:
            value = _int_or_none(usage.get(key))
            if value is not None:
                tokens[field] = value
    return tokens


def _text(value, limit: int) -> str:
    if value is None or value == "":
        return ""
    return clip(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), limit)


def _content_of(attributes: dict, kind: str, limit: int) -> dict:
    """input/output on a call or generic span, arguments/result on a tool."""
    inp = attributes.get("input.value")
    if inp is None:
        inp = attributes.get("langfuse.observation.input")
    if inp is None and kind == "tool":
        inp = attributes.get("tool.parameters")
    out = attributes.get("output.value")
    if out is None:
        out = attributes.get("langfuse.observation.output")
    fields = ("arguments", "result") if kind == "tool" else ("input", "output")
    content = {}
    for field, value in zip(fields, (inp, out)):
        text = _text(value, limit)
        if text:
            content[field] = text
    return content


def _events_of(wire: list) -> list:
    out = []
    for ev in wire or []:
        if not isinstance(ev, dict):
            continue
        out.append({
            "name": redact(str(ev.get("name") or "")),
            "time_ns": _int_or_none(ev.get("timeUnixNano")),
            "attributes": attributes_of(ev.get("attributes")),
        })
    return out


def _span_of(rec: dict, is_root: bool, present: set, limit: int) -> dict:
    """One wire span as a ``harbor-trace-v1`` span, children still empty."""
    wire = rec["span"]
    attributes = {**rec["resource"]}
    if rec["scope"]["name"]:
        attributes["otel.scope.name"] = rec["scope"]["name"]
    if rec["scope"]["version"]:
        attributes["otel.scope.version"] = rec["scope"]["version"]
    attributes.update(attributes_of(wire.get("attributes")))
    attributes["otel.span_kind"] = _enum(wire.get("kind"), _SPAN_KINDS)

    status = wire.get("status") or {}
    code = _enum(status.get("code"), _STATUS_CODES) if isinstance(status, dict) else "UNSET"
    attributes["otel.status"] = code
    if isinstance(status, dict) and status.get("message"):
        attributes["otel.status_message"] = redact(str(status["message"]))

    events = _events_of(wire.get("events"))
    start_ns = _int_or_none(wire.get("startTimeUnixNano")) or 0
    if events:
        attributes["otel.events"] = "; ".join(
            f"{ev['name']} +{(ev['time_ns'] - start_ns) // trace.NS_PER_MS}ms"
            if ev["time_ns"] is not None else ev["name"] for ev in events)
        exc = next((ev for ev in events if ev["name"] == "exception"), None)
        if exc:
            for key in ("exception.type", "exception.message"):
                if exc["attributes"].get(key):
                    attributes["otel." + key] = exc["attributes"][key]

    kind = _kind_of(attributes, is_root)
    tool = attributes.get("tool.name")
    if isinstance(tool, str) and tool.strip():
        attributes["tool_name"] = tool.strip()

    parent = str(wire.get("parentSpanId") or "")
    if parent and parent not in present:
        attributes["otel.orphan"] = True

    span = {
        "span_id": str(wire.get("spanId") or ""),
        "trace_id": str(wire.get("traceId") or ""),
        "name": redact(str(wire.get("name") or "")),
        "kind": kind,
        "start_ns": start_ns,
        "timing_quality": "measured",
        "source": "otel",
        "attributes": attributes,
        "children": [],
    }
    if parent:
        span["parent_span_id"] = parent
    end_ns = _int_or_none(wire.get("endTimeUnixNano"))
    if end_ns:
        span["end_ns"] = end_ns
        span["duration_ns"] = end_ns - start_ns
    else:
        attributes["otel.incomplete"] = True
        span["timing_quality"] = "unknown"
    if kind == "llm_request":
        tokens = _tokens_of(attributes)
        if tokens:
            span["tokens"] = tokens
    content = _content_of(attributes, kind, limit)
    if content:
        span["content"] = content
    if events:
        span["events"] = events
    return span



def _order(spans: list) -> list:
    return sorted(spans, key=lambda s: (s["start_ns"], s["span_id"]))


def build(trace_id: str, records: list, limit: int = DEFAULT_MAX_CONTENT,
          files: tuple = ()) -> dict:
    """One trace's records as a ``harbor-trace-v1`` document.

    Spans are ordered by start at every level; a span whose parent is not in
    the trace becomes a root marked orphan; a span id seen twice keeps its
    first occurrence and the duplicate is noted, since the page keys rows by id
    and would otherwise silently drop one of them.
    """
    notes: list = []
    present = {str(r["span"].get("spanId") or "") for r in records}
    by_id: dict = {}
    order: list = []
    for rec in records:
        sid = str(rec["span"].get("spanId") or "")
        parent = str(rec["span"].get("parentSpanId") or "")
        is_root = not parent or parent not in present
        if sid in by_id:
            notes.append(f"span id {sid} appears twice; the first occurrence is kept")
            continue
        by_id[sid] = _span_of(rec, is_root, present, limit)
        order.append(sid)

    roots = []
    for sid in order:
        span = by_id[sid]
        parent = span.get("parent_span_id", "")
        if parent and parent in by_id and parent != sid:
            by_id[parent]["children"].append(span)
        else:
            roots.append(span)
    # A parent cycle (a under b under a) leaves both spans off every root. JSON
    # cannot express one in the tree this writes, so the loop is broken here:
    # whatever no root reaches is surfaced as an orphan root, not lost.
    reached: set = set()
    stack = list(roots)
    while stack:
        span = stack.pop()
        if span["span_id"] in reached:
            continue
        reached.add(span["span_id"])
        stack.extend(span["children"])
    for sid in order:
        if sid not in reached:
            span = by_id[sid]
            by_id[span["parent_span_id"]]["children"].remove(span)
            span["attributes"]["otel.orphan"] = True
            notes.append(f"span {sid} sits in a parent cycle; kept as a root")
            roots.append(span)
            reached.update(s["span_id"] for s in trace.raw_spans_of([span]))
    for span in by_id.values():
        span["children"] = _order(span["children"])
    roots = _order(roots)

    starts = [s["start_ns"] for s in by_id.values()]
    ends = [s["end_ns"] for s in by_id.values() if "end_ns" in s]
    wall_ns = (max(ends) - min(starts)) if starts and ends and max(ends) > min(starts) else 0
    orphans = sum(1 for s in by_id.values() if s["attributes"].get("otel.orphan"))
    incomplete = sum(1 for s in by_id.values() if s["attributes"].get("otel.incomplete"))
    if orphans:
        notes.append(f"{orphans} span(s) name a parent this trace does not carry; kept as roots")
    if incomplete:
        notes.append(f"{incomplete} span(s) have no end; kept without a duration")

    root_name = roots[0]["name"] if roots else ""
    resource = records[0]["resource"] if records else {}
    return {
        "schema_version": SCHEMA,
        "producer": PRODUCER,
        "trial_name": root_name or trace_id,
        "session_id": trace_id,
        "agent": {
            "name": str(resource.get("service.name") or ""),
            "version": str(resource.get("service.version") or ""),
        },
        "clock": {
            "source": "span start/end (Unix epoch nanoseconds, as exported)",
            "note": "wall_ns is the trace's own window, first start to last end",
        },
        "spans": roots,
        # The trial page's partition, run once here: idle > tool > reasoning >
        # llm, every instant charged to one category. Tokens are the sum over
        # LLM spans, the only ones usage was read on.
        "totals": trace.breakdown(roots, wall_ns).as_totals(),
        "import": {
            "format": FORMAT,
            "conventions": CONVENTIONS,
            "trace_id": trace_id,
            "files": list(files),
            "notes": notes,
        },
    }


def model_of(document: dict) -> str:
    """The first LLM span's model name, or "" — what the importer shows."""
    for raw in trace.raw_spans_of(document.get("spans") or []):
        if raw.get("kind") != "llm_request":
            continue
        attrs = raw.get("attributes") or {}
        for key in ("llm.model_name", "langfuse.observation.model.name", "gen_ai.request.model"):
            if isinstance(attrs.get(key), str) and attrs[key].strip():
                return attrs[key].strip()
    return ""


def read(paths: list, limit: int = DEFAULT_MAX_CONTENT) -> list:
    """Every trace across *paths*, one document each, in first-seen order.

    A trace may be split across files (a batch per export), which is why the
    files are read together and grouped afterwards. A file that does not read
    raises :class:`ReadError` — the caller decides whether that is fatal.
    """
    records = []
    where: dict = {}
    for path in paths:
        for request in read_file(path):
            for rec in flat_spans(request):
                records.append(rec)
                where.setdefault(str(rec["span"].get("traceId") or ""), []).append(
                    os.path.basename(path))
    return [
        build(tid, recs, limit, tuple(sorted(set(where.get(tid, ())))))
        for tid, recs in traces_of(records).items()
    ]
