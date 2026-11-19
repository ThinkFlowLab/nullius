"""Deleting a launch Nullius made, one run or a shard per machine: per run its
folder, its roots and parsed jobs in every workspace, the judgements of its
trials there, and its host copy when asked; never a launch with a run still
going, nor a folder that is not a run's."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from nullius import execution, hosts, workspace
from nullius.judge import store

from tests.test_workspace import _job_dir, _write_json


class RunDelete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-rundel-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root, self.home = os.path.join(self.tmp, "runs"), os.path.join(self.tmp, "ws")
        patcher = patch.dict(execution.EXECUTIONS, clear=True); patcher.start(); self.addCleanup(patcher.stop)
        self.jobs = self.run_("r1")
        other = os.path.join(self.tmp, "elsewhere"); _job_dir(other, "job-b")
        self.a = workspace.create(self.home, [self.jobs, other], label="a")
        self.b = workspace.create(self.home, [self.jobs], label="b")
        self.c = workspace.create(self.home, [other], label="c")
        for ws in (self.a, self.b, self.c):
            workspace.load_jobs(ws, ws.roots)                                        # parsed, as a page leaves them
            for job in ("job-a", "job-b"):
                store.save(ws.dir, job, "t0", {"questions": "v", "answers": {"q": 1}})
                store.save(ws.dir, job, "t0", {"questions": "v", "answers": {"q": 1}}, lane="llm")

    def run_(self, name, state="done", host=None, step="run", job="job-a"):
        plan = execution.Plan(name=name, script="true", host=hosts.parse(host) if host else None)
        _write_json(os.path.join(self.root, name, execution.RECORD), {"plan": plan.to_dict(), "state": state, "step": step})
        jobs = os.path.join(self.root, name, "jobs"); _job_dir(jobs, job)
        return jobs

    def test_it_asks_what_goes(self):
        gone = execution.deletion(["r1"], self.home, self.root)
        self.assertEqual((gone["bytes"], gone["judgements"], gone["workspaces"], gone["hosts"]), (gone["runs"][0]["bytes"], 4, ["a", "b"], {}))
        gone = gone["runs"][0]
        self.assertEqual(gone["dir"], os.path.realpath(os.path.join(self.root, "r1")))
        self.assertGreater(gone["bytes"], 0)
        self.assertEqual(sorted(w["ws"].name for w in gone["workspaces"]), ["a", "b"])
        self.assertEqual(gone["judgements"], 4)                                      # job-a's, in both lanes, in a and b
        self.assertIsNone(gone["host"])

    def test_it_deletes_the_folder_its_roots_and_its_judgements_only(self):
        execution.delete(["r1"], self.home, self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root, "r1")))
        a, b, c = (workspace.load(self.home, n) for n in "abc")
        self.assertEqual(a.roots, [os.path.join(self.tmp, "elsewhere")]); self.assertEqual(b.roots, [])
        self.assertEqual(c.roots, self.c.roots)
        for ws in (a, b):
            self.assertEqual(store.job_dirs(ws.dir, ["job-a"]), [])
            self.assertNotIn(os.path.join(self.jobs, "job-a"), workspace.job_index(ws))
            self.assertEqual([j.job_id for j in workspace.load_jobs(ws, ws.roots)], ["job-b"] if ws is a else [])
        for ws in (a, c):
            self.assertEqual(len(store.job_dirs(ws.dir, ["job-b"])), 2)                # another root's judgements stay
        self.assertEqual(len(store.job_dirs(c.dir, ["job-a"])), 2)                     # c never held the run

    def test_a_job_of_the_same_id_in_another_root_keeps_its_judgements(self):
        twin = os.path.join(self.tmp, "twin"); _job_dir(twin, "job-a")
        self.a.roots.append(twin); workspace.save(self.a); workspace.load_jobs(self.a, self.a.roots)
        execution.delete(["r1"], self.home, self.root)
        self.assertEqual(len(store.job_dirs(self.a.dir, ["job-a"])), 2)
        self.assertEqual(store.job_dirs(self.b.dir, ["job-a"]), [])

    def test_a_run_still_going_is_refused(self):
        for state in execution.ALIVE:
            with self.subTest(state=state):
                self.run_("live", state=state)
                with self.assertRaisesRegex(ValueError, f"live is {state}: stop it"):
                    execution.delete(["r1", "live"], self.home, self.root)
                self.assertTrue(os.path.isdir(os.path.join(self.root, "live")))
                self.assertTrue(os.path.isdir(os.path.join(self.root, "r1")))                # nothing of the launch goes

    def test_the_registry_s_state_wins_over_the_record_s(self):
        ex = execution.Execution(execution.Plan(name="r1", script="true"), root=self.root, record={"state": "done"})
        ex.state = "running"; execution.EXECUTIONS[ex.id] = ex
        with self.assertRaisesRegex(ValueError, "is running"):
            execution.delete(["r1"], self.home, self.root)
        ex.state = "done"
        execution.delete(["r1"], self.home, self.root)
        self.assertNotIn(ex.id, execution.EXECUTIONS)

    def test_only_a_run_folder_in_the_runs_root_is_deleted(self):
        outside = os.path.join(self.tmp, "outside"); _write_json(os.path.join(outside, execution.RECORD), {"plan": {}, "state": "done"})
        os.symlink(outside, os.path.join(self.root, "link"))
        os.makedirs(os.path.join(self.root, "bare"))                                # no record: not a run
        for name in ("../outside", "link", "bare", "", ".", "r1/jobs", os.path.join(self.tmp, "outside")):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "is not a run Nullius made"):
                execution.delete([name], self.home, self.root)
        self.assertTrue(os.path.isfile(os.path.join(outside, execution.RECORD)))

    def test_a_root_is_a_run_s_own_folder_only_inside_the_runs_root(self):
        outside = os.path.join(self.tmp, "outside"); _write_json(os.path.join(outside, execution.RECORD), {"plan": {}, "state": "done"})
        os.symlink(outside, os.path.join(self.root, "link"))                        # a link out of the runs root
        os.symlink(self.jobs, os.path.join(self.tmp, "alias"))                      # a link into it: the run it resolves to
        os.makedirs(os.path.join(self.jobs, "job-a", "deeper"), exist_ok=True)
        for path, name in ((self.jobs, "r1"), (os.path.dirname(self.jobs), "r1"), (os.path.join(self.tmp, "alias"), "r1"),
                           (self.root, None), (os.path.join(self.root, "link"), None), (outside, None),
                           (os.path.join(self.jobs, "job-a"), None), (os.path.join(self.tmp, "elsewhere"), None)):
            with self.subTest(path=path):
                self.assertEqual(execution.run_of(path, self.root), name)

    def test_the_host_copy_goes_when_asked(self):
        self.run_("far", host="ubuntu@10.0.0.4")
        calls = []
        execution.delete(["far"], self.home, self.root, copies={"ubuntu@10.0.0.4"}, run=lambda argv, timeout: calls.append(argv) or (0, ""))
        self.assertEqual(calls[0][-2:], ["ubuntu@10.0.0.4", 'rm -rf "$HOME/.nullius/runs/far"'])
        self.run_("far", host="ubuntu@10.0.0.4"); calls.clear()
        execution.delete(["far"], self.home, self.root, run=lambda argv, timeout: calls.append(argv) or (0, ""))
        self.assertEqual(calls, [])

    def test_a_host_that_does_not_answer_keeps_its_copy_and_says_so(self):
        self.run_("far", host="ubuntu@10.0.0.4")
        gone = execution.delete(["far"], self.home, self.root, copies={"ubuntu@10.0.0.4"},
                                run=lambda argv, timeout: (255, "ssh: connect to host 10.0.0.4: No route"))
        self.assertEqual(gone["kept"], ["the copy of far on ubuntu@10.0.0.4 stays: ssh: connect to host 10.0.0.4: No route"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "far")))

    def test_a_run_refused_at_the_name_check_leaves_the_host_s_folder(self):
        self.run_("far", state="failed", host="ubuntu@10.0.0.4", step="name")          # that folder on the host is another run's
        self.assertEqual(execution.deletion(["far"], self.home, self.root)["hosts"], {})

    def test_a_sharded_launch_goes_whole_each_copy_from_its_own_host(self):
        roots = [self.run_(f"s{i}", host=f"ubuntu@10.0.0.{3 + i}", job=f"L-shard-{i}of2") for i in (1, 2)]
        ws = workspace.create(self.home, roots, label="d"); workspace.load_jobs(ws, ws.roots)
        for i in (1, 2):
            store.save(ws.dir, f"L-shard-{i}of2", "t0", {"questions": "v", "answers": {"q": 1}})
        asked = execution.deletion(["s1", "s2"], self.home, self.root)
        self.assertEqual((asked["hosts"], asked["workspaces"]), ({"ubuntu@10.0.0.4": 1, "ubuntu@10.0.0.5": 1}, ["d"]))
        calls = []
        gone = execution.delete(["s1", "s2"], self.home, self.root, copies={"ubuntu@10.0.0.4", "ubuntu@10.0.0.5"},
                                run=lambda argv, timeout: calls.append(argv[-2:]) or (0, ""))
        self.assertEqual(calls, [["ubuntu@10.0.0.4", 'rm -rf "$HOME/.nullius/runs/s1"'], ["ubuntu@10.0.0.5", 'rm -rf "$HOME/.nullius/runs/s2"']])
        self.assertEqual((gone["stays"], gone["kept"]), ([], []))
        for name in ("s1", "s2"):
            self.assertFalse(os.path.exists(os.path.join(self.root, name)))
        self.assertEqual(workspace.load(self.home, "d").roots, [])                     # both runs left it, neither wrote it back
        self.assertEqual(store.job_dirs(ws.dir, ["L-shard-1of2", "L-shard-2of2"]), [])

    def test_a_launch_with_one_shard_still_going_is_refused_whole(self):
        self.run_("s1", host="ubuntu@10.0.0.4"); self.run_("s2", state="running", host="ubuntu@10.0.0.5")
        calls = []
        with self.assertRaisesRegex(ValueError, "^s2 is running: stop it before you delete it$"):
            execution.delete(["s1", "s2"], self.home, self.root, copies={"ubuntu@10.0.0.4"}, run=lambda argv, timeout: calls.append(argv))
        self.assertEqual(calls, [])
        self.assertTrue(all(os.path.isdir(os.path.join(self.root, n)) for n in ("s1", "s2")))

    def test_a_folder_that_cannot_go_is_said_and_its_run_kept_whole(self):
        self.run_("s1", host="ubuntu@10.0.0.4"); self.run_("s2", host="ubuntu@10.0.0.5")
        real = shutil.rmtree
        def rmtree(path, *a, **kw):
            if os.path.basename(path) == "s2":
                raise PermissionError(13, "Permission denied", path)
            return real(path, *a, **kw)
        calls = []
        with patch.object(execution.shutil, "rmtree", rmtree):
            gone = execution.delete(["s1", "s2"], self.home, self.root, copies={"ubuntu@10.0.0.4", "ubuntu@10.0.0.5"},
                                    run=lambda argv, timeout: calls.append(argv[-2]) or (0, ""))
        s2 = os.path.realpath(os.path.join(self.root, "s2"))
        self.assertEqual((gone["stays"], gone["kept"]), (["s2"], [f"s2 stays in {s2}: Permission denied"]))
        self.assertEqual(calls, ["ubuntu@10.0.0.4"])                                     # s2's host copy stays with it
        self.assertFalse(os.path.exists(os.path.join(self.root, "s1"))); self.assertTrue(os.path.isdir(s2))


class RunSize(unittest.TestCase):
    setUp, run_ = RunDelete.setUp, RunDelete.run_

    def _grow(self):
        with open(os.path.join(self.jobs, "job-a", "tau3-bench__t0__abc", "result.json"), "a") as fh:
            fh.write(" " * 1000)

    def test_an_ended_run_is_walked_again_once_it_moves_and_delete_always_asks_afresh(self):
        ex = execution.Execution(execution.Plan(name="r1", script="true"), root=self.root, record={"state": "done"})
        first = execution.run_size(ex)
        self.assertEqual(first, execution.size_of(ex.dir))
        self._grow()                                            # deep in a trial: the listing keeps what it read
        self.assertEqual(execution.run_size(ex), first)
        self.assertEqual(execution.deletion(["r1"], self.home, self.root)["bytes"], first + 1000)
        _write_json(os.path.join(ex.dir, "nullius.log"), {})    # its folder moves: walked again
        self.assertEqual(execution.run_size(ex), execution.size_of(ex.dir))

    def test_a_run_still_going_is_walked_at_each_ask(self):
        ex = execution.Execution(execution.Plan(name="r1", script="true"), root=self.root, record={"state": "running"})
        first = execution.run_size(ex)
        self._grow()
        self.assertEqual(execution.run_size(ex), first + 1000)


class Owners(unittest.TestCase):
    def test_several_records_naming_a_job_give_it_the_newest_and_the_first_listed_of_a_tie(self):
        def rec(name, started, *jobs):
            plan = execution.Plan(name=name, script="true", jobs=[{"name": j} for j in jobs])
            return execution.Execution(plan, root=tempfile.gettempdir(), record={"id": name, "state": "done", "started": started})
        recs = [rec("old", 1, "a", "b"), rec("tie-1", 5, "b", "c"), rec("tie-2", 5, "b", "c"), rec("new", 9, "a"), rec("older", 3, "a", "c")]
        with patch.dict(execution.EXECUTIONS, {e.id: e for e in recs}, clear=True):
            owner = execution.owners()
            for job in "abcd":
                found = [e for e in recs if any(j["name"] == job for j in e.plan.jobs)]
                before = max(found, key=lambda e: e.started) if found else None     # for_job as it was: one scan per job
                self.assertIs(owner.get(job), before)
                self.assertIs(execution.for_job(job), before); self.assertIs(execution.for_job(job, owner), before)
        self.assertEqual({j: e.id for j, e in owner.items()}, {"a": "new", "b": "tie-1", "c": "tie-1"})


class SizeOf(unittest.TestCase):
    def test_a_file_gone_since_the_walk_counts_nothing(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d)
        for name, data in (("run.json", b"{}"), ("run.json.tmp", b"12345")):
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(data)
        real = os.walk
        def walk(path):                                         # the run replaces its record between the listing and the stat
            for entry in real(path):
                os.remove(os.path.join(d, "run.json.tmp"))
                yield entry
        with patch.object(execution.os, "walk", walk):
            self.assertEqual(execution.size_of(d), 2)


if __name__ == "__main__":
    unittest.main()
