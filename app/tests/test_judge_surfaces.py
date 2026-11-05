"""The judge's surfaces: three question groups (feasibility triage, behaviour
interpretation, task-instruction quality), each over a small purpose-built
state - a slice, never the whole transcript - with every count written in
by code and an explicit escape option where a list may not cover the input.
Nothing here asks the judge."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from nullius.judge import questions, state
from nullius.traces import behavior, trajectory

KEY = "sk-fixture-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
ASKS_FOR_A_FIGURE = re.compile(r"\b(how many|how long|how often|count|number of|percent|sum|total|before or after|earlier than|later than)\b", re.I)


class Groups(unittest.TestCase):
    def test_the_groups_their_states_and_their_escape_options(self):
        self.assertEqual(questions.group_ids("feasibility"), ["control_failure_cause"])
        self.assertNotIn("behaviour", questions.GROUPS)                                # the trial page's "How it worked" went with its questions
        self.assertEqual(questions.group_ids("task"), ["instruction_quality"])
        self.assertEqual(questions.state_of("control_failure_cause"), "control"); self.assertEqual(questions.state_of("instruction_quality"), "task")
        for q in ("control_failure_cause",):
            self.assertEqual(questions.QUESTIONS[q]["type"], "choice"); self.assertIn("other", questions.QUESTIONS[q]["criteria"], q)
            self.assertIn("outside this list", questions.QUESTIONS[q]["criteria"]["other"])
        self.assertEqual(questions.QUESTIONS["instruction_quality"]["type"], "score"); self.assertEqual(len(questions.QUESTIONS["instruction_quality"]["criteria"]), 4)
        self.assertEqual(sorted(questions.QUESTIONS["control_failure_cause"]["criteria"]), ["bad_environment", "bad_task", "bad_verifier", "control_behaved", "other"])
        # none is a trial-page question, none is a flag, and the API shape is still type/instructions/criteria
        for q in questions.group_ids("feasibility") + questions.group_ids("task"):
            self.assertNotIn(q, questions.for_trial(False)); self.assertFalse(questions.QUESTIONS[q].get("flag"))
            self.assertEqual(sorted(questions.request_questions([q])[q]), ["criteria", "instructions", "type"])

    def test_the_wording_asks_for_a_judgment_never_a_figure_or_a_date(self):
        for q in questions.group_ids("feasibility") + questions.group_ids("task"):
            text = questions.QUESTIONS[q]["instructions"] + " " + json.dumps(questions.QUESTIONS[q]["criteria"])
            self.assertIsNone(ASKS_FOR_A_FIGURE.search(text), q)
        # each names the part of the state it reads, in backticks
        self.assertIn("`[control]`", questions.QUESTIONS["control_failure_cause"]["instructions"])
        self.assertIn("`[instruction]`", questions.QUESTIONS["instruction_quality"]["instructions"])


class States(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-surfaces-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        path = self.tmp / "trajectory.json"
        path.write_text(json.dumps({"schema_version": "ATIF-v1.5", "steps": [
            {"step_id": 1, "source": "user", "message": "Fix the failing test in calc.py."},
            {"step_id": 2, "source": "agent", "message": "(tool use)",
             "tool_calls": [{"tool_call_id": "c1", "function_name": "Bash", "arguments": {"command": "pytest -q"}}],
             "observation": {"results": [{"source_call_id": "c1", "content": "1 failed"}]}},
            {"step_id": 3, "source": "agent", "message": "Fixed the off-by-one in add()."}]}))
        self.traj = trajectory.load(str(path)); self.beh = behavior.analyze(self.traj)

    def test_the_kinds_are_registered_and_reach_build(self):
        for kind in ("control", "task"):
            self.assertIn(kind, state.BUILDERS)
        self.assertNotIn("behaviour", state.BUILDERS)                     # no page asks the behaviour group any more
        with self.assertRaises(ValueError):
            state.register("task", state.task_state)


    def test_the_task_state_is_the_instruction_alone(self):
        text, truncated = state.build("task", self.traj)
        self.assertFalse(truncated); self.assertEqual(text, "[instruction]\n" + state.instruction_of(self.traj))
        self.assertNotIn("[tool ", text); self.assertNotIn("[final message]", text)
        text, _ = state.build("task", None, instruction="Do the thing. API_KEY=" + KEY)
        self.assertEqual(text, "[instruction]\nDo the thing. API_KEY=[REDACTED]")


if __name__ == "__main__":
    unittest.main()
