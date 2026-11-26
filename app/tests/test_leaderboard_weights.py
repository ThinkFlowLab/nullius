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
        self.assertTrue(all(axes[k]["weight"] == 0 for k in configuration.DOMAINS))
        self.assertEqual([(e["dataset"], e["version"], e["weight"]) for e in axes["Retrieval"]["entries"]], [("gaia-validation", "v1", 1)])
        self.assertEqual([(e["dataset"], e["weight"]) for e in axes["Execution"]["entries"]], [("gaia-validation", .5)])
        self.assertFalse(any(e["benchmark"] != "gaia" for a in axes.values() for e in a["entries"]))

    def test_c19_invalid_values_preserve_stored_bytes(self):
        configuration.save(self.path, self.defaults, self.benchmarks)
        before = self.path.read_bytes()
        for value in (-1, float("nan"), float("inf"), "abc", True):
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

    def test_c19_broken_settings_warn_without_overwrite_and_removed_references_survive(self):
        self.path.write_text("{")
        doc, warning = configuration.load(self.path, self.benchmarks)
        self.assertEqual(doc, self.defaults)
        self.assertIn("defaults", warning)
        self.assertEqual(self.path.read_text(), "{")
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

    def test_c20_custom_scores_names_stability_and_removal(self):
        doc = copy.deepcopy(self.defaults)
        doc["custom"] = [{"id": "calendar", "name": " Family calendar test ", "scores": {"A": 72, "B": 0, "C": None}}]
        for axis in ("Personalization", "Home"):
            doc["axes"][axis]["entries"] = [{"custom": "calendar", "weight": 1}]
        clean = configuration.validate(doc, self.benchmarks)
        self.assertEqual(clean["custom"][0]["name"], "Family calendar test")
        for value in (120, -1, "abc", float("nan"), float("inf")):
            bad = copy.deepcopy(doc)
            bad["custom"][0]["scores"]["A"] = value
            with self.assertRaises(ValueError):
                configuration.validate(bad, self.benchmarks)
        for name in ("", "  "):
            bad = copy.deepcopy(doc)
            bad["custom"][0]["name"] = name
            with self.assertRaises(ValueError):
                configuration.validate(bad, self.benchmarks)
        bad = copy.deepcopy(doc)
        bad["custom"].append({"id": "second", "name": " family calendar TEST ", "scores": {}})
        with self.assertRaises(ValueError):
            configuration.validate(bad, self.benchmarks)
        clean["custom"][0]["name"] = "<b>test</b>"
        renamed = configuration.validate(clean, self.benchmarks)
        self.assertEqual(renamed["custom"][0]["scores"], {"A": 72, "B": 0, "C": None})
        removed = configuration.remove_custom(renamed, "calendar")
        self.assertFalse(removed["custom"])
        self.assertFalse(removed["axes"]["Home"]["entries"])


if __name__ == "__main__":
    unittest.main()
