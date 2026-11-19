"""`loop.py classify`: the seven classes, signatures, groups, warnings and the agent's own classes."""
from __future__ import annotations

import os
import sys
from typing import Dict, List

from jobs import (LIMITS, NO_LIMITS, LoopCase, Trial, adapter, load, run, trial, write, write_job, write_json,
                  write_trial)

FIVE = {"pass", "time_limit", "exception", "no_score", "verifier_failed"}
SEVEN = FIVE | {"step_limit", "output_limit"}


def precedence_trials() -> List[Trial]:
    passed_slowly = trial("pass_slow__a", "bench/pass-slow", 1.0)
    passed_slowly.agent_s = 700
    timeout = trial("timeout__a", "bench/timeout", None)
    timeout.exception = ("AgentTimeoutError", "the agent did not end in 600 s")
    long_phase = trial("long_phase__a", "bench/long-phase", 0.0)
    long_phase.agent_s = 600
    crash_after_long_reply = trial("crash_long_reply__a", "bench/crash-long-reply", None)
    crash_after_long_reply.exception = ("RuntimeError", "repeated stream chunk 12")
    crash_after_long_reply.max_output = 4096
    no_score = trial("no_score__a", "bench/no-score", None)
    no_score_many_steps = trial("no_score_steps__a", "bench/no-score-steps", None)
    no_score_many_steps.calls = 20
    steps = trial("steps__a", "bench/steps", 0.0)
    steps.calls = 20
    output = trial("output__a", "bench/output", 0.0)
    output.max_output = 4096
    failed = trial("failed__a", "bench/failed", 0.0)
    partial = trial("partial__a", "bench/partial", 0.5)
    return [trial("pass__a", "bench/pass", 1.0), passed_slowly, timeout, long_phase, crash_after_long_reply,
            no_score, no_score_many_steps, steps, output, failed, partial]


class Classes(LoopCase):
    def classes(self) -> Dict[str, str]:
        return {name: row["class"] for name, row in self.by_trial().items()}

    def test_precedence_with_every_limit(self) -> None:
        run_dir = write_job(self.jobs, "run", "alpha", "harbor", precedence_trials())
        self.set_adapter(adapter("alpha", LIMITS, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        self.assertEqual(self.classes(), {
            "pass__a": "pass", "pass_slow__a": "pass", "timeout__a": "time_limit", "long_phase__a": "time_limit",
            "crash_long_reply__a": "exception", "no_score__a": "no_score", "no_score_steps__a": "no_score",
            "steps__a": "step_limit", "output__a": "output_limit", "failed__a": "verifier_failed",
            "partial__a": "verifier_failed"})
        groups = load(self.round_file("groups.json"))
        self.assertEqual(groups["classes"], {"pass": 2, "time_limit": 2, "exception": 1, "no_score": 2,
                                             "step_limit": 1, "output_limit": 1, "verifier_failed": 2})
        self.assertEqual(groups["rules_off"], [])

    def test_a_null_limit_switches_its_class_off(self) -> None:
        run_dir = write_job(self.jobs, "run", "alpha", "harbor", precedence_trials())
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        got = self.classes()
        self.assertEqual(got["timeout__a"], "time_limit")
        self.assertEqual(got["long_phase__a"], "verifier_failed")
        self.assertEqual(got["steps__a"], "verifier_failed")
        self.assertEqual(got["output__a"], "verifier_failed")
        self.assertEqual(load(self.round_file("groups.json"))["rules_off"],
                         ["time_limit by the agent phase", "step_limit", "output_limit"])

    def test_a_made_up_agent_with_no_limits_and_no_commands(self) -> None:
        timeout = trial("t2__z", "made-up-bench/u2", None)
        timeout.exception = ("AgentTimeoutError", "the agent did not answer in time")
        crash = trial("t3__z", "made-up-bench/u3", None)
        crash.exception = ("ConnectionError", "relay closed")
        capped = trial("t5__z", "made-up-bench/u5", 0.0)
        capped.calls = 500
        capped.max_output = 100000
        capped.agent_s = 99999
        capped.files = {"agent/output.json": '{"status": "step_cap", "steps": 500}'}
        trials = [trial("t1__z", "made-up-bench/u1", 1.0), timeout, crash, trial("t4__z", "made-up-bench/u4", None),
                  capped, trial("t6__z", "made-up-bench/u6", 0.0)]
        run_dir = write_job(self.jobs, "zephyr-run", "zephyr", "standard", trials)
        self.set_adapter(adapter("zephyr", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        got = self.classes()
        self.assertTrue(set(got.values()) <= FIVE, got)
        self.assertEqual(set(got.values()), FIVE)
        self.assertEqual(got["t5__z"], "verifier_failed")


class Signatures(LoopCase):
    def test_numbers_are_masked_and_verifier_text_is_read(self) -> None:
        e1 = trial("e1__a", "bench/e1", None)
        e1.exception = ("ValueError", "Traceback (most recent call last):\n  ...\nValueError: bad token at 12")
        e2 = trial("e2__a", "bench/e2", None)
        e2.exception = ("ValueError", "Traceback (most recent call last):\n  ...\nValueError: bad token at 345")
        ctrf = trial("ctrf__a", "bench/ctrf", 0.0)
        ctrf.files = {"verifier/ctrf.json": '{"results": {"tests": [{"name": "test_a", "status": "passed"}, '
                                            '{"name": "test_b", "status": "failed"}]}}'}
        stdout = trial("stdout__a", "bench/stdout", 0.0)
        stdout.verifier_text = "ok test_one\nFAILED test_total - assert 3 == 4\nFAILED test_other"
        silent = trial("silent__a", "bench/silent", 0.0)
        run_dir = write_job(self.jobs, "run", "alpha", "harbor", [e1, e2, ctrf, stdout, silent,
                                                                  trial("p__a", "bench/p", 1.0)])
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        rows = self.by_trial()
        self.assertEqual(rows["e1__a"]["signature"], "exception: ValueError: bad token at #")
        self.assertEqual(rows["e2__a"]["signature"], rows["e1__a"]["signature"])
        self.assertEqual(rows["ctrf__a"]["signature"], "verifier_failed: test_b")
        self.assertEqual(rows["stdout__a"]["signature"], "verifier_failed: FAILED test_total - assert # == #")
        self.assertEqual(rows["silent__a"]["signature"], "verifier_failed")
        self.assertEqual(rows["p__a"]["signature"], "pass")

    def test_the_standard_score_reason(self) -> None:
        wrong = trial("w__s", "made-up-bench/w", 0.0)
        wrong.verifier_text = "expected 42, got 41"
        run_dir = write_job(self.jobs, "run", "alpha", "standard", [wrong, trial("p__s", "made-up-bench/p", 1.0)])
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        self.assertEqual(self.by_trial()["w__s"]["signature"], "verifier_failed: expected #, got #")


def shared_failure(name: str, task: str) -> Trial:
    t = trial(name, task, 0.0)
    t.files = {"verifier/ctrf.json": '{"results": {"tests": [{"name": "test_x", "status": "failed"}]}}'}
    return t


class GroupsAndPairs(LoopCase):
    def test_groups_warnings_and_pairs(self) -> None:
        run_trials = [shared_failure(f"{t}__r", f"bench/{t}") for t in "abcd"]
        run_trials += [trial("e__r", "bench/e", 0.0), trial("f__r", "bench/f", 1.0)]
        run_trials += [trial(f"p{i}__r", f"bench/p{i}", 1.0) for i in range(14)]
        base_trials = [shared_failure(f"{t}__b", f"bench/{t}") for t in "abcd"]
        base_trials += [trial("e__b", "bench/e", 1.0), trial("f__b", "bench/f", 0.0)]
        base_trials += [trial(f"p{i}__b", f"bench/p{i}", 1.0) for i in range(14)]
        run_dir = write_job(self.jobs, "run", "alpha", "harbor", run_trials)
        base_dir = write_job(self.jobs, "base", "alpha", "harbor", base_trials)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, base_dir)
        self.classify_ok()

        rows = self.by_trial()
        self.assertEqual(rows["a__r"]["pair"], "neither")
        self.assertEqual(rows["e__r"]["pair"], "baseline_only")
        self.assertEqual(rows["f__r"]["pair"], "run_only")
        self.assertEqual(rows["p0__r"]["pair"], "both")

        groups = load(self.round_file("groups.json"))
        self.assertEqual(groups["failures"], 5)
        shared = next(g for g in groups["groups"] if g["signature"] == "verifier_failed: test_x")
        self.assertEqual(shared["trials"], 4)
        self.assertEqual(sorted(shared["trial_names"]), ["a__r", "b__r", "c__r", "d__r"])
        kinds = {w["kind"]: w for w in shared["warnings"]}
        self.assertEqual(set(kinds), {"frequent", "fails_on_baseline"})
        self.assertIn("benchmark", kinds["fails_on_baseline"]["text"])
        self.assertEqual(kinds["frequent"]["trials"], 4)
        lone = next(g for g in groups["groups"] if g["trial_names"] == ["e__r"])
        self.assertEqual(lone["warnings"], [])
        self.assertEqual(groups["groups"][0]["id"], "g01")
        self.assertEqual(groups["groups"][0]["signature"], "verifier_failed: test_x")

    def test_fails_in_every_repeat(self) -> None:
        trials = [shared_failure("a1__r", "bench/a"), shared_failure("a2__r", "bench/a"),
                  shared_failure("b1__r", "bench/b"), shared_failure("b2__r", "bench/b"),
                  shared_failure("c1__r", "bench/c"), trial("c2__r", "bench/c", 1.0),
                  trial("p1__r", "bench/p", 1.0), trial("p2__r", "bench/p", 1.0)]
        run_dir = write_job(self.jobs, "run", "alpha", "harbor", trials)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        group = load(self.round_file("groups.json"))["groups"][0]
        self.assertEqual(group["trials"], 5)
        repeat = [w for w in group["warnings"] if w["kind"] == "fails_in_every_repeat"]
        self.assertEqual(len(repeat), 1)
        self.assertEqual(repeat[0]["tasks"], 2)

    def test_a_count_that_disagrees_with_run_analysis_stops(self) -> None:
        run_dir = write_job(self.jobs, "run", "alpha", "harbor",
                            [trial("p__r", "bench/p", 1.0), trial("f__r", "bench/f", 0.0)])
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        metrics = load(self.round_file("metrics.json"))
        metrics["runs"][0]["trials_passed"] = 2
        write_json(self.round_file("metrics.json"), metrics)
        r = run(["classify", "--loop", self.loop])
        self.assertNotEqual(r.code, 0)
        self.assertIn("metrics.json", r.err)
        self.assertFalse(os.path.exists(self.round_file("trials.csv")))


class BothRunPaths(LoopCase):
    def test_the_same_columns_for_harbor_and_standard(self) -> None:
        headers, classes = [], []
        for runner in ("harbor", "standard"):
            self.loop = os.path.join(self.tmp, f"loop-{runner}")
            os.makedirs(self.loop)
            timeout = trial("t__x", "bench/t", None)
            timeout.exception = ("AgentTimeoutError", "late")
            run_dir = write_job(self.jobs, runner, "alpha", runner,
                                [trial("p__x", "bench/p", 1.0), trial("f__x", "bench/f", 0.0), timeout])
            self.set_adapter(adapter("alpha", LIMITS, {}))
            self.read_ok(run_dir, None)
            self.classify_ok()
            rows = self.rows()
            headers.append(list(rows[0].keys()))
            classes.append({r["trial"]: r["class"] for r in rows})
        self.assertEqual(headers[0], headers[1])
        self.assertEqual(classes[0], classes[1])
        self.assertEqual(classes[0], {"p__x": "pass", "f__x": "verifier_failed", "t__x": "time_limit"})
        for column in ("trial", "task", "class", "signature", "reward", "pair", "cost_usd", "model_calls",
                       "agent_s", "max_completion_tokens", "exception_type"):
            self.assertIn(column, headers[0])


STUB_CLASSES = """
import json, os, sys
job_dir, out = sys.argv[1], sys.argv[2]
trials = {}
for name in sorted(os.listdir(job_dir)):
    if os.path.isfile(os.path.join(job_dir, name, "result.json")):
        trials[name] = {"classes": ["reply_overflow"] if name.startswith("f") else [], "flags": ["slow"]}
json.dump({"trials": trials}, open(out, "w"))
"""


class AgentClasses(LoopCase):
    def job(self) -> str:
        return write_job(self.jobs, "run", "alpha", "harbor",
                         [trial("p__r", "bench/p", 1.0), trial("f1__r", "bench/f1", 0.0),
                          trial("f2__r", "bench/f2", 0.0)])

    def test_extra_columns_beside_the_seven(self) -> None:
        stub = os.path.join(self.tmp, "stub_classes.py")
        write(stub, STUB_CLASSES)
        run_dir = self.job()
        self.set_adapter(adapter("alpha", NO_LIMITS, {"classes": f"{sys.executable} {stub} {{job_dir}} {{out_json}}"}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        rows = self.by_trial()
        self.assertEqual(rows["f1__r"]["class"], "verifier_failed")
        self.assertEqual(rows["f1__r"]["agent_class:reply_overflow"], "1")
        self.assertEqual(rows["p__r"]["agent_class:reply_overflow"], "0")
        self.assertEqual(rows["p__r"]["agent_flag:slow"], "1")
        agent = load(self.round_file("groups.json"))["agent_classes"]
        self.assertTrue(agent["ran"])
        self.assertEqual(agent["classes"], {"reply_overflow": 2})
        self.assertEqual(agent["flags"], {"slow": 3})

    def test_a_null_command_is_a_check_that_did_not_run(self) -> None:
        run_dir = self.job()
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        agent = load(self.round_file("groups.json"))["agent_classes"]
        self.assertFalse(agent["ran"])
        self.assertIn("commands.classes is null", agent["reason"])
        self.assertFalse(any(k.startswith(("agent_class:", "agent_flag:")) for k in self.rows()[0]))

    def test_a_failing_command_stops(self) -> None:
        run_dir = self.job()
        self.set_adapter(adapter("alpha", NO_LIMITS, {"classes": "echo broken classes; exit 3 # {job_dir} {out_json}"}))
        self.read_ok(run_dir, None)
        r = run(["classify", "--loop", self.loop])
        self.assertNotEqual(r.code, 0)
        self.assertIn("commands.classes", r.err)
        self.assertIn("broken classes", r.err)


class FoundClasses(LoopCase):
    def job(self) -> str:
        own = trial("f1__r", "bench/f1", 0.0)
        own.files = {"agent/output.json": '{"status": "step_cap"}'}
        broken = trial("f3__r", "bench/f3", 0.0)
        broken.files = {"agent/output.json": '{"status": '}
        run_dir = write_job(self.jobs, "run", "alpha", "harbor",
                            [trial("p__r", "bench/p", 1.0), own, trial("f2__r", "bench/f2", 0.0), broken])
        for name in ("p__r", "f2__r"):
            path = os.path.join(run_dir, name, "agent", "trajectory.json")
            data = load(path)
            data["steps"][-1]["extra"] = {"finish_reason": "length"}
            write_json(path, data)
        return run_dir

    def rules(self, rules: List[dict]) -> None:
        write_json(os.path.join(self.loop, "found-classes.json"),
                   {"schema": "run-to-fix.found-classes/v1", "rules": rules})

    def test_rules_count_every_trial(self) -> None:
        run_dir = self.job()
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.rules([
            {"id": "reply_cut", "meaning": "a model reply ended at the length limit.",
             "file": "agent/trajectory.json", "json_path": "steps.*.extra.finish_reason", "value": "length"},
            {"id": "own_step_cap", "meaning": "the agent wrote that it stopped at its step cap.",
             "file": "agent/output.json", "regex": "\"status\":\\s*\"step_cap\""},
            {"id": "never", "meaning": "a text no trial has.", "file": "agent/trajectory.json",
             "regex": "no-such-text-anywhere"},
        ])
        self.read_ok(run_dir, None)
        self.classify_ok()
        rows = self.by_trial()
        self.assertEqual({n: r["found:reply_cut"] for n, r in rows.items()},
                         {"p__r": "1", "f1__r": "0", "f2__r": "1", "f3__r": "0"})
        self.assertEqual({n: r["found:own_step_cap"] for n, r in rows.items()},
                         {"p__r": "0", "f1__r": "1", "f2__r": "0", "f3__r": "0"})
        self.assertEqual({r["found:never"] for r in rows.values()}, {"0"})
        self.assertEqual(rows["f1__r"]["class"], "verifier_failed")
        found = load(self.round_file("groups.json"))["found_classes"]
        self.assertEqual(found["reply_cut"]["trials"], 2)
        self.assertEqual(found["reply_cut"]["failing_trials"], 1)
        self.assertEqual(found["own_step_cap"]["trials"], 1)
        self.assertEqual(found["never"]["trials"], 0)

    def test_an_unreadable_file_is_counted_apart(self) -> None:
        run_dir = self.job()
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.rules([{"id": "status", "meaning": "the agent wrote a step cap status.", "file": "agent/output.json",
                     "json_path": "status", "value": "step_cap"}])
        self.read_ok(run_dir, None)
        self.classify_ok()
        rows = self.by_trial()
        self.assertEqual(rows["f3__r"]["found:status"], "unreadable")
        self.assertEqual(rows["f1__r"]["found:status"], "1")
        self.assertEqual(rows["p__r"]["found:status"], "0")
        found = load(self.round_file("groups.json"))["found_classes"]["status"]
        self.assertEqual((found["trials"], found["unreadable"]), (1, 1))

    def test_a_rule_outside_the_trial_is_refused(self) -> None:
        run_dir = self.job()
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.rules([{"id": "escape", "meaning": "reads a file of another trial.", "file": "../p__r/result.json",
                     "regex": "x"}])
        self.read_ok(run_dir, None)
        r = run(["classify", "--loop", self.loop])
        self.assertNotEqual(r.code, 0)
        self.assertIn("escape", r.err)

    def test_a_rule_with_two_patterns_is_refused(self) -> None:
        run_dir = self.job()
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.rules([{"id": "both", "meaning": "two patterns.", "file": "result.json", "regex": "x",
                     "json_path": "a", "value": 1}])
        self.read_ok(run_dir, None)
        r = run(["classify", "--loop", self.loop])
        self.assertNotEqual(r.code, 0)
        self.assertIn("both", r.err)


#: the last lines of two exception messages of a made-up agent; they differ in the model text quoted after unit=
REPEATED_STREAM = [
    r"MadeUpAgent failed: LoopError: [4021] model call failed, reason: repeated stream output detected: "
    r"field=reasoning, unit='Fine.\n\nNext step.\n\n', repeat_count=4",
    r"MadeUpAgent failed: LoopError: [4021] model call failed, reason: repeated stream output detected: "
    r"field=reasoning, unit='\n\nWait.', repeat_count=19",
]


class QuotedStrings(LoopCase):
    def classify_exceptions(self, messages: List[str]) -> Dict[str, Dict[str, str]]:
        trials = []
        for i, message in enumerate(messages):
            t = trial(f"e{i}__a", f"bench/e{i}", None)
            t.exception = ("NonZeroAgentExitCodeError", message)
            trials.append(t)
        trials.append(trial("p__a", "bench/p", 1.0))
        run_dir = write_job(self.jobs, "run", "alpha", "harbor", trials)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        return self.by_trial()

    def test_two_repeated_streams_share_one_signature(self) -> None:
        rows = self.classify_exceptions(REPEATED_STREAM)
        self.assertEqual(rows["e0__a"]["signature"], rows["e1__a"]["signature"])
        self.assertTrue(rows["e0__a"]["signature"].endswith("unit='#', repeat_count=#"), rows["e0__a"]["signature"])
        group = load(self.round_file("groups.json"))["groups"][0]
        self.assertEqual(group["trials"], 2)
        self.assertEqual(group["example"], "NonZeroAgentExitCodeError: " + REPEATED_STREAM[0])

    def test_the_mask_runs_before_the_cut(self) -> None:
        rows = self.classify_exceptions([f"reason: unit='{'x' * 400}', repeat_count=3"])
        self.assertEqual(rows["e0__a"]["signature"], "exception: NonZeroAgentExitCodeError: reason: unit='#', repeat_count=#")

    def test_a_quote_that_is_not_closed_masks_to_the_end(self) -> None:
        rows = self.classify_exceptions(["reason: unit='OK. Writing. Let me go"])
        self.assertEqual(rows["e0__a"]["signature"], "exception: NonZeroAgentExitCodeError: reason: unit='#")

    def test_an_apostrophe_inside_a_word_masks_nothing(self) -> None:
        rows = self.classify_exceptions(["the agent's reply isn't valid at step 12"])
        self.assertEqual(rows["e0__a"]["signature"],
                         "exception: NonZeroAgentExitCodeError: the agent's reply isn't valid at step #")

    def test_double_quotes_are_masked(self) -> None:
        rows = self.classify_exceptions(['tool "browser_click" failed with "element not found"'])
        self.assertEqual(rows["e0__a"]["signature"], 'exception: NonZeroAgentExitCodeError: tool "#" failed with "#"')

    def test_verifier_and_timeout_details_keep_their_quotes(self) -> None:
        failed = trial("f__a", "bench/f", 0.0)
        failed.verifier_text = "FAIL: the sample's invoice does not have the 'Done' label"
        late = trial("t__a", "bench/t", None)
        late.exception = ("AgentTimeoutError", "the agent did not answer 'ping' in time")
        run_dir = write_job(self.jobs, "run", "alpha", "harbor", [failed, late, trial("p__a", "bench/p", 1.0)])
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        rows = self.by_trial()
        self.assertEqual(rows["f__a"]["signature"],
                         "verifier_failed: FAIL: the sample's invoice does not have the 'Done' label")
        self.assertEqual(rows["t__a"]["signature"], "time_limit: AgentTimeoutError: the agent did not answer 'ping' in time")


class BaselineWarningText(LoopCase):
    def test_a_small_group_only_says_the_task_fails_on_both_runs(self) -> None:
        run_dir = write_job(self.jobs, "run", "alpha", "harbor",
                            [shared_failure("a__r", "bench/a"), trial("p__r", "bench/p", 1.0)])
        base_dir = write_job(self.jobs, "base", "alpha", "harbor",
                             [shared_failure("a__b", "bench/a"), trial("p__b", "bench/p", 1.0)])
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, base_dir)
        self.classify_ok()
        warning = load(self.round_file("groups.json"))["groups"][0]["warnings"][0]
        self.assertEqual(warning["kind"], "fails_on_baseline")
        self.assertIn("fails on both runs", warning["text"])
        self.assertNotIn("benchmark", warning["text"])


class RetriedTries(LoopCase):
    """A job of the standard keeps the earlier tries of an attempt in retried/<trial>.try-N; none is a trial."""

    def test_the_skill_and_run_analysis_count_the_same_trials(self) -> None:
        trials = [trial("u1__s", "made-up-bench/u1", 1.0), trial("u2__s", "made-up-bench/u2", 0.0),
                  trial("u3__s", "made-up-bench/u3", 1.0)]
        run_dir = write_job(self.jobs, "standard-run", "alpha", "standard", trials)
        earlier = trial("u2__s.try-1", "made-up-bench/u2", None)
        earlier.exception = ("ConnectionError", "the relay closed")
        write_trial(os.path.join(run_dir, "retried"), earlier, "alpha", "made-up-model", "standard", 9)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        metrics = load(self.round_file("metrics.json"))
        self.assertEqual([(r["trials"], r["trials_passed"]) for r in metrics["runs"]], [(3, 2)])
        self.classify_ok()
        self.assertEqual(sorted(self.by_trial()), ["u1__s", "u2__s", "u3__s"])
        self.assertEqual(load(self.round_file("groups.json"))["trials"], 3)


class FoundClassConfinement(LoopCase):
    def test_a_rule_that_reaches_outside_the_trial_through_a_link_is_refused(self) -> None:
        run_dir = write_job(self.jobs, "run", "alpha", "harbor",
                            [trial("p__r", "bench/p", 1.0), trial("f__r", "bench/f", 0.0)])
        outside = os.path.join(self.tmp, "outside.txt")
        write(outside, "SYNTHETIC\n")
        os.symlink(outside, os.path.join(run_dir, "f__r", "link"))
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        write_json(os.path.join(self.loop, "found-classes.json"), {"schema": "run-to-fix.found-classes/v1", "rules": [
            {"id": "linked", "meaning": "A file the trial links to.", "file": "link", "regex": "SYNTHETIC"}]})
        self.read_ok(run_dir, None)
        r = run(["classify", "--loop", self.loop])
        self.assertNotEqual(r.code, 0)
        self.assertIn("outside the trial", r.err)
        self.assertFalse(os.path.exists(self.round_file("trials.csv")))


#: the trajectory of the standard path: one step per agent message, with no usage per step
STEPS_WITHOUT_USAGE = json.dumps({"schema_version": "ATIF-v1.6", "steps": [
    {"step_id": i, "source": "agent", "message": "step"} for i in range(31)]})


class StandardModelCalls(LoopCase):
    def test_the_count_of_the_standard_wins_over_the_steps_of_the_trajectory(self) -> None:
        t = trial("s1__s", "made-up-bench/s1", 0.0)
        t.calls = 20
        t.files = {"agent/trajectory.json": STEPS_WITHOUT_USAGE}
        run_dir = write_job(self.jobs, "run", "alpha", "standard", [t, trial("p__s", "made-up-bench/p", 1.0)])
        self.set_adapter(adapter("alpha", {"step_limit": 30}, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        row = self.by_trial()["s1__s"]
        self.assertEqual((row["model_calls"], row["class"]), ("20", "verifier_failed"))
