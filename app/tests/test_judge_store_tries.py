"""A retry's judgement is kept beside the earlier try's, not over it."""
from __future__ import annotations

import shutil
import tempfile
import unittest

from nullius.judge import questions as Q, store


def rec(choice, q="q"):
    return {"questions": Q.VERSION, "answers": {q: {"choice": choice}}, "judge": {"model": "m"}}


class Tries(unittest.TestCase):
    S1, S2 = "2026-01-01T00:00:00", "2026-01-01T01:00:00"

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="nullius-judge-tries-")
        self.addCleanup(shutil.rmtree, self.ws, True)

    def test_judging_a_retry_keeps_the_earlier_try_judgement(self):
        store.save(self.ws, "job", "t1", rec("first try"), started=self.S1)
        store.save(self.ws, "job", "t1", rec("retry"), started=self.S2)
        self.assertEqual(store.load(self.ws, "job", "t1", started=self.S2)["answers"]["q"]["choice"], "retry")
        first = store.load(self.ws, "job", "t1", started=self.S1)
        self.assertIsNotNone(first, "the first try's judgement is gone")
        self.assertEqual(first["answers"]["q"]["choice"], "first try")
        self.assertEqual(store.for_job(self.ws, "job", {"t1": self.S1})["t1"]["answers"]["q"]["choice"], "first try")
        self.assertEqual(store.for_job(self.ws, "job", {"t1": self.S2})["t1"]["answers"]["q"]["choice"], "retry")

    def test_judging_an_earlier_try_again_merges_into_its_own_record(self):
        store.save(self.ws, "job", "t1", rec("first try"), started=self.S1)
        store.save(self.ws, "job", "t1", rec("retry"), started=self.S2)
        store.save(self.ws, "job", "t1", rec("later", q="r"), started=self.S1)
        self.assertEqual(sorted(store.load(self.ws, "job", "t1", started=self.S1)["answers"]), ["q", "r"])
        self.assertEqual(store.load(self.ws, "job", "t1", started=self.S2)["answers"], {"q": {"choice": "retry"}})


if __name__ == "__main__":
    unittest.main()
