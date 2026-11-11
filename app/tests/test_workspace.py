"""Workspaces: named roots, a parse cache keyed by what the files are.

The cache must be invisible when it is right and absent when it is wrong: a
cached job round-trips to an equal Job, a changed result.json is parsed
again, and a schema bump throws every entry away.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from nullius.runs import parse
from nullius import workspace


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _job_dir(root, name="job-a", n=2):
    d = os.path.join(root, name)
    _write_json(os.path.join(d, "config.json"), {"job_name": name, "agents": [
        {"name": "my_agent:MyAgent", "model_name": "openai/m"}],
        "datasets": [{"path": "/ds/tau3-bench"}]})
    _write_json(os.path.join(d, "result.json"), {"id": "j1", "stats": {}})
    for i in range(n):
        t = os.path.join(d, f"tau3-bench__t{i}__abc")
        _write_json(os.path.join(t, "result.json"), {
            "task_name": f"x/tau3-bench__t{i}", "trial_name": f"tau3-bench__t{i}__abc",
            "source": "tau3-bench", "agent_info": {"version": "0.2.3", "model_info": {"name": "m"}},
            "agent_result": {"n_input_tokens": 10 + i, "n_output_tokens": 2, "n_cache_tokens": 5,
                             "cost_usd": 0.01},
            "verifier_result": {"rewards": {"reward": 1.0 if i == 0 else 0.0}},
            "exception_info": None,
            "started_at": "2026-09-04T01:00:00Z", "finished_at": "2026-09-04T01:02:00Z",
            "environment_setup": {"started_at": "2026-09-04T01:00:00Z", "finished_at": "2026-09-04T01:00:10Z"},
            "agent_execution": {"started_at": "2026-09-04T01:00:10Z", "finished_at": "2026-09-04T01:01:50Z"},
            "config": {"environment": {"type": "docker"}},
        })
    return d


class Names(unittest.TestCase):
    def test_random_names_avoid_taken(self):
        rng = random.Random(7)
        first = workspace.random_name(rng=random.Random(7))
        self.assertRegex(first, r"^[a-z]+-[a-z]+$")
        again = workspace.random_name(taken={first}, rng=rng)
        self.assertNotEqual(first, again)

    def test_slug(self):
        self.assertEqual(workspace.slug("  Tau3 full — Sept! "), "tau3-full-sept")
        self.assertEqual(workspace.slug("***"), "")

    def test_a_label_cut_on_a_separator_is_still_a_name(self):
        """48 characters ended on the hyphen before "label": the name was refused, and its workspace listed nowhere."""
        label = "tau3 runs and fake jobs, a fairly long workspace label"
        name = workspace.slug(label)
        self.assertEqual(name, "tau3-runs-and-fake-jobs-a-fairly-long-workspace")
        self.assertEqual(workspace.slug(name), name)
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        a, b = workspace.create(home, ["/r/one"], label=label), workspace.create(home, ["/r/two"], label=label + "s")
        self.assertEqual((a.name, b.name), (name, "tau3-runs-and-fake-jobs-a-fairly-long-workspac-2"))   # suffixed within 48
        self.assertEqual({w.name for w in workspace.list_all(home)}, {a.name, b.name})
        self.assertEqual(workspace.load(home, b.name).roots, ["/r/two"])

    def test_a_name_an_earlier_version_cut_is_read_again_and_nothing_else_is(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        olds = {"tau3-runs-and-fake-jobs-a-fairly-long-workspace-", "tau3-runs-and-fake-jobs-a-fairly-long-workspace-label-2"}
        for old in olds:                                                          # as the earlier slug() and _free() named them
            ws = workspace.create(home, ["/r/" + old], label="x")
            shutil.rmtree(ws.dir)
            workspace.save(replace(ws, name=old, dir=os.path.join(home, old)))
        self.assertEqual({w.name for w in workspace.list_all(home)}, olds)
        for unsafe in ("..", "-x", "a/b", "A", "a_b", ".hidden"):
            self.assertIsNone(workspace.load(home, unsafe))


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_create_load_list(self):
        a = workspace.create(self.home, ["/r/one"], label="Tau3 runs")
        self.assertEqual(a.name, "tau3-runs")
        self.assertEqual(a.label, "Tau3 runs")
        b = workspace.create(self.home, ["/r/two"])
        self.assertRegex(b.name, r"^[a-z]+-[a-z]+$")
        c = workspace.create(self.home, ["/r/three"], label="Tau3 runs")
        self.assertEqual(c.name, "tau3-runs-2")
        self.assertEqual({w.name for w in workspace.list_all(self.home)}, {a.name, b.name, c.name})
        again = workspace.load(self.home, a.name)
        self.assertEqual(again.roots, ["/r/one"])
        self.assertEqual(again.imports_dir, os.path.join(self.home, "tau3-runs", "imported"))
        self.assertIsNone(workspace.load(self.home, "../etc"))
        self.assertIsNone(workspace.load(self.home, "missing"))

    def test_find_by_roots_ignores_order_and_symlinks(self):
        ws = workspace.create(self.home, ["/r/a", "/r/b"])
        self.assertEqual(workspace.find_by_roots(self.home, ["/r/b", "/r/a"]).name, ws.name)
        self.assertIsNone(workspace.find_by_roots(self.home, ["/r/a"]))

    def test_the_runs_a_workspace_started_do_not_hide_it_from_its_own_paths(self):
        """A run started from the page adds its job root to the open workspace;
        a restart with the same paths reopened a new, empty one, and the runs
        were only in the first."""
        ws = workspace.create(self.home, ["/r/jobs", "/data/runs/one/jobs", "/data/runs/two/jobs"])
        self.assertIsNone(workspace.find_by_roots(self.home, ["/r/jobs"]))
        self.assertEqual(workspace.find_by_roots(self.home, ["/r/jobs"], besides="/data/runs").name, ws.name)
        self.assertIsNone(workspace.find_by_roots(self.home, ["/r/other"], besides="/data/runs"))
        self.assertIsNone(workspace.find_by_roots(self.home, ["/r/jobs"], besides="/data/run"))   # a prefix is not the folder

    def test_delete_removes_the_workspace_and_leaves_its_roots(self):
        root = tempfile.mkdtemp(); open(os.path.join(root, "keep"), "w").close()
        a = workspace.create(self.home, [root], label="a")
        os.makedirs(a.imports_dir, exist_ok=True)
        self.assertTrue(workspace.delete(self.home, a.name))
        self.assertIsNone(workspace.load(self.home, a.name)); self.assertFalse(os.path.exists(a.dir))
        self.assertTrue(os.path.exists(os.path.join(root, "keep")))                       # the job root is not the workspace's to remove
        self.assertFalse(workspace.delete(self.home, a.name)); self.assertFalse(workspace.delete(self.home, "../etc"))

    def test_rename_moves_the_name_with_the_label(self):
        """A workspace has one name: renamed, its directory follows the label
        as a new one's would, and everything in it moves along."""
        a = workspace.create(self.home, ["/r/a"], label="sharp lemur")
        os.makedirs(a.imports_dir); open(os.path.join(a.imports_dir, "kept"), "w").close()
        workspace.create(self.home, ["/r/b"], label="tau3 test")
        old = a.dir
        workspace.rename(self.home, a, "tau3 test")
        self.assertEqual((a.name, a.label), ("tau3-test-2", "tau3 test"))                     # taken: suffixed, as create does
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(os.path.join(a.imports_dir, "kept")))
        again = workspace.load(self.home, "tau3-test-2")
        self.assertEqual((again.label, again.roots), ("tau3 test", ["/r/a"]))
        workspace.rename(self.home, a, "Tau3 Test 2")                                        # its own slug: no suffix, the label as typed
        self.assertEqual((a.name, a.label), ("tau3-test-2", "Tau3 Test 2"))
        workspace.rename(self.home, a, "τ³")                                                  # nothing to slug: the name stays
        self.assertEqual((a.name, a.label), ("tau3-test-2", "τ³"))
        self.assertEqual(sorted(w.name for w in workspace.list_all(self.home)), ["tau3-test", "tau3-test-2"])

    def test_rename_never_moves_into_a_directory_already_there(self):
        """A directory at the new name's slug that holds no workspace - left
        behind, or half made - is taken all the same: the rename is
        suffixed and what was there is left alone."""
        a = workspace.create(self.home, ["/r/a"], label="sharp lemur")
        stray = os.path.join(self.home, "tau3-test")
        os.makedirs(stray); open(os.path.join(stray, "left-behind"), "w").close()
        workspace.rename(self.home, a, "tau3 test")
        self.assertEqual(a.name, "tau3-test-2")
        self.assertEqual(os.listdir(stray), ["left-behind"])

    def test_touch_orders_the_list(self):
        a = workspace.create(self.home, ["/r/a"], label="a")
        b = workspace.create(self.home, ["/r/b"], label="b")
        a.opened = "2000-01-01T00:00:00+00:00"
        workspace.save(a)
        self.assertEqual([w.name for w in workspace.list_all(self.home)], ["b", "a"])
        workspace.touch(a)
        self.assertEqual([w.name for w in workspace.list_all(self.home)][0], "a")


class Cache(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.root = tempfile.mkdtemp()
        self.job_dir = _job_dir(self.root)
        self.ws = workspace.create(self.home, [self.root], label="c")

    def test_round_trip_equals_a_fresh_parse(self):
        fresh = parse.parse_job(self.job_dir)
        first, from_cache = workspace.load_job(self.ws, self.job_dir)
        self.assertFalse(from_cache)
        second, from_cache = workspace.load_job(self.ws, self.job_dir)
        self.assertTrue(from_cache)
        self.assertEqual(second, fresh)
        self.assertEqual(second.trials[0].phases, fresh.trials[0].phases)
        self.assertEqual(second.trials[0].started_at, fresh.trials[0].started_at)
        self.assertEqual(self.ws.n_cached, 1)

    def test_changed_result_is_parsed_again(self):
        workspace.load_job(self.ws, self.job_dir)
        target = os.path.join(self.job_dir, "tau3-bench__t1__abc", "result.json")
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
        data["verifier_result"] = {"rewards": {"reward": 1.0}}
        time.sleep(0.01)
        _write_json(target, data)
        job, from_cache = workspace.load_job(self.ws, self.job_dir)
        self.assertFalse(from_cache)
        self.assertEqual([t.reward for t in job.trials], [1.0, 1.0])

    def test_schema_bump_invalidates(self):
        workspace.load_job(self.ws, self.job_dir)
        old = workspace.SCHEMA
        workspace.SCHEMA = old + "-next"
        try:
            _, from_cache = workspace.load_job(self.ws, self.job_dir)
        finally:
            workspace.SCHEMA = old
        self.assertFalse(from_cache)

    def test_two_requests_writing_the_same_cache_at_once_both_return(self):
        """The job page and its /live poll both re-parse a job whose files keep
        changing, and both wrote the cache through one shared `<hash>.json.tmp`:
        the first rename took the file away and the second raised
        FileNotFoundError, a 500 on the page. Both writers are held with
        their temp file open until the other has opened its own, then released:
        each must finish, and the cache must be whole."""
        gate = threading.Barrier(2, timeout=5)
        stamp = workspace._now
        def held_now():
            gate.wait()                                   # both are inside the write, temp file open, nothing renamed yet
            return stamp()
        results, errors = [], []
        def load():
            try:
                results.append(workspace.load_job(self.ws, self.job_dir))
            except Exception as e:                        # noqa: BLE001 - the failure under test
                errors.append(e)
        with patch.object(workspace, "_now", held_now):
            threads = [threading.Thread(target=load) for _ in range(2)]
            for t in threads: t.start()
            for t in threads: t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual([from_cache for _, from_cache in results], [False, False])
        job, from_cache = workspace.load_job(self.ws, self.job_dir)
        self.assertTrue(from_cache); self.assertEqual(job, results[0][0])
        self.assertEqual([n for n in os.listdir(self.ws.jobs_dir) if n.endswith(".tmp")], [])

    def test_load_jobs_matches_load_all(self):
        _job_dir(self.root, "job-b", n=1)
        cached = workspace.load_jobs(self.ws, [self.root])
        plain = parse.load_all([self.root])
        self.assertEqual([j.job_id for j in cached], [j.job_id for j in plain])
        self.assertEqual(cached, plain)
        self.assertEqual(workspace.clear_cache(self.ws), 2)
        self.assertEqual(self.ws.n_cached, 0)

    def test_an_unchanged_job_is_decoded_once_per_process(self):
        from unittest import mock
        first, _ = workspace.load_job(self.ws, self.job_dir)
        with mock.patch.object(workspace, "job_from_dict", side_effect=AssertionError("decoded again")):
            again, from_cache = workspace.load_job(self.ws, self.job_dir)
        self.assertIs(again, first); self.assertTrue(from_cache)
        _job_dir(self.root, os.path.basename(self.job_dir), n=3)       # its files change: read again
        changed, from_cache = workspace.load_job(self.ws, self.job_dir)
        self.assertIsNot(changed, first); self.assertFalse(from_cache)

    def test_a_running_job_reads_only_the_trials_that_moved(self):
        from unittest import mock
        import shutil
        first, _ = workspace.load_job(self.ws, self.job_dir)
        shutil.copytree(os.path.join(self.job_dir, "tau3-bench__t1__abc"), os.path.join(self.job_dir, "tau3-bench__t9__abc"))
        with mock.patch.object(parse, "parse_trial", wraps=parse.parse_trial) as parsed:
            again, from_cache = workspace.load_job(self.ws, self.job_dir)
        self.assertFalse(from_cache)
        self.assertEqual(parsed.call_count, 1)                               # the trial that ended, alone
        self.assertEqual(len(again.trials), 3)
        self.assertIs(again.trials[0], first.trials[0]); self.assertIs(again.trials[1], first.trials[1])

    def test_index_lists_runs_without_a_parse(self):
        self.assertEqual(workspace.job_index(self.ws), {})
        workspace.load_job(self.ws, self.job_dir)
        index = workspace.job_index(self.ws)
        self.assertEqual(list(index.values()), [{"job_id": "job-a", "n_trials": 2,
                                                 "dataset": "tau3-bench", "agent": "MyAgent"}])
        self.assertEqual(list(index), [self.job_dir])
        workspace.load_job(self.ws, self.job_dir)          # a cache hit keeps it
        self.assertEqual(len(workspace.job_index(self.ws)), 1)
        workspace.clear_cache(self.ws)
        self.assertEqual(workspace.job_index(self.ws), {})

    def test_corrupt_cache_falls_back_to_parse(self):
        workspace.load_job(self.ws, self.job_dir)
        path = next(os.path.join(self.ws.jobs_dir, n) for n in os.listdir(self.ws.jobs_dir)
                    if n != "index.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        workspace._DECODED.clear()                       # a new process: only the file on disk
        job, from_cache = workspace.load_job(self.ws, self.job_dir)
        self.assertFalse(from_cache)
        self.assertEqual(len(job.trials), 2)


if __name__ == "__main__":
    unittest.main()
