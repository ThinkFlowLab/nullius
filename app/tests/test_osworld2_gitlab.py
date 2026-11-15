"""OSWorld2's GitLab, as gitlab_server.py and the driver reach it, before a fake GitLab on loopback.
With NULLIUS_OSWORLD2_GITLAB or NULLIUS_OSWORLD2_UPSTREAM set, also checked against them, read as syntax trees."""
from __future__ import annotations

import ast
import contextlib
import glob
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import urlsplit

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(APP, "nullius", "catalogue", "benchmarks", "osworld2")
HOST = "gitlab.10.213.0.1.nip.io"
URL = f"http://{HOST}"
TOKEN = "t" * 24
JUDGE = {"model": "anthropic/claude-sonnet-4.6", "base_url": "http://127.0.0.1:9/u1-judge/v1", "key": "k"}


def load():
    spec = importlib.util.spec_from_file_location("osworld2_gitlab", os.path.join(HERE, "gitlab_server.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "path", [HERE, *sys.path]):
        spec.loader.exec_module(module)
    return module


class Lab(BaseHTTPRequestHandler):
    """A fake GitLab: an ``empty`` sign-in page, the token's ``admin``, its online ``runners``."""
    empty, admin, runners = False, True, [{"id": 1}]

    def log_message(self, *_):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/users/sign_in":
            self.answer(200, b"" if self.empty else b"<html>GitLab</html>")
        elif self.headers.get("PRIVATE-TOKEN") != TOKEN:
            self.answer(401, b"{}")
        elif url.path == "/api/v4/user":
            self.answer(200, json.dumps({"is_admin": self.admin}).encode())
        elif url.path == "/api/v4/runners/all":
            self.answer(200, json.dumps(self.runners).encode())
        else:
            self.answer(404, b"{}")

    def answer(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Case(unittest.TestCase):
    def setUp(self):
        self.g = load()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        Lab.empty, Lab.admin, Lab.runners = False, True, [{"id": 1}]
        server = ThreadingHTTPServer(("127.0.0.1", 0), Lab)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for patch in (mock.patch.object(self.g, "PROXIES", {"http": f"http://127.0.0.1:{server.server_port}"}),
                      mock.patch.object(self.g.websites, "_resolves", lambda host: {"10.213.0.1"}),
                      mock.patch.object(self.g, "LOCK", os.path.join(self.tmp, "gitlab.lck")),
                      mock.patch.object(self.g.websites, "SITES_LOCK", os.path.join(self.tmp, "sites.lck"))):
            patch.start()
            self.addCleanup(patch.stop)


class Option(Case):
    def test_none_gives_no_url_and_a_task_that_needs_gitlab_is_refused(self):
        self.assertIsNone(self.g.url({}))
        self.assertIsNone(self.g.needed("001", {}))
        self.assertEqual(self.g.env("026", {}), {})
        with self.assertRaises(self.g.GitLabError) as raised:
            self.g.needed("026", {self.g.OPTION: "none", self.g.TOKEN: TOKEN})
        self.assertIn("task 026 needs GitLab", str(raised.exception))
        self.assertIn("GitLab option", str(raised.exception))

    def test_a_url_with_no_token_is_refused(self):
        with self.assertRaises(self.g.GitLabError) as raised:
            self.g.needed("041", {self.g.OPTION: HOST})
        self.assertIn("licence GITLAB_PRIVATE_TOKEN", str(raised.exception))

    def test_the_url_is_gitlab_s_tasks_alone(self):
        given = {self.g.OPTION: HOST, self.g.TOKEN: TOKEN}
        self.assertEqual(self.g.needed("041", given), URL)
        self.assertIsNone(self.g.needed("005", given))
        self.assertEqual(self.g.env("026", given), {"GITLAB_URL": URL})
        self.assertEqual(self.g.env("005", given), {})

    def test_a_value_that_is_no_url_is_refused(self):
        for bad in ("http://gitlab.x.io", "x.io/path", "X.IO", "-x.io", "x.io:0", "a" * 65):
            with self.subTest(bad), self.assertRaises(self.g.GitLabError):
                self.g.url({self.g.OPTION: bad})

    def test_the_option_s_pattern_is_the_module_s(self):
        with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
            bench = json.load(f)
        self.assertEqual(bench["options"]["gitlab"]["pattern"], "none|" + self.g.HOST.pattern)
        from nullius.standard import definitions
        self.assertIsNone(definitions.option_problem(bench["options"]["gitlab"], HOST))
        self.assertIsNotNone(definitions.option_problem(bench["options"]["gitlab"], URL))
        self.assertEqual([s for e in bench["environments"].values() for s in e["needs_secrets"]],
                         [{"name": self.g.TOKEN, "to": "driver", "summary": mock.ANY}])


class Ready(Case):
    def test_sign_in_token_and_runner(self):
        self.assertEqual(self.g.ready(URL, TOKEN, runner=True), URL)

    def test_an_empty_sign_in_page_is_not_gitlab(self):
        Lab.empty = True
        with self.assertRaises(self.g.GitLabError) as raised:
            self.g.ready(URL, TOKEN)
        self.assertIn("answered no page", str(raised.exception))

    def test_another_token_or_no_administrator_is_refused(self):
        for token, admin in (("x" * 24, True), (TOKEN, False)):
            Lab.admin = admin
            with self.subTest(admin=admin), self.assertRaises(self.g.GitLabError) as raised:
                self.g.ready(URL, token)
            self.assertIn("GITLAB_PRIVATE_TOKEN", str(raised.exception))
            self.assertNotIn(token, str(raised.exception))

    def test_no_runner_online_fails_only_where_one_is_needed(self):
        Lab.runners = []
        self.assertEqual(self.g.ready(URL, TOKEN), URL)
        with self.assertRaises(self.g.GitLabError) as raised:
            self.g.ready(URL, TOKEN, runner=True)
        self.assertIn("no runner online", str(raised.exception))

    def test_no_answer_says_what_to_do(self):
        with mock.patch.object(self.g, "PROXIES", {"http": "http://127.0.0.1:9"}), \
                self.assertRaises(self.g.GitLabError) as raised:
            self.g.ready(URL, TOKEN)
        self.assertIn("did not answer", str(raised.exception))


class Held(Case):
    def test_gitlab_s_tasks_run_one_at_a_time_and_the_lock_goes_to_the_reaper(self):
        inside, released, order = threading.Event(), threading.Event(), []

        def first():
            with self.g.held(URL, threading.Event()):
                order.append("first")
                inside.set()
                released.wait(10)

        t = threading.Thread(target=first)
        t.start()
        self.assertTrue(inside.wait(10))
        closed = threading.Event()
        closed.set()
        with self.assertRaises(self.g.websites.WebsiteError) as raised:
            with self.g.held(URL, closed):
                pass
        self.assertIn("another unit's GitLab task ran", str(raised.exception))
        with self.g.held(None, closed):
            order.append("other")
        released.set()
        t.join(10)
        with self.g.held(URL, threading.Event()):
            self.assertEqual(len(self.g.websites.held_fds()), 2)
            order.append("second")
        self.assertEqual(order, ["first", "other", "second"])
        self.assertEqual(self.g.websites.held_fds(), ())


class Shadow(unittest.TestCase):
    def test_no_module_of_the_folder_hides_one_the_upstream_imports(self):
        """A module here named as one the upstream imports would be loaded in its place by nop.py."""
        here = {n[:-3] for n in os.listdir(HERE) if n.endswith(".py")}
        self.assertFalse(here & {"gitlab", "requests", "dotenv", "docker", "desktop_env", "mm_agents", "lib_run_single",
                                 "task_loader"})
        up = os.environ.get("NULLIUS_OSWORLD2_UPSTREAM", "")
        if not up:
            return
        files = glob.glob(os.path.join(up, "*.py")) + _task_classes()
        for d in ("desktop_env", "mm_agents"):
            files += glob.glob(os.path.join(up, d, "**", "*.py"), recursive=True)
        imported = set()
        for p in files:
            with open(p, encoding="utf-8", errors="replace") as f:
                try:
                    tree = ast.parse(f.read())
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    imported.add(node.module.split(".")[0])
        self.assertGreater(len(files), 100)
        self.assertFalse(here & imported)


class Share(unittest.TestCase):
    def setUp(self):
        self.g = load()

    def test_none_with_no_vm_limit_and_a_share_given(self):
        self.assertIsNone(self.g.share({}))
        self.assertEqual(self.g.share({self.g.SHARE_OPTION: "8:80", "NULLIUS_OPTION_WRITE_MBPS": "8"}), (8, 80))

    def test_with_the_vms_limited_gitlab_needs_a_share(self):
        for limited in ({"NULLIUS_OPTION_WRITE_MBPS": "8"}, {"NULLIUS_OPTION_WRITE_IOPS": "80"}):
            with self.subTest(limited), self.assertRaises(self.g.GitLabError) as raised:
                self.g.share(limited)
            self.assertIn("GitLab write share", str(raised.exception))

    def test_a_share_that_is_no_share_is_refused(self):
        for bad in ("8", "8:", ":80", "0:80", "8:4", "8/80"):
            with self.subTest(bad), self.assertRaises(self.g.GitLabError):
                self.g.share({self.g.SHARE_OPTION: bad})

    def test_the_option_s_pattern_is_the_module_s(self):
        with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
            bench = json.load(f)
        self.assertEqual(bench["options"]["gitlab_write"]["pattern"], "none|" + self.g.SHARE.pattern)


class Allocation(unittest.TestCase):
    def test_every_service_s_share_within_gitlab_s(self):
        g = load()
        out = g.allocation(8, 80)
        self.assertEqual(set(out), {s for s, _ in g.SHARES})
        self.assertLessEqual(sum(v[0] for v in out.values()), 8 * g.websites.MB)
        self.assertLessEqual(sum(v[1] for v in out.values()), 80)
        self.assertEqual(out["GITLAB"], (6 * g.websites.MB, 60))
        for bad in ((0, 80), (8, 4)):
            with self.subTest(bad), self.assertRaises(g.GitLabError):
                g.allocation(*bad)


class Started(Exception):
    pass


def load_driver(up):
    spec = importlib.util.spec_from_file_location("osworld2_driver_gitlab", os.path.join(HERE, "driver.py"))
    driver = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", ["driver.py"]), mock.patch.object(sys, "path", [HERE, *sys.path]), \
            mock.patch.dict(os.environ, NULLIUS_UPSTREAM=up):
        spec.loader.exec_module(driver)
    return driver


class Driver(unittest.TestCase):
    """The driver's use of gitlab_server.py."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.driver = load_driver(self.tmp)
        self.g = self.driver.gitlab_server
        self.envs, self.checked, self.held = [], [], []

        def run(argv, cwd, env, *a, **kw):
            self.envs.append(env)
            raise Started()

        @contextlib.contextmanager
        def held(value, closed):
            self.held.append((value, closed))
            yield

        self.ups, self.order = [], []

        @contextlib.contextmanager
        def warden(dir):
            self.order.append(("warden", dir))
            try:
                yield
            finally:
                self.order.append(("let go", dir))

        for target, name, fake in ((self.driver, "_vm", lambda dir, domain, eid: os.path.join(dir, "vm.qcow2")),
                                   (self.driver, "_run", run), (self.driver, "_task_file", lambda *a: None),
                                   (self.g, "held", held), (self.g, "prepared", lambda: self.checked.append(1) or "/s"),
                                   (self.g, "unit", lambda where, dir: os.path.join(dir, "gitlab.limits")),
                                   (self.g, "up", lambda *a: self.ups.append(a) or self.order.append(("up",))),
                                   (self.driver, "_warden", warden)):
            p = mock.patch.object(target, name, fake)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {self.g.OPTION: HOST, self.g.TOKEN: TOKEN,
                                         self.driver.websites.OPTION: "10.213.0.1.nip.io"})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.driver.websites, "held", lambda *a: contextlib.nullcontext())
        p.start()
        self.addCleanup(p.stop)

    def answer(self, eid):
        return self.driver.answer(f"tasks__{eid}", self.tmp, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1", "k")

    def test_a_task_that_needs_gitlab_is_refused_without_it_before_anything_runs(self):
        with mock.patch.dict(os.environ, {self.g.OPTION: "none"}):
            setup = self.driver.EnvironmentSetupError
            for start, kind in ((lambda: self.driver.setup("tasks__026", self.tmp), self.g.GitLabError),
                                (lambda: self.answer("026"), setup),
                                (lambda: self.driver.score("tasks__041", self.tmp, judge=JUDGE), setup)):
                with self.assertRaises(kind) as raised:
                    start()
                self.assertEqual(kind is setup, str(raised.exception).startswith("GitLabError: "))
        self.assertEqual(self.envs, [])

    def test_the_url_and_token_reach_gitlab_s_tasks_alone_and_gitlab_is_held(self):
        for start in (lambda: self.answer("026"), lambda: self.driver.score("tasks__041", self.tmp, judge=JUDGE)):
            with self.assertRaises(Started):
                start()
            self.assertEqual((self.envs[-1]["GITLAB_URL"], self.envs[-1][self.g.TOKEN]), (URL, TOKEN))
        with self.assertRaises(Started):
            self.answer("001")
        self.assertNotIn("GITLAB_URL", self.envs[-1])
        self.assertNotIn(self.g.TOKEN, self.envs[-1])
        self.assertEqual(self.held, [(URL, mock.ANY)] * 2 + [(None, mock.ANY)])
        limits = os.path.join(self.tmp, "gitlab.limits")
        self.assertEqual(self.ups, [("/s", limits, mock.ANY)] * 2)
        self.assertIsNotNone(self.held[0][-1].until)
        self.assertIsNotNone(self.ups[0][-1].until)
        self.assertEqual(self.order, [("warden", self.tmp), ("up",), ("let go", self.tmp)] * 2)

    def test_gitlab_is_checked_among_what_a_vm_start_needs(self):
        for name in ("vm_image", "_provider_image", "_write_limit"):
            p = mock.patch.object(self.driver, name, lambda: "image")
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(self.driver, "_nat", lambda: True), \
                mock.patch.object(self.driver.os, "listdir", lambda p: ["x"]), \
                mock.patch.object(self.driver.os.path, "isdir", lambda p: True), \
                mock.patch.object(self.driver.websites, "ready", lambda *a, **kw: None), \
                mock.patch.object(self.driver._startup(), "network", lambda docker: "nullius-osworld2"):
            self.driver._ready("tasks", "001")
            self.driver._ready("tasks", "026")
            self.driver._ready("tasks", "041")
        self.assertEqual(self.checked, [1, 1])

    def test_a_hosts_snapshot_carries_the_module(self):
        from pathlib import Path

        from nullius.standard import payload
        tmp = Path(tempfile.mkdtemp(prefix="nullius-osworld2-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {"NULLIUS_HOME": str(tmp / "profile"), "NULLIUS_CATALOGUE": str(tmp / "catalogue")}):
            _, root = payload.build(str(tmp / "cache"))
        entry = Path(root) / "app" / "nullius" / "catalogue" / "benchmarks" / "osworld2"
        for name in ("gitlab_server.py", "gitlab.compose.yml"):
            self.assertEqual((entry / name).read_bytes(), (Path(HERE) / name).read_bytes(), name)
        with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["driver"]["command"][-1], "{here}/gitlab.compose.yml")


def _stack():
    stack = os.environ.get("NULLIUS_OSWORLD2_GITLAB", "")
    return stack if stack and shutil.which("docker") and os.path.isfile(os.path.join(stack, "docker-compose.yml")) else ""


@unittest.skipUnless(_stack(), "NULLIUS_OSWORLD2_GITLAB names no Task-Web/gitlab, or no docker compose")
class Override(unittest.TestCase):
    def config(self, *args):
        g = load()
        shares = g.allocation(8, 80)
        settings = {"GITLAB_URL": URL, "GITLAB_PRIVATE_TOKEN": g.UNUSED, "OSWORLD2_GITLAB_HOST": URL[7:],
                    "OSWORLD2_GITLAB_INGRESS": "osworld2-web_web", "OSWORLD2_GITLAB_DISK": "/dev/sda",
                    "OSWORLD2_GITLAB_RB": "/x/gitlab.rb",
                    **{f"OSWORLD2_GITLAB_{s}_{k}": str(v[i]) for s, v in shares.items() for i, k in enumerate(("BPS", "IOPS"))}}
        said = subprocess.run(["docker", "compose", "-p", g.PROJECT, "-f", os.path.join(_stack(), "docker-compose.yml"),
                               "-f", g.OVERRIDE, *args, "config", "--format", "json"], capture_output=True, text=True,
                              env=dict(os.environ, **settings), timeout=120)
        self.assertEqual(said.returncode, 0, said.stderr)
        return g, shares, json.loads(said.stdout)["services"]

    def test_the_project_as_compose_reads_it(self):
        g, shares, services = self.config()
        self.assertEqual(sorted(services), ["dind", "gitlab", "gitlab-runner"])
        for name, service in services.items():
            share = shares[{"gitlab": "GITLAB", "gitlab-runner": "RUNNER", "dind": "DIND"}[name]]
            self.assertEqual(service.get("blkio_config"), {
                "device_write_bps": [{"Path": "/dev/sda", "Rate": str(share[0])}],
                "device_write_iops": [{"Path": "/dev/sda", "Rate": str(share[1])}]}, name)
            self.assertFalse(service.get("ports"), name)
            self.assertNotIn("container_name", service, name)
            self.assertEqual(service.get("restart"), "no", name)
            self.assertRegex(service["image"], r"@sha256:[0-9a-f]{64}$", name)
            self.assertFalse([v for v in service.get("volumes") or [] if "docker.sock" in str(v.get("source"))], name)
        self.assertEqual(services["gitlab"]["labels"]["caddy"], f"{URL}, http://*.{URL[7:]}")
        self.assertEqual(list(services["gitlab"]["networks"]), ["web"])
        self.assertEqual(list(services["gitlab-runner"]["networks"]), ["ci"])
        self.assertTrue(services["dind"]["privileged"])

    def test_the_upstream_s_token_service_is_disarmed_and_has_no_socket(self):
        g, _, services = self.config("--profile", "upstream-init")
        self.assertEqual(sorted(services), ["dind", "gitlab", "gitlab-init-token", "gitlab-runner"])
        for name, service in services.items():
            self.assertFalse([v for v in service.get("volumes") or [] if "docker.sock" in str(v.get("source"))], name)
        self.assertEqual(services["gitlab-init-token"]["entrypoint"], ["/bin/false"])
        self.assertFalse(services["gitlab-init-token"].get("command"))

    def test_the_upstream_s_token_step_is_the_one_gitlab_server_py_runs(self):
        g = load()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        stack = os.path.join(tmp, "gitlab")
        os.symlink(_stack(), stack)
        with open(stack + g.ENV_FILE, "w") as f:
            f.write(f"GITLAB_URL={URL}\nGITLAB_PRIVATE_TOKEN={g.UNUSED}\n")
        script = g._upstream_script(stack)
        self.assertEqual(script.count(f'-e {g.TOKEN}="$${g.TOKEN}" gitlab '), 1)
        self.assertEqual(script.count("docker exec gitlab "), 1)


def _task_classes():
    up = os.environ.get("NULLIUS_OSWORLD2_UPSTREAM", "")
    return sorted(glob.glob(os.path.join(up, "evaluation_examples", "task_class", "task_*.py"))) if up else []


@unittest.skipUnless(_task_classes(), "NULLIUS_OSWORLD2_UPSTREAM names no upstream with the gated task classes")
class TaskClasses(unittest.TestCase):
    def test_the_tasks_that_need_gitlab(self):
        g = load()
        found = set()
        for p in _task_classes():
            with open(p, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            if any(isinstance(n, ast.ImportFrom) and (n.module or "").endswith("controllers.gitlab")
                   for n in ast.walk(tree)):
                found.add(os.path.basename(p)[5:-3])
        self.assertEqual(sorted(found), sorted(g.TASKS))
        self.assertLess(g.RUNNER_TASKS, g.TASKS)


class Fake:
    """A finished process as subprocess.run returns it."""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Lifecycle(unittest.TestCase):
    """configure, the token step, the runner, the volumes' copy, mark and reap, and the lock, on fakes."""

    def setUp(self):
        self.g = load()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stack = os.path.join(self.tmp, "gitlab")
        self.calls = []
        p = mock.patch.object(self.g.websites, "SITES_LOCK", os.path.join(self.tmp, "sites.lck"))
        p.start()
        self.addCleanup(p.stop)
        for name, fake in (("LOCK", os.path.join(self.tmp, "gitlab.lck")),
                           ("_docker", lambda *a, **kw: self.calls.append((a, kw)) or Fake()),
                           ("_pinned", lambda stack: os.path.abspath(stack)),
                           ("_container", lambda stack, service: f"id-{service}")):
            p = mock.patch.object(self.g, name, fake)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {self.g.TOKEN: TOKEN})
        p.start()
        self.addCleanup(p.stop)

    def configure(self):
        with mock.patch.object(self.g.websites, "_network", lambda: "nullius-osworld2"), \
                mock.patch.object(self.g.websites, "_root_disk", lambda: ("/var/lib/docker", "/dev/sda")):
            return self.g.configure(self.stack, "10.213.0.1.nip.io", 8, 80)

    def test_configure_writes_no_token_and_its_placeholder_is_no_token(self):
        path, settings = self.configure()
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(TOKEN, text)
        self.assertIn(f"GITLAB_PRIVATE_TOKEN={self.g.UNUSED}\n", text)
        self.assertEqual(settings["GITLAB_URL"], URL)
        self.assertEqual(settings["OSWORLD2_GITLAB_SHARE"], "stack")
        with mock.patch.dict(os.environ, {self.g.TOKEN: self.g.UNUSED}), self.assertRaises(self.g.GitLabError):
            self.g._token()

    def test_the_token_step_passes_the_token_by_name(self):
        script = ('until docker exec gitlab gitlab-rails runner "x"; do sleep 1; done\n'
                  f'docker exec -e {self.g.TOKEN}="$${self.g.TOKEN}" gitlab gitlab-rails runner "y"\n')
        ran = []
        with mock.patch.object(self.g, "_upstream_script", lambda stack: script), \
                mock.patch.object(self.g.subprocess, "run", lambda argv, **kw: ran.append((argv, kw)) or Fake()):
            self.g._make_token(self.stack)
        (argv, kw), = ran
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertEqual(kw["env"][self.g.TOKEN], TOKEN)
        self.assertIn(f"docker exec -e {self.g.TOKEN} id-gitlab gitlab-rails", argv[-1])
        self.assertIn("until docker exec id-gitlab gitlab-rails", argv[-1])
        self.assertNotIn("$$", argv[-1])
        with mock.patch.object(self.g, "_upstream_script", lambda stack: "echo other"), \
                self.assertRaises(self.g.GitLabError):
            self.g._make_token(self.stack)

    def test_the_runner_runs_jobs_unprivileged_in_the_project_s_docker(self):
        asked = []

        def request(path, value, token=None, data=None, method=None):
            asked.append((path, method))
            return 201, json.dumps({"id": 7, "token": "glrt-secret"}).encode()

        with mock.patch.object(self.g, "_request", request):
            self.assertEqual(self.g._register(self.stack, URL), 7)
            (argv, kw), = self.calls
            self.assertNotIn("--docker-privileged", argv)
            self.assertNotIn("glrt-secret", " ".join(argv))
            self.assertEqual(kw["env"]["CI_SERVER_TOKEN"], "glrt-secret")
            for flag in ("--docker-tlsverify", self.g.DOCKER_HOST, "/certs/client"):
                self.assertIn(flag, argv)
            with mock.patch.object(self.g, "_docker", lambda *a, **kw: Fake(1, stderr="FATAL: no")), \
                    self.assertRaises(self.g.GitLabError):
                self.g._register(self.stack, URL)
        self.assertEqual(asked[-1], ("/api/v4/runners/7", "DELETE"))

    def test_the_copy_runs_under_gitlab_s_limit_with_its_labels(self):
        settings = {"OSWORLD2_GITLAB_DISK": "/dev/sda", "OSWORLD2_GITLAB_GITLAB_BPS": "6291456",
                    "OSWORLD2_GITLAB_GITLAB_IOPS": "60", "OSWORLD2_GITLAB_SHARE": "unit"}
        with mock.patch.object(self.g, "compose", lambda *a, **kw: Fake(stdout="gitlab/gitlab-ce@sha256:x\n")):
            self.g._copy(self.stack, settings, "tar -x", keep=False)
            self.g._copy(self.stack, settings, "tar -c", keep=True)
        restore, keep = [a for a, _ in self.calls]
        for flag in ("--network none", "--label com.docker.compose.project=osworld2-gitlab",
                     "--label nullius.osworld2.share=unit", "--device-write-bps /dev/sda:6291456",
                     "--device-write-iops /dev/sda:60"):
            self.assertIn(flag, " ".join(restore))
        self.assertIn(f"{self.g.SNAPSHOT}:/snapshot:ro", restore)
        self.assertIn(f"{self.g.SNAPSHOT}:/snapshot", keep)

    def test_a_unit_s_mark_and_limits_and_its_reap(self):
        self.configure()
        environ = {self.g.SHARE_OPTION: "8:80", "NULLIUS_OPTION_WRITE_MBPS": "8"}
        limits = self.g.unit(self.stack, self.tmp, environ)
        settings = self.g._settings(self.stack, limits)
        self.assertEqual(settings["OSWORLD2_GITLAB_SHARE"], "unit")
        self.assertEqual(settings["OSWORLD2_GITLAB_GITLAB_IOPS"], "60")
        gone = []
        with mock.patch.object(self.g, "down", lambda stack, limits=None: gone.append((stack, limits))):
            self.assertTrue(self.g.reap(self.tmp))
            self.assertFalse(self.g.reap(self.tmp))
        self.assertEqual(gone, [(self.stack, limits)])
        self.assertIsNone(self.g.unit(self.stack, self.tmp, {}))

    def test_up_down_and_init_by_hand_refuse_while_a_unit_holds_gitlab(self):
        ran = []
        with mock.patch.object(self.g, "up", lambda *a, **kw: ran.append("up")), \
                mock.patch.object(self.g, "down", lambda *a, **kw: ran.append("down")), \
                mock.patch.object(self.g, "init", lambda *a, **kw: ran.append("init")):
            with self.g.held(URL, threading.Event()), mock.patch("sys.stderr", new=io.StringIO()) as err:
                for op in ("up", "down", "init"):
                    self.assertEqual(self.g.main([op, self.stack]), 1)
            self.assertIn("a unit's GitLab task runs", err.getvalue())
            self.assertEqual(ran, [])
            with mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(self.g.main(["down", self.stack]), 0)
        self.assertEqual(ran, ["down"])

    def test_compose_by_hand_changes_nothing_while_a_unit_holds_gitlab(self):
        ran = []
        with mock.patch.object(self.g, "compose", lambda stack, args, **kw: ran.append(args) or 0):
            with self.g.held(URL, threading.Event()), mock.patch("sys.stderr", new=io.StringIO()):
                for args in (["down", "--volumes"], ["--profile", "upstream-init", "up", "-d"], ["-p", "ps", "rm"], []):
                    self.assertEqual(self.g.main(["compose", self.stack, *args]), 1, args)
                for args in (["ps"], ["--profile", "upstream-init", "config"], ["--", "logs", "gitlab"]):
                    self.assertEqual(self.g.main(["compose", self.stack, *args]), 0, args)
            self.assertEqual(self.g.main(["compose", self.stack, "down"]), 0)
        self.assertEqual(ran, [["ps"], ["--profile", "upstream-init", "config"], ["logs", "gitlab"], ["down"]])

    def test_one_gitlab_task_at_a_time_whoever_runs_it(self):
        locks = []
        for uid in (1000, 1001):
            with mock.patch.object(os, "getuid", lambda: uid):
                locks.append(load().LOCK)
        self.assertEqual(locks[0], locks[1])
        given = []
        with mock.patch.object(self.g, "_pinned", lambda stack: stack), mock.patch.object(self.g, "_settings", lambda *a: {}), \
                mock.patch.object(self.g.websites, "_compose_version", lambda: None), \
                mock.patch.object(self.g.subprocess, "run", lambda *a, **kw: given.append(kw["pass_fds"]) or mock.Mock()):
            with self.g._alone():
                fds = self.g.websites.held_fds()
                self.g.compose(self.stack, ["down"], capture=False)
        self.assertEqual(len(fds), 2)
        self.assertEqual(given, [fds])

    def test_the_run_s_gitlab_host_must_be_the_stack_s(self):
        self.configure()
        with mock.patch.object(self.g, "stack", lambda: self.stack):
            self.assertEqual(self.g.prepared({self.g.OPTION: HOST}), self.stack)
            with self.assertRaises(self.g.GitLabError) as raised:
                self.g.prepared({self.g.OPTION: "gitlab.example.org"})
        self.assertIn("set the GitLab option to the stack's host", str(raised.exception))

    def test_the_stack_s_folder_is_read_from_the_snapshot_read_only(self):
        with mock.patch.object(self.g, "_docker", lambda *a, **kw: self.calls.append(a) or Fake(stdout="/srv/gitlab\n")):
            self.assertEqual(self.g.stack(), "/srv/gitlab")
        self.assertIn(f"{self.g.SNAPSHOT}:/snapshot:ro", self.calls[-1])
        self.assertIn("none", self.calls[-1])
        for said in (Fake(1), Fake(stdout="relative\n")):
            with mock.patch.object(self.g, "_docker", lambda *a, **kw: said), self.assertRaises(self.g.GitLabError):
                self.g.stack()

    def test_no_override_is_one_line_saying_what_to_do(self):
        with mock.patch.object(self.g, "OVERRIDE", os.path.join(self.tmp, "none.yml")), \
                self.assertRaises(self.g.GitLabError) as raised:
            self.g._argv(self.stack, ["ps"])
        self.assertIn("install Nullius again", str(raised.exception))


GIT = """#!/bin/sh
case "$*" in *rev-parse*) echo 8655d651722f4254e59e813de9f68a6732ea525c ;; esac
exit 0
"""
FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *"version --short"*) echo 2.40.3 ;;
  *" down "*) sleep 2; echo "$*" >> "$FAKE_DOCKER_LOG.done" ;;
esac
exit 0
"""
KILLED = """
import importlib.util, os, sys, time
here, lock, sites = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("osworld2_driver_killed", os.path.join(here, "driver.py"))
driver = importlib.util.module_from_spec(spec)
sys.argv = ["driver.py"]
sys.path.insert(0, here)
spec.loader.exec_module(driver)
driver.gitlab_server.LOCK, driver.websites.SITES_LOCK = lock, sites
driver.gitlab_server.prepared = lambda: os.environ["STACK"]
driver.gitlab_server.up = lambda *a: (print("up", flush=True), time.sleep(600))
with driver._gitlab("041", os.environ["UNIT"]):
    pass
"""


class Warden(unittest.TestCase):
    """The driver killed while GitLab starts: its warden removes GitLab, then lets the locks go."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        for name, text in (("git", GIT), ("docker", FAKE_DOCKER)):
            with open(os.path.join(bin_, name), "w") as f:
                f.write(text)
            os.chmod(os.path.join(bin_, name), 0o755)
        self.stack, self.unit = os.path.join(self.tmp, "gitlab"), os.path.join(self.tmp, "unit")
        os.makedirs(self.stack)
        os.makedirs(self.unit)
        with open(self.stack + ".osworld2.env", "w") as f:
            f.write(f"GITLAB_URL={URL}\n")
        self.log = os.path.join(self.tmp, "docker.log")
        self.lock, self.sites = os.path.join(self.tmp, "gitlab.lck"), os.path.join(self.tmp, "sites.lck")
        self.env = dict(os.environ, PATH=bin_ + os.pathsep + os.environ["PATH"], FAKE_DOCKER_LOG=self.log,
                        NULLIUS_UPSTREAM=self.tmp, STACK=self.stack, UNIT=self.unit,
                        NULLIUS_OPTION_GITLAB=HOST, GITLAB_PRIVATE_TOKEN=TOKEN)

    def free(self, path, mode):
        import fcntl
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    def test_the_driver_killed_during_up_leaves_gitlab_to_its_warden(self):
        import fcntl
        import signal
        p = subprocess.Popen([sys.executable, "-c", KILLED, HERE, self.lock, self.sites], env=self.env,
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.assertEqual(p.stdout.readline().strip(), "up")
        self.assertFalse(self.free(self.lock, fcntl.LOCK_EX))
        self.assertFalse(self.free(self.sites, fcntl.LOCK_EX))
        p.send_signal(signal.SIGKILL)
        p.wait(10)
        seen_held = False
        t0 = time.time()
        while not os.path.exists(self.log + ".done") and time.time() - t0 < 60:
            seen_held = seen_held or not self.free(self.lock, fcntl.LOCK_EX)
            time.sleep(0.2)
        self.assertTrue(os.path.exists(self.log + ".done"), "the warden removed no GitLab")
        self.assertTrue(seen_held, "the lock was let go before GitLab was removed")
        while not self.free(self.lock, fcntl.LOCK_EX) and time.time() - t0 < 90:
            time.sleep(0.5)
        self.assertTrue(self.free(self.lock, fcntl.LOCK_EX))
        self.assertTrue(self.free(self.sites, fcntl.LOCK_EX))
        with open(self.log) as f:
            said = f.read()
        self.assertIn("compose -p osworld2-gitlab", said)
        self.assertIn("down --volumes --remove-orphans", said)
        self.assertFalse(os.path.exists(os.path.join(self.unit, "gitlab.stack")))


class Sites(Case):
    """GitLab's way in is the mock websites' Caddy: a GitLab unit and a hand operation keep a reset away."""

    def test_a_reset_or_smoke_is_refused_while_a_gitlab_unit_runs(self):
        with self.g.held(URL, threading.Event()):
            with self.assertRaises(self.g.websites.WebsiteError):
                with self.g.websites._alone():
                    pass
            self.assertEqual(len(self.g.websites.held_fds()), 2)
        with self.g.websites._alone():
            pass

    def test_gitlab_by_hand_and_a_unit_wait_while_the_sites_are_reset(self):
        closed = threading.Event()
        closed.set()
        with self.g.websites._alone():
            with self.assertRaises(self.g.websites.WebsiteError) as raised:
                with self.g._alone():
                    pass
            self.assertIn("mock websites are being reset", str(raised.exception))
            with self.assertRaises(self.g.websites.WebsiteError):
                with self.g.held(URL, closed):
                    pass


if __name__ == "__main__":
    unittest.main()
