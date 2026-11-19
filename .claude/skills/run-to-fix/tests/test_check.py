"""`loop.py candidate` and `loop.py check`: the test, the suite and the replay of a candidate, in worktrees."""
from __future__ import annotations

import json
import os

from agentrepo import RepoCase, git
from jobs import load, run, write


class Candidate(RepoCase):
    def test_records_refs_and_the_prediction_of_its_cause(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("good", self.repo.good[:8], self.repo.test_ref[:8], "outcome")
        data = load(self.round_file("candidates/good/candidate.json"))
        self.assertEqual((data["ref"], data["test_ref"]), (self.repo.good, self.repo.test_ref))
        self.assertEqual(data["cause"], "state")
        self.assertEqual(data["rule"], "g01")
        self.assertEqual(data["effect"], "outcome")
        self.assertEqual(data["prediction"], {"trials": ["f1__r", "f2__r"], "tasks": ["bench/f1", "bench/f2"]})

    def test_a_cause_without_a_rule_is_refused(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        write(self.round_file("causes/vague.md"), "# Something is wrong\n")
        r = run(["candidate", "--loop", self.loop, "--id", "c1", "--cause", "vague", "--ref", self.repo.good,
                 "--test", "tests/test_fix.py", "--test-ref", self.repo.test_ref, "--effect", "outcome"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("Rule:", r.err)

    def test_a_rule_that_counts_nothing_is_refused(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        write(self.round_file("causes/ghost.md"), "# A cause\n\nRule: g09\n")
        r = run(["candidate", "--loop", self.loop, "--id", "c1", "--cause", "ghost", "--ref", self.repo.good,
                 "--test", "tests/test_fix.py", "--test-ref", self.repo.test_ref, "--effect", "outcome"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("g09", r.err)

    def test_an_unknown_ref_is_refused(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        r = run(["candidate", "--loop", self.loop, "--id", "c1", "--cause", "state", "--ref", "no-such-ref",
                 "--test", "tests/test_fix.py", "--test-ref", self.repo.test_ref, "--effect", "outcome"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("no-such-ref", r.err)


class Check(RepoCase):
    def test_a_good_candidate(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
        checks = self.check("good")
        self.assertEqual(checks["test"]["ran"], True)
        self.assertTrue(checks["test"]["fails_before"])
        self.assertTrue(checks["test"]["passes_after"])
        self.assertEqual(checks["suite"]["base_failures"], ["test_old"])
        self.assertEqual((checks["suite"]["added"], checks["suite"]["removed"]), ([], []))
        replay = checks["replay"]
        self.assertTrue(replay["ran"])
        self.assertEqual(replay["before"], {"done_pass": 2, "done_fail": 2, "not_done_pass": 1, "not_done_fail": 1})
        self.assertEqual(replay["after"], {"done_pass": 2, "done_fail": 0, "not_done_pass": 1, "not_done_fail": 3})
        self.assertEqual(replay["false_success_added"], -2)
        self.assertEqual(replay["cause"], {"trials": 2, "changed": 2, "by_direction": {"done → not done": 2}})
        self.assertEqual(replay["changed_trials"], ["f1__r", "f2__r"])
        self.assertEqual(replay["touched"], ["f1__r", "f2__r", "p1__r"])
        self.assertEqual(checks["diff"]["files"], 2)

    def test_a_candidate_that_adds_failures_and_false_successes(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("bad", self.repo.bad, self.repo.test_ref, "outcome")
        checks = self.check("bad")
        self.assertEqual(checks["suite"]["added"], ["test_other"])
        self.assertEqual(checks["replay"]["after"]["done_fail"], 1)
        self.assertEqual(checks["replay"]["false_success_added"], -1)
        self.assertEqual(checks["replay"]["false_success_new"], ["f3__r"])

    def test_a_test_that_does_not_fail_before_the_fix(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("weak", self.repo.good, self.repo.good, "outcome")
        checks = self.check("weak")
        self.assertFalse(checks["test"]["fails_before"])

    def test_the_ref_before_the_fix_is_replayed_once_per_round(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
        self.candidate("bad", self.repo.bad, self.repo.test_ref, "outcome")
        self.check("good")
        self.check("bad")
        self.assertEqual(self.marker("replay.marker"), [self.repo.test_ref, self.repo.good, self.repo.bad])

    def test_worktrees_are_removed_and_the_checkout_is_untouched(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        head = git(self.repo.path, ["rev-parse", "HEAD"])
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
        self.check("good")
        self.assertEqual(git(self.repo.path, ["worktree", "list", "--porcelain"]).count("worktree "), 1)
        self.assertEqual(os.listdir(os.path.join(self.loop, "worktrees")), [])
        self.assertEqual(git(self.repo.path, ["rev-parse", "HEAD"]), head)
        self.assertEqual(git(self.repo.path, ["status", "--porcelain"]), "")

    def test_a_failing_replay_still_removes_its_worktree(self) -> None:
        commands = self.commands()
        commands["replay"] = "exit 4 # {worktree} {ref} {job_dir} {out_json}"
        self.start(commands, ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
        r = run(["check", "--loop", self.loop, "--candidate", "good"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("commands.replay", r.err)
        self.assertEqual(git(self.repo.path, ["worktree", "list", "--porcelain"]).count("worktree "), 1)


class CheckFive(RepoCase):
    """Known-answer check 5 of the brief, with the stub adapter."""

    def test_a_replay_of_two_refs_gives_the_table_and_the_false_results(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("bad", self.repo.bad, self.repo.test_ref, "outcome")
        replay = self.check("bad")["replay"]
        self.assertEqual(replay["before_ref"], self.repo.test_ref)
        self.assertEqual(replay["ref"], self.repo.bad)
        self.assertEqual(replay["before"], {"done_pass": 2, "done_fail": 2, "not_done_pass": 1, "not_done_fail": 1})
        self.assertEqual(replay["after"], {"done_pass": 2, "done_fail": 1, "not_done_pass": 1, "not_done_fail": 2})
        self.assertEqual((replay["false_success_before"], replay["false_success_after"]), (2, 1))

    def test_a_paid_command_is_written_and_not_started(self) -> None:
        self.start(self.commands(), ["run_tasks", "replay"])
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
        replay = self.check("good")["replay"]
        self.assertFalse(replay["ran"])
        self.assertIn("costs money", replay["reason"])
        self.assertEqual(self.marker("replay.marker"), [])
        written = sorted(os.listdir(self.round_file("commands")))
        self.assertEqual(written, sorted([f"replay-{self.repo.test_ref[:12]}.sh", f"replay-{self.repo.good[:12]}.sh"]))
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
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
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
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
        r = run(["check", "--loop", self.loop, "--candidate", "good"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("f3__r", r.err)



REPORT_RULE = {"id": "p3_reports", "meaning": "The trial p3, whose report the report fix changes.",
               "file": "result.json", "regex": "\"trial_name\": \"p3__r\""}


class Effects(RepoCase):
    """A candidate says which effect its fix has: failing trials turn into passes, or the agent's report changes."""

    def with_report_cause(self, rule: dict) -> None:
        write(os.path.join(self.loop, "found-classes.json"),
              json.dumps({"schema": "run-to-fix.found-classes/v1", "rules": [rule]}))
        r = run(["classify", "--loop", self.loop])
        self.assertEqual(r.code, 0, r.out + r.err)
        write(self.round_file("causes/report.md"), f"# The agent reports p3 wrongly\n\nRule: {rule['id']}\n")

    def test_a_report_candidate_predicts_no_pass(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.with_report_cause(REPORT_RULE)
        self.candidate_of("report", "rep", self.repo.report, self.repo.test_ref, "report")
        data = load(self.round_file("candidates/rep/candidate.json"))
        self.assertEqual(data["effect"], "report")
        self.assertEqual(data["prediction"], {"trials": ["p3__r"], "tasks": []})
        replay = self.check("rep")["replay"]
        self.assertEqual(replay["cause"], {"trials": 1, "changed": 1, "by_direction": {"not done → done": 1}})
        self.assertEqual(replay["changed_trials"], ["p3__r"])

    def test_an_outcome_candidate_on_passing_trials_is_refused(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.with_report_cause(REPORT_RULE)
        r = run(["candidate", "--loop", self.loop, "--id", "rep", "--cause", "report", "--ref", self.repo.report,
                 "--test", "tests/test_fix.py", "--test-ref", self.repo.test_ref, "--effect", "outcome"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("counts no failing trial", r.err)

    def test_a_report_candidate_needs_the_replay(self) -> None:
        commands = self.commands()
        commands["replay"] = None
        self.start(commands, ["run_tasks"])
        self.with_report_cause(REPORT_RULE)
        r = run(["candidate", "--loop", self.loop, "--id", "rep", "--cause", "report", "--ref", self.repo.report,
                 "--test", "tests/test_fix.py", "--test-ref", self.repo.test_ref, "--effect", "report"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("commands.replay", r.err)

    def test_a_report_candidate_on_a_rule_that_matches_nothing(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.with_report_cause(dict(REPORT_RULE, regex="no-such-trial"))
        r = run(["candidate", "--loop", self.loop, "--id", "rep", "--cause", "report", "--ref", self.repo.report,
                 "--test", "tests/test_fix.py", "--test-ref", self.repo.test_ref, "--effect", "report"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("matches no trial", r.err)

    def test_the_effect_has_no_default(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        r = run(["candidate", "--loop", self.loop, "--id", "c1", "--cause", "state", "--ref", self.repo.good,
                 "--test", "tests/test_fix.py", "--test-ref", self.repo.test_ref])
        self.assertNotEqual(r.code, 0)
        self.assertIn("--effect", r.err)


class RefsOfTheTable(RepoCase):
    def test_a_candidate_without_a_test_is_compared_with_the_base(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        r = run(["candidate", "--loop", self.loop, "--id", "notest", "--cause", "state", "--ref", self.repo.good,
                 "--effect", "outcome"])
        self.assertEqual(r.code, 0, r.out + r.err)
        replay = self.check("notest")["replay"]
        self.assertEqual((replay["before_ref"], replay["ref"]), (self.repo.base, self.repo.good))

    def test_the_replay_gets_the_base_and_the_ref_before(self) -> None:
        commands = self.commands()
        commands["replay"] = (commands["replay"] or "") + f" && echo {{base_ref}} {{before_ref}} {{ref}} >> {self.tmp}/args.marker"
        self.start(commands, ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
        self.check("good")
        with open(os.path.join(self.tmp, "args.marker"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines, [f"{self.repo.base} {self.repo.test_ref} {self.repo.test_ref}",
                                 f"{self.repo.base} {self.repo.test_ref} {self.repo.good}"])

    def test_registering_again_drops_the_old_checks(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
        self.check("good")
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
        self.assertFalse(os.path.exists(self.round_file("candidates/good/checks.json")))


class OldCandidate(RepoCase):
    def test_a_candidate_without_an_effect_is_refused(self) -> None:
        self.start(self.commands(), ["run_tasks"])
        self.candidate("good", self.repo.good, self.repo.test_ref, "outcome")
        path = self.round_file("candidates/good/candidate.json")
        data = load(path)
        del data["effect"]
        write(path, json.dumps(data))
        for args in (["check", "--loop", self.loop, "--candidate", "good"], ["decide", "--loop", self.loop, "--stop", "1"]):
            r = run(args)
            self.assertNotEqual(r.code, 0, args)
            self.assertIn("register the candidate again", r.err)
