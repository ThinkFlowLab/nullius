"""The feasibility report: both control outcomes per task, and a class that
never validates a task on one side alone.

Synthetic control jobs are written the way Harbor writes them — a job
directory with config.json and one trial directory per task holding a
result.json — under the names the builder gives them (`<name>-nop`,
`<name>-oracle`, `<name>-<agent>`, and the bundle's `-bracket-` spelling).
The one real capture, `liveboard-oracle-x4`, is an oracle-only sweep and is
read as exactly that."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from nullius.runs import feasibility, parse

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh)


def swe_report(resolved=False, f2p=((), ()), p2p=((), ()), patch=True):
    """One task's block of SWE-bench's `verifier/report.json`, the shape the
    reader documents: `f2p` and `p2p` are (passing, failing) name lists.
    `patch=False` is what the nop control leaves — no patch at all."""
    return {
        "resolved": resolved, "patch_exists": patch, "patch_is_None": not patch, "patch_successfully_applied": patch,
        "tests_status": {
            "FAIL_TO_PASS": {"success": list(f2p[0]), "failure": list(f2p[1])},
            "PASS_TO_PASS": {"success": list(p2p[0]), "failure": list(p2p[1])},
            "FAIL_TO_FAIL": {"success": [], "failure": []},
            "PASS_TO_FAIL": {"success": [], "failure": []},
        },
    }


def write_job(root, job_id, agent, trials, dataset="../datasets/tb-small", n_tasks=0):
    """One Harbor-shaped job. `trials` maps task name to a reward (the
    verifier's answer), an exception type (a failure before the verifier),
    None (no verifier record at all), or a `swe_report` block — a SWE-bench
    trial, whose verifier names the tests it ran."""
    d = os.path.join(root, job_id)
    _write_json(os.path.join(d, "config.json"), {
        "job_name": job_id, "agents": [{"name": agent, "model_name": "" if agent in ("nop", "oracle") else "openai/x"}],
        "datasets": [{"path": dataset, **({"n_tasks": n_tasks} if n_tasks else {})}],
    })
    _write_json(os.path.join(d, "result.json"), {"stats": {"n_completed_trials": len(trials)}})
    for task, said in trials.items():
        res = {"task_name": f"tb-small/{task}", "trial_name": f"{task}__x", "source": "terminal-bench",
               "agent_info": {"name": agent}, "agent_result": {"n_input_tokens": 0, "n_output_tokens": 0}}
        if isinstance(said, str):
            res["exception_info"] = {"exception_type": said, "exception_message": "boom"}
        elif isinstance(said, dict):
            res.update(task_name=f"swe-bench/{task}", source="swe-bench")
            res["verifier_result"] = {"rewards": {"reward": 1.0 if said.get("resolved") else 0.0}}
            _write_json(os.path.join(d, f"{task}__x", "verifier", "report.json"), {task: said})
        elif said is not None:
            res["verifier_result"] = {"rewards": {"reward": said}}
        _write_json(os.path.join(d, f"{task}__x", "result.json"), res)
        if isinstance(said, (int, float)):
            # the verifier's own marker: without it the reward is not consulted and the trial is unscored
            os.makedirs(os.path.join(d, f"{task}__x", "verifier"), exist_ok=True)
            with open(os.path.join(d, f"{task}__x", "verifier", "reward.txt"), "w", encoding="utf-8") as fh:
                fh.write(str(said))
    return parse.parse_job(d)


class Classification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def sweep(self, nop, oracle, agent=None, prefix="sweep", bracket=False, n_tasks=0):
        infix = "-bracket-" if bracket else "-"
        jobs = [write_job(self.root, f"{prefix}{infix}nop", "nop", nop, n_tasks=n_tasks),
                write_job(self.root, f"{prefix}{infix}oracle", "oracle", oracle, n_tasks=n_tasks)]
        if agent is not None:
            jobs.append(write_job(self.root, f"{prefix}-agent", "my-agent", agent, n_tasks=n_tasks))
        return jobs

    def test_every_class_from_the_pair_of_outcomes(self):
        nop = {"good": 0, "leaky": 1, "broken-ref": 0, "env": "EnvironmentSetupError", "half": 0, "blank": None}
        oracle = {"good": 1, "leaky": 1, "broken-ref": 0, "env": 1, "blank": 1, "oracle-only": 1}
        jobs = self.sweep(nop, oracle)
        rep = feasibility.report(jobs[0], jobs)
        by = {r.task_base: r for r in rep.rows}
        self.assertEqual(by["good"].verdict, feasibility.FEASIBLE)
        self.assertEqual(by["leaky"].verdict, feasibility.UNEXPECTED_REWARD)        # nop accepted: the verifier rejects nothing
        self.assertEqual(by["broken-ref"].verdict, feasibility.REFERENCE_REJECTED)
        self.assertEqual(by["env"].verdict, feasibility.SETUP_FAILURE)              # nop failed before the verifier, oracle passed: not validated
        self.assertEqual(by["half"].verdict, feasibility.MISSING)                   # oracle never ran this task
        self.assertEqual(by["blank"].verdict, feasibility.MISSING)                  # nop recorded no verifier answer
        self.assertEqual(by["oracle-only"].verdict, feasibility.MISSING)
        self.assertEqual([r.task_base for r in rep.rows if r.validated], ["good"])
        self.assertEqual(rep.counts, {"feasible": 1, "unexpected_reward": 1, "task_broken": 0, "reference_rejected": 1, "setup_failure": 1, "missing": 3})
        # both outcomes are kept on every row, as recorded
        self.assertEqual((by["env"].nop.label, by["env"].nop.exception_type, by["env"].oracle.label), ("Setup error", "EnvironmentSetupError", "Resolved"))
        self.assertEqual((by["half"].nop.present, by["half"].oracle.present), (True, False))
        self.assertEqual(by["half"].oracle.label, "not run")
        self.assertEqual(by["env"].why, "nop: EnvironmentSetupError")
        self.assertEqual(by["half"].why, "oracle not run")
        self.assertEqual(by["blank"].why, "nop unscored")

    def test_a_task_is_never_validated_on_one_side(self):
        # every pairing where a side is absent, unscored or failed — with the other side as right as it can be
        for nop, oracle in [({}, {"t": 1}), ({"t": 0}, {}), ({"t": None}, {"t": 1}), ({"t": 0}, {"t": None}),
                            ({"t": "ApiRateLimitError"}, {"t": 1}), ({"t": 0}, {"t": "EnvironmentSetupError"})]:
            with self.subTest(nop=nop, oracle=oracle):
                jobs = self.sweep(nop, oracle, prefix="one-%d" % id(nop))
                rep = feasibility.report(jobs[0], jobs)
                self.assertEqual(len(rep.rows), 1)
                self.assertFalse(rep.rows[0].validated)
                self.assertIn(rep.rows[0].verdict, (feasibility.MISSING, feasibility.SETUP_FAILURE))

    def test_an_unexpected_reward_outranks_everything_else(self):
        jobs = self.sweep({"t": 1}, {"t": "EnvironmentSetupError"})
        self.assertEqual(feasibility.report(jobs[0], jobs).rows[0].verdict, feasibility.UNEXPECTED_REWARD)
        jobs = self.sweep({"u": 1}, {}, prefix="alone")
        self.assertEqual(feasibility.report(jobs[0], jobs).rows[0].verdict, feasibility.UNEXPECTED_REWARD)

    def test_the_agent_job_finds_its_controls_and_keeps_its_own_tasks(self):
        jobs = self.sweep({"a": 0, "b": 0}, {"a": 1, "b": 1}, agent={"a": 1, "b": 0, "c": 0}, n_tasks=3)
        agent = jobs[2]
        self.assertEqual(sorted(feasibility.related(agent, jobs)), ["nop", "oracle"])
        rep = feasibility.report(agent, jobs)
        self.assertEqual([c.summary for c in rep.executed], ["nop · sweep-nop · 2 trials of 3 planned", "oracle · sweep-oracle · 2 trials of 3 planned"])
        self.assertEqual(rep.not_run, [])
        self.assertEqual(rep.scope, "3 tasks of tb-small (--n-tasks 3)")
        self.assertEqual([(r.task_base, r.verdict) for r in rep.rows],
                         [("a", feasibility.FEASIBLE), ("b", feasibility.FEASIBLE), ("c", feasibility.MISSING)])   # the agent ran c; no control did

    def test_the_bundles_bracket_spelling_is_found_too(self):
        jobs = self.sweep({"a": 0}, {"a": 1}, agent={"a": 0}, bracket=True)
        rep = feasibility.report(jobs[2], jobs)
        self.assertEqual([c.job_id for c in rep.executed], ["sweep-bracket-nop", "sweep-bracket-oracle"])
        self.assertEqual(rep.rows[0].verdict, feasibility.FEASIBLE)
        # and a control job finds its sibling in the same spelling
        self.assertEqual(sorted(feasibility.related(jobs[0], jobs)), ["nop", "oracle"])

    def test_a_control_over_another_dataset_is_not_this_jobs(self):
        nop = write_job(self.root, "x-nop", "nop", {"a": 0}, dataset="../datasets/other")
        oracle = write_job(self.root, "x-oracle", "oracle", {"a": 1})
        rep = feasibility.report(oracle, [nop, oracle])
        self.assertEqual([c.control for c in rep.executed], ["oracle"])
        self.assertEqual(rep.not_run, ["nop"])
        self.assertTrue(rep.caveat.startswith("Only oracle ran"))

    def test_the_full_scope_is_said_when_no_limit_was_recorded(self):
        jobs = self.sweep({"a": 0}, {"a": 1})
        self.assertEqual(feasibility.report(jobs[0], jobs).scope, "every task of tb-small (no task limit recorded)")


class TaskBroken(unittest.TestCase):
    """A task whose nop control fails previously-passing tests was
    broken before any agent touched it — the same `p2p_fail` the trial page
    flags as ⚠ Broke tests, read on the control. The specimen is written
    here in the SWE-bench verifier's own shape, since no capture on disk has
    a failing nop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def sweep(self, nop, oracle, prefix="swe"):
        return [write_job(self.root, f"{prefix}-nop", "nop", nop, dataset="../datasets/swebench-verified"),
                write_job(self.root, f"{prefix}-oracle", "oracle", oracle, dataset="../datasets/swebench-verified")]

    def test_nop_failing_held_tests_is_task_broken_and_names_them(self):
        held = (["tests/test_a.py::test_ok"], ["tests/test_b.py::test_flaky", "tests/test_c.py::test_gone"])
        nop = {"already-broken": swe_report(p2p=held, f2p=([], ["tests/test_fix.py::test_it"]), patch=False),
               "sound": swe_report(p2p=(["tests/test_a.py::test_ok"], []), f2p=([], ["tests/test_fix.py::test_it"]), patch=False)}
        oracle = {"already-broken": swe_report(resolved=True, p2p=(["tests/test_a.py::test_ok"], []), f2p=(["tests/test_fix.py::test_it"], [])),
                  "sound": swe_report(resolved=True, p2p=(["tests/test_a.py::test_ok"], []), f2p=(["tests/test_fix.py::test_it"], []))}
        jobs = self.sweep(nop, oracle)
        # the specimen reads as the trial page would read it: rejected, and a regression
        nop_trial = next(t for t in jobs[0].trials if t.task_base == "already-broken")
        self.assertEqual((nop_trial.outcome, nop_trial.is_regression, nop_trial.p2p_fail), ("no_patch", True, 2))
        rep = feasibility.report(jobs[0], jobs)
        by = {r.task_base: r for r in rep.rows}
        broken, sound = by["already-broken"], by["sound"]
        self.assertEqual(broken.verdict, feasibility.TASK_BROKEN)
        self.assertFalse(broken.validated)                                      # never feasible, though oracle resolved
        self.assertEqual(broken.oracle.label, "Resolved")
        self.assertTrue(broken.nop.broke_tests)
        self.assertEqual(broken.nop.p2p_failures, ["tests/test_b.py::test_flaky", "tests/test_c.py::test_gone"])
        self.assertEqual(broken.why, "nop broke 2 passing tests: tests/test_b.py::test_flaky, tests/test_c.py::test_gone")
        self.assertEqual(broken.label, "Task broken")
        # the same verifier, the same rejection, no held failure: feasible as before
        self.assertEqual((sound.verdict, sound.nop.broke_tests, sound.nop.p2p_fail), (feasibility.FEASIBLE, False, 0))
        self.assertEqual(rep.counts["task_broken"], 1)
        self.assertEqual(rep.n_feasible, 1)

    def test_task_broken_is_decided_by_nop_alone_after_unexpected_reward(self):
        nop_broken = swe_report(p2p=(["t1"], ["t2"]), patch=False)
        for oracle in [{}, {"t": None}, {"t": "EnvironmentSetupError"}, {"t": swe_report(resolved=False, p2p=(["t1"], ["t2"]))}]:
            with self.subTest(oracle=oracle):
                jobs = self.sweep({"t": nop_broken}, oracle, prefix="alone-%d" % id(oracle))
                row = feasibility.report(jobs[0], jobs).rows[0]
                self.assertEqual(row.verdict, feasibility.TASK_BROKEN)     # not missing, setup_failure or reference_rejected
                self.assertFalse(row.validated)
        # but a nop the verifier accepted is an unexpected reward first, whatever else it says
        leaky = swe_report(resolved=True, p2p=(["t1"], []), patch=False)
        jobs = self.sweep({"t": leaky}, {"t": swe_report(resolved=True)}, prefix="leaky")
        self.assertEqual(feasibility.report(jobs[0], jobs).rows[0].verdict, feasibility.UNEXPECTED_REWARD)

    def test_a_benchmark_without_held_checks_never_produces_the_class(self):
        # Terminal-Bench reports no held checks: a rejected nop there is a rejection, nothing more
        jobs = [write_job(self.root, "tb-nop", "nop", {"t": 0}), write_job(self.root, "tb-oracle", "oracle", {"t": 1})]
        row = feasibility.report(jobs[0], jobs).rows[0]
        self.assertEqual((row.verdict, row.nop.broke_tests, row.nop.p2p_fail, row.nop.p2p_failures), (feasibility.FEASIBLE, False, 0, []))

    def test_the_agents_regression_is_not_the_tasks(self):
        # the agent's patch broke a held test; the controls are clean — that is ⚠ Broke tests on the trial, not task_broken here
        nop = {"t": swe_report(p2p=(["t1"], []), f2p=([], ["f1"]), patch=False)}
        oracle = {"t": swe_report(resolved=True, p2p=(["t1"], []), f2p=(["f1"], []))}
        jobs = self.sweep(nop, oracle)
        jobs.append(write_job(self.root, "swe-agent", "my-agent", {"t": swe_report(p2p=([], ["t1"]), f2p=(["f1"], []))}, dataset="../datasets/swebench-verified"))
        self.assertTrue(jobs[2].trials[0].is_regression)
        row = feasibility.report(jobs[2], jobs).rows[0]
        self.assertEqual((row.verdict, row.nop.broke_tests), (feasibility.FEASIBLE, False))


class RealOracleSweep(unittest.TestCase):
    """Harbor's real liveboard-oracle-x4 tree is an oracle-only sweep: four
    resolved trials, no nop anywhere. Read as oracle-only evidence — every
    task missing its nop side, none validated, the caveat stated."""

    def test_oracle_only_validates_nothing_and_says_so(self):
        job = parse.parse_job(os.path.join(FIXTURES, "harbor-jobs", "liveboard-oracle-x4"))
        rep = feasibility.report(job, [job])
        self.assertEqual([c.summary for c in rep.executed], ["oracle · liveboard-oracle-x4 · 4 trials"])
        self.assertEqual(rep.not_run, ["nop"])
        self.assertEqual(rep.caveat, "Only oracle ran: this cannot establish that the verifier rejects an empty solution. No task is validated.")
        self.assertEqual(rep.scope, "every task of oracle-x4 (no task limit recorded)")
        self.assertEqual(len(rep.rows), 4)
        for r in rep.rows:
            self.assertEqual((r.oracle.label, r.nop.label, r.verdict, r.validated), ("Resolved", "not run", feasibility.MISSING, False))
        self.assertEqual(rep.n_feasible, 0)


class Vocabulary(unittest.TestCase):
    def test_every_class_has_a_label_and_a_description(self):
        self.assertEqual(set(feasibility.ORDER), set(feasibility.LABELS))
        self.assertEqual(set(feasibility.ORDER), set(feasibility.DESCRIPTIONS))


if __name__ == "__main__":
    unittest.main()
