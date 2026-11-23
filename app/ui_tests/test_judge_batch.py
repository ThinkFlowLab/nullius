"""A judge batch asks several trials at once, in their order, and stops on a refused key.
"""
from __future__ import annotations

import threading
import unittest

from nullius.judge import client
from .test_routes import web  # noqa: F401  - the app, so the web package imports as the routes do

from web import judge


class RunBatch(unittest.TestCase):
    def test_calls_overlap_and_the_count_and_order_hold(self):
        # Each call waits until CONCURRENCY calls are in flight at once, twice over n items.
        # Serial calls break the barrier at its timeout, which only bounds a hang.
        n = 2 * judge.CONCURRENCY
        in_flight = threading.Barrier(judge.CONCURRENCY, timeout=10)

        def ask(i):
            in_flight.wait()
            if i == 3:
                raise client.JudgeError(500, "busy")
        done, failed = judge.run_batch(list(range(n)), ask, lambda i: f"t{i}")
        self.assertEqual((done, failed), (n - 1, ["t3: busy"]))

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
