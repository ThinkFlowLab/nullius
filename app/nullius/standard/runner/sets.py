"""Task sets: a named list of a benchmark's units, kept in the user catalogue.

An entry is ``sets/<benchmark>/<name>.json`` under the user catalogue
(``paths.catalogue_dir``, ``NULLIUS_CATALOGUE``): a plain file to copy or keep
in git. A set is a group of tasks, run as a benchmark's own dataset is: it
holds an id minted when it is made, its label and the name made from it, its
description, the benchmark, the version of the benchmark it was made at, the
dataset it is cut from, and its list, and nothing that binds a run (no
benchmark option, no setup). The label is the text a person typed; the name
is what commands take: the label in lower case with hyphens, ``-2`` and on
for a second equal one (``name_for``).

A set is frozen: nothing changes its list once made, and another list is
another set. The file keeps one entry in ``revisions``: its number, its date,
a note, the unit ids and a content hash over the benchmark, the dataset and
the sorted ids. A file made before sets were frozen may hold several; it reads
as its latest, and is never rewritten for it. Two runs ran the same set when
their jobs name the same id. Two entries with equal lists stay two sets. An
entry holding any other field is refused where it is read; one of the earlier
format, which kept a run's options, is refused as such (``Earlier``), and
``plain`` says it on a page without its path. ``rename`` gives a set another
label, and the name made from it, its id and list kept, so a job that ran it
finds it by its id (``by_id``); ``delete`` removes its file, and a job that
ran it keeps its record.

``record`` is what a job's ``config.json`` keeps of the set it ran
(``standard.task_set``): its id, name, label, the number of its list, the
hash, the units and the version it was made at. With the job's benchmark and
dataset that is enough for ``import_path`` to rebuild the entry on another
machine, its description empty and its list dated by the import. The same id
with another list is refused there.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

from nullius import paths
from nullius.runs import classify
from nullius.standard.contract import definitions

SCHEMA = "nullius.task-set/v1"
FOLDER = "sets"
# A name stays inside its benchmark's folder: no slash, no leading dot, no ``..``
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
NAME_MAX = 64
LABEL_MAX = 80
# the base of a name made from a label that holds no Latin letter or digit
FALLBACK = "task-set"
_TAG = re.compile(r"__u(\d+)(?:r\d+)?$")
# The outcome groups ``--outcome`` takes beside the pages' outcome names: a pass, a verdict against the
# agent (its failures and its own errors, as the attempts' pass count reads them), the infrastructure's
GROUPS = {
    "passed": {classify.RESOLVED},
    "failed": {classify.PATCH_FAILED_TESTS, classify.FAILED_VERIFICATION, classify.PATCH_NOT_APPLIED,
               classify.NO_PATCH, classify.AGENT_ERROR},
    "infra": set(classify.INFRA_OUTCOMES),
}
HEADER = ("id", "name", "label", "description", "benchmark", "version", "dataset")
# what an entry of the earlier format held beside its header: a run's options, bound to the set
EARLIER = ("benchmark_version", "benchmark_options", "setup")
REVISION = ("revision", "date", "note", "units", "hash")
RECORD = ("id", "name", "revision", "hash", "units")
# what the record of a newer job holds beside those: what its page shows, and what a run checks the set against
RECORD_MORE = ("label", "version")


class Refused(Exception):
    """The set cannot be made, changed, read or run as asked; the message says why."""


class NotFound(Refused):
    """No such set, file or job."""


class Earlier(Refused):
    """An entry of the earlier format, which kept a run's options: refused where it is read."""


def root_dir(root: Optional[str] = None) -> str:
    return root if root is not None else paths.catalogue_dir()


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.match(name) or ".." in name:
        raise Refused(f"{name!r} is not a task set's name: letters, digits, '.', '_' and '-', "
                      "starting with a letter or a digit, at most 64, no '..'")
    return name


def check_label(label: str) -> str:
    """The words a person names a set with, trimmed: any words with at least one letter or digit, at most 80
    characters, on one line."""
    text = label.strip() if isinstance(label, str) else ""
    if not text:
        raise Refused("a task set needs a name")
    if len(text) > LABEL_MAX:
        raise Refused(f"a task set's name is at most {LABEL_MAX} characters")
    if any(unicodedata.category(c) == "Cc" for c in text):
        raise Refused("a task set's name is one line")
    if not any(c.isalnum() for c in text):
        raise Refused("a task set's name needs a letter or a digit")
    return text


def slug(label: str) -> str:
    """The name a label makes: its Latin letters and digits in lower case, every other run of characters one
    hyphen, at most 64; ``task-set`` when nothing is left. "Custom task set" makes ``custom-task-set``."""
    plain = unicodedata.normalize("NFKD", label or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    return s[:NAME_MAX].rstrip("-") or FALLBACK


def name_for(benchmark: str, label: str, root: Optional[str] = None, but: str = "") -> str:
    """The name a set of ``benchmark`` labelled ``label`` takes: its ``slug``, or the first ``<slug>-<n>`` from 2
    that no other set of the benchmark has (``but``, the set's own name when it is renamed, counts as free)."""
    base = slug(check_label(label))
    folder = _folder(benchmark, root)
    taken = {f[:-5] for f in os.listdir(folder) if f.endswith(".json")} if os.path.isdir(folder) else set()
    taken.discard(but)
    if base not in taken:
        return base
    for i in range(2, 10000):
        name = f"{base[:NAME_MAX - len(str(i)) - 1].rstrip('-')}-{i}"
        if name not in taken:
            return name
    raise Refused(f"every name made from {label!r} is taken")


def label_of(entry: dict) -> str:
    """What a person reads a set by: its label, or the name of a set made before labels."""
    return entry.get("label") or entry["name"]


def version_label(bench: Optional[dict], version: str) -> str:
    """A version a set was made at as a reader names it (``main (044bc33)``); the version's key itself when the
    benchmark's definition no longer knows it."""
    return ((bench or {}).get("_labels") or {}).get(version) or version


def version_of(bench: dict) -> str:
    """The version of the benchmark a set made from ``bench`` records: its resolved version, "" when it has none."""
    return str(bench.get("_version") or "")


def _bench_key(benchmark: str) -> str:
    if not isinstance(benchmark, str) or not definitions._KEY.match(benchmark):
        raise Refused(f"{benchmark!r} is not a benchmark's key")
    return benchmark


def _contained(path: str, folder: str) -> str:
    resolved = os.path.realpath(path)
    if os.path.commonpath((resolved, folder)) != folder:
        raise Refused(f"{path}: outside the task set folder {folder}")
    return resolved


def _folder(benchmark: Optional[str], root: Optional[str]) -> str:
    folder = os.path.join(os.path.realpath(root_dir(root)), FOLDER)
    if benchmark is not None:
        folder = os.path.join(folder, _bench_key(benchmark))
    return _contained(folder, folder)


def path_of(benchmark: str, name: str, root: Optional[str] = None) -> str:
    folder = _folder(benchmark, root)
    return _contained(os.path.join(folder, check_name(name) + ".json"), folder)


def split_ref(ref: str) -> Tuple[str, Optional[int]]:
    """``NAME``, or ``NAME@N`` as recipes and commands made before sets were frozen name one: (name, N or None).
    ``latest`` reads N."""
    name, at, rev = str(ref).partition("@")
    if not at:
        return check_name(name), None
    if not rev.isdigit() or int(rev) < 1:
        raise Refused(f"{ref!r}: a task set's name")
    return check_name(name), int(rev)


def content_hash(benchmark: str, dataset: str, units: Iterable[str]) -> str:
    """The sha256 over what makes the membership: the benchmark, the dataset and the ids sorted (an order, a
    comment, a file's name say nothing)."""
    doc = {"benchmark": benchmark, "dataset": dataset, "units": sorted(set(str(u) for u in units))}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _hash_of(entry: dict, units: Iterable[str]) -> str:
    return content_hash(entry["benchmark"], entry["dataset"], units)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ids(units: Iterable[str], what: str) -> List[str]:
    ids = [str(u) for u in units]
    if any(not u or u.startswith("#") or u != u.strip() for u in ids):
        raise Refused(f"{what}: an id is a word with no space around it, not empty and not starting with #")
    twice = sorted({u for u in ids if ids.count(u) > 1})
    if twice:
        raise Refused(f"{what} lists {', '.join(twice)} more than once")
    if not ids:
        raise Refused(f"{what} lists no unit")
    return ids


def check(entry: dict, where: str) -> dict:
    """``entry`` as read from ``where``, its shape and every list's hash checked."""
    if not isinstance(entry, dict) or entry.get("schema") != SCHEMA:
        raise Refused(f"{where}: not a task set (schema {SCHEMA})")
    other = sorted(set(entry) - {"schema", *HEADER, "revisions"})
    if other and set(other) & set(EARLIER):
        raise Earlier(f"{where}: holds {', '.join(other)}, which a task set does not hold: an entry of the earlier "
                      "format, which kept a run's options. Delete it and make it again")
    if other:
        raise Refused(f"{where}: holds {', '.join(other)}, which a task set does not hold: a set is its dataset "
                      "and its ids, and a run gives its own options")
    for k in ("id", "name", "benchmark", "dataset"):
        if not isinstance(entry.get(k), str) or not entry[k]:
            raise Refused(f"{where}: no {k}")
    check_name(entry["name"])
    _bench_key(entry["benchmark"])
    if not isinstance(entry.get("description"), str):
        raise Refused(f"{where}: description is text")
    for k in ("label", "version"):
        if k in entry and (not isinstance(entry[k], str) or not entry[k]):
            raise Refused(f"{where}: {k} is text")
    revs = entry.get("revisions")
    if not isinstance(revs, list) or not revs:
        raise Refused(f"{where}: no list of units")
    seen = set()
    for r in revs:
        n = r.get("revision") if isinstance(r, dict) else None
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n in seen:
            raise Refused(f"{where}: a list's number is a whole number, each once")
        seen.add(n)
        if not isinstance(r.get("date"), str) or not r["date"]:
            raise Refused(f"{where}: list {n} has no date")
        if not isinstance(r.get("note"), str):
            raise Refused(f"{where}: list {n}'s note is text")
        if not isinstance(r.get("units"), list) or any(not isinstance(u, str) for u in r["units"]):
            raise Refused(f"{where}: list {n} lists no units")
        _ids(r["units"], f"{where}: list {n}")
        if r.get("hash") != _hash_of(entry, r["units"]):
            raise Refused(f"{where}: list {n}'s hash is not that of its units and dataset: "
                          "the file was changed by hand")
    return entry


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise NotFound(f"{path}: no such file")
    except (OSError, ValueError) as e:
        raise Refused(f"{path}: cannot read it as JSON: {e}")


def load(benchmark: str, name: str, root: Optional[str] = None) -> dict:
    path = path_of(benchmark, name, root)
    if not os.path.isfile(path):
        raise NotFound(f"no task set {name!r} of {benchmark} (in {os.path.dirname(path)})")
    entry = check(_read(path), path)
    if entry["name"] != name or entry["benchmark"] != benchmark:
        raise Refused(f"{path}: it names {entry['benchmark']}/{entry['name']}, not {benchmark}/{name}")
    return entry


def latest(entry: dict, rev: Optional[int] = None) -> dict:
    """The list ``entry`` holds: its latest, the only one of a set made since sets were frozen. ``rev``, the number a
    recipe or a command from before named it by, is taken when it is that list's, and refused otherwise."""
    last = max(entry["revisions"], key=lambda r: r["revision"])
    if rev is not None and rev != last["revision"]:
        raise NotFound(f"{entry['name']}@{rev} names an earlier list of the task set {label_of(entry)}: a set is "
                       f"read as its latest list now; name it {entry['name']}")
    return last


# the name the modules that read a set by its list number knew it by
revision = latest


def entries(benchmark: Optional[str] = None, root: Optional[str] = None) -> Tuple[List[dict], List[str]]:
    """Every entry (of ``benchmark``), by benchmark and name; and the files that could not be read."""
    base = _folder(None, root)
    got, bad = [], []
    try:
        benches = sorted(os.listdir(base)) if benchmark is None else [_bench_key(benchmark)]
    except FileNotFoundError:
        return [], []
    for b in benches:
        if not definitions._KEY.match(b):
            continue
        try:
            folder = _folder(b, root)
        except Refused as e:
            bad.append(str(e))
            continue
        if not os.path.isdir(folder):
            continue
        for fname in sorted(os.listdir(folder)):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            try:
                got.append(load(b, fname[:-5], root))
            except Refused as e:
                bad.append(str(e))
    return got, bad


def rows(benchmark: str, root: Optional[str] = None) -> List[dict]:
    """The readable entries of ``benchmark`` as a page lists them, by name: {name, label, description, dataset,
    version, units, date}, ``units`` the number of its ids, ``date`` the day its list was made, ``version`` ""
    for a set made before sets recorded one."""
    out = []
    for e in entries(benchmark, root)[0]:
        last = latest(e)
        out.append({"name": e["name"], "label": label_of(e), "description": e.get("description") or "", "dataset": e["dataset"],
                    "version": e.get("version") or "", "units": len(last["units"]), "date": last["date"]})
    return out


def by_id(benchmark: str, set_id: str, name: str = "", root: Optional[str] = None) -> Optional[dict]:
    """The readable entry of ``benchmark`` holding the id ``set_id``, whatever its name now; None when there is none.
    ``name``, the name it was known by, is read first."""
    if not set_id:
        return None
    if name:
        try:
            entry = load(benchmark, name, root)
        except Refused:
            entry = None
        if entry is not None and entry["id"] == set_id:
            return entry
    try:
        return next((e for e in entries(benchmark, root)[0] if e["id"] == set_id), None)
    except Refused:
        return None


def plain(e: Refused, benchmark: str, name: str) -> str:
    """Why the set ``name`` could not be read, in a line for a page: no file's path."""
    if isinstance(e, Earlier):
        return f"The task set {name} is of an earlier format, which kept a run's options: delete it and make it again."
    if isinstance(e, NotFound):
        return f"There is no task set {name}."
    return f"The task set {name} cannot be read: nullius standard set show {benchmark} {name} says why."


def write(entry: dict, root: Optional[str] = None) -> str:
    """The entry written whole, in place of the old file at once."""
    path = path_of(entry["benchmark"], entry["name"], root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ordered = {"schema": SCHEMA, **{k: entry[k] for k in HEADER if k in entry},
               "revisions": sorted(entry["revisions"], key=lambda r: r["revision"])}
    fd, tmp = tempfile.mkstemp(prefix=".set-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ordered, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def _dataset(bench: dict, dataset: Optional[str]) -> str:
    ids = [d["id"] for d in bench.get("datasets") or []]
    if not dataset:
        raise Refused(f"--dataset: the dataset the set is cut from, one of {', '.join(ids)}")
    if dataset not in ids:
        raise Refused(f"{bench['label']} has no dataset {dataset!r}: {', '.join(ids)}")
    return dataset


def create(bench: dict, label: str, *, dataset: str, units: List[str], description: str = "", note: str = "",
           root: Optional[str] = None) -> dict:
    """A new entry labelled ``label``, its name made from it (``name_for``), at the version ``bench`` is; written.
    A label another set has already gives the next free name."""
    label = check_label(label)
    entry = {"id": uuid.uuid4().hex, "name": name_for(bench["key"], label, root), "label": label,
             "description": description or "", "benchmark": bench["key"],
             **({"version": version_of(bench)} if version_of(bench) else {}), "dataset": _dataset(bench, dataset)}
    path = path_of(bench["key"], entry["name"], root)
    if os.path.lexists(path):
        raise Refused(f"a task set {entry['name']!r} of {bench['key']} exists already ({path})")
    ids = _ids(units, f"the set {label}")
    entry["revisions"] = [{"revision": 1, "date": _now(), "note": note or "", "units": ids, "hash": _hash_of(entry, ids)}]
    write(entry, root)
    return entry


def _file_of(benchmark: str, name: str, root: Optional[str]) -> str:
    """The entry's file as named in its folder (a link stays a link), refused when it leads out of the folder."""
    path_of(benchmark, name, root)
    path = os.path.join(_folder(benchmark, root), name + ".json")
    if not os.path.isfile(path):
        raise NotFound(f"no task set {name!r} of {benchmark} (in {os.path.dirname(path)})")
    return path


def rename(benchmark: str, name: str, label: str, root: Optional[str] = None) -> dict:
    """The entry labelled ``label`` and named by it (``name_for``, its own name free), its id, description and list
    kept: written under the new name, then the old file removed. Refused: a label it has already or one not
    allowed, and an entry that cannot be read."""
    old = _file_of(benchmark, name, root)
    entry = load(benchmark, name, root)
    label = check_label(label)
    if label == label_of(entry):
        raise Refused(f"the task set {name} is named {label!r} already")
    new = name_for(benchmark, label, root, but=name)
    if new != name and os.path.lexists(os.path.join(_folder(benchmark, root), new + ".json")):
        raise Refused(f"a task set {new!r} of {benchmark} exists already")
    entry = dict(entry, name=new, label=label)
    write(entry, root)
    if new != name:
        os.unlink(old)
    return entry


def delete(benchmark: str, name: str, root: Optional[str] = None) -> Optional[dict]:
    """Remove the entry's file: the entry as it was, or None when it could not be read (an earlier format, a file
    changed by hand). A job that ran it keeps its record."""
    path = _file_of(benchmark, name, root)
    try:
        entry = load(benchmark, name, root)
    except NotFound:
        raise
    except Refused:
        entry = None
    os.unlink(path)
    return entry


def _job_config(job_dir: str) -> dict:
    cfg_path = os.path.join(job_dir, "config.json")
    if not os.path.isfile(cfg_path):
        raise NotFound(f"{job_dir}: no job (no config.json)")
    cfg = _read(cfg_path)
    if cfg.get("runner") != "standard" or not isinstance(cfg.get("standard"), dict):
        raise Refused(f"{job_dir}: not a job of the Nullius standard")
    return cfg


def _place(trial: str) -> int:
    m = _TAG.search(trial)
    return int(m.group(1)) if m else 1 << 30


def outcome_names(given: Optional[List[str]]) -> set:
    """``--outcome O,...`` (repeatable): the pages' outcome names, or passed, failed, infra."""
    out = set()
    for word in (w.strip() for g in given or [] for w in g.split(",")):
        if not word:
            continue
        if word in GROUPS:
            out |= GROUPS[word]
        elif word in classify.ORDER:
            out.add(word)
        else:
            raise Refused(f"--outcome {word}: one of {', '.join(classify.ORDER)}, or {', '.join(GROUPS)}")
    return out


def _job_dataset(cfg: dict) -> str:
    return str(((cfg.get("datasets") or [{}])[0].get("name") or "")).rpartition("/")[2]


def from_job(bench: dict, job_dir: str, outcomes: Optional[List[str]] = None, named: Optional[List[str]] = None) -> dict:
    """What a set made from a job holds: {dataset, units, note}. The trials of the
    ``outcomes`` and the units ``named`` (each the full id a trial's ``standard.unit`` records), every unit
    the job ran when neither is given; a unit of several attempts is taken when any of them is."""
    from nullius.runs import parse
    from nullius.standard.runner import job
    job_dir = os.path.abspath(job_dir.rstrip("/"))
    cfg = _job_config(job_dir)
    std = cfg["standard"]
    if std.get("benchmark") != bench["key"]:
        raise Refused(f"{job_dir} ran {std.get('benchmark')}, not {bench['key']}")
    dataset = _job_dataset(cfg)
    wanted = outcome_names(outcomes)
    trials = []
    for trial_dir in job.trial_dirs(job_dir):
        res = _read(os.path.join(trial_dir, "result.json"))
        unit = (res.get("standard") or {}).get("unit")
        if unit is None:
            continue
        t = parse.parse_trial(os.path.basename(job_dir), trial_dir)
        trials.append((_place(os.path.basename(trial_dir)), str(unit), t.outcome if t else classify.UNKNOWN))
    trials.sort()
    held = list(dict.fromkeys(u for _, u, _ in trials))
    if not held:
        raise Refused(f"{job_dir}: no trial records its unit")
    names = list(dict.fromkeys(named or []))
    unknown = [u for u in names if u not in held]
    if unknown:
        raise Refused(f"{job_dir} has no trial of {', '.join(unknown)}")
    if wanted or names:
        chosen = {u for _, u, o in trials if o in wanted} | set(names)
        units = [u for u in held if u in chosen]
    else:
        units = held
    if not units:
        raise Refused(f"{job_dir} has no trial of the outcomes {', '.join(sorted(wanted))}")
    said = ([f"outcomes {','.join(outcomes)}"] if outcomes else []) + ([f"{len(names)} named"] if names else [])
    return {"dataset": dataset, "units": units, "note": f"from the job {cfg.get('job_name') or os.path.basename(job_dir)}"
            + (f": {'; '.join(said)}" if said else "")}


def record(entry: dict, rev: Optional[dict] = None) -> dict:
    """What a job's config keeps of the set it ran (``standard.task_set``): its id, name, label, the number and
    hash of its list, its units, and the version it was made at when it recorded one."""
    rev = rev or latest(entry)
    return {"id": entry["id"], "name": entry["name"], "label": label_of(entry), "revision": rev["revision"],
            "hash": rev["hash"], "units": rev["units"], **({"version": entry["version"]} if entry.get("version") else {})}


def for_run(bench: dict, ref: str, *, dataset: Optional[str] = None, units_given: bool = False,
            root: Optional[str] = None) -> Tuple[dict, str]:
    """``run --set NAME``: (the record the job keeps, the dataset), the set's dataset taken over the run's.
    A ``--units`` beside it, or another ``--dataset``, is refused. The run's benchmark options are its own, as
    on the whole dataset: the run then refuses an id its driver does not list under them."""
    name, rev = split_ref(ref)
    if units_given:
        raise Refused(f"--set {ref} and --units: the set lists the units, give one or the other")
    entry = load(bench["key"], name, root)
    got = latest(entry, rev)
    if dataset and dataset != entry["dataset"]:
        raise Refused(f"--dataset {dataset}: the task set {name} is cut from the dataset {entry['dataset']}")
    return record(entry, got), entry["dataset"]


def import_path(path: str, root: Optional[str] = None) -> Tuple[dict, bool]:
    """(the entry, whether it was written): an entry's file, or a job that ran a set (its ``standard.task_set``),
    written here when no set of that name is. When one is, with the same id and the same list, nothing is written.
    Refused: the entry here has another id, another entry holds the id, or the same id holds another list here."""
    path = os.path.abspath(path.rstrip("/"))
    if os.path.isdir(path):
        cfg = _job_config(path)
        rec = cfg["standard"].get("task_set")
        if not isinstance(rec, dict):
            raise NotFound(f"{path}: the job ran no task set")
        missing = [k for k in RECORD if k not in rec]
        if missing:
            raise Refused(f"{path}: its task set's record has no {', '.join(missing)}")
        incoming = {"schema": SCHEMA, "id": rec["id"], "name": rec["name"],
                    **{k: rec[k] for k in RECORD_MORE if k in rec}, "description": "",
                    "benchmark": cfg["standard"].get("benchmark"), "dataset": _job_dataset(cfg),
                    "revisions": [{"revision": rec["revision"], "date": _now(),
                                   "note": f"from the job {cfg.get('job_name') or os.path.basename(path)}",
                                   "units": rec["units"], "hash": rec["hash"]}]}
    elif os.path.isfile(path):
        incoming = _read(path)
    else:
        raise NotFound(f"{path}: no such file or job")
    check(incoming, path)
    bench, name = incoming["benchmark"], incoming["name"]
    others, _ = entries(bench, root)
    holder = next((e for e in others if e["id"] == incoming["id"] and e["name"] != name), None)
    if holder:
        raise Refused(f"the set {incoming['id']} is here already, named {holder['name']}")
    target = path_of(bench, name, root)
    if not os.path.exists(target):
        incoming = {k: v for k, v in incoming.items() if k != "schema"}
        write(incoming, root)
        return incoming, True
    here = load(bench, name, root)
    if here["id"] != incoming["id"]:
        raise Refused(f"a task set {name!r} of {bench} with another id is here already ({target})")
    if here["dataset"] != incoming["dataset"]:
        raise Refused(f"the set {name} here differs in its dataset: {here['dataset']!r}, not {incoming['dataset']!r}")
    mine, theirs = latest(here), latest(incoming)
    if mine["hash"] != theirs["hash"]:
        raise Refused(f"the set {name} holds another list here (hash {mine['hash'][:12]}, not {theirs['hash'][:12]}): "
                      "the same id with another list")
    return here, False
