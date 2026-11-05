"""A benchmark's MCP server run on the machine, in the driver's environment
(``mcp_servers``; ``nullius.standard.mcp_machine``): declared and checked; on
the local backend the agent starts it from that environment; on docker a
listener here, one door on the unit's network (``forward.py`` to a Unix
socket) and a bridge in the agent's container. And the driver environment
installed from its upstream's own lock file (``install.locked``).

No docker here: the docker side is checked by the commands it would run, and
by the same three programs - listener, door, bridge - joined on this machine."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from nullius.standard import container, definitions, install, mcp_machine, run

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard")
FORWARD = container.FORWARD

# A stdio MCP server of the standard library: one tool, ``echo``, whose answer
# says what it was given and where it runs; every call logged to FAKE_LOG.
SERVER = r'''import json, os, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif m["method"] == "tools/list":
        r = {"tools": [{"name": "echo", "description": "says it back", "inputSchema": {"type": "object"}}]}
    else:
        a = m["params"].get("arguments") or {}
        text = "echo:" + str(a.get("say")) + " upstream:" + os.environ.get("NULLIUS_UPSTREAM", "-") + " args:" + ",".join(sys.argv[1:])
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(json.dumps({"tool": m["params"]["name"], "arguments": a}) + "\n")
        r = {"content": [{"type": "text", "text": text}], "isError": False}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''

DRIVER = r'''import json, os
from nullius.standard.jsonl import serve
def log(dir):
    return os.path.join(os.path.dirname(os.path.abspath(dir)), "verifier", "calls.jsonl")
def setup(unit, dir):
    os.makedirs(os.path.dirname(log(dir)), exist_ok=True)
    return {"prompt": 'mcp echo {"say": "%s"}' % unit,
            "mcp": [{"name": "tools", "server": "fake", "args": ["--unit", unit], "env": {"FAKE_LOG": log(dir)}}]}
def score(unit, dir, transcript, answer):
    calls = [json.loads(l) for l in open(log(dir))] if os.path.exists(log(dir)) else []
    ok = calls == [{"tool": "echo", "arguments": {"say": unit}}] and answer.startswith("echo:" + unit)
    return {"reward": 1.0 if ok else 0.0, "passed": ok, "detail": {"calls": calls, "answer": answer}}
serve({"units": lambda dataset: {"units": ["a", "b"]}, "setup": setup, "score": score})
'''


def _stop(p: subprocess.Popen) -> None:
    p.kill()
    p.wait()
    for f in (p.stdin, p.stdout):
        if f is not None:
            f.close()


def bench(here: str) -> dict:
    return {"schema": "nullius.benchmark/v1", "key": "mcpbench", "label": "MCP bench", "summary": "s", "family": "agent",
            "needs": ["agent.tools/v1"], "driver": {"command": ["python3", "{here}/driver.py"]},
            "mcp_servers": {"fake": {"command": ["python3", "{here}/server.py"], "summary": "an echo"}},
            "datasets": [{"id": "all", "units": 2}], "units": {"noun": "task", "nouns": "tasks"},
            "score": {"kind": "pass", "says": "the server's log shows the call"}, "_here": here}


def agent() -> dict:
    a = definitions.get("agent", "fake", os.path.join(FIX, "catalogue"))
    proof = {"on": "2026-10-13", "passed": "1/1"}
    return dict(a, offers=["agent.task/v1", "agent.tools/v1"], proven={"agent.task/v1": proof, "agent.tools/v1": proof})


class Schema(unittest.TestCase):
    def test_a_server_is_a_command_and_a_summary(self):
        b = bench("/defs/b")
        self.assertEqual(definitions.problems(b, "benchmark"), [])
        bad = definitions.problems(dict(b, mcp_servers={"Bad Name": {"command": ["{prompt}"], "summary": "", "x": 1}}), "benchmark")
        self.assertIn("mcp_servers.Bad Name: a name of lower case letters, digits and dashes", bad)
        self.assertTrue(any("{prompt} is not a slot" in p for p in bad), bad)
        self.assertIn("mcp_servers.Bad Name.summary: say what it serves", bad)
        self.assertIn("mcp_servers.Bad Name: x is not a field (command, summary)", bad)
        bad = definitions.problems(dict(b, mcp_servers={"s": {"command": ["{install}/.venv/bin/python"], "summary": "s"}}), "benchmark")
        self.assertIn("mcp_servers.s.command: {install} names driver.install, which is not set", bad)

    def test_locked_goes_with_a_project(self):
        b = dict(bench("/d"), upstream={"repository": "https://example.org/r", "tested": "a" * 40, "license": "MIT"})
        ok = dict(b, driver={"command": ["{install}/.venv/bin/python", "{here}/driver.py"],
                             "install": {"project": ".", "locked": True, "pip": ["mcp==1.30.0"]}})
        self.assertEqual(definitions.problems(ok, "benchmark"), [])
        bad = definitions.problems(dict(ok, driver=dict(ok["driver"], install={"pip": ["a==1"], "locked": True})), "benchmark")
        self.assertIn("driver.install.locked: the lock file of install.project, which is not set", bad)
        bad = definitions.problems(dict(ok, driver=dict(ok["driver"], install={"project": ".", "locked": "yes"})), "benchmark")
        self.assertIn("driver.install.locked: true or false", bad)


class Locked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_lock_is_exported_then_installed_with_pip_then_the_project_without_its_dependencies(self):
        ran = []
        with mock.patch.object(install, "_run", side_effect=lambda cmd, cwd, log, env=None: ran.append(cmd)):
            install._packages({"python": "3.12", "project": ".", "locked": True, "pip": ["mcp==1.30.0"], "extras": ["x"]},
                              os.path.join(self.tmp, "w"), {"repository": "r", "tested": "c"}, src="/up/clone")
        export = next(c for c in ran if c[:2] == ["uv", "export"])
        self.assertIn("--frozen", export)
        self.assertEqual(export[export.index("--project") + 1], "/up/clone")
        self.assertEqual(export[-2:], ["--extra", "x"])
        req = next(c for c in ran if "-r" in c)
        self.assertEqual(req[-1], "mcp==1.30.0")                                    # beside the lock's pins, in one resolution
        self.assertEqual(ran[-1][-3:], ["--no-deps", "-e", "/up/clone[x]"])
        self.assertFalse(any(c[:2] == ["git", "fetch"] for c in ran))               # the clone that was there, not a second one

    def test_a_drivers_project_is_installed_from_its_benchmarks_upstream_clone(self):
        b = dict(bench("/d"), upstream={"repository": "https://example.org/r", "tested": "a" * 40},
                 driver={"command": ["{install}/.venv/bin/python"], "install": {"project": "."}})
        with mock.patch.object(install, "upstream", return_value="/up/clone") as up, \
                mock.patch.object(install, "packages", return_value="/inst") as pk:
            self.assertEqual(install.driver_packages(b, self.tmp), "/inst")
        up.assert_called_once()
        self.assertEqual(pk.call_args.kwargs["src"], "/up/clone")


class Entries(unittest.TestCase):
    def test_the_local_entry_is_the_servers_own_command_in_the_drivers_environment(self):
        b = bench("/defs/b")
        setup = {"mcp": [{"name": "tools", "server": "fake", "args": ["--unit", "a"], "env": {"FAKE_LOG": "/t/log"}},
                         {"name": "kit", "command": "python3", "args": ["/k.py"]}]}
        got = mcp_machine.entries(b, setup, None, {"PATH": "/bin", "NULLIUS_UPSTREAM": "/up"})
        self.assertEqual(got[0], {"name": "tools", "command": "python3", "args": ["/defs/b/server.py", "--unit", "a"],
                                  "env": {"PATH": "/bin", "NULLIUS_UPSTREAM": "/up", "FAKE_LOG": "/t/log"}})
        self.assertEqual(got[1], setup["mcp"][1])                                   # an agent-side server as it was
        with self.assertRaises(mcp_machine.Unknown):
            mcp_machine.entries(b, {"mcp": [{"name": "x", "server": "nope"}]}, None, {})

    def test_on_docker_the_entry_is_the_bridge_told_its_door(self):
        b = bench("/defs/b")
        out, doors, listeners = mcp_machine.doors(b, {"mcp": [{"name": "Tools", "server": "fake"}]}, None, {}, "/", tempfile.gettempdir())
        try:
            self.assertEqual(out, [{"name": "Tools", "command": "python3", "args": [mcp_machine.BRIDGE],
                                    "env": {"NULLIUS_MCP_AT": "mcp-1-tools:9000"}}])
            self.assertEqual(doors, [("mcp-1-tools", 9000, listeners[0].path)])
            self.assertEqual(oct(os.stat(os.path.dirname(listeners[0].path)).st_mode & 0o777), "0o700")
        finally:
            for ln in listeners:
                ln.stop()
        self.assertFalse(os.path.exists(listeners[0].dir))


class Door(unittest.TestCase):
    def test_the_door_is_on_the_units_network_alone_with_the_socket_alone(self):
        a = container.door_argv("img", name="n", job="j", network="net", alias="mcp-1-tools", port=9000, socket_path="/tmp/x/s")
        self.assertEqual(a[a.index("--network") + 1], "net")
        self.assertIn("--network-alias", a)
        self.assertNotIn("-p", a)                                                   # nothing published
        self.assertIn("/tmp/x/s:/tmp/x/s:ro", a)
        self.assertIn(f"{FORWARD}:{container.FORWARD_IN}:ro", a)
        self.assertIn("NULLIUS_FORWARD_TO", a)
        self.assertFalse(any("unix:" in w for w in a))                              # where it goes is named, never given
        self.assertEqual(a[a.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertIn("ALL", a)

    def test_a_unit_opens_one_door_per_server_and_never_joins_it_to_the_bridge(self):
        said = []

        def docker(args, env=None, **kw):
            said.append((args, dict(env or {})))
            out = "127.0.0.1:5555\n" if args[:1] == ["port"] else '{"event": "listening"}'
            return subprocess.CompletedProcess(args, 0, out, "")
        with mock.patch.object(container, "_docker", side_effect=docker):
            u = container.Unit("job", "u0001", tempfile.mkdtemp(), image="img", app="/app", base="https://x/v1", key="k",
                               doors=[("mcp-1-tools", 9000, "/tmp/d/s")]).start()
        runs = [(a, e) for a, e in said if a[:1] == ["run"]]
        door = next((a, e) for a, e in runs if "--network-alias" in a)
        self.assertEqual(door[1]["NULLIUS_FORWARD_TO"], "unix:/tmp/d/s")
        self.assertEqual(door[0][door[0].index("--network") + 1], u.names["network"])
        joined = [a for a, _ in said if a[:2] == ["network", "connect"]]
        self.assertEqual(len(joined), 1)                                            # the relay alone joins the unit's network
        self.assertIn(u.names["agent"] + "-mcp1", u.started)


class DockerUnit(unittest.TestCase):
    """What a run on docker hands the unit: the doors, the bridge as the
    agent's entry, the bridge file alone among the mounts, and the listeners
    stopped when the unit cannot start."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def runner(self):
        r = run.Run.__new__(run.Run)
        r.base, r.key, r.model, r.provider = "https://x/v1", "k", "m", "custom"
        r.judge_model = r.simulator_model = None
        r.backend, r.other, r.bench, r.name, r.image = "docker", agent(), bench("/defs/b"), "job", "img"
        r.up, r.data, r.ds, r.licences = "/up", "/data", {"id": "all"}, {}
        return r

    def test_the_unit_gets_a_door_and_the_agent_the_bridge(self):
        got = {}

        class FakeUnit:
            def __init__(self, *a, **kw):
                got.update(kw)

            def start(self):
                got["started"] = True

            def inside(self, tag):
                return "http://relay:8080/" + tag + "/v1"
        setup = {"prompt": "p", "mcp": [{"name": "tools", "server": "fake", "env": {"FAKE_LOG": "/t/log"}}]}
        trial = os.path.join(self.tmp, "t")
        with mock.patch.object(container, "Unit", FakeUnit):
            rl = self.runner().unit_relay("u0001", trial, setup, None, own=True)
        try:
            self.assertEqual([d[:2] for d in got["doors"]], [("mcp-1-tools", 9000)])
            self.assertEqual(setup["mcp"], [mcp_machine.bridge_entry("tools", "mcp-1-tools:9000")])
            self.assertEqual(rl.unit_files, [mcp_machine.BRIDGE])                      # the bridge alone; nothing of the server
            self.assertTrue(os.path.exists(rl.mcp_listeners[0].path))
        finally:
            for ln in rl.mcp_listeners:
                ln.stop()

    def test_a_unit_that_cannot_start_stops_its_listeners(self):
        made = []
        real = mcp_machine.Listener

        def listener(*a, **kw):
            made.append(real(*a, **kw))
            return made[-1]

        class Broken:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                raise container.Unavailable("no docker")
        setup = {"prompt": "p", "mcp": [{"name": "tools", "server": "fake"}]}
        with mock.patch.object(container, "Unit", Broken), mock.patch.object(mcp_machine, "Listener", side_effect=listener):
            with self.assertRaises(container.Unavailable):
                self.runner().unit_relay("u0001", os.path.join(self.tmp, "t"), setup, None, own=True)
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0].dir))


class Chain(unittest.TestCase):
    """Listener, door and bridge as processes on this machine: the bytes an
    agent writes to the bridge reach a server started for the connection,
    and its answers come back."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_call_through_bridge_door_and_listener(self):
        server = os.path.join(self.tmp, "server.py")
        with open(server, "w") as f:
            f.write(SERVER)
        log = os.path.join(self.tmp, "calls.jsonl")
        ln = mcp_machine.Listener([sys.executable, server, "--unit", "a"], {"FAKE_LOG": log, "PATH": os.defpath,
                                                                           "NULLIUS_UPSTREAM": "/up"},
                                  self.tmp, os.path.join(self.tmp, "server.log")).start()
        self.addCleanup(ln.stop)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        door = subprocess.Popen([sys.executable, FORWARD, "--listen", f"127.0.0.1:{port}"], stdout=subprocess.PIPE, text=True,
                                env={"NULLIUS_FORWARD_TO": "unix:" + ln.path, "PATH": os.defpath})
        self.addCleanup(_stop, door)
        self.assertIn("listening", door.stdout.readline())
        bridge = subprocess.Popen([sys.executable, mcp_machine.BRIDGE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                  env={"NULLIUS_MCP_AT": f"127.0.0.1:{port}", "PATH": os.defpath})
        self.addCleanup(_stop, bridge)
        for m in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                  {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo", "arguments": {"say": "hi"}}}):
            bridge.stdin.write(json.dumps(m) + "\n")
        bridge.stdin.flush()
        self.assertEqual(json.loads(bridge.stdout.readline())["id"], 1)
        got = json.loads(bridge.stdout.readline())
        self.assertEqual(got["result"]["content"][0]["text"], "echo:hi upstream:/up args:--unit,a")
        with open(log) as f:
            self.assertEqual([json.loads(l) for l in f], [{"tool": "echo", "arguments": {"say": "hi"}}])   # logged here
        bridge.stdin.close()
        self.assertEqual(bridge.wait(timeout=10), 0)


class LocalRun(unittest.TestCase):
    """A whole run on the local backend: the agent is given the server's own
    command, starts it, and the driver scores from the server's log."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.addCleanup(setattr, install, "HOME", self._home)

    def test_each_unit_calls_the_benchmarks_server_through_its_own_command(self):
        here = os.path.join(self.tmp, "mcpbench")
        os.makedirs(here)
        for name, text in (("server.py", SERVER), ("driver.py", DRIVER)):
            with open(os.path.join(here, name), "w") as f:
                f.write(text)
        env = {"PATH": os.environ.get("PATH", os.defpath), "MODEL_API_URL": "http://127.0.0.1:9/v1", "MODEL_API_KEY": "sk-test"}
        job_dir = run.run(bench(here), agent(), model="m", provider="custom", jobs_root=os.path.join(self.tmp, "jobs"),
                          concurrency=2, env=env, say=lambda s: None, unsandboxed=True)
        seen = 0
        for name in sorted(os.listdir(job_dir)):
            score = os.path.join(job_dir, name, "verifier", "score.json")
            if os.path.isfile(score):
                with open(score) as f:
                    s = json.load(f)
                self.assertEqual(s["reward"], 1.0, s)
                self.assertIn("args:--unit,", s["detail"]["answer"])
                seen += 1
        self.assertEqual(seen, 2)


if __name__ == "__main__":
    unittest.main()
