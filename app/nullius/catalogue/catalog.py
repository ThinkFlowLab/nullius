"""The benchmark catalogue: which benchmarks exist, in what state, and where
each dataset comes from.

Bundled JSON files beside this module, overlaid by the user's catalogue,
one folder per benchmark. The app renders them as ``/benchmarks`` - a card
per benchmark - and
``/benchmarks/<key>``, one page per folder: how it runs, its datasets, its
limitations (the upstream's, then Nullius's), its citation. ``validate`` gates it.
The knowledge itself — what runs a benchmark, how its dataset is fetched —
is written once, here::

    benchmarks/<key>/             one folder per benchmark, named by its key
        benchmark.json            the entry, its datasets nested in it
        standard.json             its definition on the Nullius standard, when
                                  it runs there (nullius.standard)

Everything about a benchmark is in its folder. A dataset's ``benchmark`` is
the folder it sits in, never written; its key is unique across the whole
catalogue. ``load`` reads the folders in name order, so the catalogue's
order is the folders', and nothing depends on it: the page and the builder's
benchmark list sort by title, a lookup goes by key, and a benchmark's
datasets keep the order its entry writes them in. ``layout`` is the check on
the tree itself - a folder named after its key - and ``validate`` runs it.

A dataset's ``source`` is a short phrase (Harbor registry, Harbor hub,
GitHub, …). Each benchmark carries a ``category`` (Coding, Terminal, Customer
service, …) used as a tag and a filter.

Every entry says where it comes from (``origin``): ``official``, the
publisher's own benchmark, run the way its publisher certifies, or
``experiment``, one taken from wherever it was found - its official source or
not - and adapted here: published, but with flaws still to find. A card and
a page show it as a tag beside the category, and the Benchmarks page shelves
by it. Whether anyone has run it is not part of an entry.

Routes: a benchmark reaches a run through a harness, and ``Benchmark.routes``
says, for each harness in ``HARNESSES``, whether that route is usable here.
A harness the entry names is usable: the Nullius standard, or Harbor. The page
lists a benchmark with a usable route, and says why for one without. A
dataset is shown inside its
benchmark's entry, so it is listed exactly when its benchmark is.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from nullius import paths
from . import storage

# Where an entry comes from: the publisher's, run as it certifies (official),
# or found and adapted here, flaws still to find (experiment).
ORIGINS = ("official", "experiment")
ROOT = os.path.dirname(os.path.abspath(__file__))
BENCHMARKS = "benchmarks"          # the folder of benchmark folders, under ROOT
ENTRY = "benchmark.json"           # a benchmark's entry, in its folder

# The harnesses a benchmark can be run through, in the order the Benchmarks
# page lists them: the Nullius standard first (benchmarks/<key>/standard.json,
# nullius.standard), then Harbor, each a route of the entries that name it. A
# benchmark's own scripts run as the standard's Original setup.
HARNESSES = (("standard", "Nullius"), ("harbor", "Harbor"))



@dataclass(frozen=True)
class Route:
    """One harness route of a benchmark: usable, or unavailable with a reason.

    ``usable`` is the one thing a selector may act on. ``reason`` is filled
    only when it is not — the text a page shows greyed beside the route, so a
    reader learns *why* rather than only that it cannot be picked."""

    harness: str         # a key of HARNESSES: harbor or standard
    label: str           # its label: Harbor
    usable: bool
    reason: str = ""

    @property
    def summary(self) -> str:
        """The route as one phrase: `Harbor · runnable` or `Harbor · unavailable — …`."""
        return f"{self.label} · runnable" if self.usable else f"{self.label} · unavailable — {self.reason}"


@dataclass
class Benchmark:
    key: str
    title: str
    category: str = ""           # one tag: Coding, Terminal, Customer service, …
    domain: str = ""
    #: how a task is scored, in two or three words for its card; each dataset's verifier says it in full
    scoring: str = ""
    harness: list = field(default_factory=list)
    good_to_know: list = field(default_factory=list)
    #: what the upstream itself gets wrong or leaves out, as published: not Nullius's to fix
    upstream_limitations: list = field(default_factory=list)
    #: Nullius's own: how it runs the benchmark differently, what it does not offer yet
    limitations: list = field(default_factory=list)
    datasets: list = field(default_factory=list)
    #: declared per-phase timeouts in seconds (env_setup, agent_setup,
    #: agent_run, verify) as recorded from a real run; empty when no run has
    #: recorded them
    timeouts: dict = field(default_factory=dict)
    #: the publisher's, run as it certifies, or adapted here (ORIGINS)
    origin: str = "official"
    #: how it runs as published when Harbor runs it: the upstream's own agent, by
    #: the name Harbor registers it (``agent``), and why it is the published one
    #: (``says``); a benchmark on the standard says it in its ``setups`` instead
    published: dict = field(default_factory=dict)
    #: how its verifier's own report is read (``nullius.runs.verifier``):
    #: the file, the fields that say pass or fail, the words the pages use; empty
    #: when the reward is all there is
    report: dict = field(default_factory=dict)
    #: how to cite it: the BibTeX its official repository gives, else its paper's
    citation: str = ""
    #: where that citation was taken from
    citation_source: str = ""

    @property
    def label(self) -> str:
        return self.origin.capitalize()

    @property
    def can_run(self) -> bool:
        """Listed in the catalogue: a harness it names is here."""
        return bool(self.usable_routes)

    @property
    def routes(self) -> list:
        """Every harness, as a `Route`: usable when the entry names it, since
        a harness in `HARNESSES` is here; Harbor, named or not, says so when
        it is not this entry's."""
        out = []
        for harness, label in HARNESSES:
            if harness in self.harness:
                out.append(Route(harness, label, True))
            elif harness == "harbor":
                out.append(Route(harness, label, False, "no Harbor dataset"))
        return out

    @property
    def usable_routes(self) -> list:
        return [r for r in self.routes if r.usable]

    @property
    def blocker(self) -> str:
        """Why nothing is usable, in one phrase — empty when a route is: the
        reason a benchmark's page shows in place of Build a run."""
        if self.usable_routes:
            return ""
        missing = [h for h in self.harness if h not in dict(HARNESSES)]
        if missing:
            return "no harness here for " + ", ".join(missing)
        return "it names no harness"

    @property
    def route_case(self) -> str:
        """Which of the four discovery cases this entry is — what a test and a
        walk-through name: `harbor-only`, `multi-route`, `non-harbor` (usable
        routes, none of them Harbor) or `none`. A benchmark served only by the
        Nullius standard is `non-harbor`; one with both a Harbor adapter and a
        definition on the standard is `multi-route`."""
        usable = [r.harness for r in self.usable_routes]
        if not usable:
            return "none"
        if usable == ["harbor"]:
            return "harbor-only"
        return "multi-route" if "harbor" in usable else "non-harbor"


@dataclass
class Dataset:
    key: str
    title: str
    benchmark: str = ""
    tasks: int = 0
    official: str = ""
    source: str = ""             # where it comes from, in a phrase: Harbor hub, GitHub …
    registry: str = ""
    hub: str = ""
    #: how its tasks are scored
    verifier: str = ""
    notes: str = ""


def sort_key(title: str) -> str:
    """Alphabetical as a reader expects: a title starting with τ files under T."""
    return title.replace("τ", "t").replace("³", "3").lower()


@dataclass
class Catalog:
    benchmarks: list
    datasets: list
    path: str = ROOT             # explicit root, or the writable user catalogue
    _folders: dict = field(default_factory=dict, repr=False)

    def entry_dir(self, key: str) -> str:
        """The selected folder: all files come from the same entry copy."""
        return self._folders.get(key, os.path.join(self.path, BENCHMARKS, key))

    def benchmark(self, key: str):
        return next((b for b in self.benchmarks if b.key == key), None)

    def dataset(self, key: str):
        return next((d for d in self.datasets if d.key == key), None)

    def datasets_of(self, b: "Benchmark") -> list:
        """A benchmark's datasets, as `Dataset`, in the order its entry writes them."""
        return [d for d in (self.dataset(k) for k in b.datasets) if d]

    def tasks_of(self, b: "Benchmark") -> tuple:
        """Its tasks as a number and a label, for a card: ``("500", "tasks")``,
        ``("500", "tasks each")`` when its datasets agree, ``("10–89", "tasks")``
        when they do not, ``("—", "tasks")`` when none is recorded."""
        counts = [d.tasks for d in self.datasets_of(b) if d.tasks]
        if not counts:
            return "—", "tasks"
        if len(set(counts)) == 1:
            return f"{counts[0]:,}", "tasks each" if len(counts) > 1 else "tasks"
        return f"{min(counts):,}–{max(counts):,}", "tasks"

    def files(self, key: str) -> list:
        """The files in a benchmark's folder, by name: what its page shows it is made of."""
        base = self.entry_dir(key)
        try:
            return sorted(n for n in os.listdir(base)
                          if os.path.isfile(os.path.join(base, n)) and not n.startswith("."))
        except OSError:
            return []

    @property
    def listed_benchmarks(self) -> list:
        """What the catalogue shows (the Benchmarks tab, the rail): the
        benchmarks with a usable route, alphabetically."""
        return sorted((b for b in self.benchmarks if b.can_run),
                      key=lambda b: sort_key(b.title))

    @property
    def categories(self) -> list:
        """Every category the listed benchmarks use, alphabetically."""
        return sorted({b.category for b in self.listed_benchmarks if b.category}, key=sort_key)


def _pick(cls, data: dict):
    known = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in data.items() if k in known})


def _read(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def folders(root: str | None = None) -> list:
    """The benchmark folders under ``root``, by name: the catalogue's order.
    A folder holding nothing but Python's ``__pycache__`` is left out: git
    does not track the cache, so a checkout that once had a package there
    keeps the folder after the package moves."""
    return list(storage.folders(BENCHMARKS, ROOT, root))


def entry_dir(key: str, root: str | None = None) -> str | None:
    return storage.entry_dir(BENCHMARKS, key, ROOT, root)


def writable_dir(key: str, root: str | None = None) -> str:
    return storage.writable_dir(BENCHMARKS, key, ROOT, root)


def signature(root: str | None = None) -> tuple:
    """What changes when any file ``load`` reads changes, or a benchmark
    folder comes or goes: the app's cache reloads on it."""
    entries = [os.path.join(folder, ENTRY) for folder in storage.folders(BENCHMARKS, ROOT, root).values()]
    out = [(base, None) for base in storage.roots(ROOT, root)]
    for p in entries:
        try:
            info = os.stat(p)
            out.append((p, (info.st_mtime_ns, info.st_size)))
        except OSError:
            out.append((p, None))
    return tuple(out)


def load(root: str | None = None) -> Catalog:
    """Every ``benchmarks/<key>/benchmark.json`` under
    ``root``, the folders in name order. A benchmark's datasets are nested in
    its entry; each comes out as a `Dataset` naming the folder it sits in,
    and the entry's ``datasets`` as their keys, in the order written.

    With no root, user folders replace bundled folders of the same name.
    An explicit root reads that tree alone, without the user overlay.
    """
    benchmarks, datasets = [], []
    selected = storage.folders(BENCHMARKS, ROOT, root)
    for name, folder in selected.items():
        path = os.path.join(folder, ENTRY)
        if not os.path.isfile(path):
            continue                         # layout() names it; nothing to list
        try:
            benchmark, nested = _entry(path, name)
        except (OSError, ValueError, TypeError, AttributeError):
            continue                         # a hand-edited entry that does not read: layout() names it, every page still opens
        benchmarks.append(benchmark)
        datasets.extend(nested)
    return Catalog(
        benchmarks=benchmarks,
        datasets=datasets,
        path=os.fspath(root) if root is not None else paths.catalogue_dir(),
        _folders=selected,
    )


def _entry(path: str, name: str) -> tuple:
    """(Benchmark, [Dataset]) of one ``benchmark.json``; ValueError or
    TypeError when it is not JSON, not an object or lacks a field."""
    entry = _read(path)
    if not isinstance(entry, dict):
        raise ValueError("not a JSON object")
    if entry.get("experiment") or entry.get("origin") == "community":
        entry = {**entry, "origin": "experiment"}   # an entry written before origin said it
    nested = [d for d in entry.get("datasets", []) if isinstance(d, dict)]
    return (_pick(Benchmark, {**entry, "datasets": [d.get("key") for d in nested]}),
            [_pick(Dataset, {**d, "benchmark": name}) for d in nested])


def layout(root: str | None = None) -> list:
    """What is wrong with the tree itself, which no loaded object can show:
    a folder without its entry, an entry whose key is not its folder's name,
    a dataset that names another benchmark than the folder it sits in.
    Empty means the folders say what they hold."""
    return _layout(storage.folders(BENCHMARKS, ROOT, root))


def _layout(selected: dict) -> list:
    bad = []
    for name, folder in selected.items():
        where = f"{BENCHMARKS}/{name}/"
        entry = {}
        if not os.path.isfile(os.path.join(folder, ENTRY)):
            bad.append(f"{where} has no {ENTRY}")
        else:
            try:
                entry = _read(os.path.join(folder, ENTRY))
                _entry(os.path.join(folder, ENTRY), name)
            except (OSError, ValueError, TypeError, AttributeError) as e:
                bad.append(f"{where}{ENTRY}: {e}")
                entry = entry if isinstance(entry, dict) else {}
            if entry and entry.get("key") != name:
                bad.append(f"{where}{ENTRY}: key {entry.get('key')!r} is not its folder's name")
            if "standard" in entry.get("harness", []) and not os.path.isfile(os.path.join(folder, "standard.json")):
                bad.append(f"{where} names the Nullius standard but has no standard.json")
            for d in entry.get("datasets", []):
                if not isinstance(d, dict):
                    bad.append(f"{where}{ENTRY}: a dataset is a JSON object")
                elif d.get("benchmark", name) != name:
                    bad.append(f"{where}{ENTRY}: dataset {d.get('key')!r} names benchmark "
                               f"{d['benchmark']!r}, and sits in {name!r}")
    return bad


def validate(cat: Catalog) -> list:
    """Everything that would make a tab lie. Empty means consistent.

    The selected folders are checked too (`layout`): a folder named after
    its key, whether from the bundle, the user catalogue or an explicit root."""
    bad = []
    if cat._folders:
        bad.extend(_layout(cat._folders))
    elif os.path.isdir(os.path.join(cat.path, BENCHMARKS)):
        bad.extend(layout(cat.path))
    keys = [b.key for b in cat.benchmarks]
    if len(set(keys)) != len(keys):
        bad.append("duplicate benchmark keys")
    every = [d.key for d in cat.datasets]
    for k in sorted({k for k in every if every.count(k) > 1}):
        bad.append(f"dataset key {k!r} used twice: a dataset key is unique across the catalogue")
    dkeys = {d.key for d in cat.datasets}
    # a report block's sources name the job's dataset: one name, one way to read it
    claimed = {}
    for b in cat.benchmarks:
        for s in (b.report or {}).get("sources") or []:
            if s.strip().lower() in claimed:
                bad.append(f"{b.key}: report source {s!r} is {claimed[s.strip().lower()]}'s too")
            claimed.setdefault(s.strip().lower(), b.key)
    for b in cat.benchmarks:
        if b.origin not in ORIGINS:
            bad.append(f"{b.key}: origin is official or experiment")
        if b.published and not (isinstance(b.published, dict) and isinstance(b.published.get("agent"), str) and b.published["agent"]
                                and isinstance(b.published.get("says"), str) and b.published["says"]):
            bad.append(f"{b.key}: published is {{agent: a Harbor agent's name, says: why it is the published one}}")
        if not b.category:
            bad.append(f"{b.key}: no category")
        for h in b.harness:
            if h not in dict(HARNESSES):
                bad.append(f"{b.key}: harness {h!r} is not one of {[k for k, _ in HARNESSES]}")
        for d in b.datasets:
            if d not in dkeys:
                bad.append(f"{b.key}: dataset {d!r} is not in the catalogue")
    for d in cat.datasets:
        # A dataset is shown on its benchmark's page and
        # nowhere else, so one its benchmark does not list would never be seen.
        owner = cat.benchmark(d.benchmark) if d.benchmark else None
        if not d.benchmark:
            bad.append(f"dataset {d.key}: names no benchmark")
        elif owner is None:
            bad.append(f"dataset {d.key}: benchmark {d.benchmark!r} is not in the catalogue")
        elif d.key not in owner.datasets:
            bad.append(f"dataset {d.key}: benchmark {d.benchmark!r} does not list it")
        if not d.source:
            bad.append(f"dataset {d.key}: no source")
    return bad
