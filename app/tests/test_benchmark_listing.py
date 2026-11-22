"""The benchmarks the Benchmarks page and the builder's Benchmark step list (recipe.benchmarks): the catalogue's
listed entries, each with what it is run with."""
from __future__ import annotations

import unittest

from nullius.catalogue import catalog
from nullius.standard import recipe


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


if __name__ == "__main__":
    unittest.main()
