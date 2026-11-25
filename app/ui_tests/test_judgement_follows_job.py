"""A judgement belongs to the job it judges, not to the workspace it was
made in: it shows in every workspace that holds the job, outlives the
workspace, and stays apart from another job of the same name."""
from __future__ import annotations

import json
import os
from dataclasses import replace
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nullius import workspace
from nullius.judge import questions as Q
from nullius.runs import parse

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

    def test_a_batch_copies_the_older_records_of_the_workspace_asked_and_a_folded_run_s_side_its_own_shard_s(self):
        copy_job(self.tmp / "ra" / "job-x", "2026-01-01T00:00:00Z"); copy_job(self.tmp / "rb" / "job-x", "2026-02-02T00:00:00Z")
        a = workspace.create(web.shared.WS_HOME, [str(self.tmp / "ra")], "A")
        b = workspace.create(web.shared.WS_HOME, [str(self.tmp / "rb")], "B")
        old = Path(b.dir) / "judgements" / "job-x"; old.mkdir(parents=True)
        (old / f"{TRIAL}.json").write_text(json.dumps({"retired": {"questions": "retired", "answers": {"kept_in_b": {"choice": "x"}}}}))
        web.shared.open_workspace(a)                                                    # a version no longer read: the trial is still to judge
        jb = workspace.load_jobs(b, b.roots)[0]
        with patch.object(web.shared, "_local_client", return_value=True), patch.object(web.shared, "_require_csrf"):
            web.app.test_client().post(f"/judge/job/job-x?workspace={b.name}", data={"lane": "decision"})
        saved = json.loads((self.tmp / "data" / "judgements" / jb.uid / f"{TRIAL}.json").read_text())
        self.assertIn("retired", saved)                                                 # B's, though its threads have no request
        with self.subTest("a folded run"):
            copy_job(self.tmp / "ra" / "job-y", "2026-03-03T00:00:00Z")
            jx, jy = sorted(workspace.load_jobs(a, a.roots), key=lambda j: j.job_id)
            fold = replace(jx, trials=jx.trials + jy.trials)                            # as launches.fold makes it: the first shard's
            t = next(t for t in jy.trials if t.trial_name == TRIAL)
            view = SimpleNamespace(jobs=[fold, jb], selected=[SimpleNamespace(workspace=""), SimpleNamespace(workspace=b.name)],
                                   tasks=[SimpleNamespace(task="hello", trials=[t, next(t for t in jb.trials if t.trial_name == TRIAL)])])
            with web.app.test_request_context("/compare"), patch.object(web.judge, "_states_for", return_value={}):
                for _, job, trial, column in web.judge._sides_todo(view)[:1]:
                    web.judge.judge_side(job, trial, column, None)
            self.assertTrue((self.tmp / "data" / "judgements" / jy.uid / f"{TRIAL}.json").is_file())
            self.assertFalse((self.tmp / "data" / "judgements" / jx.uid).exists())

    def test_a_record_kept_before_that_two_jobs_of_one_name_may_own_is_copied_by_neither_save(self):
        copy_job(self.tmp / "one" / "job-x", "2026-01-01T00:00:00Z")
        copy_job(self.tmp / "two" / "job-x", "2026-02-02T00:00:00Z")
        both = workspace.create(web.shared.WS_HOME, [str(self.tmp / "one"), str(self.tmp / "two")], "Both")
        web.shared.open_workspace(both)
        old = Path(both.dir) / "judgements" / "job-x"; old.mkdir(parents=True)
        (old / f"{TRIAL}.json").write_text(json.dumps({Q.VERSION: {"questions": Q.VERSION, "answers": {"kept": {"choice": "x"}},
                                                                   "asked_at": "2026-10-01T00:00:00Z"}}))
        for j in workspace.load_jobs(both, both.roots):
            t = next(t for t in j.trials if t.trial_name == TRIAL)
            with web.app.test_request_context(f"/trial/job-x/{TRIAL}"):
                web.judge.judge_trial(j, t, None)
                self.assertTrue(web.judge.old_of(j))                                    # still read on both
            saved = json.loads((self.tmp / "data" / "judgements" / j.uid / f"{TRIAL}.json").read_text())
            self.assertEqual({k: sorted(r["answers"]) if isinstance(r, dict) else r for k, r in saved.items()},
                             {Q.VERSION: ["failure_cause"]})

    def test_a_record_a_job_spelt_alike_or_one_that_cannot_be_searched_may_own_is_not_copied_by_a_save(self):
        kept = {Q.VERSION: {"questions": Q.VERSION, "answers": {"kept": {"choice": "x"}}, "asked_at": "2026-10-01T00:00:00Z"}}
        for name, other, how in (("x:y", "x_y", "alike"), ("job-x", "job-x", "unsearchable")):
            with self.subTest(how):
                one, two = self.tmp / how / "one", self.tmp / how / "two"
                copy_job(one / name, "2026-01-01T00:00:00Z"); copy_job(two / other, "2026-02-02T00:00:00Z")
                ws = workspace.create(web.shared.WS_HOME, [str(one), str(two)], how)
                web.shared.open_workspace(ws)
                old = Path(ws.dir) / "judgements" / other; old.mkdir(parents=True)     # x:y's older spelling is x_y's
                (old / f"{TRIAL}.json").write_text(json.dumps(kept))
                j = next(j for j in workspace.load_jobs(ws, ws.roots) if j.dir == str(one / name))
                t = next(t for t in j.trials if t.trial_name == TRIAL)
                twin, scandir, looks = str(two / other), web.judge.store.os.scandir, parse.looks_like_job_dir
                hidden = how == "unsearchable"                                            # as a folder without search permission looks
                def scan(p):
                    if hidden and str(p) == twin:
                        raise PermissionError(13, "Permission denied", p)
                    return scandir(p)
                with web.app.test_request_context(f"/trial/{name}/{TRIAL}"), patch.object(web.judge.store.os, "scandir", scan), \
                        patch("nullius.runs.parse.looks_like_job_dir", lambda p: not (hidden and str(p) == twin) and looks(p)):
                    web.judge.judge_trial(j, t, None)
                saved = json.loads((self.tmp / "data" / "judgements" / web.judge.store.key(j.uid) / f"{TRIAL}.json").read_text())
                self.assertNotIn("kept", saved[Q.VERSION]["answers"])


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
        copy_job(self.tmp / "runs" / "job-x")
        with self.subTest("a folder of its imports cannot be listed once the delete has begun"):
            os.makedirs(self.ws.imports_dir, exist_ok=True)
            real = os.walk
            def walk(top, *a, onerror=None, **k):
                if os.path.realpath(top) == os.path.realpath(self.ws.imports_dir):
                    onerror(PermissionError(13, "Permission denied", top)); return iter(())
                return real(top, *a, onerror=onerror, **k)
            with patch("os.walk", walk):
                r = self.client.post(f"/workspaces/delete/{self.ws.name}", data={"csrf": self.token()}); self.addCleanup(r.close)
            self.assertIsNotNone(web.shared.STATE["workspace"])
        r = self.client.post(f"/workspaces/delete/{self.ws.name}", data={"csrf": self.token()}); self.addCleanup(r.close)
        self.assertIsNone(web.shared.STATE["workspace"])
        self.assertFalse(Path(self.ws.dir).exists())                          # closed without being saved again


class EachTryItsOwn(unittest.TestCase):
    """A retried trial's earlier try is judged, and shown, apart from its last
    try; a try that recorded no start is not judged, and shows no record."""
    def test_an_older_pair_record_names_no_start_so_no_retried_try_shows_it(self):
        tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-pair-")); self.addCleanup(shutil.rmtree, tmp, True)
        side = lambda name, retries: SimpleNamespace(trial_name=name, key=name, n_retries=retries, started_at=None, has_trajectory=True)
        job = SimpleNamespace(uid="u", job_id="j", dir=str(tmp))
        view = lambda a: SimpleNamespace(selected=[SimpleNamespace(workspace=None)], jobs=[job, job],
                                         tasks=[SimpleNamespace(task="k", trials=[a, side("b", 0)])])
        with patch.object(web.judge.store, "home", return_value=str(tmp / "data")), \
                patch.object(web.judge, "old_of", return_value={}), patch.object(web.judge, "job_key", return_value="u"):
            web.judge.store.save(str(tmp / "data"), "u", web.judge.store.pair_key("a", "b"), {
                "questions": Q.EARLIER[0], "answers": {web.judge.LEGACY_QUESTION: {"choice": "A"}}})
            self.assertEqual([bool(web.judge.pair_records(view(side("a", n)))) for n in (0, 1)], [True, False])

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
                with patch.object(web.judge.batch, "ask_states", side_effect=asked(choice)) as ask:
                    if trial is first:
                        with self.assertRaises(web.judge.client.JudgeError):
                            web.judge.judge_trial(job, trial, "k", calibration={})
                        ask.assert_not_called()
                    else:
                        web.judge.judge_trial(job, trial, "k", calibration={})
            web.judge.store.save(str(tmp / "data"), job.uid, first.trial_name, {"questions": Q.VERSION, "answers": {
                "failure_cause": {"choice": "first"}}})                                  # under its own name, as before
            shown = [(web.judge.judgement_of(job, t) or {}).get("answers", {}).get("failure_cause", {}).get("choice")
                     for t in (first, last)]
        self.assertEqual(shown, [None, "last"])

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
            t = next(t for t in job.trials if t.trial_name == TRIAL)
            web.judge.judge_trial(job, t, "k", calibration={})
            web.judge.store.save(str(tmp / "data"), job.uid, TRIAL, {"questions": Q.VERSION, "answers": {
                "control_failure_cause": {"choice": "x"}}}, started=web.judge.started_of(t))
            web.judge.store.save(str(tmp / "data"), job.uid, f"{TRIAL}.try-1", {"questions": Q.VERSION, "answers": {
                "failure_cause": {"choice": "unstamped"}}}, lane="llm")                  # under the try's name, before starts were kept
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
            side = SimpleNamespace(present=True, failed=True, broke_tests=False, control="nop", job_id=job.job_id, trial_name=TRIAL)
            report = SimpleNamespace(rows=[SimpleNamespace(verdict="unclear", task_base="hello", nop=side, oracle=SimpleNamespace(present=False))])
            self.assertEqual(web.judge.triage_summary(report, [job])["judged"], 0)       # the first try's cause, not the last's


if __name__ == "__main__":
    unittest.main()
