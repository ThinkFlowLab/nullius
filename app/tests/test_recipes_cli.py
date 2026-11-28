"""`nullius recipes launch --wait`: it asks each run's state until none is going, and a run paused on the page's
question ends the wait as an end does."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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


class Review(unittest.TestCase):
    def test_dry_run_uses_the_servers_provider_refusal(self):
        for refusal, code in (("Set the provider key in Settings.", 4), ("", 0)):
            answers = [(200, '<script id="nb-schema" type="application/json">{"local": true}</script>'),
                       (200, json.dumps({"errors": [], "provider_error": refusal})),
                       (200, '{"machines": [], "ready": true}')]
            with self.subTest(refusal=refusal), patch.object(cli, "find", return_value={"harness": "standard"}), \
                 patch.object(cli.Server, "send", side_effect=answers) as send, \
                 redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(["launch", "saved", "--dry-run", "--json"]), code)
                self.assertEqual(json.loads(out.getvalue())["refused"], refusal)
                self.assertEqual(err.getvalue().strip(), refusal)
                self.assertEqual([(c.args[0], c.args[1]) for c in send.call_args_list],
                                 [("GET", "/new-run"), ("POST", "/new-run/runner"), ("POST", "/new-run/prerequisites")])


class Scripted(cli.Server):
    """A server whose ``GET /run/<id>`` answers the states given for that run, one per call, the last one kept."""
    def __init__(self, states):
        super().__init__("http://localhost")
        self.states, self.asked = {k: list(v) for k, v in states.items()}, []

    def send(self, method, path, body=None):
        if (method, path) == ("GET", "/new-run"):
            return 200, '<script id="nb-schema" type="application/json">{"local": true}</script>'
        if (method, path) == ("POST", "/new-run/runner"):
            return 200, '{"errors": []}'
        if (method, path) == ("POST", "/new-run/prerequisites"):
            return 200, '{"machines": [], "ready": true}'
        if (method, path) == ("POST", "/run"):
            return 200, json.dumps({"ok": True, "id": "a", "runs": [
                {"id": rid, "host": "local", "jobs": f"jobs/{rid}", "state": "preparing"} for rid in self.states]})
        assert method == "GET" and path.startswith("/run/"), (method, path)
        rid = path.rsplit("/", 1)[1]
        self.asked.append(rid)
        left = self.states[rid]
        state = left.pop(0) if len(left) > 1 else left[0]
        return 200, '{"id": "%s", "state": "%s", "log": ["a line"]}' % (rid, state)


class Wait(unittest.TestCase):
    def test_the_command_waits_and_prints_the_terminal_states_and_exit_code(self):
        for state, code in (("done", 0), ("paused", 4), ("failed", 4), ("stopped", 4)):
            server = Scripted({"a": ["preparing", "running", "done"], "b": ["running", state]})
            with self.subTest(state=state), patch.object(cli, "find", return_value={"harness": "standard", "name": "saved"}), \
                 patch.object(cli, "Server", return_value=server), patch.object(cli.time, "sleep") as sleep, \
                 redirect_stdout(io.StringIO()) as out:
                result = cli.main(["launch", "saved", "--wait"])
                self.assertEqual(server.asked, ["a", "b", "a", "b", "a"])
                self.assertEqual(sleep.call_count, 2)
                self.assertEqual(out.getvalue().splitlines(), ["launch run:a", "run a on local: done", "  jobs jobs/a",
                                                             f"run b on local: {state}", "  jobs jobs/b"])
                self.assertEqual(result, code)

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
