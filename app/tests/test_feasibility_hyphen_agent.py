"""An agent job named `<recipe>-<agent>` finds its recipe's nop and oracle
jobs, whatever hyphens the agent's name holds."""
from __future__ import annotations

import tempfile
import unittest

from nullius.runs import feasibility

from .test_feasibility import write_job


class HyphenatedAgentNames(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_each_agent_job_of_one_launch_finds_the_same_controls(self):
        base = "run-0000-terminal-bench"
        jobs = [write_job(self.root, f"{base}-nop", "nop", {"a": 0}),
                write_job(self.root, f"{base}-oracle", "oracle", {"a": 1})]
        for agent in ("codex", "claude-code", "terminus-2", "mini-swe-agent"):
            jobs.append(write_job(self.root, f"{base}-{agent}", agent, {"a": 1}))
        for job in jobs[2:]:
            with self.subTest(job=job.job_id):
                self.assertEqual(sorted(feasibility.related(job, jobs)), ["nop", "oracle"])
                self.assertEqual([c.job_id for c in feasibility.report(job, jobs).executed],
                                 [f"{base}-nop", f"{base}-oracle"])

    def test_a_standard_job_whose_word_holds_a_version_finds_its_controls(self):
        jobs = [write_job(self.root, "std-nop", "nop", {"a": 0}),
                write_job(self.root, "std-oracle", "oracle", {"a": 1}),
                write_job(self.root, "std-codex-2.2.2", "codex", {"a": 1})]
        self.assertEqual({c: j.job_id for c, j in feasibility.related(jobs[2], jobs).items()},
                         {"nop": "std-nop", "oracle": "std-oracle"})

    def test_a_job_whose_agent_is_not_its_suffix_keeps_todays_rule(self):
        jobs = [write_job(self.root, "x-nop", "nop", {"a": 0}),
                write_job(self.root, "x-oracle", "oracle", {"a": 1}),
                write_job(self.root, "x-mine", "claude-code", {"a": 1})]
        self.assertEqual(sorted(feasibility.related(jobs[2], jobs)), ["nop", "oracle"])

    def test_a_control_todays_rule_finds_is_kept_over_the_agents(self):
        jobs = [write_job(self.root, "y-nop", "nop", {"a": 0}),
                write_job(self.root, "y-claude-nop", "nop", {"a": 0}),
                write_job(self.root, "y-oracle", "oracle", {"a": 1}),
                write_job(self.root, "y-claude-code", "claude-code", {"a": 1})]
        self.assertEqual({c: j.job_id for c, j in feasibility.related(jobs[3], jobs).items()},
                         {"nop": "y-claude-nop", "oracle": "y-oracle"})

    def test_a_job_without_an_agent_suffix_finds_nothing_new(self):
        jobs = [write_job(self.root, "z-nop", "nop", {"a": 0}),
                write_job(self.root, "solo", "claude-code", {"a": 1}),
                write_job(self.root, "w-other", "codex", {"a": 1})]
        self.assertEqual(feasibility.related(jobs[1], jobs), {})
        self.assertEqual(feasibility.related(jobs[2], jobs), {})


if __name__ == "__main__":
    unittest.main()
