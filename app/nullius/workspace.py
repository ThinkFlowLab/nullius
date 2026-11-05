"""Workspaces: a named set of roots, and what the app made from them.

A workspace gives the app's roots, and what it made from them, a name and a
directory, so they survive a restart, so two sets of runs can be kept apart,
and so the expensive part — parsing a 375-trial job, converting a foreign
run tree — happens once per change rather than once per page.

    <home>/<name>/workspace.json   name, label, roots, created, opened
    <home>/<name>/imported/        foreign run trees converted to job dirs
    <home>/<name>/jobs/<id>.json   a parsed Job, keyed by the job dir's signature

``home`` is ``WORKSPACES_DIR`` or ``workspaces/`` in the persistent user data directory.
A name is a slug the user typed, or two words picked at random — something
to say out loud, the way Harbor suffixes a trial with ``__TKLnaSW`` and
nobody can.

The cache is keyed by a *signature* of the job directory (every result.json
and trajectory.json under it, with size and mtime), never by time: a run
still going changes its files, the signature moves, the job is parsed again.
A cached job that was parsed by an older version of ``parse`` is refreshed
too — the signature carries ``parse.SCHEMA`` so a change there invalidates.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import random
import re
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone

from nullius.runs import parse
from nullius import paths

ADJECTIVES = """brisk quiet tidy sturdy curious gentle nimble patient plucky sunny wry keen
mellow bold calm clever crisp eager fuzzy glossy humble jolly lucky merry neat proud quirky
rapid sharp snug spry steady swift warm witty zesty cosmic dapper mossy""".split()
NOUNS = """otter quasar kernel lantern marmot nebula pixel socket walrus badger comet dynamo
ferret gadget heron ion jackal kestrel lemur mantis newt orbit pelican quokka radar sprocket
tapir urchin vector wombat yak zephyr beacon cactus dolphin falcon gecko magnet pulsar""".split()

SCHEMA = "1"   # bump when the cached shape of Job/Trial changes


def home() -> str:
    return paths.workspaces_dir()


def slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return s[:48]


def random_name(taken=(), rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    taken = set(taken)
    for _ in range(200):
        name = f"{rng.choice(ADJECTIVES)}-{rng.choice(NOUNS)}"
        if name not in taken:
            return name
    return f"{rng.choice(ADJECTIVES)}-{rng.choice(NOUNS)}-{rng.randrange(1000)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Workspace:
    name: str
    label: str
    roots: list
    created: str
    opened: str
    dir: str
    job_roots: list = field(default_factory=list)   # after imports: what parse walks

    @property
    def imports_dir(self) -> str:
        return os.path.join(self.dir, "imported")

    @property
    def jobs_dir(self) -> str:
        return os.path.join(self.dir, "jobs")

    @property
    def n_cached(self) -> int:
        try:
            return sum(1 for n in os.listdir(self.jobs_dir)
                       if n.endswith(".json") and n != "index.json")
        except OSError:
            return 0


def _path(home_dir: str, name: str) -> str:
    return os.path.join(home_dir, name, "workspace.json")


def _write_atomically(path: str, dump) -> None:
    """Write *path* whole or not at all: *dump* fills a temp file beside it,
    which is then renamed over the target.

    The temp name is this writer's own - process and thread - because two
    requests writing the same file at once are ordinary here: the job page and
    its /live poll both re-parse a job whose files keep changing. With one
    shared `<name>.tmp`, the first rename took the file away and the second
    raised FileNotFoundError, a 500 on the page. Whichever rename lands
    last wins, and both wrote a complete file."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            dump(fh)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save(ws: Workspace) -> None:
    os.makedirs(ws.dir, exist_ok=True)
    data = {k: v for k, v in asdict(ws).items() if k != "dir"}
    _write_atomically(os.path.join(ws.dir, "workspace.json"), lambda fh: json.dump(data, fh, ensure_ascii=False, indent=1))


def load(home_dir: str, name: str) -> Workspace | None:
    if not name or slug(name) != name:
        return None
    try:
        with open(_path(home_dir, name), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("name"):
        return None
    known = {f.name for f in fields(Workspace)} - {"dir"}
    return Workspace(dir=os.path.join(home_dir, name),
                     **{k: v for k, v in data.items() if k in known})


def list_all(home_dir: str) -> list:
    """Every workspace under *home_dir*, most recently opened first."""
    try:
        names = os.listdir(home_dir)
    except OSError:
        return []
    found = [ws for ws in (load(home_dir, n) for n in sorted(names)) if ws]
    return sorted(found, key=lambda w: w.opened, reverse=True)


def find_by_roots(home_dir: str, roots, besides: str = "") -> Workspace | None:
    """The workspace already pointing at exactly these roots, if any; the
    most recently opened. ``besides``: a folder whose roots a workspace may
    hold as well - the runs started from it, each added as it began - so a
    restart with the same paths reopens it, its runs and all."""
    want = sorted(os.path.realpath(r) for r in roots)
    extra = os.path.join(os.path.realpath(besides), "") if besides else None
    for ws in list_all(home_dir):
        held = sorted(os.path.realpath(r) for r in ws.roots)
        if held == want or (extra and [r for r in held if not r.startswith(extra)] == want):
            return ws
    return None


def _free(name: str, taken) -> str:
    """*name*, or the first ``<name>-<n>`` no other workspace has."""
    return name if name not in taken else next(f"{name}-{i}" for i in range(2, 1000) if f"{name}-{i}" not in taken)


def create(home_dir: str, roots, label: str = "") -> Workspace:
    taken = {ws.name for ws in list_all(home_dir)}
    name = slug(label)
    name = _free(name, taken) if name else random_name(taken)
    now = _now()
    ws = Workspace(name=name, label=label.strip() or name, roots=list(roots),
                   created=now, opened=now, dir=os.path.join(home_dir, name))
    save(ws)
    return ws


def rename(home_dir: str, ws: Workspace, label: str) -> Workspace:
    """*ws* called *label*, and named after it as a new workspace would be:
    its directory moves to the label's slug (suffixed when another has it),
    with everything in it - parsed jobs, imports, judgements - so a
    workspace never carries one name on the page and another on disk. A
    label with nothing to slug keeps the name it has. The roots it reads
    stay where they are."""
    label = (label or "").strip()
    if not label:
        return ws
    name = slug(label)
    if name and name != ws.name:
        # a directory there that holds no workspace - left behind, half made - is taken too
        taken = {w.name for w in list_all(home_dir)} | set(os.listdir(home_dir))
        name = _free(name, taken - {ws.name})
        new_dir = os.path.join(home_dir, name)
        os.rename(ws.dir, new_dir)
        ws.name, ws.dir = name, new_dir
    ws.label = label
    save(ws)
    return ws


def touch(ws: Workspace) -> None:
    ws.opened = _now()
    save(ws)


def delete(home_dir: str, name: str) -> bool:
    """Remove a workspace: its record, its parse cache and its converted
    imports. The job roots it pointed at are not touched - they were never
    its own. True when something was removed."""
    ws = load(home_dir, name)
    if ws is None:
        return False
    shutil.rmtree(ws.dir, ignore_errors=True)
    return True



def signature(job_dir: str) -> str:
    """What the parse of *job_dir* depends on: the files it reads, by size
    and mtime. Cheap — a few hundred stats — and honest: a running job moves
    its result.json files, so its signature moves with it."""
    return _signatures(job_dir)[0]


def _signatures(job_dir: str) -> tuple:
    """(the job's signature, {trial folder: its own part of it}): a trial whose
    part is unchanged parses the same, so a running job re-reads only the
    trials that moved (load_job), not all of them at each one that ends."""
    parts = [f"schema={SCHEMA}/{parse.SCHEMA}"]
    trials = {}
    try:
        names = sorted(os.listdir(job_dir))
    except OSError:
        return "", {}

    def stat(path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        return f"{os.path.relpath(path, job_dir)}:{st.st_size}:{st.st_mtime_ns}"

    for top in ("result.json", "config.json"):
        s = stat(os.path.join(job_dir, top))
        if s:
            parts.append(s)
    for n in names:
        sub = os.path.join(job_dir, n)
        if not os.path.isdir(sub):
            continue
        own = [s for s in (stat(os.path.join(sub, rel)) for rel in
                           ("result.json", os.path.join("agent", "trajectory.json"), os.path.join("agent", "trace.json"))) if s]
        parts += own
        trials[sub] = "\n".join(own)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest(), trials


def _encode(obj):
    if isinstance(obj, datetime):
        return {"$dt": obj.isoformat()}
    raise TypeError(f"not serialisable: {type(obj).__name__}")


def _decode(d):
    if "$dt" in d and len(d) == 1:
        return datetime.fromisoformat(d["$dt"])
    return d


def job_to_dict(job: parse.Job) -> dict:
    return asdict(job)


def job_from_dict(data: dict) -> parse.Job:
    trial_fields = {f.name for f in fields(parse.Trial)}
    trials = []
    for t in data.get("trials") or []:
        t = {k: v for k, v in t.items() if k in trial_fields}
        t["phases"] = {k: tuple(v) for k, v in (t.get("phases") or {}).items()}
        trials.append(parse.Trial(**t))
    job_fields = {f.name for f in fields(parse.Job)}
    body = {k: v for k, v in data.items() if k in job_fields and k != "trials"}
    job = parse.Job(**body)
    job.trials = trials
    return job


def _cache_file(ws: Workspace, job_dir: str) -> str:
    key = hashlib.sha256(os.path.realpath(job_dir).encode()).hexdigest()[:20]
    return os.path.join(ws.jobs_dir, f"{key}.json")


def _index_path(ws: Workspace) -> str:
    return os.path.join(ws.jobs_dir, "index.json")


def job_index(ws: Workspace) -> dict:
    """job dir → {job_id, n_trials}: what the rail lists without parsing."""
    try:
        with open(_index_path(ws), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _note_in_index(ws: Workspace, job_dir: str, job: parse.Job, index: dict | None = None) -> bool:
    """The job's entry in the rail's index. With ``index`` (load_jobs, which
    reads the file once and writes it once) the dict is changed in place and
    True returned when it changed; alone, the file is read and written here."""
    own = index is None
    index = job_index(ws) if own else index
    entry = {"job_id": job.job_id, "n_trials": len(job.trials), "dataset": job.dataset,
             "agent": job.agent_label or job.agent_name}
    if index.get(job_dir) == entry:
        return False
    index[job_dir] = entry
    if own:
        _write_index(ws, index)
    return True


def _write_index(ws: Workspace, index: dict) -> None:
    os.makedirs(ws.jobs_dir, exist_ok=True)
    _write_atomically(_index_path(ws), lambda fh: json.dump(index, fh, ensure_ascii=False, indent=1))


# Jobs already decoded in this process, by cache file: (signature, Job). A page
# that loads the workspace then skips the JSON decode and the rebuild of every
# unchanged job. A Job is never changed once loaded (parse.split_agents and the
# compare page make new ones with dataclasses.replace).
_DECODED: dict = {}
_DECODED_LOCK = threading.Lock()


def load_job(ws: Workspace, job_dir: str, index: dict | None = None) -> tuple:
    """(Job, from_cache). Parses only when the signature changed. ``index``:
    the rail's index as load_jobs holds it, noted in place."""
    sig, trial_sigs = _signatures(job_dir)
    path = _cache_file(ws, job_dir)
    with _DECODED_LOCK:
        known = _DECODED.get(path)
    if sig:
        if known is not None and known[0] == sig and known[1].dir == job_dir:
            _note_in_index(ws, job_dir, known[1], index)
            return known[1], True
        try:
            with open(path, encoding="utf-8") as fh:
                cached = json.load(fh, object_hook=_decode)
            # "agents": a cache from before split_agents knows nothing of a job's several agents
            if cached.get("signature") == sig and cached.get("dir") == job_dir and "agents" in cached.get("job", {}):
                job = job_from_dict(cached["job"])
                with _DECODED_LOCK:
                    _DECODED[path] = (sig, job, trial_sigs)
                _note_in_index(ws, job_dir, job, index)
                return job, True
        except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError):
            pass
    reuse = None
    if known is not None and known[1].dir == job_dir:
        before = {t.dir: t for t in known[1].trials}
        old_sigs = known[2]
        # a trial whose own files are as they were parses the same: kept, not read again
        reuse = lambda sub: before.get(sub) if old_sigs.get(sub) and old_sigs.get(sub) == trial_sigs.get(sub) else None
    job = parse.parse_job(job_dir, reuse=reuse)
    if sig:
        os.makedirs(ws.jobs_dir, exist_ok=True)
        _write_atomically(path, lambda fh: json.dump({"signature": sig, "dir": job_dir, "cached": _now(),
                                                      "job": job_to_dict(job)}, fh, default=_encode, ensure_ascii=False))
        with _DECODED_LOCK:
            _DECODED[path] = (sig, job, trial_sigs)
        _note_in_index(ws, job_dir, job, index)
    return job, False


def load_jobs(ws: Workspace, roots) -> list:
    """Every job under *roots*, each from the cache when its files are
    unchanged. Same order and de-duplication as ``parse.load_all``."""
    # the rail's index read once and written once, not once per job: re-reading it for
    # each made a workspace of n jobs cost n parses of an index of n entries
    index = job_index(ws)
    before = dict(index)
    jobs = [load_job(ws, d, index)[0] for d in parse.find_job_dirs(roots)]
    if index != before:
        _write_index(ws, index)
    return parse.split_agents(jobs)


def clear_cache(ws: Workspace) -> int:
    """Drop every cached parse (the index too), this process's decoded jobs
    included. Returns how many jobs."""
    with _DECODED_LOCK:
        for path in [p for p in _DECODED if os.path.dirname(p) == ws.jobs_dir]:
            del _DECODED[path]
    try:
        names = [n for n in os.listdir(ws.jobs_dir) if n.endswith(".json")]
    except OSError:
        return 0
    for n in names:
        try:
            os.remove(os.path.join(ws.jobs_dir, n))
        except OSError:
            pass
    return len([n for n in names if n != "index.json"])
