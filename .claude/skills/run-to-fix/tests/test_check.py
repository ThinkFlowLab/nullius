"""`loop.py candidate` and `loop.py check`: the test, the suite and the replay of a candidate, in worktrees."""
from __future__ import annotations

import os

from agentrepo import RepoCase, git
from jobs import load, run, write


class Candidate(RepoCase):
    def test_records_refs_and_the_prediction_of_its_cause(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("good", self.repo.good[:8], self.repo.test_ref[:8])
        data = load(self.round_file("candidates/good/candidate.json"))
        self.assertEqual((data["ref"], data["test_ref"]), (self.repo.good, self.repo.test_ref))
        self.assertEqual(data["cause"], "state")
        self.assertEqual(data["rule"], "g01")
        self.assertEqual(data["prediction"], {"trials": ["f1__r", "f2__r"], "tasks": ["bench/f1", "bench/f2"]})

    def test_a_cause_without_a_rule_is_refused(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        write(self.round_file("causes/vague.md"), "# Something is wrong\n")
        r = run(["candidate", "--loop", self.loop, "--id", "c1", "--cause", "vague", "--ref", self.repo.good,
                 "--test", "tests/test_fix.py", "--test-ref", self.repo.test_ref])
        self.assertNotEqual(r.code, 0)
        self.assertIn("Rule:", r.err)

    def test_a_rule_that_counts_nothing_is_refused(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        write(self.round_file("causes/ghost.md"), "# A cause\n\nRule: g09\n")
        r = run(["candidate", "--loop", self.loop, "--id", "c1", "--cause", "ghost", "--ref", self.repo.good,
                 "--test", "tests/test_fix.py", "--test-ref", self.repo.test_ref])
        self.assertNotEqual(r.code, 0)
        self.assertIn("g09", r.err)

    def test_an_unknown_ref_is_refused(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        r = run(["candidate", "--loop", self.loop, "--id", "c1", "--cause", "state", "--ref", "no-such-ref",
                 "--test", "tests/test_fix.py", "--test-ref", self.repo.test_ref])
        self.assertNotEqual(r.code, 0)
        self.assertIn("no-such-ref", r.err)


class Check(RepoCase):
    def test_a_good_candidate(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref)
        checks = self.check("good")
        self.assertEqual(checks["test"]["ran"], True)
        self.assertTrue(checks["test"]["fails_before"])
        self.assertTrue(checks["test"]["passes_after"])
        self.assertEqual(checks["suite"]["base_failures"], ["test_old"])
        self.assertEqual((checks["suite"]["added"], checks["suite"]["removed"]), ([], []))
        replay = checks["replay"]
        self.assertTrue(replay["ran"])
        self.assertEqual(replay["before"], {"done_pass": 2, "done_fail": 2, "not_done_pass": 1, "not_done_fail": 1})
        self.assertEqual(replay["after"], {"done_pass": 3, "done_fail": 0, "not_done_pass": 0, "not_done_fail": 3})
        self.assertEqual(replay["false_success_added"], -2)
        self.assertEqual(replay["touched"], ["f1__r", "f2__r", "p1__r"])
        self.assertEqual(checks["diff"]["files"], 2)

    def test_a_candidate_that_adds_failures_and_false_successes(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("bad", self.repo.bad, self.repo.test_ref)
        checks = self.check("bad")
        self.assertEqual(checks["suite"]["added"], ["test_other"])
        self.assertEqual(checks["replay"]["after"]["done_fail"], 1)
        self.assertEqual(checks["replay"]["false_success_added"], -1)
        self.assertEqual(checks["replay"]["false_success_new"], ["f3__r"])

    def test_a_test_that_does_not_fail_before_the_fix(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("weak", self.repo.good, self.repo.good)
        checks = self.check("weak")
        self.assertFalse(checks["test"]["fails_before"])

    def test_the_base_is_checked_once_per_round(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref)
        self.candidate("bad", self.repo.bad, self.repo.test_ref)
        self.check("good")
        self.check("bad")
        self.assertEqual(self.marker("replay.marker"), [self.repo.base, self.repo.good, self.repo.bad])

    def test_worktrees_are_removed_and_the_checkout_is_untouched(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        head = git(self.repo.path, ["rev-parse", "HEAD"])
        self.candidate("good", self.repo.good, self.repo.test_ref)
        self.check("good")
        self.assertEqual(git(self.repo.path, ["worktree", "list", "--porcelain"]).count("worktree "), 1)
        self.assertEqual(os.listdir(os.path.join(self.loop, "worktrees")), [])
        self.assertEqual(git(self.repo.path, ["rev-parse", "HEAD"]), head)
        self.assertEqual(git(self.repo.path, ["status", "--porcelain"]), "")

    def test_a_failing_replay_still_removes_its_worktree(self) -> None:
        commands = self.commands()
        commands["replay"] = "exit 4 # {worktree} {ref} {job_dir} {out_json}"
        self.start(commands, ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref)
        r = run(["check", "--loop", self.loop, "--candidate", "good"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("commands.replay", r.err)
        self.assertEqual(git(self.repo.path, ["worktree", "list", "--porcelain"]).count("worktree "), 1)


class CheckFive(RepoCase):
    """Known-answer check 5 of the brief, with the stub adapter."""

    def test_a_replay_of_two_refs_gives_the_table_and_the_false_results(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("bad", self.repo.bad, self.repo.test_ref)
        replay = self.check("bad")["replay"]
        self.assertEqual(replay["base_ref"], self.repo.base)
        self.assertEqual(replay["ref"], self.repo.bad)
        self.assertEqual(replay["before"], {"done_pass": 2, "done_fail": 2, "not_done_pass": 1, "not_done_fail": 1})
        self.assertEqual(replay["after"], {"done_pass": 3, "done_fail": 1, "not_done_pass": 0, "not_done_fail": 2})
        self.assertEqual((replay["false_success_before"], replay["false_success_after"]), (2, 1))

    def test_a_paid_command_is_written_and_not_started(self) -> None:
        self.start(self.commands(), ["run_tasks", "replay"])
        self.candidate("good", self.repo.good, self.repo.test_ref)
        replay = self.check("good")["replay"]
        self.assertFalse(replay["ran"])
        self.assertIn("costs money", replay["reason"])
        self.assertEqual(self.marker("replay.marker"), [])
        written = sorted(os.listdir(self.round_file("commands")))
        self.assertEqual(written, sorted([f"replay-{self.repo.base[:12]}.sh", f"replay-{self.repo.good[:12]}.sh"]))
        with open(self.round_file(os.path.join("commands", f"replay-{self.repo.good[:12]}.sh")), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn(self.repo.good, script)
        self.assertIn("worktree add --detach", script)
        self.assertIn("worktree remove", script)

    def test_a_null_command_is_a_check_that_did_not_run(self) -> None:
        commands = self.commands()
        commands["test"] = None
        commands["replay"] = None
        self.start(commands, ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref)
        checks = self.check("good")
        self.assertFalse(checks["suite"]["ran"])
        self.assertIn("commands.test is null", checks["suite"]["reason"])
        self.assertFalse(checks["replay"]["ran"])
        self.assertIn("commands.replay is null", checks["replay"]["reason"])
        r = run(["decide", "--loop", self.loop, "--stop", "1"])
        self.assertEqual(r.code, 0, r.out + r.err)
        with open(self.round_file("decision-1.md"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("did not run", page)
        self.assertIn("commands.test is null", page)
        self.assertIn("commands.replay is null", page)
        self.assertNotIn("{{", page)

    def test_a_replay_that_leaves_out_a_trial_stops(self) -> None:
        commands = self.commands()
        commands["replay"] = (commands["replay"] or "") + " && python3 -c \"import json,sys; p=sys.argv[1]; " \
            "d=json.load(open(p)); d['trials'].pop('f3__r'); json.dump(d, open(p, 'w'))\" {out_json}"
        self.start(commands, ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref)
        r = run(["check", "--loop", self.loop, "--candidate", "good"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("f3__r", r.err)

