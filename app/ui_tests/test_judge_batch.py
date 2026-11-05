"""A judge batch asks several trials at once, in their order, and stops on a refused key.

Judge all, a comparison's sides and task instructions asked one trial at a
time inside the click's request: a 500-trial job at 2-5 s a call took most
of an hour.
"""
from __future__ import annotations

import threading
import time
import unittest

from nullius.judge import client
from .test_routes import web  # noqa: F401  - the app, so the web package imports as the routes do

from web import judge


class RunBatch(unittest.TestCase):
    def test_calls_overlap_and_the_count_and_order_hold(self):
        live, most = [0], [0]
        lock = threading.Lock()

        def ask(i):
            with lock:
                live[0] += 1; most[0] = max(most[0], live[0])
            time.sleep(0.1)
            with lock:
                live[0] -= 1
            if i == 3:
                raise client.JudgeError(500, "busy")
        t0 = time.monotonic()
        done, failed = judge.run_batch(list(range(16)), ask, lambda i: f"t{i}")
        self.assertEqual((done, failed), (15, ["t3: busy"]))
        self.assertGreater(most[0], 1)
        self.assertLess(time.monotonic() - t0, 16 * 0.1 / 2)

    def test_a_refused_key_starts_no_further_call(self):
        asked = []

        def ask(i):
            asked.append(i)
            raise client.JudgeError(401, "refused")
        done, failed = judge.run_batch(list(range(100)), ask, str)
        self.assertEqual(done, 0)
        self.assertLessEqual(len(asked), judge.CONCURRENCY)       # only those already started

    def test_a_judge_still_busy_after_its_retries_stops_the_batch(self):
        asked = []

        def ask(i):
            asked.append(i)
            raise client.JudgeError(429, "busy")
        judge.run_batch(list(range(100)), ask, str)
        self.assertLessEqual(len(asked), judge.CONCURRENCY)


if __name__ == "__main__":
    unittest.main()
