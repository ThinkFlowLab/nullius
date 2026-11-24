"""The step bar's underline (underlineAt in command-builder.js) under Node, with
stand-in steps: under the active step on every row when the steps wrap."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

BUILDER = Path(__file__).resolve().parents[1] / "static" / "builder" / "command-builder.js"


class StepUnderline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node is required for the builder's script")

    def at(self, on, last):
        step = lambda s: "{offsetLeft:%s,offsetWidth:%s,getBoundingClientRect:()=>({bottom:%s})}" % (s["left"], s["width"], s["bottom"])
        program = ("const {underlineAt}=require(" + json.dumps(str(BUILDER)) + ");"
                   "process.stdout.write(JSON.stringify(underlineAt(" + step(on) + "," + step(last) + ")));")
        return json.loads(subprocess.run([self.node, "-"], input=program, capture_output=True, text=True, check=True).stdout)

    def test_one_row_keeps_the_bar_at_its_bottom_edge(self):
        last = {"left": 1122, "width": 119, "bottom": 215.0}
        self.assertEqual(self.at({"left": 631, "width": 119, "bottom": 215.0}, last), {"left": 631, "bottom": -1, "width": 119})

    def test_a_step_on_an_upper_row_raises_the_bar_to_that_row(self):
        last = {"left": 137, "width": 117, "bottom": 427.5}   # 390 px: three steps a row, 08 Review on the third
        for bottom, raised in ((313.9, 112.6), (370.7, 55.8), (427.5, -1)):
            with self.subTest(bottom=bottom):
                out = self.at({"left": 16, "width": 117, "bottom": bottom}, last)
                self.assertEqual((out["left"], out["width"]), (16, 117))
                self.assertAlmostEqual(out["bottom"], raised)

    def test_the_bar_follows_the_active_step_and_the_last_step(self):
        page = BUILDER.read_text(encoding="utf-8")
        self.assertIn("const u=underlineAt(on,[...nav.querySelectorAll('.nb-step')].pop());bar.style.left=u.left+'px';bar.style.bottom=u.bottom+'px';bar.style.width=u.width+'px';", page)


if __name__ == "__main__":
    unittest.main()
