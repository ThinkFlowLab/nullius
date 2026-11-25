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


def tree(root: Path) -> dict:
    """Every file under *root* and its bytes, and every folder."""
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None for p in sorted(root.rglob("*"))} if root.exists() else {}


class JudgementsKeptBefore(unittest.TestCase):
    """Records a workspace kept before are shown by a read that writes
    nothing, and copied into the user's store only by the move."""
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-judge-move-")))
        stack.enter_context(patch.object(web.shared, "WS_HOME", str(self.tmp / "workspaces")))
        stack.enter_context(patch.object(web.judge.store, "home", return_value=str(self.tmp / "data")))
        stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}, clear=True))
        copy_job(self.tmp / "runs" / "job-x")
        self.ws = workspace.create(web.shared.WS_HOME, [str(self.tmp / "runs")], "A")
        old = Path(self.ws.dir) / "judgements" / "job-x"
        old.mkdir(parents=True)
        (old / f"{TRIAL}.json").write_text(json.dumps({Q.VERSION: {
            "questions": Q.VERSION, "answers": {"failure_cause": {"choice": "agent_error"}}, "judge": {"model": "m"},
            "asked_at": "2026-11-01T00:00:00Z"}}))
        web.shared.open_workspace(self.ws)
        self.client = web.app.test_client()

    def get(self, url):
        r = self.client.get(url, follow_redirects=True); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200, url)
        return r

    def token(self):
        self.get("/settings")
        with self.client.session_transaction() as s:
            return s["csrf"]

    def shown(self) -> bool:
        with web.app.test_request_context(f"/trial/job-x/{TRIAL}"):
            job, trial = web.traces._trial_evidence("job-x", TRIAL)
            return bool(web.judge.judgement_of(job, trial))

    def test_a_job_page_shows_them_and_writes_nothing(self):
        self.get("/")                                                    # the workspace parsed, as any page leaves it
        before = tree(self.tmp / "data"), tree(Path(self.ws.dir) / "judgements")
        page = self.get("/job/job-x").get_data(as_text=True)
        self.get(f"/trial/job-x/{TRIAL}")
        self.assertEqual((tree(self.tmp / "data"), tree(Path(self.ws.dir) / "judgements")), before)
        self.assertTrue(self.shown())
        self.assertIn("judgements-move.js", page)

    def test_the_move_copies_them_once_and_says_so_where_it_is_offered(self):
        self.assertEqual(self.get("/judgements/move").get_json(), {"workspaces": [{"label": "A", "n": 1}]})
        self.assertIn("data-move-judgements", self.get("/settings").get_data(as_text=True))
        self.assertEqual(self.client.post("/judgements/move", data={"csrf": "stale"}).status_code, 403)
        self.assertFalse((self.tmp / "data").exists())
        old = tree(Path(self.ws.dir) / "judgements")
        for _ in range(2):                                               # twice: the second changes nothing
            r = self.client.post("/judgements/move", headers={"X-CSRF-Token": self.token()}); self.addCleanup(r.close)
            self.assertEqual(r.get_json(), {"ok": True})
            if not _:
                moved = tree(self.tmp / "data")
        self.assertEqual(tree(self.tmp / "data"), moved)
        self.assertEqual(tree(Path(self.ws.dir) / "judgements"), old)    # the old files stay as they were
        self.assertTrue(self.shown())
        self.assertEqual(self.get("/judgements/move").get_json(), {"workspaces": []})
        self.assertNotIn("data-move-judgements", self.get("/settings").get_data(as_text=True))
        self.assertNotIn("judgements-move.js", self.get("/").get_data(as_text=True))

    def test_judging_a_job_checks_the_form_token_first(self):
        r = self.client.post("/judge/job/no-such-job", data={"csrf": "stale"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 403)

    def test_a_workspace_with_a_root_that_cannot_be_read_is_not_deleted(self):
        shutil.rmtree(self.tmp / "runs")
        r = self.client.post(f"/workspaces/delete/{self.ws.name}", data={"csrf": self.token()}); self.addCleanup(r.close)
        self.assertTrue(Path(self.ws.dir, "judgements", "job-x", f"{TRIAL}.json").is_file())
        self.assertIsNotNone(web.shared.STATE["workspace"])                  # left open, as it was
        self.assertIn("cannot be read", self.get("/workspaces").get_data(as_text=True))


class EachTryItsOwn(unittest.TestCase):
    """A retried trial's earlier try is judged, and shown, apart from its last
    try, even when the try recorded no start."""
    def test_two_tries_each_show_and_keep_their_own_record(self):
        from nullius.runs import parse
        tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-tries-")); self.addCleanup(shutil.rmtree, tmp, True)
        d = copy_job(tmp / "runs" / "job-x")
        res = json.loads((d / TRIAL / "result.json").read_text())
        (d / TRIAL / "result.json").write_text(json.dumps(dict(res, standard={"retries": 1})))
        shutil.copytree(d / TRIAL, d / "retried" / f"{TRIAL}.try-1")
        (d / "retried" / f"{TRIAL}.try-1" / "result.json").write_text(json.dumps({k: v for k, v in res.items() if k != "started_at"}))
        job = parse.parse_job(str(d))
        (n1, first), (n2, last) = parse.tries(next(t for t in job.trials if t.trial_name == TRIAL))
        self.assertEqual((n1, n2), (1, 2))
        self.assertIsNone(first.started_at)
        asked = lambda choice: lambda *a, **k: {"questions": Q.VERSION, "answers": {"failure_cause": {"choice": choice}}}
        with patch.object(web.judge.store, "home", return_value=str(tmp / "data")), \
                patch.object(web.judge.trajectory, "load", return_value=SimpleNamespace(error=None)), \
                patch.object(web.judge, "_trial_ids_and_states", return_value={}), \
                web.app.test_request_context("/"):
            for trial, choice in ((last, "last"), (first, "first"), (last, "last")):
                with patch.object(web.judge.batch, "ask_states", asked(choice)):
                    web.judge.judge_trial(job, trial, "k", calibration={})
            shown = [web.judge.judgement_of(job, t)["answers"]["failure_cause"]["choice"] for t in (first, last)]
        self.assertEqual(shown, ["first", "last"])

    def test_a_try_judged_while_it_was_the_last_keeps_its_record_once_moved_aside(self):
        from nullius.runs import parse
        tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-tries-")); self.addCleanup(shutil.rmtree, tmp, True)
        d = copy_job(tmp / "runs" / "job-x")
        asked = lambda *a, **k: {"questions": Q.VERSION, "answers": {"failure_cause": {"choice": "first"}}}
        with patch.object(web.judge.store, "home", return_value=str(tmp / "data")), \
                patch.object(web.judge.trajectory, "load", return_value=SimpleNamespace(error=None)), \
                patch.object(web.judge, "_trial_ids_and_states", return_value={}), \
                patch.object(web.judge.batch, "ask_states", asked), web.app.test_request_context("/"):
            job = parse.parse_job(str(d))
            web.judge.judge_trial(job, next(t for t in job.trials if t.trial_name == TRIAL), "k", calibration={})
            (d / "retried").mkdir()
            shutil.move(d / TRIAL, d / "retried" / f"{TRIAL}.try-1")                # the runner tries the attempt again
            shutil.copytree(d / "retried" / f"{TRIAL}.try-1", d / TRIAL)
            res = json.loads((d / TRIAL / "result.json").read_text())
            (d / TRIAL / "result.json").write_text(json.dumps(dict(res, started_at="2026-12-01T00:00:00Z", standard={"retries": 1})))
            job = parse.parse_job(str(d))
            (_, first), (_, last) = parse.tries(next(t for t in job.trials if t.trial_name == TRIAL))
            web.judge.store.save(str(tmp / "data"), job.uid, TRIAL, {"questions": Q.VERSION, "answers": {"q": 1}}, lane="llm")
            self.assertEqual(web.judge.judgement_of(job, first)["answers"]["failure_cause"]["choice"], "first")
            self.assertIsNone(web.judge.judgement_of(job, last))
            self.assertEqual([web.judge.judgement_of(job, t, lane="llm") for t in (first, last)], [None, None])   # no start: whose is unknown


if __name__ == "__main__":
    unittest.main()
