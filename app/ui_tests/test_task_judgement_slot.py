"""A task instruction's score belongs to the instruction it scored: a
changed instruction, or another dataset of the same folder name, is
judged on its own."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from nullius import workspace
from nullius.judge import questions as Q, store

from .test_routes import web

SCORED = {"questions": Q.VERSION, "judge": {"model": "m"},
          "answers": {"instruction_quality": {"score": 2, "legend": ["bad", "ok", "good"]}}}


class TaskSlot(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-task-slot-")))
        stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "workspaces")))
        stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}, clear=True))
        web.shared.open_workspace(workspace.create(web.shared.WS_HOME, [], "w"))
        self.instruction = "Fix the bug in parse.py."
        stack.enter_context(patch.object(web.judge, "_task_instruction", lambda t: self.instruction))
        stack.enter_context(patch.object(web.judge.batch, "ask_states", return_value=dict(SCORED)))

    def job(self, job_id, dataset, path=""):
        trial = NS(task_base="fix-bug", has_trajectory=True)
        return NS(job_id=job_id, dataset=dataset, dataset_path=path, trials=[trial]), trial

    def test_a_changed_instruction_is_judged_again(self):
        job, trial = self.job("run-1", "tasks")
        web.judge.judge_task(job, trial, None)
        self.instruction = "Fix the bug in render.py, and add a test."      # the task was edited, same name
        self.assertEqual(web.judge.tasks_todo(job), [trial],
                         "still scored: " + str(web.judge.task_quality_of(job, trial)))

    def test_two_datasets_with_one_folder_name_are_judged_apart(self):
        a, ta = self.job("bench-a", "tasks", "/x/bench-a/tasks")      # parse.py keeps the basename as the dataset
        b, tb = self.job("bench-b", "tasks", "/y/bench-b/tasks")
        web.judge.judge_task(a, ta, None)
        self.assertIsNone(web.judge.task_quality_of(b, tb), "bench-b shows bench-a's score, slot " + web.judge.task_key(b))
        again, _ = self.job("run-2", "tasks", "/x/bench-a/tasks")
        self.assertEqual(web.judge.task_quality_of(again, ta)["score"], 2)    # another run of bench-a: judged once for both

    def test_two_jobs_of_no_dataset_are_judged_apart(self):
        a, ta = self.job("run-1", "")
        b, tb = self.job("run-2", "")
        web.judge.judge_task(a, ta, None)
        self.assertIsNone(web.judge.task_quality_of(b, tb))

    def test_a_score_kept_without_its_instruction_is_not_shown(self):
        job, trial = self.job("run-1", "tasks", "/x/bench-a/tasks")
        store.save(web.judge.ws_dir(), web.judge.task_key(job), "fix-bug", dict(SCORED))     # as kept before the digest
        self.assertIsNone(web.judge.task_quality_of(job, trial))
        self.assertEqual(web.judge.tasks_todo(job), [trial])


if __name__ == "__main__":
    unittest.main()
