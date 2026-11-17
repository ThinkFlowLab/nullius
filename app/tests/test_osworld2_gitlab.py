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
import stat
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
SUFFIX = "10.213.0.1.nip.io"
SITES = "NULLIUS_OPTION_WEBSITES"
URL = f"http://gitlab.{SUFFIX}"
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
    def test_no_sites_give_no_url_and_a_task_that_needs_gitlab_is_refused(self):
        self.assertIsNone(self.g.url({}))
        self.assertIsNone(self.g.needed("001", {}))
        self.assertEqual(self.g.env("001", {}), {})
        for eid in sorted(self.g.TASKS):
            with self.subTest(eid), self.assertRaises(self.g.GitLabError) as raised:
                self.g.env(eid, {SITES: "none"})
            self.assertIn(f"task {eid} needs GitLab", str(raised.exception))
            self.assertIn("Website host suffix", str(raised.exception))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_the_url_and_this_machine_s_token_are_gitlab_s_tasks_alone(self):
        given = {SITES: SUFFIX, "NULLIUS_UPSTREAM": os.path.join(self.tmp, "upstreams", "osworld2", "c")}
        self.assertEqual(self.g.needed("041", given), URL)
        self.assertIsNone(self.g.needed("005", given))
        made = self.g.env("026", given)
        self.assertEqual(made, {"GITLAB_URL": URL, self.g.TOKEN: self.g.token(self.g.home(given))})
        self.assertEqual(self.g.env("041", given), made)
        self.assertEqual(self.g.env("005", given), {})

    def test_no_option_and_no_licence_ask_for_gitlab(self):
        with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
            bench = json.load(f)
        self.assertFalse({"gitlab", "gitlab_write"} & set(bench["options"]))
        self.assertFalse(bench.get("environments"))
        self.assertFalse(os.path.exists(os.path.join(HERE, "gitlab.Dockerfile")))


class Token(unittest.TestCase):
    def setUp(self):
        self.g = load()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stack = os.path.join(self.tmp, "upstreams", "osworld2", f"gitlab-{self.g.COMMIT}")

    def test_the_stack_is_beside_the_upstream_s_clone(self):
        self.assertEqual(self.g.home({"NULLIUS_UPSTREAM": os.path.dirname(self.stack) + "/c/"}), self.stack)

    def test_made_once_beside_the_stack_for_this_user_alone(self):
        found, failed = [], []

        def make():
            try:
                found.append(self.g.token(self.stack))
            except Exception as e:
                failed.append(e)
        threads = [threading.Thread(target=make) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual((len(found), failed), (8, []))
        self.assertEqual(len(set(found)), 1)
        self.assertEqual(self.g.token(self.stack), found[0])
        self.assertGreaterEqual(len(found[0]), self.g.MIN_TOKEN)
        self.assertTrue(found[0].isalnum())
        path = self.stack + self.g.TOKEN_FILE
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(os.listdir(os.path.dirname(path)), [os.path.basename(path)])
        self.assertNotEqual(self.g.token(self.stack + "-other"), found[0])

    def test_no_file_left_when_the_token_cannot_be_kept(self):
        with mock.patch.object(self.g.os, "link", side_effect=PermissionError), self.assertRaises(PermissionError):
            self.g.token(self.stack)
        self.assertEqual(os.listdir(os.path.dirname(self.stack)), [])

    def test_a_file_that_holds_no_token_is_refused_without_its_words(self):
        os.makedirs(os.path.dirname(self.stack))
        for said in ("secret-but-short", "x" * 30 + "\n"):
            with open(self.stack + self.g.TOKEN_FILE, "w") as f:
                f.write(said)
            with self.subTest(said), self.assertRaises(self.g.GitLabError) as raised:
                self.g.token(self.stack)
            self.assertNotIn(said.strip(), str(raised.exception))


class Clone(unittest.TestCase):
    def test_cloned_at_the_commit_once_and_again_when_changed(self):
        g = load()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        source, stack = os.path.join(tmp, "source"), os.path.join(tmp, "gitlab")
        git = lambda *a: subprocess.run(["git", "-C", source, *a], check=True, capture_output=True, text=True).stdout
        os.makedirs(source)
        git("init", "--quiet")
        with open(os.path.join(source, "docker-compose.yml"), "w") as f:
            f.write("services: {}\n")
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "c")
        with mock.patch.object(g, "REPOSITORY", "file://" + source), mock.patch.object(g, "COMMIT", git("rev-parse", "HEAD").strip()):
            self.assertEqual(g._clone(stack), stack)
            os.utime(stack, (0, 0))
            g._clone(stack)
            self.assertEqual(os.stat(stack).st_mtime, 0)
            with open(os.path.join(stack, "docker-compose.yml"), "w") as f:
                f.write("changed\n")
            g._clone(stack)
            with open(os.path.join(stack, "docker-compose.yml")) as f:
                self.assertEqual(f.read(), "services: {}\n")
            self.assertEqual(sorted(os.listdir(tmp)), ["gitlab", "source"])
        with mock.patch.object(g, "REPOSITORY", "file://" + os.path.join(tmp, "none")), self.assertRaises(g.GitLabError):
            g._clone(os.path.join(tmp, "other"))
        self.assertEqual(sorted(os.listdir(tmp)), ["gitlab", "source"])
        with mock.patch.object(g, "prepare", lambda *a, **kw: self.fail("prepared")), \
                mock.patch.object(g, "LOCK", os.path.join(tmp, "gitlab.lck")), \
                mock.patch.object(g.websites, "SITES_LOCK", os.path.join(tmp, "sites.lck")), \
                mock.patch("sys.stderr", new=io.StringIO()) as err:
            for given in (source, tmp):
                self.assertEqual(g.main(["prepare", given, SUFFIX]), 1)
        self.assertIn("give the stack Nullius cloned", err.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(source, "docker-compose.yml")))


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

    def test_the_vms_own_limits_and_the_default_where_one_is_none(self):
        self.assertIsNone(self.g.share({}))
        self.assertEqual(self.g.share({"NULLIUS_OPTION_WRITE_MBPS": "8", "NULLIUS_OPTION_WRITE_IOPS": "80"}), (8, 80))
        self.assertEqual(self.g.share({"NULLIUS_OPTION_WRITE_MBPS": "8"}), (8, 160))
        self.assertEqual(self.g.share({"NULLIUS_OPTION_WRITE_IOPS": "80"}), (16, 80))

    def test_a_limit_that_leaves_a_service_none_is_refused(self):
        with self.assertRaises(self.g.GitLabError):
            self.g.share({"NULLIUS_OPTION_WRITE_IOPS": "4"})


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
        self.envs, self.held = [], []

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
                                   (self.g, "held", held),
                                   (self.g, "prepare", lambda where, **kw: self.order.append(("prepare", where))
                                    or self.ups.append(kw["closed"]) or self.order.append(("limits", kw["limits"]))),
                                   (self.g, "unit", lambda where, dir: os.path.join(dir, "gitlab.limits")),
                                   (self.g, "up", lambda *a: self.ups.append(a) or self.order.append(("up",))),
                                   (self.driver, "_warden", warden)):
            p = mock.patch.object(target, name, fake)
            p.start()
            self.addCleanup(p.stop)
        self.home = os.path.join(self.tmp, "upstreams", "osworld2", f"gitlab-{self.g.COMMIT}")
        p = mock.patch.dict(os.environ, {SITES: SUFFIX, "NULLIUS_UPSTREAM": os.path.join(os.path.dirname(self.home), "c")})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.driver.websites, "held", lambda *a: contextlib.nullcontext())
        p.start()
        self.addCleanup(p.stop)

    def answer(self, eid, **kw):
        return self.driver.answer(f"tasks__{eid}", self.tmp, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1", "k", **kw)

    def test_a_task_that_needs_gitlab_is_refused_without_the_sites_before_anything_runs(self):
        with mock.patch.dict(os.environ, {SITES: "none"}):
            setup = self.driver.EnvironmentSetupError
            for start, kind in ((lambda: self.driver.setup("tasks__041", self.tmp), self.g.GitLabError),
                                (lambda: self.answer("041", judge=JUDGE), setup),
                                (lambda: self.driver.score("tasks__041", self.tmp, judge=JUDGE), setup)):
                with self.assertRaises(kind) as raised:
                    start()
                self.assertEqual(kind is setup, str(raised.exception).startswith("GitLabError: "))
        self.assertEqual(self.envs, [])
        self.assertFalse(os.path.exists(self.home + self.g.TOKEN_FILE))

    def test_the_url_and_token_reach_gitlab_s_tasks_alone_and_gitlab_is_held(self):
        for start in (lambda: self.answer("026"), lambda: self.driver.score("tasks__041", self.tmp, judge=JUDGE)):
            with self.assertRaises(Started):
                start()
            self.assertEqual((self.envs[-1]["GITLAB_URL"], self.envs[-1][self.g.TOKEN]), (URL, self.g.token(self.home)))
        with self.assertRaises(Started):
            self.answer("001")
        self.assertNotIn("GITLAB_URL", self.envs[-1])
        self.assertNotIn(self.g.TOKEN, self.envs[-1])
        self.assertEqual(self.held, [(URL, mock.ANY)] * 2 + [(None, mock.ANY)])
        limits = os.path.join(self.tmp, "gitlab.limits")
        self.assertEqual(self.ups[1::2], [(self.home, limits, mock.ANY)] * 2)
        self.assertIsNotNone(self.held[0][-1].until)
        self.assertIsNotNone(self.ups[1][-1].until)
        self.assertIs(self.ups[0], self.ups[1][-1])
        self.assertEqual(self.order, [("warden", self.tmp), ("prepare", self.home), ("limits", limits), ("up",),
                                      ("let go", self.tmp)] * 2)

    def test_the_sites_that_serve_gitlab_are_checked_among_what_a_vm_start_needs(self):
        for name in ("vm_image", "_provider_image", "_write_limit"):
            p = mock.patch.object(self.driver, name, lambda: "image")
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(self.driver, "_nat", lambda: True), \
                mock.patch.object(self.driver.os, "listdir", lambda p: ["x"]), \
                mock.patch.object(self.driver.os.path, "isdir", lambda p: True), \
                mock.patch.object(self.driver.websites, "ready", lambda *a, **kw: self.held.append((a, kw))), \
                mock.patch.object(self.driver._startup(), "network", lambda docker: "nullius-osworld2"):
            self.driver._ready("tasks", "001")
            self.driver._ready("tasks", "026")
            self.driver._ready("tasks", "041")
        self.assertEqual(self.held, [((SUFFIX,), {"state": False})] * 2)
        self.assertEqual(self.order, [])

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
        p = mock.patch.object(self.g, "token", lambda stack: TOKEN)
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
        self.assertEqual(argv[:3], ["timeout", "--foreground", str(self.g.START_SECONDS)])
        self.assertGreater(kw["timeout"], self.g.START_SECONDS)
        with mock.patch.object(self.g, "_upstream_script", lambda stack: "echo other"), \
                self.assertRaises(self.g.GitLabError):
            self.g._make_token(self.stack)
        with mock.patch.object(self.g, "_upstream_script", lambda stack: script), \
                mock.patch.object(self.g.subprocess, "run", lambda argv, **kw: Fake(1, stderr=f"x {TOKEN} y\n")), \
                self.assertRaises(self.g.GitLabError) as raised:
            self.g._make_token(self.stack)
        self.assertNotIn(TOKEN, str(raised.exception))

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
        environ = {"NULLIUS_OPTION_WRITE_MBPS": "8", "NULLIUS_OPTION_WRITE_IOPS": "80"}
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

    def test_up_down_and_prepare_by_hand_refuse_while_a_unit_holds_gitlab(self):
        ran = []
        with mock.patch.object(self.g, "up", lambda *a, **kw: ran.append("up")), \
                mock.patch.object(self.g, "down", lambda *a, **kw: ran.append("down")), \
                mock.patch.object(self.g, "prepare", lambda *a, **kw: ran.append("prepare")):
            with self.g.held(URL, threading.Event()), mock.patch("sys.stderr", new=io.StringIO()) as err:
                for args in (["up", self.stack], ["down", self.stack], ["prepare", self.stack, SUFFIX]):
                    self.assertEqual(self.g.main(args), 1)
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

    def test_the_stamp_is_read_from_the_snapshot_read_only(self):
        with mock.patch.object(self.g, "_snapshot", lambda: True):
            with mock.patch.object(self.g, "_docker",
                                   lambda *a, **kw: self.calls.append(a) or Fake(stdout=f"abc {int(time.time())}")):
                self.assertEqual(self.g._made(), "abc")
            self.assertIn(f"{self.g.SNAPSHOT}:/snapshot:ro", self.calls[-1])
            self.assertIn("none", self.calls[-1])
            old = int(time.time()) - self.g.TOKEN_DAYS * 86400 - 1
            for said in ("abc", f"abc {old}", "abc x"):
                with self.subTest(said), mock.patch.object(self.g, "_docker", lambda *a, **kw: Fake(stdout=said)):
                    self.assertIsNone(self.g._made())
            run = lambda said: lambda *a, **kw: Fake() if a[0] == "image" else said
            with mock.patch.object(self.g, "_docker", run(Fake(1, stderr="cat: /snapshot/made: No such file or directory\n"))):
                self.assertIsNone(self.g._made())
            with mock.patch.object(self.g, "_docker", run(Fake(1, stderr="cat: /snapshot/made: I/O error\n"))), \
                    self.assertRaises(self.g.GitLabError):
                self.g._made()
            with mock.patch.object(self.g, "_docker", run(Fake(125, stderr="daemon\n"))), \
                    self.assertRaises(self.g.GitLabError):
                self.g._made()
            gone = Fake(1, stderr="Error: No such image: gitlab/gitlab-ce\n")
            with mock.patch.object(self.g, "_docker", lambda *a, **kw: self.calls.append(a) or (gone if a[0] == "image" else Fake())):
                self.assertIsNone(self.g._made())
            self.assertEqual(self.calls[-1][:2], ("image", "inspect"))
            with mock.patch.object(self.g, "_docker", lambda *a, **kw: Fake(1, stderr="Cannot connect\n")), \
                    self.assertRaises(self.g.GitLabError):
                self.g._made()
        with mock.patch.object(self.g, "_snapshot", lambda: False):
            self.assertIsNone(self.g._made())
        for said in ("Error response from daemon: get x: no such volume\n", "Error: No such volume: x\n"):
            with mock.patch.object(self.g, "_docker", lambda *a, **kw: Fake(1, stderr=said)):
                self.assertFalse(self.g._snapshot())
        with mock.patch.object(self.g, "_docker", lambda *a, **kw: Fake(1, stderr="Cannot connect to the Docker daemon\n")), \
                self.assertRaises(self.g.GitLabError):
            self.g._snapshot()

    def test_init_keeps_the_stamp_prepare_reads_and_not_the_token(self):
        self.configure()
        copied = []
        for name, fake in (("compose", lambda *a, **kw: Fake()), ("_until", lambda *a, **kw: 1),
                           ("_make_token", lambda stack: None), ("ready", lambda *a, **kw: URL),
                           ("_register", lambda stack, value: 7), ("_volumes", lambda: []), ("_snapshot", lambda: False),
                           ("_copy", lambda stack, settings, tar, keep: copied.append(tar))):
            p = mock.patch.object(self.g, name, fake)
            p.start()
            self.addCleanup(p.stop)
        self.g.init(self.stack)
        self.assertIn(f"printf '%s %s' {self.g._stamp(os.path.abspath(self.stack), URL)} ", copied[-1])
        self.assertIn(f" > /snapshot/{self.g.STAMP}", copied[-1])
        self.assertNotIn(TOKEN, copied[-1])

    def test_init_runs_under_the_unit_s_limits(self):
        self.configure()
        limits = self.g.unit(self.stack, self.tmp, {"NULLIUS_OPTION_WRITE_MBPS": "8", "NULLIUS_OPTION_WRITE_IOPS": "80"})
        given, copied = [], []
        for name, fake in (("compose", lambda stack, args, **kw: given.append(kw.get("limits")) or Fake()),
                           ("_until", lambda *a, **kw: 1), ("_make_token", lambda stack: None),
                           ("ready", lambda *a, **kw: URL), ("_register", lambda stack, value: 7),
                           ("_volumes", lambda: []), ("_snapshot", lambda: False),
                           ("_copy", lambda stack, settings, tar, keep: copied.append(settings))):
            p = mock.patch.object(self.g, name, fake)
            p.start()
            self.addCleanup(p.stop)
        self.g.init(self.stack, limits=limits)
        self.assertEqual(set(given), {limits})
        self.assertEqual((copied[0]["OSWORLD2_GITLAB_GITLAB_IOPS"], copied[0]["OSWORLD2_GITLAB_SHARE"]), ("60", "unit"))

    def test_a_snapshot_made_for_this_stack_host_and_token_is_kept(self):
        self.configure()
        done = []
        for name in ("_clone", "configure", "compose", "down", "init"):
            p = mock.patch.object(self.g, name, lambda *a, n=name, **kw: done.append(n) or Fake())
            p.start()
            self.addCleanup(p.stop)
        stamp = self.g._stamp(self.stack, URL)
        with mock.patch.object(self.g, "_made", lambda: stamp):
            self.assertEqual(self.g.prepare(self.stack, {SITES: SUFFIX}), self.stack)
            self.assertEqual(done, [])
        for made, environ in ((None, {SITES: SUFFIX}), ("other", {SITES: SUFFIX}),
                              (stamp, {SITES: "10.0.0.1.nip.io"})):
            done.clear()
            with self.subTest(made=made, environ=environ), mock.patch.object(self.g, "_made", lambda: made), \
                    mock.patch.object(self.g, "_snapshot", lambda: True):
                self.g.prepare(self.stack, environ)
                self.assertEqual(done, ["_clone", "configure", "compose", "down", "init"])

    def test_made_anew_in_order_under_the_run_s_share(self):
        order = []
        for name, fake in (("_made", lambda: None), ("_clone", lambda stack: order.append("clone")),
                           ("configure", lambda *a: order.append(("configure",) + a[1:])),
                           ("compose", lambda stack, args, **kw: order.append(args[0]) or Fake()),
                           ("down", lambda stack: order.append("down")), ("_snapshot", lambda: True),
                           ("init", lambda stack, say, closed, limits: order.append(("init", limits)))):
            p = mock.patch.object(self.g, name, fake)
            p.start()
            self.addCleanup(p.stop)
        self.g.prepare(self.stack, {SITES: SUFFIX})
        self.g.prepare(self.stack, {SITES: SUFFIX}, limits="/unit/gitlab.limits")
        self.assertEqual(order, ["clone", ("configure", SUFFIX, 16, 160), "pull", "down", ("init", None),
                                 "clone", ("configure", SUFFIX, 16, 160), "pull", "down", ("init", "/unit/gitlab.limits")])
        self.assertEqual([a for a, _ in self.calls], [("volume", "rm", self.g.SNAPSHOT)] * 2)
        with self.assertRaises(self.g.GitLabError):
            self.g.prepare(self.stack, {})

    def test_a_snapshot_that_cannot_be_read_is_kept(self):
        def fail():
            raise self.g.GitLabError("docker volume got no answer in 300 s")
        done = []
        for name in ("_clone", "configure", "compose", "down", "init"):
            p = mock.patch.object(self.g, name, lambda *a, n=name, **kw: done.append(n))
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(self.g, "_made", fail), self.assertRaises(self.g.GitLabError):
            self.g.prepare(self.stack, {SITES: SUFFIX})
        self.assertEqual((done, self.calls), ([], []))

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
driver.gitlab_server.home = lambda: os.environ["STACK"]
driver.gitlab_server.prepare = lambda *a, **kw: None
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
                        NULLIUS_OPTION_WEBSITES=SUFFIX)

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
