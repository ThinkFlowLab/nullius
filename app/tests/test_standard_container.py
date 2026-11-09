"""The container backend's argument lists, read without docker: what is
mounted where, the network, the labels, and no secret on an argument list;
the base image's name follows its Dockerfile. Two tests need a usable docker
and the base image already built, and are skipped otherwise: they build nothing."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from nullius.standard import container, install
from tests import docker_tier


class ImageTest(unittest.TestCase):
    def test_the_tag_is_the_dockerfiles_hash_and_stable(self):
        a, b = container.dockerfile("x86_64"), container.dockerfile("x86_64")
        self.assertEqual(container.image_tag(a), container.image_tag(b))
        self.assertRegex(container.image_tag(a), r"^nullius/base:[0-9a-f]{12}$")
        self.assertNotEqual(container.image_tag(a), container.image_tag(container.dockerfile("aarch64")))

    def test_the_dockerfile_pins_ubuntu_node_and_uv_by_checksum(self):
        text = container.dockerfile("x86_64")
        self.assertTrue(text.startswith("FROM docker.io/library/ubuntu:24.04\n"))       # in full: podman's short names vary
        self.assertIn(f"node-v{container.NODE_VERSION}-linux-x64.tar.xz", text)
        self.assertIn(container.NODE_SHA256["x64"] + "  /tmp/node.tar.xz\" | sha256sum -c -", text)
        self.assertRegex(container.UV_DIGEST, r"^sha256:[0-9a-f]{64}$")
        self.assertIn(f"COPY --from=ghcr.io/astral-sh/uv:{container.UV_VERSION}@{container.UV_DIGEST} /uv /uvx /usr/local/bin/\n", text)
        for pkg in ("git", "python3", "ca-certificates", "curl", "tmux"):
            self.assertIn(f" {pkg} ", text)

    def test_an_unpinned_machine_is_refused(self):
        with self.assertRaises(container.Unavailable):
            container.dockerfile("riscv64")


class EngineTest(unittest.TestCase):
    def test_podman_is_asked_whether_it_is_rootless_in_its_own_terms(self):
        self.enterContext(mock.patch.object(container, "DOCKER", "podman"))
        for said, rootless in (("name=rootless\n", True), ("\n", False)):
            self.enterContext(mock.patch.object(container, "_security", None))
            with mock.patch.object(container, "_docker", return_value=subprocess.CompletedProcess([], 0, said, "")) as ask:
                self.assertEqual((container.rootless(), container.userns_remap()), (rootless, False))
            self.assertEqual(ask.call_args.args[0], ["info", "--format", "{{if .Host.Security.Rootless}}name=rootless{{end}}"])

    def test_on_podman_a_container_with_a_way_out_starts_on_a_bridge_with_dns(self):
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))
        self.enterContext(mock.patch.object(container, "DOCKER", "docker"))
        argvs = lambda: [container.relay_argv("img", name="r", job="j", app="/app", record="/rec", key_file="/k", base="b",
                                              model="m", routes={}),
                         container.forward_argv("img", name="f", job="j", port=1), container.egress_argv("img", name="e", job="j"),
                         container.door_argv("img", name="d", job="j", port=8000, socket_path="/s")]
        for argv in argvs():
            self.assertNotIn("--network", argv)                              # docker's default bridge
        with mock.patch.object(container, "DOCKER", "podman"):
            for argv in argvs():
                self.assertEqual(argv[argv.index("--network") + 1], container.BRIDGE)
            ok = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(container, "_docker", return_value=ok) as made:
                container.make_bridge()
                container.make_bridge()
            self.assertEqual(made.call_args_list, 2 * [mock.call(["network", "create", "--ignore", "--label", "nullius.bridge=1",
                                                                  container.BRIDGE])])     # asked each time: it may have gone

    def test_a_drivers_check_finds_the_engine_from_its_own_home(self):
        env = {"HOME": "/home/u", "PATH": "/bin", "DOCKER_HOST": "unix:///d.sock", "XDG_RUNTIME_DIR": "/run/user/1",
               "XDG_CONFIG_HOME": "/home/u/conf", "CONTAINER_HOST": "unix:///p.sock"}
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))
        with mock.patch.object(container, "DOCKER", "docker"):
            self.assertEqual(container.check_env(), {"DOCKER_HOST": "unix:///d.sock"})
        with mock.patch.object(container, "DOCKER", "podman"):
            got = container.check_env()
            start = container.check_start()
        engine = json.loads(got.pop(container.CHECK_ENGINE))
        self.assertEqual(got, {"DOCKER_HOST": "unix:///d.sock", "XDG_RUNTIME_DIR": "/run/user/1",
                               "CONTAINER_HOST": "unix:///p.sock"})     # no XDG folder of the user's
        self.assertEqual(engine, ["env", "XDG_DATA_HOME=/home/u/.local/share", "XDG_CONFIG_HOME=/home/u/conf", *start])
        with mock.patch.dict(os.environ, {container.CHECK_IMAGE: "img", container.CHECK_ENGINE: json.dumps(engine), "HOME": "/driver"}):
            self.assertEqual(container.check_argv("/w", [], ["true"]), [*engine, "-v", "/w:/w:rw", "-w", "/w", "img", "true"])

    def test_podman_without_netavark_is_refused_plainly(self):
        self.enterContext(mock.patch.object(container, "DOCKER", "podman"))
        said = lambda net: (lambda args, **kw: subprocess.CompletedProcess([], 0, net if args[0] == "info" else "5.8.9\n", ""))
        with mock.patch.object(container, "_docker", said("cni\n")):
            self.assertEqual(container.unusable(), "podman's network backend is cni: Nullius needs netavark"
                                                   ' (podman 4.7 or later, network_backend = "netavark")')
        with mock.patch.object(container, "_docker", said("netavark\n")):
            self.assertIsNone(container.unusable())

    def test_podman_that_does_not_say_its_network_backend_is_a_reason_not_an_error(self):
        self.enterContext(mock.patch.object(container, "DOCKER", "podman"))
        def answer(args, **kw):
            if args[0] == "info":
                raise container.Unavailable("podman info did not answer in 30 s")
            return subprocess.CompletedProcess([], 0, "5.8.9\n", "")
        with mock.patch.object(container, "_docker", answer):
            self.assertEqual(container.unusable(), "podman info did not answer in 30 s")


class ArgvTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(container, "_security", ""))      # a default rootful docker, never asked
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(mock.patch.dict(os.environ, {"NULLIUS_HOME": os.path.join(self.tmp, "profile")}))

    def test_mounts_each_folder_once_at_its_own_path(self):
        ws, home, app = (os.path.join(self.tmp, n) for n in ("ws", "home", "app"))
        here = os.path.join(app, "catalogue", "agents", "x")
        for p in (ws, home, here):
            os.makedirs(p)
        got = container.mounts([ws, home], [here, app, app, os.path.join(self.tmp, "missing"), ""])
        self.assertEqual(got, [(ws, False), (home, False), (app, True)])     # here sits inside app: once

    def test_an_install_in_the_base_image_sees_its_folder_alone_as_this_user(self):
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))
        self.enterContext(mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://u:secret@proxy:3128", "UNRELATED": "x"}))
        a = container.install_argv("img", "/s/agents/x/abc", ["npm", "install", "p@1"])
        self.assertEqual(a[:3], ["docker", "run", "--rm"])
        self.assertIn(["-e", "HTTPS_PROXY"], [a[i:i + 2] for i in range(len(a))])     # this machine's proxy, by name
        self.assertNotIn("secret", " ".join(a))
        self.assertNotIn("UNRELATED", " ".join(a))
        self.assertEqual(a[a.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual([a[i + 1] for i, w in enumerate(a) if w == "-v"], ["/s/agents/x/abc:/s/agents/x/abc:rw"])
        self.assertEqual(a[a.index("img"):], ["img", "npm", "install", "p@1"])
        env = dict(a[i + 1].split("=", 1) for i, w in enumerate(a) if w == "-e" and "=" in a[i + 1])
        self.assertEqual(env, {"HOME": "/s/agents/x/abc/.home", "npm_config_cache": "/s/agents/x/abc/.npm-cache",
                               "UV_CACHE_DIR": "/s/agents/x/abc/.uv-cache", "UV_PYTHON_DOWNLOADS": "automatic",
                               "UV_PYTHON_INSTALL_DIR": "/s/agents/x/abc/.python"})    # inside: the agent's container mounts it
        self.assertEqual({os.path.basename(env[k]) for k in ("HOME", "npm_config_cache", "UV_CACHE_DIR")},
                         set(install.SCRATCH))                # what the install removes after
        self.assertNotIn("--network", a)                     # it downloads the packages; no unit, no key
        a = container.install_argv("img", "/w", ["python", "-c", "x"], {"HF_HUB_CACHE": "/w/hf"})
        self.assertIn("HF_HUB_CACHE=/w/hf", a)

    def test_the_standalone_python_is_installed_in_the_base_image_and_its_scratch_removed(self):
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))
        self.enterContext(mock.patch.object(container, "runtimes_dir", return_value=self.tmp))
        self.enterContext(mock.patch.object(container, "base_image", return_value="nullius/base:x"))
        self.enterContext(mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy:3128"}))
        where = os.path.join(self.tmp, f"python-{container.PYTHON_VERSION}")

        def run(argv, **kw):
            os.makedirs(os.path.join(where, f"cpython-{container.PYTHON_VERSION}-linux-x86_64-gnu"))
            for scratch in install.SCRATCH:
                os.makedirs(os.path.join(where, scratch))
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(container.subprocess, "run", side_effect=run) as ran:
            got = container.python_runtime()
        argv = ran.call_args.args[0]
        self.assertEqual(argv[:2], ["docker", "run"])
        self.assertIn(["-e", "HTTPS_PROXY"], [argv[i:i + 2] for i in range(len(argv))])
        self.assertEqual(ran.call_args.kwargs["env"]["HTTPS_PROXY"], "http://proxy:3128")    # the value -e HTTPS_PROXY passes on
        self.assertEqual(argv[argv.index("nullius/base:x"):],
                         ["nullius/base:x", "uv", "python", "install", container.PYTHON_VERSION, "--install-dir", where, "--no-bin"])
        self.assertEqual(got, os.path.join(where, f"cpython-{container.PYTHON_VERSION}-linux-x86_64-gnu"))
        self.assertEqual(sorted(os.listdir(where)), sorted([install.MARK, os.path.basename(got)]))

    def test_the_agents_container_is_on_the_units_network_with_its_variables_by_name(self):
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))
        a = container.run_argv("img", ["codex-acp"], name="nullius-j-u0000", network="nullius-j-u0000", job="j",
                               workdir="/w", mounted=[("/w", False), ("/i", True)],
                               env=dict.fromkeys(["HOME", "NULLIUS_RELAY_KEY", "PATH"], ""), path="/i/bin:/usr/bin")
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

    def test_the_relays_container_is_given_the_keys_file_never_the_key(self):
        a = container.relay_argv("img", name="nullius-j-u0000-relay", job="j", app="/app", record="/rec", key_file="/k/key",
                                 base="https://api.example/v1", model="m", routes={"u0000-judge": "the-judge"})
        self.assertEqual(a[a.index("--mount") + 1], "type=bind,source=/k/key,target=/opt/nullius/relay-key,readonly")
        self.assertEqual(a[a.index("--key-file") + 1], container.RELAY_KEY_IN)
        self.assertFalse(any("KEY" in a[i + 1] for i, w in enumerate(a) if w == "-e"))
        self.assertIn("127.0.0.1::8080", a)                                 # the driver's way in, this machine only
        self.assertIn("/app:/app:ro", a)
        self.assertIn("/rec:/rec:rw", a)
        self.assertEqual(a[a.index("--route") + 1], "u0000-judge=the-judge")
        self.assertEqual(a[a.index("--public") + 1], "http://relay:8080")
        self.assertEqual(a[a.index("--label") + 1], "nullius.job=j")
        self.assertNotIn("--ledger", a)

    def test_the_relays_container_is_given_the_runs_ledger_and_its_folder_alone(self):
        a = container.relay_argv("img", name="r", job="j", app="/app", record="/rec", key_file="/key", base="b", model="m", routes={},
                                 ledger="/job/.work/ledger/ledger.json")
        self.assertEqual(a[a.index("--ledger") + 1], "/job/.work/ledger/ledger.json")
        # where every relay of the job books its calls: that folder, and nothing else of the job but its own record
        self.assertEqual([w for w in a if w.endswith(":rw")], ["/rec:/rec:rw", "/job/.work/ledger:/job/.work/ledger:rw"])

    def test_an_endpoint_on_this_machines_loopback_is_reached_through_the_bridge(self):
        self.assertTrue(all(map(container.on_loopback, ["http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1"])))
        self.assertFalse(any(map(container.on_loopback, ["https://openrouter.ai/api/v1", "http://172.17.0.1:8000/v1", ""])))
        a = container.relay_argv("img", name="r", job="j", app="/app", record="/rec", key_file="/k", base="http://host.docker.internal:4000/v1",
                                 model="m", routes={}, hosts={"host.docker.internal": "172.17.0.1"})
        self.assertEqual(a[a.index("--add-host") + 1], "host.docker.internal:172.17.0.1")
        self.assertNotIn("--add-host", container.relay_argv("img", name="r", job="j", app="/app", record="/rec", key_file="/k", base="b", model="m", routes={}))
        with mock.patch.object(container, "bridge_gateway", return_value=""), \
                mock.patch.object(container, "rootless", return_value=False):           # Docker Desktop: the bridge is elsewhere
            door = container.Loopback("http://user@localhost:11434/v1?x=1").start()
        door.stop()
        self.assertEqual((door.base, door.hosts), ("http://user@host.docker.internal:11434/v1?x=1", {"host.docker.internal": "host-gateway"}))
        self.assertIsNone(door.door)

    def _rootless_loopback(self, fail=None, ip="10.88.0.7 \n"):
        said = []

        def docker(args, env=None, **_):
            said.append((args, dict(env or {})))
            if fail and args[:1] == [fail]:
                raise container.Unavailable(f"docker {fail}: no")
            return subprocess.CompletedProcess(args, 0, ip if args[:1] == ["inspect"] else "", "")
        self.enterContext(mock.patch.object(container, "_docker", side_effect=docker))
        self.enterContext(mock.patch.object(container, "wait_listening"))
        self.enterContext(mock.patch.object(container, "rootless", return_value=True))
        self.enterContext(mock.patch.object(container.platform, "system", return_value="Linux"))
        return container.Loopback("http://user@localhost:80/v1?x=1", image="img", job="job"), said

    def test_on_a_rootless_engine_an_endpoint_on_this_machines_loopback_is_one_door_for_the_run(self):
        door, said = self._rootless_loopback()
        door.start()
        self.addCleanup(door.stop)
        self.assertEqual((door.base, door.hosts), ("http://user@host.docker.internal:8000/v1?x=1",
                                                   {"host.docker.internal": "10.88.0.7"}))
        self.assertEqual(door.door.target, ("localhost", 80))
        (a, env), = [(a, e) for a, e in said if a[:1] == ["run"]]
        self.assertNotIn("--network", a)                                               # the default network, as the relays
        self.assertEqual(a[a.index("--name") + 1], "nullius-job-model")
        self.assertEqual(a[a.index("--label") + 1], "nullius.job=job")
        self.assertEqual(a[a.index("--user") + 1], "0:0")
        self.assertIn(f"{door.door.path}:{door.door.path}:ro", a)
        self.assertEqual(a[-1], "0.0.0.0:8000")
        self.assertEqual(env["NULLIUS_FORWARD_TO"], "unix:" + door.door.path)
        r = container.relay_argv("img", name="r", job="job", app="/app", record="/rec", key_file="/k", base=door.base,
                                 model="m", routes={}, hosts=door.hosts)
        self.assertEqual(r[r.index("--add-host") + 1], "host.docker.internal:10.88.0.7")
        said.clear()
        door.stop()
        self.assertIn(["rm", "-f", "nullius-job-model"], [a for a, _ in said])
        self.assertFalse(os.path.exists(door.door.dir))

    def test_a_model_door_that_fails_to_start_removes_what_it_made(self):
        for fail, ip in (("run", ""), ("inspect", ""), (None, " \n")):           # the last: inspect gives no address
            door, said = self._rootless_loopback(fail, ip)
            with self.assertRaises(container.Unavailable):
                door.start()
            self.assertIn(["rm", "-f", "nullius-job-model"], [a for a, _ in said])
            self.assertFalse(os.path.exists(door.door.dir))
            self.assertIsNone(door.container)

    def test_on_rootful_podman_an_endpoint_on_this_machines_loopback_is_one_door_on_its_bridge(self):
        door, said = self._rootless_loopback()
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))     # its bridge has no address here yet
        self.enterContext(mock.patch.object(container, "DOCKER", "podman"))
        door.start()
        self.addCleanup(door.stop)
        self.assertEqual(door.hosts, {"host.docker.internal": "10.88.0.7"})
        self.assertEqual([a for a, _ in said][0], ["network", "create", "--ignore", "--label", "nullius.bridge=1", container.BRIDGE])
        (a, _), = [(a, e) for a, e in said if a[:1] == ["run"]]
        self.assertEqual(a[a.index("--network") + 1], container.BRIDGE)

    def test_a_rootless_engine_off_linux_keeps_the_gateway_forwarder(self):
        with mock.patch.object(container, "bridge_gateway", return_value=""), \
                mock.patch.object(container, "rootless", return_value=True), \
                mock.patch.object(container.platform, "system", return_value="Darwin"):   # podman machine, Lima
            door = container.Loopback("http://localhost:11434/v1").start()
        door.stop()
        self.assertEqual((door.base, door.door), ("http://host.docker.internal:11434/v1", None))

    def test_a_socket_door_passes_each_connection_to_its_endpoint_and_removes_its_folder(self):
        import socket, threading
        srv = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(srv.close)
        def echo():
            c, _ = srv.accept()
            with c:
                c.sendall(c.recv(100).upper())
        threading.Thread(target=echo, daemon=True).start()
        door = container.SocketDoor("127.0.0.1", srv.getsockname()[1])
        self.addCleanup(door.stop)
        self.assertTrue(os.path.basename(door.dir).startswith("nullius-door-"))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(door.path)
            s.sendall(b"hello")
            self.assertEqual(s.recv(100), b"HELLO")
        door.stop()
        self.assertFalse(os.path.exists(door.dir))

    def _started(self, **kw):
        said = []

        def docker(args, env=None, **_):
            said.append((args, dict(env or {})))
            return subprocess.CompletedProcess(args, 0, "127.0.0.1:5555\n" if args[:1] == ["port"] else "", "")
        self.enterContext(mock.patch.object(container, "_docker", side_effect=docker))
        self.enterContext(mock.patch.object(container, "wait_listening"))
        u = container.Unit("job", "u0001", os.path.join(self.tmp, "rec"), image="img", app="/app", key="k", **kw).start()
        return u, said

    def test_a_licence_server_on_this_machine_on_a_rootless_engine_is_reached_through_a_door(self):
        self.enterContext(mock.patch.object(container, "rootless", return_value=True))
        self.enterContext(mock.patch.object(container.platform, "system", return_value="Linux"))
        u, said = self._started(base="https://x/v1", servers=["host.docker.internal:27000", "lic.example:1055", "localhost:27001"])
        self.assertEqual(u.reach, {"host.docker.internal:27000": "licence-server-1:27000", "lic.example:1055": "lic.example:1055",
                                   "localhost:27001": "localhost:27001"})                # as on a rootful engine: the forwarder's own
        sd, = u.socket_doors
        self.addCleanup(sd.stop)
        self.assertEqual(sd.target, ("127.0.0.1", 27000))
        steps = [(a, e) for a, e in said if a[:1] == ["run"]]
        a, env = next((a, e) for a, e in steps if u.names["agent"] + "-lic1" in a)
        self.assertEqual((a[a.index("--network") + 1], a[a.index("--network-alias") + 1]), (u.names["network"], "licence-server-1"))
        self.assertEqual(env["NULLIUS_FORWARD_TO"], "unix:" + sd.path)
        a, env = next((a, e) for a, e in steps if u.names["agent"] + "-lic2" in a)
        self.assertIn("host.docker.internal:host-gateway", a)                          # elsewhere: the forwarder as it is
        self.assertEqual(env["NULLIUS_FORWARD_TO"], "lic.example:1055")
        u.stop()
        self.assertFalse(os.path.exists(sd.dir))
        self.assertEqual(u.socket_doors, [])

    def test_a_licence_server_on_this_machine_on_rootful_podman_is_reached_through_a_door(self):
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))
        self.enterContext(mock.patch.object(container, "DOCKER", "podman"))      # its bridge has no address here until a container joins it
        self.enterContext(mock.patch.object(container.platform, "system", return_value="Linux"))
        u, _ = self._started(base="https://x/v1", servers=["host.docker.internal:27000"])
        self.addCleanup(u.stop)
        self.assertEqual(u.reach, {"host.docker.internal:27000": "licence-server-1:27000"})
        self.assertEqual(u.socket_doors[0].target, ("127.0.0.1", 27000))

    def test_the_loopback_forwarder_passes_connections_until_it_stops(self):
        import http.server, socket, threading, urllib.request
        class Ok(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ok)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        with mock.patch.object(container, "bridge_gateway", return_value="127.0.0.2"), \
                mock.patch.object(container, "rootless", return_value=False):           # a second loopback address stands in for it
            door = container.Loopback(f"http://localhost:{srv.server_address[1]}/v1").start()
        self.addCleanup(door.stop)
        port = int(door.base.rsplit(":", 1)[1].split("/")[0])
        self.assertEqual(door.hosts, {"host.docker.internal": "127.0.0.2"})
        self.assertEqual(urllib.request.urlopen(f"http://127.0.0.2:{port}/v1", timeout=10).read(), b"ok")
        door.stop()                                                                    # after it, nothing listens on the gateway
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.2", port), timeout=2).close()

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
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))
        u = container.Unit("j", "u0001", os.path.join(self.tmp, "rec"), image="img", app="/app", base="b", key="k")
        calls = []
        with mock.patch.object(container, "_docker", side_effect=lambda argv, **kw: calls.append(argv[:2])), \
                mock.patch.object(container, "wait_listening", side_effect=lambda name, *a: calls.append(["wait", name])):
            self.assertEqual(u._forward(1, "licence.example:27000"), "licence.example:27000")
        name = u.names["agent"] + "-lic1"
        self.assertEqual(calls, [["run", "-d"], ["wait", name], ["network", "connect"]])

    def start(self, fail_on=None):
        """A unit started against a stand-in docker: each call's arguments and
        environment, and the key's file as it was when the relay was run."""
        u = container.Unit("job", "u0000", os.path.join(self.tmp, "rec"), image="img", app="/app", base="b", key="sk-real")
        calls, seen = [], {}

        def docker(args, env=None, **_):
            calls.append((args, env or {}))
            if args[0] == "run":
                src = args[args.index("--mount") + 1].split(",")[1].removeprefix("source=")
                with open(src) as f:
                    seen.update(path=src, key=f.read(), mode=stat.S_IMODE(os.stat(src).st_mode),
                                dir_mode=stat.S_IMODE(os.stat(os.path.dirname(src)).st_mode))
            if args[:2] == fail_on:
                raise container.Unavailable("docker failed")
            return subprocess.CompletedProcess(args, 0, "0.0.0.0:32768\n" if args[0] == "port" else "", "")
        self.enterContext(mock.patch.object(container, "_docker", docker))       # for the test's stop too
        self.enterContext(mock.patch.object(container, "wait_listening"))
        if fail_on:
            with self.assertRaises(container.Unavailable):
                u.start()
        else:
            u.start()
        return u, calls, seen

    def test_the_key_reaches_the_relay_as_an_owner_only_file_removed_once_read(self):
        u, calls, seen = self.start()
        self.assertEqual((seen["key"], seen["mode"], seen["dir_mode"]), ("sk-real", 0o600, 0o700))
        self.assertEqual(os.path.dirname(os.path.dirname(seen["path"])), container.key_root())  # never the record's folder
        self.assertEqual(stat.S_IMODE(os.stat(container.key_root()).st_mode), 0o700)
        self.assertFalse(os.path.exists(os.path.dirname(seen["path"])))          # gone once the relay listens
        self.assertNotIn("sk-real", json.dumps(calls))                           # no argument, no variable
        u.stop()

    def test_a_failed_start_removes_the_keys_file(self):
        u, calls, seen = self.start(fail_on=["network", "connect"])             # after the relay's run
        self.assertEqual(calls[-1][0][:2], ["network", "rm"])
        self.assertFalse(os.path.exists(os.path.dirname(seen["path"])))
        self.assertIsNone(u.key_dir)

    def test_a_sweep_removes_the_jobs_key_folders_and_no_other_jobs(self):
        os.makedirs(container.key_root())
        mine, other = (tempfile.mkdtemp(prefix=container.key_prefix(j), dir=container.key_root()) for j in ("job", "job-2"))
        with mock.patch.object(container, "_docker", side_effect=container.Unavailable("no docker")):
            container.sweep("job")
        self.assertEqual((os.path.exists(mine), os.path.exists(other)), (False, True))

    def test_a_start_removes_stale_key_folders_of_any_job(self):
        os.makedirs(container.key_root())
        old, fresh = (tempfile.mkdtemp(prefix=container.key_prefix(j), dir=container.key_root()) for j in ("dead", "live"))
        t = time.time() - container.KEY_STALE - 1
        os.utime(old, (t, t))
        self.start()[0].stop()
        self.assertEqual((os.path.exists(old), os.path.exists(fresh)), (False, True))

    def test_on_podman_a_units_home_goes_by_value_and_podman_keeps_its_own(self):
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))
        env = {"HOME": "/h", "XDG_CONFIG_HOME": "/h/c", "NULLIUS_RELAY_KEY": "r", "PATH": "/p"}
        for engine, envs in (("docker", ["PATH=/p", "HOME", "NULLIUS_RELAY_KEY", "XDG_CONFIG_HOME"]),
                             ("podman", ["PATH=/p", "HOME=/h", "XDG_CONFIG_HOME=/h/c", "NULLIUS_RELAY_KEY"])):
            with mock.patch.object(container, "DOCKER", engine):
                u = container.Unit("job", "u0004", os.path.join(self.tmp, "rec"), image="img", app="/app", base="b", key="k")
                argv, prog = u.wrap(["agent"], env, "/w", [("/w", False)])
            self.assertEqual([argv[i + 1] for i, w in enumerate(argv) if w == "-e"], envs)
            self.assertEqual(prog["NULLIUS_RELAY_KEY"], "r")
            self.assertEqual((prog.get("HOME"), prog.get("XDG_CONFIG_HOME")),
                             ("/h", "/h/c") if engine == "docker" else (os.environ.get("HOME"), os.environ.get("XDG_CONFIG_HOME")))

    def _argvs(self) -> dict:
        self.enterContext(mock.patch.dict(os.environ, {container.CHECK_IMAGE: "img"}))
        return {"agent": container.run_argv("img", ["a"], name="n", network="n", job="j", workdir="/w", mounted=[], env={}, path="/p"),
                "check": container.check_argv("/w", [], []), "install": container.install_argv("img", "/w", ["a"]),
                "relay": container.relay_argv("img", name="r", job="j", app="/app", record="/rec", key_file="/k", base="b", model="m", routes={}),
                "forward": container.forward_argv("img", name="f", job="j", port=1),
                "door": container.door_argv("img", name="d", job="j", network="n", alias="a", port=1, socket_path="/s"),
                "egress": container.egress_argv("img", name="e", job="j")}

    def _users(self) -> set:
        return {a[a.index("--user") + 1] for a in self._argvs().values()}

    def test_on_podman_every_container_runs_without_an_selinux_label(self):
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))
        box = mock.patch.object(container, "_docker", side_effect=container.Unavailable("stop at the first call"))
        for engine in ("docker", "podman"):
            with mock.patch.object(container, "DOCKER", engine), box as ran, self.assertRaises(container.Unavailable):
                argvs = list(self._argvs().values())
                container.Box("j", "u0000", "img", workdir="/", network="n").start()
            argvs.append(ran.call_args_list[0].args[0])                              # an own image's container
            self.assertEqual({"label=disable" in a for a in argvs}, {engine == "podman"})

    def test_a_rootless_engine_runs_every_container_as_its_root_which_is_this_user(self):
        with mock.patch.object(container, "rootless", return_value=True):
            self.assertEqual(self._users(), {"0:0"})
        with mock.patch.object(container, "rootless", return_value=False):
            self.assertEqual(self._users(), {f"{os.getuid()}:{os.getgid()}"})

    def test_a_docker_with_userns_remap_runs_what_uses_this_users_files_in_the_machines_user_namespace(self):
        self.enterContext(mock.patch.object(container, "rootless", return_value=False))
        with mock.patch.object(container, "userns_remap", return_value=True):
            on = {k for k, a in self._argvs().items() if "--userns=host" in a}
            self.assertEqual(on, {"agent", "check", "install", "relay", "door"})   # the forwarder and the proxy read one public file
            self.assertEqual(self._users(), {f"{os.getuid()}:{os.getgid()}"})
        with mock.patch.object(container, "userns_remap", return_value=False):
            self.assertFalse([k for k, a in self._argvs().items() if "--userns=host" in a])

    def test_the_engine_is_asked_once_whether_it_is_rootless_and_one_that_cannot_answer_counts_as_rootful(self):
        self.enterContext(mock.patch.object(container, "_security", None))
        with mock.patch.object(container, "_docker", side_effect=container.Unavailable("no docker")):
            self.assertFalse(container.rootless())
        down = subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon")
        with mock.patch.object(container, "_docker", return_value=down):
            self.assertFalse(container.rootless())
        self.assertIsNone(container._security)                             # not an answer: asked again next time
        said = subprocess.CompletedProcess([], 0, "[name=seccomp,profile=builtin name=rootless name=cgroupns]\n", "")
        with mock.patch.object(container, "_docker", return_value=said) as ask:
            self.assertTrue(container.rootless())
            self.assertTrue(container.rootless())
        self.assertEqual(ask.call_count, 1)
        container._security = None
        said = subprocess.CompletedProcess([], 0, "[name=apparmor name=seccomp,profile=builtin name=cgroupns]\n", "")
        with mock.patch.object(container, "_docker", return_value=said):
            self.assertFalse(container.rootless())
            self.assertFalse(container.userns_remap())
        container._security = None
        said = subprocess.CompletedProcess([], 0, "[name=seccomp,profile=builtin name=userns name=cgroupns]\n", "")
        with mock.patch.object(container, "_docker", return_value=said) as ask:
            self.assertTrue(container.userns_remap())
            self.assertFalse(container.rootless())
        self.assertEqual(ask.call_count, 1)                                # one question answers both

    def test_a_long_job_name_gives_a_short_unique_slug(self):
        a, b = container.slug("x" * 200), container.slug("x" * 199 + "y")
        self.assertLessEqual(len(a), 80)
        self.assertNotEqual(a, b)

    def test_missing_docker_is_said_plainly(self):
        from unittest import mock
        with mock.patch.object(container, "DOCKER", "nullius-no-such-docker"):
            self.assertEqual(container.unusable(), "nullius-no-such-docker is not installed on this machine")


class ContextTest(unittest.TestCase):
    """A Docker selected only by ``docker context use`` is in the user's
    ``~/.docker``: the docker program still reaches it with a unit's home."""
    ALT = {"Name": "alt", "Endpoints": {"docker": {"Host": "unix:///run/alt.sock", "SkipTLSVerify": False}},
           "TLSMaterial": {}, "Storage": {"TLSPath": "/u/.docker/contexts/tls/abc"}}

    def env(self, inspect: dict, **environ) -> dict:
        container._context.cache_clear()
        self.addCleanup(container._context.cache_clear)
        kept = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER")}
        done = subprocess.CompletedProcess([], 0, json.dumps([inspect]), "")
        with mock.patch.dict(os.environ, {**kept, **environ}, clear=True), \
                mock.patch.object(container.subprocess, "run", return_value=done):
            return container.client_env({"HOME": "/unit-home"})

    def test_the_current_context_is_passed_as_its_host(self):
        env = self.env(self.ALT)
        self.assertEqual((env["HOME"], env["DOCKER_HOST"]), ("/unit-home", "unix:///run/alt.sock"))
        self.assertNotIn("DOCKER_CONFIG", env)                                     # not the user's logins
        self.assertNotIn("DOCKER_HOST", self.env(dict(self.ALT, Name="default")))

    def test_docker_host_and_docker_context_win(self):
        self.assertEqual(self.env(self.ALT, DOCKER_HOST="tcp://h:2375")["DOCKER_HOST"], "tcp://h:2375")
        env = self.env(self.ALT, DOCKER_CONTEXT="other")
        self.assertEqual((env["DOCKER_CONTEXT"], env.get("DOCKER_HOST")), ("other", None))

    def test_a_tls_context_passes_its_certificates(self):
        tls = dict(self.ALT, TLSMaterial={"docker": ["ca.pem", "cert.pem", "key.pem"]})
        env = self.env(tls)
        self.assertEqual((env["DOCKER_CERT_PATH"], env["DOCKER_TLS_VERIFY"]), ("/u/.docker/contexts/tls/abc/docker", "1"))
        tls["Endpoints"] = {"docker": {"Host": "tcp://h:2376", "SkipTLSVerify": True}}
        env = self.env(tls)
        self.assertEqual((env["DOCKER_TLS"], env.get("DOCKER_TLS_VERIFY")), ("1", None))


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
            seen = subprocess.run([container.DOCKER, "inspect", u.names["relay"]], stdout=subprocess.PIPE, text=True).stdout
            self.assertIn(container.RELAY_KEY_IN, seen)
            self.assertNotIn("sk-test-not-real", seen)                                 # the key is in no configuration
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
        left = subprocess.run([container.DOCKER, "ps", "-aq", "--filter", f"label=nullius.job={job}"], stdout=subprocess.PIPE, text=True)
        nets = subprocess.run([container.DOCKER, "network", "ls", "-q", "--filter", f"label=nullius.job={job}"], stdout=subprocess.PIPE, text=True)
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
        door = container.Loopback(f"http://localhost:{srv.server_address[1]}/v1", image=container.base_image(), job=job).start()
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
