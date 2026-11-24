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
    return NS(job_id=job_id, uid=uid, dataset="", dataset_path="", trials=[NS(trial_name=t, key=t, n_retries=0) for t in trials])


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
            self.assertEqual(list(json.load(f)), [Q.VERSION, store.ADOPTED])
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

    def test_an_earlier_try_is_read_under_its_trial_filed_under_its_try(self):
        first, last = "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"
        store.save(self.a, "job-x", "t0.try-1", rec("2026-01-01T00:30:00Z", "first try"), started=first)
        store.save(self.a, "job-x", "t0", rec("2026-01-01T01:30:00Z", "last try"), started=last)
        run = job("job-x", "uid-1"); run.trials[0].n_retries = 1
        old = store.overlay(self.a, run)
        for name, read in (("read", lambda **k: store.load(self.home, "uid-1", "t0", old=old, **k)),
                           ("copied", lambda **k: store.load(self.home, "uid-1", "t0", **k))):
            with self.subTest(name):
                if name == "copied":
                    store.adopt(self.a, run)
                self.assertEqual(read(started=first)["answers"]["q"]["choice"], "first try")
                self.assertEqual(read(started=last)["answers"]["q"]["choice"], "last try")

    def test_a_record_copied_once_is_not_copied_again_after_a_save_changed_it(self):
        store.save(self.a, "job-x", "t0", rec("2026-01-01T00:00:00Z"))
        run = job("job-x", "uid-1")
        store.adopt(self.a, run)
        store.save(self.home, "uid-1", "t0", dict(rec("2026-01-02T00:00:00Z"), answers={"other": {"choice": "y"}}))
        store.adopt(self.a, run)
        shown = store.load(self.home, "uid-1", "t0", old=store.overlay(self.a, run))
        self.assertEqual(set(shown["answers"]), {"q", "other"})
        with open(os.path.join(self.home, "judgements", "uid-1", "t0.json")) as f:
            self.assertEqual(sorted(json.load(f)), [Q.VERSION, store.ADOPTED])           # no second slot of the same record

    def test_an_old_file_that_holds_no_object_is_skipped(self):
        d = os.path.join(self.a, "judgements", "job-x"); os.makedirs(d)
        for name, body in (("t0", [1, 2]), ("t1", {Q.VERSION: [1]})):
            with open(os.path.join(d, name + ".json"), "w") as f:
                json.dump(body, f)
        run = job("job-x", "uid-1", trials=("t0", "t1"))
        self.assertIsNone(store.load(self.home, "uid-1", "t0", old=store.overlay(self.a, run)))
        store.adopt(self.a, run)
        self.assertEqual(list(store.all_records(self.home, "uid-1")), [])

    def test_a_store_file_that_cannot_be_read_is_never_written_over(self):
        store.save(self.a, "job-x", "t0", rec("2026-01-01T00:00:00Z"))
        path = os.path.join(self.home, "judgements", "uid-1", "t0.json"); os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write('{"judgement.questions.v2": {"questi')                                  # cut short
        for name, write in (("a save", lambda: store.save(self.home, "uid-1", "t0", rec("2026-01-02T00:00:00Z"))),
                            ("a copy", lambda: store.adopt(self.a, job("job-x", "uid-1")))):
            with self.subTest(name), self.assertRaises(store.Unreadable):
                write()
            self.assertEqual(open(path).read(), '{"judgement.questions.v2": {"questi')

    def test_the_move_waits_for_a_root_that_cannot_be_read(self):
        root = os.path.join(self.tmp, "jobs"); _job_dir(root, "job-a")
        home_dir = os.path.join(self.tmp, "ws")
        ws = workspace.create(home_dir, [root], label="w")
        [j] = workspace.load_jobs(ws, ws.roots)
        store.save(ws.dir, "job-a", j.trials[0].trial_name, rec("2026-01-01T00:00:00Z"))
        os.rename(root, root + ".away")
        self.assertEqual(store.move(home_dir), ["w"])
        self.assertFalse(store.moved())
        with self.assertRaisesRegex(ValueError, "cannot be read"):
            workspace.delete(home_dir, ws.name)
        self.assertTrue(os.path.isdir(ws.dir))
        os.rename(root + ".away", root)
        self.assertEqual(store.move(home_dir), [])
        self.assertTrue(store.moved())
        self.assertEqual(store.old_stores(home_dir), [])
        self.assertTrue(store.load(self.home, j.uid, j.trials[0].trial_name))


if __name__ == "__main__":
    unittest.main()
