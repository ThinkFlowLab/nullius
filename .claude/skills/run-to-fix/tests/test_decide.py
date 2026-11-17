"""`loop.py decide`, `answer` and `select`: the two stops, the person's answers and the trial run."""
from __future__ import annotations

import os
from typing import List

from agentrepo import RepoCase
from jobs import Result, load, run, trial, write_job

KEEP_GOOD = "Keep good. bad breaks test_other."


class Flow(RepoCase):
    """The steps up to each stop, shared by the tests below."""

    def checked(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref)
        self.candidate("bad", self.repo.bad, self.repo.test_ref)
        self.check("good")
        self.check("bad")

    def page(self, name: str) -> str:
        with open(self.round_file(name), encoding="utf-8") as fh:
            return fh.read()

    def kept(self) -> None:
        self.checked()
        run(["decide", "--loop", self.loop, "--stop", "1"])
        r = run(["answer", "--loop", self.loop, "--stop", "1", "--keep", "good", "--text", KEEP_GOOD])
        self.assertEqual(r.code, 0, r.out + r.err)

    def select(self, extra: List[str]) -> Result:
        return run(["select", "--loop", self.loop, "--ref", self.repo.good, "--canaries", "1"] + extra)

    def trial_job(self) -> str:
        trials = []
        for task, passes in (("f1", 3), ("f2", 2), ("p1", 3)):
            for k in range(3):
                trials.append(trial(f"{task}-{k}__t", f"bench/{task}", 1.0 if k < passes else 0.0))
        return write_job(self.jobs, "trial", "alpha", "harbor", trials)

    def chosen(self) -> str:
        self.kept()
        r = self.select(["--size", "guards", "--budget-usd", "0.5", "--text", "targets and guards"])
        self.assertEqual(r.code, 0, r.out + r.err)
        return self.trial_job()


class StopOne(Flow):
    def test_one_row_per_candidate(self) -> None:
        self.checked()
        r = run(["decide", "--loop", self.loop, "--stop", "1"])
        self.assertEqual(r.code, 0, r.out + r.err)
        page = self.page("decision-1.md")
        rows = [ln for ln in page.splitlines() if ln.startswith("| good |") or ln.startswith("| bad |")]
        self.assertEqual(len(rows), 2)
        good = next(ln for ln in rows if ln.startswith("| good |"))
        bad = next(ln for ln in rows if ln.startswith("| bad |"))
        self.assertIn("2 / 2 / 1 / 1 → 3 / 0 / 0 / 3", good)
        self.assertIn("2 / 2 / 1 / 1 → 3 / 1 / 0 / 2", bad)
        self.assertIn("test_other", bad)
        self.assertIn("f3__r", bad)
        self.assertIn("fails, then passes", good)
        self.assertNotIn("{{", page)

    def test_the_answer_is_stored_in_the_person_s_words(self) -> None:
        self.checked()
        run(["decide", "--loop", self.loop, "--stop", "1"])
        r = run(["answer", "--loop", self.loop, "--stop", "1", "--keep", "good", "--text", KEEP_GOOD])
        self.assertEqual(r.code, 0, r.out + r.err)
        entry = load(os.path.join(self.loop, "ledger.json"))["rounds"][0]
        self.assertEqual(entry["answers"][0]["text"], KEEP_GOOD)
        self.assertEqual(entry["answers"][0]["about"], "stop 1: the candidates to keep")
        self.assertEqual(entry["kept"], ["good"])
        self.assertEqual(entry["predictions"], [{"candidate": "good", "cause": "state", "rule": "g01",
                                                 "tasks": ["bench/f1", "bench/f2"], "expect": "pass"}])

    def test_an_answer_before_the_page_is_refused(self) -> None:
        self.checked()
        r = run(["answer", "--loop", self.loop, "--stop", "1", "--keep", "good", "--text", KEEP_GOOD])
        self.assertNotEqual(r.code, 0)
        self.assertIn("decision-1.md", r.err)

    def test_keeping_an_unchecked_candidate_is_refused(self) -> None:
        self.checked()
        run(["decide", "--loop", self.loop, "--stop", "1"])
        r = run(["answer", "--loop", self.loop, "--stop", "1", "--keep", "good,other", "--text", "keep both"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("other", r.err)


class Select(Flow):
    def test_select_before_stop_one_is_refused(self) -> None:
        self.checked()
        r = self.select([])
        self.assertNotEqual(r.code, 0)
        self.assertIn("stop 1", r.err)

    def test_the_proposal_prices_each_size(self) -> None:
        self.kept()
        r = self.select([])
        self.assertEqual(r.code, 0, r.out + r.err)
        self.assertIn("targets: 2 tasks, 6 trials, $0.06", r.out)
        self.assertIn("targets and guards: 3 tasks, 9 trials, $0.09", r.out)
        self.assertIn("targets, guards and canaries: 4 tasks, 12 trials, $0.12", r.out)
        self.assertFalse(os.path.exists(self.round_file("trial-tasks.txt")))

    def test_a_ref_without_a_kept_candidate_is_refused(self) -> None:
        self.kept()
        r = run(["select", "--loop", self.loop, "--ref", self.repo.bad, "--canaries", "1"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("good", r.err)

    def test_choosing_a_size_writes_the_run_and_starts_nothing(self) -> None:
        self.kept()
        words = "Run targets and guards, 0.50 USD."
        r = self.select(["--size", "guards", "--budget-usd", "0.5", "--text", words])
        self.assertEqual(r.code, 0, r.out + r.err)
        with open(self.round_file("trial-tasks.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().split(), ["bench/f1", "bench/f2", "bench/p1"])
        entry = load(os.path.join(self.loop, "ledger.json"))["rounds"][0]
        sel = entry["selection"]
        self.assertEqual((sel["targets"], sel["guards"], sel["canaries"]), (["bench/f1", "bench/f2"], ["bench/p1"], []))
        self.assertEqual((sel["repeat"], sel["budget_usd"], sel["ref"]), (3, 0.5, self.repo.good))
        self.assertAlmostEqual(sel["price_usd"], 0.09)
        self.assertEqual(entry["answers"][-1]["text"], words)
        build = load(self.round_file("build.json"))
        self.assertTrue(os.path.isfile(build["path"]))
        self.assertEqual(build["ref"], self.repo.good)
        with open(self.round_file("commands/run_tasks.sh"), encoding="utf-8") as fh:
            script = fh.read()
        for part in (self.round_file("trial-tasks.txt"), "--repeat 3", "--cap 0.5", self.repo.good, build["path"]):
            self.assertIn(part, script)
        self.assertEqual(self.marker("runner.marker"), [])

    def test_a_budget_under_the_price_is_refused(self) -> None:
        self.kept()
        r = self.select(["--size", "guards", "--budget-usd", "0.05", "--text", "five cents"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("$0.09", r.err)
        self.assertFalse(os.path.exists(self.round_file("trial-tasks.txt")))

    def test_a_budget_over_the_round_s_is_refused(self) -> None:
        self.kept()
        r = self.select(["--size", "targets", "--budget-usd", "11", "--text", "eleven"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("$10.00", r.err)

    def test_canaries_come_from_a_fixed_seed(self) -> None:
        self.kept()
        first = self.select([]).out
        self.assertEqual(self.select([]).out, first)

    def test_without_touched_there_are_no_guards(self) -> None:
        commands = self.commands()
        commands["replay"] = None
        self.start(commands, ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref)
        self.check("good")
        run(["decide", "--loop", self.loop, "--stop", "1"])
        run(["answer", "--loop", self.loop, "--stop", "1", "--keep", "good", "--text", "keep good"])
        r = self.select([])
        self.assertEqual(r.code, 0, r.out + r.err)
        self.assertIn("guards: none", r.out)
        self.assertIn("no replay", r.out)


class StopTwo(Flow):
    def test_the_page_gives_three_options_with_their_cost(self) -> None:
        job = self.chosen()
        r = run(["decide", "--loop", self.loop, "--stop", "2", "--trial-job", job])
        self.assertEqual(r.code, 0, r.out + r.err)
        trial_record = load(self.round_file("trial.json"))
        self.assertAlmostEqual(trial_record["cost_usd"], 0.09)
        self.assertAlmostEqual(trial_record["budget_left_usd"], 9.91)
        self.assertEqual(trial_record["targets"]["before"], {"trials": 2, "passed": 0})
        self.assertEqual(trial_record["targets"]["after"], {"trials": 6, "passed": 5})
        self.assertEqual(trial_record["guards"], {"trials": 3, "passed": 3})
        options = {o["name"]: o for o in trial_record["options"]}
        self.assertEqual(set(options), {"stop", "rerun", "full"})
        self.assertAlmostEqual(options["stop"]["usd"], 0.0)
        self.assertAlmostEqual(options["rerun"]["usd"], 0.09)
        self.assertAlmostEqual(options["full"]["usd"], 0.06)
        page = self.page("decision-2.md")
        for text in ("$9.91", "$0.09", "$0.06", "5 of 6", "agent-"):
            self.assertIn(text, page)
        self.assertNotIn("{{", page)
        for name in ("run_tasks-rerun.sh", "run_tasks-full.sh"):
            self.assertTrue(os.path.isfile(self.round_file(os.path.join("commands", name))))
        with open(self.round_file("full-tasks.txt"), encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().split()), 6)
        self.assertEqual(self.marker("runner.marker"), [])

    def test_the_answer_of_stop_two(self) -> None:
        job = self.chosen()
        run(["decide", "--loop", self.loop, "--stop", "2", "--trial-job", job])
        words = "A full pass, the targets look fixed."
        r = run(["answer", "--loop", self.loop, "--stop", "2", "--choice", "full", "--text", words])
        self.assertEqual(r.code, 0, r.out + r.err)
        entry = load(os.path.join(self.loop, "ledger.json"))["rounds"][0]
        self.assertEqual(entry["answers"][-1], {"about": "stop 2: what to run next", "date": entry["answers"][-1]["date"],
                                                "text": words, "choice": "full"})

    def test_compare_reads_the_selection_and_the_predictions(self) -> None:
        job = self.chosen()
        r = run(["compare", "--loop", self.loop, "--new", job, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        result = load(self.round_file("result.json"))
        self.assertEqual(result["targets"]["after"], {"trials": 6, "passed": 5})
        self.assertEqual(result["predictions"][0]["result"], {"trials": 6, "passed": 5, "tasks": 2})

    def test_the_stop_rule_counts_the_trial_run_and_the_new_run(self) -> None:
        job = self.chosen()
        run(["decide", "--loop", self.loop, "--stop", "2", "--trial-job", job])
        run(["answer", "--loop", self.loop, "--stop", "2", "--choice", "full", "--text", "a full pass"])
        full = write_job(self.jobs, "full", "alpha", "harbor",
                         [trial(f"{t}__f", f"bench/{t}", 1.0) for t in ("p1", "p2", "p3", "f1", "f2", "f3")])
        r = run(["compare", "--loop", self.loop, "--new", full, "--against", "run"])
        self.assertEqual(r.code, 0, r.out + r.err)
        rule = load(self.round_file("result.json"))["stop_rule"]
        self.assertAlmostEqual(rule["spent_usd"], 0.09 + 0.06)
        self.assertAlmostEqual(rule["left_usd"], 10 - 0.15)
