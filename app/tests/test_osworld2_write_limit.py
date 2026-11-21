"""OSWorld2's disk write limit: startup/sitecustomize.py on docker-py's create, and the driver's proof before a VM.
A fake docker-py, docker on PATH and sysfs stand in."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from nullius.standard.contract import definitions
from nullius.standard.runner import run

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(APP, "nullius", "catalogue", "benchmarks", "osworld2")
STARTUP = os.path.join(HERE, "startup")
PROVIDER = "happysixd/osworld-docker@sha256:" + "0" * 64
OFF = {"NULLIUS_OPTION_WRITE_MBPS": "none", "NULLIUS_OPTION_WRITE_IOPS": "none", "NULLIUS_OPTION_VM_PROVIDER": "docker"}
ON = {"NULLIUS_OPTION_WRITE_MBPS": "30", "NULLIUS_OPTION_WRITE_IOPS": "300"}
MB30 = 30 * 1024 * 1024
CONTAINERS = """class Container:
    drop = False

    def __init__(self, image, kwargs):
        self.image, self.kwargs, self.attrs, self.removed, self.started = image, kwargs, {}, None, False

    def start(self):
        self.started = True

    def reload(self):
        kept = {} if Container.drop else self.kwargs
        self.attrs = {"HostConfig": {"BlkioDeviceWriteBps": kept.get("device_write_bps"),
                                     "BlkioDeviceWriteIOps": kept.get("device_write_iops")}}

    def remove(self, **kwargs):
        self.removed = kwargs


class ContainerCollection:
    made = []

    def __init__(self, client=None):
        self.client = client

    def create(self, image, command=None, **kwargs):
        ContainerCollection.made.append(Container(image, kwargs))
        return ContainerCollection.made[-1]

    def run(self, image, command=None, **kwargs):
        container = self.create(image=image, command=command, **kwargs)
        container.start()
        return container
"""
CHILD = """import json, multiprocessing, sys
import sibling
from docker.models.containers import ContainerCollection


def look(queue):
    run = ContainerCollection.run
    queue.put({"wrapped": hasattr(ContainerCollection.create, "__wrapped__"),
               "other": run(ContainerCollection(), "ubuntu:24.04", x=1).kwargs})


if __name__ == "__main__":
    out = {"main": hasattr(ContainerCollection.create, "__wrapped__"), "path0": sys.path[0]}
    for method in ("fork", "spawn"):
        ctx = multiprocessing.get_context(method)
        queue = ctx.Queue()
        p = ctx.Process(target=look, args=(queue,))
        p.start()
        out[method] = queue.get(timeout=60)
        p.join()
    print(json.dumps(out))
"""
DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) printf '%s\t%s\n' "$FAKE_ROOT" "${FAKE_CGROUP:-2}" ;;
  ps) case "$*" in *name=*) printf '%s\n' $FAKE_PROBES ;; esac ;;
  create) case "$FAKE_CREATE_HANG" in
      all) exec sleep 5 ;;
      once) [ -e "$FAKE_DOCKER_LOG.hung" ] || { : > "$FAKE_DOCKER_LOG.hung"; exec sleep 5; } ;;
    esac
    [ -z "$FAKE_CREATE_FAIL" ] || { echo "Error: refused" >&2; exit 1; }
    echo "WARNING: write limit discarded" >&2; echo probe ;;
  inspect) echo "$FAKE_HOST" ;;
  network) echo "1|10.213.0.0/24 10.213.0.1" ;;
  rm) [ -z "$FAKE_RM_FAIL" ] || { echo "Error: busy" >&2; exit 1; } ;;
esac
"""


def load_site(environ, name="osworld2_startup"):
    spec = importlib.util.spec_from_file_location(name, os.path.join(STARTUP, "sitecustomize.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, environ):
        spec.loader.exec_module(module)
    return module


def load_driver(up, argv=()):
    spec = importlib.util.spec_from_file_location("osworld2_driver_limit", os.path.join(HERE, "driver.py"))
    driver = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", ["driver.py", *argv]), mock.patch.object(sys, "path", [HERE, *sys.path]), \
            mock.patch.dict(os.environ, NULLIUS_UPSTREAM=up):
        spec.loader.exec_module(driver)
    return driver


def sysfs(case, site, root, kind="disk"):
    """A fake sysfs and /dev for ``root``'s device: a disk, a partition, a device mapper volume, or no uevent."""
    st = os.stat(root).st_dev
    tmp = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    block, dev = os.path.join(tmp, "sys", "dev", "block"), os.path.join(tmp, "dev")
    name = "dm-0" if kind == "dm" else "sda"
    disk = os.path.join(tmp, "sys", "devices", "virtual" if kind == "dm" else "pci", name)
    node = os.path.join(disk, "sda1") if kind == "partition" else disk
    for d in (node, block, dev):
        os.makedirs(d, exist_ok=True)
    if kind != "no uevent":
        with open(os.path.join(disk, "uevent"), "w") as f:
            f.write(f"MAJOR=8\nMINOR=0\nDEVNAME={name}\nDEVTYPE=disk\n")
    open(os.path.join(dev, name), "w").close()
    if kind == "partition":
        with open(os.path.join(node, "uevent"), "w") as f:
            f.write("MAJOR=8\nMINOR=1\nDEVNAME=sda1\nDEVTYPE=partition\n")
        with open(os.path.join(node, "partition"), "w") as f:
            f.write("1\n")
        open(os.path.join(dev, "sda1"), "w").close()
    os.symlink(node, os.path.join(block, f"{os.major(st)}:{os.minor(st)}"))
    for attr, value in (("SYS_BLOCK", block), ("DEV", dev)):
        patched = mock.patch.object(site, attr, value)
        patched.start()
        case.addCleanup(patched.stop)
    return os.path.join(dev, name)


class Client:
    def __init__(self, root):
        self.root, self.asked = root, 0

    def info(self):
        self.asked += 1
        return {"DockerRootDir": self.root}


class Intercept(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "docker-root")
        os.makedirs(self.root)
        self.containers = types.ModuleType("docker.models.containers")
        exec(CONTAINERS, self.containers.__dict__)
        self.original = self.containers.ContainerCollection.create
        modules = mock.patch.dict(sys.modules, {"docker": types.ModuleType("docker"),
                                                "docker.models": types.ModuleType("docker.models"),
                                                "docker.models.containers": self.containers})
        modules.start()
        self.addCleanup(modules.stop)
        self.site = load_site(dict(OFF, **ON))
        self.client = Client(self.root)

    def start(self, image, environ=ON, **kwargs):
        self.site.wrap_docker_create(dict(OFF, **environ))
        return self.containers.ContainerCollection(self.client).run(image, **kwargs)

    def test_the_providers_container_is_limited_on_dockers_disk(self):
        device = sysfs(self, self.site, self.root)
        made = self.start(PROVIDER, detach=True, device_write_bps=[{"Path": "/dev/x", "Rate": 1}])
        self.assertEqual(made.image, PROVIDER)
        self.assertTrue(made.kwargs["detach"])
        self.assertEqual(made.kwargs["device_write_bps"], [{"Path": "/dev/x", "Rate": 1}, {"Path": device, "Rate": MB30}])
        self.assertEqual(made.kwargs["device_write_iops"], [{"Path": device, "Rate": 300}])
        self.assertIsNone(made.removed)
        self.assertTrue(made.started)

    def test_a_partition_is_limited_on_its_disk(self):
        device = sysfs(self, self.site, self.root, "partition")
        self.assertEqual(self.start(PROVIDER).kwargs["device_write_bps"][0]["Path"], device)

    def test_a_device_mapper_volume_is_limited_as_it_is(self):
        device = sysfs(self, self.site, self.root, "dm")
        self.assertEqual(device.rsplit(os.sep, 1)[1], "dm-0")
        self.assertEqual(self.start(PROVIDER).kwargs["device_write_iops"], [{"Path": device, "Rate": 300}])

    def test_one_limit_alone(self):
        device = sysfs(self, self.site, self.root)
        made = self.start(PROVIDER, {"NULLIUS_OPTION_WRITE_IOPS": "150"})
        made.kwargs.pop("labels")
        self.assertEqual(made.kwargs, {"device_write_iops": [{"Path": device, "Rate": 150}],
                                       "network": self.site.NETWORK})

    def test_the_disk_is_found_once_a_process(self):
        sysfs(self, self.site, self.root)
        self.start(PROVIDER)
        self.containers.ContainerCollection(self.client).run(PROVIDER)
        self.assertEqual(self.client.asked, 1)

    def test_another_image_is_left_alone(self):
        sysfs(self, self.site, self.root)
        made = self.start("ubuntu:24.04", detach=True)
        self.assertEqual((made.image, made.kwargs, self.client.asked), ("ubuntu:24.04", {"detach": True}, 0))

    def test_a_container_kept_without_the_limit_is_removed_and_fails_the_start(self):
        sysfs(self, self.site, self.root)
        self.containers.Container.drop = True
        with self.assertRaises(self.site.WriteLimitError) as raised:
            self.start(PROVIDER)
        made = self.containers.ContainerCollection.made[-1]
        self.assertEqual((made.removed, made.started), ({"force": True, "v": True}, False))
        self.assertIn("Disk write MB/s and Disk write IOPS to none", str(raised.exception))

    def test_no_device_fails_the_start(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with mock.patch.object(self.site, "SYS_BLOCK", empty):
            with self.assertRaises(self.site.WriteLimitError) as raised:
                self.start(PROVIDER)
        self.assertIn(self.root, str(raised.exception))
        self.assertEqual(self.containers.ContainerCollection.made, [])

    def test_no_uevent_fails_the_start(self):
        sysfs(self, self.site, self.root, "no uevent")
        with self.assertRaises(self.site.WriteLimitError):
            self.start(PROVIDER)
        self.assertEqual(self.containers.ContainerCollection.made, [])

    def test_no_docker_answer_fails_the_start(self):
        sysfs(self, self.site, self.root)
        self.client.info = lambda: {}
        with self.assertRaises(self.site.WriteLimitError):
            self.start(PROVIDER)

    def test_only_a_positive_whole_number_is_a_limit(self):
        for value in ("30mb", "0", "007", "\u0663", "\u00b2", "-1", " 30"):
            with self.subTest(value), self.assertRaises(self.site.WriteLimitError) as raised:
                self.site.wanted({"NULLIUS_OPTION_WRITE_MBPS": value})
            self.assertIn("Disk write MB/s is", str(raised.exception))
            self.assertIn("Disk write IOPS to none", str(raised.exception))
        self.assertEqual(self.site.wanted({"NULLIUS_OPTION_WRITE_MBPS": "30"}),
                         [("device_write_bps", MB30, "BlkioDeviceWriteBps", "--device-write-bps")])

    def test_the_driver_and_the_wrapper_name_one_image(self):
        driver = load_driver(self.tmp)
        self.assertEqual(self.site.PROVIDER, driver.PROVIDER_IMAGE.split("@")[0])

    def test_no_limit_adds_none_and_asks_docker_nothing(self):
        made = self.start(PROVIDER, {}, detach=True)
        self.assertEqual(sorted(made.kwargs), ["detach", "labels", "network"])
        self.assertEqual(self.client.asked, 0)

    def test_no_docker_py_changes_nothing(self):
        with mock.patch.dict(sys.modules, {"docker": None, "docker.models": None, "docker.models.containers": None}):
            self.site.wrap_docker_create(dict(OFF, **ON))
        self.assertIs(self.containers.ContainerCollection.create, self.original)

    def test_only_python_startup_wraps_and_only_for_docker(self):
        self.assertIs(self.containers.ContainerCollection.create, self.original)
        load_site(dict(ON, NULLIUS_OPTION_VM_PROVIDER="aws"), "sitecustomize")
        self.assertIs(self.containers.ContainerCollection.create, self.original)
        load_site(dict(ON, NULLIUS_OPTION_VM_PROVIDER="docker"), "sitecustomize")
        self.assertTrue(hasattr(self.containers.ContainerCollection.create, "__wrapped__"))


class Processes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        package = os.path.join(self.tmp, "fake", "docker", "models")
        os.makedirs(package)
        for folder in (package, os.path.dirname(package)):
            open(os.path.join(folder, "__init__.py"), "w").close()
        with open(os.path.join(package, "containers.py"), "w") as f:
            f.write(CONTAINERS)
        self.scripts = os.path.join(self.tmp, "scripts")
        os.makedirs(self.scripts)
        open(os.path.join(self.scripts, "sibling.py"), "w").close()
        with open(os.path.join(self.scripts, "runner.py"), "w") as f:
            f.write(CHILD)

    def child(self, **environ):
        env = load_driver(self.tmp)._env("001", **dict(OFF, **environ))
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep), [STARTUP, self.tmp])
        env["PYTHONPATH"] += os.pathsep + os.path.join(self.tmp, "fake")
        ran = subprocess.run([sys.executable, os.path.join(self.scripts, "runner.py")], cwd=self.tmp, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stderr, "")
        return json.loads(ran.stdout)

    def test_the_limit_holds_in_the_runner_and_its_workers(self):
        out = self.child(**ON)
        self.assertEqual(out["path0"], self.scripts)
        self.assertTrue(out["main"])
        for method in ("fork", "spawn"):
            self.assertEqual(out[method], {"wrapped": True, "other": {"x": 1}})

    def test_with_no_limit_the_create_is_still_wrapped(self):
        out = self.child()
        self.assertTrue(out["main"])
        for method in ("fork", "spawn"):
            self.assertEqual(out[method], {"wrapped": True, "other": {"x": 1}})

    def test_another_vm_provider_leaves_every_process_alone(self):
        out = self.child(NULLIUS_OPTION_VM_PROVIDER="aws", **ON)
        self.assertFalse(out["main"])
        for method in ("fork", "spawn"):
            self.assertFalse(out[method]["wrapped"])

    def test_the_file_comes_from_the_drivers_command(self):
        elsewhere = os.path.join(self.tmp, "snapshot", "startup", "sitecustomize.py")
        driver = load_driver(self.tmp, [os.path.join(HERE, "agents.json"), os.path.join(HERE, "nop.py"), elsewhere])
        self.assertEqual(driver._env("001")["PYTHONPATH"].split(os.pathsep)[0], os.path.dirname(elsewhere))


class Before(unittest.TestCase):
    """The driver's proof, before a VM start, that the limit holds."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(DOCKER)
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.root = os.path.join(self.tmp, "docker-root")
        os.makedirs(self.root)
        self.calls = os.path.join(self.tmp, "calls")
        self.driver = load_driver(self.tmp)
        self.device = sysfs(self, self.driver._startup(), self.root)
        self.kept = json.dumps({"BlkioDeviceWriteBps": [{"Path": self.device, "Rate": MB30}],
                                "BlkioDeviceWriteIOps": [{"Path": self.device, "Rate": 300}]})
        find = importlib.util.find_spec
        spec = mock.patch.object(importlib.util, "find_spec", lambda n, *a: object() if n == "docker" else find(n, *a))
        spec.start()
        self.addCleanup(spec.stop)
        self.env = dict(OFF, **ON, PATH=bin_dir + os.pathsep + os.environ["PATH"], FAKE_DOCKER_LOG=self.calls,
                        FAKE_ROOT=self.root, FAKE_HOST=self.kept)

    def check(self, **environ):
        with mock.patch.dict(os.environ, dict(self.env, **environ)):
            self.driver._write_limit()

    def docker_calls(self):
        try:
            with open(self.calls) as f:
                return [ln.split()[0] for ln in f]
        except OSError:
            return []

    def test_a_limit_docker_keeps_passes_and_the_probe_is_removed(self):
        self.check()
        self.assertEqual(self.docker_calls(), ["info", "ps", "create", "inspect", "rm"])
        with open(self.calls) as f:
            create = f.read().splitlines()[2].split()
        self.assertTrue(create[create.index("--name") + 1].startswith(f"nullius-osworld2-probe-{os.getuid()}-{os.getpid()}-"))
        self.assertEqual(create[create.index("--device-write-bps") + 1], f"{self.device}:{MB30}")
        self.assertEqual(create[create.index("--device-write-iops") + 1], f"{self.device}:300")
        self.assertNotIn("start", self.docker_calls())

    def test_a_limit_docker_drops_fails_with_what_to_do(self):
        with self.assertRaises(RuntimeError) as raised:
            self.check(FAKE_HOST=json.dumps({"BlkioDeviceWriteBps": None, "BlkioDeviceWriteIOps": []}))
        said = str(raised.exception)
        self.assertIn("BlkioDeviceWriteBps, BlkioDeviceWriteIOps", said)
        self.assertIn("write limit discarded", said)
        self.assertIn("set Disk write MB/s and Disk write IOPS to none", said)
        self.assertEqual(self.docker_calls()[-1], "rm")

    def test_a_failed_removal_does_not_hide_why_the_probe_failed(self):
        real = self.driver._docker

        def docker(*args, **kw):
            if args[0] == "rm":
                raise RuntimeError("docker rm got no answer")
            return real(*args, **kw)

        with mock.patch.object(self.driver, "_docker", docker), self.assertRaises(RuntimeError) as raised:
            self.check(FAKE_CREATE_FAIL="1")
        self.assertIn("a probe container was refused", str(raised.exception))

    def test_a_refused_probe_fails(self):
        with self.assertRaises(RuntimeError) as raised:
            self.check(FAKE_CREATE_FAIL="1")
        self.assertIn("refused", str(raised.exception))
        self.assertEqual(self.docker_calls()[-1], "rm")

    def test_no_disk_fails_before_any_container(self):
        with mock.patch.object(self.driver._startup(), "SYS_BLOCK", os.path.join(self.tmp, "none")):
            with self.assertRaises(RuntimeError):
                self.check()
        self.assertEqual(self.docker_calls(), ["info"])

    def test_no_docker_py_fails(self):
        with mock.patch.object(importlib.util, "find_spec", lambda n, *a: None):
            with self.assertRaises(RuntimeError) as raised:
                self.check()
        self.assertIn("no docker-py", str(raised.exception))
        self.assertIn("install the driver's environment again", str(raised.exception))

    def test_no_docker_root_asks_after_the_daemon(self):
        with self.assertRaises(RuntimeError) as raised:
            self.check(FAKE_ROOT="")
        self.assertIn("is Docker's daemon running", str(raised.exception))
        self.assertEqual(self.docker_calls(), ["info"])

    def test_cgroup_v1_fails_before_any_container(self):
        with self.assertRaises(RuntimeError) as raised:
            self.check(FAKE_CGROUP="1")
        self.assertIn("cgroup v1", str(raised.exception))
        self.assertEqual(self.docker_calls(), ["info"])

    def names(self, verb):
        with open(self.calls) as f:
            lines = [ln.split() for ln in f if ln.startswith(verb + " ")]
        return [ln[ln.index("--name") + 1] if verb == "create" else ln[-1] for ln in lines]

    def test_every_check_waits_on_docker_twice_the_cleanups_time(self):
        waited, real = [], subprocess.run

        def ran(*a, **kw):
            waited.append((a[0][1], kw.get("timeout")))
            return real(*a, **kw)

        with mock.patch.object(self.driver.subprocess, "run", ran), \
                mock.patch.dict(os.environ, dict(self.env, FAKE_PROBES="")):
            self.check()
            self.driver._provider_image()
            self.driver._old_probes()
        self.assertEqual(self.driver.READY_SECONDS, 120)
        self.assertEqual(self.driver.DOCKER_SECONDS, 60)
        self.assertEqual(waited, [(c, 120) for c in ("info", "ps", "create", "inspect", "rm", "image")] + [("ps", 60)])

    def test_a_create_with_no_answer_is_tried_again_under_a_new_name(self):
        with mock.patch.object(self.driver, "READY_SECONDS", 1):
            self.check(FAKE_CREATE_HANG="once")
        self.assertEqual(self.docker_calls(), ["info", "ps", "create", "rm", "create", "inspect", "rm"])
        first, second = self.names("create")
        self.assertNotEqual(first, second)
        self.assertEqual(self.names("rm"), [first, second])

    def test_two_creates_with_no_answer_fail_in_one_line_and_leave_no_probe(self):
        with mock.patch.object(self.driver, "READY_SECONDS", 1), self.assertRaises(RuntimeError) as raised:
            self.check(FAKE_CREATE_HANG="all")
        self.assertEqual(str(raised.exception),
                         "Docker's daemon did not answer docker create in 1 s: it may be busy; "
                         "try again or run fewer units at once")
        self.assertEqual(self.docker_calls(), ["info", "ps", "create", "rm", "create", "rm"])
        self.assertEqual(self.names("rm"), self.names("create"))

    def test_no_second_create_while_the_first_may_stand(self):
        with mock.patch.object(self.driver, "READY_SECONDS", 1), self.assertRaises(RuntimeError) as raised:
            self.check(FAKE_CREATE_HANG="all", FAKE_RM_FAIL="1")
        self.assertIn("did not answer docker create in 1 s", str(raised.exception))
        self.assertEqual(self.docker_calls(), ["info", "ps", "create", "rm", "rm"])
        self.assertEqual(set(self.names("rm")), set(self.names("create")))

    def test_only_this_users_gone_or_old_probes_are_removed(self):
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        now, me = __import__("time").time_ns(), f"nullius-osworld2-probe-{os.getuid()}"
        probes = {"dead": f"{me}-{gone.pid}-{now}", "old": f"{me}-{os.getpid()}-{now - 601 * 10 ** 9}",
                  "live": f"{me}-{os.getpid()}-{now}", "other": f"nullius-osworld2-probe-{os.getuid() + 1}-{gone.pid}-{now}",
                  "odd": f"{me}-x"}
        with mock.patch.dict(os.environ, dict(self.env, FAKE_PROBES=" ".join(probes.values()))):
            self.driver._old_probes()
        with open(self.calls) as f:
            removed = [ln.split()[2:] for ln in f if ln.startswith("rm")]
        self.assertEqual(removed, [[probes["dead"], probes["old"]]])

    def test_no_startup_file_fails(self):
        with mock.patch.object(self.driver, "SITE", os.path.join(self.tmp, "gone", "sitecustomize.py")):
            with self.assertRaises(RuntimeError) as raised:
                self.check()
        self.assertIn("Disk write IOPS to none", str(raised.exception))

    def test_no_limit_or_another_provider_asks_nothing(self):
        self.check(**OFF)
        self.check(NULLIUS_OPTION_VM_PROVIDER="aws")
        self.assertEqual(self.docker_calls(), [])

    def test_the_reaper_removes_a_gone_drivers_probe(self):
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        probe = f"nullius-osworld2-probe-{os.getuid()}-{gone.pid}-{__import__('time').time_ns()}"
        with mock.patch.object(self.driver, "SWEEPS", (0,)), \
                mock.patch.dict(os.environ, dict(self.env, FAKE_PROBES=probe)):
            self.driver.reap(gone.pid, self.tmp, True)
        with open(self.calls) as f:
            self.assertIn(f"rm -f {probe}", f.read().splitlines())

    def test_a_vm_start_is_refused_before_the_runner_and_the_null_control(self):
        starts = {"original": lambda: self.driver.answer("tasks__001", self.tmp, "original", "MiniMax-M3",
                                                         "http://relay/v1", "k"),
                  "nop": lambda: self.driver.score("tasks__001", self.tmp)}
        for name, start in starts.items():
            with self.subTest(name), mock.patch.object(self.driver, "vm_image", lambda: "image"), \
                    mock.patch.object(self.driver, "_task_file", lambda *a: None), \
                    mock.patch.object(self.driver, "ASSETS", self.tmp), \
                    mock.patch.object(self.driver, "_provider_image", lambda: None), \
                    mock.patch.object(self.driver, "_run", side_effect=AssertionError("no process started")), \
                    mock.patch.dict(os.environ, dict(self.env, FAKE_HOST="{}")):
                with self.assertRaises(RuntimeError) as raised:
                    start()
                self.assertIn("the VM's disk write limit cannot be applied", str(raised.exception))


class Options(unittest.TestCase):
    def test_the_options_reach_the_original_setup_and_the_null_control(self):
        with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
            bench = json.load(f)
        given = {"write_mbps": "30", "write_iops": "300"}
        for setup in ("original", "yours"):
            values, bad = definitions.bench_options_of(bench, "offline36", given, setup)
            self.assertEqual(bad, [])
            env = run.option_env(values)
            self.assertEqual((env["NULLIUS_OPTION_WRITE_MBPS"], env["NULLIUS_OPTION_WRITE_IOPS"]), ("30", "300"), setup)
        values, _ = definitions.bench_options_of(bench, "offline36", None, "yours")
        self.assertEqual((values["write_mbps"], values["write_iops"]), ("none", "none"))
        self.assertTrue(definitions.bench_options_of(bench, "offline36", {"write_mbps": "30mb"}, "original")[1])


class Turn(unittest.TestCase):
    def test_the_original_setups_turn_holds_500_steps(self):
        with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
            original = json.load(f)["setups"]["original"]
        self.assertEqual(original["turn_seconds"], 86400)
        self.assertGreater(original["turn_seconds"], 2 * 500 * 32)


class Snapshot(unittest.TestCase):
    def test_a_hosts_snapshot_carries_the_startup_file(self):
        from nullius.standard.runner import payload
        tmp = Path(tempfile.mkdtemp(prefix="nullius-osworld2-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {"NULLIUS_HOME": str(tmp / "profile"), "NULLIUS_CATALOGUE": str(tmp / "catalogue")}):
            _, root = payload.build(str(tmp / "cache"))
        entry = Path(root) / "app" / "nullius" / "catalogue" / "benchmarks" / "osworld2"
        for name in ("driver.py", "nop.py", "agents.json", "startup/sitecustomize.py"):
            self.assertEqual((entry / name).read_bytes(), (Path(HERE) / name).read_bytes(), name)
        manifest = json.loads((Path(root) / "manifest.json").read_text())
        self.assertIn("catalogue/benchmarks/osworld2/startup/sitecustomize.py", manifest["files"])


if __name__ == "__main__":
    unittest.main()
