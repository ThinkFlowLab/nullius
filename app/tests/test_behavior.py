"""Tests for behavioral metrics over a parsed trajectory.

These read the *shape* of a run — was the agent methodical or flailing —
which the outcome alone can't show: two resolved trials can hide very
different amounts of wasted work.
"""

import json
import os
import tempfile
import unittest

from nullius.traces import trajectory
from nullius.traces import behavior
from tests.test_trajectory import two_agent_steps


def _traj(steps):
    data = {"schema_version": "ATIF-v1.7", "session_id": "s", "steps": steps}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "trajectory.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return trajectory.load(path)


def _tool_step(step_id, name, args, ts=None, observation="success=True data={}"):
    call_id = f"c{step_id}"
    step = {
        "step_id": step_id,
        "source": "agent",
        "message": "(tool use)",
        "tool_calls": [{"tool_call_id": call_id, "function_name": name, "arguments": args}],
        "observation": {"results": [{"source_call_id": call_id, "content": observation}]},
    }
    if ts:
        step["timestamp"] = ts
    return step


class Analyze(unittest.TestCase):
    def test_counts_and_first_edit(self):
        tr = _traj(
            [
                {"step_id": 1, "source": "user", "message": "fix",
                 "timestamp": "2026-08-09T22:00:00+00:00"},
                _tool_step(2, "read_file", {"path": "a.py"}, "2026-08-09T22:00:10+00:00"),
                _tool_step(3, "read_file", {"path": "a.py"}, "2026-08-09T22:00:20+00:00"),
                _tool_step(4, "edit_file", {"path": "a.py"}, "2026-08-09T22:00:30+00:00"),
                _tool_step(5, "run_shell", {"command": "pytest"}, "2026-08-09T22:00:40+00:00"),
            ]
        )
        b = behavior.analyze(tr)
        self.assertEqual(b.first_edit_call, 3)          # 3rd call overall
        self.assertEqual(b.time_to_first_edit, 30.0)    # 22:00:00 -> 22:00:30
        self.assertEqual(b.n_redundant_reads, 1)        # a.py read twice
        self.assertEqual(b.edit_test_cycles, 1)         # edit then execute
        self.assertEqual(b.exploration_per_edit, 2.0)   # 2 search/read for 1 edit

    def test_repeated_failed_calls(self):
        fail = "success=False data={} error='boom'"
        tr = _traj(
            [
                _tool_step(1, "run_shell", {"command": "pytest -x"}, observation=fail),
                _tool_step(2, "run_shell", {"command": "pytest -x"}, observation=fail),
                _tool_step(3, "run_shell", {"command": "pytest -x"}, observation=fail),
                _tool_step(4, "run_shell", {"command": "ls"}),
            ]
        )
        b = behavior.analyze(tr)
        # Two re-runs of a command already seen to fail.
        self.assertEqual(b.n_repeated_failed_calls, 2)

    def test_no_clock_no_time_to_first_edit(self):
        tr = _traj([_tool_step(1, "edit_file", {"path": "a.py"})])
        b = behavior.analyze(tr)
        self.assertEqual(b.first_edit_call, 1)
        self.assertIsNone(b.time_to_first_edit)

    def test_empty_trajectory(self):
        tr = _traj([])
        b = behavior.analyze(tr)
        self.assertIsNone(b.first_edit_call)
        self.assertEqual(b.n_redundant_reads, 0)
        self.assertIsNone(b.exploration_per_edit)


class GoldFiles(unittest.TestCase):
    SOLVE = """#!/bin/bash
cat > /testbed/solution_patch.diff << '__SOLUTION__'
diff --git a/sklearn/mixture/base.py b/sklearn/mixture/base.py
--- a/sklearn/mixture/base.py
+++ b/sklearn/mixture/base.py
@@ -1 +1 @@
-x
+y
__SOLUTION__
"""

    def test_gold_files_from_solve_script(self):
        self.assertEqual(behavior.gold_files(self.SOLVE), ["sklearn/mixture/base.py"])

    def test_localization_matches_by_suffix(self):
        # The agent's paths may be container-absolute; the gold diff's are
        # repo-relative. /testbed/sklearn/... must still count as a hit.
        loc = behavior.localization(
            ["/testbed/sklearn/mixture/base.py", "notes.md"],
            ["sklearn/mixture/base.py", "sklearn/mixture/gaussian_mixture.py"],
        )
        self.assertEqual(loc.hit, ["sklearn/mixture/base.py"])
        self.assertEqual(loc.missed, ["sklearn/mixture/gaussian_mixture.py"])
        self.assertEqual(loc.extra, ["notes.md"])


class CategoryIsTheKey(unittest.TestCase):
    """The behavioural numbers are keyed by category, never by tool name.

    Two agents doing the same work under different names must read the same
    on the "How it worked" cards; the fixture is test_trajectory's."""

    def test_same_work_under_other_names_reads_the_same(self):
        steps = two_agent_steps()
        short = behavior.analyze(_traj(steps["short"]))
        verbose = behavior.analyze(_traj(steps["verbose"]))
        self.assertEqual(short, verbose)
        # And it read something: an edit followed by a run, three looks before it.
        self.assertEqual(short.first_edit_call, 4)
        self.assertEqual(short.edit_test_cycles, 1)
        self.assertEqual(short.exploration_per_edit, 3.0)
        self.assertEqual((short.n_search, short.n_read, short.n_edit), (1, 2, 1))

    def test_a_retry_is_the_same_name_and_target_within_one_agent(self):
        # The one place a name is a key: repeating a call that already failed.
        # That compares an agent with itself, so its own vocabulary is right.
        failed = "success=False data={} error='boom'"
        tr = _traj([_tool_step(1, "Run the tests", {"command": "pytest"}, observation=failed),
                    _tool_step(2, "Run the tests", {"command": "pytest"}, observation=failed)])
        self.assertEqual(behavior.analyze(tr).n_repeated_failed_calls, 1)


if __name__ == "__main__":
    unittest.main()
