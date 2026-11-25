"""The client of the HTTP API (``nullius.client``) against a server of this
test's own on the loopback: what each call sends - the token, the path, the
query, the body - what a refusal raises, how a run is followed; the ``nullius
remote`` command's output and exit codes; and the MCP tools over the same client
(``nullius.mcp_server``). The routes themselves are the route suite's
(``ui_tests/test_api.py``)."""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from nullius import cli, client, mcp_server

TOKEN = "the-test-token"


class Server:
    """Answers each request from ``answers`` ({(method, path): (status, document) | callable}) and keeps what it was asked."""

    def __init__(self):
        self.asked, self.answers, self.headers = [], {}, {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                path, _, query = self.path.partition("?")
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                asked = {"method": self.command, "path": path, "query": query, "auth": self.headers.get("Authorization"),
                         "body": json.loads(body) if body else None}
                outer.asked.append(asked)
                answer = outer.answers.get((self.command, path), (404, {"error": f"no {path}"}))
                status, doc = answer(asked) if callable(answer) else answer
                raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
                self.send_response(status)
                for name, value in outer.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            do_GET = do_POST = _serve

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Case(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.addCleanup(self.server.close)
        self.nx = client.Client(self.server.url, TOKEN)

    def answer(self, method, path, doc, status=200):
        self.server.answers[(method, "/api/v1" + path)] = (status, doc)

    def last(self):
        return self.server.asked[-1]


class Calls(Case):
    def test_every_call_carries_the_token_and_asks_its_route(self):
        for call, method, path, query, body in (
                (lambda: self.nx.info(), "GET", "", "", None),
                (lambda: self.nx.catalogue(), "GET", "/catalogue", "", None),
                (lambda: self.nx.workspaces(), "GET", "/workspaces", "", None),
                (lambda: self.nx.jobs(), "GET", "/jobs", "", None),
                (lambda: self.nx.jobs("nightly"), "GET", "/jobs", "workspace=nightly", None),
                (lambda: self.nx.job("run a/b"), "GET", "/jobs/run%20a%2Fb", "", None),
                (lambda: self.nx.live("j"), "GET", "/jobs/j/live", "", None),
                (lambda: self.nx.trial("j", "t__1"), "GET", "/jobs/j/trials/t__1", "", None),
                (lambda: self.nx.conversation("j", "t", page=2, q="apply patch"), "GET", "/jobs/j/trials/t/conversation", "page=2&q=apply+patch", None),
                (lambda: self.nx.conversation("j", "t", step=0), "GET", "/jobs/j/trials/t/conversation", "step=0", None),
                (lambda: self.nx.trace("j", "t"), "GET", "/jobs/j/trials/t/trace", "", None),
                (lambda: self.nx.artifacts("j", "t"), "GET", "/jobs/j/trials/t/artifacts", "", None),
                (lambda: self.nx.artifact("j", "t", "result"), "GET", "/jobs/j/trials/t/artifacts/result", "", None),
                (lambda: self.nx.compare("a", "b", matched=True), "GET", "/compare", "job=a&job=b&matched=1", None),
                (lambda: self.nx.compare("a", "b"), "GET", "/compare", "job=a&job=b", None),
                (lambda: self.nx.judgements("j"), "GET", "/jobs/j/judgements", "lane=decision", None),
                (lambda: self.nx.judgement("j", "t", lane="llm"), "GET", "/jobs/j/trials/t/judgement", "lane=llm", None),
                (lambda: self.nx.judge("j", "t"), "POST", "/jobs/j/trials/t/judge", "", {"lane": "decision"}),
                (lambda: self.nx.judge_job("j", lane="llm"), "POST", "/jobs/j/judge", "", {"lane": "llm"}),          # no yes unless asked for
                (lambda: self.nx.judge_job("j", confirm=True), "POST", "/jobs/j/judge", "", {"lane": "decision", "confirm": True}),
                (lambda: self.nx.recipes(), "GET", "/recipes", "", None),
                (lambda: self.nx.recipe("nightly"), "GET", "/recipes/nightly", "", None),
                (lambda: self.nx.save_recipe({"version": 1}), "POST", "/recipes", "", {"recipe": {"version": 1}, "id": None}),
                (lambda: self.nx.check("nightly"), "POST", "/runs/check", "", {"recipe": "nightly"}),
                (lambda: self.nx.check({"version": 1}, where="bench"), "POST", "/runs/check", "", {"recipe": {"version": 1}, "where": "bench"}),
                (lambda: self.nx.start("nightly"), "POST", "/runs", "", {"recipe": "nightly"}),      # no yes unless asked for
                (lambda: self.nx.start("nightly", where=["local", "bench"], name="n-1", confirm=True), "POST", "/runs", "",
                 {"recipe": "nightly", "where": ["local", "bench"], "name": "n-1", "confirm": True}),
                (lambda: self.nx.runs(), "GET", "/runs", "", None),
                (lambda: self.nx.run("r1"), "GET", "/runs/r1", "", None),
                (lambda: self.nx.run("r1", since=40), "GET", "/runs/r1", "since=40", None),
                (lambda: self.nx.stop("r1"), "POST", "/runs/r1/stop", "", None),
                (lambda: self.nx.continue_("r1"), "POST", "/runs/r1/continue", "", None)):
            self.answer(method, path, {"ok": path})
            self.assertEqual(call(), {"ok": path})
            self.assertEqual(self.last(), {"method": method, "path": "/api/v1" + path, "query": query,
                                           "auth": "Bearer " + TOKEN, "body": body})

    def test_a_refusal_raises_with_the_server_s_sentence_its_status_and_its_document(self):
        self.answer("POST", "/runs", {"error": "A machine lacks what this recipe needs: see machines.", "machines": [{"where": "local"}]}, 409)
        with self.assertRaises(client.ApiError) as c:
            self.nx.start("nightly")
        self.assertEqual((c.exception.status, str(c.exception)), (409, "A machine lacks what this recipe needs: see machines."))
        self.assertEqual(c.exception.document["machines"], [{"where": "local"}])
        self.server.answers[("GET", "/api/v1/jobs")] = (502, b"<html>bad gateway</html>")                   # not this server's JSON
        with self.assertRaises(client.ApiError) as c:
            self.nx.jobs()
        self.assertEqual((c.exception.status, c.exception.document), (502, {}))
        self.assertIn("HTTP 502", str(c.exception))

    def test_a_server_that_does_not_answer_is_an_error_with_status_zero(self):
        self.server.close()
        with self.assertRaises(client.ApiError) as c:
            client.Client(self.server.url, TOKEN, timeout=2).info()
        self.assertEqual(c.exception.status, 0)
        self.assertIn("did not answer", str(c.exception))
        self.server = Server()                                                                       # for the cleanup

    def test_a_redirect_is_refused_and_the_token_goes_nowhere_else(self):
        other = Server(); self.addCleanup(other.close)
        self.server.headers = {"Location": other.url + "/api/v1/runs"}
        for status in (301, 302, 303, 307, 308):
            self.answer("POST", "/runs", {}, status)
            with self.assertRaises(client.ApiError) as e:
                self.nx.start("nightly", confirm=True)
            self.assertEqual(e.exception.status, status)
            self.assertIn(other.url, str(e.exception))
        self.assertEqual(other.asked, [])

    def test_a_token_from_a_file_with_line_ends_and_one_that_is_no_token(self):
        self.assertEqual(client.Client("http://h", TOKEN + "\r\n").token, TOKEN)
        with self.assertRaises(ValueError) as e:
            client.Client("http://h", "nls_a\r\nb")
        self.assertNotIn("nls_a", str(e.exception))

    def test_what_is_not_an_http_answer_is_an_error_too(self):
        for url in ("http://127.0.0.1:abc", "http://a b"):
            with self.assertRaises(client.ApiError) as e:
                client.Client(url, TOKEN).jobs()
            self.assertEqual(e.exception.status, 0, url)
        self.answer("GET", "/jobs", b"<html>")
        with self.assertRaises(client.ApiError):
            self.nx.jobs()

    def test_the_address_and_token_come_from_the_environment_or_are_asked_for(self):
        with mock.patch.dict(os.environ, {"NULLIUS_SERVER": self.server.url + "/", "NULLIUS_TOKEN": "from-env"}):
            nx = client.Client()
        self.assertEqual((nx.url, nx.token), (self.server.url, "from-env"))
        with mock.patch.dict(os.environ):
            os.environ.pop("NULLIUS_SERVER", None), os.environ.pop("NULLIUS_TOKEN", None)
            for url, token in ((None, "t"), ("bench:5057", "t"), (self.server.url, None)):
                with self.assertRaises(ValueError, msg=(url, token)):
                    client.Client(url, token)

    def test_the_client_is_one_file_of_the_standard_library(self):
        """It works copied alone to a machine without Nullius: it imports nothing else of the package."""
        tree = ast.parse(Path(client.__file__).read_text())
        names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} \
            | {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(names, {"__future__", "argparse", "http", "json", "os", "sys", "time", "typing", "urllib"})


class Following(Case):
    def run_of(self, states):
        """A run whose log grows by one line a call, through ``states``."""
        calls = []

        def answer(asked):
            since = int(dict(p.split("=") for p in asked["query"].split("&") if p).get("since", 0))
            i = min(len(calls), len(states) - 1)
            calls.append(since)
            state, question = states[i] if isinstance(states[i], tuple) else (states[i], None)
            lines = [f"line {n}" for n in range(i + 1)]
            return 200, {"id": "r1", "state": state, "question": question, "lines": len(lines), "log": lines[since:]}
        self.server.answers[("GET", "/api/v1/runs/r1")] = answer
        return calls

    def test_follow_yields_each_line_once_until_the_run_ends(self):
        calls = self.run_of(["preparing", "running", "running", "done"])
        self.assertEqual(list(self.nx.follow("r1", every=0)), ["line 0", "line 1", "line 2", "line 3"])
        self.assertEqual(calls, [0, 1, 2, 3])                                 # each call asks from where the last ended

    def test_follow_and_wait_return_at_a_question(self):
        self.run_of(["running", ("paused", "nop exited 1. Continue with oracle, or stop?")])
        self.assertEqual(list(self.nx.follow("r1", every=0)), ["line 0", "line 1"])
        self.run_of(["running", ("paused", "nop exited 1. Continue with oracle, or stop?")])
        got = self.nx.wait("r1", every=0)
        self.assertEqual((got["state"], got["log"]), ("paused", []))           # the summary, without the log again

    def test_wait_returns_the_ended_run_or_times_out(self):
        self.run_of(["running", "running", "failed"])
        self.assertEqual(self.nx.wait("r1", every=0)["state"], "failed")
        self.run_of(["running"])
        with self.assertRaises(TimeoutError):
            self.nx.wait("r1", every=0.01, timeout=0.05)


class Remote(Case):
    """``nullius remote``: one JSON document on stdout, the rest on stderr, the exit codes of ``docs/cli/exit-codes.md``."""

    def remote(self, *argv, env=True):
        out, err = io.StringIO(), io.StringIO()
        vars_ = {"NULLIUS_SERVER": self.server.url, "NULLIUS_TOKEN": TOKEN} if env else {}
        with mock.patch.dict(os.environ, vars_), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if not env:
                os.environ.pop("NULLIUS_SERVER", None), os.environ.pop("NULLIUS_TOKEN", None)
            code = cli.main(["remote", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_a_read_prints_the_server_s_document(self):
        self.answer("GET", "/jobs", [{"job_id": "a"}])
        self.answer("GET", "/jobs/a", {"job_id": "a", "trials": []})
        self.answer("GET", "/jobs/a/trials/t", {"trial": {"trial_name": "t"}})
        self.answer("GET", "/compare", {"columns": []})
        self.assertEqual(self.remote("jobs")[:2], (0, json.dumps([{"job_id": "a"}], indent=1) + "\n"))
        self.assertEqual(json.loads(self.remote("job", "a")[1]), {"job_id": "a", "trials": []})
        self.assertEqual(json.loads(self.remote("job", "a", "t")[1]), {"trial": {"trial_name": "t"}})
        self.assertEqual(self.remote("compare", "a", "b", "--matched")[0], 0)
        self.assertEqual(self.last()["query"], "job=a&job=b&matched=1")

    def test_the_flags_name_the_server_too(self):
        self.answer("GET", "", {"api": 1})
        code, out, _ = self.remote("--server", self.server.url, "--token", TOKEN, "info", env=False)
        self.assertEqual((code, json.loads(out)), (0, {"api": 1}))
        code, out, err = self.remote("info", env=False)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("NULLIUS_SERVER", err)

    def test_not_found_is_three_and_a_refusal_is_four_with_its_document(self):
        code, out, err = self.remote("job", "nope")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("no /api/v1/jobs/nope", err)
        self.answer("POST", "/runs", {"error": "Run all problems of GSM8K? Send \"confirm\": true to start it.", "confirm": "Run all problems of GSM8K?"}, 409)
        code, out, err = self.remote("run", "nightly")
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["confirm"], "Run all problems of GSM8K?")
        self.assertIn("Run all problems of GSM8K?", err)

    def test_run_sends_a_recipe_file_or_a_saved_name_and_the_yes_only_when_given(self):
        self.answer("POST", "/runs", {"runs": [{"id": "r1", "state": "preparing"}]})
        self.remote("run", "nightly")
        self.assertEqual(self.last()["body"], {"recipe": "nightly"})
        recipe = {"version": 1, "harness": "standard", "name": "g"}
        with tempfile.TemporaryDirectory() as tmp:
            for doc in (recipe, {"id": "0" * 16, "name": "G", "saved": "2026-01-01T00:00:00Z", "recipe": recipe}):    # as exported, or as the store keeps it
                path = os.path.join(tmp, "r.json")
                with open(path, "w") as fh:
                    json.dump(doc, fh)
                self.remote("run", path, "--where", "bench", "--name", "g-1", "--yes")
                self.assertEqual(self.last()["body"], {"recipe": recipe, "where": "bench", "name": "g-1", "confirm": True})
            with open(path, "w") as fh:
                fh.write("{not json")
            self.assertEqual(self.remote("run", path)[0], 2)

    def test_judge_asks_one_trial_or_a_job_and_the_job_only_with_yes(self):
        self.answer("POST", "/jobs/a/trials/t/judge", {"answers": {}})
        self.answer("POST", "/jobs/a/judge", {"error": "This asks the judge about 4 trial(s), a paid call each.", "todo": 4}, 409)
        self.assertEqual(self.remote("judge", "a", "t", "--lane", "llm")[0], 0)
        self.assertEqual(self.last()["body"], {"lane": "llm"})
        code, out, _ = self.remote("judge", "a")
        self.assertEqual((code, json.loads(out)["todo"], self.last()["body"]), (4, 4, {"lane": "decision"}))
        self.remote("judge", "a", "--yes")
        self.assertEqual(self.last()["body"], {"lane": "decision", "confirm": True})
        self.answer("GET", "/jobs/a/judgements", {"judged": 0, "records": {}})
        self.assertEqual(self.remote("judgements", "a")[0], 0)

    def test_run_follow_prints_the_log_and_exits_by_how_the_run_ended(self):
        for state, code in (("done", 0), ("failed", 4)):
            self.answer("POST", "/runs", {"runs": [{"id": "r1", "state": "preparing"}]})
            self.answer("GET", "/runs/r1", {"id": "r1", "state": state, "question": None, "lines": 2, "log": ["one", "two"]})
            got, out, err = self.remote("run", "nightly", "--follow")
            self.assertEqual(got, code)
            self.assertEqual(json.loads(out)["runs"][0]["state"], state)
            self.assertIn("one\ntwo\n", err)


class McpTools(Case):
    def call(self, name, arguments=None, rid=1):
        return mcp_server.handle({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}, self.nx)

    def test_it_initialises_and_lists_its_tools(self):
        got = mcp_server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, self.nx)
        self.assertEqual(got["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(got["result"]["capabilities"], {"tools": {}})
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.nx))
        tools = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.nx)["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, set(mcp_server.TOOLS))
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])
            self.assertLessEqual(set(t["inputSchema"]["required"]), set(t["inputSchema"]["properties"]), t["name"])
        self.assertEqual(mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "nope"}, self.nx)["error"]["code"], -32601)

    def test_a_tool_is_one_call_of_the_client(self):
        self.answer("GET", "/jobs/a", {"job_id": "a", "adjusted_solve_rate": 0.5})
        got = self.call("get_job", {"job_id": "a"})["result"]
        self.assertEqual((got["isError"], json.loads(got["content"][0]["text"])), (False, {"job_id": "a", "adjusted_solve_rate": 0.5}))
        self.answer("GET", "/compare", {"columns": []})
        self.call("compare_jobs", {"job_ids": ["a", "b"], "matched": True})
        self.assertEqual(self.last()["query"], "job=a&job=b&matched=1")
        self.answer("POST", "/runs/check", {"ready": True, "confirm": "Run all?"})
        self.call("check_run", {"recipe": "nightly", "where": "bench"})
        self.assertEqual(self.last()["body"], {"recipe": "nightly", "where": "bench"})

    def test_start_run_says_yes_only_when_told(self):
        self.answer("POST", "/runs", {"runs": []})
        self.call("start_run", {"recipe": "nightly"})
        self.assertEqual(self.last()["body"], {"recipe": "nightly"})
        self.call("start_run", {"recipe": "nightly", "confirm": True})
        self.assertEqual(self.last()["body"], {"recipe": "nightly", "confirm": True})
        asked = len(self.server.asked)
        for yes in ("false", "true", 1, [0]):                                  # only true is a yes
            self.assertEqual(self.call("start_run", {"recipe": "nightly", "confirm": yes})["error"]["code"], -32602, yes)
        self.assertEqual(len(self.server.asked), asked)

    def test_a_malformed_message_is_answered_and_the_next_one_too(self):
        lines = ["not json", "[]", '{"jsonrpc":"2.0","id":1,"result":{}}']
        lines += [json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": p})
                  for p in ([1], {"name": []}, {"name": "get_job", "arguments": "a"}, {"name": "get_job", "arguments": [1]},
                            {"name": "compare_jobs", "arguments": {"job_ids": 7}}, {"name": "compare_jobs", "arguments": {"job_ids": "ab"}})]
        lines.append(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}))
        lines.append(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {"protocolVersion": {}}}))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"NULLIUS_SERVER": self.server.url, "NULLIUS_TOKEN": TOKEN}), \
                mock.patch("sys.stdin", io.StringIO("\n".join(lines))), contextlib.redirect_stdout(out):
            self.assertEqual(mcp_server.main([]), 0)
        got = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([g.get("error", {}).get("code") for g in got], [-32700, -32600] + [-32602] * 6 + [None, None])
        self.assertEqual(got[-1]["result"]["protocolVersion"], mcp_server.PROTOCOLS[0])
        self.assertEqual(self.server.asked, [])

    def test_a_refusal_an_unknown_tool_and_a_missing_argument(self):
        self.answer("POST", "/runs", {"error": "This token's scope is read: this needs run."}, 403)
        got = self.call("start_run", {"recipe": "nightly"})["result"]
        self.assertTrue(got["isError"])
        self.assertEqual(json.loads(got["content"][0]["text"])["error"], "This token's scope is read: this needs run.")
        self.assertEqual(self.call("delete_everything")["error"]["code"], -32602)
        asked = len(self.server.asked)
        got = self.call("get_trial", {"job_id": "a"})["result"]
        self.assertTrue(got["isError"])
        self.assertIn("get_trial needs trial", got["content"][0]["text"])
        self.assertEqual(len(self.server.asked), asked)                       # nothing was asked of the server


if __name__ == "__main__":
    unittest.main()
