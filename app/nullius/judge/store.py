"""Where a judgement lives: the workspace's own store, beside the parsed
jobs - ``<workspace>/judgements/<job_id>/<trial_name>.json`` (each name
spelled by `key`) for System
One's, ``<workspace>/judgements-llm/...`` for the LLM judge's (a trial has
both, apart, ``lane``) - keyed by question version inside. Never under the job's ``agent/`` directory: the
run tree is Harbor's, and a re-parse of the job does not touch this.

Every version is kept, and under each every try of a retried trial: the
try judged last under the version, each earlier one under
``<version>@<its start>``. A read without a version asks for the current one
and falls back to the earlier ones the vocabulary still names
(``questions.EARLIER``), newest first, so a judgement made under v1 is
still shown after the bump - under its own version, which the record
carries - and a write always lands under the version the record names."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from typing import Optional

from . import client, questions as Q

_SAFE = re.compile(r"[^A-Za-z0-9._@+-]")


_LOCKS: dict = {}                    # one per judgement file: a save reads, merges and writes it whole
_LOCKS_GUARD = threading.Lock()


def _lock(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


ROOTS = {"decision": "judgements", "llm": "judgements-llm"}     # one directory per judgement


def _root(ws_dir: str, lane: str) -> str:
    return os.path.join(ws_dir, ROOTS.get(lane, "judgements"))


def key(name: str) -> str:
    """A job id's or trial name's file name: the name itself when every
    character is safe, else its safe spelling and a digest of the name, so
    two names never share a file (no safe spelling holds a ``~``)."""
    safe = _SAFE.sub("_", name)
    return safe if safe == name else f"{safe}~{hashlib.sha256(name.encode('utf-8')).hexdigest()[:10]}"


def _names(name: str) -> tuple:
    """`key`, then the safe spelling alone that a store written before `key` used, still read."""
    return tuple(dict.fromkeys((key(name), _SAFE.sub("_", name))))


def _paths(ws_dir: str, job_id: str, trial_name: str, lane: str = "decision") -> list:
    """Where a judgement is written, then where an older store may hold it."""
    root = _root(ws_dir, lane)
    return [os.path.join(root, j, t + ".json") for j in _names(job_id) for t in _names(trial_name)]


def _read(paths) -> dict:
    """The first judgement file of `paths` there is, read; {} when there is none or it cannot be read."""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return {}
    return {}


def _versions(version: Optional[str]) -> tuple:
    return (version,) if version else (Q.VERSION, *Q.EARLIER)


def _of(rec: Optional[dict], started: Optional[str]) -> bool:
    """Whether a record judged the try that started at ``started``: a retry
    runs an attempt again under its trial's name, and what was asked of the
    try before is not this one's. A record from before tries were told
    apart, or a check that names no start, takes it as it is."""
    return not started or not rec or rec.get("trial_started_at") in (None, started)


def _try(version: str, started: str) -> str:
    """Where a version keeps an earlier try's record, beside the latest try's under `version`."""
    return f"{version}@{started}"


def _pick(data: dict, version: Optional[str], started: Optional[str] = None) -> Optional[dict]:
    for v in _versions(version):
        for rec in (data.get(v), started and data.get(_try(v, started))):
            if rec and _of(rec, started):
                return rec
    return None


def load(ws_dir: str, job_id: str, trial_name: str, version: Optional[str] = None, lane: str = "decision",
         started: Optional[str] = None) -> Optional[dict]:
    """The record under `version`; with none named, the current version or
    the newest earlier one that has a record. `started`: the trial's own
    start, so a record of an earlier try under its name is none of its."""
    return _pick(_read(_paths(ws_dir, job_id, trial_name, lane)), version, started)


def is_current(record: Optional[dict]) -> bool:
    """Whether a record was asked under the current vocabulary."""
    return bool(record) and record.get("questions") == Q.VERSION


def save(ws_dir: str, job_id: str, trial_name: str, record: dict, lane: str = "decision",
         started: Optional[str] = None) -> dict:
    """The record under its question version, returned as stored; other versions kept. A
    record's answers and hygiene readings merge over the version's earlier
    ones, so judging one question later does not drop the others. Saves to
    one file take turns; each writes through a temporary file of its own."""
    paths = _paths(ws_dir, job_id, trial_name, lane)
    path = paths[0]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _lock(path):
        data = _read(paths)
        version = record.get("questions", Q.VERSION)
        earlier = data.get(version) or {}
        if not _of(earlier, started):        # another try's: kept beside, and none of its answers are this one's
            data[_try(version, earlier["trial_started_at"])] = earlier
            earlier = data.pop(_try(version, started), None) or {}
        merged = dict(record, **({"trial_started_at": started} if started else {}))
        merged["answers"] = {**(earlier.get("answers") or {}), **(record.get("answers") or {})}
        if "hygiene" in earlier or "hygiene" in record:
            merged["hygiene"] = {**(earlier.get("hygiene") or {}), **(record.get("hygiene") or {})}
        data[version] = merged
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    return merged


def for_job(ws_dir: str, job_id: str, started: dict, version: Optional[str] = None, lane: str = "decision") -> dict:
    """The judged trials of a job: {trial_name: record} for each trial of
    `started` - {trial_name: its start}, every trial of the job - that has
    one, as `load` picks it. Nothing else under the job's name is a trial of
    it: a v1 pair record is not."""
    out = {}
    for name, start in started.items():
        rec = load(ws_dir, job_id, name, version, lane, start)
        if rec:
            out[name] = rec
    return out


def job_dirs(ws_dir: str, job_ids) -> list:
    """The folders that hold these jobs' judgements, in every lane."""
    return [d for lane in ROOTS for j in job_ids for n in _names(j) if os.path.isdir(d := os.path.join(_root(ws_dir, lane), n))]


def count(ws_dir: str) -> int:
    """The stored judgements of a workspace, every lane: one per trial and
    lane, its versions inside. What deleting the workspace loses for good."""
    return sum(n.endswith(".json") for lane in ROOTS for _, _, names in os.walk(_root(ws_dir, lane)) for n in names)


def all_records(ws_dir: str, job_id: Optional[str] = None, lane: str = "decision"):
    """Every stored record, every version: yields (job_id, trial_name,
    version, record). One job's when `job_id` is given."""
    root = _root(ws_dir, lane)
    try:
        jobs = list(_names(job_id)) if job_id else sorted(os.listdir(root))
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
