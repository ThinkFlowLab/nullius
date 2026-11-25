"""A job of another workspace, opened with ``?workspace=``, shows and keeps its
judgements in that workspace's store, not the open one's."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from flask import url_for

from nullius import workspace
from nullius.judge import questions as Q, store

from .test_routes import web

JOB = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "harbor-jobs" / "liveboard-oracle-x4"
TRIAL = "hello-world__mAXMKXR"


class JudgementsOfAnotherWorkspace(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-judge-ws-")))
        shutil.copytree(JOB, tmp / "a" / "job-x")
        shutil.copytree(JOB, tmp / "b" / "job-y")
        stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "workspaces")))
        stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}, clear=True))
        self.a = workspace.create(web.shared.WS_HOME, [str(tmp / "a")], "A")
        self.b = workspace.create(web.shared.WS_HOME, [str(tmp / "b")], "B")
        web.shared.open_workspace(self.a)
        self.url = f"/trial/job-y/{TRIAL}?workspace={self.b.name}"

    def test_the_trial_page_reads_the_judgement_kept_in_its_own_workspace(self):
        store.save(self.b.dir, "job-y", TRIAL, {"questions": Q.VERSION, "answers": {"q": {"choice": "x"}}, "judge": {"model": "m"},
                                               "asked_at": "2026-11-01T00:00:00Z"})
        seen = []
        real = web.traces._judgement_panel
        with patch.object(web.traces, "_judgement_panel", side_effect=lambda *a: seen.append(real(*a)) or seen[-1]):
            self.assertEqual(web.app.test_client().get(self.url).status_code, 200)
        self.assertTrue(seen[0]["judged"], "B's judgement of B's trial is not shown")

    def test_judging_that_trial_writes_into_its_own_workspace(self):
        rec = {"questions": Q.VERSION, "answers": {"q": {"choice": "x"}}, "judge": {"model": "m"}}
        with web.app.test_request_context(self.url):
            job, trial = web.traces._trial_evidence("job-y", TRIAL)
            with patch.object(web.judge.batch, "ask_states", return_value=dict(rec)):
                web.judge.batch.ask_and_store(web.judge.ws_dir(), job.job_id, trial.trial_name, {}, None)
        self.assertNotIn("judgements", os.listdir(self.a.dir), "a judgement of B's trial was written into A")
        self.assertIsNotNone(store.load(self.b.dir, "job-y", TRIAL))

    def test_the_judge_forms_post_back_to_that_workspace(self):
        with web.app.test_request_context(self.url):
            for endpoint, args in (("judge_trial_route", {"trial_name": TRIAL}), ("judge_job_route", {}), ("judge_tasks_route", {})):
                self.assertIn(f"workspace={self.b.name}", url_for(endpoint, job_id="job-y", **args))

    def test_each_compare_column_reads_its_own_workspace(self):
        with web.app.test_request_context(f"/compare?pick={self.a.name}:job-x&pick={self.b.name}:job-y"):
            view = web.compare._comparison_requested()
            self.assertEqual([web.judge.ws_dir(s.workspace) for s in view.selected], [self.a.dir, self.b.dir])

    def test_a_folded_run_s_side_reads_and_keeps_its_own_shard_s_record(self):
        jx, jy = workspace.load_jobs(self.a, self.a.roots)[0], workspace.load_jobs(self.b, self.b.roots)[0]
        fold = replace(jx, trials=jx.trials + jy.trials)                                # as launches.fold makes it: the first shard's
        t = replace(next(t for t in jy.trials if t.trial_name == TRIAL), has_trajectory=True)
        rec = {"questions": Q.VERSION, "answers": {web.judge.SIDE_QUESTION: {"score": 1}}, "judge": {"model": "m"}}
        store.save(self.b.dir, "job-y", TRIAL, dict(rec), started=web.judge.started_of(t))
        self.assertIsNotNone(web.judge.side_of(fold, t, self.b.dir), "the trial page's record is not read")
        with patch.object(web.judge.trajectory, "load", return_value=unittest.mock.Mock(error=None)), \
             patch.object(web.judge, "_states_for", return_value={}), patch.object(web.judge.batch, "ask_states", return_value=dict(rec)), \
             patch.object(web.judge, "side_ids", return_value=["more"]):                # one question still to ask
            web.judge.judge_side(fold, t, self.b.dir, None)
        self.assertIsNone(store.load(self.b.dir, "job-x", TRIAL), "saved under the first shard's job")

    def test_a_job_of_the_open_workspace_keeps_its_store(self):
        with web.app.test_request_context(f"/trial/job-x/{TRIAL}"):
            self.assertEqual(web.judge.ws_dir(), self.a.dir)


if __name__ == "__main__":
    unittest.main()
