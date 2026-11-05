"""JiuwenSwarm's ACP wrapper: on ACP, JiuwenSwarm edits files only through the
client, and a client that offers none (Nullius on docker) leaves it with no
file at all - so the wrapper, in the agent's container, answers them itself,
inside the session's directory."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

from tests import APP

spec = importlib.util.spec_from_file_location(
    "nullius_jiuwenswarm_agent", os.path.join(APP, "nullius", "catalogue", "agents", "jiuwenswarm", "agent.py"))
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class Pipe(io.BytesIO):
    def close(self):                    # the wrapper closes the bridge's stdin at the end; the test reads it after
        pass


def lines(*msgs):
    return io.BytesIO(b"".join((json.dumps(m) + "\n").encode() for m in msgs))


def read(buf):
    return [json.loads(x) for x in buf.getvalue().decode().splitlines()]


class FilesWithoutTheClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-jw-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = os.path.join(self.tmp, "ws"); os.makedirs(self.ws)

    def session(self, fs: bool):
        ses, bridge = agent.Sessions(), Pipe()
        ses.agent_in = bridge
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"clientCapabilities": {"fs": {"readTextFile": fs, "writeTextFile": fs}}}}
        new = {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": self.ws, "mcpServers": []}}
        agent.mcp_into_config = lambda servers: None
        agent.to_agent(lines(init, new), bridge, ses)
        out = io.BytesIO()
        agent.to_client(lines({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}}), out, ses)
        return ses, bridge, out

    def test_a_client_without_files_gets_none_and_the_wrapper_serves_them(self):
        ses, bridge, _ = self.session(fs=False)
        self.assertTrue(ses.serve_fs)
        self.assertEqual(read(bridge)[0]["params"]["clientCapabilities"]["fs"], {"readTextFile": True, "writeTextFile": True})
        bridge.seek(0); bridge.truncate()
        out = io.BytesIO()
        agent.to_client(lines(
            {"jsonrpc": "2.0", "id": 7, "method": "fs/write_text_file", "params": {"sessionId": "x", "path": "result.txt", "content": "ok"}},
            {"jsonrpc": "2.0", "id": 8, "method": "fs/read_text_file", "params": {"sessionId": "x", "path": "result.txt"}},
            {"jsonrpc": "2.0", "id": 9, "method": "fs/read_text_file", "params": {"sessionId": "x", "path": "../outside"}}), out, ses)
        self.assertEqual(out.getvalue(), b"")                                          # nothing reached the client
        got = {m["id"]: m for m in read(bridge)}
        self.assertEqual(got[7]["result"], {})
        self.assertEqual(got[8]["result"], {"content": "ok"})
        self.assertIn("outside", got[9]["error"]["message"])
        with open(os.path.join(self.ws, "result.txt")) as f:
            self.assertEqual(f.read(), "ok")

    def test_a_client_with_files_still_serves_them(self):
        ses, bridge, _ = self.session(fs=True)
        self.assertFalse(ses.serve_fs)
        out = io.BytesIO()
        agent.to_client(lines({"jsonrpc": "2.0", "id": 7, "method": "fs/read_text_file",
                               "params": {"sessionId": "x", "path": "a.txt"}}), out, ses)
        self.assertEqual(read(out)[0]["params"], {"sessionId": "s1", "path": os.path.join(self.ws, "a.txt")})


if __name__ == "__main__":
    unittest.main()
