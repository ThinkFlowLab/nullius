"""What a benchmark recorded about a trial that fell short: the Failure analysis tab's rows.

A benchmark's ``benchmark.json`` may name them (``evidence``), in order::

    {"label": "Expected answer",
     "path": "reward_info.reward",     a dotted path in the score's detail; an object gives a row per plain value
     "values": {"1.0": "Pass"},        a value as the page says it
     "names": {"DB": "Database"},      an object's keys as the page says them
     "unless": ["no"],                 values that say nothing: no row
     "reply": true}                    the trial's last reply when the detail holds no such value

An empty list shows nothing. A benchmark without the block shows its whole
detail (``parse.score_detail``)."""
from __future__ import annotations

from nullius.redact import redact

from . import parse

_CACHE: dict = {}


def declared(source) -> list | None:
    """The rows the benchmark of a trial's *source* names, or None when it names none."""
    from nullius.catalogue import catalog
    sig = catalog.signature()
    if _CACHE.get("sig") != sig:
        table = {}
        for b in catalog.load().benchmarks:
            if isinstance(b.evidence, list):
                rows = [r for r in b.evidence if catalog.valid_evidence(r)]
                for name in [b.key, *b.datasets, *((b.report or {}).get("sources") or [])]:
                    table.setdefault(str(name).strip().lower(), rows)
        _CACHE.update(sig=sig, table=table)
    return _CACHE["table"].get((source or "").strip().lower())


def _at(detail, path):
    """(found, value) at a dotted path; a key may hold dots itself."""
    if not isinstance(detail, dict):
        return False, None
    if path in detail:
        return True, detail[path]
    for i, c in enumerate(path):
        if c == "." and path[:i] in detail:
            found, value = _at(detail[path[:i]], path[i + 1:])
            if found:
                return True, value
    return False, None


def rows(trial_dir: str, spec: list, reply: str = "") -> list:
    """``{label, text}`` per row *spec* names that the trial's score has, redacted."""
    detail = parse.read_detail(trial_dir) or {}
    out = []

    def add(label, value, entry):
        text = parse._shown(value)
        if text not in entry.get("unless", ()):
            out.append({"label": redact(str(label)), "text": redact(entry.get("values", {}).get(text, text))})

    for entry in spec:
        found, value = _at(detail, entry["path"])
        if not found and entry.get("reply") and reply:
            found, value = True, reply
        if not found:
            continue
        if isinstance(value, dict):
            for key, v in value.items():
                if parse._plain(v):
                    add(entry.get("names", {}).get(key, key), v, entry)
        else:
            add(entry["label"], value, entry)
    return out
