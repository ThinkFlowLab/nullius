"""`loop.py compare`: pairs, the exact tests of D7, the targets, the stop rule and the closing of a round."""
from __future__ import annotations

import os
from typing import List, Tuple

from jobs import (NO_LIMITS, LoopCase, Trial, adapter, load, run, trial, write_job, write_json)


def paired(both: int, won: int, lost: int, neither: int) -> Tuple[List[Trial], List[Trial]]:
    """Trials of a run before and a new run, one per task, with the given counts of changed tasks."""
    before, new = [], []
    kinds = ["both"] * both + ["won"] * won + ["lost"] * lost + ["neither"] * neither
    for i, kind in enumerate(kinds):
        task = f"bench/task-{i:03d}"
        before.append(trial(f"task-{i:03d}__b", task, 1.0 if kind in ("both", "lost") else 0.0))
        new.append(trial(f"task-{i:03d}__n", task, 1.0 if kind in ("both", "won") else 0.0))
    return before, new


def shared_failure(name: str, task: str) -> Trial:
    t = trial(name, task, 0.0)
    t.files = {"verifier/ctrf.json": '{"results": {"tests": [{"name": "test_x", "status": "failed"}]}}'}
    return t


class Verdicts(LoopCase):
    def compare(self, both: int, won: int, lost: int, neither: int) -> dict:
        before, new = paired(both, won, lost, neither)
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        self.last_out = r.out
        return load(self.round_file("result.json"))

    def test_ten_won_and_thirteen_lost_is_noise(self) -> None:
        result = self.compare(271 // 10, 10, 13, 6)
        self.assertEqual(result["pairs"], {"common_tasks": 27 + 10 + 13 + 6, "both": 27, "won": 10, "lost": 13,
                                           "neither": 6})
        self.assertAlmostEqual(result["test"]["p"], 0.6776394844055176, places=9)
        self.assertEqual(result["verdict"], "noise")
        self.assertIn("noise", self.last_out)

    def test_twelve_won_and_one_lost_is_improved(self) -> None:
        result = self.compare(20, 12, 1, 3)
        self.assertAlmostEqual(result["test"]["p"], 0.00341796875, places=9)
        self.assertEqual(result["verdict"], "improved")

    def test_one_won_and_twelve_lost_is_regressed(self) -> None:
        self.assertEqual(self.compare(20, 1, 12, 3)["verdict"], "regressed")

    def test_no_changed_task_is_noise(self) -> None:
        result = self.compare(10, 0, 0, 2)
        self.assertEqual(result["test"]["p"], 1.0)
        self.assertEqual(result["verdict"], "noise")

    def test_pass_rates_carry_their_standard_error(self) -> None:
        result = self.compare(20, 12, 1, 3)
        new = result["pass_rate"]["new"]
        self.assertAlmostEqual(new["rate"], 32 / 36)
        self.assertAlmostEqual(new["standard_error"], ((32 / 36) * (4 / 36) / 36) ** 0.5)
        self.assertEqual((new["tasks"], new["trials"]), (36, 36))
        self.assertIn("standard error", self.last_out)

    def test_the_round_is_closed_and_recorded(self) -> None:
        self.compare(20, 1, 1, 3)
        ledger = load(os.path.join(self.loop, "ledger.json"))
        self.assertEqual(ledger["rounds"][0]["round"], "round-01")
        self.assertEqual(ledger["rounds"][0]["result"]["verdict"], "noise")
        self.assertTrue(os.path.isfile(self.round_file("report.html")))
        self.assertTrue(os.path.isfile(self.round_file("compare-metrics.json")))


class AgainstTheBaseline(LoopCase):
    def test_the_round_run_against_its_baseline(self) -> None:
        before, new = paired(5, 2, 1, 1)
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(new_dir, before_dir)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "baseline"])
        self.assertEqual(r.code, 0, r.out + r.err)
        result = load(self.round_file("result.json"))
        self.assertEqual((result["pairs"]["won"], result["pairs"]["lost"]), (2, 1))
        self.assertEqual(result["against"]["job_dir"], before_dir)
        self.assertEqual(result["stop_rule"]["spent_usd"], 0.0)

    def test_against_a_baseline_the_round_does_not_have(self) -> None:
        before, new = paired(5, 2, 1, 1)
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "baseline"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("baseline", r.err)

    def test_a_new_run_that_is_not_analysable(self) -> None:
        before, _ = paired(5, 0, 0, 1)
        broken = []
        for i in range(6):
            t = trial(f"task-{i:03d}__n", f"bench/task-{i:03d}", None)
            t.exception = ("BadRequestError", "Error code: 402 - insufficient credits")
            broken.append(t)
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", broken)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("insufficient credits", r.err)
        self.assertFalse(os.path.exists(self.round_file("result.json")))


class Targets(LoopCase):
    def run_targets(self, before_pass: List[bool], after_pass: List[bool]) -> dict:
        tasks = ["bench/tg-0", "bench/tg-1", "bench/tg-2"]
        before = [trial(f"tg-{i}__b", t, 1.0 if before_pass[i] else 0.0) for i, t in enumerate(tasks)]
        before += [trial(f"p{i}__b", f"bench/p{i}", 1.0) for i in range(5)]
        new = []
        for i, t in enumerate(tasks):
            for k in range(3):
                new.append(trial(f"tg-{i}-{k}__n", t, 1.0 if after_pass[3 * i + k] else 0.0))
        new += [trial(f"p{i}__n", f"bench/p{i}", 1.0) for i in range(5)]
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        ledger_path = os.path.join(self.loop, "ledger.json")
        ledger = load(ledger_path)
        ledger["rounds"][-1]["selection"] = {"targets": tasks, "guards": [], "canaries": [], "repeat": 3}
        write_json(ledger_path, ledger)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        return load(self.round_file("result.json"))["targets"]

    def test_targets_that_now_pass(self) -> None:
        got = self.run_targets([False] * 3, [True] * 9)
        self.assertEqual(got["tasks"], 3)
        self.assertEqual(got["before"], {"trials": 3, "passed": 0})
        self.assertEqual(got["after"], {"trials": 9, "passed": 9})
        self.assertAlmostEqual(got["p"], 0.004545454545454545, places=9)
        self.assertEqual(got["verdict"], "improved")
        self.assertIn("3 tasks", got["text"])
        self.assertIn("12 trials", got["text"])

    def test_targets_within_noise(self) -> None:
        got = self.run_targets([True, False, False], [True, True, False, True, False, True, False, True, False])
        self.assertEqual(got["after"], {"trials": 9, "passed": 5})
        self.assertAlmostEqual(got["p"], 1.0, places=9)
        self.assertEqual(got["verdict"], "noise")

    def test_without_a_selection(self) -> None:
        before, new = paired(5, 1, 1, 1)
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        got = load(self.round_file("result.json"))["targets"]
        self.assertIsNone(got["p"])
        self.assertIn("no targets", got["text"])


class StopRule(LoopCase):
    def compare_with_group(self, budget: str, with_group: bool) -> dict:
        before, new = paired(20, 1, 1, 1)
        if with_group:
            new = new[:-3] + [shared_failure(f"g{i}__n", f"bench/g{i}") for i in range(3)]
            before = before[:-3] + [trial(f"g{i}__b", f"bench/g{i}", 1.0) for i in range(3)]
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        r = run(["read", "--loop", self.loop, "--run", before_dir, "--budget-usd", budget, "--pass-threshold", "1"])
        self.assertEqual(r.code, 0, r.out + r.err)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        self.last_out = r.out
        return load(self.round_file("result.json"))["stop_rule"]

    def test_noise_and_no_group_of_three_stops(self) -> None:
        rule = self.compare_with_group("10", False)
        self.assertTrue(rule["stop"])
        self.assertEqual(rule["groups_of_3_or_more"], 0)
        self.assertTrue(any("noise" in r for r in rule["reasons"]))
        self.assertIn("stop rule: stop", self.last_out)

    def test_a_group_of_three_goes_on(self) -> None:
        rule = self.compare_with_group("10", True)
        self.assertEqual(rule["groups_of_3_or_more"], 1)
        self.assertAlmostEqual(rule["spent_usd"], 0.23)
        self.assertAlmostEqual(rule["left_usd"], 9.77)
        self.assertAlmostEqual(rule["cheapest_option"]["usd"], 0.09)
        self.assertFalse(rule["stop"], rule)

    def test_a_budget_under_the_cheapest_option_stops(self) -> None:
        rule = self.compare_with_group("0.3", True)
        self.assertAlmostEqual(rule["left_usd"], 0.07)
        self.assertTrue(rule["stop"])
        self.assertTrue(any("budget" in r for r in rule["reasons"]))

    def test_the_next_read_after_a_stop_needs_the_person(self) -> None:
        self.compare_with_group("10", False)
        run_dir = os.path.join(self.jobs, "new")
        r = run(["read", "--loop", self.loop, "--run", run_dir, "--budget-usd", "5", "--pass-threshold", "1"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("stop rule", r.err)
        self.assertFalse(os.path.exists(os.path.join(self.loop, "round-02")))
        words = "Go on: the second fix of the step cap is ready."
        r = run(["read", "--loop", self.loop, "--run", run_dir, "--budget-usd", "5", "--pass-threshold", "1",
                 "--continue-after-stop", words])
        self.assertEqual(r.code, 0, r.out + r.err)
        answers = load(os.path.join(self.loop, "ledger.json"))["rounds"][1]["answers"]
        self.assertEqual(answers[0]["about"], "continue after the stop rule of round-01")
        self.assertEqual(answers[0]["text"], words)
        self.assertRegex(answers[0]["date"], r"^\d{4}-\d{2}-\d{2}T")
