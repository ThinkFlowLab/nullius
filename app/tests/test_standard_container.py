"""The container backend's argument lists, read without docker: what is
mounted where, the network, the labels, and no secret on an argument list;
the base image's name follows its Dockerfile. Two tests need a usable docker
and the base image already built, and are skipped otherwise: they build nothing."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from nullius.standard import container
from tests import docker_tier


class ImageTest(unittest.TestCase):
    def test_the_tag_is_the_dockerfiles_hash_and_stable(self):
        a, b = container.dockerfile("x86_64"), container.dockerfile("x86_64")
        self.assertEqual(container.image_tag(a), container.image_tag(b))
        self.assertRegex(container.image_tag(a), r"^nullius/base:[0-9a-f]{12}$")
        self.assertNotEqual(container.image_tag(a), container.image_tag(container.dockerfile("aarch64")))

    def test_the_dockerfile_pins_ubuntu_and_node_by_checksum(self):
        text = container.dockerfile("x86_64")
        self.assertTrue(text.startswith("FROM ubuntu:24.04\n"))
        self.assertIn(f"node-v{container.NODE_VERSION}-linux-x64.tar.xz", text)
        self.assertIn(container.NODE_SHA256["x64"] + "  /tmp/node.tar.xz\" | sha256sum -c -", text)
        for pkg in ("git", "python3", "ca-certificates", "curl", "tmux"):
            self.assertIn(f" {pkg} ", text)

    def test_an_unpinned_machine_is_refused(self):
        with self.assertRaises(container.Unavailable):
            container.dockerfile("riscv64")


class ArgvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_mounts_each_folder_once_at_its_own_path(self):
        ws, home, app = (os.path.join(self.tmp, n) for n in ("ws", "home", "app"))
        here = os.path.join(app, "catalogue", "agents", "x")
        for p in (ws, home, here):
            os.makedirs(p)
        got = container.mounts([ws, home], [here, app, app, os.path.join(self.tmp, "missing"), ""])
        self.assertEqual(got, [(ws, False), (home, False), (app, True)])     # here sits inside app: once

    def test_a_uv_environments_interpreter_is_found_from_pyvenv_cfg(self):
        py = os.path.join(self.tmp, "uvpython", "cpython-3.12", "bin")
        os.makedirs(py)
        os.makedirs(os.path.join(self.tmp, "inst", ".venv"))
        with open(os.path.join(self.tmp, "inst", ".venv", "pyvenv.cfg"), "w") as f:
            f.write(f"home = {py}\nversion = 3.12\n")
        self.assertEqual(container.python_homes(os.path.join(self.tmp, "inst")), [os.path.dirname(py)])
        self.assertEqual(container.python_homes(self.tmp), [])
        # uv names the home by a link (cpython-3.12-...) to the release folder: both are mounted, so
        # the environment's bin/python link, written through the first, resolves in the container
        link = os.path.join(self.tmp, "uvpython", "cpython-3.12-link")
        os.symlink(os.path.dirname(py), link)
        with open(os.path.join(self.tmp, "inst", ".venv", "pyvenv.cfg"), "w") as f:
            f.write(f"home = {link}/bin\nversion = 3.12\n")
        self.assertEqual(container.python_homes(os.path.join(self.tmp, "inst")), [link, os.path.dirname(py)])

    def test_an_install_in_the_base_image_sees_its_folder_alone_as_this_user(self):
        a = container.install_argv("img", "/s/agents/x/abc", ["npm", "install", "p@1"])
        self.assertEqual(a[:3], ["docker", "run", "--rm"])
        self.assertEqual(a[a.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual([a[i + 1] for i, w in enumerate(a) if w == "-v"], ["/s/agents/x/abc:/s/agents/x/abc:rw"])
        self.assertEqual(a[a.index("img"):], ["img", "npm", "install", "p@1"])
        self.assertIn("npm_config_cache=/s/agents/x/abc/.npm-cache", a)
        self.assertNotIn("--network", a)                     # it downloads the packages; no unit, no key

    def test_the_agents_container_is_on_the_units_network_with_its_variables_by_name(self):
        a = container.run_argv("img", ["codex-acp"], name="nullius-j-u0000", network="nullius-j-u0000", job="j",
                               workdir="/w", mounted=[("/w", False), ("/i", True)],
                               env_names=["HOME", "NULLIUS_RELAY_KEY", "PATH"], path="/i/bin:/usr/bin")
        self.assertEqual(a[:3], ["docker", "run", "-i"])
        self.assertEqual(a[a.index("--network") + 1], "nullius-j-u0000")
        self.assertEqual(a[a.index("--label") + 1], "nullius.job=j")
        self.assertEqual(a[a.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertIn("/w:/w:rw", a)
        self.assertIn("/i:/i:ro", a)
        envs = [a[i + 1] for i, w in enumerate(a) if w == "-e"]
        self.assertEqual(envs, ["PATH=/i/bin:/usr/bin", "HOME", "NULLIUS_RELAY_KEY"])   # values only for PATH
        self.assertEqual(a[-2:], ["img", "codex-acp"])
        self.assertNotIn("-p", a)                                                          # nothing published

    def test_the_relays_container_names_the_key_and_never_holds_it(self):
        a = container.relay_argv("img", name="nullius-j-u0000-relay", job="j", app="/app", record="/rec",
                                 base="https://api.example/v1", model="m", routes={"u0000-judge": "the-judge"})
        self.assertIn("NULLIUS_RELAY_UPSTREAM_KEY", a)
        self.assertFalse(any(w.startswith("NULLIUS_RELAY_UPSTREAM_KEY=") for w in a))
        self.assertIn("127.0.0.1::8080", a)                                 # the driver's way in, this machine only
        self.assertIn("/app:/app:ro", a)
        self.assertIn("/rec:/rec:rw", a)
        self.assertEqual(a[a.index("--route") + 1], "u0000-judge=the-judge")
        self.assertEqual(a[a.index("--public") + 1], "http://relay:8080")
        self.assertEqual(a[a.index("--label") + 1], "nullius.job=j")

    def test_an_endpoint_on_this_machines_loopback_is_reached_through_the_bridge(self):
        self.assertTrue(all(map(container.on_loopback, ["http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1"])))
        self.assertFalse(any(map(container.on_loopback, ["https://openrouter.ai/api/v1", "http://172.17.0.1:8000/v1", ""])))
        a = container.relay_argv("img", name="r", job="j", app="/app", record="/rec", base="http://host.docker.internal:4000/v1",
                                 model="m", routes={}, hosts={"host.docker.internal": "172.17.0.1"})
        self.assertEqual(a[a.index("--add-host") + 1], "host.docker.internal:172.17.0.1")
        self.assertNotIn("--add-host", container.relay_argv("img", name="r", job="j", app="/app", record="/rec", base="b", model="m", routes={}))
        with mock.patch.object(container, "bridge_gateway", return_value=""):          # Docker Desktop: the bridge is elsewhere
            door = container.Loopback("http://user@localhost:11434/v1?x=1").start()
        self.assertEqual((door.base, door.hosts), ("http://user@host.docker.internal:11434/v1?x=1", {"host.docker.internal": "host-gateway"}))
        door.stop()

    def test_the_loopback_forwarder_passes_each_connection_to_the_endpoint(self):
        import http.server, threading, urllib.request
        class Ok(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ok)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        with mock.patch.object(container, "bridge_gateway", return_value="127.0.0.2"):  # a second loopback address stands in for it
            door = container.Loopback(f"http://localhost:{srv.server_address[1]}/v1").start()
        self.addCleanup(door.stop)
        port = int(door.base.rsplit(":", 1)[1].split("/")[0])
        self.assertEqual(door.hosts, {"host.docker.internal": "127.0.0.2"})
        self.assertEqual(urllib.request.urlopen(f"http://127.0.0.2:{port}/v1", timeout=10).read(), b"ok")

    def test_wrap_passes_values_in_the_docker_programs_environment_and_leaves_host_paths_out(self):
        u = container.Unit("job one", "u0002", os.path.join(self.tmp, "rec"), image="img", app="/app", base="b", key="sk-real")
        argv, env = u.wrap(["agent"], {"HOME": "/h", "TMPDIR": "/host/tmp", "NULLIUS_RELAY_KEY": "nullius-relay",
                                       "PATH": "/i/bin:" + container.IMAGE_PATH}, "/w", [("/w", False)])
        self.assertEqual(u.names, {"network": "nullius-job-one-u0002", "agent": "nullius-job-one-u0002",
                                   "relay": "nullius-job-one-u0002-relay"})
        self.assertNotIn("TMPDIR", argv)
        self.assertNotIn("TMPDIR", env)
        self.assertEqual(env["NULLIUS_RELAY_KEY"], "nullius-relay")
        self.assertNotIn("sk-real", " ".join(argv) + " ".join(env.values()))
        self.assertNotEqual(env["PATH"], "/i/bin:" + container.IMAGE_PATH)          # the docker program's own
        self.assertEqual(u.inside("u0002"), "http://relay:8080/u0002/v1")

    def test_a_licence_forwarder_listens_before_it_takes_the_servers_name(self):
        u = container.Unit("j", "u0001", os.path.join(self.tmp, "rec"), image="img", app="/app", base="b", key="k")
        calls = []
        with mock.patch.object(container, "_docker", side_effect=lambda argv, **kw: calls.append(argv[:2])), \
                mock.patch.object(container, "wait_listening", side_effect=lambda name, *a: calls.append(["wait", name])):
            self.assertEqual(u._forward(1, "licence.example:27000"), "licence.example:27000")
        name = u.names["agent"] + "-lic1"
        self.assertEqual(calls, [["run", "-d"], ["wait", name], ["network", "connect"]])

    def test_a_long_job_name_gives_a_short_unique_slug(self):
        a, b = container.slug("x" * 200), container.slug("x" * 199 + "y")
        self.assertLessEqual(len(a), 80)
        self.assertNotEqual(a, b)

    def test_missing_docker_is_said_plainly(self):
        from unittest import mock
        with mock.patch.object(container, "DOCKER", "nullius-no-such-docker"):
            self.assertEqual(container.unusable(), "docker is not installed on this machine")


def _ready() -> str:
    return docker_tier.why_not()


@unittest.skipIf(_ready(), _ready() or "")
class DockerTest(unittest.TestCase):
    """A unit's network and relay, for real: the relay answers the host and
    the agent's container; nothing else does; everything goes when it stops."""

    def test_the_relay_is_the_only_way_out_and_nothing_is_left(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        job = "test-container-" + os.path.basename(tmp)
        image = container.base_image()
        u = container.Unit(job, "u0000", os.path.join(tmp, "rec"), image=image, app=app,
                           base="http://127.0.0.1:9/v1", key="sk-test-not-real")      # an endpoint no one serves
        try:
            u.start()
            with self.assertRaises(urllib.error.HTTPError) as e:                       # the host reaches it
                urllib.request.urlopen(urllib.request.Request(u.url("u0000") + "/chat/completions", data=b"{}"), timeout=30)
            self.assertEqual(e.exception.code, 502)
            self.assertEqual(u.calls("u0000")[-1]["status"], 502)                     # recorded where the host reads it
            probe = ("import socket\n"
                     "def reach(h, p):\n"
                     "    try:\n"
                     "        socket.create_connection((h, p), timeout=5).close(); return 'open'\n"
                     "    except OSError: return 'closed'\n"
                     "print(reach('relay', 8080), reach('1.1.1.1', 80))\n")
            argv, env = u.wrap(["python3", "-c", probe], {"HOME": tmp}, tmp, [(tmp, False)], interactive=False)
            r = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
            self.assertEqual(r.stdout.split(), ["open", "closed"], r.stderr)
        finally:
            u.stop()
            container.sweep(job)
        left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=nullius.job={job}"], stdout=subprocess.PIPE, text=True)
        nets = subprocess.run(["docker", "network", "ls", "-q", "--filter", f"label=nullius.job={job}"], stdout=subprocess.PIPE, text=True)
        self.assertEqual((left.stdout.strip(), nets.stdout.strip()), ("", ""))

    def test_a_relay_reaches_a_model_server_on_this_machines_loopback(self):
        """A local model server (Ollama on localhost:11434) answered every
        unit of a docker run with a refused connection: 127.0.0.1 in the
        relay's container is the container."""
        import http.server, threading
        class Chat(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "hi"}}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Chat)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        job = "test-loopback-" + os.path.basename(tmp)
        door = container.Loopback(f"http://localhost:{srv.server_address[1]}/v1").start()
        u = container.Unit(job, "u0000", os.path.join(tmp, "rec"), image=container.base_image(), app=app,
                           base=door.base, key="sk-test-not-real", hosts=door.hosts)
        try:
            u.start()
            got = urllib.request.urlopen(urllib.request.Request(u.url("u0000") + "/chat/completions", data=b'{"model": "m"}',
                                                                headers={"Content-Type": "application/json"}), timeout=60)
            self.assertEqual(json.load(got)["choices"][0]["message"]["content"], "hi")
        finally:
            u.stop()
            door.stop()
            container.sweep(job)


class AgentMountsTest(unittest.TestCase):
    """The agent's container never sees the application's folder (the keys'
    file is there) nor a benchmark's folder (its data holds the answers)."""

    def test_an_mcp_server_brings_its_file_alone_and_its_log_folder(self):
        with tempfile.TemporaryDirectory() as d:
            bench = os.path.join(d, "bench")
            os.makedirs(os.path.join(bench, "data"))
            server = os.path.join(bench, "server.py")
            open(server, "w").close()
            trial = os.path.join(d, "job", "u0000")
            log = os.path.join(trial, "verifier", "calls.jsonl")
            rw, ro = container.mcp_mounts([{"command": "python3", "args": [server], "env": {"LOG": log, "SEED": "x"}}], trial)
            self.assertEqual((rw, ro), ([os.path.dirname(log)], [server]))
            got = container.mounts([os.path.join(trial, "workspace")], [], ro)
            self.assertIn((server, True), got)
            self.assertNotIn(bench, [p for p, _ in got])

    def test_the_application_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            u = container.Unit("job", "u0000", os.path.join(d, "rec"), image="img", app=os.path.join(d, "app"),
                               base="https://x/v1", key="k")
            for p in (os.path.join(d, "app"), d):
                with self.assertRaises(ValueError):
                    u.wrap(["true"], {}, d, [(p, True)])
            argv, _ = u.wrap(["true"], {}, d, [(os.path.join(d, "ws"), False), (os.path.join(d, "app", "nullius", "kit"), True)])
            self.assertNotIn(f"{d}/app:{d}/app", " ".join(argv))


if __name__ == "__main__":
    unittest.main()
