"""JiuwenSwarm's ACP wrapper: on ACP, JiuwenSwarm edits files only through the
client, and a client that offers none (Nullius on docker) leaves it with no
file at all - so the wrapper, in the agent's container, answers them itself,
inside the workspace or the unit's own JiuwenSwarm home."""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import io
import json
import os
import shutil
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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


class McpConfiguration(unittest.TestCase):
    def test_waits_for_upstream_writer_and_preserves_its_update(self):
        # JSON is also YAML: keep this suite independent of the agent's
        # packages while exercising the whole read/modify/replace boundary.
        yaml = SimpleNamespace(safe_load=json.load,
                               safe_dump=lambda data, stream, **kw: json.dump(data, stream))
        attempted, finished = threading.Event(), threading.Event()
        mutex = threading.Lock()
        errors, locks = [], []

        @contextmanager
        def lock(path, timeout):
            locks.append((path, timeout))
            attempted.set()
            with mutex:
                yield

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.yaml")
            with open(path, "w") as f:
                json.dump({"mcp": {"servers": []}, "startup": "old"}, f)
            servers = [{"name": "kit", "command": "python3", "args": ["kit.py"],
                        "env": [{"name": "KIT_SEED", "value": "test"}]}]

            def write():
                try:
                    agent.mcp_into_config(servers)
                except BaseException as e:
                    errors.append(e)
                finally:
                    finished.set()

            with patch.object(agent, "CONFIG", directory), \
                    patch.dict("sys.modules", {"yaml": yaml, "portalocker": SimpleNamespace(Lock=lock)}):
                mutex.acquire()          # upstream startup has read an old snapshot
                worker = threading.Thread(target=write, daemon=True)
                worker.start()
                try:
                    self.assertTrue(attempted.wait(2), "wrapper did not acquire upstream's lock")
                    self.assertFalse(finished.is_set())
                    with open(path, "w") as f:
                        json.dump({"mcp": {"servers": []}, "startup": "updated"}, f)
                finally:
                    mutex.release()
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
            self.assertEqual(locks, [(os.path.join(directory, "config.lock"), 10)])
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["startup"], "updated")
            self.assertEqual(data["mcp"]["servers"], [{"name": "kit", "enabled": True,
                             "transport": "stdio", "command": "python3", "args": ["kit.py"],
                             "env": {"KIT_SEED": "test"}}])


class SearchOption(unittest.TestCase):
    def test_vendor_default_range_and_environment_are_version_specific(self):
        from nullius.standard.contract import definitions
        from nullius.standard.runner import run
        path = os.path.join(APP, "nullius", "catalogue", "agents", "jiuwenswarm", "agent.json")
        with open(path) as f:
            raw = json.load(f)
        current = definitions.resolve(raw, "0.2.9")
        current["_here"] = os.path.dirname(path)
        option = current["options"]["web_max_fetches"]
        self.assertEqual(option["default"], "3")
        for value in ("1", "3", "6", "100"):
            self.assertFalse(definitions.option_problem(option, value))
        for value in ("0", "101", "-1", "1.5", "6; echo bad"):
            self.assertTrue(definitions.option_problem(option, value))
        for version in ("0.2.7", "0.2.8"):
            self.assertNotIn("web_max_fetches", definitions.resolve(raw, version)["options"])
        with tempfile.TemporaryDirectory() as home:
            for value in ("3", "6"):
                env = run.agent_env(current, "/install", home, "http://127.0.0.1:1", "fixture",
                                    {"reasoning_effort": "default", "web_max_fetches": value})
                self.assertEqual(env["WEB_RESEARCH_MAX_FETCHES"], value)


class RuntimeSocket(unittest.TestCase):
    def test_startup_uses_the_installed_acp_entry_point(self):
        # Upstream moved the bridge module into agent_gateway in 0.2.9;
        # its installed command remains the same across supported versions.
        server = Mock()
        server.poll.return_value = 0
        bridge = Mock()
        bridge.wait.return_value = 0
        bridge.poll.return_value = 0
        with tempfile.TemporaryDirectory() as home, \
                patch.object(agent, "HOME", home), \
                patch.object(agent, "PY", "/install/.venv/bin/python"), \
                patch.object(agent.os.path, "isfile", return_value=True), \
                patch.object(agent, "reasoning_level"), \
                patch.object(agent, "write_env"), \
                patch.object(agent, "free_ports", return_value=[8001, 8002, 8003, 8004]), \
                patch.object(agent, "listening", return_value=True), \
                patch.object(agent, "stop_worker"), \
                patch.object(agent.signal, "signal"), \
                patch.object(agent.threading, "Thread"), \
                patch.object(agent.subprocess, "Popen", side_effect=[server, bridge, server, bridge]) as start:
            self.assertEqual(agent.run("/tmp/test-worker.sock"), 0)
            self.assertEqual(agent.run("/tmp/test-worker.sock", cli_prompt="write a file; $(literal)"), 0)
        self.assertEqual(start.call_args_list[1].args[0], [
            "/install/.venv/bin/jiuwenswarm-acp", "--gateway-url", "ws://127.0.0.1:8002/acp"])
        self.assertEqual(start.call_args_list[3].args[0], [
            "/install/.venv/bin/jiuwenswarm", "chat", "--mode", "agent", "--gateway-url",
            "ws://127.0.0.1:8002/tui", "--cwd", os.getcwd(), "--", "write a file; $(literal)"])
        self.assertEqual(start.call_args_list[3].kwargs["stdin"], agent.subprocess.DEVNULL)

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
        with patch.object(agent, "mcp_into_config"):
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
