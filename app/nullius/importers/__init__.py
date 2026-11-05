"""Importers for run trees this app did not produce.

The app walks one shape: a job directory whose subdirectories each carry a
``result.json``. Everything else — a run tree another harness wrote — is
converted into that shape rather than taught to the walker: a job directory
is all the app can walk, so emit one. That keeps `parse.py` a reader of one
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

and optionally ``LABEL``, how a person should see it named — the picker shows
it, and ``NAME.upper()`` is a poor guess for anything but an acronym —
``check``, ``(path) -> None``, which reads the tree the way `convert` would
and raises ``SystemExit`` for what `convert` would refuse, writing
nothing (without it, :func:`problem` converts into a scratch directory to
learn the same thing) — and ``trees``, ``(root) -> list[str]``, which names
the directories under a root the app opened that are one `convert` each.
Without it a claimed root is one tree, which is how the CLI reads it. The
picker uses the same boundaries so both surfaces produce the same jobs.

**Refusing a tree** is `convert` raising ``SystemExit`` with a message
that names the file and what is wrong with it — never a job with no trials,
which reads as a run that did nothing, and never a traceback, which reads as
the app breaking. The message is what a person sees, on the picker or from
the CLI, so it is redacted at construction: a tree is arbitrary bytes from
wherever a run was made, and a credential in a bad file's path or content
must not travel into an error just because the file was bad.

**Removing one is deleting its module**, plus its tests. Registration is by
discovery rather than a table here, so there is no second place that keeps
naming a harness after its file is gone — the failure mode a hand-maintained
registry has, where the import survives the deletion and the app stops
loading. Nothing outside a harness module mentions the harness.

An empty registry is a valid state - the CLI says ``no importers
registered`` and every run tree is refused as unrecognised.
"""
from __future__ import annotations

import importlib
import pkgutil
import shutil
import tempfile
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

    First match wins in name order, which is stable rather than clever: two
    harnesses claiming one directory is a conflict to fix in `detect`, not to
    arbitrate here by scoring.
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


def labels() -> str:
    return ", ".join(getattr(m, "LABEL", name) for name, m in available().items())
