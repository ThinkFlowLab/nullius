"""JiuwenSwarm's ACP wrapper: on ACP, JiuwenSwarm edits files only through the
client, and a client that offers none (Nullius on docker) leaves it with no
file at all - so the wrapper, in the agent's container, answers them itself,
inside the workspace or the unit's own JiuwenSwarm home."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

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


class RuntimeSocket(unittest.TestCase):
    def test_each_unit_gets_a_short_private_socket_directory_that_is_cleaned(self):
        paths = []

        def run(path):
            paths.append(path)
            self.assertLess(len(path.encode()), 104)
            self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(path)
            if len(paths) == 1:
                agent.main()
            return 0

        with patch.object(agent, "HOME", "/long/" + "job/" * 50), patch.object(agent, "run", side_effect=run):
            self.assertEqual(agent.main(), 0)
        self.assertNotEqual(paths[0], paths[1])
        for path in paths:
            self.assertFalse(os.path.exists(os.path.dirname(path)))

    def test_runtime_directory_is_cleaned_when_startup_fails(self):
        paths = []

        def fail(path):
            paths.append(path)
            raise RuntimeError("startup failed")

        with patch.object(agent, "run", side_effect=fail), self.assertRaisesRegex(RuntimeError, "startup failed"):
            agent.main()
        self.assertFalse(os.path.exists(os.path.dirname(paths[0])))

    def test_older_versions_without_a_worker_need_no_shutdown_call(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(agent.subprocess, "run") as run:
            agent.stop_worker({"JIUWENSWARM_RUNTIME_SOCKET": os.path.join(directory, "missing.sock")})
        run.assert_not_called()


class FilesWithoutTheClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-jw-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = os.path.join(self.tmp, "ws"); os.makedirs(self.ws)
        self.jhome = os.path.join(self.tmp, "home", ".jiuwenswarm"); os.makedirs(self.jhome)
        home = patch.object(agent, "JHOME", self.jhome)
        home.start(); self.addCleanup(home.stop)

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

    def test_unit_home_config_and_todos_on_both_backends(self):
        config = os.path.join(self.jhome, "config", "config.yaml")
        os.makedirs(os.path.dirname(config))
        with open(config, "w") as stream:
            stream.write("language: en\n")
        todo = os.path.join(self.jhome, "todos", "task.txt")
        for fs in (False, True):
            with self.subTest(client_files=fs):
                ses, bridge, out = self.session(fs)
                bridge.seek(0); bridge.truncate()
                out.seek(0); out.truncate()
                agent.to_client(lines(
                    {"jsonrpc": "2.0", "id": 11, "method": "fs/read_text_file",
                     "params": {"sessionId": "x", "path": config}},
                    {"jsonrpc": "2.0", "id": 12, "method": "fs/write_text_file",
                     "params": {"sessionId": "x", "path": todo, "content": "pending"}},
                    {"jsonrpc": "2.0", "id": 13, "method": "fs/read_text_file",
                     "params": {"sessionId": "x", "path": todo}}), out, ses)
                self.assertEqual(out.getvalue(), b"")
                replies = {m["id"]: m for m in read(bridge)}
                self.assertEqual(replies[11]["result"], {"content": "language: en\n"})
                self.assertEqual(replies[12]["result"], {})
                self.assertEqual(replies[13]["result"], {"content": "pending"})

    def test_home_access_does_not_expose_other_files(self):
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w") as stream:
            stream.write("private")
        os.symlink(outside, os.path.join(self.jhome, "escape"))
        os.symlink(self.tmp, os.path.join(self.jhome, "outside-directory"))
        for path in (outside, os.path.join(self.jhome, "..", "outside.txt"),
                     self.jhome + "-other/file", os.path.join(self.jhome, "escape"),
                     os.path.join(self.jhome, "outside-directory", "new.txt")):
            for method in ("fs/read_text_file", "fs/write_text_file"):
                with self.subTest(path=path, method=method):
                    error, result = agent.serve_fs({"method": method,
                        "params": {"path": path, "content": "changed"}}, self.ws)
                    self.assertEqual(error["code"], -32602)
                    self.assertIsNone(result)
        with open(outside) as stream:
            self.assertEqual(stream.read(), "private")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "new.txt")))


if __name__ == "__main__":
    unittest.main()
