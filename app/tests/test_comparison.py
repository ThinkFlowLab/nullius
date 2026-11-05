"""A task cohort changes every aggregate, never the underlying parsed run."""

from dataclasses import asdict
from datetime import datetime, timedelta
import unittest

from nullius.runs import parse
from nullius.compare import comparison


def trial(task, outcome="resolved", **overrides):
    fields = dict(
        job_id="run", dir="/unused", task_name=task, trial_name=task,
        task_base=task, source="swe-bench-verified", model_name="model",
        outcome=outcome, reward=1 if outcome == "resolved" else 0,
        exception_type="", exception_message="", patch_exists=None,
        patch_applied=None, f2p_pass=0, f2p_fail=0, p2p_pass=0, p2p_fail=0,
        n_input_tokens=100, n_output_tokens=10, n_cache_tokens=25,
        cost_usd=1.0, dur_total=60, dur_env=5, dur_agent_setup=5,
        dur_agent=40, dur_verifier=10,
    )
    return parse.Trial(**(fields | overrides))


def job(trials, dataset="swebench-verified@1.0", job_id="run", agent="agent:A"):
    start = datetime(2026, 9, 1)
    return parse.Job(job_id=job_id, dir="/unused", model_name="model", dataset=dataset,
                     n_tasks=len(trials), started_at=start,
                     finished_at=start + timedelta(minutes=10),
                     trials=trials, agent_name=agent)


def selected(*jobs):
    return tuple(comparison.SelectedRun(comparison.RunRef(f"ws-{i}", run.job_id), run, f"Workspace {i}")
                 for i, run in enumerate(jobs))


class QualifiedSelections(unittest.TestCase):
    def test_duplicate_run_ids_in_different_workspaces_are_distinct(self):
        first, second = job([trial("a")]), job([trial("b")])
        got = comparison.resolve(["second:run", "first:run", "second:run"],
                                 {"first": [first], "second": [second]})
        self.assertEqual([item.value for item in got], ["second:run", "first:run"])
        self.assertIs(got[0].job, second)
        self.assertIs(got[1].job, first)

    def test_never_falls_back_to_another_workspace(self):
        with self.assertRaises(ValueError):
            comparison.resolve(["missing:run"], {"": [job([])]})
        with self.assertRaises(ValueError):
            comparison.resolve(["first:missing"], {"first": [job([])]})

    def test_ambiguous_ids_inside_a_workspace_are_refused(self):
        with self.assertRaises(ValueError):
            comparison.resolve(["first:run"], {"first": [job([]), job([])]})

    def test_reference_round_trip_keeps_colons_in_run_ids(self):
        ref = comparison.RunRef.parse("first:2026-09-01T12:30:00")
        self.assertEqual(ref.workspace, "first")
        self.assertEqual(ref.value, "first:2026-09-01T12:30:00")
        with self.assertRaises(ValueError):
            comparison.RunRef.parse("../../first:run")


class CohortArithmetic(unittest.TestCase):
    def test_partial_coverage_changes_all_aggregates_together(self):
        left = job([trial("shared", cost_usd=2), trial("only-left", "agent_error", cost_usd=8)])
        right = job([trial("shared", "agent_error", cost_usd=3), trial("only-right", cost_usd=9)])
        originals = [asdict(left), asdict(right)]
        whole = comparison.build(selected(left, right))
        match = comparison.build(selected(left, right), "matched")
        self.assertEqual([len(run.trials) for run in whole.jobs], [2, 2])
        self.assertEqual([run.raw_solve_rate for run in match.jobs], [1, 0])
        self.assertEqual([run.total_cost_usd for run in match.jobs], [2, 3])
        self.assertEqual([run.total_input_tokens for run in match.jobs], [100, 100])
        self.assertEqual([run.total_output_tokens for run in match.jobs], [10, 10])
        self.assertEqual([run.total_cache_tokens for run in match.jobs], [25, 25])
        self.assertEqual(match.excluded_trials, (1, 1))
        self.assertEqual(match.shared_tasks, 1)
        self.assertEqual(match.n_differing, 1)
        self.assertEqual([run.duration_min for run in match.jobs], [None, None])
        self.assertEqual([asdict(left), asdict(right)], originals)

    def test_infra_is_excluded_but_unknown_stays_in_adjusted_denominator(self):
        attempts = [trial("a"), trial("b", "rate_limited"), trial("c", "unknown")]
        result = comparison.build(selected(job(attempts), job(attempts)), "matched")
        self.assertAlmostEqual(result.jobs[0].raw_solve_rate, 1 / 3)
        self.assertEqual(result.jobs[0].adjusted_solve_rate, .5)
        self.assertEqual(result.jobs[0].n_infra, 1)

    def test_repeated_attempts_count_in_metrics_and_rows_disclose_their_projection(self):
        first = trial("a", "agent_error", trial_name="a-1", cost_usd=2)
        last = trial("a", trial_name="a-2", cost_usd=3)
        result = comparison.build(selected(job([first, last]), job([trial("a")])), "matched")
        self.assertEqual(len(result.jobs[0].trials), 2)
        self.assertEqual(result.jobs[0].raw_solve_rate, .5)
        self.assertEqual(result.jobs[0].total_cost_usd, 5)
        self.assertEqual(result.shared_tasks, 1)
        self.assertEqual(result.repeated_trials, (1, 0))
        self.assertEqual(result.tasks[0].attempts, (2, 1))
        self.assertIs(result.tasks[0].trials[0], last)

    def test_dataset_and_version_differences_do_not_align_same_task_name(self):
        for right_dataset in ("other-dataset", "swebench-verified@2.0", ""):
            with self.subTest(dataset=right_dataset):
                runs = selected(job([trial("a")]), job([trial("a")], dataset=right_dataset))
                whole = comparison.build(runs)
                matched = comparison.build(runs, "matched")
                self.assertEqual(whole.shared_tasks, 0)
                self.assertEqual(len(whole.tasks), 2)
                self.assertEqual([len(run.trials) for run in matched.jobs], [0, 0])
                self.assertEqual(matched.excluded_trials, (1, 1))

    def test_unknown_dataset_names_are_not_proof_of_shared_identity(self):
        result = comparison.build(selected(job([trial("a")], dataset=""),
                                           job([trial("a")], dataset="")), "matched")
        self.assertEqual(result.shared_tasks, 0)
        self.assertFalse(result.datasets_known)

    def test_controls_keep_their_kind_and_real_zeros(self):
        control = job([trial("a", "no_patch", cost_usd=0, n_input_tokens=0,
                             n_output_tokens=0, n_cache_tokens=0)], agent="nop")
        result = comparison.build(selected(job([trial("a")]), control), "matched")
        self.assertTrue(result.jobs[1].is_control)
        self.assertEqual(result.jobs[1].kind, "control")
        self.assertEqual(result.jobs[1].total_cost_usd, 0)
        self.assertEqual(result.jobs[1].total_input_tokens, 0)

    def test_an_unpriced_run_has_no_cost_in_either_cohort(self):
        """Unpriced and free are different claims: a run whose agent reported
        no cost sums to None, whole or matched, while a priced one beside it
        keeps its figure — and an unpriced attempt among priced ones neither
        zeroes nor drops the sum."""
        unpriced = job([trial("a", cost_usd=None), trial("b", cost_usd=None)])
        priced = job([trial("a", cost_usd=2), trial("b", cost_usd=None)])
        for cohort in ("whole", "matched"):
            with self.subTest(cohort=cohort):
                result = comparison.build(selected(unpriced, priced), cohort)
                self.assertIsNone(result.jobs[0].total_cost_usd)
                self.assertEqual(result.jobs[1].total_cost_usd, 2)
                self.assertEqual([len(run.trials) for run in result.jobs], [2, 2])

    def test_empty_and_invalid_cohorts_do_not_invent_rates(self):
        result = comparison.build(selected(job([trial("a")]), job([trial("b")])), "matched")
        self.assertEqual(result.shared_tasks, 0)
        self.assertIsNone(result.jobs[0].raw_solve_rate)
        self.assertIsNone(result.jobs[0].adjusted_solve_rate)
        with self.assertRaises(ValueError):
            comparison.build([], "best-attempt")


class JudgedPair(unittest.TestCase):
    """The pick between two judged sides is made here, from two scores on
    one rubric, never by the model: a margin makes a tie explicit, and
    swapping the sides mirrors the answer."""

    def test_the_higher_score_wins_by_the_margin_or_it_is_a_tie(self):
        self.assertEqual(comparison.better_of(2.7, 1.2), "A")
        self.assertEqual(comparison.better_of(1.2, 2.7), "B")
        self.assertEqual(comparison.better_of(2.0, 2.4), "same")                     # within half a level
        self.assertEqual(comparison.better_of(2.0, 2.5), "B")                        # exactly the margin apart: a pick
        self.assertEqual(comparison.better_of(2.0, 2.0), "same")
        self.assertEqual(comparison.better_of(0.0, 3.0), "B")
        self.assertEqual(comparison.better_of(2.0, 2.4, margin=0.1), "B")            # the margin is the caller's to change
        self.assertEqual(comparison.TIE_MARGIN, 0.5)

    def test_nothing_is_picked_until_both_sides_are_scored(self):
        self.assertIsNone(comparison.better_of(None, 2.0))
        self.assertIsNone(comparison.better_of(2.0, None))
        self.assertIsNone(comparison.better_of(None, None))

    def test_swapping_the_sides_mirrors_the_verdict(self):
        mirror = {"A": "B", "B": "A", "same": "same"}
        for a, b in ((2.7, 1.2), (1.2, 2.7), (2.0, 2.4), (0.0, 0.0), (3.0, 2.4)):
            self.assertEqual(comparison.better_of(b, a), mirror[comparison.better_of(a, b)], (a, b))


class ScoredRuns(unittest.TestCase):
    """A benchmark that gives each task a score and no pass or fail - a
    conversation's mean token F1 - is compared on its scores."""

    def scored(self, task, score, parts):
        return trial(task, outcome="scored", reward=None, score=score, n_parts=parts)

    def test_two_readers_are_told_apart_by_their_scores(self):
        a = job([self.scored("conv-26", 0.6, 150), self.scored("conv-30", 0.4, 50)], dataset="locomo10", job_id="gpt-4-turbo")
        b = job([self.scored("conv-26", 0.6, 150), self.scored("conv-30", 0.7, 50)], dataset="locomo10", job_id="gpt-3.5")
        view = comparison.build(selected(a, b), "matched")
        self.assertEqual([(r.task, r.differs) for r in view.tasks], [("conv-30", True), ("conv-26", False)])   # a score apart, not an outcome
        self.assertEqual([round(j.mean_score, 3) for j in view.jobs], [0.55, 0.625])     # weighted by questions: (0.6*150 + 0.4*50) / 200


if __name__ == "__main__":
    unittest.main()
