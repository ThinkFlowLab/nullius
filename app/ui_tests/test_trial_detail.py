"""Trial evidence renders as escaped, redacted fields and nested groups, without
execution metadata or duplicate scores. Fixtures never execute a benchmark."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from unittest.mock import patch

from nullius.runs import parse
from .test_routes import APP_DIR, web

NATIVE = APP_DIR / "tests" / "fixtures" / "native"


def text_of(html):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


class TrialDetail(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "parts-arm")
        shutil.copytree(NATIVE / "parts-arm", self.root)
        self.client = web.app.test_client()

    def page(self, detail, standard=None, score=None, source=None, reward=None):
        """The trial page's HTML with ``detail`` as t1's score's detail; ``standard``, its record's standard block."""
        with open(os.path.join(self.root, "t1", "verifier", "score.json"), "w") as f:
            json.dump({"reward": 0.0, "detail": detail}, f)
        if score is not None or source is not None or reward is not None:
            at = os.path.join(self.root, "t1", "result.json")
            with open(at) as f:
                res = json.load(f)
            if source is not None:
                res["source"] = source
            if score is not None or reward is not None:
                res["verifier_result"] = {"rewards": {"score": score} if reward is None else {"reward": reward}}
            if reward is not None:
                with open(os.path.join(self.root, "t1", "verifier", "reward.txt"), "w") as f:
                    f.write(str(reward))
            with open(at, "w") as f:
                json.dump(res, f)
        if standard is not None:
            at = os.path.join(self.root, "t1", "result.json")
            with open(at) as f:
                res = json.load(f)
            with open(at, "w") as f:
                json.dump(dict(res, standard=standard), f)
        job = parse.parse_job(self.root)
        with patch.object(web.shared, "_jobs_for_request", return_value=[job]):
            response = self.client.get(f"/trial/{job.job_id}/t1")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def panel(self, html):
        self.assertIn('id="tab-evidence"', html)
        return html.split('id="trial-evidence"', 1)[1].split('>', 1)[1].split('</section>', 1)[0]

    def test_plain_values_are_a_table_and_flags_a_list_under_the_drivers_names(self):
        html = self.panel(self.page({"variant": "the second", "result": 0.5, "by": "run.py, evaluate", "tool_calls": 21,
                                     "steps": 2, "score": 0.5, "rank": 42, "cached": False,
                                     "checks": {"saved": True, "closed": False}, "notes": {"cursor": "not taken"}}))
        said = text_of(html)
        self.assertIn("Variant the second Rank 42 Cached no", said)   # nothing the page says elsewhere
        self.assertNotIn("tool_calls", said); self.assertNotIn("run.py", said)
        self.assertIn("Result timed out Steps 15 Score 0.25",       # text, or a number the head does not show
                      text_of(self.panel(self.page({"result": "timed out", "steps": 15, "score": 0.25}))))
        self.assertEqual(text_of(self.panel(self.page({"steps": 15, "steps_limit": 60, "ended": "steps", "rank": 7}, standard={}))),
                         "Rank 7")                           # the head's steps of their limit and what ended the work
        self.assertIn("checks ✓ saved × closed notes · cursor: not taken", said)
        self.assertRegex(html, r'<span class="ok">✓</span> saved')
        self.assertRegex(html, r'<span class="bad">×</span> closed')
        lists = re.findall(r"<ul[^>]*>", html)
        self.assertEqual(len(lists), 2)
        for ul in lists:                                     # each flag's mark is its only one: no bullet before it
            self.assertIn("list-style:none", ul)

    def test_nested_scoring_evidence_excludes_configuration_and_duplicate_reward(self):
        detail = {"domain": "airline", "termination": "user_stop", "tool_errors": 0,
                  "evaluation": "all", "max_steps": 200, "max_errors": 10, "seed": 626729,
                  "trial": 0, "split": "base", "attempts": 1,
                  "reward_info": {"reward": 0.5, "db_check": {"db_match": True, "db_reward": 1.0},
                                  "reward_breakdown": {"DB": 1.0, "COMMUNICATE": 0.0}}}
        html = self.panel(self.page(detail))
        said = text_of(html)
        for name in ("evaluation", "max steps", "max errors", "seed", "trial", "split", "attempts"):
            self.assertNotIn(name, said)
        self.assertIn("Domain airline Termination user_stop Tool errors 0", said)
        self.assertIn("reward info / db check ✓ db match", said)
        self.assertIn("COMMUNICATE: 0.0", said)
        self.assertNotIn("0.5", said)
        self.assertNotIn("{", said)
        self.assertNotIn("&quot;", html)

    def test_a_long_value_is_whole_in_a_block_and_a_long_flag_is_behind_a_disclosure(self):
        long, text = "word " * 60, "x" * 100_000 + "<end>"
        html = self.panel(self.page({"why": long, "flags": {"explanation": text}}))
        self.assertIn(f'<pre class="instruction">{long}</pre>', html)
        self.assertIn(f'<details class="faint"><summary>{text[:parse.DETAIL_CELL]}…</summary>'
                      + "x" * 100_000 + "&lt;end&gt;</details>", html)
        self.assertNotIn("<details open", html)

    def test_values_and_names_are_text_never_markup_and_never_a_credential(self):
        hidden = "example12345678901234567890"
        html = self.page({"<b>name</b>": "<script>alert(1)</script>", "api_key": hidden, "credentials": {"password": hidden},
                          "flags": {"<i>flag</i>": True, "password": hidden, "held": True},
                          "rows": [{"inner": {"secret": hidden}}]})
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;b&gt;name&lt;/b&gt;", html)
        self.assertIn("&lt;i&gt;flag&lt;/i&gt;", html)
        self.assertNotIn(hidden, html)
        self.assertIn("[REDACTED]", self.panel(html))
        self.assertIn("✓</span> held", self.panel(html))

    def test_a_score_without_a_detail_has_no_tab(self):
        for detail in ({}, None, ["a"], {"result": 0.5, "by": "run.py, evaluate", "tool_calls": 3, "steps": 2}):
            with self.subTest(detail=detail):
                self.assertNotIn('id="tab-evidence"', self.page(detail))
        os.remove(os.path.join(self.root, "t1", "verifier", "score.json"))
        job = parse.parse_job(self.root)
        with patch.object(web.shared, "_jobs_for_request", return_value=[job]):
            response = self.client.get(f"/trial/{job.job_id}/t1")
        self.addCleanup(response.close)
        self.assertNotIn('id="tab-evidence"', response.get_data(as_text=True))

    def test_only_a_trial_that_fell_short_has_the_tab(self):
        detail = {"extracted": "A", "target": "C"}
        for kwargs, shown in (({"score": 0.4}, True), ({"score": 1.0}, False), ({"reward": 0.0}, True), ({"reward": 1.0}, False)):
            with self.subTest(**kwargs):
                html = self.page(detail, **kwargs)
                self.assertEqual('id="tab-evidence"' in html, shown)
                self.assertEqual('id="trial-evidence"' in html, shown)
                self.assertIn('id="tab-failure"', html)                          # the User judge stays

    def test_a_benchmarks_named_rows_and_nothing_else(self):
        html = self.panel(self.page({"scorer": "choice", "value": "I", "extracted": "A", "target": "<b>C</b>", "answer": "so\nA"},
                                    source="open-telco", reward=0.0))
        self.assertEqual(text_of(html), "Extracted answer A Expected answer <b>C</b> Reply so A")
        self.assertIn("&lt;b&gt;C", html)
        self.assertIn('<pre class="instruction">so\nA</pre>', html)
        self.assertNotIn('id="tab-evidence"', self.page({"last_action": "click"}, source="osworld1", score=0.0))

    def test_failing_tests_are_in_the_tab_once(self):
        with open(os.path.join(self.root, "t1", "verifier", "report.json"), "w") as f:
            json.dump({"t1": {"patch_exists": True, "patch_successfully_applied": True, "resolved": False, "tests_status": {
                "FAIL_TO_PASS": {"success": [], "failure": ["test_wanted"]},
                "PASS_TO_PASS": {"success": [], "failure": ["test_broken"]}}}}, f)
        html = self.page({}, source="swe-bench-verified", reward=0.0)
        for name in ("test_wanted", "test_broken"):
            self.assertIn(name, self.panel(html))
            self.assertEqual(html.count(name), 1)

    def test_the_agents_last_reply_stands_in_for_an_answer_not_recorded(self):
        at = os.path.join(self.root, "t1", "agent", "trajectory.json")
        with open(at) as f:
            doc = json.load(f)
        doc["steps"] = [{"step_id": 1, "source": "agent", "message": "the first"}, {"step_id": 2, "source": "agent", "message": "economics"}]
        with open(at, "w") as f:
            json.dump(doc, f)
        said = text_of(self.panel(self.page({"reference": "business", "judge_said": "no"}, source="longmemeval", reward=0.0)))
        self.assertEqual(said, "Answer economics Expected answer business Judge said no")


if __name__ == "__main__":
    unittest.main()
