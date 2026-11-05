"""A capability's reply in its trajectory: whole, its ``trace`` with it.

A capability may say what its library did beside the port's fields (Mem0: the
memories it wrote, each hit's score); a recall of twenty hits with their score
details runs past ten thousand characters, and a cut reply is no longer JSON.
"""
from __future__ import annotations

import json
import unittest

from nullius.standard import job


class CapabilityTrace(unittest.TestCase):
    def test_a_long_reply_with_its_trace_is_kept_whole(self):
        hits = [{"id": f"m{i}", "score": 0.5, "score_details": {"semantic_score": 0.5, "bm25_score": 0.1}, "session": "s1"}
                for i in range(200)]
        reply = {"memory": "a fact\n" * 100, "trace": {"hits": hits, "held": 200, "warnings": []}}
        t = job.trajectory_from_exchanges([{"op": "recall", "request": {"question": "q"}, "reply": reply, "t0": 0, "t1": 1}],
                                          [], {"key": "mem0"}, "m", "job/u")
        content = t["steps"][0]["observation"]["results"][0]["content"]
        self.assertGreater(len(content), 8000)
        self.assertEqual(json.loads(content), reply)


if __name__ == "__main__":
    unittest.main()
