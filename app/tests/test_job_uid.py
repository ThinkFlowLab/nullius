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

    def config(self, d, name="run", model="m", dataset="all", other=AGENT):
        os.makedirs(d, exist_ok=True)
        job.write_config(d, job=name, bench=BENCH, other=other, model=model, provider="openai", dataset=dataset,
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

    def test_another_job_under_the_same_name_gets_a_uuid_of_its_own(self):
        d = os.path.join(self.tmp, "run")
        first = self.config(d)
        for what, kw in (("model", {"model": "m2"}), ("dataset", {"dataset": "hard"}),
                         ("plugged", {"other": dict(AGENT, key="other", _ref="other")})):
            with self.subTest(what):
                self.assertNotEqual(self.config(d, **kw), first)
                first = self.config(d)                          # back to the first job: a new one again
        self.assertEqual(self.config(d), first)

    def test_resuming_a_job_from_before_uuids_declares_the_identity_it_was_read_under(self):
        d = self.older_standard(self.tmp, "2026-01-01T00:00:00Z")
        cfg = read(os.path.join(d, "config.json"))
        write(os.path.join(d, "config.json"), dict(cfg, datasets=[{"name": "fakebench/all"}]))
        before = parse.parse_job(d)
        self.assertEqual(before.uid_source, "derived")
        self.assertEqual(self.config(d), before.uid)
        self.assertEqual((parse.parse_job(d).uid, parse.parse_job(d).uid_source), (before.uid, "declared"))
        with self.subTest("another job under its name"):
            other = self.older_standard(os.path.join(self.tmp, "b"), "2026-01-01T00:00:00Z")
            derived = parse.parse_job(other).uid
            self.assertNotEqual(self.config(other, model="m2"), derived)

    def test_a_harbor_jobs_identity_does_not_follow_the_feed(self):
        d = shutil.copytree(HARBOR, os.path.join(self.tmp, "h", "liveboard-oracle-x4"))
        feed = os.path.join(d, "events.jsonl")
        if os.path.exists(feed):
            os.remove(feed)
        without = parse.parse_job(d).uid
        with open(feed, "w") as f:
            f.write(json.dumps({"event": "job.start", "ts": "2020-01-01T00:00:00Z"}) + "\n")
        self.assertEqual(parse.parse_job(d).uid, without)

    def test_a_feed_that_is_not_utf8_is_read_not_raised(self):
        d = self.older_standard(self.tmp, "2026-01-01T00:00:00Z")
        with open(os.path.join(d, "events.jsonl"), "wb") as f:
            f.write(b"\xff\xfe not text\n" + json.dumps({"event": "job.start", "ts": "2026-01-01T00:00:01Z"}).encode() + b"\n")
        j = parse.parse_job(d)
        self.assertEqual(j.uid_source, "derived")

    def test_two_jobs_alike_but_for_their_folder_or_their_start_are_two_jobs(self):
        for name, make in (("harbor", lambda root: shutil.copytree(HARBOR, os.path.join(root, "liveboard-oracle-x4"))),
                           ("older standard", lambda root: self.older_standard(root, "2026-01-01T00:00:00Z"))):
            with self.subTest(name):
                src = make(os.path.join(self.tmp, name, "a"))
                renamed = shutil.copytree(src, os.path.join(self.tmp, name, "b", "renamed"))   # its files untouched
                self.assertNotEqual(parse.parse_job(renamed).uid, parse.parse_job(src).uid)
                later = shutil.copytree(src, os.path.join(self.tmp, name, "c", os.path.basename(src)))
                res = read(os.path.join(later, "result.json"))
                write(os.path.join(later, "result.json"), dict(res, started_at="2026-03-03T00:00:00Z"))
                if os.path.exists(os.path.join(later, "events.jsonl")):
                    os.remove(os.path.join(later, "events.jsonl"))
                self.assertNotEqual(parse.parse_job(later).uid, parse.parse_job(src).uid)

    def test_a_copy_or_a_move_keeps_the_identity(self):
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
