"""The documentation's metric fields and outcome vocabulary follow the backend."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from nullius.runs import classify

APP = Path(__file__).resolve().parent.parent
GUIDE_PATH = APP.parent / "docs" / "field-guide.md"
GUIDE = GUIDE_PATH.read_text(encoding="utf-8")
NUMBERS = {"eleven": 11, "twelve": 12, "ten": 10}


def sections():
    return re.findall(r"^## (.*?)\n(.*?)(?=^## |\Z)", GUIDE, re.M | re.S)


def rows(body):
    return [[cell.strip() for cell in line.strip('|').split('|')]
            for line in body.splitlines() if line.startswith('| `')]


class FieldGuide(unittest.TestCase):
    def test_every_field_it_names_is_still_in_the_backend(self):
        source = " ".join(p.read_text(encoding="utf-8", errors="ignore") for p in (APP / "nullius").rglob("*.py"))
        words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source))
        tables = [(heading, rows(body)) for heading, body in sections() if '| Field |' in body]
        self.assertTrue(tables, "the guide must document backend metric fields")
        missing = [(heading, name) for heading, fields in tables for cells in fields
                   for name in re.findall(r"\b[a-z_][a-z0-9_]*\b", cells[0]) if name not in words]
        self.assertEqual(missing, [], "update metric names in docs/field-guide.md when the backend changes")

    def test_its_outcome_vocabulary_is_the_classifiers(self):
        vocab = next(body for heading, body in sections() if heading.startswith('Outcome vocabulary'))
        outcomes = rows(vocab)
        shown = [cells[0].strip('`') for cells in outcomes]
        self.assertEqual(sorted(shown), sorted(classify.ORDER))
        self.assertEqual(NUMBERS[re.search(r"(\w+) classes", vocab)[1].lower()], len(classify.ORDER))
        self.assertIn(f"One of {[w for w, n in NUMBERS.items() if n == len(classify.ORDER)][0]} classes", GUIDE)
        infra = [cells[0].strip('`') for cells in outcomes if cells[2] == 'yes']
        self.assertEqual(sorted(infra), sorted(classify.INFRA_OUTCOMES))


if __name__ == "__main__":
    unittest.main()
