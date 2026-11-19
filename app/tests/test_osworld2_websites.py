"""OSWorld2's mock websites, as websites.py and the driver reach them, before a fake proxy and a fake docker.
With NULLIUS_OSWORLD2_UPSTREAM or NULLIUS_OSWORLD2_WEB set, also checked against them, read as syntax trees."""
from __future__ import annotations

import ast
import contextlib
import glob
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(APP, "nullius", "catalogue", "benchmarks", "osworld2")
GATEWAY = "10.213.0.1"
SUFFIX = f"{GATEWAY}.nip.io"
DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  ps) [ -n "$FAKE_ROW" ] && printf '%s\\n' "$FAKE_ROW" ;;
  compose) [ -n "$FAKE_UP" ] && touch "$FAKE_UP" ;;
esac
exit 0
"""


def load():
    spec = importlib.util.spec_from_file_location("osworld2_websites", os.path.join(HERE, "websites.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Sites(BaseHTTPRequestHandler):
    """Each site by the host asked for: ``down`` 502, ``empty`` an empty page, ``stateless`` no state."""
    down, empty, stateless, overleaf_up = set(), set(), set(), None

    def log_message(self, *_):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        name = url.hostname[:-len(SUFFIX) - 1]
        if name in self.down or (name == "overleaf" and url.path == "/login" and self.overleaf_up
                                 and not os.path.exists(self.overleaf_up)):
            self.send_response(502)
            self.end_headers()
            return
        if url.path == "/api/state":
            cookie = parse_qs(url.query).get("cookie", [""])[0]
            body = b"{}" if name in self.stateless else json.dumps({"user_id": cookie, "state": {"data": {}}}).encode()
        else:
            body = b"" if name in self.empty else b"<html>site</html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Case(unittest.TestCase):
    def setUp(self):
        self.w = load()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        Sites.down, Sites.empty, Sites.stateless, Sites.overleaf_up = set(), set(), set(), None
        server = ThreadingHTTPServer(("127.0.0.1", 0), Sites)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.unknown, self.elsewhere = set(), set()

        def resolves(host):
            name = host[:-len(SUFFIX) - 1]
            if not host.endswith(SUFFIX) or name in self.unknown:
                return set()
            return {"172.17.0.1"} if name in self.elsewhere else {GATEWAY}

        for patch in (mock.patch.object(self.w, "PROXIES", {"http": f"http://127.0.0.1:{server.server_port}"}),
                      mock.patch.object(self.w, "_resolves", resolves),
                      mock.patch.object(self.w, "POLL", 0.2),
                      mock.patch.object(self.w, "LOCK", os.path.join(self.tmp, "overleaf.lck")),
                      mock.patch.object(self.w, "SITES_LOCK", os.path.join(self.tmp, "websites.lck"))):
            patch.start()
            self.addCleanup(patch.stop)


class Option(Case):
    def test_none_gives_no_suffix_and_a_task_that_needs_the_sites_is_refused(self):
        self.assertEqual(self.w.env({}), {})
        self.assertIsNone(self.w.needed("001", {}))
        with self.assertRaises(self.w.WebsiteError) as raised:
            self.w.needed("005", {self.w.OPTION: "none"})
        self.assertIn("task 005 needs the mock websites", str(raised.exception))

    def test_a_suffix_is_every_process_s_website_host_suffix(self):
        given = {self.w.OPTION: SUFFIX}
        self.assertEqual(self.w.env(given), {"WEBSITE_HOST_SUFFIX": SUFFIX})
        self.assertEqual(self.w.needed("057", given), SUFFIX)
        self.assertIsNone(self.w.needed("001", given))

    def test_a_value_that_is_no_host_suffix_is_refused(self):
        for bad in ("http://x.nip.io", "x", "a b.io", "x.nip.io/", "-x.io", "X.IO"):
            with self.subTest(bad), self.assertRaises(self.w.WebsiteError):
                self.w.env({self.w.OPTION: bad})

    def test_the_tasks(self):
        self.assertEqual(len(self.w.TASKS), 40)
        self.assertLess(self.w.OVERLEAF_TASKS, self.w.TASKS)
        self.assertEqual(len(self.w.NAMES), 25)
        self.assertEqual(len(set(self.w.NAMES)), 25)


class Ready(Case):
    def test_every_name_and_each_stateful_site_answers(self):
        self.assertEqual(self.w.ready(SUFFIX, overleaf=True), 25)

    def test_a_unit_s_check_leaves_overleaf_to_its_own_tasks(self):
        Sites.down.add("overleaf")
        self.assertEqual(self.w.ready(SUFFIX), 24)
        with self.assertRaises(self.w.WebsiteError):
            self.w.ready(SUFFIX, overleaf=True)

    def test_a_name_that_does_not_resolve(self):
        self.unknown.add("trippza")
        with self.assertRaises(self.w.WebsiteError) as raised:
            self.w.ready(SUFFIX)
        self.assertIn(f"trippza.{SUFFIX} does not resolve", str(raised.exception))

    def test_a_suffix_whose_names_resolve_past_the_gateway_is_refused(self):
        self.elsewhere.add("awsconsole")
        with self.assertRaises(self.w.WebsiteError) as raised:
            self.w.ready(SUFFIX)
        self.assertIn(f"awsconsole.{SUFFIX} resolves to 172.17.0.1, not {GATEWAY}", str(raised.exception))
        self.assertIn(f"such as {GATEWAY}.nip.io", str(raised.exception))
        with self.assertRaises(self.w.WebsiteError) as raised:
            self.w.suffix({self.w.OPTION: "x"})
        self.assertIn(f"such as {GATEWAY}.nip.io", str(raised.exception))

    def test_a_site_that_fails(self):
        Sites.down.add("mailhub")
        with self.assertRaises(self.w.WebsiteError) as raised:
            self.w.ready(SUFFIX)
        self.assertIn(f"http://mailhub.{SUFFIX}/ answered 502", str(raised.exception))
        self.assertIn("websites.py compose STACK up -d", str(raised.exception))

    def test_an_empty_page_is_a_name_the_stack_does_not_serve(self):
        Sites.empty.add("interfacinglinux")
        with self.assertRaises(self.w.WebsiteError) as raised:
            self.w.ready(SUFFIX)
        self.assertIn("serves another suffix", str(raised.exception))

    def test_a_site_with_no_state(self):
        Sites.stateless.add("wandb")
        with self.assertRaises(self.w.WebsiteError) as raised:
            self.w.ready(SUFFIX)
        self.assertIn(f"wandb.{SUFFIX}/api/state?cookie={self.w.COOKIE} answered no state", str(raised.exception))


class Overleaf(Case):
    def setUp(self):
        super().setUp()
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(DOCKER)
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.calls = os.path.join(self.tmp, "calls")
        self.settings = os.path.join(self.tmp, "OSWorld-web.osworld2.env")
        with open(self.settings, "w") as f:
            f.write(f"HOST_SUFFIX={SUFFIX}\nCADDY_SCHEME=http://\n")
        self.up = os.path.join(self.tmp, "up")
        Sites.overleaf_up = self.up
        env = mock.patch.dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"], FAKE_DOCKER_LOG=self.calls,
                              FAKE_UP=self.up, FAKE_ROW=f"/s/docker-compose.yml,/s.osworld2.compose.yml\t/s\t{self.settings}")
        env.start()
        self.addCleanup(env.stop)

    def docker(self):
        with open(self.calls) as f:
            return f.read().splitlines()

    def test_any_other_task_holds_nothing(self):
        with self.w.held("005", SUFFIX, threading.Event()):
            pass
        self.assertFalse(os.path.exists(self.calls))

    def test_overleaf_starts_anew_as_the_project_runs(self):
        with self.w.held("057", SUFFIX, threading.Event()):
            calls = self.docker()
        compose = f"compose -p osworld2-web --project-directory /s -f /s/docker-compose.yml -f /s.osworld2.compose.yml --env-file {self.settings}"
        self.assertEqual(calls[1:], [
            f"{compose} rm --stop --force -v " + " ".join(self.w.OVERLEAF),
            "volume rm --force osworld2-web_overleaf_web_data osworld2-web_overleaf_web_mongo_data osworld2-web_overleaf_web_redis_data",
            f"{compose} up -d --no-build --pull never overleaf_web_app"])
        self.assertIn("label=com.docker.compose.project=osworld2-web", calls[0])
        self.assertNotIn("service=", calls[0])

    def test_a_stack_for_another_suffix_is_not_touched(self):
        with self.assertRaises(self.w.WebsiteError) as raised:
            with self.w.held("072", "10.0.0.5.nip.io", threading.Event()):
                pass
        self.assertIn(f"the stack serves {SUFFIX}", str(raised.exception))
        self.assertEqual(len(self.docker()), 1)

    def test_no_overleaf_running(self):
        with mock.patch.dict(os.environ, FAKE_ROW=""), self.assertRaises(self.w.WebsiteError) as raised:
            self.w.reset_overleaf(SUFFIX)
        self.assertIn("no container of the osworld2-web project", str(raised.exception))

    def test_overleaf_is_waited_on_until_its_login_page_answers(self):
        with mock.patch.object(self.w, "OVERLEAF_SECONDS", 1), mock.patch.dict(os.environ, FAKE_UP=""):
            with self.assertRaises(self.w.WebsiteError) as raised:
                self.w.reset_overleaf(SUFFIX)
        self.assertIn("Overleaf did not come up in 1 s", str(raised.exception))

    def test_the_next_overleaf_task_waits_for_the_lock(self):
        order, released = [], threading.Event()

        def first():
            with self.w.held("057", SUFFIX, threading.Event()):
                order.append("057 in")
                released.wait(10)
                order.append("057 out")

        t = threading.Thread(target=first)
        t.start()
        while not order:
            time.sleep(0.05)
        threading.Timer(1.0, released.set).start()
        with self.w.held("072", SUFFIX, threading.Event()):
            order.append("072 in")
        t.join(10)
        self.assertEqual(order, ["057 in", "057 out", "072 in"])

    def test_a_process_given_the_locks_keeps_them_after_the_unit_lets_go(self):
        with self.w.held("057", SUFFIX, threading.Event()):
            fds = self.w.held_fds()
            self.assertEqual(len(fds), 2)
            child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                     pass_fds=fds)
        self.addCleanup(child.wait, 10)
        self.assertEqual(self.w.held_fds(), ())
        with self.assertRaises(self.w.WebsiteError):
            with self.w._alone():
                pass
        closed = threading.Event()
        threading.Timer(1.0, closed.set).start()
        with self.assertRaises(self.w.WebsiteError) as raised:
            with self.w.held("072", SUFFIX, closed):
                pass
        self.assertIn("another unit's Overleaf task ran", str(raised.exception))
        child.stdin.close()
        child.wait(10)
        with self.w.held("072", SUFFIX, threading.Event()):
            pass

    def test_reset_and_smoke_refuse_while_a_unit_uses_the_sites(self):
        stack = os.path.join(self.tmp, "OSWorld-web")
        with self.w.held("005", SUFFIX, threading.Event()), mock.patch.object(self.w, "_pinned", os.path.abspath):
            for start in (self.w.reset, self.w.smoke):
                with self.assertRaises(self.w.WebsiteError) as raised:
                    start(stack)
                self.assertIn("a unit is using the mock websites", str(raised.exception))
        self.assertFalse(os.path.exists(self.calls))

    def test_compose_by_hand_changes_nothing_while_a_unit_uses_the_sites(self):
        ran = []
        with mock.patch.object(self.w, "compose", lambda stack, args, **kw: ran.append(args) or 0):
            with self.w.held("005", SUFFIX, threading.Event()), mock.patch("sys.stderr", new=io.StringIO()) as err:
                for args in (["down", "--volumes"], ["up", "-d", "--force-recreate"], ["-p", "ps", "rm"], []):
                    self.assertEqual(self.w.main(["compose", self.tmp, *args]), 1, args)
                for args in (["ps"], ["--", "logs", "caddy"], ["stats", "--no-stream"]):
                    self.assertEqual(self.w.main(["compose", self.tmp, *args]), 0, args)
            self.assertIn("a unit is using the mock websites", err.getvalue())
            self.assertEqual(self.w.main(["compose", self.tmp, "down"]), 0)
        self.assertEqual(ran, [["ps"], ["logs", "caddy"], ["stats", "--no-stream"], ["down"]])

    def test_the_locks_are_the_machine_s_whoever_runs(self):
        names = []
        for uid in (1000, 1001):
            with mock.patch.object(os, "getuid", lambda: uid):
                w = load()
            names.append((w.LOCK, w.SITES_LOCK))
        self.assertEqual(names[0], names[1])
        with self.w.held("005", SUFFIX, threading.Event()):
            pass
        self.assertEqual(os.stat(self.w.SITES_LOCK).st_mode & 0o777, 0o644)
        os.chmod(self.w.SITES_LOCK, 0o444)
        other = subprocess.Popen([sys.executable, "-c", "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDONLY); "
                                  "fcntl.flock(fd, fcntl.LOCK_SH); print(flush=True); sys.stdin.read()",
                                  self.w.SITES_LOCK], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(other.wait, 10)
        self.addCleanup(other.stdout.close)
        self.addCleanup(other.kill)
        other.stdout.readline()
        with self.assertRaises(self.w.WebsiteError) as raised:
            with self.w._alone():
                pass
        self.assertIn("a unit is using the mock websites", str(raised.exception))
        with self.w.held("005", SUFFIX, threading.Event()):
            pass
        other.stdin.close()
        other.wait(10)
        with self.w._alone():
            pass
        os.remove(self.w.SITES_LOCK)
        os.symlink(os.path.join(self.tmp, "elsewhere"), self.w.SITES_LOCK)
        with self.assertRaises(self.w.WebsiteError) as raised:
            with self.w.held("005", SUFFIX, threading.Event()):
                pass
        self.assertIn("cannot be opened", str(raised.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "elsewhere")))

    def test_one_overleaf_task_at_a_time_and_no_longer_than_its_unit(self):
        inside, waited = threading.Event(), threading.Event()
        released = threading.Event()

        def first():
            with self.w.held("057", SUFFIX, threading.Event()):
                inside.set()
                released.wait(10)

        t = threading.Thread(target=first)
        t.start()
        self.assertTrue(inside.wait(10))
        closed = threading.Event()
        threading.Timer(1.5, closed.set).start()
        t0 = time.time()
        with self.assertRaises(self.w.WebsiteError) as raised:
            with self.w.held("072", SUFFIX, closed):
                waited.set()
        self.assertFalse(waited.is_set())
        self.assertIn("time ran out", str(raised.exception))
        self.assertLess(time.time() - t0, 5)
        released.set()
        t.join(10)
        with self.w.held("072", SUFFIX, threading.Event()):
            waited.set()
        self.assertTrue(waited.is_set())


class Stack(Case):
    def test_a_folder_not_at_the_release_is_refused(self):
        with self.assertRaises(self.w.WebsiteError) as raised:
            self.w.configure(self.tmp, SUFFIX, 2, 20)
        self.assertIn(self.w.COMMIT, str(raised.exception))

    def test_the_stack_listens_on_the_vms_network_s_gateway_alone(self):
        stack, made = os.path.join(self.tmp, "OSWorld-web"), []
        with mock.patch.object(self.w, "_pinned", lambda stack: stack), \
                mock.patch.object(self.w, "_root_disk", lambda: ("/var/lib/docker", "/dev/sda")), \
                mock.patch.object(self.w, "_network", lambda: made.append(1) or "nullius-osworld2"):
            with self.assertRaises(self.w.WebsiteError):
                self.w.configure(stack, SUFFIX, 0, 80)
            with self.assertRaises(self.w.WebsiteError) as raised:
                self.w.configure(stack, SUFFIX, 8, 40)
            self.assertIn("at least 80 IOPS", str(raised.exception))
            self.assertEqual(made, [])
            path, settings = self.w.configure(stack, SUFFIX, 8, 80)
        self.assertEqual((settings["OSWORLD2_WEB_ADDRESS"], made), (GATEWAY, [1]))
        with open(path) as f:
            self.assertIn(f"OSWORLD2_WEB_ADDRESS={GATEWAY}\n", f.read())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.w.main(["configure", stack, SUFFIX, "--address", "172.17.0.1", "--write-mbps", "8", "--write-iops", "80"])

    def test_the_sum_of_the_services_limits_never_passes_the_stack_s_share(self):
        for mbps, iops in ((8, 80), (10, 100), (9, 81), (30, 300), (1, 80)):
            tiers = self.w.allocation(mbps, iops)
            settings = {f"OSWORLD2_WEB_{t}_{k}": str(v[i]) for t, v in tiers.items() for i, k in enumerate(("BPS", "IOPS"))}
            each, total = self.w.limits(settings)
            self.assertEqual(sorted(each), sorted(self.w.SERVICES))
            self.assertLessEqual(total, (mbps * self.w.MB, iops))
            self.assertTrue(all(b > 0 and i > 0 for b, i in each.values()))
            writers = [each[s] for s in self.w.WEIGHTS]
            self.assertTrue(all(w >= each["awsconsole"] for w in writers))
            if (mbps, iops) == (8, 80):
                self.assertEqual(total[1], 80)
                self.assertGreater(total[0], 8 * self.w.MB - 1024)

    def test_the_groups_build_every_service_once(self):
        self.assertEqual(len(self.w.SERVICES), 40)
        self.assertEqual(len(set(self.w.SERVICES)), 40)

    def test_every_service_of_the_override_is_limited_and_caddy_pinned_on_one_address(self):
        with open(os.path.join(HERE, "websites.compose.yml"), encoding="utf-8") as f:
            text = f.read()
        services = re.findall(r"^  ([a-z0-9_]+):$", text.split("\nservices:\n", 1)[1], re.M)
        self.assertEqual(sorted(services), sorted(self.w.SERVICES))
        self.assertEqual(text.count("    blkio_config: *blkio\n    logging: *logs\n"), 40 - len(self.w.WEIGHTS))
        self.assertEqual(text.count("    logging: *logs\n"), 40)
        self.assertEqual(set(re.findall(r"\$\{(OSWORLD2_WEB_\w+|HOST_SUFFIX|CADDY_SCHEME)", text)) - {"HOST_SUFFIX", "CADDY_SCHEME"},
                         set(self.w.SETTINGS) - {"HOST_SUFFIX", "CADDY_SCHEME"})
        for service in self.w.WEIGHTS:
            block = re.split(r"\n  (?=\S)", text.split(f"\n  {service}:\n", 1)[1], maxsplit=1)[0]
            self.assertIn(f"OSWORLD2_WEB_{service.upper()}_BPS", block)
        self.assertRegex(self.w.CADDY.search(text).group(1), r"@sha256:[0-9a-f]{64}$")
        self.assertIn("    ports: !override\n      - ${OSWORLD2_WEB_ADDRESS:", text)
        self.assertNotIn("443", text)


BUILD_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1 $2" in
  "network inspect") echo "1|10.213.0.0/24 10.213.0.1" ;;
  "inspect -f") printf '%s\\n' "$FAKE_HOSTCONFIG" ;;
  "buildx inspect") exit "${FAKE_NO_BUILDER:-0}" ;;
  "image inspect") echo "sha256:$(echo "$5" | md5sum | cut -c1-12)" ;;
  "compose version") echo "${FAKE_COMPOSE:-2.40.3+ds1-0ubuntu1}" ;;
esac
case "$*" in *" config --format json") printf '%s\\n' "$FAKE_CONFIG" ;; esac
exit 0
"""


class Build(Case):
    """The stack's own BuildKit with a write limit, and the build group by group, on a fake docker."""

    def setUp(self):
        super().setUp()
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(BUILD_DOCKER)
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.stack = os.path.join(self.tmp, "OSWorld-web")
        os.makedirs(self.stack)
        with open(self.stack + ".osworld2.env", "w") as f:
            f.write(f"HOST_SUFFIX={SUFFIX}\n")
        self.calls = os.path.join(self.tmp, "calls")
        limits = {"BlkioDeviceWriteBps": [{"Path": "/dev/dm-0", "Rate": 20 * 1024 * 1024}],
                  "BlkioDeviceWriteIOps": [{"Path": "/dev/dm-0", "Rate": 200}]}
        config = {"services": {"caddy": {"image": "lucaslorentz/caddy-docker-proxy@sha256:" + "c" * 64},
                               "overleaf_web_mongo": {"image": "mongo:8.0.20"}}}
        self.free = [100.0]
        for patch in (mock.patch.dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"], FAKE_DOCKER_LOG=self.calls,
                                      FAKE_HOSTCONFIG=json.dumps(limits), FAKE_CONFIG=json.dumps(config)),
                      mock.patch.object(self.w, "_pinned", os.path.abspath),
                      mock.patch.object(self.w, "_root_disk", lambda: ("/var/lib/docker", "/dev/dm-0")),
                      mock.patch.object(self.w, "_free_gb", lambda root: self.free.pop(0) if len(self.free) > 1 else self.free[0])):
            patch.start()
            self.addCleanup(patch.stop)

    def docker(self):
        with open(self.calls) as f:
            return f.read().splitlines()

    def test_the_builder_writes_under_its_limit(self):
        self.w.builder(self.stack, "up", 20, 200)
        run, inspect, create = self.docker()
        self.assertIn("--privileged", run)
        self.assertIn("--device-write-bps /dev/dm-0:20971520 --device-write-iops /dev/dm-0:200", run)
        self.assertTrue(run.endswith(self.w.BUILDKIT))
        self.assertEqual(create, f"buildx create --name {self.w.BUILDER} --driver remote docker-container://{self.w.BUILDKITD}")
        with open(self.stack + ".osworld2.buildkitd.toml") as f:
            self.assertIn("max-parallelism = 4", f.read())

    def test_a_builder_docker_kept_no_limit_for_is_removed_and_never_used(self):
        with mock.patch.dict(os.environ, FAKE_HOSTCONFIG="{}"), self.assertRaises(self.w.WebsiteError) as raised:
            self.w.builder(self.stack, "up", 20, 200)
        self.assertIn("no write limit", str(raised.exception))
        self.assertEqual(self.docker()[-1], f"rm -f {self.w.BUILDKITD}")
        self.assertFalse([c for c in self.docker() if c.startswith("buildx create")])

    def test_the_groups_one_after_another_each_cache_removed(self):
        said = []
        out = self.w.build(self.stack, say=said.append)
        calls = self.docker()
        steps = [c for c in calls if " pull " in c or " build " in c or c.startswith("buildx prune")]
        self.assertEqual(len(steps), 1 + 2 * (len(self.w.GROUPS) - 1))
        self.assertTrue(steps[0].endswith("pull " + " ".join(self.w.GROUPS[0][1])))
        for (name, services, _), (built, pruned) in zip(self.w.GROUPS[1:], zip(steps[1::2], steps[2::2])):
            self.assertTrue(built.endswith("build " + " ".join(services)), name)
            self.assertEqual(pruned, f"buildx prune --builder {self.w.BUILDER} --all --force")
        self.assertEqual([json.loads(s)["group"] for s in said], [g[0] for g in self.w.GROUPS])
        with open(out["images"]) as f:
            images = json.load(f)["images"]
        self.assertEqual(len(images), 40)
        self.assertEqual(images["overleaf_web_mongo"]["image"], "mongo:8.0.20")
        self.assertEqual(images["wandb"]["image"], "osworld2-web-wandb")

    def test_the_build_stops_before_a_group_that_would_eat_the_reserve(self):
        self.free = [40.0, 30.0]
        with self.assertRaises(self.w.WebsiteError) as raised:
            self.w.build(self.stack, say=lambda line: None)
        self.assertIn("stopped before overleaf: 30.0 GB free", str(raised.exception))
        self.assertIn("done: pulled", str(raised.exception))
        self.assertFalse([c for c in self.docker() if " build " in c])

    def test_a_compose_without_override_tags_is_refused(self):
        for old in ("2.20.3", "2.24.3", "v2.24.0"):
            with self.subTest(old), mock.patch.dict(os.environ, FAKE_COMPOSE=old), \
                    self.assertRaises(self.w.WebsiteError) as raised:
                self.w.compose(self.stack, ["config"], capture=True)
            self.assertIn("Docker Compose 2.24.4 or later", str(raised.exception))
        with mock.patch.dict(os.environ, FAKE_COMPOSE="2.24.4"):
            self.assertEqual(self.w.compose(self.stack, ["config"], capture=True).returncode, 0)

    def test_compose_hands_the_locks_it_holds_to_docker(self):
        given = []
        with mock.patch.object(self.w, "_compose_version", lambda: None), mock.patch.object(self.w, "_network", lambda: None), \
                mock.patch.object(self.w.subprocess, "run", lambda *a, **kw: given.append(kw["pass_fds"]) or mock.Mock()):
            with self.w._alone():
                fds = self.w.held_fds()
                self.w.compose(self.stack, ["down"])
                self.w.compose(self.stack, ["up", "-d"], capture=True)
        self.assertEqual(len(fds), 1)
        self.assertEqual(given, [fds, fds])

    def test_every_compose_makes_the_vms_network_first(self):
        self.w.compose(self.stack, ["up", "-d"], capture=True)
        calls = self.docker()
        made = next(i for i, c in enumerate(calls) if c.startswith("network inspect ") and c.endswith(" nullius-osworld2"))
        self.assertLess(made, next(i for i, c in enumerate(calls) if c.endswith(" up -d")))

    def test_the_names_are_tried_from_the_vms_network(self):
        self.w._from_vms(SUFFIX)
        run = [c for c in self.docker() if c.startswith("run ")][-1].split()
        self.assertEqual(run[run.index("--network") + 1], "nullius-osworld2")
        self.assertEqual(run[-len(self.w.NAMES):], [f"{n}.{SUFFIX}" for n in self.w.NAMES])

    def test_a_missing_override_says_so(self):
        with mock.patch.object(self.w, "OVERRIDE", os.path.join(self.tmp, "none.yml")), \
                self.assertRaises(self.w.WebsiteError) as raised:
            self.w.compose(self.stack, ["config"], capture=True)
        self.assertIn("this benchmark's folder is incomplete", str(raised.exception))

    def test_no_builder_or_an_unknown_group_builds_nothing(self):
        with self.assertRaises(self.w.WebsiteError):
            self.w.build(self.stack, groups=["nope"])
        with mock.patch.dict(os.environ, FAKE_NO_BUILDER="1"), self.assertRaises(self.w.WebsiteError) as raised:
            self.w.build(self.stack)
        self.assertIn("no builder", str(raised.exception))
        self.assertFalse([c for c in self.docker() if " pull " in c or " build " in c])


class Started(Exception):
    pass


def load_driver(up):
    spec = importlib.util.spec_from_file_location("osworld2_driver_websites", os.path.join(HERE, "driver.py"))
    driver = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", ["driver.py"]), mock.patch.object(sys, "path", [HERE, *sys.path]), \
            mock.patch.dict(os.environ, NULLIUS_UPSTREAM=up):
        spec.loader.exec_module(driver)
    return driver


class Driver(unittest.TestCase):
    """The driver's use of websites.py."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.driver = load_driver(self.tmp)
        self.w = self.driver.websites
        self.envs, self.checked, self.held = [], [], []

        def run(argv, cwd, env, *a, **kw):
            self.envs.append(env)
            raise Started()

        @contextlib.contextmanager
        def held(eid, value, closed):
            self.held.append((eid, value, closed))
            yield

        for target, name, fake in ((self.driver, "_vm", lambda dir, domain, eid: os.path.join(dir, "vm.qcow2")),
                                   (self.driver, "_run", run), (self.driver, "_task_file", lambda *a: None),
                                   (self.w, "held", held), (self.w, "ready", lambda *a, **kw: self.checked.append((a, kw)))):
            p = mock.patch.object(target, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def option(self, value):
        p = mock.patch.dict(os.environ, {self.w.OPTION: value})
        p.start()
        self.addCleanup(p.stop)

    def answer(self, eid):
        return self.driver.answer(f"tasks__{eid}", self.tmp, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1", "k")

    def test_a_task_that_needs_the_sites_is_refused_without_a_suffix_before_anything_runs(self):
        self.option("none")
        setup = self.driver.EnvironmentSetupError
        for start, kind in ((lambda: self.driver.setup("tasks__005", self.tmp), self.w.WebsiteError),
                            (lambda: self.answer("005"), setup), (lambda: self.driver.score("tasks__005", self.tmp), setup)):
            with self.assertRaises(kind) as raised:
                start()
            self.assertIn(("WebsiteError: " if kind is setup else "") + "task 005 needs the mock websites", str(raised.exception))
        self.assertEqual(self.envs, [])

    def test_the_suffix_reaches_every_process_and_overleaf_is_held_around_the_run(self):
        self.option(SUFFIX)
        for start in (lambda: self.answer("057"), lambda: self.driver.score("tasks__057", self.tmp),
                      lambda: self.driver.setup("tasks__001", self.tmp)):
            with self.assertRaises(Started):
                start()
            self.assertEqual(self.envs[-1]["WEBSITE_HOST_SUFFIX"], SUFFIX)
        self.assertEqual(self.held, [("057", SUFFIX, mock.ANY)] * 2)
        self.assertIsNotNone(self.held[0][-1].until)

    def test_no_suffix_is_no_variable(self):
        self.option("none")
        with self.assertRaises(Started):
            self.answer("001")
        self.assertNotIn("WEBSITE_HOST_SUFFIX", self.envs[-1])
        self.assertEqual(self.held, [("001", None, mock.ANY)])

    def test_the_sites_are_checked_among_what_a_vm_start_needs(self):
        for name in ("vm_image", "_provider_image", "_write_limit"):
            p = mock.patch.object(self.driver, name, lambda: "image")
            p.start()
            self.addCleanup(p.stop)
        made = []
        with mock.patch.object(self.driver, "_nat", lambda: True), mock.patch.object(self.driver.os, "listdir", lambda p: ["x"]), \
                mock.patch.object(self.driver.os.path, "isdir", lambda p: True), \
                mock.patch.object(self.driver._startup(), "network",
                                  lambda docker: made.append((docker, len(self.checked))) or "nullius-osworld2"):
            self.option(SUFFIX)
            self.driver._ready("tasks", "001")
            self.driver._ready("tasks", "081")
            self.assertEqual(self.checked, [((SUFFIX,), {"state": False})])
            self.assertEqual(made, [(self.driver._asked, 0), (self.driver._asked, 0)])
            self.assertEqual(self.w.SITE, self.driver.SITE)
            self.option("none")
            with self.assertRaises(self.w.WebsiteError):
                self.driver._ready("tasks", "081")

    def test_a_hosts_snapshot_carries_the_module_and_the_override(self):
        from pathlib import Path

        from nullius.standard import payload
        tmp = Path(tempfile.mkdtemp(prefix="nullius-osworld2-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {"NULLIUS_HOME": str(tmp / "profile"), "NULLIUS_CATALOGUE": str(tmp / "catalogue")}):
            _, root = payload.build(str(tmp / "cache"))
        entry = Path(root) / "app" / "nullius" / "catalogue" / "benchmarks" / "osworld2"
        manifest = json.loads((Path(root) / "manifest.json").read_text())
        for name in ("websites.py", "websites.compose.yml"):
            self.assertEqual((entry / name).read_bytes(), (Path(HERE) / name).read_bytes(), name)
            self.assertIn(f"catalogue/benchmarks/osworld2/{name}", manifest["files"])

    def test_the_override_is_the_one_the_command_names(self):
        with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
            command = json.load(f)["driver"]["command"]
        self.assertEqual(command[-2], "{here}/websites.compose.yml")
        self.assertEqual(self.w.OVERRIDE, os.path.join(HERE, "websites.compose.yml"))
        given = os.path.join(self.tmp, "snapshot", "websites.compose.yml")
        spec = importlib.util.spec_from_file_location("osworld2_driver_argv", os.path.join(HERE, "driver.py"))
        driver = importlib.util.module_from_spec(spec)
        files = [os.path.join(HERE, n) for n in ("agents.json", "nop.py", os.path.join("startup", "sitecustomize.py"))]
        with mock.patch.object(sys, "argv", ["driver.py", *files, given]), \
                mock.patch.object(sys, "path", [HERE, *sys.path]), mock.patch.dict(os.environ, NULLIUS_UPSTREAM=self.tmp):
            spec.loader.exec_module(driver)
        self.addCleanup(setattr, driver.websites, "OVERRIDE", os.path.join(HERE, "websites.compose.yml"))
        self.assertEqual(driver.websites.OVERRIDE, given)

    def test_the_lists(self):
        lists = self.driver.LISTS
        gitlab_or_proxy = set("026 036 050 056 098".split())
        self.assertEqual(lists["websites35"], sorted(self.w.TASKS - gitlab_or_proxy))
        self.assertEqual(lists["websites98"], sorted(lists["no_services63"] + lists["websites35"]))
        self.assertEqual(len(set(lists["websites98"])), 98)
        self.assertFalse(set(lists["no_services63"]) & self.w.TASKS)
        with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
            bench = json.load(f)
        counted = {d["id"]: d["units"] for d in bench["datasets"]}
        self.assertEqual((counted["websites35"], counted["websites98"]), (35, 98))
        self.assertEqual(bench["options"]["websites"]["pattern"], "none|" + self.w.SUFFIX.pattern)
        with mock.patch.dict(os.environ, {"NULLIUS_DATASET": "", "NULLIUS_DATASET_ID": "websites98"}):
            self.assertEqual(len(self.driver.units()["units"]), 98)


class DriverSites(Case):
    """Overleaf down, or reset for one of its tasks, fails no other task's check."""

    def setUp(self):
        super().setUp()
        self.driver = load_driver(self.tmp)
        sites = self.driver.websites
        for target, name, fake in ((sites, "PROXIES", self.w.PROXIES), (sites, "_resolves", self.w._resolves),
                                   (sites, "LOCK", self.w.LOCK), (sites, "SITES_LOCK", self.w.SITES_LOCK),
                                   (self.driver, "_task_file", lambda *a: None), (self.driver, "_nat", lambda: True),
                                   (self.driver, "vm_image", lambda: "image"), (self.driver, "_provider_image", lambda: None),
                                   (self.driver, "_write_limit", lambda: None), (self.driver, "ASSETS", HERE),
                                   (self.driver._startup(), "network", lambda docker: "nullius-osworld2")):
            p = mock.patch.object(target, name, fake)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {sites.OPTION: SUFFIX})
        p.start()
        self.addCleanup(p.stop)

    def test_the_reaper_keeps_the_unit_s_locks_also_when_its_time_ran_out(self):
        given = []
        with mock.patch.object(self.driver.subprocess, "Popen", lambda *a, **kw: given.append(kw.get("pass_fds"))):
            with self.driver.websites.held("005", SUFFIX, threading.Event()):
                fds = self.driver.websites.held_fds()
                self.driver._reaper(1, self.tmp, True)
                with self.assertRaises(RuntimeError):
                    self.driver._stop(mock.Mock(pid=1), os.path.join(self.tmp, "x.log"), "the run", True)
            self.driver._reaper(1, self.tmp, True)
        self.assertEqual(len(fds), 1)
        self.assertEqual(given, [fds, fds, ()])

    def test_overleaf_down_fails_no_other_task(self):
        Sites.down.add("overleaf")
        self.assertEqual(self.driver._ready("tasks", "005"), "image")

    def test_no_site_s_state_is_read_for_a_unit_s_check(self):
        Sites.stateless.update(self.w.STATEFUL)
        self.assertEqual(self.driver._ready("tasks", "005"), "image")
        with self.assertRaises(self.w.WebsiteError):
            self.w.ready(SUFFIX)

    def test_overleaf_being_reset_for_its_task_fails_no_other_task(self):
        inside, released = threading.Event(), threading.Event()

        def anew(value, closed=None):
            Sites.down.add("overleaf")
            inside.set()
            released.wait(10)
            Sites.down.discard("overleaf")

        with mock.patch.object(self.driver.websites, "reset_overleaf", anew):
            def first():
                with self.driver.websites.held("057", SUFFIX, threading.Event()):
                    pass

            t = threading.Thread(target=first)
            t.start()
            self.assertTrue(inside.wait(10))
            self.assertEqual(self.driver._ready("tasks", "005"), "image")
            self.assertEqual(self.driver._ready("tasks", "072"), "image")
            released.set()
            t.join(10)


def _web():
    stack = os.environ.get("NULLIUS_OSWORLD2_WEB", "")
    return stack if stack and shutil.which("docker") and os.path.isfile(os.path.join(stack, "docker-compose.yml")) else ""


@unittest.skipUnless(_web(), "NULLIUS_OSWORLD2_WEB names no OSWorld-web, or no docker compose")
class Override(unittest.TestCase):
    def test_the_project_as_compose_reads_it(self):
        w = load()
        tiers = w.allocation(8, 80)
        settings = {"HOST_SUFFIX": SUFFIX, "CADDY_SCHEME": "http://", "OSWORLD2_WEB_ADDRESS": GATEWAY,
                    "OSWORLD2_WEB_DISK": "/dev/sda",
                    **{f"OSWORLD2_WEB_{t}_{k}": str(v[i]) for t, v in tiers.items() for i, k in enumerate(("BPS", "IOPS"))}}
        each, total = w.limits(settings)
        self.assertLessEqual(total, (8 * w.MB, 80))
        self.assertGreater(total[0], 8 * w.MB - 1024)
        said = subprocess.run(["docker", "compose", "-p", w.PROJECT, "-f", os.path.join(_web(), "docker-compose.yml"),
                               "-f", w.OVERRIDE, "config", "--format", "json"], capture_output=True, text=True,
                              env=dict(os.environ, **settings), timeout=120)
        self.assertEqual(said.returncode, 0, said.stderr)
        services = json.loads(said.stdout)["services"]
        self.assertEqual(len(services), 40)
        self.assertEqual(sorted(services), sorted(w.SERVICES))
        built = {n for n, s in services.items() if "build" in s}
        self.assertEqual(built, {s for g, group, _ in w.GROUPS if g != "pulled" for s in group})
        for name, service in services.items():
            self.assertEqual(service.get("blkio_config"), {
                "device_write_bps": [{"Path": "/dev/sda", "Rate": str(each[name][0])}],
                "device_write_iops": [{"Path": "/dev/sda", "Rate": str(each[name][1])}]}, name)
            self.assertEqual(service.get("logging"), {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}, name)
            if name != "caddy":
                self.assertNotIn("ports", service, name)
        self.assertEqual([(p["host_ip"], p["published"]) for p in services["caddy"]["ports"]], [(GATEWAY, "80")])
        labelled = {v for s in services.values() for k, v in (s.get("labels") or {}).items() if k == "caddy"}
        served = {h.strip()[len("http://"):-len(SUFFIX) - 1] for v in labelled for h in v.split(",")}
        self.assertEqual(served, set(w.NAMES))
        self.assertEqual(served - {"studio.streamview", "interfacinglinux", "overleaf"}, set(w.STATEFUL))


def _task_classes():
    up = os.environ.get("NULLIUS_OSWORLD2_UPSTREAM", "")
    return sorted(glob.glob(os.path.join(up, "evaluation_examples", "task_class", "task_*.py"))) if up else []


@unittest.skipUnless(_task_classes(), "NULLIUS_OSWORLD2_UPSTREAM names no upstream with the gated task classes")
class TaskClasses(unittest.TestCase):
    """The tasks that import the website controller, and those that name Overleaf's site."""

    def test_the_tasks_that_need_the_sites_and_overleaf_s(self):
        w = load()
        hosted, overleaf = set(), set()
        for p in _task_classes():
            n = os.path.basename(p)[5:-3]
            with open(p, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                modules = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                           else [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                if any(m.endswith("controllers.website") for m in modules) or (
                        isinstance(node, ast.ImportFrom) and (node.module or "").endswith("controllers")
                        and "website" in {a.name for a in node.names}):
                    hosted.add(n)
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                        node.value == "overleaf" or node.value.endswith("//overleaf.")):
                    overleaf.add(n)
        self.assertEqual(sorted(hosted), sorted(w.TASKS))
        self.assertEqual(sorted(overleaf), sorted(w.OVERLEAF_TASKS))


if __name__ == "__main__":
    unittest.main()
