"""OSWorld2's VM container create (startup/sitecustomize.py) with a busy Docker daemon: one container or none.
A fake docker-py, requests and docker command line stand in."""
from __future__ import annotations

import importlib.util
import ipaddress
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nullius", "catalogue",
                    "benchmarks", "osworld2")
STARTUP = os.path.join(HERE, "startup")
PROVIDER = "happysixd/osworld-docker@sha256:" + "0" * 64
NONE = {"NULLIUS_OPTION_WRITE_MBPS": "none", "NULLIUS_OPTION_WRITE_IOPS": "none"}
LIMITS = {"NULLIUS_OPTION_WRITE_MBPS": "10", "NULLIUS_OPTION_WRITE_IOPS": "100"}
DEVICE = "/dev/sdz"


class Timeout(Exception):
    pass


class ReadTimeout(Timeout):
    pass


class ConnectionError_(Exception):
    pass


class ConnectTimeout(ConnectionError_, Timeout):
    pass


class APIError(Exception):
    pass


class Daemon:
    """A fake daemon's create: slow (``seconds``), made ``late``, a slow ``inspect``, refused, or limits dropped (``drop``)."""

    def __init__(self, seconds=0, late=0, refuse=False, error=ReadTimeout, drop=False, inspect=0):
        self.seconds, self.late, self.refuse, self.error, self.drop = seconds, late, refuse, error, drop
        self.inspect = inspect
        self.containers, self.creates, self.list_timeouts, self.clock = [], 0, [], 0.0
        self.pending, self.gave_up, self.info_timeouts = None, None, []


class Container:
    def __init__(self, daemon, image, kwargs):
        self.daemon, self.image, self.kwargs, self.started, self.removed = daemon, image, kwargs, False, None
        self.attrs = {}

    @property
    def labels(self):
        return self.kwargs.get("labels") or {}

    def reload(self):
        kept = {} if self.daemon.drop else self.kwargs
        self.attrs = {"HostConfig": {"BlkioDeviceWriteBps": kept.get("device_write_bps"),
                                     "BlkioDeviceWriteIOps": kept.get("device_write_iops")}}

    def remove(self, **kwargs):
        self.removed = kwargs

    def start(self):
        self.started = True


class API:
    def __init__(self, daemon, timeout=60):
        self.daemon, self.timeout = daemon, timeout


class Client:
    def __init__(self, daemon, timeout=60):
        self.api = API(daemon, timeout)

    def info(self):
        self.api.daemon.info_timeouts.append(self.api.timeout)
        return {"DockerRootDir": "/var/lib/docker"}


class ContainerCollection:
    def __init__(self, client):
        self.client = client

    def create(self, image, command=None, **kwargs):
        d = self.client.api.daemon
        d.creates += 1
        if d.refuse:
            raise APIError("refused")
        if d.seconds > self.client.api.timeout:
            d.clock = d.gave_up = self.client.api.timeout
            if d.late is not None:
                d.pending = Container(d, image, kwargs)
            raise d.error("gave up")
        d.containers.append(Container(d, image, kwargs))
        if d.inspect > self.client.api.timeout:
            d.clock = d.gave_up = 2 * self.client.api.timeout
            raise d.error("gave up")
        return d.containers[-1]

    def list(self, all=False, filters=None):
        d = self.client.api.daemon
        d.list_timeouts.append(self.client.api.timeout)
        if d.pending is not None and d.clock - d.gave_up >= d.late:
            d.containers.append(d.pending)
            d.pending = None
        key, _, value = (filters or {}).get("label", "").partition("=")
        return [c for c in d.containers if c.labels.get(key) == value]

    def run(self, image, command=None, **kwargs):
        container = self.create(image=image, command=command, **kwargs)
        container.start()
        return container


class Patient(unittest.TestCase):
    def setUp(self):
        self.containers = types.ModuleType("docker.models.containers")
        self.containers.ContainerCollection = ContainerCollection
        self.original = ContainerCollection.create
        self.addCleanup(setattr, ContainerCollection, "create", self.original)
        requests = types.ModuleType("requests")
        requests.exceptions = types.SimpleNamespace(Timeout=Timeout, ReadTimeout=ReadTimeout,
                                                    ConnectionError=ConnectionError_, ConnectTimeout=ConnectTimeout)
        modules = mock.patch.dict(sys.modules, {"docker": types.ModuleType("docker"),
                                                "docker.models": types.ModuleType("docker.models"),
                                                "docker.models.containers": self.containers,
                                                "requests": requests})
        modules.start()
        self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location("osworld2_startup_create", os.path.join(STARTUP, "sitecustomize.py"))
        self.site = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.site)

        def root_disk(root):
            self.site._DISK[root] = DEVICE
            return DEVICE

        disk = mock.patch.object(self.site, "root_disk", root_disk)
        disk.start()
        self.addCleanup(disk.stop)
        self.swept = []

        def sweeper(token):
            r, w = os.pipe()
            self.swept.append((token, r))
            self.addCleanup(os.close, r)
            return w

        sweeper = mock.patch.object(self.site, "_sweeper", sweeper)
        sweeper.start()
        self.addCleanup(sweeper.stop)

    def said(self):
        return [(token, os.read(r, 16)) for token, r in self.swept]

    def start(self, daemon, image=PROVIDER, timeout=60, environ=NONE, **kwargs):
        ContainerCollection.create = self.original
        self.site.wrap_docker_create(environ)
        self.collection = ContainerCollection(Client(daemon, timeout))

        def tick(seconds):
            daemon.clock += seconds

        with mock.patch.object(self.site.time, "sleep", tick), \
                mock.patch.object(self.site.time, "monotonic", lambda: daemon.clock):
            return self.collection.run(image, detach=True, **kwargs)

    def test_a_create_slower_than_docker_pys_60_s_goes_through(self):
        daemon = Daemon(seconds=90)
        with self.assertRaises(ReadTimeout):
            ContainerCollection(Client(daemon)).run(PROVIDER)
        made = self.start(Daemon(seconds=90))
        self.assertTrue(made.started)
        self.assertEqual(self.collection.client.api.timeout, self.site.CREATE_SECONDS)
        self.assertEqual(len(made.daemon.containers), 1)

    def test_a_create_the_client_timed_out_on_is_the_one_started(self):
        daemon = Daemon(seconds=150, late=8)
        with self.assertLogs("nullius.osworld2", "WARNING") as said:
            made = self.start(daemon)
        self.assertTrue(made.started)
        self.assertEqual((daemon.creates, len(daemon.containers)), (1, 1))
        self.assertIs(made, daemon.containers[0])
        self.assertIn("ReadTimeout", said.output[0])

    def test_a_request_answered_whose_inspect_timed_out_is_looked_for_and_started(self):
        daemon = Daemon(inspect=150)
        with self.assertLogs("nullius.osworld2", "WARNING"):
            made = self.start(daemon, environ=LIMITS)
        self.assertEqual((daemon.creates, len(daemon.containers)), (1, 1))
        self.assertIs(made, daemon.containers[0])
        self.assertEqual((made.started, made.removed), (True, None))
        self.assertEqual(daemon.list_timeouts, [self.site.LOOK_SECONDS])
        self.assertEqual(daemon.gave_up, 2 * self.site.CREATE_SECONDS)

    def test_each_look_waits_its_own_short_time_and_the_clients_comes_back(self):
        daemon = Daemon(seconds=150, late=8)
        with self.assertLogs("nullius.osworld2", "WARNING"):
            self.start(daemon)
        self.assertGreater(len(daemon.list_timeouts), 1)
        self.assertEqual(set(daemon.list_timeouts), {self.site.LOOK_SECONDS})
        self.assertEqual(self.collection.client.api.timeout, self.site.CREATE_SECONDS)

    def test_none_made_in_time_is_the_error_and_no_second_create(self):
        for late in (None, 61):
            with self.subTest(f"late {late}"):
                daemon = Daemon(seconds=150, late=late)
                with self.assertRaises(ReadTimeout):
                    self.start(daemon)
                self.assertEqual((daemon.creates, daemon.containers), (1, []))
                self.assertGreater(len(daemon.list_timeouts), 1)
                self.assertLessEqual(daemon.clock - daemon.gave_up, self.site.ADOPT_SECONDS + self.site.POLL_SECONDS)
                self.assertEqual(self.collection.client.api.timeout, self.site.CREATE_SECONDS)

    def test_a_failed_connection_stands_at_once(self):
        for error in (ConnectionError_, ConnectTimeout):
            with self.subTest(error.__name__):
                daemon = Daemon(seconds=150, late=0, error=error)
                with self.assertRaises(error):
                    self.start(daemon)
                self.assertEqual((daemon.creates, daemon.list_timeouts), (1, []))

    def test_each_create_looks_only_for_its_own_label(self):
        daemon = Daemon(seconds=150, late=None)
        daemon.containers.append(Container(daemon, PROVIDER, {"labels": {self.site.LABEL: "another"}}))
        with self.assertRaises(ReadTimeout):
            self.start(daemon)
        self.assertFalse(daemon.containers[0].started)

    def test_a_refusal_stands_at_once(self):
        daemon = Daemon(refuse=True)
        with self.assertRaises(APIError):
            self.start(daemon)
        self.assertEqual((daemon.creates, daemon.list_timeouts), (1, []))

    def test_a_late_container_keeps_its_write_limit_and_is_started(self):
        daemon = Daemon(seconds=150, late=8)
        with self.assertLogs("nullius.osworld2", "WARNING"):
            made = self.start(daemon, environ=LIMITS)
        self.assertIs(made, daemon.containers[0])
        self.assertEqual(made.attrs["HostConfig"]["BlkioDeviceWriteBps"], [{"Path": DEVICE, "Rate": 10 * 1024 * 1024}])
        self.assertEqual(made.attrs["HostConfig"]["BlkioDeviceWriteIOps"], [{"Path": DEVICE, "Rate": 100}])
        self.assertEqual((made.started, made.removed), (True, None))

    def test_a_late_container_without_its_write_limit_is_removed_and_fails_the_start(self):
        daemon = Daemon(seconds=150, late=8, drop=True)
        with self.assertLogs("nullius.osworld2", "WARNING"), self.assertRaises(self.site.WriteLimitError):
            self.start(daemon, environ=LIMITS)
        made = daemon.containers[0]
        self.assertEqual((made.started, made.removed), (False, {"force": True, "v": True}))

    def test_dockers_root_is_asked_with_the_patient_client(self):
        daemon = Daemon()
        self.start(daemon, environ=LIMITS)
        self.assertEqual(daemon.info_timeouts, [self.site.CREATE_SECONDS])

    def test_another_image_is_left_alone(self):
        daemon = Daemon()
        made = self.start(daemon, "ubuntu:24.04")
        self.assertEqual(made.kwargs, {"detach": True})
        self.assertEqual(self.collection.client.api.timeout, 60)

    def test_the_providers_ports_and_the_guests_a_unit_names_go_on_loopback_only(self):
        given = {8006: 8006, 5000: 5001, 9222: 9223, 8080: 8081}
        made = self.start(Daemon(), environ=dict(NONE, OSWORLD2_GUEST_PORTS="3000 3001"), ports=given)
        self.assertEqual(made.kwargs["ports"], {8006: ("127.0.0.1", 8006), 5000: ("127.0.0.1", 5001),
                                                9222: ("127.0.0.1", 9223), 8080: ("127.0.0.1", 8081),
                                                3000: ("127.0.0.1", 3000), 3001: ("127.0.0.1", 3001)})
        self.assertEqual(given, {8006: 8006, 5000: 5001, 9222: 9223, 8080: 8081})
        self.assertEqual(self.start(Daemon(), ports=given).kwargs["ports"],
                         {k: ("127.0.0.1", v) for k, v in given.items()})
        self.assertEqual(self.start(Daemon(), ports={5000: None, 8006: ("127.0.0.1", 8006)}).kwargs["ports"],
                         {5000: ("127.0.0.1", None), 8006: ("127.0.0.1", 8006)})
        self.assertEqual(self.start(Daemon(), "ubuntu:24.04", environ=dict(NONE, OSWORLD2_GUEST_PORTS="3000")).kwargs,
                         {"detach": True})

    def test_the_upstreams_retry_gets_a_second_vm_like_the_first(self):
        environ = dict(LIMITS, OSWORLD2_GUEST_PORTS="3000")
        daemon = Daemon()
        first = self.start(daemon, environ=environ, ports={5000: 5000})
        first.remove()
        daemon.seconds, daemon.late = 150, None
        with mock.patch.object(self.site.time, "sleep", lambda s: setattr(daemon, "clock", daemon.clock + s)), \
                mock.patch.object(self.site.time, "monotonic", lambda: daemon.clock), self.assertRaises(ReadTimeout):
            self.collection.run(PROVIDER, detach=True, ports={5000: 5001})
        self.assertTrue(first.removed.get("v"))
        self.assertEqual([said for _, said in self.said()], [b"kept", b""])
        daemon.seconds = 0
        second = self.collection.run(PROVIDER, detach=True, ports={5000: 5001})
        self.assertEqual((daemon.creates, len(daemon.containers)), (3, 2))
        self.assertIsNot(second, first)
        self.assertNotEqual(first.labels[self.site.LABEL], second.labels[self.site.LABEL])
        for made in (first, second):
            self.assertEqual(made.kwargs["network"], self.site.NETWORK)
            self.assertEqual(made.kwargs["ports"][3000], ("127.0.0.1", 3000))
            self.assertEqual(made.attrs["HostConfig"]["BlkioDeviceWriteBps"],
                             [{"Path": DEVICE, "Rate": 10 * 1024 * 1024}])
            self.assertEqual(made.attrs["HostConfig"]["BlkioDeviceWriteIOps"], [{"Path": DEVICE, "Rate": 100}])
        self.assertEqual((second.started, second.removed), (True, None))

    def test_the_upstreams_removal_takes_the_vms_volume(self):
        made = self.start(Daemon())
        made.remove()
        self.assertEqual(made.removed, {"v": True})
        made.remove(force=True)
        self.assertEqual(made.removed, {"force": True, "v": True})
        other = self.start(Daemon(), "ubuntu:24.04")
        other.remove()
        self.assertEqual(other.removed, {})

    def test_only_a_create_never_kept_is_swept_for_later(self):
        daemon = Daemon(seconds=150, late=self.site.ADOPT_SECONDS + 1)
        with self.assertRaises(ReadTimeout):
            self.start(daemon)
        self.assertEqual(self.said(), [(daemon.pending.labels[self.site.LABEL], b"")])
        self.swept.clear()
        with self.assertLogs("nullius.osworld2", "WARNING"):
            self.start(Daemon(seconds=150, late=8))
        self.start(Daemon())
        for daemon in (Daemon(refuse=True), Daemon(seconds=150, error=ConnectionError_)):
            with self.assertRaises(Exception):
                self.start(daemon)
        self.assertEqual([said for _, said in self.said()], [b"kept"] * 4)

    def test_a_create_cut_short_leaves_its_sweeper_to_the_process_end(self):
        with self.assertRaises(KeyboardInterrupt):
            self.start(Daemon(seconds=150, error=KeyboardInterrupt))
        (_, r), = self.swept
        os.set_blocking(r, False)
        with self.assertRaises(BlockingIOError):
            os.read(r, 16)

    def test_a_longer_timeout_and_the_callers_labels_are_kept(self):
        made = self.start(Daemon(), timeout=300, labels={"a": "1"})
        self.assertEqual(self.collection.client.api.timeout, 300)
        self.assertEqual(made.labels["a"], "1")
        self.assertRegex(made.labels[self.site.LABEL], r"^[0-9a-f]{32}$")
        self.assertEqual(sorted(self.start(Daemon(), labels=["b"]).labels), ["b", self.site.LABEL])

    def test_the_vm_goes_on_its_own_network_unless_the_create_names_one(self):
        self.assertEqual(self.start(Daemon()).kwargs["network"], self.site.NETWORK)
        self.assertEqual(self.start(Daemon(), network="another").kwargs["network"], "another")
        self.assertNotIn("network", self.start(Daemon(), network_mode="host").kwargs)
        daemon = Daemon(seconds=150, late=8)
        with self.assertLogs("nullius.osworld2", "WARNING"):
            self.assertEqual(self.start(daemon).kwargs["network"], self.site.NETWORK)

    def test_the_start_queue_waits_out_the_longest_first_start(self):
        site = self.site
        port_lists = site.PORT_LISTS * site.DOCKER_PY_SECONDS
        root = request = inspect = reload = start = site.CREATE_SECONDS
        late_search = site.ADOPT_SECONDS + site.POLL_SECONDS + 2 * site.LOOK_SECONDS
        request_answered_inspect_timed_out = request + inspect + late_search
        self.assertEqual(site.LONGEST_START, port_lists + root + request_answered_inspect_timed_out + reload + start)
        spec = importlib.util.spec_from_file_location("osworld2_driver_create", os.path.join(HERE, "driver.py"))
        driver = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "argv", ["driver.py"]), mock.patch.object(sys, "path", [HERE, *sys.path]), \
                mock.patch.dict(os.environ, NULLIUS_UPSTREAM=STARTUP):
            spec.loader.exec_module(driver)
        self.assertGreaterEqual(driver.START_SECONDS, site.LONGEST_START)


SWEPT_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  ps) [ -e "$FAKE_MADE" ] && echo cid9 ;;
  rm) [ -e "$FAKE_RM_FAILS" ] && exit 1 ;;
esac
exit 0
"""


class Sweep(unittest.TestCase):
    """sweep(), the abandoned create's later removal, with a fake docker command line."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(SWEPT_DOCKER)
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.log, self.made = os.path.join(self.tmp, "calls"), os.path.join(self.tmp, "made")
        env = mock.patch.dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"], FAKE_DOCKER_LOG=self.log,
                              FAKE_MADE=self.made, FAKE_RM_FAILS=os.path.join(self.tmp, "rm_fails"))
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("osworld2_startup_sweep", os.path.join(STARTUP, "sitecustomize.py"))
        self.site = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.site)

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def sweep(self, on_sleep=lambda n: None):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
            on_sleep(len(self.calls()))

        with mock.patch.object(self.site, "time", types.SimpleNamespace(sleep=sleep, monotonic=lambda: clock[0])):
            self.site.sweep("t" * 32)
        return clock[0]

    def test_a_container_made_later_is_removed_with_its_volume(self):
        self.sweep(lambda n: n >= 3 and open(self.made, "w").close())
        look = f"ps -a -q --filter label={self.site.LABEL}={'t' * 32}"
        self.assertEqual(self.calls(), [look] * 4 + ["rm -f -v cid9"])

    def test_a_refused_removal_is_tried_again(self):
        open(self.made, "w").close()
        rm_fails = os.environ["FAKE_RM_FAILS"]
        open(rm_fails, "w").close()
        self.sweep(lambda n: os.path.exists(rm_fails) and os.remove(rm_fails))
        self.assertEqual([c for c in self.calls() if c.startswith("rm ")], ["rm -f -v cid9"] * 2)
        self.assertEqual(self.calls()[-1], "rm -f -v cid9")

    def test_none_made_ends_the_sweep_in_its_time(self):
        took = self.sweep()
        self.assertGreaterEqual(took, self.site.SWEEP_SECONDS)
        self.assertEqual(len(self.calls()), self.site.SWEEP_SECONDS // self.site.LOOK_SECONDS)
        self.assertTrue(all(c.startswith("ps ") for c in self.calls()))

    def test_the_sweeper_runs_apart_outlives_a_sigterm_and_sweeps_once_its_caller_is_gone(self):
        for kept in (True, False):
            with self.subTest(kept=kept):
                self.swept_by_sweeper(kept)

    def swept_by_sweeper(self, kept):
        token = ("k" if kept else "s") * 32
        for path in (self.made, self.log):
            if os.path.exists(path):
                os.remove(path)
        w = self.site._sweeper(token)

        def sweepers():
            found = []
            for pid in filter(str.isdigit, os.listdir("/proc")):
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        if token.encode() in f.read():
                            found.append(int(pid))
                except OSError:
                    pass
            return found

        self.addCleanup(lambda: [os.kill(p, signal.SIGKILL) for p in sweepers()])
        found = sweepers()
        self.assertEqual(len(found), 1)
        self.assertEqual(os.getsid(found[0]), found[0])
        t0 = time.time()
        while not self.ignores_sigterm(found[0]) and time.time() - t0 < 20:
            time.sleep(0.05)
        os.kill(found[0], signal.SIGTERM)
        open(self.made, "w").close()
        if kept:
            self.site._kept(w)
        else:
            os.close(w)
        while sweepers() and time.time() - t0 < 30:
            time.sleep(0.1)
        self.assertEqual(sweepers(), [])
        self.assertEqual(self.calls()[-1:], [] if kept else ["rm -f -v cid9"])

    @staticmethod
    def ignores_sigterm(pid):
        with open(f"/proc/{pid}/status") as f:
            ignored = next(ln.split()[1] for ln in f if ln.startswith("SigIgn:"))
        return bool(int(ignored, 16) & 1 << (signal.SIGTERM - 1))


PROVIDER_PY = "LOCK_TIMEOUT = 10\n"


class StartQueue(unittest.TestCase):
    """The provider's port lock, with a fake desktop_env and the startup folder first on PYTHONPATH."""

    def test_every_vm_start_waits_for_the_providers_port_lock(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        package = os.path.join(tmp, "desktop_env", "providers", "docker")
        os.makedirs(package)
        for folder in ("desktop_env", os.path.join("desktop_env", "providers"), package):
            open(os.path.join(tmp, folder, "__init__.py"), "w").close()
        with open(os.path.join(package, "provider.py"), "w") as f:
            f.write(PROVIDER_PY)
        environ = {k: v for k, v in os.environ.items() if not k.startswith("NULLIUS_OPTION_")}
        for option, wanted in (("docker", "-1"), ("aws", "10")):
            with self.subTest(option=option):
                done = subprocess.run([sys.executable, "-c", "import desktop_env.providers.docker.provider as m; "
                                       "print(m.LOCK_TIMEOUT)"], cwd=tmp, capture_output=True, text=True, timeout=60,
                                      env=dict(environ, PYTHONPATH=os.pathsep.join([STARTUP, tmp]),
                                               NULLIUS_OPTION_VM_PROVIDER=option))
                self.assertEqual(done.returncode, 0, done.stderr[-2000:])
                self.assertEqual(done.stdout.strip(), wanted)


class Said:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Network(unittest.TestCase):
    """network(), with a fake docker command line."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("osworld2_startup_network", os.path.join(STARTUP, "sitecustomize.py"))
        self.site = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.site)
        self.networks, self.calls, self.refuse = {}, [], None
        self.shape = f"1|{self.site.SUBNET} {self.site.GATEWAY} "

    def docker(self, *args):
        self.calls.append(args)
        name = args[-1]
        if args[:2] == ("network", "inspect"):
            return Said(0, self.networks[name] + "\n") if name in self.networks else Said(1, "", f"Error: no such network: {name}")
        if args[:2] == ("network", "create"):
            if self.refuse:
                return Said(1, "", self.refuse)
            if name in self.networks:
                return Said(1, "", f"Error response from daemon: network with name {name} already exists")
            subnet, gateway = args[args.index("--subnet") + 1], args[args.index("--gateway") + 1]
            label = "1" if f"{self.site.NETWORK_LABEL}=1" in args else ""
            self.networks[name] = f"{label}|{subnet} {gateway} "
            return Said(0, "id")
        raise AssertionError(args)

    def test_a_missing_network_is_made_with_its_subnet_gateway_and_label(self):
        self.assertEqual(self.site.network(self.docker), self.site.NETWORK)
        create, = [c for c in self.calls if c[1] == "create"]
        for flag in (("--driver", "bridge"), ("--subnet", self.site.SUBNET), ("--gateway", self.site.GATEWAY),
                     ("--label", f"{self.site.NETWORK_LABEL}=1")):
            self.assertIn(" ".join(flag), " ".join(create))
        self.calls.clear()
        self.assertEqual(self.site.network(self.docker), self.site.NETWORK)
        self.assertEqual([c[1] for c in self.calls], ["inspect"])

    def test_another_maker_at_the_same_moment_is_no_error(self):
        first = self.docker

        def racing(*args):
            if args[:2] == ("network", "create"):
                first("network", "create", "--subnet", self.site.SUBNET, "--gateway", self.site.GATEWAY,
                      "--label", f"{self.site.NETWORK_LABEL}=1", args[-1])
            return first(*args)

        self.assertEqual(self.site.network(racing), self.site.NETWORK)
        self.assertEqual(self.networks, {self.site.NETWORK: self.shape})

    def test_a_network_of_that_name_on_another_subnet_is_refused(self):
        self.networks[self.site.NETWORK] = "1|172.17.0.0/16 172.17.0.1 "
        with self.assertRaises(self.site.NetworkError) as raised:
            self.site.network(self.docker)
        self.assertIn(f"docker network rm {self.site.NETWORK}", str(raised.exception))
        self.assertFalse([c for c in self.calls if c[1] == "create"])

    def test_another_owner_s_network_of_that_name_is_never_offered_for_removal(self):
        self.networks[self.site.NETWORK] = "|172.17.0.0/16 172.17.0.1 "
        with self.assertRaises(self.site.NetworkError) as raised:
            self.site.network(self.docker)
        self.assertNotIn("docker network rm", str(raised.exception))
        self.assertIn("Nullius did not make it", str(raised.exception))
        self.assertIn(f"{self.site.NETWORK} and {self.site.SUBNET} are free", str(raised.exception))

    def test_a_network_made_by_hand_with_that_subnet_and_gateway_is_used(self):
        self.networks[self.site.NETWORK] = f"|{self.site.SUBNET} {self.site.GATEWAY} "
        self.assertEqual(self.site.network(self.docker), self.site.NETWORK)
        self.assertFalse([c for c in self.calls if c[1] == "create"])

    def test_a_network_that_cannot_be_made_says_why(self):
        self.refuse = "Error response from daemon: Pool overlaps with other one on this address space"
        with self.assertRaises(self.site.NetworkError) as raised:
            self.site.network(self.docker)
        self.assertIn("Pool overlaps", str(raised.exception))
        self.assertIn(self.site.SUBNET, str(raised.exception))

    def test_the_label_is_read_with_the_subnet(self):
        self.site.network(self.docker)
        inspect = self.calls[0]
        self.assertIn('{{index .Labels "' + self.site.NETWORK_LABEL + '"}}|', inspect[inspect.index("-f") + 1])

    def test_the_subnet_is_none_docker_takes_for_its_own_bridges(self):
        subnet = ipaddress.ip_network(self.site.SUBNET)
        for taken in ("172.16.0.0/12", "192.168.0.0/16"):
            self.assertFalse(subnet.overlaps(ipaddress.ip_network(taken)), taken)
        self.assertIn(ipaddress.ip_address(self.site.GATEWAY), subnet)
        self.assertTrue(subnet.is_private)


if __name__ == "__main__":
    unittest.main()
