"""The rows a benchmark names for a trial that fell short (``nullius.runs.evidence``)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from nullius.catalogue import catalog
from nullius.runs import evidence


class Evidence(unittest.TestCase):
    def rows(self, detail, *spec, reply=""):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "verifier"))
            with open(os.path.join(d, "verifier", "score.json"), "w") as f:
                json.dump({"detail": detail}, f)
            return [(r["label"], r["text"]) for r in evidence.rows(d, list(spec), reply)]

    def test_paths_reach_nested_values_and_keys_that_hold_dots(self):
        detail = {"a": {"b.c": 1}, "solution.py": "x", "gone": None}
        got = self.rows(detail, *({"label": p, "path": p} for p in ("a.b.c", "solution.py", "gone", "missing", "a.z")))
        self.assertEqual(got, [("a.b.c", "1"), ("solution.py", "x"), ("gone", "none")])

    def test_an_object_is_a_row_per_plain_value_under_its_names(self):
        got = self.rows({"basis": {"DB": 1.0, "OTHER": 0.0, "deep": {}}}, {
            "label": "Checks", "path": "basis", "names": {"DB": "Database"}, "values": {"1.0": "Pass", "0.0": "Fail"}})
        self.assertEqual(got, [("Database", "Pass"), ("OTHER", "Fail")])

    def test_values_that_say_nothing_have_no_row(self):
        spec = {"label": "Timed out", "path": "t", "unless": ["no"]}
        self.assertEqual(self.rows({"t": False}, spec), [])
        self.assertEqual(self.rows({"t": True}, spec), [("Timed out", "yes")])

    def test_the_reply_stands_in_only_when_nothing_is_recorded_and_is_redacted(self):
        spec = {"label": "Answer", "path": "answer", "reply": True}
        self.assertEqual(self.rows({"answer": "said"}, spec, reply="other"), [("Answer", "said")])
        self.assertEqual(self.rows({}, spec), [])
        key = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 2
        self.assertNotIn(key, str(self.rows({}, spec, reply=key)))
        self.assertNotIn("hunter2hunter2", str(self.rows({"password": "hunter2hunter2"}, {"label": "P", "path": "password"})))

    def test_the_catalogue_refuses_a_malformed_row(self):
        for rows in ({}, [{"label": "A"}], [{"label": "A", "path": "a", "values": {"x": 1}}], [{"label": "A", "path": "a", "odd": 1}]):
            with self.subTest(rows=rows):
                bench = catalog.Benchmark(key="bench", title="B", category="C", evidence=rows)
                self.assertTrue(any("evidence rows" in m for m in catalog.validate(catalog.Catalog([bench], []))))


if __name__ == "__main__":
    unittest.main()
