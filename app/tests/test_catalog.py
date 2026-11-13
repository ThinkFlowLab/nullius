"""The catalogue is consistent with the tree it describes.

Pinning `validate` here means a wrong status word fails the app suite.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import pathlib
import shutil
import unittest

from nullius.catalogue import catalog
from nullius.standard import definitions as standard_definitions

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
UUID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class Shipped(unittest.TestCase):
    def test_shipped_catalogue_is_consistent(self):
        cat = catalog.load(catalog.ROOT)
        self.assertEqual(catalog.validate(cat), [])
        self.assertTrue(any(b.origin == "official" for b in cat.benchmarks))
        # every benchmark's datasets resolve, and every dataset knows its benchmark
        for b in cat.benchmarks:
            for d in b.datasets:
                self.assertIsNotNone(cat.dataset(d), d)
        for d in cat.datasets:
            self.assertIsNotNone(cat.benchmark(d.benchmark), d.key)

    def test_an_entry_says_official_or_community_and_nothing_of_its_runs(self):
        for b in catalog.load(catalog.ROOT).benchmarks:
            self.assertIn(b.origin, catalog.ORIGINS, b.key)
        self.assertEqual(catalog.ORIGINS, ("official", "community"))
        for gone in ("status", "status_note", "branch", "experiment"):
            self.assertNotIn(gone, catalog.Benchmark.__dataclass_fields__)

    def test_tabs_are_alphabetical_and_lanes_are_not_benchmarks(self):
        cat = catalog.load(catalog.ROOT)
        titles = [b.title for b in cat.listed_benchmarks]
        self.assertEqual(titles, sorted(titles, key=catalog.sort_key))
        # what has a usable route is listed, whoever made it and whether or not it has run
        listed = {x.key for x in cat.listed_benchmarks}
        self.assertEqual(sorted(listed), ["gaia", "gsm8k", "humaneval", "locomo", "longmemeval", "osworld1", "swe-bench-verified", "tau3-bench", "terminal-bench"])
        self.assertEqual((cat.benchmark("longmemeval").origin, cat.benchmark("longmemeval").label), ("official", "Official"))
        self.assertEqual(cat.benchmark("tau3-bench").label, "Official")
        # τ³ files as "t3…", before "te…": a reader looking under T finds it
        self.assertLess(titles.index("τ³-bench"), titles.index("Terminal-Bench 2.0"))
        self.assertTrue(all(d.source for d in cat.datasets))

    def test_units_that_may_not_be_redistributed_are_named_nowhere(self):
        """No UUID-shaped string, a task id's shape, in such a benchmark's folder or its generated page.
        A failure names the files, never what was found."""
        held = {b["key"]: b["_here"] for b in standard_definitions.load("benchmark", standard_definitions.CATALOGUE, every=True)[0]
                if b["units"].get("redistribute") is False}
        self.assertIn("gaia", held)
        for key, here in held.items():
            files = [os.path.join(REPO, "docs", "benchmarks", "supported", f"{key}.md")]
            for folder, dirs, names in os.walk(here):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                files += [os.path.join(folder, n) for n in names]
            named = []
            for path in files:
                with open(path, "rb") as f:
                    if UUID.search(f.read()):
                        named.append(os.path.relpath(path, REPO))
            self.assertEqual(named, [], key)


class TaskCounts(unittest.TestCase):
    def catalogue(self, count=None):
        datasets = [catalog.Dataset(key=f"d{i}", title=f"D{i}", benchmark="overlap", tasks=n,
                                    source="Hugging Face") for i, n in enumerate((165, 53, 86, 26))]
        benchmark = catalog.Benchmark(key="overlap", title="Overlap", category="QA",
                                      datasets=[d.key for d in datasets], task_count=count)
        return catalog.Catalog(benchmarks=[benchmark], datasets=datasets), benchmark

    def test_distinct_headline_preserves_dataset_counts_and_builder_payload(self):
        from nullius.recipes import launch
        cat, benchmark = self.catalogue(165)
        self.assertEqual(catalog.validate(cat), [])
        self.assertEqual(cat.tasks_of(benchmark), ("165", "tasks"))
        self.assertEqual([d.tasks for d in cat.datasets_of(benchmark)], [165, 53, 86, 26])
        self.assertEqual(launch.payload(cat, [])["benchmarks"][0]["task_count"], 165)

    def test_no_explicit_count_retains_dataset_summary(self):
        cat, benchmark = self.catalogue()
        self.assertEqual(cat.tasks_of(benchmark), ("26–165", "tasks"))
        for dataset in cat.datasets:
            dataset.tasks = 500
        self.assertEqual(cat.tasks_of(benchmark), ("500", "tasks each"))

    def test_invalid_or_impossible_distinct_count_is_named(self):
        for count in (True, "165", 165.0, 0, -1, 164):
            with self.subTest(count=count):
                cat, _ = self.catalogue(count)
                self.assertTrue(any("task_count" in p for p in catalog.validate(cat)))


class DeclaredTimeouts(unittest.TestCase):
    """A benchmark's declared per-phase timeouts are recorded from a real run
    or not at all — the builder shows numbers only where a run has shown them."""

    PHASES = ("env_setup", "agent_setup", "agent_run", "verify")

    def test_tau3_carries_the_four_phases(self):
        t = catalog.load(catalog.ROOT).benchmark("tau3-bench").timeouts
        self.assertEqual({k: t[k] for k in self.PHASES}, {"env_setup": 600, "agent_setup": 1200, "agent_run": 3600, "verify": 300})

    def test_a_recording_is_complete_or_absent(self):
        for b in catalog.load(catalog.ROOT).benchmarks:
            with self.subTest(b.key):
                if not b.timeouts:
                    continue                                              # nothing recorded: the builder shows the rule only
                self.assertTrue(all(isinstance(b.timeouts.get(k), int) and b.timeouts[k] > 0 for k in self.PHASES), b.timeouts)


class Validation(unittest.TestCase):
    def test_bad_origin_is_named(self):
        cat = catalog.Catalog(
            benchmarks=[catalog.Benchmark(key="x", title="X", origin="maybe", datasets=["d"])],
            datasets=[])
        bad = catalog.validate(cat)
        self.assertIn("x: origin is official or community", bad)
        self.assertTrue(any("dataset 'd'" in m for m in bad))

    def test_a_dataset_its_benchmark_does_not_list_is_named(self):
        # The Benchmarks page shows a dataset only inside its benchmark's
        # entry: one that names no benchmark, or one its benchmark does not
        # list, would be shown nowhere.
        cat = catalog.Catalog(
            benchmarks=[catalog.Benchmark(key="x", title="X", category="C", datasets=["listed"])],
            datasets=[catalog.Dataset(key="listed", title="L", benchmark="x", source="GitHub"),
                      catalog.Dataset(key="stray", title="S", benchmark="x", source="GitHub"),
                      catalog.Dataset(key="orphan", title="O", source="GitHub")])
        bad = catalog.validate(cat)
        self.assertIn("dataset stray: benchmark 'x' does not list it", bad)
        self.assertIn("dataset orphan: names no benchmark", bad)
        self.assertFalse(any("dataset listed" in m for m in bad), bad)


class Folders(unittest.TestCase):
    """One folder per benchmark, named by its key, and everything about the
    benchmark in it: its entry, its datasets nested in the entry, a runner's
    definition beside them."""

    ROOT = pathlib.Path(catalog.ROOT)

    def test_every_folder_holds_an_entry_named_after_it(self):
        folders = catalog.folders(catalog.ROOT)
        self.assertTrue(folders)
        for name in folders:
            with self.subTest(name):
                entry = json.loads((self.ROOT / catalog.BENCHMARKS / name / catalog.ENTRY).read_text(encoding="utf-8"))
                self.assertEqual(entry["key"], name)
                for d in entry["datasets"]:
                    self.assertIsInstance(d, dict)              # nested in the entry, not a key pointing elsewhere
                    self.assertNotIn("benchmark", d)            # the folder says which
        self.assertEqual([b.key for b in catalog.load(catalog.ROOT).benchmarks], folders)
        self.assertEqual(catalog.layout(catalog.ROOT), [])

    def test_no_runner_definition_is_left(self):
        # a benchmark's own scripts run as the standard's Original setup; no folder keeps a runner.json
        self.assertEqual([f for f in sorted(os.listdir(self.ROOT / catalog.BENCHMARKS))
                          if (self.ROOT / catalog.BENCHMARKS / f / "runner.json").exists()], [])
        self.assertEqual(catalog.HARNESSES, (("standard", "Nullius"), ("harbor", "Harbor")))

    def test_an_entry_that_does_not_read_is_left_out_and_named(self):
        """A hand-edited user entry - a trailing comma, a list, no title -
        returned 500 on every page, Settings included."""
        with tempfile.TemporaryDirectory() as root:
            shutil.copytree(self.ROOT / catalog.BENCHMARKS / "gsm8k", pathlib.Path(root, catalog.BENCHMARKS, "gsm8k"))
            for name, text in (("comma", '{"key": "comma",}'), ("list", "[]"), ("bare", '{"key": "bare"}')):
                d = pathlib.Path(root, catalog.BENCHMARKS, name); d.mkdir()
                (d / catalog.ENTRY).write_text(text, encoding="utf-8")
            cat = catalog.load(root)
            self.assertEqual([b.key for b in cat.benchmarks], ["gsm8k"])
            problems = catalog.validate(cat)
            for name in ("comma", "list", "bare"):
                self.assertTrue(any(p.startswith(f"{catalog.BENCHMARKS}/{name}/") for p in problems), (name, problems))

    def test_no_dataset_key_repeats(self):
        keys = [d.key for d in catalog.load(catalog.ROOT).datasets]
        self.assertEqual(len(keys), len(set(keys)), keys)

    def test_validation_refuses_a_misnamed_folder_and_a_repeated_dataset(self):
        with tempfile.TemporaryDirectory() as root:
            def entry(folder, key, datasets):
                p = pathlib.Path(root, catalog.BENCHMARKS, folder)
                p.mkdir(parents=True)
                (p / catalog.ENTRY).write_text(json.dumps(
                    {"key": key, "title": key.upper(), "category": "Coding",
                     "datasets": [{"key": k, "title": k, "source": "GitHub"} for k in datasets]}))
                return p
            entry("a", "a", ["shared"])
            entry("b", "bee", ["shared"])
            entry("c", "c", [])
            pathlib.Path(root, catalog.BENCHMARKS, "empty").mkdir()
            cat = catalog.load(root)
            self.assertEqual(cat.path, root)
            self.assertEqual([b.key for b in cat.benchmarks], ["a", "bee", "c"])
            self.assertEqual([d.benchmark for d in cat.datasets], ["a", "b"])   # the folder, not what the entry claims
            bad = catalog.validate(cat)
            self.assertIn("benchmarks/b/benchmark.json: key 'bee' is not its folder's name", bad)
            self.assertIn("dataset key 'shared' used twice: a dataset key is unique across the catalogue", bad)
            self.assertIn("benchmarks/empty/ has no benchmark.json", bad)

    def test_a_folder_left_with_only_its_python_cache_is_not_a_benchmark(self):
        """A checkout that had a package there keeps its untracked __pycache__
        after the package moves; that folder is no benchmark and no problem."""
        with tempfile.TemporaryDirectory() as root:
            p = pathlib.Path(root, catalog.BENCHMARKS, "old_reader", "__pycache__")
            p.mkdir(parents=True); (p / "__init__.cpython-312.pyc").write_bytes(b"")
            self.assertEqual(catalog.folders(root), [])
            self.assertEqual(catalog.layout(root), [])


class Routes(unittest.TestCase):
    """A benchmark's routes: every harness named, usable or not and why. A
    listing alone is never a usable route: only a named harness with an
    adapter behind it."""

    def _bench(self, **kw):
        return catalog.Benchmark(key="k", title="T", category="Coding", **kw)

    def test_the_shipped_catalogue_carries_the_cases_its_harnesses_allow(self):
        cat = catalog.load(catalog.ROOT)
        cases = {b.key: b.route_case for b in cat.listed_benchmarks}
        self.assertEqual(cases["terminal-bench"], "multi-route")       # Harbor's dataset, and the standard (each task's own image)
        self.assertEqual(cases["swe-bench-verified"], "multi-route")     # Harbor's dataset, and the standard (each instance's own image)
        self.assertEqual(cases["longmemeval"], "non-harbor")           # the standard only
        self.assertEqual(cases["tau3-bench"], "multi-route")           # Harbor's adapter, and the standard
        # every listed benchmark falls in one of the three; Harbor is every entry's route, and the
        # standard only of the entries naming it (every Harbor benchmark here runs on the standard too)
        self.assertTrue(set(cases.values()) <= {"harbor-only", "multi-route", "non-harbor"})
        self.assertEqual(set(cases.values()), {"multi-route", "non-harbor"})
        for b in cat.listed_benchmarks:
            self.assertEqual([r.harness for r in b.routes], [k for k, _ in catalog.HARNESSES if k == "harbor" or k in b.harness], b.key)
        self.assertEqual([k for k, _ in catalog.HARNESSES], ["standard", "harbor"])      # Nullius first, everywhere
        self.assertEqual(cat.benchmark("longmemeval").harness, ["standard"])   # its own scripts: the standard's Original setup

    def test_every_listed_benchmark_says_how_to_cite_it_and_where_that_came_from(self):
        for b in catalog.load(catalog.ROOT).listed_benchmarks:
            self.assertTrue(b.citation.lstrip().startswith("@"), b.key)                 # BibTeX, as its repository gives it
            self.assertEqual(b.citation.count("{"), b.citation.count("}"), b.key)
            self.assertTrue(b.citation_source.startswith("https://"), b.key)

    def test_a_benchmark_on_both_names_both_usable_nullius_first(self):
        tb = catalog.load(catalog.ROOT).benchmark("swe-bench-verified")
        self.assertEqual([r.summary for r in tb.routes], ["Nullius · runnable", "Harbor · runnable"])
        self.assertEqual([r.harness for r in tb.usable_routes], ["standard", "harbor"])
        self.assertEqual(tb.blocker, "")

    def test_a_named_harness_that_is_here_is_usable_run_or_not(self):
        b = self._bench(harness=["harbor"], origin="community")
        self.assertEqual([r.summary for r in b.routes], ["Harbor · runnable"])
        self.assertEqual((b.route_case, b.blocker), ("harbor-only", ""))

    def test_no_usable_route_says_what_is_missing(self):
        none = self._bench()
        self.assertEqual((none.usable_routes, none.blocker), ([], "it names no harness"))
        elsewhere = self._bench(harness=["docker"])
        self.assertEqual(elsewhere.blocker, "no harness here for docker")
        self.assertEqual(elsewhere.route_case, "none")
        cat = catalog.Catalog(benchmarks=[elsewhere], datasets=[])
        self.assertTrue(any("harness 'docker'" in m for m in catalog.validate(cat)))
        self.assertEqual(cat.listed_benchmarks, [])

    def test_legacy_experiments_become_official_and_community_stays_community(self):
        with tempfile.TemporaryDirectory() as root:
            for key, extra in (("old-exp", {"status": "blocked", "experiment": True}),
                               ("old-origin", {"origin": "experiment"}), ("community", {"origin": "community"})):
                p = pathlib.Path(root, catalog.BENCHMARKS, key); p.mkdir(parents=True)
                (p / catalog.ENTRY).write_text(json.dumps(
                    {"key": key, "title": key, "category": "Coding", "harness": ["harbor"], "datasets": [], **extra}))
            cat = catalog.load(root)
            self.assertEqual([(b.key, b.origin, b.blocker) for b in cat.benchmarks],
                             [("community", "community", ""), ("old-exp", "official", ""), ("old-origin", "official", "")])


class Example(unittest.TestCase):
    """docs/examples/new-benchmark is what a person copies to add a benchmark by
    hand: its two folders are benchmark folders as the catalogue holds them, so
    the catalogue's own checks read them, and an example that stopped passing
    them would teach a broken entry."""

    EXAMPLES = os.path.join(REPO, "docs", "examples", "new-benchmark")

    def setUp(self):
        # a catalogue root around the two folders: benchmarks/<key>/, as load() reads it
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        self.ROOT = tmp
        for folder in ("harbor",):                          # copied under its own key, as the catalogue names it
            with open(os.path.join(self.EXAMPLES, folder, catalog.ENTRY), encoding="utf-8") as fh:
                key = json.load(fh)["key"]
            shutil.copytree(os.path.join(self.EXAMPLES, folder), os.path.join(tmp, catalog.BENCHMARKS, key))

    def test_the_examples_pass_the_catalogue_checks(self):
        cat = catalog.load(self.ROOT)
        self.assertEqual([b.key for b in cat.benchmarks], ["example-harbor"])
        self.assertEqual(catalog.validate(cat), [])
        self.assertEqual([b.key for b in cat.listed_benchmarks], ["example-harbor"])
        self.assertEqual({b.origin for b in cat.benchmarks}, {"official"})

class ReportSources(unittest.TestCase):
    def test_a_source_two_benchmarks_claim_is_named(self):
        spec = {"sources": ["shared-name"]}
        cat = catalog.Catalog(benchmarks=[catalog.Benchmark(key="a", title="A", category="C", report=spec),
                                          catalog.Benchmark(key="b", title="B", category="C", report=spec)], datasets=[])
        self.assertIn("b: report source 'shared-name' is a's too", catalog.validate(cat))


if __name__ == "__main__":
    unittest.main()
