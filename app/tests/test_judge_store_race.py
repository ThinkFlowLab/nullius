"""Two saves to one trial's judgement that overlap both land in the file."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from nullius.judge import store


class OverlappingSaves(unittest.TestCase):
    def test_two_overlapping_saves_keep_both_answers(self):
        ws = tempfile.mkdtemp(prefix="nullius-judge-race-")
        self.addCleanup(shutil.rmtree, ws, True)
        both_read = threading.Barrier(2, timeout=2)
        real_load = json.load

        def load_then_wait(fh, *a, **k):
            data = real_load(fh, *a, **k)
            try:
                both_read.wait()               # each save has read the file before either writes, unless a lock keeps them apart
            except threading.BrokenBarrierError:
                pass
            return data

        store.save(ws, "job", "t1", {"questions": "v1", "answers": {}})
        errors = []

        def save(question):
            try:
                store.save(ws, "job", "t1", {"questions": "v1", "answers": {question: "yes"}})
            except Exception as e:
                errors.append(repr(e))

        with mock.patch.object(store.json, "load", load_then_wait):
            threads = [threading.Thread(target=save, args=(q,)) for q in ("q1", "q2")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        answers = store.load(ws, "job", "t1", "v1")["answers"]
        self.assertEqual((errors, sorted(answers)), ([], ["q1", "q2"]))

    def test_saves_to_two_trials_do_not_wait_on_each_other(self):
        ws = tempfile.mkdtemp(prefix="nullius-judge-race-")
        self.addCleanup(shutil.rmtree, ws, True)
        for t in ("t1", "t2"):
            store.save(ws, "job", t, {"questions": "v1", "answers": {}})
        both_read = threading.Barrier(2, timeout=5)
        real_load = json.load

        def load_then_wait(fh, *a, **k):
            data = real_load(fh, *a, **k)
            both_read.wait()                   # raises if one save holds the other back
            return data

        errors = []

        def save(trial):
            try:
                store.save(ws, "job", trial, {"questions": "v1", "answers": {"q": "yes"}})
            except Exception as e:
                errors.append(repr(e))

        with mock.patch.object(store.json, "load", load_then_wait):
            threads = [threading.Thread(target=save, args=(t,)) for t in ("t1", "t2")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
