"""Leaderboard contributions retain tasks and attempts, never best-run scores."""
from dataclasses import replace
from datetime import timedelta
import unittest

from tests.test_comparison import job, trial
from nullius.leaderboard import scoring


def run(name, outcomes, dataset="first", **changes):
    result = job([trial(str(i), outcome, trial_name=f"{name}-{i}")
                  for i, outcome in enumerate(outcomes)], dataset=dataset, job_id=name)
    result.identity = {"benchmark": "gaia", "benchmark_version": "v1"}
    return replace(result, **changes)


class Contributions(unittest.TestCase):
    def contribution(self, runs, datasets=None, **options):
        return scoring.contribution(runs, benchmark="gaia", version="v1",
                                    datasets=datasets or {"first": None}, **options)

    def test_c2_most_distinct_tasks_then_latest_never_best_score(self):
        large = run("large", ["resolved"] * 60 + ["agent_error"] * 40)
        small = run("small", ["resolved"] * 10)
        got = self.contribution([large, small])
        self.assertEqual((got.score, got.covered, got.run_ids), (.6, 100, ("large",)))
        newer = replace(large, job_id="newer", finished_at=large.finished_at + timedelta(seconds=1),
                        trials=[trial(str(i), "agent_error") for i in range(100)])
        self.assertEqual(self.contribution([large, newer]).score, 0)
        tied = replace(newer, job_id="aaa")
        self.assertEqual(self.contribution([newer, tied]).run_ids, ("aaa",))

    def test_c2_attempts_stay_together_without_repeat_pooling(self):
        repeated = run("repeated", [])
        repeated.trials = [trial(str(i), "resolved" if j == 0 else "agent_error", trial_name=f"{i}-{j}")
                           for i in range(10) for j in range(3)]
        got = self.contribution([repeated])
        self.assertEqual(got.covered, 10)
        self.assertAlmostEqual(got.score, 1 / 3)

    def test_c2_explicit_aliases_keep_the_canonical_tasks_attempts_together(self):
        repeated = run("repeated", ["resolved", "agent_error"])
        got = self.contribution([repeated], {"first": {"0": "same", "1": "same"}})
        self.assertEqual((got.score, got.covered, got.total), (.5, 1, 1))

    def test_c2_disjoint_overlap_and_dataset_slice(self):
        first = run("first-run", ["resolved", "agent_error"])
        second = run("second-run", ["resolved", "resolved", "agent_error"], dataset="second")
        manifests = {"first": {"0": "a", "1": "b"}, "second": {"0": "c", "1": "d", "2": "e"}}
        got = self.contribution([first, second], manifests)
        self.assertEqual((got.score, got.covered, got.total), (.6, 5, 5))
        manifests["second"]["0"] = "a"
        got = self.contribution([first, second], manifests)
        self.assertEqual((got.score, got.covered, got.total), (.5, 4, 4))
        self.assertEqual(self.contribution([first, second], manifests, dataset="first").score, .5)

    def test_c2_unknown_overlap_and_unavailable_slice_are_not_guessed(self):
        runs = [run("a", ["resolved"]), run("b", ["resolved"], dataset="second")]
        got = self.contribution(runs, {"first": None, "second": None})
        self.assertIsNone(got.score)
        self.assertIsNone(got.total)
        self.assertIn("identity", got.reason)
        self.assertIsNone(self.contribution(runs, dataset="missing").score)

    def test_c2_explicit_merge_sources_count_once(self):
        source = run("aaa-source", ["resolved"])
        merged = replace(source, job_id="zzz-merged", merged_from=["aaa-source"])
        self.assertEqual(self.contribution([source, merged]).run_ids, ("zzz-merged",))

    def test_c3_pass_fail_infrastructure_and_missing(self):
        got = self.contribution([run("a", ["resolved", "agent_error", "rate_limited"])])
        self.assertEqual((got.score, got.covered, got.excluded), (.5, 2, 1))
        self.assertIsNone(self.contribution([run("a", ["unknown", "rate_limited"])]).score)

    def test_c3_unknown_score_scale_is_not_used_as_a_pass_rate(self):
        scored = run("scored", [])
        scored.trials = [trial("task", "scored", score=2)]
        self.assertIsNone(self.contribution([scored]).score)

    def test_c3_graded_crashes_parts_and_unsupported_scale(self):
        graded = run("graded", ["agent_error"] * 9 + ["rate_limited"] * 2)
        graded.trials.append(trial("grade", "scored", score=1, n_parts=1))
        got = self.contribution([graded], graded=True)
        self.assertEqual((got.score, got.covered, got.excluded), (.1, 10, 2))
        self.assertEqual(got.note, "1 graded, 9 agent crashes; crashes count as 0")
        weighted = replace(graded, trials=[trial("same", "scored", score=1, n_parts=3),
                                           trial("same", "agent_error", n_parts=1)])
        self.assertEqual(self.contribution([weighted], graded=True).score, .75)
        self.assertEqual(self.contribution([run("crash", ["agent_error"])], graded=True).score, 0)
        for score in (0, None, 2, float("nan")):
            with self.subTest(score=score):
                value = replace(graded, trials=[trial("grade", "scored", score=score)])
                got = self.contribution([value], graded=True)
                self.assertEqual(got.score, 0 if score == 0 else None)

    def test_c4_pass_cubed_keeps_existing_eligible_unit_semantics(self):
        task = run("tau", [])
        task.trials = [trial("same", trial_name=str(i)) for i in range(3)]
        self.assertEqual(self.contribution([task], metric="pass^3").score, 1)
        task.trials[-1].outcome = "agent_error"
        self.assertEqual(self.contribution([task], metric="pass^3").score, 0)
        task.trials.pop()
        self.assertIsNone(self.contribution([task], metric="pass^3").score)
        self.assertIsNone(self.contribution([run(str(i), ["resolved"]) for i in range(3)], metric="pass^3").score)
        for metric in ("pass^2", "pass^4", "pass^5"):
            with self.assertRaises(ValueError):
                self.contribution([task], metric=metric)

    def test_c1_c4_only_completed_matching_version_contributes(self):
        good = run("good", ["resolved", "agent_error"])
        for bad in (replace(good, preparing=True), replace(good, n_running_trials=1),
                    replace(good, finished_at=None), replace(good, agent_name="oracle"),
                    replace(good, agent_name="nop"), replace(good, identity={}),
                    replace(good, identity={"benchmark": "gaia", "benchmark_version": "v2"})):
            with self.subTest(bad=bad):
                self.assertIsNone(self.contribution([bad]).score)
        self.assertEqual(self.contribution([good]).score, .5)


if __name__ == "__main__":
    unittest.main()
