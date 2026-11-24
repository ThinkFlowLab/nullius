"""LoCoMo's settings inventory: every setting of every evaluation entry point
of its upstream - the paper's scripts, evaluate_qa.py's flags, the readers'
constants, the retrievers - offered or recorded, and what is offered named in
it. The definition checks need nothing installed; the check against
evaluate_qa.py's own flags reads the upstream under ``STANDARD_DIR`` and is
skipped without it."""
from __future__ import annotations

import json
import os
import re
import unittest

from nullius.standard.contract import definitions
from nullius.standard.requirements import install

BENCH = definitions.get("benchmark", "locomo")


def rows():
    with open(os.path.join(BENCH["_here"], definitions.SETTINGS_FILE)) as f:
        return json.load(f)


class Inventory(unittest.TestCase):
    def test_what_is_offered_is_named(self):
        named = {r["nullius"] for r in rows()}
        for n in BENCH.get("options") or {}:
            self.assertIn(f"option:{n}", named)
        for d in BENCH["datasets"]:
            self.assertIn(f"dataset:{d['id']}", named)
        for s in BENCH["setups"]:
            self.assertIn(f"setup:{s}", named)


def _upstream():
    return "" if install.ready(install.upstream_dir(BENCH)) else "LoCoMo's upstream is not installed"


@unittest.skipIf(_upstream(), _upstream())
class Flags(unittest.TestCase):
    def test_every_flag_of_evaluate_qa_has_a_row(self):
        with open(os.path.join(install.upstream_dir(BENCH), "task_eval", "evaluate_qa.py")) as f:
            flags = set(re.findall(r"add_argument\('(--[a-z0-9-]+)'", f.read()))
        self.assertEqual(len(flags), 11)
        recorded = {r["setting"] for r in rows() if r["entry"] == "task_eval/evaluate_qa.py"}
        self.assertEqual(flags - recorded, set())


if __name__ == "__main__":
    unittest.main()
