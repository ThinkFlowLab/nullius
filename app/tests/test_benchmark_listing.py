"""The benchmarks the Benchmarks page and the builder's Benchmark step list (recipe.benchmarks): the catalogue's
listed entries, each with what it is run with."""
from __future__ import annotations

import unittest
from unittest import mock

from nullius.catalogue import catalog
from nullius.standard.contract import definitions
from nullius.standard.pages import recipe
from nullius.standard.requirements import hostreq


class Listing(unittest.TestCase):
    def test_it_is_the_catalogues_listed_benchmarks_with_their_family(self):
        cat = catalog.load(catalog.ROOT)
        rows = recipe.benchmarks(cat)
        self.assertEqual([r["b"].key for r in rows], [b.key for b in cat.listed_benchmarks])
        by = {r["b"].key: r for r in rows}
        self.assertEqual((by["locomo"]["family"], by["locomo"]["kind"]), ("capability", "memory"))
        self.assertEqual((by["gsm8k"]["family"], by["gsm8k"]["kind"]), ("agent", ""))
        self.assertEqual((by["gsm8k"]["tasks"], by["gsm8k"]["unit"]), cat.tasks_of(cat.benchmark("gsm8k")))
        self.assertTrue(all(r["family"] in ("agent", "capability") for r in rows))

    def test_a_benchmark_with_versions_counts_each_ones_tasks(self):
        cat = catalog.load(catalog.ROOT)
        os1 = next(r for r in recipe.benchmarks(cat) if r["b"].key == "osworld1")
        b = cat.benchmark("osworld1")
        self.assertEqual((os1["tasks"], os1["unit"]), cat.tasks_of(b))                       # the default version
        self.assertEqual(set(os1["versions"]), set(b.versions))
        for v in b.versions:
            self.assertEqual(os1["versions"][v], cat.tasks_of(b, cat.in_version(b, v)[1]))
        self.assertEqual(next(r for r in recipe.benchmarks(cat) if r["b"].key == "gsm8k")["versions"], {})


    def test_what_its_options_need_of_a_machine_is_named_by_label_and_by_key_without_one(self):
        cat = catalog.load(catalog.ROOT)
        by = {r["b"].key: r for r in recipe.benchmarks(cat)}
        self.assertEqual(by["locomo"]["host"], {"cuda_gpu": "NVIDIA GPU"})
        self.assertEqual(by["osworld2"]["host"], {"docker": "Docker", "scoring_programs": "Scoring tools"})
        self.assertIsNone(by["gsm8k"]["host"])
        defs, bad = definitions.load("benchmark")
        bare = [dict(d, host={n: {k: v for k, v in r.items() if k != "label"} for n, r in d["host"].items()}) if d.get("host") else d
                for d in defs]
        with mock.patch.object(definitions, "load", return_value=(bare, bad)):
            self.assertEqual({r["b"].key: r["host"] for r in recipe.benchmarks(cat)}["longmemeval"], {"cuda_gpu": "cuda_gpu", "stella_gpu": "stella_gpu"})


class HostLabels(unittest.TestCase):
    def test_a_requirement_is_named_by_its_label_else_its_key(self):
        bench = {"host": {"gpu": {"label": "NVIDIA GPU", "summary": "s", "check": ["nvidia-smi"]}, "rg": {"summary": "s", "check": ["rg"]}}}
        self.assertEqual([hostreq.label(bench, n) for n in ("gpu", "rg", "gone")], ["NVIDIA GPU", "rg", "gone"])

    def test_a_label_is_a_nonempty_string(self):
        req = {"summary": "s", "check": ["nvidia-smi"]}
        self.assertEqual(definitions._host({"host": {"gpu": dict(req, label="NVIDIA GPU")}}), [])
        for value in ("", "  ", None, 3, ["GPU"]):
            with self.subTest(value=value):
                self.assertIn("host.gpu.label: its name on a page", definitions._host({"host": {"gpu": dict(req, label=value)}}))

    def test_every_bundled_requirement_carries_a_label(self):
        for d in definitions.load("benchmark")[0]:
            for name, r in (d.get("host") or {}).items():
                with self.subTest(benchmark=d["key"], requirement=name):
                    self.assertTrue(r.get("label"))


if __name__ == "__main__":
    unittest.main()
