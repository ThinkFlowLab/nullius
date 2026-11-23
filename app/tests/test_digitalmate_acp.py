"""DigitalMate's ACP transport: one product instance per session, turns forwarded as they are."""
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nullius/catalogue/agents/digitalmate"))

from tests.test_digitalmate_gateway import stub_websockets  # noqa: E402

stub_websockets()
import acp  # noqa: E402
import run  # noqa: E402


class Peer:
    def __init__(self, install, cwd, *, mcp_servers_rows=None):
        acp.mcp_servers(mcp_servers_rows)
        self.cwd, self.rows = cwd, mcp_servers_rows
        self.turns = []
        self.cancelled = threading.Event()
        self.entered = threading.Event()
        self.block = False

    def ready(self):
        pass

    def prompt(self, text):
        self.turns.append(text)
        if self.block:
            self.entered.set()
            self.cancelled.wait(3)
            raise RuntimeError("Product stopped")
        return "product answer " + str(len(self.turns))

    def close(self):
        self.cancelled.set()


def socket_on(port):
    reservation = patch.object(acp.socket, "socket")
    mock = reservation.start()
    mock.return_value.__enter__.return_value.getsockname.return_value = ("127.0.0.1", port)
    return reservation


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.server = acp.Server(Path("/install"), self.output, Peer)

    def frames(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def request(self, method, **params):
        self.server.handle({"id": 1, "method": method, "params": params})
        return self.frames()[-1]

    def new(self, **params):
        return self.request("session/new", cwd="/workspace", **params)["result"]["sessionId"]

    def test_turns_keep_instance_and_forward_only_current_prompt(self):
        sid = self.new()
        for text in ("Remember code alpha.", "What was the code?"):
            reply = self.request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": text}])
            self.assertEqual(reply["result"]["stopReason"], "end_turn")
        self.assertEqual(self.server.sessions[sid].turns, ["Remember code alpha.", "What was the code?"])
        updates = [f for f in self.frames() if f.get("method") == "session/update"]
        self.assertEqual([f["params"]["update"]["content"]["text"] for f in updates],
                         ["product answer 1", "product answer 2"])
        other = self.new()
        self.assertIsNot(self.server.sessions[sid], self.server.sessions[other])
        self.assertEqual(self.server.sessions[other].turns, [])

    def test_stdio_mcp_servers_reach_the_session(self):
        rows = [{"name": "kit", "command": "python3", "args": ["bridge.py"], "env": []}]
        sid = self.new(mcpServers=rows)
        self.assertEqual(self.server.sessions[sid].rows, rows)

    def test_other_mcp_transports_are_refused_before_a_product_starts(self):
        result = self.request("session/new", cwd="/workspace",
                              mcpServers=[{"name": "web", "type": "http", "url": "http://127.0.0.1:1/mcp"}])
        self.assertEqual(result["error"]["code"], -32602)
        self.assertEqual(self.server.sessions, {})

    def test_nontext_prompt_is_not_silently_discarded(self):
        sid = self.new()
        result = self.request("session/prompt", sessionId=sid, prompt=[{"type": "image", "data": "x"}])
        self.assertIn("error", result)
        self.assertEqual(self.server.sessions[sid].turns, [])

    def test_cancel_stops_active_product_without_emitting_an_answer(self):
        sid = self.new()
        peer = self.server.sessions[sid]
        peer.block = True
        thread = threading.Thread(target=self.request, args=("session/prompt",),
                                  kwargs={"sessionId": sid, "prompt": [{"type": "text", "text": "task"}]})
        thread.start()
        self.assertTrue(peer.entered.wait(1))
        self.server.handle({"method": "session/cancel", "params": {"sessionId": sid}})
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.frames()[-1]["result"]["stopReason"], "cancelled")
        self.assertFalse(any(f.get("method") == "session/update" for f in self.frames()))

    def test_shutdown_stops_each_session(self):
        self.new()
        self.new()
        self.server.close()
        self.assertTrue(all(s.cancelled.is_set() for s in self.server.sessions.values()))


class SessionTests(unittest.TestCase):
    def test_session_binds_its_cwd_and_round_trips_turns(self):
        real_popen = subprocess.Popen
        code = ("import json,sys\nprint(json.dumps({'ready':True}),flush=True)\n"
                "for line in sys.stdin:\n print(json.dumps({'answer':json.loads(line)['prompt']}),flush=True)\n")
        seen = []

        def child(argv, **kwargs):
            seen.append(argv)
            return real_popen([sys.executable, "-u", "-c", code], **kwargs)

        reservation = socket_on(12345)
        try:
            with tempfile.TemporaryDirectory() as cwd, patch.object(acp.subprocess, "Popen", side_effect=child):
                session = acp.Session(Path("/install"), cwd)
                try:
                    session.ready()
                    self.assertEqual(session.prompt("first"), "first")
                    self.assertEqual(session.prompt("second"), "second")
                finally:
                    session.close()
                session.close()
                self.assertIsNotNone(session.process.poll())
                self.assertTrue(session.process.stdout.closed)
                argv = seen[0]
                self.assertEqual(argv[argv.index("--project") + 1], cwd)
                self.assertEqual(argv[argv.index("--port") + 1], "12345")
                self.assertIn("--session", argv)
                self.assertNotIn("--mcp-config", argv)
        finally:
            reservation.stop()

    def test_mcp_rows_go_to_a_private_config_that_is_removed(self):
        reservation = socket_on(1234)
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(acp.Path, "home", return_value=Path(tmp)), \
                    patch.object(acp.subprocess, "Popen") as start, patch.object(acp, "stop"):
                row = {"name": "kit", "command": "python3", "args": ["bridge.py"],
                       "env": [{"name": "NULLIUS_MCP_AT", "value": "fixture:123"}]}
                session = acp.Session(Path("/install"), tmp, mcp_servers_rows=[row])
                argv = start.call_args.args[0]
                config = Path(argv[argv.index("--mcp-config") + 1])
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(config.read_text()), {"cwd": tmp, "servers": [
                    {"name": "kit", "command": "python3", "args": ["bridge.py"],
                     "env": {"NULLIUS_MCP_AT": "fixture:123"}}]})
                session.close()
                self.assertFalse(config.exists())
        finally:
            reservation.stop()

    def test_relative_cwd_is_refused_before_a_product_starts(self):
        with patch.object(acp.subprocess, "Popen") as start:
            with self.assertRaisesRegex(ValueError, "absolute directory"):
                acp.Session(Path("/install"), "workspace")
            start.assert_not_called()


class ProductSessionTests(unittest.TestCase):
    def test_one_product_answers_every_line_of_a_session(self):
        from contextlib import contextmanager
        calls = []

        @contextmanager
        def product(install, **options):
            calls.append(("start", options))
            yield "token"
            calls.append(("stop", {}))

        output = io.StringIO()
        with patch.object(run, "product", product), \
                patch.object(run.sys, "stdin", io.StringIO('{"prompt":"first"}\n{"prompt":"second"}\n')), \
                patch.object(run.sys, "stdout", output), \
                patch.object(run, "submit_task", side_effect=["one", "two"]) as submit:
            run.session(Path("/install"), 12345, project=Path("/workspace"), mcp_config=Path("/private/servers.json"))
        self.assertEqual(calls, [("start", {"port": 12345, "project": Path("/workspace"),
                                            "mcp_config": Path("/private/servers.json")}), ("stop", {})])
        self.assertEqual([c.args for c in submit.call_args_list], [("first", "token"), ("second", "token")])
        self.assertEqual(submit.call_args.kwargs["url"], "ws://127.0.0.1:12345/digitalmate/ws")
        self.assertEqual([json.loads(x) for x in output.getvalue().splitlines()],
                         [{"ready": True}, {"answer": "one"}, {"answer": "two"}])


if __name__ == "__main__":
    unittest.main()
