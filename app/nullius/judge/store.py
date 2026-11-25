"""Where a judgement lives: the user's own store, by the job it judges -
``<data>/judgements/<job uid>/<trial key>.json`` (each name spelled by
`key`; ``Job.uid``, ``Trial.trial_name``) for System One's,
``<data>/judgements-llm/...`` for the LLM judge's (a trial has both,
apart, ``lane``), ``<data>/judgements-tasks/<dataset>/...`` for task
instructions - keyed by question version inside. ``<data>`` is
``home()``, the user data directory. Never in a job folder: it is its
producer's, and can be rewritten, pruned or read-only. So one judgement
shows in every workspace that holds the job, and deleting a workspace
leaves it.

Every function takes the store's base first: ``home()``, or a workspace's
folder for the store judgements were kept in before (``<workspace>/
judgements{,-llm}/<job id>/``). Until `move` has copied a workspace's into
the user's store, a read merges a job's older records from it in memory
(`overlay`, passed as ``old``) and writes nothing; a save copies in those of
the file it writes (`adopt`). The older files are never deleted or rewritten.

A try of a retried trial is a trial of its own, by its folder's name
(``<trial>.try-N``). Every version is kept, and under each every run of
one trial name: the one judged last under the version, each earlier one
under ``<version>@<its start>``. A read without a version asks for the current one
and falls back to the earlier ones the vocabulary still names
(``questions.EARLIER``), newest first, so an earlier judgement is still shown, and a write always lands under the version the record names."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from typing import Optional

from nullius import ondisk, paths

from . import client, questions as Q

_SAFE = re.compile(r"[^A-Za-z0-9._@+-]")


ROOTS = {"decision": "judgements", "llm": "judgements-llm"}     # one directory per judge
TASKS = "task"                       # the lane of task-instruction judgements, by dataset (`task_dataset`)
ADOPTED = "~adopted"                 # in a file: the digests of the older records copied in, so none is copied twice
MOVED = ".moved"                     # in ``<home>/judgements/``: a file per workspace whose older records are all in the user's store


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
    """One save at a time to a judgement file, across threads and processes: a save reads, merges and writes it whole.
    A delete of the folder takes it too (`remove`)."""
    return ondisk.locked(os.path.join(os.path.dirname(path), ".lock"))


class Gone(client.JudgeError, ValueError):
    """The job a judgement was being written for was deleted meanwhile: nothing is written."""
    def __init__(self, job_dir: str):
        super().__init__(0, f"{job_dir} was deleted; its judgement is not kept.")


def _open_for(path: str, job_dir: Optional[str]):
    """The folder of `path` made, unless the job `job_dir` is gone already (`Gone`)."""
    if job_dir and not os.path.isdir(job_dir):
        raise Gone(job_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _still(path: str, job_dir: Optional[str]) -> None:
    """Under the lock: `Gone` when the job was deleted since `_open_for`, the folder it made removed again."""
    if job_dir and not os.path.isdir(job_dir):
        d = os.path.dirname(path)
        if not any(n.endswith(".json") for n in (os.listdir(d) if os.path.isdir(d) else ())):
            shutil.rmtree(d, ignore_errors=True)
        raise Gone(job_dir)


def remove(folder: str) -> None:
    """A job's judgement folder deleted, under the lock its saves take, so none writes into it meanwhile."""
    if os.path.isdir(folder):
        with ondisk.locked(os.path.join(folder, ".lock")):
            shutil.rmtree(folder, ignore_errors=True)


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


def _of(rec: Optional[dict], started: Optional[str], strict: bool = False) -> bool:
    """Whether a record judged the try that started at ``started``: a retry
    runs an attempt again under its trial's name, and what was asked of the
    try before is not this one's. A record from before tries were told
    apart, or a check that names no start, takes it as it is, unless
    `strict` (an attempt with several tries): both starts are then needed, and the same."""
    if not rec:
        return True
    if strict:
        return bool(started) and rec.get("trial_started_at") == started
    return not started or rec.get("trial_started_at") in (started, None)


def _try(version: str, started: str) -> str:
    """Where a version keeps an earlier try's record, beside the latest try's under `version`."""
    return f"{version}@{started}"


def _pick(data: dict, version: Optional[str], started: Optional[str] = None, strict: bool = False) -> Optional[dict]:
    for v in _versions(version):
        for rec in (data.get(v), started and data.get(_try(v, started))):
            if rec and _of(rec, started, strict):
                return rec
    return None


def load(base: str, job_id: str, trial_name: str, version: Optional[str] = None, lane: str = "decision",
         started: Optional[str] = None, old: Optional[dict] = None, strict: bool = False) -> Optional[dict]:
    """The record under `version`; with none named, the current version or
    the newest earlier one that has a record. `started`: the trial's own
    start, so a record of an earlier try under its name is none of its
    (`strict`: an attempt with several tries, `_of`).
    `old`: the job's older records (`overlay`), read with it."""
    if not job_id:                   # a job with no identity yet (Job.uid_source "pending") has no judgement
        return None
    found = _paths(base, job_id, trial_name, lane)
    return _pick(_with(_read(found), _olds(old, found)), version, started, strict)


def _olds(old: Optional[dict], found) -> list:
    """The older records `old` holds for any of the spellings `found` of one file."""
    return [d for p in found for d in (old or {}).get(p) or ()]


def is_current(record: Optional[dict]) -> bool:
    """Whether a record was asked under the current vocabulary."""
    return bool(record) and record.get("questions") == Q.VERSION


def save(base: str, job_id: str, trial_name: str, record: dict, lane: str = "decision",
         started: Optional[str] = None, old: Optional[dict] = None, job_dir: Optional[str] = None,
         strict: bool = False) -> dict:
    """The record under its question version, returned as stored; other versions kept. A
    record's answers and hygiene readings merge over the version's earlier
    ones, so judging one question later does not drop the others. Saves to
    one file take turns, across processes; each writes through a temporary file of its own.
    `old`: the job's older records (`overlay`), those of this file copied in first.
    `Unreadable` when the file is there and cannot be read, `Gone` when the
    job's folder `job_dir` is no longer there: nothing is written."""
    if not job_id:
        raise ValueError("a judgement needs the job's identity")
    found = _paths(base, job_id, trial_name, lane)
    path = found[0]
    _open_for(path, job_dir)
    with _locked(path):
        _still(path, job_dir)
        data = _with(_read(found, strict=True), _olds(old, found))
        version = record.get("questions", Q.VERSION)
        earlier = data.get(version) or {}
        if not _of(earlier, started, strict):        # another try's: kept beside, and none of its answers are this one's
            at = earlier.get("trial_started_at")
            data[_try(version, at) if at else f"{version}~{_digest(earlier)}"] = earlier     # unstamped: whose is unknown
            earlier = data.pop(_try(version, started), None) or {}
        merged = dict(record, **({"trial_started_at": started} if started else {}))
        merged["answers"] = {**(earlier.get("answers") or {}), **(record.get("answers") or {})}
        if "hygiene" in earlier or "hygiene" in record:
            merged["hygiene"] = {**(earlier.get("hygiene") or {}), **(record.get("hygiene") or {})}
        data[version] = merged
        ondisk.write_whole(path, json.dumps(data, indent=1), mode=0o600)
    return merged


def for_job(base: str, job_id: str, started: dict, version: Optional[str] = None, lane: str = "decision",
            old: Optional[dict] = None, strict=()) -> dict:
    """The judged trials of a job: {trial_name: record} for each trial of
    `started` - {trial_name: its start}, every trial of the job - that has
    one, as `load` picks it; `strict`: the names of those with several tries."""
    out = {}
    for name, start in started.items():
        rec = load(base, job_id, name, version, lane, start, old, strict=name in strict)
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
    later try's shown; of one try, the newer by `asked_at`. The other is kept
    under its start (`_try`), so a try still finds it, or with none under
    ``<key>~<its digest>``. Nothing is dropped, and a record copied in once is never
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
            slot = _try(k, other["trial_started_at"]) if other.get("trial_started_at") else f"{k}~{_digest(other)}"
            if slot in cur:
                slot = f"{slot}~{_digest(other)}"
            cur[k], cur[slot] = shown, other
        have |= {_digest(r) for r in cur.values() if isinstance(r, dict)}
    if adopted:
        cur[ADOPTED] = sorted(adopted)
    return cur


def _merge_into(path: str, old, job_dir: Optional[str] = None) -> None:
    """The older records `old` (a list of files' contents) into the file at `path` (`_merged`),
    under its lock; nothing written when nothing is new. `Unreadable` when the file cannot be read,
    `Gone` when the job `job_dir` was deleted."""
    _open_for(path, job_dir)
    with _locked(path):
        _still(path, job_dir)
        cur = _read([path], strict=True)
        got = _with(cur, old)
        if got != cur:
            ondisk.write_whole(path, json.dumps(got, indent=1), mode=0o600)


def _raise(e: OSError):
    raise e


def _old_files(ws_dir: str) -> list:
    """The files of a workspace's store from before, by their path in it. OSError when a folder
    of it cannot be listed: what it holds is unknown."""
    return sorted(os.path.relpath(os.path.join(d, f), ws_dir) for r in ROOTS.values() if os.path.isdir(os.path.join(ws_dir, r))
                  for d, _, fs in os.walk(os.path.join(ws_dir, r), onerror=_raise) for f in fs if f.endswith(".json"))


def _marker(ws_dir: str, into: str) -> str:
    return os.path.join(_root(into, "decision"), MOVED, os.path.basename(ws_dir))


def _bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _signature(ws_dir: str, files, raw: Optional[dict] = None) -> str:
    """The digest of the files `files` of a workspace's store from before, by path and contents:
    those in `raw` ({path: bytes}) as they were read, the others as they are now."""
    h = hashlib.sha256()
    for f in files:
        data = (raw or {}).get(f)
        data = _bytes(os.path.join(ws_dir, f)) if data is None else data
        h.update(f"{f}\0{hashlib.sha256(data).hexdigest() if data is not None else '-'}\n".encode("utf-8"))
    return h.hexdigest()


def moved(ws_dir: Optional[str], into: Optional[str] = None) -> bool:
    """Whether `move` copied every record of the workspace's store from before into the user's
    store, and its files are as `move` read them: one added or rewritten since, by a restore or an
    older Nullius, is read again, and so is a store whose folders cannot all be listed."""
    if not ws_dir:
        return False
    try:
        with open(_marker(ws_dir, into or home()), encoding="utf-8") as fh:
            return fh.read() == _signature(ws_dir, _old_files(ws_dir))
    except OSError:
        return False


def _mark(ws_dir: str, into: str, files, raw: Optional[dict] = None) -> None:
    os.makedirs(os.path.dirname(_marker(ws_dir, into)), exist_ok=True)
    ondisk.write_whole(_marker(ws_dir, into), _signature(ws_dir, files, raw))


def unmark(ws_dir: str, to: Optional[str] = None) -> None:
    """The workspace no longer `moved`, or, renamed to the folder `to`, still: a marker names a
    workspace by its folder, and another made later under that name is not this one."""
    try:
        if to:
            os.replace(_marker(ws_dir, home()), _marker(to, home()))
        else:
            os.remove(_marker(ws_dir, home()))
    except FileNotFoundError:
        pass


def forget(ws_dir: str, folders) -> None:
    """A workspace's older folders of a deleted job removed, and the workspace read again until a
    later `move`: a file changed meanwhile is never taken as one `move` read."""
    for d in folders:
        shutil.rmtree(d, ignore_errors=True)
    if folders:
        unmark(ws_dir)


def _places(ws_dir: str, job, into: str):
    """(an older file, the file of the user's store it belongs in) for each the workspace's store
    from before holds for `job`: under its job id its trials' and tries' (each by its own name), and
    its dataset's task records. The file is None where it is named by the job's identity and the job
    has none yet (pending)."""
    names = {}
    for t in job.trials:
        for name in (t.trial_name, *(f"{t.trial_name}.try-{i}" for i in range(1, min(getattr(t, "n_retries", 0) or 0, 99) + 1))):
            names.update((n, key(name)) for n in _names(name))
    places = [(lane, j, job.uid, names) for lane in ROOTS for j in _names(job.job_id)]
    # before, a dataset's task records were a job of their own, named `task:<dataset>`, and a job naming none its id
    places += [(TASKS, j, task_dataset(job) if job.uid or job.dataset_path or job.dataset else "", {})
               for j in _names("task:" + (job.dataset_path or job.dataset or "job:" + job.job_id))]
    for lane, old, new, rename in places:
        src = os.path.join(_root(ws_dir, "decision" if lane == TASKS else lane), old)
        try:
            files = sorted(f for f in os.listdir(src) if f.endswith(".json"))
        except OSError:
            continue
        for f in files:
            yield os.path.join(src, f), (os.path.join(_root(into, lane), key(new), rename.get(f[:-5], f[:-5]) + ".json")
                                         if new else None)


def owners(ws, into: Optional[str] = None, names=None) -> Optional[dict]:
    """Who each older file of the workspace `ws`'s store may belong to: {the file: {the file of the
    user's store it belongs in: the job's folder}}, over every job its roots hold, read afresh. A
    job with no identity yet claims the files it would own under ``("pending", its folder)``, so
    they have no single owner until it has one. `names`: the job folders whose names share a
    spelling (`_names`) with one of these only, all that can claim a job's own files. None while one
    of its roots, or a folder in one that may be a job, cannot be read: its jobs, and so the owners,
    are unknown."""
    from nullius.runs import parse
    into = into or home()
    roots = ws.job_roots or ws.roots
    spelt = None if names is None else {s for n in names for s in _names(n)}
    mine = lambda name: spelt is None or not spelt.isdisjoint(_names(name))
    if not all(os.path.isdir(r) for r in roots):
        return None
    try:
        dirs = [d for d in parse.find_job_dirs(roots) if mine(os.path.basename(d))]
        for r in roots:
            with os.scandir(r) as entries:
                for e in entries:
                    if e.is_dir() and mine(e.name):
                        os.scandir(e.path).close()
    except OSError:
        return None
    out: dict = {}
    for d in dirs:
        for job in parse.split_agents([parse.parse_job(d)]):
            for src, dest in _places(ws.dir, job, into):
                out.setdefault(src, {}).setdefault(dest or ("pending", d), d)
    return out


def single(owned: Optional[dict]) -> dict:
    """{older file: (the file of the user's store it belongs in, its job's folder)} for each file
    of `owners` that one job with an identity owns: the only ones ever copied."""
    out = {}
    for src, dests in (owned or {}).items():
        [(dest, job_dir)] = dests.items() if len(dests) == 1 else [(None, None)]
        if isinstance(dest, str):
            out[src] = (dest, job_dir)
    return out


def _old(path: str, raw: Optional[bytes] = None) -> Optional[dict]:
    """An older file's records (from `raw`, its bytes, when given); None when it cannot be read or
    holds no JSON object, or a version in it holds no record."""
    raw = _bytes(path) if raw is None else raw
    try:
        data = json.loads(raw) if raw is not None else False
    except ValueError:
        return None
    if data is None:
        return {}
    ok = isinstance(data, dict) and all(isinstance(r, dict) for k, r in data.items() if k != ADOPTED)
    return data if ok else None


def overlay(ws_dir: Optional[str], job, into: Optional[str] = None, only=None) -> dict:
    """What the workspace's store from before (`ws_dir`) holds for `job` (`_places`), as {the file
    of the user's store (`into`, `home()` by default) it belongs in: [its contents]}. Read only; a
    file that cannot be read is left out. Two jobs of one id in that workspace each get the same
    records, as each showed them before. `only`: {older file: the file it belongs in} (a save's,
    `single`), those alone and only where they belong in `job`'s. {} once the workspace is `moved`."""
    into = into or home()
    if not ws_dir or not job.uid or moved(ws_dir, into):
        return {}
    out: dict = {}
    for src, dest in _places(ws_dir, job, into):
        data = _old(src) if dest and (only is None or only.get(src) == dest) else None
        if data:
            out.setdefault(dest, []).append(data)
    return out


def adopt(ws_dir: str, job, into: Optional[str] = None, only=None) -> None:
    """Copy `overlay` (`only` those older files) into the user's store, file by file under each
    file's lock. The workspace's files are left as they are."""
    for path, old in overlay(ws_dir, job, into, only).items():
        _merge_into(path, old, getattr(job, "dir", None))


def adopt_owned(ws, into: Optional[str] = None, dirs=None) -> Optional[set]:
    """Copy into the user's store each older file of the workspace `ws` that one job owns
    (`single`), of the job folders `dirs` only when given: the files copied (read, if empty), or
    None, nothing copied, while a root cannot be read. A file several jobs may own stays where it is."""
    owned = owners(ws, into)
    if owned is None:
        return None
    by_dest, done = {}, set()
    for src, (dest, job_dir) in single(owned).items():
        if dirs is None or job_dir in dirs:
            data = _old(src)
            if data is not None:
                done.add(src)
            if data:
                by_dest.setdefault(dest, (job_dir, []))[1].append(data)
    for dest, (job_dir, old) in by_dest.items():
        _merge_into(dest, old, job_dir)
    return done


def records_of(base: str, uids, olds=()) -> int:
    """How many trial records the jobs `uids` have in the user's store, their older records `olds`
    (`overlay`s) counted with them, nothing copied."""
    dirs = {os.path.join(_root(base, lane), key(u)) for lane in ROOTS for u in uids if u}
    found = {os.path.join(d, n) for d in dirs if os.path.isdir(d) for n in os.listdir(d) if n.endswith(".json")}
    return len(found | {p for o in olds for p in o if os.path.dirname(p) in dirs})


def old_stores(home_dir: str, into: Optional[str] = None) -> list:
    """[(workspace, its count of older records)] for each workspace whose store from before holds
    any, until it is `moved`; one whose folders cannot all be listed counts those it can."""
    from nullius import workspace
    out = []
    for ws in workspace.list_all(home_dir):
        try:
            n = len(_old_files(ws.dir))
        except OSError:
            n = -1
        if n and not moved(ws.dir, into):
            out.append((ws, max(n, 0)))
    return out


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def move(home_dir: str, into: Optional[str] = None) -> list:
    """Copy every workspace's older records into the user's store, the older files left as they
    are, and mark each workspace whose every file went (`moved`), so reads stop consulting it.
    A workspace is left for a later move, as one line in the list returned, while one of its roots
    or folders cannot be read, or one of its files cannot be read, belongs to no job its roots
    hold, or may belong to several (`owners`: a job id two jobs share, or one still starting):
    such a file is copied nowhere, and still read as before. Safe to run again."""
    into = into or home()
    left = []
    for ws, _ in old_stores(home_dir, into):
        try:
            files = _old_files(ws.dir)                   # first: one added during the move leaves the workspace unmarked
        except OSError:
            left.append(f"{ws.label} (a folder cannot be read)")
            continue
        owned = owners(ws, into)
        if owned is None:
            left.append(f"{ws.label} (a root cannot be read)")
            continue
        raw = {f: _bytes(os.path.join(ws.dir, f)) for f in files}
        one = single(owned)
        unread, shared, pending, by_dest = 0, 0, 0, {}
        for src, dests in owned.items():
            rel = os.path.relpath(src, ws.dir)
            data = _old(src, raw.get(rel))
            if data is None:
                unread += 1
            elif src not in one:                         # unresolved: whose it is cannot be told
                if all(isinstance(d, str) for d in dests):
                    shared += 1
                else:
                    pending += 1
            elif data:
                dest, job_dir = one[src]
                by_dest.setdefault(dest, (job_dir, []))[1].append(data)
        for dest, (job_dir, old) in by_dest.items():
            _merge_into(dest, old, job_dir)
        orphans = len({os.path.join(ws.dir, f) for f in files} - set(owned))
        why = [_plural(n, *w) for n, w in ((unread, ("file cannot be read", "files cannot be read")),
                                           (orphans, ("record of no job found", "records of no job found")),
                                           (shared, ("record two jobs share", "records several jobs share")),
                                           (pending, ("record of a job still starting", "records of jobs still starting"))) if n]
        if why:
            left.append(f"{ws.label} ({', '.join(why)})")
        else:
            _mark(ws.dir, into, files, raw)
    return left
