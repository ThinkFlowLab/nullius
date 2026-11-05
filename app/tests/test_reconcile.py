"""Reconciliation: the classifier's outcome against the judge's
failure_cause. The join is a stated table, not a rename; a label with no
correspondence is not comparable rather than agreeing; the gate is the
Choice's own confidence; disagreements rank strongest first; an unknown
outcome gets a suggestion and is never written. Every count is code and
nothing here asks a judge."""
from __future__ import annotations

import tempfile
import unittest

from nullius.judge import questions, reconcile as R
from nullius.runs import classify
from tests.test_feasibility import write_job


def record(choice, confidence=0.8, p=None, version=questions.VERSION):
    probs = {o: 0.0 for o in questions.QUESTIONS["failure_cause"]["criteria"]}
    probs[choice] = p if p is not None else (confidence if confidence is not None else 0.5)
    return {"questions": version, "judge": {"provider": "typesafe.ai", "model": "jev-latest"}, "asked_at": "2026-09-18T10:00:00Z",
            "state_chars": 1, "truncated": False, "usage": {"input_tokens": 1, "output_tokens": 1},
            "answers": {"failure_cause": {"choice": choice, "confidence": confidence, "probabilities": probs}}}


class TheJoin(unittest.TestCase):
    def test_every_label_is_in_the_table_and_every_option_named_exists(self):
        self.assertEqual(sorted(R.COMPATIBLE), sorted(classify.ORDER))
        options = set(questions.QUESTIONS["failure_cause"]["criteria"])
        for label, opts in R.COMPATIBLE.items():
            self.assertTrue(set(opts) <= options, label)
        self.assertEqual(R.COMPATIBLE[classify.UNKNOWN], ())                                             # no correspondence, said so
        self.assertEqual(len(R.COMPATIBLE[classify.PATCH_FAILED_TESTS]), 3)                            # several compatible, said so
        self.assertNotIn("other", {o for opts in R.COMPATIBLE.values() for o in opts})                 # the escape names no label

    def test_the_reverse_map_is_the_table_turned_round(self):
        self.assertEqual(R.SUGGESTS["environment_error"], (classify.SETUP_ERROR,))
        self.assertEqual(R.SUGGESTS["not_a_failure"], (classify.RESOLVED,))
        self.assertEqual(R.SUGGESTS["rate_limit_or_timeout"], (classify.AGENT_ERROR, classify.RATE_LIMITED, classify.API_ERROR))
        self.assertNotIn("other", R.SUGGESTS)
        for opt, labels in R.SUGGESTS.items():
            for label in labels:
                self.assertIn(opt, R.COMPATIBLE[label])


class Rows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.job = write_job(self.tmp.name, "r-agent", "my-agent", {
            "solved": 1, "failed": 0, "setup": "EnvironmentSetupError", "limited": "ApiRateLimitError",
            "timed": "AgentTimeoutError", "odd": "SomethingNobodyKnowsError", "odd2": "AnotherOddError", "unjudged": 0, "weak": 0})
        self.by_task = {t.task_base: t for t in self.job.trials}
        self.assertEqual({t: self.by_task[t].outcome for t in ("solved", "failed", "setup", "limited", "timed", "odd")},
                         {"solved": "resolved", "failed": "failed_verification", "setup": "setup_error", "limited": "rate_limited", "timed": "agent_error", "odd": "unknown"})

    def records(self):
        return {"failed__x": record("environment_error", 0.9, 0.85),            # disagree: the verifier said no, the judge blames the environment
                "setup__x": record("environment_error", 0.7),                    # agree
                "limited__x": record("agent_error", 0.95, 0.9),                  # disagree, stronger than failed
                "timed__x": record("rate_limit_or_timeout", 0.6),                # agree: the vocabulary folds timeouts in
                "odd__x": record("environment_error", 0.8),                      # unknown: a suggestion
                "odd2__x": record("other", 0.9),                                 # unknown, the escape: not comparable
                "weak__x": record("not_a_failure", 0.3, 0.4)}                    # a disagreement under the threshold: gated

    def test_statuses_ranking_and_counts(self):
        out = R.reconcile(self.job, self.records())
        rows = out["rows"]
        self.assertEqual([r["task"] for r in rows["disagree"]], ["limited", "failed"])                  # strongest first
        self.assertEqual([r["task"] for r in rows["agree"]], ["setup", "timed"])
        self.assertEqual([r["task"] for r in rows["suggestion"]], ["odd"]); self.assertEqual(rows["suggestion"][0]["suggests"], ["setup_error"])
        self.assertEqual([r["task"] for r in rows["not_comparable"]], ["odd2"]); self.assertEqual(rows["not_comparable"][0]["suggests"], [])
        self.assertEqual([r["task"] for r in rows["gated"]], ["weak"])
        self.assertEqual(sorted(r["task"] for r in rows["not_judged"]), ["solved", "unjudged"])         # resolved is never asked; unjudged has no record
        self.assertEqual(rows["other"], [])
        self.assertEqual(out["counts"], {"trials": 9, "asked": 8, "judged": 7, "unjudged": 1, "comparable": 5,
                                         "agree": 2, "disagree": 2, "other": 0, "suggested": 1, "gated": 1, "not_comparable": 1})
        self.assertTrue(out["applies"]); self.assertEqual(out["threshold"], R.THRESHOLD)
        row = rows["disagree"][0]
        self.assertEqual((row["outcome"], row["outcome_label"], row["choice"], row["confidence"], row["p"], row["compatible"]),
                         ("rate_limited", "Rate limited", "agent_error", 0.95, 0.9, ["rate_limit_or_timeout"]))
        self.assertEqual(row["version"], questions.VERSION)

    def test_the_threshold_gates_by_the_choices_own_confidence_and_is_a_parameter(self):
        out = R.reconcile(self.job, self.records(), threshold=0.0)
        self.assertEqual([r["task"] for r in out["rows"]["disagree"]], ["limited", "failed", "weak"]); self.assertEqual(out["rows"]["gated"], [])
        out = R.reconcile(self.job, self.records(), threshold=0.92)
        self.assertEqual([r["task"] for r in out["rows"]["disagree"]], ["limited"])
        self.assertEqual(sorted(r["task"] for r in out["rows"]["gated"]), ["failed", "odd", "setup", "timed", "weak"])   # agreements and suggestions gate too
        self.assertEqual(out["counts"]["gated"], 5); self.assertEqual(out["counts"]["comparable"], 5)                    # gated rows are still counted, never dropped
        # a missing confidence is under every threshold
        recs = {"failed__x": record("environment_error", None)}
        self.assertEqual(R.reconcile(self.job, recs, threshold=0.0)["rows"]["gated"][0]["task"], "failed")

    def test_other_is_listed_apart_and_a_v1_record_still_reconciles(self):
        recs = {"failed__x": record("other", 0.9), "setup__x": record("agent_error", 0.8, version="judgement.questions.v1")}
        out = R.reconcile(self.job, recs)
        self.assertEqual([r["task"] for r in out["rows"]["other"]], ["failed"]); self.assertEqual(out["rows"]["disagree"][0]["task"], "setup")
        self.assertEqual(out["rows"]["disagree"][0]["version"], "judgement.questions.v1"); self.assertEqual(out["counts"]["other"], 1)

    def test_the_stored_name_is_mapped_through_key_and_nothing_is_written(self):
        recs = {"KEY:failed__x": record("environment_error", 0.9)}
        before = [(t.trial_name, t.outcome, t.reward) for t in self.job.trials]
        out = R.reconcile(self.job, recs, key=lambda n: "KEY:" + n)
        self.assertEqual(out["rows"]["disagree"][0]["task"], "failed")
        self.assertEqual([(t.trial_name, t.outcome, t.reward) for t in self.job.trials], before)         # the trials are read, never written
        self.assertEqual(self.by_task["odd"].outcome, "unknown")
        self.assertIsNone(R.answer_of({"answers": {"failure_cause": {"noul": 0.9}}}))                     # not a Choice: no pick
        self.assertIsNone(R.answer_of(None))

    def test_a_dedicated_likely_outcome_suggests_directly_and_gates_on_its_own_confidence(self):
        """An unknown trial asked `likely_outcome` suggests that pick itself (one label, its own confidence), not the reverse map of failure_cause."""
        odd = self.by_task["odd"]
        rec = {"questions": "judgement.questions.v2", "answers": {"failure_cause": {"choice": "environment_error", "confidence": 0.9, "probabilities": {"environment_error": 0.9}},
                                                                    "likely_outcome": {"choice": "setup_error", "confidence": 0.7, "probabilities": {"setup_error": 0.66, "api_error": 0.2}}}}
        row = R.row_of(odd, rec)
        self.assertEqual((row["status"], row["suggests"], row["likely"], row["likely_p"]), (R.SUGGESTION, ["setup_error"], "setup_error", 0.66))
        low = R.row_of(odd, {"questions": "judgement.questions.v2", "answers": dict(rec["answers"], likely_outcome={"choice": "setup_error", "confidence": 0.2, "probabilities": {}})})
        self.assertEqual(low["status"], R.GATED)                                                            # gated by likely_outcome's own confidence, not failure_cause's 0.9
        strange = R.row_of(odd, {"questions": "judgement.questions.v2", "answers": dict(rec["answers"], likely_outcome={"choice": "other", "confidence": 0.9, "probabilities": {}})})
        self.assertEqual(strange["suggests"], ["setup_error"])                                              # `other` names no label: the reverse map of failure_cause stands in
        self.assertEqual(odd.outcome, "unknown")                                                              # read, never written

    def test_a_control_job_does_not_apply(self):
        nop = write_job(self.tmp.name, "r-nop", "nop", {"a": 0, "b": 1})
        out = R.reconcile(nop, {"a__x": record("agent_error", 0.9)})
        self.assertFalse(out["applies"]); self.assertTrue(R.applies(self.job))


if __name__ == "__main__":
    unittest.main()
