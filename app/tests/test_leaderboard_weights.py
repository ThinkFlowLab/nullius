"""Profile scoring validates complete documents and normalizes measured sources."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nullius.leaderboard import configuration, weights


class WeightExamples(unittest.TestCase):
    def test_c5_independent_weights_shares_and_missing(self):
        self.assertEqual(weights.combine([(.5, 1), (1, 1)]), (.75, [0.5, 0.5]))
        self.assertEqual(weights.combine([(.5, 3), (1, 1)]), (.625, [.75, .25]))
        self.assertEqual(weights.combine([(.5, 3), (None, 1)]), (.5, [1, 0]))
        self.assertEqual(weights.combine([(.625, 1), (0, 1)])[0], .3125)
        self.assertEqual(weights.combine([(.625, 3), (0, 1)])[0], .46875)
        self.assertEqual(weights.percent(.46875), "46.9%")
        self.assertEqual(weights.combine([(.625, 1), (0, 0)])[0], .625)
        self.assertEqual(weights.combine([(.5, 1), (1, 1)])[0], .75)
        self.assertEqual(weights.combine([(.5, 1), (1, 0)])[0], .5)
        self.assertEqual(weights.combine([(1, 0)]), (None, [0]))

    def test_c5_finite_weights_do_not_overflow_their_shares(self):
        self.assertEqual(weights.combine([(.5, 1e308), (1, 1e308)]), (.75, [.5, .5]))

    def test_c7_unrounded_competition_ranks_and_groups(self):
        rows = [{"key": key, "name": key, "overall": score, "reasons": reasons}
                for key, score, reasons in [("partial", 1, ["GAIA 1 of 100 tasks"]),
                                            ("a", .6, []), ("b", .6, []), ("c", .59999, []),
                                            ("none", None, [])]]
        result = weights.rank(rows)
        self.assertEqual([(r["key"], r["rank"]) for r in result],
                         [("partial", 1), ("a", 2), ("b", 2), ("c", 4), ("none", None)])
        self.assertEqual(weights.rank(rows, sort="name", descending=True)[0]["key"], "partial")
        # one mean summed in two orders differs in its last bit: the same score, the same rank, the names' order
        noisy = .9 * .5 + .8 * .5
        self.assertNotEqual(noisy, .85)
        tied = weights.rank([{"key": key, "name": key, "overall": score} for key, score in (("b", noisy), ("a", .85))])
        self.assertEqual([(r["key"], r["rank"]) for r in tied], [("a", 1), ("b", 1)])


class ConfigurationExamples(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "scoring.json"
        self.benchmarks = [{"key": "gaia", "_version": "v1", "_default": "v1", "datasets": [{"id": "gaia-validation"}], "score": {"kind": "pass"}}]
        self.defaults = configuration.defaults(self.benchmarks)

    def test_c1_c5_all_fifteen_axes_and_defaults(self):
        axes = self.defaults["axes"]
        self.assertEqual(len(axes), 15)
        # only GAIA is in this catalogue: the capabilities without a benchmark weigh nothing
        self.assertEqual([axes[k]["weight"] for k in configuration.CAPABILITIES], [0, 0, 0, 0, 0, .10, 0, .20])
        self.assertEqual(configuration.defaults([])["axes"]["Memory"]["weight"], .12)
        weighted = copy.deepcopy(self.defaults)
        weighted["axes"]["Memory"]["weight"] = .5                     # a row with no benchmark weighs nothing, whatever was saved
        self.assertEqual(configuration.validate(weighted, self.benchmarks)["axes"]["Memory"]["weight"], 0)
        self.assertEqual({axes[k]["weight"] for k in configuration.DOMAINS}, {.14})   # no domain has a benchmark here: equal weights stay
        self.assertEqual([(e["dataset"], e["version"], e["weight"]) for e in axes["Retrieval"]["entries"]], [("gaia-validation", "v1", 1)])
        self.assertEqual([(e["dataset"], e["weight"]) for e in axes["Execution"]["entries"]], [("gaia-validation", .5)])
        self.assertFalse(any(e["benchmark"] != "gaia" for a in axes.values() for e in a["entries"]))

    def test_c19_invalid_values_preserve_stored_bytes(self):
        configuration.save(self.path, self.defaults, self.benchmarks)
        before = self.path.read_bytes()
        for value in (-1, float("nan"), float("inf"), "abc", True, 10 ** 400):
            draft = copy.deepcopy(self.defaults)
            draft["axes"]["Retrieval"]["weight"] = value
            with self.assertRaises(ValueError):
                configuration.save(self.path, draft, self.benchmarks)
            self.assertEqual(self.path.read_bytes(), before)
        draft = copy.deepcopy(self.defaults)
        for axis in draft["axes"].values():
            axis["weight"] = 0
        with self.assertRaises(ValueError):
            configuration.save(self.path, draft, self.benchmarks)
        draft = copy.deepcopy(self.defaults)
        draft["axes"]["Retrieval"]["entries"] *= 2
        with self.assertRaises(ValueError):
            configuration.save(self.path, draft, self.benchmarks)
        # a family whose every row with a benchmark weighs 0 would have no Overall: a write refuses it,
        # a file that already holds it still reads
        draft = copy.deepcopy(self.defaults)
        for name in configuration.CAPABILITIES:
            draft["axes"][name]["weight"] = 0
        with self.assertRaises(ValueError):
            configuration.save(self.path, draft, self.benchmarks)
        self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text(json.dumps(draft))
        self.assertEqual(configuration.load(self.path, self.benchmarks), (draft, ""))

    def test_c19_broken_settings_warn_without_overwrite_and_removed_references_survive(self):
        huge = copy.deepcopy(self.defaults)
        huge["axes"]["Retrieval"]["weight"] = 7.25
        damaged = {"cut short": "{", "a number no float holds": json.dumps(huge).replace("7.25", "1" + "0" * 400),
                   "nested too deep": "[" * 100000 + "]" * 100000}
        for case, text in damaged.items():
            with self.subTest(case):
                self.path.write_text(text)
                doc, warning = configuration.load(self.path, self.benchmarks)
                self.assertEqual(doc, self.defaults)
                self.assertIn("defaults", warning)
                self.assertEqual(self.path.read_text(), text)
        configuration.save(self.path, self.defaults, self.benchmarks)
        configuration.save(self.path, self.defaults, [])
        self.assertEqual(json.loads(self.path.read_text()), self.defaults)

    def test_c19_atomic_write_failure_preserves_valid_document(self):
        configuration.save(self.path, self.defaults, self.benchmarks)
        before = self.path.read_bytes()
        with patch("nullius.ondisk.os.replace", side_effect=OSError("private path")):
            with self.assertRaises(OSError):
                configuration.save(self.path, self.defaults, self.benchmarks)
        self.assertEqual(self.path.read_bytes(), before)

    def test_c20_a_schema_1_file_loads_without_custom_benchmarks_and_with_domain_weights(self):
        doc = copy.deepcopy(self.defaults) | {"schema": 1}
        for name in configuration.DOMAINS:
            doc["axes"][name]["weight"] = 0                              # domains weighed nothing before schema 2
        doc["custom"] = [{"id": "calendar", "name": "Family calendar test", "scores": {"A": 72}}]
        doc["axes"]["Retrieval"]["entries"].append({"custom": "calendar", "weight": 1})
        self.path.write_text(json.dumps(doc))
        loaded, warning = configuration.load(self.path, self.benchmarks)
        self.assertEqual((loaded, warning), (self.defaults, ""))       # the GAIA row is kept, the custom one dropped
        with self.assertRaises(ValueError):                             # and a write never takes them back
            configuration.validate(doc, self.benchmarks)
        doc["schema"] = 2
        doc.pop("custom")
        doc["axes"]["Retrieval"]["entries"].pop()
        self.path.write_text(json.dumps(doc))
        loaded, _ = configuration.load(self.path, self.benchmarks)
        self.assertEqual({loaded["axes"][n]["weight"] for n in configuration.DOMAINS}, {0})   # a schema 2 zero is the user's


if __name__ == "__main__":
    unittest.main()
