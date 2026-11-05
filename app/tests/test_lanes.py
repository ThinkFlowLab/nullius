"""The run page's record -> judge lanes: reconcile's rows grouped by
outcome and by (status, cause). Every status is reconcile's own; the lanes
only group, so a line's count is exactly the trials it names."""
from __future__ import annotations

import tempfile
import unittest

from nullius.judge import lanes as L, reconcile as R
from tests.test_feasibility import write_job
from tests.test_reconcile import record


class Lanes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.job = write_job(self.tmp.name, "l-agent", "my-agent", {
            "solved": 1, "failed": 0, "failed2": 0, "setup": "EnvironmentSetupError", "limited": "ApiRateLimitError",
            "odd": "SomethingNobodyKnowsError", "odd2": "AnotherOddError", "unjudged": 0, "weak": 0})

    def records(self):
        return {"failed__x": record("environment_error", 0.9),     # disagree
                "failed2__x": record("environment_error", 0.8),    # the same line: one per (status, cause)
                "weak__x": record("not_a_failure", 0.3),           # below the gate: gutter
                "setup__x": record("environment_error", 0.7),      # agree
                "limited__x": record("other", 0.9),                # cause outside the list: the disagrees lane
                "odd__x": record("environment_error", 0.8),        # unknown: suggests setup_error
                "odd2__x": record("other", 0.9)}                   # unknown, the escape: not comparable

    def by_outcome(self, out):
        return {r["outcome"]: r for r in out["rows"]}

    def test_lines_group_by_status_and_cause_in_the_classifiers_order(self):
        out = L.board(self.job, self.records())
        rows = self.by_outcome(out)
        self.assertEqual([r["outcome"] for r in out["rows"]], ["resolved", "failed_verification", "rate_limited", "setup_error", "unknown"])
        fv = rows["failed_verification"]
        self.assertEqual(fv["n"], 4)
        self.assertEqual([(g["lane"], g["status"], g["cause"], g["n"]) for g in fv["lines"]],
                         [("disagree", "disagree", "environment_error", 2), ("gutter", "gated", "not_a_failure", 1), ("gutter", "not_judged", None, 1)])
        self.assertEqual(sorted(fv["lines"][0]["trials"]), sorted(t.trial_name for t in self.job.trials if t.task_base in ("failed", "failed2")))
        self.assertEqual([(g["lane"], g["status"]) for g in rows["setup_error"]["lines"]], [("agree", "agree")])
        self.assertEqual([(g["lane"], g["status"], g["status_label"]) for g in rows["rate_limited"]["lines"]],
                         [("disagree", "other", "Cause outside the list")])

    def test_what_is_not_a_finding_stays_in_the_gutter(self):
        rows = self.by_outcome(L.board(self.job, self.records()))
        self.assertEqual([(g["lane"], g["status"], g["word"]) for g in rows["resolved"]["lines"]], [("gutter", "not_asked", "not asked")])
        self.assertEqual([(g["status"], g["word"], g["cause"]) for g in rows["unknown"]["lines"]],
                         [("suggestion", "suggests Setup error", None), ("not_comparable", "not comparable", None)])
        self.assertTrue(all(g["lane"] == "gutter" for g in rows["unknown"]["lines"]))

    def test_the_tally_is_reconciles_counts_and_the_trial_status_map_matches_it(self):
        out = L.board(self.job, self.records())
        counts = R.reconcile(self.job, self.records())["counts"]
        tally = {s: n for g in out["tally"] for s, _, n in g["items"]}
        self.assertEqual(tally, {"disagree": counts["disagree"], "other": counts["other"], "agree": counts["agree"],
                                 "gated": counts["gated"], "suggestion": counts["suggested"], "not_comparable": counts["not_comparable"],
                                 "not_judged": counts["unjudged"]})
        self.assertEqual(out["of"], counts["comparable"])
        self.assertEqual([g["shape"] for g in out["tally"]], ["jog", "straight", "dotted", "open", "ring"])
        self.assertEqual(out["status"][next(t.trial_name for t in self.job.trials if t.task_base == "solved")], "not_asked")
        self.assertTrue(out["has_agree"] and out["has_disagree"])

    def test_nothing_judged_keeps_disagree_and_agree_at_zero_and_draws_no_lane(self):
        out = L.board(self.job, {})
        self.assertEqual(out["tally"][0]["items"], [("disagree", "Disagree", 0)])
        self.assertEqual([i[0] for g in out["tally"] for i in g["items"]], ["disagree", "agree", "not_judged"])
        self.assertFalse(out["has_agree"] or out["has_disagree"])
        self.assertEqual(out["of"], 0)

    def test_a_threshold_moves_a_reading_out_of_the_lane(self):
        rows = self.by_outcome(L.board(self.job, self.records(), threshold=0.95))
        self.assertEqual({g["status"] for g in rows["failed_verification"]["lines"]}, {"gated", "not_judged"})

    def test_a_control_has_no_lanes(self):
        job = write_job(self.tmp.name, "nop", "nop", {"a": 0})
        self.assertIsNone(L.board(job, {}))


if __name__ == "__main__":
    unittest.main()
