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
judgements{,-llm}/<job id>/``). Until `move` has copied every workspace's
into the user's store, a read merges a job's older records in memory
(`overlay`, passed as ``old``) and writes nothing; a save copies in those of
the file it writes (`adopt`). The older files are never deleted or rewritten.

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
ADOPTED = "~adopted"                 # in a file: the digests of the older records copied in, so none is copied twice
MOVED = ".moved"                     # in ``<home>/judgements/``: every workspace's older records are in the user's store


class Unreadable(client.JudgeError, ValueError):
    """A judgement file of the user's store that cannot be read: left as it is, never written over."""
    def __init__(self, path: str):
        super().__init__(0, f"{path} cannot be read; it is left as it is. Repair or remove it, then try again.")


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


def _read(paths, strict: bool = False) -> dict:
    """The first judgement file of `paths` there is, read; {} when there is none. One that
    cannot be read, or holds no JSON object: {}, or `Unreadable` when `strict` (a write)."""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            data = False
        if data is None:
            return {}
        if not isinstance(data, dict):
            if strict:
                raise Unreadable(path)
            return {}
        return data
    return {}


def _with(data: dict, old) -> dict:
    """`data` with the older records `old` (a list of files' contents) merged in, in memory (`_merged`)."""
    for d in old or ():
        data = _merged(data, d)
    return data


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
         started: Optional[str] = None, old: Optional[dict] = None) -> Optional[dict]:
    """The record under `version`; with none named, the current version or
    the newest earlier one that has a record. `started`: the trial's own
    start, so a record of an earlier try under its name is none of its.
    `old`: the job's older records (`overlay`), read with it."""
    if not job_id:                   # a job with no identity yet (Job.uid_source "pending") has no judgement
        return None
    found = _paths(base, job_id, trial_name, lane)
    return _pick(_with(_read(found), (old or {}).get(found[0])), version, started)


def is_current(record: Optional[dict]) -> bool:
    """Whether a record was asked under the current vocabulary."""
    return bool(record) and record.get("questions") == Q.VERSION


def save(base: str, job_id: str, trial_name: str, record: dict, lane: str = "decision",
         started: Optional[str] = None, old: Optional[dict] = None) -> dict:
    """The record under its question version, returned as stored; other versions kept. A
    record's answers and hygiene readings merge over the version's earlier
    ones, so judging one question later does not drop the others. Saves to
    one file take turns, across processes; each writes through a temporary file of its own.
    `old`: the job's older records (`overlay`), those of this file copied in first.
    `Unreadable` when the file is there and cannot be read: nothing is written."""
    if not job_id:
        raise ValueError("a judgement needs the job's identity")
    found = _paths(base, job_id, trial_name, lane)
    path = found[0]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _locked(path):
        data = _with(_read(found, strict=True), (old or {}).get(path))
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


def for_job(base: str, job_id: str, started: dict, version: Optional[str] = None, lane: str = "decision",
            old: Optional[dict] = None) -> dict:
    """The judged trials of a job: {trial_name: record} for each trial of
    `started` - {trial_name: its start}, every trial of the job - that has
    one, as `load` picks it."""
    out = {}
    for name, start in started.items():
        rec = load(base, job_id, name, version, lane, start, old)
        if rec:
            out[name] = rec
    return out


def job_dirs(base: str, job_ids) -> list:
    """The folders that hold these jobs' judgements, in every lane."""
    return [d for lane in ROOTS for j in job_ids if j for n in _names(j) if os.path.isdir(d := os.path.join(_root(base, lane), n))]


def count_in(dirs) -> int:
    """The judgements stored in these folders: one per trial and lane, its versions inside."""
    return sum(n.endswith(".json") for d in dirs for _, _, names in os.walk(d) for n in names)


def all_records(base: str, job_id: Optional[str] = None, lane: str = "decision", old: Optional[dict] = None):
    """Every stored record, every version: yields (job_id, trial_name,
    version, record). One job's when `job_id` is given, with its older
    records `old` (`overlay`)."""
    root = _root(base, lane)
    try:
        jobs = list(_names(job_id)) if job_id else sorted(os.listdir(root))
    except OSError:
        jobs = []
    files = {}
    for j in jobs:
        try:
            files.update((os.path.join(root, j, n), None) for n in os.listdir(os.path.join(root, j)) if n.endswith(".json"))
        except OSError:
            continue
    files.update((p, None) for p in old or {} if os.path.dirname(os.path.dirname(p)) == root)
    for path in sorted(files):
        for version, rec in _with(_read([path]), (old or {}).get(path)).items():
            if isinstance(rec, dict):
                yield os.path.basename(os.path.dirname(path)), os.path.basename(path)[:-5], version, rec


def calibration(base: Optional[str], job_ids=()) -> Optional[dict]:
    """The judge's observed characters per token over the stored calls of
    the jobs `job_ids` (`client.calibrate`); None with no store or no sample."""
    if not base:
        return None
    return client.calibrate(rec for j in dict.fromkeys(job_ids) if j for _, _, _, rec in all_records(base, j))


def truncation_report(base: str, job_id: str, limit: Optional[int] = None, old: Optional[dict] = None) -> dict:
    """Which state kinds were cut to the limit over a job's stored calls,
    and how often - counted here. `kinds[kind]` is {calls, truncated,
    max_chars}, most-truncated first; `untyped` counts records that say
    `truncated` but carry no per-state sizes (v1), which cannot be placed."""
    from . import state as judge_state
    limit = judge_state.LIMIT if limit is None else limit
    kinds: dict = {}
    records = truncated = untyped = 0
    for _, _, _, rec in all_records(base, job_id, old=old):
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


def _merged(cur: dict, data: dict) -> dict:
    """`cur`, a file's records, with the records `data` of an older store
    added: each one not there yet. Where `cur` has another under its key, the
    one shown stays there and the other is kept beside: of two tries, the
    later try's shown and the earlier under its own start (`_try`); of one
    try, the newer by `asked_at` shown and the other under ``<key>~<its
    digest>``. Nothing is dropped, and a record copied in once is never
    copied again (`ADOPTED`), even after a save has changed it."""
    cur = dict(cur)
    adopted = set(cur.get(ADOPTED) or ())
    have = {_digest(r) for r in cur.values() if isinstance(r, dict)}
    for k, rec in data.items():
        if not isinstance(rec, dict) or k == ADOPTED:
            continue
        d = _digest(rec)
        if d in adopted or d in have:
            adopted.add(d)
            continue
        adopted.add(d)
        now = cur.get(k)
        if not isinstance(now, dict):
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
        have |= {_digest(r) for r in cur.values() if isinstance(r, dict)}
    if adopted:
        cur[ADOPTED] = sorted(adopted)
    return cur


def _merge_into(path: str, old) -> None:
    """The older records `old` (a list of files' contents) into the file at `path` (`_merged`),
    under its lock; nothing written when nothing is new. `Unreadable` when the file cannot be read."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _locked(path):
        cur = _read([path], strict=True)
        got = _with(cur, old)
        if got != cur:
            ondisk.write_whole(path, json.dumps(got, indent=1), mode=0o600)


def moved(into: Optional[str] = None) -> bool:
    """Whether `move` has copied every workspace's older records into the user's store."""
    return os.path.exists(os.path.join(_root(into or home(), "decision"), MOVED))


def _try_file(data: dict, n: int) -> dict:
    """An older store's record of try `n` of a trial (``<trial>.try-N.json``), filed under its try:
    each version's under ``<version>@<its start>``, as `save` keeps an earlier try."""
    return {k if "@" in k or not isinstance(rec, dict) else _try(k, rec.get("trial_started_at") or f"try-{n}"): rec
            for k, rec in data.items()}


def overlay(ws_dir: Optional[str], job, into: Optional[str] = None) -> dict:
    """What the workspace's store from before (`ws_dir`) holds for `job` -
    under its job id, its trials' (a try's ``<trial>.try-N`` under its trial,
    filed under its try) and its dataset's task records - as {the file of the
    user's store (`into`, `home()` by default) it belongs in: [its contents]}.
    Read only. Two jobs of one id in that workspace each get the same
    records, as each showed them before. {} once `move` is done."""
    into = into or home()
    if not ws_dir or not job.uid or moved(into):
        return {}
    names = {}
    for t in job.trials:
        names.update((n, (key(t.key), 0)) for n in _names(t.trial_name))
        for i in range(1, min(getattr(t, "n_retries", 0) or 0, 99) + 1):
            names.update((n, (key(t.key), i)) for n in _names(f"{t.trial_name}.try-{i}"))
    places = [(lane, j, job.uid, names) for lane in ROOTS for j in _names(job.job_id)]
    # before, a dataset's task records were a job of their own, named `task:<dataset>`, and a job naming none its id
    places += [(TASKS, j, task_dataset(job), {}) for j in _names("task:" + (job.dataset_path or job.dataset or "job:" + job.job_id))]
    out: dict = {}
    for lane, old, new, rename in places:
        src = os.path.join(_root(ws_dir, "decision" if lane == TASKS else lane), old)
        try:
            files = sorted(f for f in os.listdir(src) if f.endswith(".json"))
        except OSError:
            continue
        for f in files:
            data = _read([os.path.join(src, f)])
            name, n = rename.get(f[:-5], (f[:-5], 0))
            if data:
                out.setdefault(os.path.join(_root(into, lane), key(new), name + ".json"), []).append(_try_file(data, n) if n else data)
    return out


def adopt(ws_dir: str, job, into: Optional[str] = None) -> None:
    """Copy `overlay` into the user's store, file by file under each file's lock. The workspace's files are
    left as they are."""
    for path, old in overlay(ws_dir, job, into).items():
        _merge_into(path, old)


def records_of(base: str, uids, olds=()) -> int:
    """How many trial records the jobs `uids` have in the user's store, their older records `olds`
    (`overlay`s) counted with them, nothing copied."""
    dirs = {os.path.join(_root(base, lane), key(u)) for lane in ROOTS for u in uids if u}
    found = {os.path.join(d, n) for d in dirs if os.path.isdir(d) for n in os.listdir(d) if n.endswith(".json")}
    return len(found | {p for o in olds for p in o if os.path.dirname(p) in dirs})


def old_stores(home_dir: str, into: Optional[str] = None) -> list:
    """[(workspace, its count of older records)] for each workspace whose store from before holds any,
    until `move` is done."""
    from nullius import workspace
    if moved(into):
        return []
    out = []
    for ws in workspace.list_all(home_dir):
        n = sum(f.endswith(".json") for r in ROOTS.values() for _, _, fs in os.walk(os.path.join(ws.dir, r)) for f in fs)
        if n:
            out.append((ws, n))
    return out


def move(home_dir: str, into: Optional[str] = None) -> list:
    """Copy every workspace's older records into the user's store (`adopt`, for every job its
    roots hold) and record that it is done (`moved`), so reads stop consulting the older stores.
    The older files stay as they are. A workspace with a root that cannot be read is left for a
    later move, which is then not done: its labels are returned. Safe to run again."""
    from nullius import workspace
    into = into or home()
    unread = []
    for ws, _ in old_stores(home_dir, into):
        roots = ws.job_roots or ws.roots
        if not all(os.path.isdir(r) for r in roots):
            unread.append(ws.label)
            continue
        for job in workspace.load_jobs(ws, roots):
            adopt(ws.dir, job, into)
    if not unread:
        os.makedirs(_root(into, "decision"), exist_ok=True)
        ondisk.write_whole(os.path.join(_root(into, "decision"), MOVED), "")
    return unread
