"""`nullius recipes launch --wait`: it asks each run's state until none is going, and a run paused on the page's
question ends the wait as an end does."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from nullius.recipes import cli


class Scripted(cli.Server):
    """A server whose ``GET /run/<id>`` answers the states given for that run, one per call, the last one kept."""
    def __init__(self, states):
        super().__init__("http://localhost")
        self.states, self.asked = {k: list(v) for k, v in states.items()}, []

    def send(self, method, path, body=None):
        rid = path.rsplit("/", 1)[1]
        self.asked.append(rid)
        left = self.states[rid]
        state = left.pop(0) if len(left) > 1 else left[0]
        return 200, '{"id": "%s", "state": "%s", "log": ["a line"]}' % (rid, state)


class Wait(unittest.TestCase):
    def test_it_waits_until_no_run_is_going_and_a_pause_ends_it(self):
        server = Scripted({"a": ["preparing", "running", "done"], "b": ["running", "paused"]})
        with patch.object(cli.time, "sleep", lambda s: None):
            ended = cli.wait(server, ["a", "b"])
        self.assertEqual({k: v["state"] for k, v in ended.items()}, {"a": "done", "b": "paused"})
        self.assertNotIn("log", ended["a"])
        self.assertEqual(server.asked, ["a", "b", "a", "b", "a"])      # an ended run is not asked again

    def test_a_route_that_does_not_answer_the_run_refuses(self):
        class Gone(cli.Server):
            def send(self, method, path, body=None):
                return 404, "{}"
        with self.assertRaises(cli.Refused):
            cli.wait(Gone("http://localhost"), ["a"])


if __name__ == "__main__":
    unittest.main()
