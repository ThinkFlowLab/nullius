"""Importers for run trees this app did not produce.

The app walks one shape: a job directory whose subdirectories each carry a
``result.json``. Everything else — a run tree another harness wrote — is
converted into that shape rather than taught to the walker. That keeps `parse.py` a reader of one
format no matter how many harnesses there are.

An importer knows how one harness lays out a whole run on disk; what a
dataset's verifier report means is `nullius.runs.verifier`'s. A new dataset
under a harness already read needs nothing here.

**Adding one** is a module in this package exposing three names:

    NAME     str                    what to pass to --harness
    detect   (path) -> bool         does this directory look like ours?
    convert  (src, out_root, job_name="", command="") -> list[str]

`convert` returns job directories, plural: one run tree is not always one
job. A directory that accumulates runs across sittings holds several, and
pooling them would report the idle time between as though it were work.

and optionally ``LABEL``, how a person should see it named —
``check``, ``(path) -> None``, which reads the tree the way `convert` would
and raises ``SystemExit`` for what `convert` would refuse, writing
nothing (without it, :func:`problem` converts into a scratch directory to
learn the same thing) — and ``trees``, ``(root) -> list[str]``, which names
the directories under a root the app opened that are one `convert` each.
Without it a claimed root is one tree, which is how the CLI reads it. The
picker uses the same boundaries so both surfaces produce the same jobs.

**One source, one job folder.** A tree is a source, known by its real
path. Its first conversion gives it an id of its own (:func:`place`), derived
from the format and that path so every workspace importing it agrees, written
in the job's ``config.json`` as ``nullius.source`` and read back on every
reconversion, so the folder and the identity stay its own: a second source of
one basename gets a folder of its own. A folder converted before the id is
kept as it is, without one.

**Refusing a tree** is `convert` raising ``SystemExit`` with a message
that names the file and what is wrong with it — never a job with no trials or a traceback. The message is what a person sees, on the picker or from
the CLI, so it is redacted at construction: a tree is arbitrary bytes from
wherever a run was made, and a credential in a bad file's path or content
must not travel into an error just because the file was bad.

**Removing one is deleting its module**, plus its tests. Registration is by
discovery rather than a table here. Nothing outside a harness module mentions the harness.
"""
from __future__ import annotations

import importlib
import json
import os
import pkgutil
import shutil
import tempfile
import uuid
from collections import Counter
from types import ModuleType

from nullius import ondisk
from nullius.redact import redact

# A module must expose all three to count. A half-written one is skipped
# rather than half-registered, so an editor's stray file cannot break `--list`.
_CONTRACT = ("NAME", "detect", "convert")


def available() -> dict[str, ModuleType]:
    """Every importer in this package, keyed by its NAME."""
    found: dict[str, ModuleType] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except ImportError:
            continue
        if all(hasattr(module, attr) for attr in _CONTRACT):
            found[getattr(module, "NAME")] = module
    return dict(sorted(found.items()))


def get(name: str) -> ModuleType | None:
    """The importer called *name*, or None."""
    return available().get((name or "").strip().lower())


def detect(path: str) -> ModuleType | None:
    """The importer that recognises *path*, or None when nothing does.

    First match wins in name order.
    """
    for module in available().values():
        try:
            if module.detect(path):
                return module
        except OSError:
            continue
    return None


def trees(module: ModuleType, root: str) -> list[str]:
    """The trees under *root* that *module* converts one job each.

    The importer's own ``trees`` when it has one; otherwise *root* itself
    when the importer claims it, and nothing when it does not. A caller
    converts each entry as it would the root a person named on the CLI, so
    the two agree on what one job is.
    """
    if hasattr(module, "trees"):
        return list(module.trees(root))
    return [root] if module.detect(root) else []


def problem(path: str) -> str | None:
    """Why the importer that claims *path* cannot read it — or None.

    None also when nothing claims *path*: an unclaimed root is one for the
    Harbor reader, and whether it holds a job is that reader's question.
    The message is redacted whichever way it arrived, so a caller can show
    it as it is.
    """
    module = detect(path)
    if module is None:
        return None
    try:
        if hasattr(module, "check"):
            module.check(path)
        else:
            scratch = tempfile.mkdtemp(prefix="nullius-check-")
            try:
                module.convert(path, scratch)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
    except (OSError, SystemExit) as e:
        return redact(str(e) or f"{module.NAME} could not read it")
    return None


def write_if_changed(path: str, data: bytes) -> bool:
    """Write *data* to *path* unless the file already holds it; True when
    written, whole, renamed over the old file: a refresh cut short leaves the
    old one. A conversion runs at each open of a workspace; an unchanged one
    keeps its mtimes, and with them the parse cache that keys on them."""
    try:
        with open(path, "rb") as fh:
            if fh.read() == data:
                return False
    except OSError:
        pass
    ondisk.write_whole(path, data)
    return True


def _reserved(job_dir: str) -> dict:
    """Trial name -> the document it was given to, as the job's ``config.json``
    keeps it across conversions: a name stays its document's after its folder
    is pruned."""
    try:
        with open(os.path.join(job_dir, "config.json"), encoding="utf-8") as fh:
            reserved = json.load(fh)["trial_documents"]
        return {str(k): str(v) for k, v in reserved.items() if v}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def reservations(job_dir: str, written, existing: dict = None, merged: set = frozenset()) -> dict:
    """The ``trial_documents`` to keep in *job_dir*'s ``config.json``: what
    it kept, and the folders *existing* held before pruning (but a *merged* one or one whose document
    could not be told), with *written* ((name, document) pairs) on top."""
    reserved = _reserved(job_dir)
    reserved.update((name, identity) for name, identity in (existing or {}).items()
                    if identity and name not in merged and name not in reserved)
    reserved.update((name, identity) for name, identity in written if identity)
    return dict(sorted(reserved.items()))


def existing_trials(job_dir: str, identity_of) -> tuple[dict, set]:
    """The trials a previous conversion left in *job_dir*: name -> the
    document each holds, by ``identity_of(trial_dir)`` (None when the files
    cannot tell), with the names reserved for documents whose folders are
    gone, and the names of folders two documents were once written
    into: a name its ``config.json`` lists twice among its tasks, or, without
    that list, any name when the job counted more trials than it has folders."""
    trials, merged = _folders(job_dir, identity_of)
    held = set(trials.values())
    for name, identity in _reserved(job_dir).items():
        if name not in trials and identity not in held:
            trials[name] = identity
    return trials, merged


def _folders(job_dir: str, identity_of) -> tuple[dict, set]:
    trials = {}
    if os.path.isdir(job_dir):
        for name in sorted(os.listdir(job_dir)):
            if os.path.isdir(os.path.join(job_dir, name)):
                trials[name] = identity_of(os.path.join(job_dir, name))
    try:
        with open(os.path.join(job_dir, "config.json"), encoding="utf-8") as fh:
            tasks = json.load(fh)["tasks"]
        listed = Counter(str(t["name"]).split("/", 1)[-1] for t in tasks)
        return trials, {name for name, n in listed.items() if n > 1}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        with open(os.path.join(job_dir, "result.json"), encoding="utf-8") as fh:
            counted = json.load(fh)["n_total_trials"]
    except (OSError, ValueError, KeyError, TypeError):
        counted = len(trials)
    # shortcut: a folder deleted by hand also counts, so any collision splits; only jobs without a tasks list
    return trials, set(trials) if isinstance(counted, int) and counted > len(trials) else set()


def unique_names(documents: list[tuple], existing: dict | None = None,
                 merged: set = frozenset()) -> list[str]:
    """One trial directory per document, decided before anything is written.

    Each document is (identity, name, longer name). A document whose trial
    already exists keeps that trial's name: judgements are keyed by it. A
    folder whose document is known stays that document's, even when it is
    gone. A folder whose document cannot be told keeps its name for the first
    document that would take it. Any other document takes its name unless
    another holds it, else the longer one. A *merged* folder (two documents
    written into one by an older conversion) is split: each of its documents
    takes the longer name. Two documents still left with one name, or a
    document left with a gone document's, are refused.
    """
    existing = existing or {}
    identities = {identity for identity, _, _ in documents}
    held = {identity: name for name, identity in existing.items() if identity in identities}
    unknown = {name for name, identity in existing.items() if identity is None}
    gone = {name for name, identity in existing.items() if identity is not None and identity not in identities}
    shorts = Counter(name for _, name, _ in documents)
    split = {name for name, n in shorts.items() if n > 1 and name in existing and name in merged}
    out = [held.get(identity) if held.get(identity) not in split else None
           for identity, _, _ in documents]
    taken = {name for name in out if name} | gone
    for i, (_, name, longer) in enumerate(documents):
        if out[i] is None:
            free = name not in taken and name not in split
            out[i] = name if free and (shorts[name] == 1 or name in unknown) else longer
            taken.add(out[i])
    shared = sorted({name for name, n in Counter(out).items() if n > 1} | (set(out) & gone))
    if shared:
        raise SystemExit(redact(f"two documents would share the trial {shared[0]!r}"))
    return out


def mark_changed(job_dir: str, summaries: list) -> None:
    """Move the job's own ``result.json`` when one of its trials was written:
    an ended job's cached parse is checked by that file, not trial by trial."""
    if any(s["changed"] for s in summaries):
        os.utime(os.path.join(job_dir, "result.json"))


def _config(job_dir: str) -> dict:
    try:
        with open(os.path.join(job_dir, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _source(cfg: dict) -> tuple:
    """``(id, path)`` a conversion recorded in ``nullius``, or ``(None, None)``."""
    own = cfg.get("nullius") if isinstance(cfg.get("nullius"), dict) else {}
    try:
        return str(uuid.UUID(str(own["source"]))), own.get("source_path")
    except (KeyError, ValueError, TypeError):
        return None, None


#: fixed for good: changing it changes every import's identity
NS_SOURCE = uuid.UUID("3b1d7e44-9a0c-4f62-8e15-2c7a9f4d6b08")


def source_id(fmt: str, root: str) -> str:
    """A source's id: a ``uuid5`` over its format and real path, ``"v": 1``; a moved tree is a new source."""
    facts = {"v": 1, "format": fmt, "path": os.path.realpath(root)}
    return str(uuid.uuid5(NS_SOURCE, json.dumps(facts, sort_keys=True, separators=(",", ":"))))


def place(out_root: str, root: str, default: str, job_name: str = "", recorded=None, fmt: str = "") -> tuple:
    """``(job_name, source id)``: the folder under *out_root* the tree at *root*
    is converted into, and its id, None for a folder converted before ids.

    The folder that already records this source is its own. Otherwise *default*,
    then ``<default>-2``, ``-3``…, skipping a folder that belongs to another
    source: one recording another path, or an older one whose
    ``recorded(config)`` names another path. A named *job_name* is used as it is.
    """
    src, new = os.path.realpath(root), source_id(fmt, root)
    names = sorted(os.listdir(out_root)) if not job_name and os.path.isdir(out_root) else []
    for name in names:
        sid, path = _source(_config(os.path.join(out_root, name)))
        if sid and path == src:
            return name, sid
    n = 1
    while True:
        name = job_name or (default if n == 1 else f"{default}-{n}")
        job_dir = os.path.join(out_root, name)
        if not os.path.isdir(job_dir):
            return name, new
        cfg = _config(job_dir)
        sid, path = _source(cfg)
        if job_name:                     # named: written there as asked; an older folder keeps having no id
            return name, (sid if path == src else new) if sid else None
        if not sid:
            old = recorded(cfg) if recorded and cfg else None
            if not old or os.path.realpath(old) == src or os.path.normpath(old) == os.path.normpath(root):
                return name, None
        n += 1


def recorded_as(sid, root: str) -> dict:
    """What a job's ``config.json`` carries of its source: nothing for a folder converted before ids."""
    return {"nullius": {"source": sid, "source_path": os.path.realpath(root)}} if sid else {}


def labels() -> str:
    return ", ".join(getattr(m, "LABEL", name) for name, m in available().items())
