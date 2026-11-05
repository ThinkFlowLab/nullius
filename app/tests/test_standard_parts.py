"""A standard trial scored over several parts weighs as many in the job's mean.

LoCoMo's Original setup scores a whole conversation at once, 81 to 199
questions each; the paper's number is over every question. The driver says
how many parts a score covers (``parts``), and the reader, which already
weighs by ``verifier_result.parts.n``, then gives the paper's number.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from nullius.runs import parse
from nullius.standard import job

BENCH = {"key": "locomo", "family": "capability", "needs": [], "score": {"kind": "score", "says": "mean token F1"}}


class Parts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-parts-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.job = os.path.join(self.tmp, "job"); os.makedirs(self.job)
        with open(os.path.join(self.job, "config.json"), "w") as f:
            json.dump({"job_name": "job", "runner": "standard"}, f)

    def trial(self, name, f1s):
        d = os.path.join(self.job, name)
        parts = {"n": len(f1s), "by": "category",
                 "parts": [{"n": i + 1, "text": f"q{i}", "group": 1 + i % 2, "score": s} for i, s in enumerate(f1s)]}
        job.write_trial(d, job="job", bench=BENCH, other={"key": "original", "_kind": "upstream"}, model="m", unit=name,
                        t0=0, t1=1, produce=(0, 1), score={"reward": sum(f1s) / len(f1s), "parts": parts, "detail": {}},
                        exception=None, calls=[], trajectory=None)
        return d

    def test_the_mean_is_over_every_question(self):
        self.trial("conv-a", [1.0] * 4)                 # 4 questions, all right
        self.trial("conv-b", [0.0] * 12)                # 12 questions, all wrong
        j = parse.parse_job(self.job)
        self.assertAlmostEqual(j.mean_score, 4 / 16)    # not (1.0 + 0.0) / 2
        self.assertEqual(sorted(g["group"] for g in j.score_by_group), ["1", "2"])
        with open(os.path.join(self.job, "conv-a", "verifier", "score.json")) as f:
            self.assertNotIn("parts", json.load(f))      # the parts live in parts.json, said once


if __name__ == "__main__":
    unittest.main()
