"""Two saves to one trial's judgement that overlap both land in the file."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from nullius import ondisk
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

    def test_a_save_waits_for_the_file_lock_another_process_would_hold(self):
        base = tempfile.mkdtemp(prefix="nullius-judge-lock-")
        self.addCleanup(shutil.rmtree, base, True)
        store.save(base, "job", "t1", {"questions": "v1", "answers": {}})
        done = threading.Event()
        save = threading.Thread(target=lambda: (store.save(base, "job", "t1", {"questions": "v1", "answers": {"q": 1}}), done.set()))
        with ondisk.locked(os.path.join(base, "judgements", "job", ".lock")):        # not this process's thread lock: a file's
            save.start()
            self.assertFalse(done.wait(0.3), "the save did not wait for the lock")
        save.join(2)
        self.assertEqual(store.load(base, "job", "t1", "v1")["answers"], {"q": 1})


if __name__ == "__main__":
    unittest.main()
