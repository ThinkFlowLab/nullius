"""Where a judgement lives: the workspace's own store, beside the parsed
jobs - ``<workspace>/judgements/<job_id>/<trial_name>.json`` for System
One's, ``<workspace>/judgements-llm/...`` for the LLM judge's (a trial has
both, apart, ``lane``) - keyed by question version inside. Never under the job's ``agent/`` directory: the
run tree is Harbor's, and a re-parse of the job does not touch this.

Every version is kept. A read without a version asks for the current one
and falls back to the earlier ones the vocabulary still names
(``questions.EARLIER``), newest first, so a judgement made under v1 is
still shown after the bump - under its own version, which the record
carries - and a write always lands under the version the record names."""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from . import client, questions as Q

_SAFE = re.compile(r"[^A-Za-z0-9._@+-]")


ROOTS = {"system-one": "judgements", "llm": "judgements-llm"}     # one directory per judgement


def _root(ws_dir: str, lane: str) -> str:
    return os.path.join(ws_dir, ROOTS.get(lane, "judgements"))


def _path(ws_dir: str, job_id: str, trial_name: str, lane: str = "system-one") -> str:
    return os.path.join(_root(ws_dir, lane), _SAFE.sub("_", job_id), _SAFE.sub("_", trial_name) + ".json")


def _versions(version: Optional[str]) -> tuple:
    return (version,) if version else (Q.VERSION, *Q.EARLIER)


def _pick(data: dict, version: Optional[str]) -> Optional[dict]:
    for v in _versions(version):
        rec = data.get(v)
        if rec:
            return rec
    return None


def load(ws_dir: str, job_id: str, trial_name: str, version: Optional[str] = None, lane: str = "system-one") -> Optional[dict]:
    """The record under `version`; with none named, the current version or
    the newest earlier one that has a record."""
    try:
        with open(_path(ws_dir, job_id, trial_name, lane), encoding="utf-8") as fh:
            return _pick(json.load(fh) or {}, version)
    except (OSError, ValueError):
        return None


def is_current(record: Optional[dict]) -> bool:
    """Whether a record was asked under the current vocabulary."""
    return bool(record) and record.get("questions") == Q.VERSION


def save(ws_dir: str, job_id: str, trial_name: str, record: dict, lane: str = "system-one") -> None:
    """The record under its question version; other versions kept. A
    record's answers merge over the version's earlier answers, so judging
    one question later does not drop the others."""
    path = _path(ws_dir, job_id, trial_name, lane)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (OSError, ValueError):
        data = {}
    version = record.get("questions", Q.VERSION)
    earlier = data.get(version) or {}
    merged = dict(record)
    merged["answers"] = {**(earlier.get("answers") or {}), **(record.get("answers") or {})}
    data[version] = merged
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def for_job(ws_dir: str, job_id: str, version: Optional[str] = None, lane: str = "system-one") -> dict:
    """Every judged trial of a job: {trial_name: record}, each the current
    version's record or the newest earlier one, as `load` picks it."""
    d = os.path.join(_root(ws_dir, lane), _SAFE.sub("_", job_id))
    out = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                rec = _pick(json.load(fh) or {}, version)
        except (OSError, ValueError):
            continue
        if rec:
            out[n[:-5]] = rec
    return out


def all_records(ws_dir: str, job_id: Optional[str] = None, lane: str = "system-one"):
    """Every stored record, every version: yields (job_id, trial_name,
    version, record). One job's when `job_id` is given."""
    root = _root(ws_dir, lane)
    try:
        jobs = [_SAFE.sub("_", job_id)] if job_id else sorted(os.listdir(root))
    except OSError:
        return
    for j in jobs:
        d = os.path.join(root, j)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, n), encoding="utf-8") as fh:
                    data = json.load(fh) or {}
            except (OSError, ValueError):
                continue
            for version, rec in data.items():
                if isinstance(rec, dict):
                    yield j, n[:-5], version, rec


def calibration(ws_dir: Optional[str]) -> Optional[dict]:
    """The judge's observed characters per token over this workspace's
    stored calls (`client.calibrate`); None with no workspace or no sample."""
    if not ws_dir:
        return None
    return client.calibrate(rec for _, _, _, rec in all_records(ws_dir))


def truncation_report(ws_dir: str, job_id: str, limit: Optional[int] = None) -> dict:
    """Which state kinds were cut to the limit over a job's stored calls,
    and how often - counted here. `kinds[kind]` is {calls, truncated,
    max_chars}, most-truncated first; `untyped` counts records that say
    `truncated` but carry no per-state sizes (v1), which cannot be placed."""
    from . import state as judge_state
    limit = judge_state.LIMIT if limit is None else limit
    kinds: dict = {}
    records = truncated = untyped = 0
    for _, _, _, rec in all_records(ws_dir, job_id):
        records += 1
        if rec.get("truncated"):
            truncated += 1
        entries = rec.get("states") or {}
        if not entries and rec.get("truncated"):
            untyped += 1
        for kind, e in entries.items():
            k = kinds.setdefault(kind, {"calls": 0, "truncated": 0, "max_chars": 0})
            k["calls"] += 1
            k["truncated"] += 1 if e.get("truncated") else 0
            k["max_chars"] = max(k["max_chars"], e.get("chars") or 0)
    return {"limit": limit, "records": records, "truncated": truncated, "untyped": untyped,
            "kinds": dict(sorted(kinds.items(), key=lambda kv: (-kv[1]["truncated"], kv[0])))}


def pair_key(a_trial: str, b_trial: str) -> str:
    return "pair:" + _SAFE.sub("_", a_trial) + "__vs__" + _SAFE.sub("_", b_trial)
