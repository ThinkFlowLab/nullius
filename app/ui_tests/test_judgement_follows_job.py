"""A judgement belongs to the job it judges, not to the workspace it was
made in: it shows in every workspace that holds the job, outlives the
workspace, and stays apart from another job of the same name."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nullius import workspace
from nullius.judge import questions as Q

from .test_routes import web

JOB = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "harbor-jobs" / "liveboard-oracle-x4"
TRIAL = "hello-world__mAXMKXR"


def copy_job(dest: Path, started: str = "") -> Path:
    shutil.copytree(JOB, dest)
    (dest / TRIAL / "agent" / "trajectory.json").write_text("{}")
    if started:
        res = json.loads((dest / "result.json").read_text())
        (dest / "result.json").write_text(json.dumps(dict(res, started_at=started)))
        (dest / "events.jsonl").unlink(missing_ok=True)
    return dest


class JudgementFollowsTheJob(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-judge-job-")))
        stack.enter_context(patch.object(web.shared, "WS_HOME", str(self.tmp / "workspaces")))
        stack.enter_context(patch.object(web.judge.store, "home", return_value=str(self.tmp / "data")))
        stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}, clear=True))
        rec = {"questions": Q.VERSION, "answers": {"failure_cause": {"choice": "agent_error"}}, "judge": {"model": "m"},
               "asked_at": "2026-11-01T00:00:00Z"}
        stack.enter_context(patch.object(web.judge, "_trial_ids_and_states", return_value={}))
        stack.enter_context(patch.object(web.judge.trajectory, "load", return_value=SimpleNamespace(error=None)))
        stack.enter_context(patch.object(web.judge.batch, "ask_states", side_effect=lambda *a, **k: dict(rec)))

    def judge(self, job_id, ws=None):
        q = f"?workspace={ws.name}" if ws else ""
        with web.app.test_request_context(f"/trial/{job_id}/{TRIAL}{q}"):
            job, trial = web.traces._trial_evidence(job_id, TRIAL)
            web.judge.judge_trial(job, trial, None)

    def judged(self, job_id, ws=None) -> bool:
        q = f"?workspace={ws.name}" if ws else ""
        with web.app.test_request_context(f"/trial/{job_id}/{TRIAL}{q}"):
            job, trial = web.traces._trial_evidence(job_id, TRIAL)
            return bool(web.judge.judgement_of(job, trial))

    def test_a_judgement_made_in_one_workspace_shows_in_another_and_outlives_it(self):
        copy_job(self.tmp / "runs" / "job-x")
        a = workspace.create(web.shared.WS_HOME, [str(self.tmp / "runs")], "A")
        b = workspace.create(web.shared.WS_HOME, [str(self.tmp / "runs")], "B")
        web.shared.open_workspace(a)
        self.judge("job-x")
        self.assertTrue(self.judged("job-x", b), "a judgement made in A does not show in B")
        workspace.delete(web.shared.WS_HOME, a.name)
        self.assertTrue(self.judged("job-x", b), "deleting A deleted the judgement")
        c = workspace.create(web.shared.WS_HOME, [str(self.tmp / "runs")], "C")
        self.assertTrue(self.judged("job-x", c))

    def test_two_jobs_of_one_name_in_two_roots_keep_their_own_judgements(self):
        copy_job(self.tmp / "one" / "job-x", "2026-01-01T00:00:00Z")
        copy_job(self.tmp / "two" / "job-x", "2026-02-02T00:00:00Z")
        both = workspace.create(web.shared.WS_HOME, [str(self.tmp / "one"), str(self.tmp / "two")], "Both")
        web.shared.open_workspace(both)
        jobs = {Path(j.dir).parent.name: j for j in workspace.load_jobs(both, both.roots)}
        trial = lambda j: next(t for t in j.trials if t.trial_name == TRIAL)
        with web.app.test_request_context(f"/trial/job-x/{TRIAL}"):
            web.judge.judge_trial(jobs["one"], trial(jobs["one"]), None)
            seen = {k: bool(web.judge.judgement_of(j, trial(j))) for k, j in jobs.items()}
        self.assertEqual(seen, {"one": True, "two": False})


if __name__ == "__main__":
    unittest.main()
