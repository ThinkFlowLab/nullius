"""Trace fixtures the backend tests build on.

Fixture construction, not tests: the trace, chart and aggregate suites build
their spans with these, against the reader in `app/nullius/traces/trace.py`.

`trace_rich.json` is a verbatim Harbor claude-code capture; `trace_degraded.json`
is a capture with most of its timing missing, which is what the coverage strip
and the `inferred` banner exist for.
"""
from __future__ import annotations

import json

from . import FIXTURES

RICH = str(FIXTURES / "trace_rich.json")
DEGRADED = str(FIXTURES / "trace_degraded.json")


def raw(path=RICH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def span(sid, start=1_000, end=2_000, **fields):
    """A minimal span. Only what the format requires, so tests add what they mean."""
    out = {
        "span_id": sid,
        "trace_id": "t",
        "name": sid,
        "kind": "tool",
        "start_ns": start,
        "timing_quality": "measured",
        "source": "otel",
        "attributes": {},
        "children": [],
    }
    if end is not None:
        out["end_ns"] = end
        out["duration_ns"] = end - start
    out.update(fields)
    return out


def doc(*spans, **top):
    out = {"schema_version": "harbor-trace-v1", "spans": list(spans)}
    out.update(top)
    return out
