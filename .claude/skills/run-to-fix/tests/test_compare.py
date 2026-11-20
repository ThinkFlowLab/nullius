"""`loop.py compare`: pairs, the exact tests of D7, the targets, the stop rule and the closing of a round."""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from jobs import (NO_LIMITS, LoopCase, Trial, adapter, load, run, trial, write, write_job, write_json)


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

    def test_eight_won_and_twelve_lost_is_noise(self) -> None:
        result = self.compare(30, 8, 12, 5)
        self.assertEqual(result["pairs"], {"common_tasks": 30 + 8 + 12 + 5, "both": 30, "won": 8, "lost": 12,
                                           "neither": 5})
        self.assertAlmostEqual(result["test"]["p"], 0.5034446716308594, places=9)
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


class PageLabels(LoopCase):
    """The page of a round names the two runs by their job folders; result.json keeps its keys."""

    def compare_jobs(self, before_root: str, before_name: str, new_root: str, new_name: str) -> Tuple[dict, str]:
        before, new = paired(5, 2, 1, 1)
        os.makedirs(before_root, exist_ok=True)
        os.makedirs(new_root, exist_ok=True)
        before_dir = write_job(before_root, before_name, "alpha", "harbor", before)
        new_dir = write_job(new_root, new_name, "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        with open(self.round_file("report.html"), encoding="utf-8") as fh:
            return load(self.round_file("compare-metrics.json")), fh.read()

    def test_the_labels_are_the_job_names(self) -> None:
        metrics, page = self.compare_jobs(self.jobs, "agent-old", self.jobs, "agent-new")
        self.assertEqual([r["label"] for r in metrics["runs"]], ["agent-old", "agent-new"])
        self.assertIn("</span>agent-old</th>", page)
        self.assertIn("</span>agent-new</th>", page)
        self.assertNotIn("</span>before</th>", page)
        result = load(self.round_file("result.json"))
        self.assertEqual(set(result["pass_rate"]), {"before", "new"})
        self.assertEqual((result["pairs"]["won"], result["pairs"]["lost"]), (2, 1))

    def test_two_jobs_of_one_name_keep_before_and_new(self) -> None:
        metrics, page = self.compare_jobs(os.path.join(self.jobs, "a"), "trial", os.path.join(self.jobs, "b"), "trial")
        self.assertEqual([r["label"] for r in metrics["runs"]], ["before", "new"])
        self.assertIn("</span>before</th>", page)


def priced(name: str, task: str, cost: Optional[float], test: str) -> Trial:
    t = trial(name, task, 0.0)
    t.cost = cost
    t.files = {"verifier/ctrf.json": '{"results": {"tests": [{"name": "%s", "status": "failed"}]}}' % test}
    return t


class Prices(LoopCase):
    """The price of an option is the recorded cost of each of its tasks, times the trials each task gets."""

    def compare_groups(self, new_failures: List[Trial]) -> Tuple[dict, str]:
        tasks = sorted({t.task for t in new_failures})
        before = [trial(f"{t.split('/')[-1]}__b", t, 1.0) for t in tasks] + [trial(f"p{i}__b", f"bench/p{i}", 1.0) for i in range(5)]
        new = list(new_failures) + [trial(f"p{i}__n", f"bench/p{i}", 1.0) for i in range(5)]
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        return load(self.round_file("result.json"))["stop_rule"], r.out

    def test_the_cheapest_option_is_the_lowest_priced_group(self) -> None:
        costly = [priced(f"a{i}__n", f"bench/a{i}", 0.10, "test_a") for i in range(4)]
        cheap = [priced(f"b{i}__n", f"bench/b{i}", 0.02, "test_b") for i in range(3)]
        rule, _ = self.compare_groups(costly + cheap)
        self.assertEqual(rule["groups_of_3_or_more"], 2)
        option = rule["cheapest_option"]
        self.assertEqual(option["group"], "g02")
        self.assertAlmostEqual(option["usd"], 3 * 0.02 * 3)
        self.assertEqual(option["unpriced_tasks"], 0)

    def test_a_task_without_a_cost_takes_the_mean_of_the_others(self) -> None:
        group = [priced("a0__n", "bench/a0", 0.02, "test_a"), priced("a1__n", "bench/a1", 0.04, "test_a"),
                 priced("a2__n", "bench/a2", None, "test_a")]
        rule, out = self.compare_groups(group)
        option = rule["cheapest_option"]
        self.assertAlmostEqual(option["usd"], (0.02 + 0.04 + 0.03) * 3)
        self.assertEqual(option["unpriced_tasks"], 1)
        self.assertIn("1 task priced at the mean of the others", out)

    def test_no_cost_at_all_leaves_the_price_unknown(self) -> None:
        group = [priced(f"a{i}__n", f"bench/a{i}", None, "test_a") for i in range(3)]
        rule, _ = self.compare_groups(group)
        self.assertIsNone(rule["cheapest_option"]["usd"])
        self.assertTrue(any("budget" in n for n in rule["not_checked"]))


class ResultBeforeLedger(LoopCase):
    def test_a_result_that_cannot_be_written_leaves_the_ledger_open(self) -> None:
        before, new = paired(5, 1, 1, 1)
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        os.makedirs(self.round_file("result.json.tmp"))
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertNotEqual(r.code, 0)
        self.assertIsNone(load(os.path.join(self.loop, "ledger.json"))["rounds"][0]["result"])


class IncompleteNewCost(LoopCase):
    def compare_partial(self, extra: List[str]) -> dict:
        before, new = paired(20, 1, 1, 1)
        new[0].cost = None
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"] + extra)
        self.assertEqual(r.code, 0, r.out + r.err)
        return load(self.round_file("result.json"))["stop_rule"]

    def test_a_new_run_of_unknown_cost_leaves_the_budget_unchecked(self) -> None:
        rule = self.compare_partial([])
        self.assertIsNone(rule["spent_usd"])
        self.assertIsNone(rule["left_usd"])
        self.assertTrue(any("budget" in n for n in rule["not_checked"]))

    def test_the_person_s_figure_for_the_new_run(self) -> None:
        rule = self.compare_partial(["--new-cost-usd", "0.5"])
        self.assertAlmostEqual(rule["spent_usd"], 0.5)


#: a found class that reads a file only the made-up agent writes
STATUS_RULE = {"id": "cut_reply", "meaning": "The agent wrote that a model reply was cut.",
               "file": "agent/output.json", "json_path": "status", "value": "cut"}


def marked(name: str, task: str, reward: float, output: Optional[str]) -> Trial:
    t = trial(name, task, reward)
    if output is not None:
        t.files = {"agent/output.json": output}
    return t


CUT, OTHER, BROKEN = '{"status": "cut"}', '{"status": "done"}', '{"status": '


class FoundClassesOnBothRuns(LoopCase):
    """`compare` counts each found class on the run compared against and on the new run."""

    def jobs_of(self) -> Tuple[str, str]:
        before = [marked("a__b", "bench/a", 0.0, CUT), marked("b__b", "bench/b", 0.0, CUT),
                  marked("c__b", "bench/c", 1.0, CUT), marked("d__b", "bench/d", 1.0, OTHER),
                  marked("e__b", "bench/e", 0.0, BROKEN)]
        new = [marked("a__n", "bench/a", 1.0, CUT), marked("b__n", "bench/b", 0.0, CUT),
               marked("c__n", "bench/c", 1.0, OTHER), marked("d__n", "bench/d", 1.0, None),
               marked("e__n", "bench/e", 0.0, OTHER)]
        return (write_job(self.jobs, "before", "alpha", "harbor", before),
                write_job(self.jobs, "new", "alpha", "harbor", new))

    def rules(self, rules: List[dict]) -> None:
        write_json(os.path.join(self.loop, "found-classes.json"),
                   {"schema": "run-to-fix.found-classes/v1", "rules": rules})

    def compare(self, against: str) -> dict:
        r = run(["compare", "--loop", self.loop, "--new", os.path.join(self.jobs, "new"), "--against", against])
        self.assertEqual(r.code, 0, r.out + r.err)
        return load(self.round_file("result.json"))

    def test_each_rule_is_counted_on_both_runs(self) -> None:
        before_dir, _ = self.jobs_of()
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.rules([STATUS_RULE])
        self.read_ok(before_dir, None)
        found = self.compare("run")["found_classes"]
        self.assertEqual(found, {"cut_reply": {
            "meaning": STATUS_RULE["meaning"], "file": "agent/output.json",
            "before": {"trials": 3, "failing_trials": 2, "unreadable": 1, "counted_trials": 5},
            "new": {"trials": 2, "failing_trials": 1, "unreadable": 0, "counted_trials": 5}}})

    def test_the_rule_as_the_file_holds_it_when_compare_runs(self) -> None:
        before_dir, _ = self.jobs_of()
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.rules([dict(STATUS_RULE, value="never")])
        self.read_ok(before_dir, None)
        self.classify_ok()
        self.rules([STATUS_RULE])
        found = self.compare("run")["found_classes"]["cut_reply"]
        self.assertEqual((found["before"]["trials"], found["new"]["trials"]), (3, 2))

    def test_against_the_baseline_the_counts_are_of_the_baseline_and_the_new_run(self) -> None:
        before_dir, new_dir = self.jobs_of()
        other = write_job(self.jobs, "other", "alpha", "harbor",
                          [marked(f"{t}__o", f"bench/{t}", 1.0, OTHER) for t in "abcde"])
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.rules([STATUS_RULE])
        self.read_ok(other, before_dir)
        found = self.compare("baseline")["found_classes"]["cut_reply"]
        self.assertEqual(found["before"], {"trials": 3, "failing_trials": 2, "unreadable": 1, "counted_trials": 5})
        self.assertEqual(found["new"], {"trials": 2, "failing_trials": 1, "unreadable": 0, "counted_trials": 5})

    def test_no_file_and_no_rule_give_an_empty_entry(self) -> None:
        before_dir, _ = self.jobs_of()
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        self.assertEqual(self.compare("run")["found_classes"], {})

    def test_an_empty_list_of_rules_gives_an_empty_entry(self) -> None:
        before_dir, _ = self.jobs_of()
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.rules([])
        self.read_ok(before_dir, None)
        self.assertEqual(self.compare("run")["found_classes"], {})

    def test_a_rule_with_an_error_stops_compare_with_the_message_of_classify(self) -> None:
        before_dir, new_dir = self.jobs_of()
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.rules([dict(STATUS_RULE, regex="cut")])
        self.read_ok(before_dir, None)
        by_classify = run(["classify", "--loop", self.loop])
        self.assertNotEqual(by_classify.code, 0)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("needs either regex, or json_path with value", r.err)
        self.assertEqual(r.err, by_classify.err)
        self.assertFalse(os.path.exists(self.round_file("result.json")))


class FoundClassesOnSharedTasks(LoopCase):
    """`compare` counts each found class on the tasks both runs share, the tasks its verdict pairs."""

    def compare_jobs(self, before: List[Trial], new: List[Trial]) -> Tuple[dict, str]:
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        write_json(os.path.join(self.loop, "found-classes.json"),
                   {"schema": "run-to-fix.found-classes/v1", "rules": [STATUS_RULE]})
        self.read_ok(before_dir, None)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        return load(self.round_file("result.json"))["found_classes"]["cut_reply"], r.out

    def test_trials_of_tasks_only_one_run_has_are_not_counted(self) -> None:
        before = [marked("a__b", "bench/a", 0.0, CUT), marked("b__b", "bench/b", 1.0, CUT),
                  marked("x__b", "bench/x", 0.0, CUT), marked("y__b", "bench/y", 0.0, CUT)]
        new = [marked("a-0__n", "bench/a", 0.0, CUT), marked("a-1__n", "bench/a", 1.0, OTHER),
               marked("a-2__n", "bench/a", 1.0, BROKEN), marked("b__n", "bench/b", 1.0, OTHER),
               marked("z__n", "bench/z", 0.0, CUT)]
        found, out = self.compare_jobs(before, new)
        self.assertEqual(found["before"], {"trials": 2, "failing_trials": 1, "unreadable": 0, "counted_trials": 2})
        self.assertEqual(found["new"], {"trials": 1, "failing_trials": 1, "unreadable": 1, "counted_trials": 4})
        self.assertIn("found class cut_reply on 2 shared tasks: before 2 of 2 trials, 1 failing, 0 unreadable; "
                      "new 1 of 4 trials, 1 failing, 1 unreadable", out)

    def test_runs_with_no_shared_task_give_zero_counts(self) -> None:
        before = [marked("x__b", "bench/x", 0.0, CUT), marked("y__b", "bench/y", 1.0, CUT)]
        new = [marked("z__n", "bench/z", 0.0, CUT)]
        found, out = self.compare_jobs(before, new)
        zero = {"trials": 0, "failing_trials": 0, "unreadable": 0, "counted_trials": 0}
        self.assertEqual((found["before"], found["new"]), (zero, zero))
        self.assertIn("found class cut_reply on 0 shared tasks", out)


class FoundClassBehindALink(LoopCase):
    """A found class whose file in one trial links outside its trial folder: `compare` reads the shared tasks only."""

    def loop_with_link(self, linked: str) -> str:
        before = [trial(f"{t}__b", f"bench/{t}", 0.0) for t in ("a", "b", "x")]
        new = [trial(f"{t}__n", f"bench/{t}", 1.0) for t in ("a", "b")]
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        outside = os.path.join(self.tmp, "outside.txt")
        write(outside, "SYNTHETIC\n")
        os.symlink(outside, os.path.join(before_dir, linked, "link"))
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        write_json(os.path.join(self.loop, "found-classes.json"), {"schema": "run-to-fix.found-classes/v1", "rules": [
            {"id": "linked", "meaning": "A file the trial links to.", "file": "link", "regex": "SYNTHETIC"}]})
        self.read_ok(before_dir, None)
        return new_dir

    def test_a_link_in_a_task_the_new_run_lacks_is_not_read_by_compare(self) -> None:
        new_dir = self.loop_with_link("x__b")
        by_classify = run(["classify", "--loop", self.loop])
        self.assertNotEqual(by_classify.code, 0)
        self.assertIn("outside the trial", by_classify.err)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        self.assertTrue(os.path.isfile(self.round_file("result.json")))
        self.assertIsNotNone(load(os.path.join(self.loop, "ledger.json"))["rounds"][0]["result"])

    def test_a_link_in_a_shared_task_stops_compare_with_the_message_of_classify(self) -> None:
        new_dir = self.loop_with_link("a__b")
        by_classify = run(["classify", "--loop", self.loop])
        self.assertNotEqual(by_classify.code, 0)
        ledger_path = os.path.join(self.loop, "ledger.json")
        with open(ledger_path, "rb") as fh:
            saved = fh.read()
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("outside the trial", r.err)
        self.assertEqual(r.err, by_classify.err)
        self.assertFalse(os.path.exists(self.round_file("result.json")))
        self.assertTrue(os.path.isfile(self.round_file("compare-metrics.json")))
        with open(ledger_path, "rb") as fh:
            self.assertEqual(fh.read(), saved)


class ResultOfAnEarlierVersion(LoopCase):
    """A round closed by a version whose result.json has no found_classes still starts the next round."""

    def test_read_starts_the_next_round_and_reads_the_recorded_stop_rule(self) -> None:
        before, new = paired(10, 1, 1, 1)
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        path = self.round_file("result.json")
        result = load(path)
        result.pop("found_classes", None)
        self.assertTrue(result["stop_rule"]["stop"])
        write_json(path, result)
        r = run(["read", "--loop", self.loop, "--run", new_dir, "--budget-usd", "5", "--pass-threshold", "1"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("the stop rule of round-01 says stop", r.err)
        r = run(["read", "--loop", self.loop, "--run", new_dir, "--budget-usd", "5", "--pass-threshold", "1",
                 "--continue-after-stop", "Go on with a new fix."])
        self.assertEqual(r.code, 0, r.out + r.err)
        self.assertTrue(os.path.isfile(os.path.join(self.loop, "round-02", "input.json")))


class StopRuleOfTheRound(LoopCase):
    def test_compare_refuses_a_stop_rule_other_than_the_one_read_recorded(self) -> None:
        before, new = paired(5, 1, 1, 1)
        before_dir = write_job(self.jobs, "before", "alpha", "harbor", before)
        new_dir = write_job(self.jobs, "new", "alpha", "harbor", new)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(before_dir, None)
        path = self.round_file("input.json")
        data = load(path)
        self.assertIn("No new round when", data["stop_rule"])
        data["stop_rule"] = "Another rule."
        write_json(path, data)
        r = run(["compare", "--loop", self.loop, "--new", new_dir, "--against", "run"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("stop rule", r.err)
        self.assertIn("keep this loop folder as it is and start a new loop folder", r.err)
        self.assertNotIn("loop.py read", r.err)
        self.assertFalse(os.path.exists(self.round_file("result.json")))
