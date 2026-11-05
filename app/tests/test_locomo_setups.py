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

from nullius.standard import definitions, install

BENCH = definitions.get("benchmark", "locomo")


def rows():
    with open(os.path.join(BENCH["_here"], definitions.SETTINGS_FILE)) as f:
        return json.load(f)


class Inventory(unittest.TestCase):
    def test_the_benchmark_and_its_inventory_are_valid(self):
        with open(os.path.join(BENCH["_here"], "standard.json")) as f:
            self.assertEqual(definitions.problems(json.load(f), "benchmark"), [])
        self.assertEqual(definitions.settings_problems([BENCH]), [])

    def test_every_entry_point_of_the_paper_is_inventoried(self):
        entries = {r["entry"] for r in rows()}
        for script in ("evaluate_gpts", "evaluate_claude", "evaluate_gemini", "evaluate_hf_llm", "evaluate_rag_gpts",
                       "generate_observations", "generate_session_summaries", "generate_conversations"):
            self.assertIn(f"scripts/{script}.sh", entries)
        for module in ("task_eval/evaluate_qa.py", "task_eval/gpt_utils.py", "task_eval/rag_utils.py",
                       "task_eval/evaluation.py", "task_eval/hf_llm_utils.py"):
            self.assertIn(module, entries)

    def test_what_is_offered_is_named(self):
        named = {r["nullius"] for r in rows()}
        for n in BENCH.get("options") or {}:
            self.assertIn(f"option:{n}", named)
        for d in BENCH["datasets"]:
            self.assertIn(f"dataset:{d['id']}", named)
        for s in BENCH["setups"]:
            self.assertIn(f"setup:{s}", named)

    def test_every_retriever_and_reader_of_the_paper_is_recorded_with_a_reason(self):
        by = {(r["entry"], r["setting"]): r["nullius"] for r in rows()}
        for retriever in ("dragon", "dpr"):                                # the two that run as published, on a GPU
            self.assertEqual(by[("task_eval/rag_utils.py", f"retriever {retriever}")], "option:retriever")
            self.assertIn(retriever, BENCH["options"]["retriever"]["values"])
        for retriever in ("contriever", "openai"):                         # broken as published
            self.assertTrue(by[("task_eval/rag_utils.py", f"retriever {retriever}")].startswith("not offered - "))
            self.assertNotIn(retriever, BENCH["options"]["retriever"]["values"])
        readers = [r for r in rows() if r["entry"] in ("scripts/evaluate_claude.sh", "scripts/evaluate_gemini.sh",
                                                       "scripts/evaluate_hf_llm.sh")]
        self.assertEqual(len(readers), 4)
        self.assertTrue(all(r["nullius"].startswith("not offered - ") for r in readers))

    def test_the_originals_models_are_the_scripts(self):
        offered = " ".join(r["setting"] + " " + r["values"] for r in rows() if r["nullius"] == "setup:original"
                           and r["entry"] == "scripts/evaluate_gpts.sh")
        for m in BENCH["setups"]["original"]["models"]:
            self.assertIn(m, offered)


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
