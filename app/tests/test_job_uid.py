"""A job's identity (Job.uid): the same wherever its folder is copied or
moved, apart for two jobs of one name, made once by the standard runner,
kept by a run resumed into its folder and new for a merge."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from nullius.runs import parse
from nullius.standard.runner import job

HERE = os.path.dirname(os.path.abspath(__file__))
HARBOR = os.path.join(HERE, "fixtures", "harbor-jobs", "liveboard-oracle-x4")
BENCH = {"key": "fakebench", "_ref": "fakebench", "needs": ["agent.workspace/v1"], "units": {"noun": "task", "nouns": "tasks"}}
AGENT = {"key": "fake", "_kind": "agent", "_ref": "fake"}


def read(path):
    with open(path) as f:
        return json.load(f)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


class JobUid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-uid-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def config(self, d, name="run"):
        os.makedirs(d, exist_ok=True)
        job.write_config(d, job=name, bench=BENCH, other=AGENT, model="m", provider="openai", dataset="all",
                         units=["a"], concurrency=1, granted=True)
        return read(os.path.join(d, "config.json"))["standard"]["uuid"]

    def older_standard(self, root, started):
        """A standard job written before jobs carried a uuid: one trial, its result."""
        d = os.path.join(root, "run")
        write(os.path.join(d, "config.json"), {"job_name": "run", "runner": "standard",
                                               "standard": {"benchmark": "fakebench", "plugged": "fake"},
                                               "agents": [{"name": "fake", "model_name": "m"}]})
        write(os.path.join(d, "result.json"), {"id": "run", "started_at": started})
        write(os.path.join(d, "a__u0000", "result.json"), {"task_name": "a", "trial_name": "a__u0000"})
        return d

    def test_a_new_standard_job_declares_a_uuid_a_resume_into_its_folder_keeps(self):
        d = os.path.join(self.tmp, "run")
        first = self.config(d)
        self.assertEqual(self.config(d), first)                       # the same --name, the same folder
        self.assertNotEqual(self.config(os.path.join(self.tmp, "other")), first)
        j = parse.parse_job(d)
        self.assertEqual((j.uid, j.uid_source), (first, "declared"))

    def test_a_copy_or_a_move_keeps_the_identity_and_another_start_is_another_job(self):
        for name, make in (("harbor", lambda root: shutil.copytree(HARBOR, os.path.join(root, "liveboard-oracle-x4"))),
                           ("older standard", lambda root: self.older_standard(root, "2026-01-01T00:00:00Z"))):
            with self.subTest(name):
                base = os.path.join(self.tmp, name)
                src = make(os.path.join(base, "a"))
                j = parse.parse_job(src)
                self.assertEqual(j.uid_source, "derived")
                copy = shutil.copytree(src, os.path.join(base, "b", os.path.basename(src)))
                moved = os.path.join(base, "c", os.path.basename(src))
                os.makedirs(os.path.dirname(moved))
                os.rename(src, moved)
                self.assertEqual({parse.parse_job(copy).uid, parse.parse_job(moved).uid}, {j.uid})
                again = os.path.join(base, "d", os.path.basename(src))
                shutil.copytree(copy, again)
                res = read(os.path.join(again, "result.json"))
                write(os.path.join(again, "result.json"), dict(res, started_at="2026-02-02T00:00:00Z"))
                if os.path.exists(os.path.join(again, "events.jsonl")):
                    os.remove(os.path.join(again, "events.jsonl"))       # its start is then the result's
                self.assertNotEqual(parse.parse_job(again).uid, j.uid)

    def test_a_job_with_no_trial_and_no_start_is_pending(self):
        d = os.path.join(self.tmp, "preparing")
        write(os.path.join(d, "config.json"), {"job_name": "preparing", "agents": [{"name": "oracle"}]})
        j = parse.parse_job(d)
        self.assertEqual((j.uid, j.uid_source), ("", "pending"))

    def test_a_try_is_keyed_by_its_attempt(self):
        d = self.older_standard(self.tmp, "2026-01-01T00:00:00Z")
        t = parse.parse_job(d).trials[0]
        cfg = read(os.path.join(d, "config.json"))
        cfg["standard"]["retries"] = 1
        write(os.path.join(d, "config.json"), cfg)
        write(os.path.join(d, "retried", "a__u0000.try-1", "result.json"), {"task_name": "a", "trial_name": "a__u0000"})
        write(t.dir + "/result.json", {"task_name": "a", "trial_name": "a__u0000", "standard": {"retries": 1}})
        t = parse.parse_job(d).trials[0]
        self.assertEqual([(n, x.key) for n, x in parse.tries(t)], [(1, "a__u0000"), (2, "a__u0000")])


if __name__ == "__main__":
    unittest.main()
