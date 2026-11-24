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
from collections import Counter
from types import ModuleType

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
    written. A conversion runs at each open of a workspace; an unchanged one
    keeps its mtimes, and with them the parse cache that keys on them."""
    try:
        with open(path, "rb") as fh:
            if fh.read() == data:
                return False
    except OSError:
        pass
    with open(path, "wb") as fh:
        fh.write(data)
    return True


def existing_trials(job_dir: str, identity_of) -> tuple[dict, set]:
    """The trials a previous conversion left in *job_dir*: name -> the
    document each holds, by ``identity_of(trial_dir)`` (None when the files
    cannot tell), and the names of folders two documents were once written
    into: a name its ``config.json`` lists twice among its tasks, or, without
    that list, any name when the job counted more trials than it has folders."""
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
    takes the longer name. Two documents still left with one name are refused.
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
    shared = sorted(name for name, n in Counter(out).items() if n > 1)
    if shared:
        raise SystemExit(redact(f"two documents would share the trial {shared[0]!r}"))
    return out


def mark_changed(job_dir: str, summaries: list) -> None:
    """Move the job's own ``result.json`` when one of its trials was written:
    an ended job's cached parse is checked by that file, not trial by trial."""
    if any(s["changed"] for s in summaries):
        os.utime(os.path.join(job_dir, "result.json"))


def labels() -> str:
    return ", ".join(getattr(m, "LABEL", name) for name, m in available().items())
