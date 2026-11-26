"""A job of another workspace, opened with ``?workspace=``, shows the
judgements that workspace kept for it before, and its new ones go to the
user's store under the job's identity, into neither workspace."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import url_for

from nullius import workspace
from nullius.judge import questions as Q, store
from nullius.runs import failure_analysis as fa

from .test_routes import web

JOB = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "harbor-jobs" / "liveboard-oracle-x4"
TRIAL = "hello-world__mAXMKXR"


class JudgementsOfAnotherWorkspace(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-judge-ws-")))
        self.tmp = tmp
        shutil.copytree(JOB, tmp / "a" / "job-x")
        shutil.copytree(JOB, tmp / "b" / "job-y")
        (tmp / "b" / "job-y" / TRIAL / "agent" / "trajectory.json").write_text("{}")
        stack.enter_context(patch.object(store, "home", return_value=str(tmp / "data")))
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

    def test_judging_that_trial_writes_into_neither_workspace(self):
        rec = {"questions": Q.VERSION, "answers": {"q": {"choice": "x"}}, "judge": {"model": "m"}}
        with web.app.test_request_context(self.url):
            job, trial = web.traces._trial_evidence("job-y", TRIAL)
            with patch.object(web.judge.trajectory, "load", return_value=SimpleNamespace(error=None)), \
                    patch.object(web.judge, "_trial_ids_and_states", return_value={}), \
                    patch.object(web.judge.batch, "ask_states", return_value=dict(rec)), \
                    patch.object(trial, "has_trajectory", True):
                web.judge.judge_trial(job, trial, None)
        for ws in (self.a, self.b):
            self.assertNotIn("judgements", os.listdir(ws.dir), f"a judgement was written into {ws.label}")
        self.assertIsNotNone(store.load(store.home(), job.uid, TRIAL))

    def test_judge_all_and_task_judging_write_into_neither_workspace(self):
        rec = {"questions": Q.VERSION, "answers": {"q": {"choice": "x"}}, "judge": {"model": "m"}}
        client = web.app.test_client()
        with client.session_transaction() as sess:
            sess["csrf"] = "t"
        with patch.dict(os.environ, TYPESAFE_API_KEY="ts-fake"), patch.object(web.judge.batch, "ask_states", side_effect=lambda *a, **k: dict(rec)), \
                patch.object(web.judge.trajectory, "load", lambda path: SimpleNamespace(error=None)), patch.object(web.judge, "_trial_ids_and_states", lambda trial, traj: {}), \
                patch.object(web.judge, "_task_instruction", lambda trial: "Say hello."):
            for route in ("job", "tasks"):
                self.assertEqual(client.post(f"/judge/{route}/job-y?workspace={self.b.name}", data={"csrf": "t"}).status_code, 302)
        for ws in (self.a, self.b):
            self.assertNotIn("judgements", os.listdir(ws.dir), f"Judge all on B's job wrote into {ws.label}")
        for lane in ("judgements", "judgements-tasks"):
            self.assertTrue(any((Path(store.home()) / lane).rglob("*.json")), f"nothing kept in the profile's {lane}")

    def test_the_judge_forms_post_back_to_that_workspace(self):
        with web.app.test_request_context(self.url):
            for endpoint, args in (("judge_trial_route", {"trial_name": TRIAL}), ("judge_job_route", {}), ("judge_tasks_route", {})):
                self.assertIn(f"workspace={self.b.name}", url_for(endpoint, job_id="job-y", **args))

    def test_every_link_to_a_job_s_record_keeps_that_workspace(self):
        with web.app.test_request_context(self.url):
            for rule in web.app.url_map.iter_rules():
                if "job_id" in rule.arguments:
                    with self.subTest(rule.endpoint):
                        self.assertIn(f"workspace={self.b.name}", url_for(rule.endpoint, **dict.fromkeys(rule.arguments, "x")))

    def test_the_pages_of_that_record_link_into_no_other_workspace(self):
        client, kept = web.app.test_client(), f"workspace={self.b.name}"
        page = unescape(client.get(f"/job/job-y?{kept}").get_data(as_text=True))
        self.assertNotIn('href="?', page, "a link of the query alone drops the workspace")
        links = re.findall(r'(?:href|data-results)="(/job/job-y\?[^"]*)"', page)
        for arg in ("sort=tokens", "outcome=", "view=results"):
            self.assertTrue([link for link in links if arg in link and kept in link], f"no {arg} link keeps the workspace")

    def test_a_user_judge_flag_goes_to_the_job_of_that_workspace(self):
        shutil.copytree(JOB, self.tmp / "b" / "job-x")              # B holds a job-x too, as the open workspace does
        client = web.app.test_client()
        with client.session_transaction() as sess:
            sess["csrf"] = "t"
        page = client.get(f"/trial/job-x/{TRIAL}?workspace={self.b.name}").get_data(as_text=True)
        action = unescape(re.search(r'action="(/failure/[^"]+)"', page).group(1))
        saved = client.post(action, data={"csrf": "t", "action": "flag", "kind": "flawed", "category": "c"})
        self.assertEqual(saved.status_code, 302)
        self.assertTrue((self.tmp / "b" / "job-x" / fa.FILE).exists(), "the flag is not in B's job")
        self.assertFalse((self.tmp / "a" / "job-x" / fa.FILE).exists(), "the flag went to the open workspace's job")
        self.assertIn(f"workspace={self.b.name}", saved.headers["Location"])

    def test_each_compare_column_reads_its_own_workspace(self):
        with web.app.test_request_context(f"/compare?pick={self.a.name}:job-x&pick={self.b.name}:job-y"):
            view = web.compare._comparison_requested()
            self.assertEqual([web.judge.ws_dir(s.workspace) for s in view.selected], [self.a.dir, self.b.dir])

    def test_a_folded_run_s_side_reads_and_keeps_its_own_shard_s_record(self):
        jx, jy = workspace.load_jobs(self.a, self.a.roots)[0], workspace.load_jobs(self.b, self.b.roots)[0]
        fold = replace(jx, trials=jx.trials + jy.trials)                                # as launches.fold makes it: the first shard's
        t = replace(next(t for t in jy.trials if t.trial_name == TRIAL), has_trajectory=True)
        rec = {"questions": Q.VERSION, "answers": {web.judge.SIDE_QUESTION: {"score": 1}}, "judge": {"model": "m"}}
        store.save(self.b.dir, "job-y", TRIAL, dict(rec), started=web.judge.started_of(t))   # kept by B before
        with web.app.test_request_context(self.url):
            self.assertIsNotNone(web.judge.side_of(fold, t, self.b.name), "the trial page's record is not read")
            with patch.object(web.judge.trajectory, "load", return_value=unittest.mock.Mock(error=None)), \
                 patch.object(web.judge, "_states_for", return_value={}), patch.object(web.judge.batch, "ask_states", return_value=dict(rec)), \
                 patch.object(web.judge, "side_ids", return_value=["more"]):            # one question still to ask
                web.judge.judge_side(fold, t, self.b.name, None)
        self.assertIsNone(store.load(store.home(), jx.uid, TRIAL), "saved under the first shard's job")
        self.assertIsNotNone(store.load(store.home(), jy.uid, TRIAL), "not saved under its own shard's job")

    def test_a_job_of_the_open_workspace_keeps_its_store(self):
        with web.app.test_request_context(f"/trial/job-x/{TRIAL}"):
            self.assertEqual(web.judge.ws_dir(), self.a.dir)


if __name__ == "__main__":
    unittest.main()
