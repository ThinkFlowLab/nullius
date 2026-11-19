"""OSWorld2's proxy for the tasks that ask for one: tinyproxy in a container of the unit's own, removed however
the driver ends. A fake docker on PATH and a fake task_loader stand in."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "nullius", "catalogue", "benchmarks", "osworld2")
DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  image) [ -z "$FAKE_NO_IMAGE" ] || exit 1; printf '%s\n' "$FAKE_IMAGE" ;;
  run) case "$*" in *"--entrypoint apk"*) echo "tinyproxy-1.11.3-r0 x86_64 {tinyproxy} (GPL-2.0-or-later) [installed]"; exit 0 ;; esac
    [ -z "$FAKE_RUN_SLEEP" ] || exec sleep "$FAKE_RUN_SLEEP"
    [ -z "$FAKE_RUN_FAIL" ] || { printf 'docker: Error: no such network\n\nRun %sdocker run --help%s for more information\n' "'" "'" >&2; exit 1; }
    printf '%s' "$TINYPROXY_CONF" > "$FAKE_CONF"; echo cid ;;
  inspect) [ -z "$FAKE_INSPECT_SLEEP" ] || sleep "$FAKE_INSPECT_SLEEP"
    if [ -s "$FAKE_INSPECT_SEQ" ]; then head -n 1 "$FAKE_INSPECT_SEQ"; sed -i 1d "$FAKE_INSPECT_SEQ"
    else printf '%s\\n' "$FAKE_INSPECT"; fi ;;
  ps) case "$*" in *--format*) [ -z "$FAKE_NAMES" ] || printf '%s\\n' "$FAKE_NAMES" ;;
    *) case "$FAKE_PS" in hang) exec sleep 5 ;; fail) echo "Error: daemon busy" >&2; exit 1 ;; esac
      printf '%s\\n' $FAKE_IDS ;; esac ;;
  rm) [ -z "$FAKE_RM_FAIL" ] || { echo "Error: busy" >&2; exit 1; } ;;
  build) cat > "$FAKE_BUILD" ;;
esac
"""
LOADER = """import json, os
def find_task_class_path(eid, base, domain, version):
    return True
def resolve_task_json_path(**kw):
    return ""
def load_task_config(path, task_id, **kw):
    return json.loads(os.environ["FAKE_TASK"])
"""


def load(name, path, up=None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", [path]), mock.patch.object(sys, "path", [HERE, *sys.path]), \
            mock.patch.dict(os.environ, NULLIUS_UPSTREAM=up or HERE):
        spec.loader.exec_module(module)
    return module


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Proxy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(DOCKER)
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.unit = os.path.join(self.tmp, "unit")
        os.makedirs(self.unit)
        self.calls = os.path.join(self.tmp, "calls")
        self.conf = os.path.join(self.tmp, "conf")
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.addCleanup(self.listener.close)
        env = mock.patch.dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"], FAKE_DOCKER_LOG=self.calls,
                              FAKE_CONF=self.conf, FAKE_INSPECT="running\t0\t127.0.0.1", FAKE_NAMES="", FAKE_IDS="",
                              FAKE_BUILD=os.path.join(self.tmp, "built"),
                              FAKE_INSPECT_SEQ=os.path.join(self.tmp, "inspect"),
                              FAKE_IMAGE='sha256:fake\t{"Labels":{"nullius.osworld2.proxy.package":"tinyproxy-1.11.3-r0"}}')
        env.start()
        self.addCleanup(env.stop)
        for name in ("FAKE_NO_IMAGE", "FAKE_RUN_FAIL", "FAKE_RUN_SLEEP", "FAKE_RM_FAIL", "FAKE_PS", "FAKE_INSPECT_SLEEP",
                     "TINYPROXY_CONF"):
            os.environ.pop(name, None)
        self.proxy = load("osworld2_proxy", os.path.join(HERE, "proxy.py"))
        self.proxy.PORT = self.listener.getsockname()[1]
        self.proxy.START_SECONDS = 3
        self.later, self.watchers = [], []
        self.proxy._later = lambda dir, name=None: self.later.append(dir)
        self.proxy._watcher = lambda dir, fd, name: self.watchers.append(
            (dir, os.path.exists(self.pool()), [ln for ln in self.lines() if ln.startswith("run ")]))

    def lines(self):
        try:
            with open(self.calls) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def ran(self):
        return [ln for ln in self.lines() if ln.startswith("run ")][-1]

    def pool(self):
        return os.path.join(self.unit, self.proxy.FILE)

    def held_open(self):
        fd = os.open(self.pool(), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        return fd

    def log(self):
        try:
            with open(os.path.join(self.unit, self.proxy.LOG)) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_a_proxy_is_on_the_vms_network_unpublished_read_only_and_marked_with_its_unit(self):
        path = self.proxy.start(self.unit)
        argv = self.ran().split()
        for flag in (["--network", "nullius-osworld2"], ["--read-only"], ["--cap-drop", "ALL"], ["--security-opt", "no-new-privileges"],
                     ["--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
                      "--log-opt", "compress=false"],
                     ["--label", f"{self.proxy.UNIT}={self.proxy._mark(self.unit)}"],
                     ["-e", self.proxy.CONF, self.proxy.IMAGE]):
            self.assertIn(" ".join(flag), " ".join(argv))
        self.assertTrue(any(a.startswith("/tmp:") for a in argv))
        self.assertFalse({"-p", "--publish", "-P", "--publish-all", "--privileged", "-v", "--volume"} & set(argv))
        self.assertNotIn("--device-write-bps", argv)
        self.assertTrue(argv[argv.index("--name") + 1].startswith(f"{self.proxy.NAME}{os.getuid()}-{os.getpid()}-"))
        self.assertEqual(path, self.pool())

    def test_the_network_is_the_vms_own_and_the_address_is_read_on_it(self):
        site = load("osworld2_startup_proxy", os.path.join(HERE, "startup", "sitecustomize.py"))
        self.assertEqual(self.proxy.NETWORK, site.NETWORK)
        self.proxy.start(self.unit)
        inspect = [ln for ln in self.lines() if ln.startswith("inspect ")][-1]
        self.assertIn('{{with index .NetworkSettings.Networks "nullius-osworld2"}}{{.IPAddress}}{{end}}', inspect)
        self.assertNotIn("bridge", inspect)

    def test_the_pool_file_names_the_container_and_the_units_password_and_only_its_owner_reads_it(self):
        self.proxy.start(self.unit)
        self.assertEqual(stat.S_IMODE(os.stat(self.pool()).st_mode), 0o600)
        with open(self.pool()) as f:
            entry, = json.load(f)
        self.assertEqual({k: entry[k] for k in ("host", "port", "username", "protocol")},
                         {"host": "127.0.0.1", "port": self.proxy.PORT, "username": self.proxy.USER, "protocol": "http"})
        self.assertRegex(entry["password"], r"^[0-9a-f]{32}$")
        with open(self.conf) as f:
            conf = f.read()
        self.assertIn(f"BasicAuth {self.proxy.USER} {entry['password']}\n", conf)
        self.assertNotIn(entry["password"], "\n".join(self.lines()))
        self.proxy.remove(self.unit)
        self.proxy.start(self.unit)
        with open(self.pool()) as f:
            self.assertNotEqual(json.load(f)[0]["password"], entry["password"])

    def test_no_log_record_of_a_process_the_driver_starts_shows_the_units_password(self):
        self.proxy.start(self.unit)
        with open(self.pool()) as f:
            word = json.load(f)[0]["password"]
        out = os.path.join(self.unit, "file.log")
        code = ("import logging, sys\n"
                "a = logging.getLogger('desktopenv'); a.setLevel(logging.INFO)\n"
                "a.addHandler(logging.StreamHandler(sys.stdout)); a.addHandler(logging.FileHandler(sys.argv[2]))\n"
                "a.handlers[0].setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))\n"
                "a.info('Command executed successfully: %s', 'Upstream http nullius:' + sys.argv[1] + '@h:1')\n"
                "a.info(f'export http_proxy=http://nullius:{sys.argv[1]}@h:1')\n"
                "try:\n    raise RuntimeError(sys.argv[1])\nexcept RuntimeError:\n    a.exception('failed')\n")
        env = dict(os.environ, PYTHONPATH=os.path.join(HERE, "startup"), PROXY_CONFIG_FILE=self.pool())
        said = subprocess.run([sys.executable, "-c", code, word, out], env=env, capture_output=True, text=True)
        with open(out) as f:
            written = said.stdout + said.stderr + f.read()
        self.assertEqual(said.returncode, 0, said.stderr)
        self.assertNotIn(word, written)
        self.assertEqual(written.count("<proxy password>"), 6)
        env.pop("PROXY_CONFIG_FILE")
        said = subprocess.run([sys.executable, "-c", code, word, out], env=env, capture_output=True, text=True)
        self.assertEqual(said.stdout.count(word), 3)

    def test_a_pool_file_without_a_password_hides_nothing_and_a_second_look_wraps_once(self):
        site = load("osworld2_startup_hide", os.path.join(HERE, "startup", "sitecustomize.py"))
        self.addCleanup(setattr, logging.Formatter, "format", logging.Formatter.format)
        self.addCleanup(setattr, logging, "raiseExceptions", logging.raiseExceptions)
        plain, path = logging.Formatter.format, os.path.join(self.unit, "pool.json")
        for text in ("{", "{}", '[{"password": 1}]', '[{"password": ""}]', "[1]"):
            with open(path, "w") as f:
                f.write(text)
            site.hide_proxy_password({"PROXY_CONFIG_FILE": path})
            self.assertIs(logging.Formatter.format, plain)
        site.hide_proxy_password({"PROXY_CONFIG_FILE": os.path.join(self.unit, "none.json")})
        site.hide_proxy_password({})
        self.assertIs(logging.Formatter.format, plain)
        with open(path, "w") as f:
            json.dump([{"password": "p" * 32}], f)
        site.hide_proxy_password({"PROXY_CONFIG_FILE": path})
        hidden = logging.Formatter.format
        site.hide_proxy_password({"PROXY_CONFIG_FILE": path})
        self.assertIs(logging.Formatter.format, hidden)
        self.assertFalse(logging.raiseExceptions)
        record = logging.LogRecord("a", logging.INFO, "", 0, "x %s", ("p" * 32,), None)
        self.assertEqual(logging.Formatter().format(record), "x <proxy password>")

    def test_tinyproxy_logs_warnings_only_to_its_own_output_and_goes_out_directly(self):
        lines = self.proxy.config("t" * 32).splitlines()
        self.assertIn("LogLevel Warning", lines)
        self.assertIn(f"Port {self.proxy.PORT}", lines)
        self.assertFalse([ln for ln in lines if ln.split()[0] in ("Upstream", "LogFile", "Syslog", "Listen")])

    def test_connect_goes_to_the_webs_ports_only_and_clients_with_the_main_thread_stay_below_its_processes(self):
        lines = self.proxy.config("t" * 32).splitlines()
        self.assertEqual(sorted(ln for ln in lines if ln.startswith("ConnectPort ")), ["ConnectPort 443", "ConnectPort 80"])
        self.assertIn(f"MaxClients {self.proxy.CLIENTS}", lines)
        self.proxy.start(self.unit)
        argv = self.ran().split()
        self.assertEqual(int(argv[argv.index("--pids-limit") + 1]), self.proxy.PIDS)
        self.assertLess(self.proxy.CLIENTS + 1, self.proxy.PIDS,
                        "a thread per client and tinyproxy's main thread all count against the container's pids limit")

    def test_the_image_is_alpine_pinned_by_digest_named_by_its_text_and_built_only_by_hand(self):
        self.assertRegex(self.proxy.DOCKERFILE.splitlines()[0], r"^FROM alpine:[\d.]+@sha256:[0-9a-f]{64}$")
        self.assertIn("tinyproxy -d -c /tmp/tinyproxy.conf", self.proxy.DOCKERFILE)
        self.assertIn("USER 65534", self.proxy.DOCKERFILE)
        os.environ["FAKE_NO_IMAGE"] = "1"
        with self.assertRaises(RuntimeError) as raised:
            self.proxy.start(self.unit)
        self.assertIn("python3 proxy.py --build", str(raised.exception))
        self.assertFalse([ln for ln in self.lines() if ln.startswith(("run ", "build "))])
        self.assertFalse(os.path.exists(self.pool()))
        built = subprocess.run([sys.executable, os.path.join(HERE, "proxy.py"), "--build"], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        builds = [ln for ln in self.lines() if ln.startswith("build ")]
        self.assertEqual(builds, [f"build -t {self.proxy.IMAGE} -",
                                  f"build --label {self.proxy.PACKAGE}=tinyproxy-1.11.3-r0 -t {self.proxy.IMAGE} -"])
        self.assertIn(f"run --rm --network none --entrypoint apk {self.proxy.IMAGE} list --installed tinyproxy", self.lines())
        self.assertEqual(built.stdout.split(), [self.proxy.IMAGE, "tinyproxy-1.11.3-r0"])
        with open(os.environ["FAKE_BUILD"]) as f:
            self.assertEqual(f.read(), self.proxy.DOCKERFILE)

    def test_each_start_writes_the_images_id_and_tinyproxy_package_into_the_units_log(self):
        self.proxy.start(self.unit)
        self.assertRegex(self.log()[-1], r"\] proxy image sha256:fake, tinyproxy-1\.11\.3-r0$")
        proxy = load("osworld2_proxy_unlabelled", os.path.join(HERE, "proxy.py"))
        proxy.PORT, proxy._watcher = self.proxy.PORT, self.proxy._watcher
        os.environ["FAKE_IMAGE"] = 'sha256:old\t{"User":"65534:65534"}'
        proxy.start(self.unit)
        self.assertIn("proxy image sha256:old, its tinyproxy package not labelled", self.log()[-1])

    def test_a_process_looks_at_the_image_and_its_users_orphans_at_its_first_start_only(self):
        for _ in range(2):
            self.proxy.start(self.unit)
        self.assertEqual(len([ln for ln in self.lines() if ln.startswith("image inspect")]), 1)
        self.assertEqual(len([ln for ln in self.lines() if ln.startswith("ps ") and "--format" in ln]), 1)
        self.assertEqual(len([ln for ln in self.log() if "proxy image" in ln]), 2)

    def test_the_proxy_and_its_units_vm_write_no_more_together_than_the_options_give_the_vm(self):
        site = load("osworld2_startup_proxy", os.path.join(HERE, "startup", "sitecustomize.py"))
        options = {"NULLIUS_OPTION_WRITE_MBPS": "8", "NULLIUS_OPTION_WRITE_IOPS": "80"}
        with self.proxy.held(self.unit, True, lambda: "/dev/sda") as routed:
            argv = self.ran().split()
            vm = {key: rate for key, rate, _, _ in site.wanted(dict(options, **routed))}
        own = {key: int(argv[argv.index(flag) + 1].rpartition(":")[2])
               for key, flag in (("device_write_bps", "--device-write-bps"), ("device_write_iops", "--device-write-iops"))}
        self.assertEqual(own, {"device_write_bps": 52428, "device_write_iops": 1})
        self.assertEqual({k: vm[k] + own[k] for k in vm}, {"device_write_bps": 8 * 1024 * 1024, "device_write_iops": 80})
        self.assertEqual({key: rate for key, rate, _, _ in site.wanted(options)},
                         {"device_write_bps": 8 * 1024 * 1024, "device_write_iops": 80})
        with self.proxy.held(self.unit, True, lambda: None) as routed:
            self.assertEqual(routed, {"PROXY_CONFIG_FILE": self.pool()})
        self.assertNotIn("--device-write-bps", self.ran())
        for iops, taken in (("1", "1"), ("80", "0x")):
            with self.subTest(iops=iops, taken=taken), self.assertRaises(site.WriteLimitError):
                site.wanted(dict(options, NULLIUS_OPTION_WRITE_IOPS=iops, OSWORLD2_PROXY_WRITE_IOPS=taken))

    def test_the_machines_limits_together_stay_within_its_budget_with_the_sites_and_a_proxy_per_unit(self):
        site = load("osworld2_startup_budget", os.path.join(HERE, "startup", "sitecustomize.py"))
        web = load("osworld2_websites_budget", os.path.join(HERE, "websites.py"))
        mb = 1024 * 1024
        for (budget, units, vm, stack) in (((120, 1200), 7, (16, 160), (8, 80)), ((30, 300), 1, (22, 220), (8, 80)),
                                           ((30, 300), 2, (11, 110), (8, 80))):
            options = {"NULLIUS_OPTION_WRITE_MBPS": str(vm[0]), "NULLIUS_OPTION_WRITE_IOPS": str(vm[1])}
            tiers = web.allocation(*stack)
            sites = web.limits({f"OSWORLD2_WEB_{t}_{k}": str(v[i]) for t, v in tiers.items()
                                for i, k in enumerate(("BPS", "IOPS"))})[1]
            for routed in ({}, self.proxy.TAKEN):
                with self.subTest(budget=budget, units=units, proxy=bool(routed)):
                    own = {key: rate for key, rate, _, _ in site.wanted(dict(options, **routed))}
                    each = (own["device_write_bps"] + (self.proxy.WRITE_BPS if routed else 0),
                            own["device_write_iops"] + (self.proxy.WRITE_IOPS if routed else 0))
                    total = (units * each[0] + sites[0], units * each[1] + sites[1])
                    self.assertLessEqual(total[0], budget[0] * mb)
                    self.assertLessEqual(total[1], budget[1])

    def test_a_removal_that_fails_on_the_way_out_is_a_line_in_the_units_log(self):
        os.environ.update(FAKE_IDS="cid", FAKE_RM_FAIL="1")
        with self.proxy.held(self.unit, True):
            pass
        lines = [ln for ln in self.log() if "proxy image" not in ln]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] the unit's proxy was not removed: .*busy")
        self.assertTrue(os.path.exists(self.pool()))

    def test_a_proxy_that_stops_or_never_answers_is_an_error_and_is_removed(self):
        os.environ["FAKE_INSPECT"] = "exited\t1\t"
        os.environ["FAKE_IDS"] = "cid"
        with self.assertRaises(RuntimeError) as raised, self.proxy.held(self.unit, True):
            pass
        self.assertIn("exit code 1", str(raised.exception))
        self.assertFalse([ln for ln in self.lines() if ln.startswith("logs ")])
        self.assertIn("rm -f cid", self.lines())
        self.assertFalse(os.path.exists(self.pool()))
        os.environ["FAKE_INSPECT"] = "running\t0\t127.0.0.1"
        self.proxy.PORT = free_port()
        with self.assertRaises(RuntimeError) as raised:
            self.proxy.start(self.unit)
        self.assertIn("did not answer", str(raised.exception))

    def test_a_refused_container_is_an_error_with_dockers_words(self):
        os.environ["FAKE_RUN_FAIL"] = "1"
        with self.assertRaises(RuntimeError) as raised, self.proxy.held(self.unit, True):
            pass
        self.assertTrue(str(raised.exception).endswith("docker: Error: no such network"), str(raised.exception))
        self.assertFalse(os.path.exists(self.pool()))

    def test_held_gives_the_pool_while_the_body_runs_and_removes_the_proxy_however_it_ends(self):
        os.environ["FAKE_IDS"] = "cid"
        with self.proxy.held(self.unit, True, lambda: None) as routed:
            self.assertEqual(routed, {"PROXY_CONFIG_FILE": self.pool()})
            self.assertTrue(os.path.exists(self.pool()))
        self.assertFalse(os.path.exists(self.pool()))
        with self.assertRaises(KeyError), self.proxy.held(self.unit, True):
            raise KeyError("the run")
        self.assertFalse(os.path.exists(self.pool()))
        self.assertEqual([ln for ln in self.lines() if ln.startswith("rm ")], ["rm -f cid", "rm -f cid"])

    def test_a_task_without_a_proxy_costs_no_docker_call_nor_the_disk(self):
        def disk():
            raise AssertionError("asked")

        with self.proxy.held(self.unit, False, disk) as routed:
            self.assertEqual(routed, {})
        self.proxy.remove(self.unit)
        self.assertEqual(self.lines(), [])

    def test_a_proxy_docker_did_not_remove_keeps_the_pool_file_for_the_next_sweep(self):
        self.proxy.start(self.unit)
        os.environ.update(FAKE_IDS="cid", FAKE_RM_FAIL="1")
        with self.assertRaises(RuntimeError):
            self.proxy.remove(self.unit)
        self.assertTrue(os.path.exists(self.pool()))
        del os.environ["FAKE_RM_FAIL"]
        self.proxy.remove(self.unit)
        self.assertFalse(os.path.exists(self.pool()))
        search = [ln for ln in self.lines() if ln.startswith("ps -a -q")][-1]
        self.assertIn(f"label={self.proxy.UNIT}={self.proxy._mark(self.unit)}", search)

    def test_a_start_removes_this_users_proxies_whose_driver_is_gone_and_only_those(self):
        ended = subprocess.Popen([sys.executable, "-c", ""])
        ended.wait()
        prefix = f"{self.proxy.NAME}{os.getuid()}-"
        dead, alive = f"{prefix}{ended.pid}-1", f"{prefix}{os.getpid()}-2"
        other = f"{self.proxy.NAME}{os.getuid() + 1}-{ended.pid}-3"
        os.environ["FAKE_NAMES"] = "\n".join([dead, alive, other, f"{prefix}junk"])
        self.proxy.start(self.unit)
        self.assertIn(f"rm -f {dead}", self.lines())
        self.assertFalse([ln for ln in self.lines() if ln.startswith("rm ") and (alive in ln or other in ln)])

    def test_a_container_the_daemon_creates_after_a_start_it_did_not_answer_is_still_removed(self):
        self.proxy.DOCKER_SECONDS = 0.5
        os.environ["FAKE_RUN_SLEEP"] = "5"
        with self.assertRaises(RuntimeError) as raised, self.proxy.held(self.unit, True):
            pass
        self.assertIn("docker run got no answer in 0.5 s", str(raised.exception))
        self.assertIn("has made no container for the proxy yet; the unit's sweeper removes one it makes later",
                      str(raised.exception))
        self.assertEqual(len([ln for ln in self.lines() if ln.startswith("run ")]), 1)
        self.assertEqual(os.path.getsize(self.pool()), 0)
        self.assertEqual(self.later, [self.unit])
        del os.environ["FAKE_RUN_SLEEP"]
        self.proxy.remove(self.unit)
        self.assertTrue(os.path.exists(self.pool()))
        os.environ["FAKE_IDS"] = "late"
        self.proxy.SWEEPS = (0, 0)
        self.proxy.sweep(self.unit, self.held_open())
        self.assertIn("rm -f late", self.lines())
        self.assertFalse(os.path.exists(self.pool()))

    def sweep_with_a_clock(self, docker_answers_at=None):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
            if docker_answers_at is not None and clock[0] >= docker_answers_at:
                os.environ.pop("FAKE_RM_FAIL", None)

        self.proxy.time = types.SimpleNamespace(sleep=sleep, monotonic=lambda: clock[0], strftime=time.strftime)
        with open(self.pool(), "w") as f:
            f.write("[]")
        os.environ.update(FAKE_IDS="cid", FAKE_RM_FAIL="1")
        self.proxy.sweep(self.unit, self.held_open(), "name")
        return clock[0], [ln for ln in self.lines() if ln.startswith("rm ")]

    def test_a_sweeper_keeps_removing_while_docker_refuses_and_stops_once_it_succeeds(self):
        ended, removals = self.sweep_with_a_clock(docker_answers_at=sum(self.proxy.SWEEPS) + 2 * self.proxy.RETRY_SECONDS)
        self.assertFalse(os.path.exists(self.pool()))
        self.assertFalse(os.path.exists(os.path.join(self.unit, self.proxy.LOG)))
        self.assertEqual(ended, sum(self.proxy.SWEEPS) + 2 * self.proxy.RETRY_SECONDS)
        self.assertEqual(len(removals), len(self.proxy.SWEEPS) + 2)

    def test_a_sweeper_whose_units_record_is_gone_waits_for_no_further_pass(self):
        ended, removals = self.sweep_with_a_clock(docker_answers_at=0)
        self.assertFalse(os.path.exists(self.pool()))
        self.assertEqual(ended, self.proxy.SWEEPS[0])
        self.assertEqual(len(removals), 1)

    def test_a_sweeper_docker_refuses_throughout_gives_up_in_time_with_a_line_in_the_units_log(self):
        ended, removals = self.sweep_with_a_clock()
        self.assertLessEqual(ended, self.proxy.SWEEP_SECONDS)
        self.assertGreater(ended, self.proxy.SWEEP_SECONDS - self.proxy.RETRY_SECONDS)
        self.assertGreater(len(removals), len(self.proxy.SWEEPS))
        self.assertTrue(os.path.exists(self.pool()))
        lines = self.log()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], rf"the unit's proxy was not removed in {self.proxy.SWEEP_SECONDS // 60} min: .*busy$")

    def test_a_start_docker_answers_late_goes_on_with_its_container_and_never_runs_a_second(self):
        self.proxy.DOCKER_SECONDS = 0.5
        os.environ.update(FAKE_RUN_SLEEP="5", FAKE_IDS="cid")
        path = self.proxy.start(self.unit)
        self.assertEqual(len([ln for ln in self.lines() if ln.startswith("run ")]), 1)
        name = self.ran().split()[self.ran().split().index("--name") + 1]
        self.assertIn(f"ps -a -q --filter label={self.proxy.UNIT}={self.proxy._mark(self.unit)} --filter name={name}",
                      self.lines())
        with open(path) as f:
            self.assertEqual(json.load(f)[0]["host"], "127.0.0.1")

    def sweep_with_slow_docker(self, found):
        clock, calls = [0.0], []

        def sleep(seconds):
            clock[0] += seconds

        def run(argv, timeout=None, **kw):
            calls.append((clock[0], timeout))
            if argv[1] == "ps" and timeout >= 119:
                clock[0] += 119
                return subprocess.CompletedProcess(argv, 0, found, "")
            clock[0] += timeout
            raise subprocess.TimeoutExpired(argv, timeout)

        self.proxy.time = types.SimpleNamespace(sleep=sleep, monotonic=lambda: clock[0], strftime=time.strftime)
        self.proxy.subprocess = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired,
                                                      CompletedProcess=subprocess.CompletedProcess)
        open(self.pool(), "w").close()
        self.proxy.sweep(self.unit, self.held_open(), "first")
        for began, timeout in calls:
            self.assertLessEqual(timeout, self.proxy.DOCKER_SECONDS)
            self.assertLessEqual(began + timeout, self.proxy.SWEEP_SECONDS + 0.1)
        return clock[0], calls

    def test_a_sweep_whose_searches_take_minutes_and_removals_never_answer_ends_in_its_time(self):
        ended, calls = self.sweep_with_slow_docker("cid\n")
        self.assertLessEqual(ended, self.proxy.SWEEP_SECONDS + 0.1)
        self.assertGreater(len(calls), 2 * len(self.proxy.SWEEPS) - 2)
        self.assertTrue(os.path.exists(self.pool()))
        self.assertRegex(self.log()[-1], rf"was not removed in {self.proxy.SWEEP_SECONDS // 60} min: docker ")

    def test_a_sweep_whose_searches_take_minutes_still_removes_its_record_in_its_time(self):
        ended, calls = self.sweep_with_slow_docker("")
        self.assertLessEqual(ended, self.proxy.SWEEP_SECONDS)
        self.assertFalse(os.path.exists(self.pool()))
        self.assertEqual(self.log(), [])

    def test_a_sweeper_leaves_a_later_start_in_the_same_folder_its_proxy_and_its_record(self):
        with open(self.pool(), "w") as f:
            f.write("[]")
        own = self.held_open()
        os.remove(self.pool())
        with open(self.pool(), "w") as f:
            f.write('[{"host": "later"}]')
        os.environ["FAKE_IDS"] = "later"
        self.proxy.SWEEPS = (0, 0)
        self.proxy.sweep(self.unit, own, "first")
        self.assertFalse([ln for ln in self.lines() if ln.startswith(("rm ", "ps -a -q"))])
        self.assertTrue(os.path.exists(self.pool()))
        with self.proxy._locked(self.unit):
            self.assertFalse(self.proxy._ours(self.unit, own))

    def test_a_sweeper_removes_its_own_starts_container_by_its_name(self):
        with open(self.pool(), "w") as f:
            f.write("[]")
        os.environ["FAKE_IDS"] = "cid"
        self.proxy.SWEEPS = (0, 0)
        self.proxy.sweep(self.unit, self.held_open(), "first")
        self.assertIn(f"ps -a -q --filter label={self.proxy.UNIT}={self.proxy._mark(self.unit)} --filter name=first",
                      self.lines())
        self.assertIn("rm -f cid", self.lines())
        self.assertFalse(os.path.exists(self.pool()))

    def test_a_start_whose_run_and_search_docker_both_leave_unanswered_is_an_error_for_its_sweeper(self):
        self.proxy.DOCKER_SECONDS = 0.5
        for said, words in (("hang", "docker ps got no answer in 0.5 s: is its daemon running?: whether it made the "
                                     "proxy's container is unknown"),
                            ("fail", "could not say whether it made the proxy's container: Error: daemon busy")):
            with self.subTest(said=said):
                os.environ.update(FAKE_RUN_SLEEP="5", FAKE_PS=said)
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self.pool())
                with self.assertRaises(RuntimeError) as raised:
                    self.proxy.start(self.unit)
                self.assertIn("docker run got no answer in 0.5 s", str(raised.exception))
                self.assertIn(words, str(raised.exception))
                self.assertTrue(str(raised.exception).endswith("the unit's sweeper removes one it makes later"))
                self.assertEqual(os.path.getsize(self.pool()), 0)

    def test_a_start_with_slow_answers_ends_within_its_time_however_docker_answers(self):
        self.proxy.START_SECONDS = 1.5
        os.environ["FAKE_INSPECT_SLEEP"] = "0.4"
        os.environ["FAKE_INSPECT"] = "created\t0\t"
        t0 = time.monotonic()
        with self.assertRaises(RuntimeError) as raised:
            self.proxy.start(self.unit)
        self.assertLess(time.monotonic() - t0, self.proxy.START_SECONDS + 0.5)
        self.assertRegex(str(raised.exception), "did not answer in its start's 1.5 s|the time given for it ran out|"
                                                "got no answer in")

    def test_a_container_created_and_not_yet_started_is_waited_for(self):
        with open(os.environ["FAKE_INSPECT_SEQ"], "w") as f:
            f.write("created\t0\t\ncreated\t0\t\n")
        self.proxy.start(self.unit)
        self.assertEqual(len([ln for ln in self.lines() if ln.startswith("inspect ")]), 3)

    def test_the_waits_are_sized_for_a_shared_daemon_that_takes_minutes_to_answer(self):
        proxy = load("osworld2_proxy_waits", os.path.join(HERE, "proxy.py"))
        self.assertGreaterEqual(proxy.DOCKER_SECONDS, 120)
        self.assertGreater(proxy.START_SECONDS, 2 * proxy.DOCKER_SECONDS)
        self.assertGreaterEqual(proxy.RETRY_SECONDS, proxy.DOCKER_SECONDS)
        self.assertGreaterEqual(sum(proxy.SWEEPS), 900)
        self.assertGreater(proxy.SWEEP_SECONDS, sum(proxy.SWEEPS) + proxy.RETRY_SECONDS)

    def test_a_late_container_whose_unit_holds_no_record_is_an_orphan_while_its_driver_lives(self):
        prefix = f"{self.proxy.NAME}{os.getuid()}-{os.getpid()}"
        kept, late = f"{prefix}-1", f"{prefix}-2"
        os.makedirs(os.path.join(self.tmp, "other"))
        open(os.path.join(self.tmp, "other", self.proxy.FILE), "w").close()
        os.environ["FAKE_NAMES"] = f"{kept}\t{os.path.join(self.tmp, 'other')}\n{late}\t{self.unit}"
        self.proxy._orphans()
        self.assertIn(f"rm -f {late}", self.lines())
        self.assertFalse([ln for ln in self.lines() if ln.startswith("rm ") and kept in ln])

    def test_a_proxy_that_started_and_was_removed_needs_no_sweeper(self):
        with self.proxy.held(self.unit, True):
            pass
        self.assertEqual(self.later, [])
        self.assertIn(f"--label {self.proxy.FOLDER}={self.unit}", self.ran())

    def test_the_sweeper_is_a_session_of_its_own_holding_its_starts_record_and_naming_its_container(self):
        proxy = load("osworld2_proxy_later", os.path.join(HERE, "proxy.py"))
        open(self.pool(), "w").close()
        with mock.patch.object(proxy.subprocess, "Popen") as popen:
            proxy._later(self.unit, "first")
        argv, kw = popen.call_args
        fd, = kw["pass_fds"]
        self.assertEqual(argv[0][1:], [os.path.join(HERE, "proxy.py"), "--sweep", self.unit, str(fd), "first"])
        self.assertTrue(kw["start_new_session"])

    def test_a_start_arms_its_watcher_once_its_record_exists_and_before_docker_is_asked(self):
        self.proxy.start(self.unit)
        self.assertEqual(self.watchers, [(self.unit, True, [])])

    def test_the_watcher_sweeps_the_units_proxy_once_its_driver_is_gone_and_only_then(self):
        ended = subprocess.Popen([sys.executable, "-c", ""])
        ended.wait()
        self.proxy.SWEEPS = (0, 0)
        open(self.pool(), "w").close()
        gone = self.held_open()
        os.remove(self.pool())
        self.proxy.watch(self.unit, ended.pid, gone, "first")
        self.assertEqual(self.lines(), [])
        open(self.pool(), "w").close()
        os.environ["FAKE_IDS"] = "cid"
        self.proxy.watch(self.unit, ended.pid, self.held_open(), "first")
        self.assertIn("rm -f cid", self.lines())
        self.assertFalse(os.path.exists(self.pool()))

    def test_a_driver_killed_while_its_proxy_starts_leaves_the_proxy_to_its_watcher(self):
        os.environ.update(FAKE_RUN_SLEEP="60", FAKE_IDS="cid")
        driver = subprocess.Popen([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import proxy; "
                                   "proxy.start(sys.argv[2])", HERE, self.unit], start_new_session=True)
        self.addCleanup(self.end_watchers)
        t0 = time.time()
        while not any(ln.startswith("run ") for ln in self.lines()) and time.time() - t0 < 20:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(self.pool()))
        os.killpg(driver.pid, signal.SIGKILL)
        while "rm -f cid" not in self.lines() and time.time() - t0 < 40:
            time.sleep(0.2)
        driver.wait()
        self.assertIn("rm -f cid", self.lines())

    def end_watchers(self):
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
            except OSError:
                continue
            if b"--watch" in argv and self.unit.encode() in argv:
                with contextlib.suppress(OSError):
                    os.kill(int(pid), signal.SIGKILL)

    def test_an_attempts_leftover_proxy_is_removed_before_a_new_one(self):
        open(self.pool(), "w").close()
        os.environ["FAKE_IDS"] = "old"
        self.proxy.start(self.unit)
        lines = self.lines()
        self.assertLess(lines.index("rm -f old"), lines.index(self.ran()))


class Driver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.driver = load("osworld2_driver_proxy", os.path.join(HERE, "driver.py"), self.tmp)
        self.envs, self.held, self.asked = [], [], []

        def start(dir, disk=None):
            self.held.append(("start", disk))
            return os.path.join(dir, "proxy.json")

        def run(argv, cwd, env, *a, **kw):
            self.envs.append(env)

        for target, name, fake in ((self.driver, "_vm", lambda dir, d, e: os.path.join(dir, "vm.qcow2")),
                                   (self.driver, "_run", run), (self.driver, "_disk", lambda: self.asked.append(1) or "/dev/sda"),
                                   (self.driver.proxy, "start", start),
                                   (self.driver.proxy, "remove", lambda dir: self.held.append(("remove", dir)))):
            p = mock.patch.object(target, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def unit(self, asks):
        p = mock.patch.object(self.driver, "_task", lambda *_: {"instruction": "", "proxy": asks})
        p.start()
        self.addCleanup(p.stop)

    def both(self):
        with self.assertRaises(RuntimeError):
            self.driver.answer("tasks__037", self.tmp, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1", "k")
        with self.assertRaises(RuntimeError):
            self.driver.score("tasks__037", self.tmp)

    def test_a_task_that_asks_for_a_proxy_runs_with_its_own_in_the_original_and_the_null_control(self):
        self.unit(True)
        self.both()
        self.assertEqual([e.get("PROXY_CONFIG_FILE") for e in self.envs], [os.path.join(self.tmp, "proxy.json")] * 2)
        self.assertEqual(self.held, [("start", "/dev/sda"), ("remove", self.tmp)] * 2)

    def test_a_task_that_asks_for_none_gets_no_proxy_nor_the_variable(self):
        self.unit(False)
        with mock.patch.dict(os.environ):
            os.environ.pop("PROXY_CONFIG_FILE", None)
            self.both()
        self.assertEqual([e.get("PROXY_CONFIG_FILE") for e in self.envs], [None, None])
        self.assertEqual((self.held, self.asked), ([], []))

    def test_the_vms_removal_removes_the_units_proxy(self):
        with mock.patch.object(self.driver, "_mine", lambda dir: ([], [])):
            self.driver._remove_vms(self.tmp)
        self.assertEqual(self.held, [("remove", self.tmp)])

    def test_a_write_limit_that_leaves_the_vm_nothing_beside_the_proxy_is_refused_before_it_starts(self):
        driver = load("osworld2_driver_share", os.path.join(HERE, "driver.py"), self.tmp)
        with mock.patch.dict(os.environ, NULLIUS_OPTION_WRITE_MBPS="8", NULLIUS_OPTION_WRITE_IOPS="1"), \
                mock.patch.object(driver, "_asked", mock.Mock(side_effect=AssertionError("asked"))):
            with self.assertRaises(driver._startup().WriteLimitError) as raised:
                driver._disk()
        self.assertIn("Disk write IOPS is 1, no more than the unit's proxy takes", str(raised.exception))

    def test_the_proxys_disk_is_the_vms_one_only_with_a_write_limit_set(self):
        driver = load("osworld2_driver_disk", os.path.join(HERE, "driver.py"), self.tmp)
        site = mock.Mock()
        site.root_disk.return_value = "/dev/sdb"
        asked = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "/var/lib/docker\n", ""))
        with mock.patch.object(driver, "_startup", lambda: site), mock.patch.object(driver, "_asked", asked):
            with mock.patch.dict(os.environ, NULLIUS_OPTION_WRITE_MBPS="none", NULLIUS_OPTION_WRITE_IOPS="none"):
                self.assertIsNone(driver._disk())
            asked.assert_not_called()
            with mock.patch.dict(os.environ, NULLIUS_OPTION_WRITE_MBPS="30", NULLIUS_OPTION_WRITE_IOPS="none"):
                self.assertEqual(driver._disk(), "/dev/sdb")
        site.root_disk.assert_called_once_with("/var/lib/docker")


class Flag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        with open(os.path.join(self.tmp, "task_loader.py"), "w") as f:
            f.write(LOADER)

    def loaded(self, task):
        out = os.path.join(self.tmp, "out.json")
        done = subprocess.run([sys.executable, os.path.join(HERE, "nop.py"), "--instruction", self.tmp, "tasks", "037", out],
                              env=dict(os.environ, PYTHONPATH=self.tmp, FAKE_TASK=json.dumps(task)),
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(out) as f:
            return json.load(f)

    def test_nop_says_whether_the_task_asks_for_a_proxy_as_the_upstream_reads_it(self):
        self.assertEqual(self.loaded({"instruction": "x", "proxy": True}), {"instruction": "x", "proxy": True})
        self.assertEqual(self.loaded({"instruction": "x"}), {"instruction": "x", "proxy": False})


if __name__ == "__main__":
    unittest.main()
