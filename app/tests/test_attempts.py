"""A task's attempts in one job: grouped by task, numbered by start."""

from datetime import datetime, timedelta, timezone
import unittest

from nullius.runs import attempts
from tests.test_comparison import job, trial

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


# the exception each outcome below is recorded with, as Harbor writes it
EXCEPTION = {"agent_error": "AgentTimeoutError", "rate_limited": "ApiRateLimitError", "setup_error": "EnvironmentSetupError"}


def at(task, name, outcome="resolved", minutes=None, **overrides):
    return trial(task, outcome, trial_name=name, task_name=f"bench/{task}",
                 started_at=None if minutes is None else T0 + timedelta(minutes=minutes),
                 **({"exception_type": EXCEPTION[outcome]} if outcome in EXCEPTION else {}) | overrides)


def tries(task, *outcomes):
    return [at(task, f"{task}__{i}", o, minutes=i) for i, o in enumerate(outcomes)]


class Attempts(unittest.TestCase):
    def test_attempts_are_numbered_by_start_then_name_with_no_start_last(self):
        run = job([at("a", "a__z", minutes=5), at("a", "a__y"), at("a", "a__x", "agent_error", minutes=1),
                   at("a", "a__w")])
        got = attempts.of(run, run.trials[0])
        self.assertEqual([t.trial_name for t in got.trials], ["a__x", "a__z", "a__w", "a__y"])
        self.assertEqual(got.number(run.trials[0]), 2)
        self.assertEqual((got.n, got.n_passed), (4, 3))

    def test_spread_is_the_mean_low_and_high_over_attempts_that_recorded_it(self):
        group = attempts.Attempts((at("a", "a__1", n_input_tokens=100, n_output_tokens=None, dur_total=4.0, dur_agent=None),
                                   at("a", "a__2", n_input_tokens=None, n_output_tokens=None, dur_total=2.0, dur_agent=None),
                                   at("a", "a__3", n_input_tokens=300, n_output_tokens=None, dur_total=None, dur_agent=None)))
        self.assertEqual(group.spread("tokens"), {"mean": 200, "lo": 100, "hi": 300, "n": 2})
        self.assertEqual(group.spread("dur_total"), {"mean": 3.0, "lo": 2.0, "hi": 4.0, "n": 2})
        self.assertIsNone(group.spread("dur_agent"))
        self.assertEqual(set(group.means()), set(attempts.MEANS))

    def test_an_agents_zero_tokens_is_not_captured_and_a_controls_is_real(self):
        group = attempts.Attempts((at("a", "a__1", n_input_tokens=200, n_output_tokens=0),
                                   at("a", "a__2", n_input_tokens=0, n_output_tokens=0),
                                   at("a", "a__3", n_input_tokens=300, n_output_tokens=0)))
        self.assertEqual(group.spread("tokens")["mean"], 250)
        self.assertAlmostEqual(group.spread("tokens", "control")["mean"], 500 / 3)

    def test_an_infra_attempt_is_counted_apart_and_left_out_of_the_means(self):
        group = attempts.Attempts(tuple(tries("a", "resolved", "rate_limited", "failed_verification")))
        self.assertEqual((group.n, group.n_ran, group.n_infra, group.n_passed), (3, 2, 1, 1))
        self.assertEqual(group.rate, .5)
        self.assertEqual(group.spread("dur_total")["n"], 2)
        never = attempts.Attempts(tuple(tries("a", "rate_limited", "setup_error")))
        self.assertEqual((never.n_ran, never.n_infra, never.has_verdicts, never.rate), (0, 2, False, None))
        self.assertIsNone(never.spread("dur_total"))

    def test_an_unknown_outcome_has_no_verdict_but_stays_in_the_count(self):
        self.assertFalse(attempts.Attempts(tuple(tries("a", "unknown", "unknown"))).has_verdicts)
        some = attempts.Attempts(tuple(tries("a", "resolved", "unknown")))
        self.assertEqual((some.has_verdicts, some.n_ran, some.rate), (True, 2, .5))

    def test_a_runs_own_numbers_order_its_attempts_whatever_their_start(self):
        # attempt 2 was rate-limited and retried after attempt 3: it keeps its number
        run = job([at("a", "0001__u0000r1", minutes=0), at("a", "0001__u0000r3", "failed_verification", minutes=1),
                   at("a", "0001__u0000r2", minutes=2)])
        self.assertEqual([t.trial_name for t in attempts.of(run, run.trials[0]).trials],
                         ["0001__u0000r1", "0001__u0000r2", "0001__u0000r3"])
        # names without the run's number (Harbor's), or with one missing: by start
        mixed = job([at("a", "a__zz", minutes=2), at("a", "0001__u0000r1", minutes=1), at("a", "a__aa", minutes=0)])
        self.assertEqual([t.trial_name for t in attempts.of(mixed, mixed.trials[0]).trials], ["a__aa", "0001__u0000r1", "a__zz"])

    def test_a_task_attempted_once_has_no_attempts(self):
        run = job([at("a", "a__1"), at("a", "a__2", "patch_failed_tests"), at("b", "b__1")])
        self.assertIsNone(attempts.of(run, run.trials[2]))
        self.assertEqual(list(attempts.by_task(run)), ["bench/a"])
        self.assertEqual([(t.trial_name, g and g.n) for t, g in attempts.grouped(run)],
                         [("a__1", 2), ("a__2", 2), ("b__1", None)])

    def test_grouped_keeps_each_task_together_where_it_first_appears(self):
        run = job([at("b", "b__2", minutes=4), at("a", "a__1"), at("b", "b__1", minutes=2), at("c", "c__1")])
        self.assertEqual([t.trial_name for t, _ in attempts.grouped(run)], ["b__1", "b__2", "a__1", "c__1"])

    def test_the_same_short_name_in_two_datasets_is_two_tasks(self):
        one = trial("a", trial_name="x", task_name="d1/a")
        two = trial("a", trial_name="y", task_name="d2/a")
        self.assertEqual([g for _, g in attempts.grouped(job([one, two]))], [None, None])

    def test_a_scored_only_task_has_no_pass_count(self):
        run = job([at("a", "a__1", "scored", score=.2), at("a", "a__2", "scored", score=.6), at("a", "a__3", "scored", score=None)])
        group = attempts.of(run, run.trials[0])
        self.assertFalse(group.has_verdicts)
        self.assertEqual((group.n_scored, round(group.mean_score, 3), round(group.rate, 3)), (2, .4, .4))
        crashed = job([at("a", "a__1", "scored", score=1.0), at("a", "a__2", "agent_error")])
        group = attempts.of(crashed, crashed.trials[0])                                          # its score, never 0 passed of 2
        self.assertEqual((group.has_verdicts, group.rate, group.n_scored), (False, 1.0, 1))
        self.assertFalse(attempts.Attempts(tuple(tries("a", "agent_error", "agent_error")), scored_only=True).has_verdicts)
        verified = attempts.Attempts((at("a", "a__1", "resolved"), at("a", "a__2", "failed_verification"), at("a", "a__3", "scored", score=.2)))
        self.assertEqual((verified.has_verdicts, round(verified.rate, 3)), (True, .333))
        mixed = job([at("a", "a__1", "scored"), at("a", "a__2", "patch_failed_tests")])
        self.assertTrue(attempts.of(mixed, mixed.trials[0]).has_verdicts)

    def test_naive_and_aware_starts_order_together(self):
        naive = trial("a", trial_name="n", task_name="t", started_at=datetime(2026, 9, 1, 0, 2))
        aware = trial("a", trial_name="w", task_name="t", started_at=T0 + timedelta(minutes=1))
        self.assertEqual([t.trial_name for t in attempts.ordered([naive, aware])], ["w", "n"])



class PassAtK(unittest.TestCase):
    def test_a_timeout_is_a_failure_so_pass_hat_one_is_the_adjusted_rate(self):
        run = job(tries("a", "resolved", "resolved", "resolved") + tries("b", "resolved", "resolved", "agent_error")
                  + tries("c", "agent_error", "agent_error", "agent_error") + tries("d", "unknown", "resolved", "failed_verification"))
        got = attempts.for_job(run)
        self.assertEqual((got["units"], got["attempts"]), (4, 3))
        self.assertAlmostEqual(got["hat"][1], run.adjusted_solve_rate)
        self.assertAlmostEqual(got["hat"][1], (3 + 2 + 0 + 1) / 12)
        self.assertAlmostEqual(got["hat"][3], (1 + 0 + 0 + 0) / 4)
        self.assertAlmostEqual(got["at"][3], (1 + 1 + 0 + 1) / 4)

    def test_a_task_with_an_infra_attempt_is_left_out(self):
        run = job(tries("a", "resolved", "failed_verification") + tries("b", "resolved", "rate_limited"))
        got = attempts.for_job(run)
        self.assertEqual((got["units"], got["hat"][1]), (1, .5))

    def test_nothing_passed_or_failed_has_no_pass_at_k(self):
        self.assertIsNone(attempts.for_job(job(tries("a", "unknown", "unknown"))))
        self.assertIsNone(attempts.for_job(job([at("a", "a__1", "scored", score=.5), at("a", "a__2", "scored", score=.7)])))
        self.assertIsNone(attempts.for_job(job(tries("a", "rate_limited", "rate_limited"))))
        self.assertIsNone(attempts.for_job(job(tries("a", "resolved"))), "one attempt each")


if __name__ == "__main__":
    unittest.main()
