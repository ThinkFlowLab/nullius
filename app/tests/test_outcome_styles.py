"""Every outcome the reader can produce has a colour in this stylesheet.

The failure it catches is quiet: an outcome with no stylesheet entry paints a
colourless dot, which reads as "no data" rather than as the outcome it is.
Adding a label without adding a colour is the easy half of the mistake, and the
light palette is a second block that is simple to forget.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from nullius.runs import classify as C

STYLESHEET = Path(__file__).resolve().parents[1] / "static" / "style.css"


class OutcomeStyles(unittest.TestCase):
    def test_every_outcome_is_styled_in_both_themes(self):
        css = STYLESHEET.read_text(encoding="utf-8")
        for outcome in C.ORDER:
            with self.subTest(outcome):
                self.assertIn(f".oc-{outcome}", css, "no dot/segment/tag rule")
                self.assertEqual(css.count(f"--{outcome}:"), 2,
                                 "expected one colour definition per theme")

    def test_the_outcome_list_comes_from_the_classifier(self):
        # If this ever reads an empty list the test above passes vacuously, so
        # the source of truth is asserted rather than assumed.
        self.assertTrue(C.ORDER)
        self.assertTrue(set(C.INFRA_OUTCOMES).issubset(set(C.ORDER)))


if __name__ == "__main__":
    unittest.main()
