"""Judgements kept in a workspace before they were kept by job
(``<workspace>/judgements{,-llm}/<job id>/``) are still read: copied into the
user's store under the job's identity, the old files left as they are; a
trial judged in two workspaces keeps both records, the newer shown."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from nullius import workspace
from nullius.judge import questions as Q, store

from tests.test_workspace import _job_dir

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "judge", "workspace-store")


def rec(asked_at, choice="x"):
    return {"questions": Q.VERSION, "answers": {"q": {"choice": choice}}, "asked_at": asked_at}


def job(job_id, uid, trials=("t0",)):
    return NS(job_id=job_id, uid=uid, dataset="", dataset_path="", trials=[NS(trial_name=t) for t in trials])


class Adopt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-judge-adopt-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "data")
        patcher = patch.object(store, "home", return_value=self.home); patcher.start(); self.addCleanup(patcher.stop)
        self.a, self.b = (os.path.join(self.tmp, n) for n in "ab")

    def test_a_record_kept_in_a_workspace_is_read_under_the_job_and_its_file_stays(self):
        shutil.copytree(FIXTURE, self.a)                                            # a workspace's store as it was kept before
        before = open(os.path.join(self.a, "judgements", "job-x", "t0.json")).read()
        for _ in range(2):                                                          # twice: nothing added the second time
            store.adopt(self.a, job("job-x", "uid-1"))
        self.assertEqual(store.load(self.home, "uid-1", "t0")["answers"]["failure_cause"]["choice"], "agent_error")
        self.assertEqual(store.load(self.home, "uid-1", "t0", lane="llm")["answers"]["failure_cause"]["choice"], "environment_error")
        with open(os.path.join(self.home, "judgements", "uid-1", "t0.json")) as f:
            self.assertEqual(list(json.load(f)), [Q.VERSION])
        self.assertEqual(open(os.path.join(self.a, "judgements", "job-x", "t0.json")).read(), before)

    def test_a_trial_judged_in_two_workspaces_shows_the_newer_and_keeps_the_other(self):
        store.save(self.a, "job-x", "t0", rec("2026-01-02T00:00:00Z", "newer"))
        store.save(self.b, "job-x", "t0", rec("2026-01-01T00:00:00Z", "older"))
        for ws in (self.b, self.a, self.b):
            store.adopt(ws, job("job-x", "uid-1"))
        self.assertEqual(store.load(self.home, "uid-1", "t0")["answers"]["q"]["choice"], "newer")
        kept = [r["answers"]["q"]["choice"] for _, _, _, r in store.all_records(self.home, "uid-1")]
        self.assertEqual(sorted(kept), ["newer", "older"])

    def test_two_jobs_of_one_id_in_a_workspace_each_get_its_record(self):
        store.save(self.a, "job-x", "t0", rec("2026-01-01T00:00:00Z"))
        for uid in ("uid-1", "uid-2"):
            store.adopt(self.a, job("job-x", uid))
        self.assertEqual([bool(store.load(self.home, u, "t0")) for u in ("uid-1", "uid-2")], [True, True])

    def test_a_task_score_kept_as_a_job_is_read_by_its_dataset(self):
        store.save(self.a, "task:/ds/bench", "fix@abc", rec("2026-01-01T00:00:00Z"))
        run = job("run-1", "uid-1"); run.dataset, run.dataset_path = "bench", "/ds/bench"
        store.adopt(self.a, run)
        self.assertTrue(store.load(self.home, "/ds/bench", "fix@abc", lane=store.TASKS))

    def test_deleting_a_workspace_keeps_its_judgements(self):
        root = os.path.join(self.tmp, "jobs"); _job_dir(root, "job-a")
        ws = workspace.create(os.path.join(self.tmp, "ws"), [root], label="w")
        [j] = workspace.load_jobs(ws, ws.roots)
        store.save(ws.dir, "job-a", j.trials[0].trial_name, rec("2026-01-01T00:00:00Z"))
        store.save(ws.dir, "job-gone", "t0", rec("2026-01-01T00:00:00Z"))          # a job its roots no longer reach
        workspace.delete(os.path.dirname(ws.dir), ws.name)
        self.assertFalse(os.path.exists(ws.dir))
        self.assertTrue(store.load(self.home, j.uid, j.trials[0].trial_name))
        [kept] = os.listdir(os.path.join(self.home, "judgements-of-deleted-workspaces"))
        self.assertTrue(os.path.isfile(os.path.join(self.home, "judgements-of-deleted-workspaces", kept, "judgements", "job-gone", "t0.json")))


if __name__ == "__main__":
    unittest.main()
