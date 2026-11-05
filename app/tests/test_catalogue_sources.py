"""Where a dataset's data comes from, one row per way its benchmark runs."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nullius.catalogue import catalog, sources  # noqa: E402
from nullius.standard import definitions  # noqa: E402

COMMIT = "0123456789abcdef0123456789abcdef01234567"


class SourcesTest(unittest.TestCase):
    def test_one_row_per_way_in_the_page_order(self):
        d = catalog.Dataset(key="main", title="Main", hub="org/bench", registry="org/bench")
        std = {"upstream": {"repository": "https://github.com/org/bench", "tested": COMMIT, "tag": "v1.0"},
               "datasets": [{"id": "main", "path": "data/main.json", "sha256": "a" * 64}]}
        rows = sources.of(d, ["harbor", "standard"], std)
        self.assertEqual([r["label"] for r in rows], ["Nullius", "Harbor"])
        nullius, harbor = rows
        self.assertEqual((harbor["from"], harbor["name"]), ("Harbor hub", "org/bench"))
        self.assertEqual((nullius["from"], nullius["name"], nullius["pin"]), ("GitHub", "org/bench", "v1.0 · 0123456"))
        self.assertEqual(nullius["href"], f"https://github.com/org/bench/tree/{COMMIT}")
        self.assertEqual(nullius["files"], [{"name": "data/main.json", "size": "", "checked": True}])

    def test_a_definition_read_from_hugging_face_names_it(self):
        d = catalog.Dataset(key="main", title="Main")
        std = {"upstream": {"repository": "https://github.com/org/bench", "tested": COMMIT},
               "datasets": [{"id": "main", "url": f"https://huggingface.co/datasets/org/data/resolve/{COMMIT}/main.json",
                             "sha256": "b" * 64, "bytes": 2_800_000}]}
        (row,) = sources.of(d, ["standard"], std)
        self.assertEqual((row["from"], row["name"], row["pin"]), ("Hugging Face", "org/data", "0123456"))
        self.assertEqual(row["files"][0]["size"], "2.8 MB")

    def test_no_harbor_row_without_a_harbor_name(self):
        d = catalog.Dataset(key="main", title="Main", source="GitHub")
        self.assertEqual(sources.of(d, ["harbor"]), [])

    def test_a_definition_dataset_matches_by_id_or_is_the_only_one(self):
        std = {"upstream": {"repository": "https://github.com/org/b", "tested": COMMIT},
               "datasets": [{"id": "set_a", "path": "a.json"}, {"id": "set_b", "path": "b.json"}]}
        a = catalog.Dataset(key="set-a", title="A")
        self.assertEqual([f["name"] for f in sources.of(a, ["standard"], std, only=False)[0]["files"]], ["a.json"])
        other = catalog.Dataset(key="whole", title="Whole")
        self.assertEqual(sources.of(other, ["standard"], std, only=False)[0]["files"], [])
        self.assertEqual(len(sources.of(other, ["standard"], std, only=True)[0]["files"]), 2)

    def test_every_listed_benchmark_names_a_source_for_each_way_it_runs(self):
        cat = catalog.load()
        stds = definitions.load("benchmark")[0]
        for b in cat.listed_benchmarks:
            std = definitions.find(stds, b.key) if "standard" in b.harness else None
            ds = cat.datasets_of(b)
            for d in ds:
                rows = sources.of(d, b.harness, std, only=len(ds) == 1)
                ways = {r["way"] for r in rows}
                with self.subTest(dataset=d.key):
                    if "standard" in b.harness:
                        self.assertIn("standard", ways)
                    for r in rows:
                        self.assertTrue(r["name"] and r["by"], r)


if __name__ == "__main__":
    unittest.main()
