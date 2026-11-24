"""Where a judgement lives: the user's own store, by the job it judges -
``<data>/judgements/<job uid>/<trial key>.json`` (each name spelled by
`key`; ``Job.uid``, ``Trial.key``) for System One's,
``<data>/judgements-llm/...`` for the LLM judge's (a trial has both,
apart, ``lane``), ``<data>/judgements-tasks/<dataset>/...`` for task
instructions - keyed by question version inside. ``<data>`` is
``home()``, the user data directory. Never in a job folder: it is its
producer's, and can be rewritten, pruned or read-only. So one judgement
shows in every workspace that holds the job, and deleting a workspace
leaves it.

Every function takes the store's base first: ``home()``, or a workspace's
folder for the store judgements were kept in before (``<workspace>/
judgements{,-llm}/<job id>/``), which is still read: `adopt` copies a job's
records from it, never deleting them.

Every version is kept, and under each every try of a retried trial: the
try judged last under the version, each earlier one under
``<version>@<its start>``. A read without a version asks for the current one
and falls back to the earlier ones the vocabulary still names
(``questions.EARLIER``), newest first, so an earlier judgement is still shown, and a write always lands under the version the record names."""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Optional

from nullius import ondisk, paths

from . import client, questions as Q

_SAFE = re.compile(r"[^A-Za-z0-9._@+-]")


ROOTS = {"decision": "judgements", "llm": "judgements-llm"}     # one directory per judge
TASKS = "task"                       # the lane of task-instruction judgements, by dataset (`task_dataset`)


def home() -> str:
    """The base of the user's own store: the user data directory."""
    return paths.data_dir()


def _root(base: str, lane: str) -> str:
    return os.path.join(base, "judgements-tasks" if lane == TASKS else ROOTS.get(lane, "judgements"))


def _locked(path: str):
    """One save at a time to a judgement file, across threads and processes: a save reads, merges and writes it whole."""
    return ondisk.locked(os.path.join(os.path.dirname(path), ".lock"))


def key(name: str) -> str:
    """A job id's or trial name's file name: the name itself when every
    character is safe, else its safe spelling and a digest of the name, so
    two names never share a file (no safe spelling holds a ``~``)."""
    safe = _SAFE.sub("_", name)
    return safe if safe == name else f"{safe}~{hashlib.sha256(name.encode('utf-8')).hexdigest()[:10]}"


def _names(name: str) -> tuple:
    """`key`, then the safe spelling alone that a store written before `key` used, still read."""
    return tuple(dict.fromkeys((key(name), _SAFE.sub("_", name))))


def _paths(base: str, job_id: str, trial_name: str, lane: str = "decision") -> list:
    """Where a judgement is written, then where an older store may hold it."""
    root = _root(base, lane)
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


def load(base: str, job_id: str, trial_name: str, version: Optional[str] = None, lane: str = "decision",
         started: Optional[str] = None) -> Optional[dict]:
    """The record under `version`; with none named, the current version or
    the newest earlier one that has a record. `started`: the trial's own
    start, so a record of an earlier try under its name is none of its."""
    if not job_id:                   # a job with no identity yet (Job.uid_source "pending") has no judgement
        return None
    return _pick(_read(_paths(base, job_id, trial_name, lane)), version, started)


def is_current(record: Optional[dict]) -> bool:
    """Whether a record was asked under the current vocabulary."""
    return bool(record) and record.get("questions") == Q.VERSION


def save(base: str, job_id: str, trial_name: str, record: dict, lane: str = "decision",
         started: Optional[str] = None) -> dict:
    """The record under its question version, returned as stored; other versions kept. A
    record's answers and hygiene readings merge over the version's earlier
    ones, so judging one question later does not drop the others. Saves to
    one file take turns, across processes; each writes through a temporary file of its own."""
    if not job_id:
        raise ValueError("a judgement needs the job's identity")
    found = _paths(base, job_id, trial_name, lane)
    path = found[0]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _locked(path):
        data = _read(found)
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
        ondisk.write_whole(path, json.dumps(data, indent=1), mode=0o600)
    return merged


def for_job(base: str, job_id: str, started: dict, version: Optional[str] = None, lane: str = "decision") -> dict:
    """The judged trials of a job: {trial_name: record} for each trial of
    `started` - {trial_name: its start}, every trial of the job - that has
    one, as `load` picks it."""
    out = {}
    for name, start in started.items():
        rec = load(base, job_id, name, version, lane, start)
        if rec:
            out[name] = rec
    return out


def job_dirs(base: str, job_ids) -> list:
    """The folders that hold these jobs' judgements, in every lane."""
    return [d for lane in ROOTS for j in job_ids if j for n in _names(j) if os.path.isdir(d := os.path.join(_root(base, lane), n))]


def count_in(dirs) -> int:
    """The judgements stored in these folders: one per trial and lane, its versions inside."""
    return sum(n.endswith(".json") for d in dirs for _, _, names in os.walk(d) for n in names)


def all_records(base: str, job_id: Optional[str] = None, lane: str = "decision"):
    """Every stored record, every version: yields (job_id, trial_name,
    version, record). One job's when `job_id` is given."""
    root = _root(base, lane)
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


def calibration(base: Optional[str]) -> Optional[dict]:
    """The judge's observed characters per token over the stored calls
    (`client.calibrate`); None with no store or no sample."""
    if not base:
        return None
    return client.calibrate(rec for _, _, _, rec in all_records(base))


def truncation_report(base: str, job_id: str, limit: Optional[int] = None) -> dict:
    """Which state kinds were cut to the limit over a job's stored calls,
    and how often - counted here. `kinds[kind]` is {calls, truncated,
    max_chars}, most-truncated first; `untyped` counts records that say
    `truncated` but carry no per-state sizes (v1), which cannot be placed."""
    from . import state as judge_state
    limit = judge_state.LIMIT if limit is None else limit
    kinds: dict = {}
    records = truncated = untyped = 0
    for _, _, _, rec in all_records(base, job_id):
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


def task_dataset(job) -> str:
    """Where a dataset's task-instruction judgements are kept (lane `TASKS`),
    so an instruction is judged once for every run of that dataset: its
    recorded path, or its registry name, so two datasets of one folder name
    keep apart; a job that names none, its own identity."""
    return job.dataset_path or job.dataset or "job:" + job.uid


def _digest(rec) -> str:
    return hashlib.sha256(json.dumps(rec, sort_keys=True).encode("utf-8")).hexdigest()[:10]


def _merge_into(path: str, data: dict) -> None:
    """Records of an older store into the file at `path`: each one not there
    yet is added. Where the file has another under its key, the one shown
    stays there and the other is kept beside: of two tries, the later try's
    shown and the earlier under its own start (`_try`); of one try, the
    newer by `asked_at` shown and the other under ``<key>~<its digest>``.
    Nothing is dropped, and adding the same record again changes nothing."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _locked(path):
        cur = _read([path])
        have = {_digest(r) for r in cur.values()}
        changed = False
        for k, rec in data.items():
            if not isinstance(rec, dict) or _digest(rec) in have:
                continue
            now = cur.get(k)
            if now is None:
                cur[k] = rec
            else:
                a, b = now.get("trial_started_at"), rec.get("trial_started_at")
                tries = bool(a and b and a != b)
                newer = str(b) > str(a) if tries else str(rec.get("asked_at") or "") > str(now.get("asked_at") or "")
                shown, other = (rec, now) if newer else (now, rec)
                slot = _try(k, other["trial_started_at"]) if tries else f"{k}~{_digest(other)}"
                if slot in cur:
                    slot = f"{slot}~{_digest(other)}"
                cur[k], cur[slot] = shown, other
            have |= {_digest(r) for r in cur.values()}
            changed = True
        if changed:
            ondisk.write_whole(path, json.dumps(cur, indent=1), mode=0o600)


def adopt(ws_dir: str, job, into: Optional[str] = None) -> None:
    """Copy what the workspace's store from before (`ws_dir`) holds for `job`
    - under its job id, its trials' and its dataset's task records - into
    the user's store (`into`, `home()` by default) under the job's identity.
    Two jobs of one id in that workspace each get the same records, as each
    showed them before. The workspace's files are left as they are."""
    if not job.uid:
        return
    into = into or home()
    names = {n: key(t.trial_name) for t in job.trials for n in _names(t.trial_name)}
    places = [(lane, j, job.uid, names) for lane in ROOTS for j in _names(job.job_id)]
    # before, a dataset's task records were a job of their own, named `task:<dataset>`, and a job naming none its id
    places += [(TASKS, j, task_dataset(job), {}) for j in _names("task:" + (job.dataset_path or job.dataset or "job:" + job.job_id))]
    for lane, old, new, rename in places:
        src = os.path.join(_root(ws_dir, "decision" if lane == TASKS else lane), old)
        try:
            files = sorted(f for f in os.listdir(src) if f.endswith(".json"))
        except OSError:
            continue
        for f in files:
            data = _read([os.path.join(src, f)])
            if data:
                _merge_into(os.path.join(_root(into, lane), key(new), rename.get(f[:-5], f[:-5]) + ".json"), data)
