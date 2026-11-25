"""`nullius recipes launch --wait`: it asks each run's state until none is going, and a run paused on the page's
question ends the wait as an end does."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from nullius.recipes import cli, store


class Selection(unittest.TestCase):
    def test_exact_id_then_display_name_then_internal_name(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"NULLIUS_HOME": home}), \
             patch.object(store.time, "strftime", side_effect=["2000-01-01", "2000-01-02", "2000-01-03"]):
            first = store.save({"version": 1, "harness": "standard", "name": "quick-check", "title": "quick-check", "limit": 1})
            second = store.save({"version": 1, "harness": "standard", "name": "quick-check", "title": "Quick check", "limit": 2})
            third = store.save({"version": 1, "harness": "standard", "name": "other", "title": first["id"]})
            self.assertEqual(cli.find("quick-check"), first["recipe"])
            self.assertEqual(cli.find("Quick check"), second["recipe"])
            self.assertEqual(cli.find(first["id"]), first["recipe"])
            self.assertEqual(cli.find("other"), third["recipe"])

    def test_ambiguity_at_any_step_lists_the_matches_and_launches_nothing(self):
        for key in ("id", "name", "internal"):
            items = [{"id": "a", "name": "First", "recipe": {"name": "first"}},
                     {"id": "b", "name": "Second", "recipe": {"name": "second"}}]
            for item in items:
                if key == "internal":
                    item["recipe"]["name"] = "match"
                else:
                    item[key] = "match"
            with self.subTest(key=key), patch.object(store, "listing", return_value=items), \
                 patch.object(cli, "Server") as server, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(["launch", "match"]), 4)
                self.assertIn("ambiguous recipe", err.getvalue())
                for item in items:
                    self.assertIn(f"{item['name']!r} ({item['id']})", err.getvalue())
                server.assert_not_called()


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
