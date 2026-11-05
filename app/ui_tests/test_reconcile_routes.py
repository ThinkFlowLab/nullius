"""The classifier-vs-judge page: disagreements strongest first, the gated
and not-comparable rows listed apart, the unknown bucket's suggestion
marked experimental, the counts stated, the threshold a query parameter, a
control run told it does not apply, and the recorded outcome untouched.
The store is seeded directly; no judge is asked."""
from __future__ import annotations

import re
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from unittest.mock import patch

from nullius import workspace
from nullius.judge import store
from tests.test_feasibility import write_job
from tests.test_reconcile import record
from .test_routes import web


def text_of(html):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


class ReconcilePage(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.job = write_job(self.tmp.name, "r-agent", "my-agent", {
            "solved": 1, "failed": 0, "setup": "EnvironmentSetupError", "limited": "ApiRateLimitError", "odd": "SomethingNobodyKnowsError", "weak": 0})
        self.nop = write_job(self.tmp.name, "r-nop", "nop", {"a": 0})
        self.stack.enter_context(patch.object(web.shared, "_jobs_for_request", return_value=[self.job, self.nop]))
        # a real workspace, so the store has a home; its jobs are the synthetic ones patched in above
        self.ws = workspace.create(self.tmp.name + "/home", [self.tmp.name], label="reconcile")
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": self.ws}))
        self.client = web.app.test_client()

    def seed(self):
        d = self.ws.dir
        store.save(d, "r-agent", "failed__x", record("environment_error", 0.9, 0.85))
        store.save(d, "r-agent", "setup__x", record("environment_error", 0.7))
        store.save(d, "r-agent", "limited__x", record("agent_error", 0.95, 0.9))
        store.save(d, "r-agent", "odd__x", record("environment_error", 0.8))
        store.save(d, "r-agent", "weak__x", record("not_a_failure", 0.3, 0.4))

    def get(self, path, status=200):
        r = self.client.get(path); self.addCleanup(r.close)
        self.assertEqual(r.status_code, status, path)
        return r.get_data(as_text=True)

    def rows(self, html, table_id):
        m = re.search(r'<table class="grid" id="%s">.*?</table>' % table_id, html, re.S)
        return re.findall(r'<tr data-trial="([^"]+)" data-status="([^"]+)" data-outcome="([^"]+)" data-choice="([^"]*)"', m.group(0)) if m else None

    def test_nothing_judged_states_so_and_the_job_page_links_only_once_judged(self):
        html = self.get("/job/r-agent/reconcile")
        text = text_of(html)
        self.assertIn("Judged 0 · 5 not judged", text); self.assertIn("Disagree 0", text); self.assertIn("None above the threshold.", text)
        self.assertNotIn("classifier vs judge</a>", self.get("/job/r-agent"))
        self.seed()
        self.assertIn('href="/job/r-agent/reconcile"', self.get("/job/r-agent"))

    def test_disagreements_first_gated_and_suggested_apart_counts_stated_outcome_unchanged(self):
        self.seed()
        before = [(t.trial_name, t.outcome, t.reward) for t in self.job.trials]
        html = self.get("/job/r-agent/reconcile")
        self.assertEqual(self.rows(html, "reconcile-disagreements"),
                         [("limited__x", "disagree", "rate_limited", "agent_error"), ("failed__x", "disagree", "failed_verification", "environment_error")])
        self.assertEqual(self.rows(html, "reconcile-agreements"), [("setup__x", "agree", "setup_error", "environment_error")])
        self.assertEqual(self.rows(html, "reconcile-gated-rows"), [("weak__x", "gated", "failed_verification", "not_a_failure")])
        self.assertEqual(self.rows(html, "reconcile-suggested"), [("odd__x", "suggestion", "unknown", "environment_error")])
        self.assertEqual(self.rows(html, "reconcile-not-judged-rows"), [("solved__x", "not_judged", "resolved", "")])
        self.assertIsNone(self.rows(html, "reconcile-others"))
        self.assertIn('href="/trial/r-agent/limited__x"', html)
        text = text_of(html)
        for said in ("Trials 6", "Asked failure cause 5", "Judged 5", "Comparable 4", "Agree 1", "Disagree 2", "Suggested 1", "Below threshold 1", "Not comparable 0"):
            self.assertIn(said, text, said)
        # the suggestion is experimental and the recorded outcome is what it was
        suggested = re.search(r'<table class="grid" id="reconcile-suggested">.*?</table>', html, re.S).group(0)
        self.assertIn("Setup error", text_of(suggested)); self.assertIn("the recorded outcome stays Unknown", text_of(suggested)); self.assertIn("experimental", text_of(suggested))
        self.assertEqual([(t.trial_name, t.outcome, t.reward) for t in self.job.trials], before)
        self.assertIn('href="/trial/r-agent/odd__x"', self.get("/job/r-agent")); self.assertEqual(self.job.counts["unknown"], 1)   # the job page still counts it unknown
        # the join is stated on the page, non-correspondence included
        self.assertIn("no correspondence — not comparable", text); self.assertIn("agent_error · verifier_disagrees · task_ambiguous", text)
        self.assertIn("never a reclassification", text); self.assertIn("The recorded outcome never changes", text)

    def test_an_earlier_vocabulary_is_badged_against_the_current_version_not_a_literal(self):
        self.seed()
        badge = 'title="Judged under an earlier question vocabulary"'
        self.assertNotIn(badge, self.get("/job/r-agent/reconcile"))                       # asked under the current version
        from nullius.judge import questions
        with patch.object(questions, "VERSION", "judgement.questions.v99"), \
             patch.object(questions, "EARLIER", ("judgement.questions.v2",) + questions.EARLIER):   # a later bump keeps v2 readable
            html = self.get("/job/r-agent/reconcile")
        self.assertIn(badge, html)

    def test_the_threshold_is_a_query_parameter_with_the_default_named(self):
        self.seed()
        html = self.get("/job/r-agent/reconcile?threshold=0")
        self.assertEqual([r[0] for r in self.rows(html, "reconcile-disagreements")], ["limited__x", "failed__x", "weak__x"])
        self.assertIn('value="0.00"', html); self.assertIn("default 0.50", html)
        html = self.get("/job/r-agent/reconcile?threshold=0.92")
        self.assertEqual([r[0] for r in self.rows(html, "reconcile-disagreements")], ["limited__x"])
        self.assertEqual(len(self.rows(html, "reconcile-gated-rows")), 4)
        html = self.get("/job/r-agent/reconcile?threshold=nonsense")
        self.assertIn('value="0.50"', html); self.assertNotIn("default 0.50", html)

    def test_a_control_run_does_not_apply_and_an_unknown_job_is_404(self):
        html = self.get("/job/r-nop/reconcile")
        self.assertIn('id="reconcile-not-applicable"', html); self.assertNotIn('id="reconcile-counts"', html)
        self.assertIn("control run (nop)", text_of(html))
        self.get("/job/nowhere/reconcile", status=404)


if __name__ == "__main__":
    unittest.main()
