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
from nullius.runs import parse

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

    def test_an_earlier_try_is_read_under_its_own_name(self):
        store.save(self.a, "job-x", "t0.try-1", rec("2026-01-01T00:30:00Z", "first try"))        # judged before starts were kept
        store.save(self.a, "job-x", "t0", rec("2026-01-01T01:30:00Z", "last try"))
        run = job("job-x", "uid-1"); run.trials[0].n_retries = 1
        old = store.overlay(self.a, run)
        for name, read in (("read", lambda t, **k: store.load(self.home, "uid-1", t, old=old, **k)),
                           ("copied", lambda t, **k: store.load(self.home, "uid-1", t, **k))):
            with self.subTest(name):
                if name == "copied":
                    store.adopt(self.a, run)
                self.assertEqual(read("t0.try-1", started="S1")["answers"]["q"]["choice"], "first try")
                self.assertEqual(read("t0", started="S2")["answers"]["q"]["choice"], "last try")

    def test_a_pair_kept_under_the_older_spelling_is_read_before_the_move(self):
        name = store.pair_key("t0", "t1")
        d = os.path.join(self.a, "judgements", "job-x"); os.makedirs(d)
        with open(os.path.join(d, store._SAFE.sub("_", name) + ".json"), "w") as f:
            json.dump({Q.VERSION: rec("2026-01-01T00:00:00Z", "pair")}, f)
        run = job("job-x", "uid-1")
        self.assertEqual(store.load(self.home, "uid-1", name, old=store.overlay(self.a, run))["answers"]["q"]["choice"], "pair")

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
        self.assertEqual(store.move(home_dir), ["w (a root cannot be read)"])
        self.assertFalse(store.moved(ws.dir))
        with self.assertRaisesRegex(ValueError, "cannot be read"):
            workspace.delete(home_dir, ws.name)
        self.assertTrue(os.path.isdir(ws.dir))
        os.rename(root + ".away", root)
        real = os.scandir
        def unlisted(p="."):                       # a root without permission: there, but its jobs cannot be listed
            if isinstance(p, str) and os.path.realpath(p) == os.path.realpath(root):
                raise PermissionError(13, "Permission denied", p)
            return real(p)
        with patch("os.scandir", unlisted), self.assertRaisesRegex(ValueError, "cannot be read"):
            workspace.delete(home_dir, ws.name)
        self.assertTrue(os.path.isdir(ws.dir))
        self.assertEqual(store.move(home_dir), [])
        self.assertTrue(store.moved(ws.dir))
        self.assertEqual(store.old_stores(home_dir), [])
        self.assertTrue(store.load(self.home, j.uid, j.trials[0].trial_name))

    def test_a_workspace_is_moved_only_once_every_old_file_was_read_and_found_its_job(self):
        root = os.path.join(self.tmp, "jobs"); _job_dir(root, "job-a")
        home_dir = os.path.join(self.tmp, "ws")
        ws = workspace.create(home_dir, [root], label="w")
        [j] = workspace.load_jobs(ws, ws.roots)
        t0, t1 = (t.trial_name for t in j.trials)
        store.save(ws.dir, "job-a", t0, rec("2026-01-01T00:00:00Z", "here"))
        store.save(ws.dir, "job-gone", "t0", rec("2026-01-01T00:00:00Z", "gone"))   # its folder away for now
        with open(os.path.join(ws.dir, "judgements", "job-a", t1 + ".json"), "w") as f:
            f.write('{"v2": {')                                                          # cut short
        self.assertEqual(store.move(home_dir), ["w (1 file cannot be read, 1 record of no job found)"])
        self.assertFalse(store.moved(ws.dir))
        self.assertEqual(store.old_stores(home_dir), [(ws, 3)])
        self.assertTrue(store.load(self.home, j.uid, t0))                                # what could go, went
        _job_dir(root, "job-gone")
        [gone] = [g for g in workspace.load_jobs(ws, ws.roots) if g.job_id == "job-gone"]
        self.assertEqual(store.load(self.home, gone.uid, "t0", old=store.overlay(ws.dir, gone))["answers"]["q"]["choice"], "gone")
        os.remove(os.path.join(ws.dir, "judgements", "job-a", t1 + ".json"))              # repaired by hand
        store.save(ws.dir, "job-a", t1, rec("2026-01-01T00:00:00Z", "repaired"))
        self.assertEqual(store.move(home_dir), [])
        self.assertTrue(store.moved(ws.dir))
        self.assertEqual(store.overlay(ws.dir, gone), {})
        store.save(ws.dir, "job-a", "restored", rec("2026-01-01T00:00:00Z"))            # a store brought back later
        self.assertFalse(store.moved(ws.dir))
        self.assertEqual([n for _, n in store.old_stores(home_dir)], [4])

    def test_a_moved_workspace_is_read_again_once_a_file_changes_or_a_folder_cannot_be_listed(self):
        root = os.path.join(self.tmp, "jobs"); _job_dir(root, "job-a")
        home_dir = os.path.join(self.tmp, "ws")
        ws = workspace.create(home_dir, [root], label="w")
        [j] = workspace.load_jobs(ws, ws.roots)
        t0 = j.trials[0].trial_name
        store.save(ws.dir, "job-a", t0, rec("2026-01-01T00:00:00Z", "first"))
        self.assertEqual(store.move(home_dir), [])
        self.assertTrue(store.moved(ws.dir))
        store.save(ws.dir, "job-a", t0, dict(rec("2026-01-02T00:00:00Z"), answers={"later": {"choice": "y"}}))   # an older Nullius, in place
        self.assertFalse(store.moved(ws.dir))
        self.assertIn("later", store.load(self.home, j.uid, t0, old=store.overlay(ws.dir, j))["answers"])
        self.assertEqual(store.move(home_dir), [])
        real = os.scandir
        def unlisted(p="."):
            if isinstance(p, str) and os.path.basename(p) == "job-a":
                raise PermissionError(13, "Permission denied", p)
            return real(p)
        with patch("os.scandir", unlisted):
            self.assertFalse(store.moved(ws.dir))
            self.assertEqual(store.move(home_dir), ["w (a folder cannot be read)"])
        old = os.path.join(ws.dir, "judgements", "job-a", t0 + ".json"); kept = open(old, "rb").read()

        def made_again():                                                            # its records alike, byte for byte
            w = workspace.create(home_dir, [root], label="w")
            os.makedirs(os.path.dirname(old)); open(old, "wb").write(kept)
            return w
        with self.subTest("renamed, then another made under its name"):
            self.assertEqual(store.move(home_dir), [])
            workspace.rename(home_dir, ws, "w2")
            self.assertTrue(store.moved(ws.dir))
            again = made_again()
            self.assertFalse(store.moved(again.dir))
        with self.subTest("deleted, then another made under its name"):
            self.assertEqual(store.move(home_dir), [])
            workspace.delete(home_dir, again.name)
            self.assertFalse(store.moved(made_again().dir))
        with self.subTest("a job's older folder forgotten"):
            self.assertEqual(store.move(home_dir), [])
            store.forget(ws.dir, [os.path.join(ws.dir, "judgements-llm")])
            self.assertFalse(store.moved(ws.dir))
        with self.subTest("a version that holds no record"):
            open(os.path.join(ws.dir, "judgements", "job-a", t0 + ".json"), "w").write(json.dumps({Q.VERSION: [1]}))
            self.assertIn("w2 (1 file cannot be read)", store.move(home_dir))
            self.assertFalse(store.moved(ws.dir))

    def test_a_record_two_jobs_of_one_id_share_is_moved_to_neither_and_still_shown_on_both(self):
        r1, r2 = (os.path.join(self.tmp, r) for r in ("r1", "r2"))
        _job_dir(r1, "job-x"); _job_dir(r2, "job-x")
        with open(os.path.join(r2, "job-x", "result.json"), "w") as f:
            json.dump({"id": "j2", "started_at": "2026-09-05T00:00:00Z", "stats": {}}, f)
        home_dir = os.path.join(self.tmp, "ws")
        ws = workspace.create(home_dir, [r1, r2], label="w")
        jobs = workspace.load_jobs(ws, ws.roots)
        self.assertEqual(len({j.uid for j in jobs}), 2)
        t0 = jobs[0].trials[0].trial_name
        store.save(ws.dir, "job-x", t0, rec("2026-01-01T00:00:00Z"))
        self.assertEqual(store.move(home_dir), ["w (1 record two jobs share)"])
        self.assertFalse(os.path.isdir(os.path.join(self.home, "judgements")) and
                         [d for d in os.listdir(os.path.join(self.home, "judgements")) if not d.startswith(".")])
        self.assertEqual([bool(store.load(self.home, j.uid, t0, old=store.overlay(ws.dir, j))) for j in jobs], [True, True])
        workspace.delete(home_dir, ws.name)                                         # nor by a delete: kept whole beside
        self.assertFalse(os.path.isdir(os.path.join(self.home, "judgements")))
        [kept] = os.listdir(os.path.join(self.home, "judgements-of-deleted-workspaces"))
        self.assertTrue(os.path.isfile(os.path.join(self.home, "judgements-of-deleted-workspaces", kept, "judgements", "job-x", t0 + ".json")))

    def test_a_job_of_the_same_name_still_starting_may_own_them_too(self):
        r1, r2 = (os.path.join(self.tmp, r) for r in ("r1", "r2"))
        _job_dir(r1, "job-x")
        home_dir = os.path.join(self.tmp, "ws")
        ws = workspace.create(home_dir, [r1, r2], label="w")
        [j] = workspace.load_jobs(ws, ws.roots)                                     # parsed before the other came
        os.makedirs(os.path.join(r2, "job-x"))
        with open(os.path.join(r2, "job-x", "config.json"), "w") as f:
            json.dump({"job_name": "job-x", "agents": [{"name": "oracle"}]}, f)
        t0 = j.trials[0].trial_name
        store.save(ws.dir, "job-x", t0, rec("2026-01-01T00:00:00Z"))
        self.assertEqual(store.move(home_dir), ["w (1 record of a job still starting)"])
        self.assertFalse(os.path.isdir(os.path.join(self.home, "judgements", j.uid)))
        self.assertEqual(store.single(store.owners(ws, names={"job-x"})), {})

    def test_a_finished_job_whose_config_cannot_be_read_owns_none_of_them(self):
        r1 = os.path.join(self.tmp, "r1")
        d = _job_dir(r1, "job-x")
        home_dir = os.path.join(self.tmp, "ws")
        ws = workspace.create(home_dir, [r1], label="w")
        t0 = workspace.load_jobs(ws, ws.roots)[0].trials[0].trial_name
        store.save(ws.dir, "job-x", t0, rec("2026-01-01T00:00:00Z"))
        with open(os.path.join(d, "config.json"), "w") as f:
            f.write('{"job_name": "job-x", "agents"')                              # the display parse still derives one
        self.assertTrue(parse.parse_job(d).uid)
        self.assertEqual(store.move(home_dir), ["w (1 record of a job that cannot be read)"])
        self.assertFalse(os.path.isdir(os.path.join(self.home, "judgements")) and
                         [n for n in os.listdir(os.path.join(self.home, "judgements")) if not n.startswith(".")])
        self.assertEqual(store.adopt_owned(ws), set())

    def test_deleting_a_workspace_keeps_the_failure_analyses_of_its_imports(self):
        from nullius.runs import failure_analysis as fa
        home_dir = os.path.join(self.tmp, "ws")
        ws = workspace.create(home_dir, [], label="w")
        j = _job_dir(ws.imports_dir, "atif__x")
        fa.update(j, lambda a: a.set_label("t0", "agent", by="me"))
        workspace.delete(home_dir, ws.name)
        [kept] = os.listdir(os.path.join(self.home, "judgements-of-deleted-workspaces"))
        self.assertTrue(os.path.isfile(os.path.join(self.home, "judgements-of-deleted-workspaces", kept, "imported", "atif__x", fa.FILE)))

    def test_a_workspace_whose_imports_cannot_all_be_listed_is_not_deleted(self):
        from nullius.runs import failure_analysis as fa
        home_dir = os.path.join(self.tmp, "ws")
        ws = workspace.create(home_dir, [], label="w")
        j = _job_dir(ws.imports_dir, "atif__x")
        fa.update(j, lambda a: a.set_label("t0", "agent", by="me"))
        real = os.scandir
        def unlisted(p="."):
            if isinstance(p, str) and os.path.realpath(p) == os.path.realpath(j):
                raise PermissionError(13, "Permission denied", p)
            return real(p)
        with patch("os.scandir", unlisted), self.assertRaisesRegex(ValueError, "cannot be read"):
            workspace.delete(home_dir, ws.name)
        self.assertTrue(os.path.isfile(os.path.join(j, fa.FILE)))

    def test_a_save_or_a_copy_for_a_job_deleted_meanwhile_writes_nothing(self):
        gone = os.path.join(self.tmp, "deleted-job")
        store.save(self.a, "job-x", "t0", rec("2026-01-01T00:00:00Z"))
        run = job("job-x", "uid-1"); run.dir = gone
        for name, write in (("a save", lambda: store.save(self.home, "uid-1", "t0", rec("2026-01-02T00:00:00Z"), job_dir=gone)),
                            ("a copy", lambda: store.adopt(self.a, run))):
            with self.subTest(name), self.assertRaises(store.Gone):
                write()
            self.assertFalse(os.path.exists(os.path.join(self.home, "judgements", "uid-1")))


if __name__ == "__main__":
    unittest.main()
