"""Constraints: one table, its validation, the docker flags and the systemd
properties of each value set and none other, the lower of a machine's and a
task's own value, the probes read back, the warnings, and a run that takes
them: its containers' flags, its processes' scopes, its concurrency, its
driver's environment and its config."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from nullius import constraints, prerequisites, settings
from nullius.standard import container, definitions, install, recipe, run

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
ALL = {"cpus": 2, "memory_mb": 512, "processes": 100, "concurrency": 3, "cpu_set": "0-1", "gpu_ids": [0]}


class Table(unittest.TestCase):
    def test_each_entry_is_whole(self):
        for c in constraints.TABLE:
            self.assertTrue(c.label and c.kind in ("cap", "pin"), c.key)
            if "docker" in c.apply:
                self.assertTrue(c.files and c.enforced, c.key)        # a container flag is read back
        self.assertEqual(list(constraints.container(ALL)), ["cpus", "memory_mb", "processes", "cpu_set"])

    def test_values_are_refused_by_key_type_and_minimum(self):
        self.assertEqual(constraints.problems(ALL), [])
        self.assertEqual(constraints.problems({"gpu_ids": []}), [])
        for bad in ({"disk_gb": 10}, {"cpus": "2"}, {"cpus": True}, {"cpus": 0.001}, {"cpus": 0}, {"processes": 2.5}, {"processes": 0}, {"memory_mb": 5},
                    {"concurrency": 0}, {"cpu_set": "3-1"}, {"cpu_set": "a"}, {"cpu_set": "0-3000000000"}, {"gpu_ids": [0, 0]}, {"gpu_ids": [-1]},
                    {"gpu_ids": "0"}):
            self.assertTrue(constraints.problems(bad), bad)

    def test_the_setting_is_json_by_machine(self):
        ok = json.dumps({"local": {"cpus": 4}, "bench": {"gpu_ids": []}, "GPU lab=u@10.0.0.4:22": {"memory_mb": 1024}})
        self.assertIsNone(settings.BY_NAME[constraints.SETTING].validate(ok))
        self.assertIn("JSON", settings.BY_NAME[constraints.SETTING].validate("cpus=4"))
        self.assertIn("not a host", settings.BY_NAME[constraints.SETTING].validate('{"not a host": {}}'))
        self.assertIn("not a constraint", settings.BY_NAME[constraints.SETTING].validate('{"local": {"disk_gb": 1}}'))
        self.assertEqual(constraints.of("bench", {constraints.SETTING: ok}), {"gpu_ids": []})
        self.assertEqual(constraints.of("other", {constraints.SETTING: ok}), {})
        self.assertEqual(constraints.of("local", {}), {})                                # unset: no constraint
        invalid = {constraints.SETTING: '{"local": {"cpus": 2}, "bench": {"cpus": 0}}'}
        with self.assertRaisesRegex(ValueError, "HOST_CONSTRAINTS must be valid constraints"):
            constraints.of("local", invalid)                                             # invalid: refused, never none
        self.assertEqual(constraints.of("local", invalid, strict=False), {})                # a hint: none
        for text in ('{"local": {"cpus": Infinity}}', '{"local": {"memory_mb": 1e400}}', '{"local": {"processes": NaN}}',
                     '{"local": {"cpu_set": "0-3\\n"}}', '{"local": {"cpu_set": 3}}'):
            self.assertTrue(constraints.validate_setting(text), text)                       # said, not raised
        self.assertTrue(constraints.problems({"cpus": float("inf")}))
        with self.assertRaisesRegex(ValueError, "must be"):
            constraints.rekey(invalid[constraints.SETTING], "bench", "other")             # never every machine's dropped

    def test_flags_only_for_what_is_set(self):
        self.assertEqual(constraints.argv({}), [])
        self.assertEqual(constraints.argv({"processes": 64, "concurrency": 2, "gpu_ids": [1]}), ["--pids-limit", "64"])
        self.assertEqual(constraints.argv(ALL), ["--cpus", "2", "--memory", "512m", "--memory-swap", "512m",
                                                 "--pids-limit", "100", "--cpuset-cpus", "0-1"])

    def test_a_task_s_own_memory_alone_keeps_its_swap(self):
        self.assertEqual(constraints.argv({}, {"memory_mb": 1024, "cpus": 2}), ["--cpus", "2", "--memory", "1024m"])   # as before constraints
        for machine in (4096, 512):                       # the machine's memory: no swap, whichever value applies
            a = constraints.argv({"memory_mb": machine}, {"memory_mb": 1024})
            low = f"{min(machine, 1024)}m"
            self.assertEqual(a, ["--memory", low, "--memory-swap", low])

    def test_the_lower_value_wins(self):
        self.assertEqual(constraints.lowest({"cpus": 2, "memory_mb": 4096}, {"cpus": 4, "memory_mb": 1024}),
                         {"cpus": 2, "memory_mb": 1024})
        self.assertEqual(constraints.lowest({}, {"cpus": 1.5}), {"cpus": 1.5})
        self.assertEqual(constraints.concurrency(8, {"concurrency": 3}), 3)
        self.assertEqual(constraints.concurrency(2, {"concurrency": 3}), 2)
        self.assertEqual(constraints.concurrency(8, {}), 8)

    def test_what_processes_on_the_machine_get(self):
        # nvidia-smi's indices: CUDA numbers them by PCI bus too
        self.assertEqual(constraints.environment({"gpu_ids": [0, 2]}), {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": "0,2"})
        self.assertEqual(constraints.environment({"gpu_ids": []})["CUDA_VISIBLE_DEVICES"], "")
        self.assertEqual(constraints.environment({"cpus": 2}), {})
        self.assertEqual(constraints.cpu_list("0-2,5"), [0, 1, 2, 5])

    def test_cli_words_read_back(self):
        words = constraints.cli_words(ALL)
        back = {w.split("=")[0]: constraints.cli_value(*w.split("=", 1)) for w in words[1::2]}
        self.assertEqual(back, ALL)
        self.assertEqual(constraints.cli_value("gpu_ids", ""), [])
        big = {"memory_mb": 1048576, "processes": 4194304, "concurrency": 1000000, "cpus": 1234567.0}
        self.assertEqual({k: constraints.cli_value(k, constraints.spell(v)) for k, v in big.items()}, big)   # exact
        self.assertEqual(constraints.spell(0.1), "0.1"); self.assertEqual(constraints.spell(4096.0), "4096")
        self.assertEqual(constraints.argv({"cpus": 1234567.0})[1], "1234567")
        self.assertEqual(constraints.scope_argv({"cpus": 1234567.5}, ())[1], "CPUQuota=123456750%")


class Probe(unittest.TestCase):
    SAID = ("ncpu=8\nmemtotal_kb=16384000\ngpu=0, NVIDIA RTX 3050, 4096\nengine=29.9.3|Engine,|[name=seccomp]\nplace.local=n:8 a:0-7\nplace.containers=n:8 a:0-7\n"
            "cpus=1000 100000 \nmemory_mb=67108864 0 \nprocesses=max \ncpu_set=docker: Error response: cpuset unavailable \n")

    def test_the_script_starts_one_container_per_flag(self):
        script = constraints.probe(constraints.samples(), image="img")
        self.assertEqual(script.count("docker run --rm --network none"), 5)          # and one where a unit's container lands
        self.assertIn("--pids-limit 1 img cat /sys/fs/cgroup/pids.max", script)
        self.assertIn("--memory 64m --memory-swap 64m img", script)                  # 64 MB: rootless Docker refuses 6
        self.assertIn("nvidia-smi --query-gpu=index,name,memory.total", script)
        self.assertNotIn("docker run", constraints.probe({"concurrency": 2, "gpu_ids": [0]}))

    def test_the_capacity_is_the_machine_s_own_and_the_engine_is_asked_only_where_one_answers(self):
        script = constraints.probe(constraints.samples(), image="img")
        for asked in ("nproc", "Cpus_allowed_list", "MemTotal", "/proc/meminfo"):
            self.assertIn(asked, script)
        before, engine = script.split("if docker version >/dev/null 2>&1; then ")
        self.assertNotIn("docker", before)                                       # the capacity needs no engine
        self.assertIn("docker run", engine); self.assertIn("docker info", engine)
        bare = constraints.read("ncpu=4\nplace.local=n:4 a:0-3\nmemtotal_kb=2048000\n", constraints.samples())
        self.assertEqual(bare["capacity"], {"cpus": 4, "cpu_ids": "0-3", "memory_mb": 2000, "gpus": []})
        self.assertEqual((bare["engine"], bare["enforced"]), ({}, []))
        self.assertTrue(constraints.engine_less(bare)); self.assertFalse(constraints.engine_less(constraints.read(self.SAID, constraints.samples())))
        self.assertFalse(constraints.engine_less(None)); self.assertFalse(constraints.engine_less({"test": {"ok": False}}))   # never measured
        self.assertFalse(constraints.engine_less(constraints.read("", {})))                     # nothing read: not known
        # no engine: the caps say nothing of enforcement (the panel says what they need), the rest is checked
        self.assertEqual(constraints.warnings({"cpus": 2, "processes": 64, "cpu_set": "0-7", "gpu_ids": [0]}, bare),
                         {"cpu_set": "More than the machine has.", "gpu_ids": "More than the machine has."})
        # a host over ssh, this machine in a shell: one probe, read alike
        from nullius import hosts
        seen = []
        def run(argv, timeout):
            seen.append(argv); return 0, "ncpu=2\nmemtotal_kb=1024000\n", ""
        self.assertEqual(hosts.measure(hosts.parse("bench"), run=run)["capacity"], {"cpus": 2, "memory_mb": 1000, "gpus": []})
        self.assertEqual(hosts.measure(None, run=run)["capacity"], {"cpus": 2, "memory_mb": 1000, "gpus": []})
        self.assertEqual((seen[0][0], seen[0][-2], seen[1][:2]), ("ssh", "bench", ["sh", "-c"]))
        self.assertEqual(seen[0][-1].split("; if ")[0], seen[1][-1].split("; if ")[0])            # the same capacity
        engine = lambda argv: argv[-1].split("; u=$(id -u)")[0]
        self.assertIn("--user ", engine(seen[1])); self.assertNotIn("--user ", engine(seen[0]))  # here: as a unit starts
        self.assertEqual(seen[0][-1].split("; u=$(id -u)")[1], seen[1][-1].split("; u=$(id -u)")[1])   # the same scope probe
        hosts.measure(hosts.parse("bench"), run=run, engine="podman")
        self.assertIn("if podman version", seen[2][-1]); self.assertIn("label=disable", seen[2][-1])   # there: the engine test found

    def test_the_ceiling_is_the_lowest_limit_on_the_cgroup_path(self):
        ceiling = constraints.ceiling
        self.assertEqual(ceiling("n:8 m:4294967296 c:400000/100000"), {"cpus": 4, "memory_mb": 4096})      # limited
        self.assertEqual(ceiling("n:8 m:max c:max/100000 m: c:"), {"cpus": 8})                              # max is no limit
        self.assertEqual(ceiling("n:8 m:max c:max/100000 m:max c: m:2147483648 c:300000/100000"),           # on a parent only
                         {"cpus": 3, "memory_mb": 2048})
        self.assertEqual(ceiling("n:8 c:150000/100000"), {"cpus": 1.5}); self.assertEqual(ceiling("n:8 c:100000/300000"), {"cpus": 0.33})
        self.assertEqual(ceiling(""), {})
        # the walk itself, from a cgroup two levels down a namespace root
        root = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, root)
        for d, mem, cpu in (("", "4294967296", "400000 100000"), ("/a", "max", "max 100000"), ("/a/b", "max", None)):
            os.makedirs(root + d, exist_ok=True)
            with open(f"{root}{d}/memory.max", "w") as f:
                f.write(mem + "\n")
            if cpu:
                with open(f"{root}{d}/cpu.max", "w") as f:
                    f.write(cpu + "\n")
        with open(os.path.join(root, "self"), "w") as f:
            f.write("0::/a/b\n")
        walk = constraints.WALK.replace(constraints.CGROUP, root).replace("/proc/self/cgroup", os.path.join(root, "self"))
        out = subprocess.run(["sh", "-c", walk], capture_output=True, text=True).stdout
        got = ceiling(out)
        self.assertEqual(constraints.cpu_list(got.pop("cpu_ids")), sorted(os.sched_getaffinity(0)), out)   # its own cpuset and affinity
        self.assertEqual(got, {"cpus": min(4, os.cpu_count() or 4), "memory_mb": 4096}, out)
        self.assertEqual(ceiling("n:4 a:6-9 m:max"), {"cpus": 4, "cpu_ids": "6-9"})

    def test_a_cpuset_without_cpu_0_is_probed_with_a_cpu_it_has(self):
        # an engine whose containers may run on CPUs 6-9 only, as on a machine pinned there
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "docker"), "w") as f:
            f.write('#!/bin/sh\ncase "$1" in version) echo "29.9.3|Engine,"; exit;; info|image) exit;; esac\n'
                    'case "$*" in *--cgroupns=host*) echo "n:4 a:6-9 m:max c:max/100000"; exit;; esac\n'
                    'while [ $# -gt 1 ]; do [ "$1" = --cpuset-cpus ] && case $2 in [6-9]) echo "$2"; exit;; '
                    '*) echo "docker: Error response from daemon: Requested CPUs are not available - requested $2, available: 6-9"; exit 125;; esac; shift; done\n')
        os.chmod(os.path.join(d, "docker"), 0o755)
        out = subprocess.run(["sh", "-c", constraints.probe(constraints.samples(), image="img")], capture_output=True, text=True,
                             env={**os.environ, "PATH": d + os.pathsep + os.environ["PATH"]}).stdout
        self.assertIn("cpu_set=6", out)
        found = constraints.read(out, constraints.samples())
        self.assertIn("cpu_set", found["enforced"]); self.assertEqual(found["capacity"]["cpu_ids"], "6-9")
        self.assertEqual(constraints.samples(found["capacity"])["cpu_set"], "6")         # what Settings says was asked
        self.assertEqual(constraints.explain("cpu_set", "6", "5"), "Asked CPUs 6, the container read 5")
        self.assertEqual(constraints.samples({})["cpu_set"], "0")                       # not read: CPU 0, as the probe takes
        self.assertEqual(constraints.warnings({"cpu_set": "0-3"}, found), {"cpu_set": "More than the machine has."})

    def test_each_placement_has_its_ceiling(self):
        whole = "ncpu=16\nmemtotal_kb=33554432\n"
        user = "place.local=n:16 m:4294967296 c:400000/100000\n"                    # the user's slice: 4 CPUs, 4 GB
        system = "place.containers=n:16 m:max c:max/100000\n"                      # rootful containers: under system.slice
        engine = "engine=29.9.3|Engine,|[name=seccomp]\n"
        cap = lambda out: constraints.read(out, {})["capacity"]
        self.assertEqual(cap(whole + user), {"cpus": 4, "memory_mb": 4096, "gpus": []})                     # no engine: local runs
        self.assertEqual(cap(whole + user + engine + system),                                               # container runs, drivers local
                         {"cpus": 16, "memory_mb": 32768, "local": {"cpus": 4, "memory_mb": 4096}, "gpus": []})
        self.assertEqual(cap(whole + user + engine + system.replace("max c:max", "4294967296 c:400000")),   # the same: one value
                         {"cpus": 4, "memory_mb": 4096, "gpus": []})
        self.assertEqual(cap(whole + user + engine), {"cpus": 16, "memory_mb": 32768, "local": {"cpus": 4, "memory_mb": 4096}, "gpus": []})  # not read: the whole machine
        scope = "place.scope=n:16 m:max c: m:2147483648 c:200000/100000\n"          # a scope's, before the probe's own
        self.assertEqual(cap(whole + user + scope), {"cpus": 2, "memory_mb": 2048, "gpus": []})
        # the CPUs each placement may use; a scope's cpu_set is probed on the first of its own
        placed = constraints.read(whole + "place.local=n:16 a:2-3\n" + engine + "place.containers=n:16 a:0-15\n"
                                  "place.scope=n:16 a:4-5\nscope=ok\nscope.cpu_set=4\n", {"cpu_set": constraints.FIRST_CPU})
        self.assertEqual((placed["capacity"]["cpu_ids"], placed["capacity"]["local"]["cpu_ids"]), ("0-15", "4-5"))
        self.assertEqual(placed["systemd"]["enforced"], ["cpu_set"])
        script = constraints.probe(constraints.samples(), image="img") + constraints.scope_probe(constraints.samples())
        self.assertIn("printf 'place.local=", script); self.assertIn("--cgroupns=host img sh -c", script); self.assertIn("printf 'place.scope=", script)

    def test_read_back(self):
        got = constraints.read(self.SAID, constraints.samples())
        self.assertEqual(got["capacity"], {"cpus": 8, "memory_mb": 16000, "cpu_ids": "0-7",
                                           "gpus": [{"index": 0, "name": "NVIDIA RTX 3050", "memory_mb": 4096}]})
        self.assertEqual(got["enforced"], ["cpus", "memory_mb"])
        self.assertEqual(sorted(got["refused"]), ["cpu_set", "processes"])
        self.assertIn("cpuset unavailable", got["refused"]["cpu_set"])
        self.assertEqual(got["applied"], ["cpu_set", "gpu_ids"])
        swap = self.SAID.replace("memory_mb=67108864 0", "memory_mb=67108864 max")
        self.assertNotIn("memory_mb", constraints.read(swap, constraints.samples())["enforced"])   # swap left open

    def test_warnings(self):
        found = constraints.read(self.SAID, constraints.samples())
        self.assertEqual(constraints.warnings({"cpus": 2}, None), {})                      # never tested
        self.assertEqual(constraints.warnings({"cpus": 2, "memory_mb": 1024}, found), {})
        w = constraints.warnings({"cpus": 12, "processes": 64, "cpu_set": "6-9", "gpu_ids": [0, 1]}, found)
        self.assertEqual(set(w), {"cpus", "processes", "cpu_set", "gpu_ids"})
        self.assertEqual(w["processes"], "Containers here do not enforce it: see Info, or remove it.")
        self.assertEqual(constraints.warnings({"cpus": 4, "concurrency": 3}, found)["concurrency"],
                         "Together these units can take more than the machine has.")

    def test_the_engine_is_read_and_a_machine_s_record_follows_its_entry(self):
        got = constraints.read("engine=5.8.9|Podman Engine,|[name=seccomp name=rootless]\n", {})
        self.assertEqual(got["engine"], {"kind": "podman", "version": "5.8.9", "rootless": True, "userns_remap": False})
        remapped = constraints.read("engine=29.9.3|Engine,|[name=seccomp,profile=builtin name=userns name=cgroupns]\n", {})["engine"]
        self.assertEqual((remapped["kind"], remapped["rootless"], remapped["userns_remap"]), ("docker", False, True))
        self.assertEqual(constraints.read("engine=29.9.3|Engine,containerd,|[name=seccomp]\n", {})["engine"]["kind"], "docker")
        self.assertEqual(constraints.read("", {})["engine"], {})
        self.assertEqual(json.loads(constraints.rekey('{"a": {"cpus": 2}, "b": {"cpus": 1}}', "a", "c")), {"c": {"cpus": 2}, "b": {"cpus": 1}})
        self.assertEqual(json.loads(constraints.rekey('{"a": {"cpus": 2}, "b": {"cpus": 1}}', "a", None)), {"b": {"cpus": 1}})
        self.assertIsNone(constraints.rekey('{"a": {"cpus": 2}}', "a", None))
        home = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, home, True)
        env = {"NULLIUS_HOME": home}
        constraints.save("a", {"capacity": {"cpus": 8}}, env); constraints.save("a", {"test": {"ok": False}}, env)
        self.assertEqual((constraints.load(env)["a"]["capacity"], constraints.load(env)["a"]["test"]), ({"cpus": 8}, {"ok": False}))  # kept
        constraints.move("a", "b", stale=True, env=env)
        self.assertEqual((list(constraints.load(env)), constraints.load(env)["b"]["stale"]), (["b"], True))
        constraints.save("b", {"test": {"ok": True}}, env)
        self.assertNotIn("stale", constraints.load(env)["b"])                                   # tested again: current
        constraints.move("b", None, env=env)
        self.assertEqual(constraints.load(env), {})

    def test_what_did_not_hold_is_said_in_words(self):
        self.assertEqual(constraints.explain("cpus", 1.5, "200000 100000"), "Asked 1.5 CPUs, the container had 2 CPUs")
        self.assertEqual(constraints.explain("cpus", 0.01, "max 100000"), "Asked 0.01 CPUs, the container had no limit")
        self.assertEqual(constraints.explain("memory_mb", 256, "2147483648 2147483648"), "Asked 256 MB, the container had 2 GB")
        self.assertEqual(constraints.explain("memory_mb", 2048, "2147483648 max"), "Asked 2 GB, the container could also use swap")
        self.assertEqual(constraints.explain("processes", 1, "max"), "Asked 1 process, the container had no limit")
        self.assertEqual(constraints.explain("cpus", 1, "no answer"), "Asked 1 CPU: the engine did not answer")
        long = "WARNING: Your kernel does not support cpuset or the cgroup is not mounted. Cpuset discarded. " * 3
        said = constraints.explain("cpu_set", "0", long)
        self.assertTrue(said.startswith("Asked CPUs 0: the engine did not apply it (WARNING: Your kernel")); self.assertLess(len(said), 220)

    def test_what_a_run_there_would_be_refused_and_why(self):
        found = constraints.read(self.SAID, constraints.samples())            # enforces cpus and memory
        self.assertEqual(list(constraints.not_enforced({"cpus": 2, "processes": 64, "concurrency": 2}, found)), ["processes"])
        self.assertEqual(constraints.not_enforced({"cpus": 2}, None), {})                                  # never tested
        bare = constraints.read("ncpu=4\nmemtotal_kb=2048000\n", constraints.samples())
        self.assertEqual(list(constraints.not_enforced({"cpus": 2, "gpu_ids": [0]}, bare, "container")), ["cpus"])   # no engine
        self.assertEqual(constraints.not_enforced({"cpus": 2}, bare), {})             # Settings: no container runs there
        self.assertEqual(constraints.not_enforced({"cpus": 2}, {"test": {"ok": False}}, "container"), {})   # not reached: not measured
        rootless = {"engine": {"kind": "podman", "rootless": True}, "enforced": [],
                    "refused": {"cpus": "max 100000", "memory_mb": "max max", "processes": "max",
                                "cpu_set": "Error: crun: controller `cpuset` is not available under /sys/fs/cgroup/x"}}
        self.assertTrue(constraints.undelegated(rootless))
        self.assertFalse(constraints.undelegated(dict(rootless, engine={"kind": "podman", "rootless": False})))   # rootful: another cause
        self.assertFalse(constraints.undelegated(dict(rootless, enforced=["cpus"])))                           # one holds: delegated
        self.assertFalse(constraints.undelegated(dict(rootless, refused=dict(rootless["refused"], cpus="200000 100000"))))   # a limit not ours

    def test_a_test_cut_short_keeps_what_it_did_not_measure(self):
        from nullius import hosts
        home = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, home, True)
        env = {"NULLIUS_HOME": home}
        good = constraints.read(self.SAID, constraints.samples())
        constraints.save("h", good, env)
        cut = hosts.measure(hosts.parse("u@h"), run=lambda argv, t: (124, "", ""))
        self.assertEqual(cut, {"applied": ["cpu_set", "gpu_ids"], "error": "no answer"})
        constraints.save("h", cut, env)
        kept = constraints.load(env)["h"]
        self.assertEqual((kept["capacity"], kept["enforced"], kept["refused"]), (good["capacity"], good["enforced"], good["refused"]))

    def test_kept_per_machine(self):
        home = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, home, True)
        env = {"NULLIUS_HOME": home}
        failed = []                                       # Test all: every machine at once, none lost
        each = [threading.Thread(target=lambda i=i: _or(failed, lambda: constraints.save(f"m{i}", {"enforced": []}, env))) for i in range(24)]
        [t.start() for t in each]; [t.join() for t in each]
        self.assertEqual((failed, len(constraints.load(env))), ([], 24))
        self.assertEqual([n for n in os.listdir(os.path.dirname(constraints.store_path(env))) if n.endswith(".part")], [])
        constraints.save("local", {"enforced": ["cpus"]}, env); constraints.save("bench", {"enforced": []}, env)
        self.assertEqual(constraints.load(env)["local"]["enforced"], ["cpus"])
        self.assertEqual(sorted(constraints.load(env))[:2], ["bench", "local"])


def _or(failed: list, call) -> None:
    try:
        call()
    except Exception as e:                                # a thread's error, for the test to see
        failed.append(e)


class Containers(unittest.TestCase):
    def setUp(self):
        self.addCleanup(container.cap, {})

    def test_every_container_a_run_starts_takes_the_machine_s_caps(self):
        from nullius.standard import setup_container
        container.cap({"cpus": 1.5, "memory_mb": 512, "concurrency": 2, "gpu_ids": [0]})
        flags = ["--cpus", "1.5", "--memory", "512m", "--memory-swap", "512m"]
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, ".work"))
        self.addCleanup(setattr, install, "HOME", install.HOME); install.HOME = tmp
        seen = []
        with mock.patch.object(container, "_docker", lambda a, **k: seen.append(a) or mock.Mock(stdout="", returncode=0)), \
                mock.patch.object(container.Box, "keeper", return_value="7"):
            container.Box("j", "u1", "img", workdir="/w", network="net").start()
        driver = setup_container.Driver({"_here": tmp}, image="img", app=tmp, job="j", work=tmp, upstream=None, data=None, needs=[])
        built = {"agent": container.run_argv("img", ["a"], name="n", network="net", job="j", workdir="/w", mounted=[], env={}, path="/bin"),
                 "check": container.check_start(), "install": container.install_argv("img", "/i", ["true"]),
                 "relay": container.relay_argv("img", name="r", job="j", app="/a", record="/r", key_file="/k", base="http://b", model=None, routes={}),
                 "forwarder": container.forward_argv("img", name="f", job="j", port=1),
                 "door": container.door_argv("img", name="d", job="j", port=1, socket_path="/s"),
                 "proxy": container.egress_argv("img", name="e", job="j"), "own image": seen[-1],
                 "setup driver": driver.command({}, tmp)[0]}
        for what, argv in built.items():
            self.assertTrue(any(argv[i:i + len(flags)] == flags for i in range(len(argv))), what)
            self.assertNotIn("concurrency", " ".join(argv))
        with mock.patch.object(container, "podman", return_value=False):        # a driver's checks, in its own process
            self.assertEqual(json.loads(container.check_env()[container.CHECK_ENGINE]), container.check_start())
        container.cap({})
        self.assertNotIn("--cpus", container.run_argv("img", ["a"], name="n", network="net", job="j", workdir="/w",
                                                      mounted=[], env={}, path="/bin"))

    def test_a_task_s_box_takes_the_lower_of_each(self):
        seen = []
        with mock.patch.object(container, "_docker", lambda a, **k: seen.append(a) or mock.Mock(stdout="", returncode=0)), \
                mock.patch.object(container.Box, "keeper", return_value="7"):
            container.cap({"cpus": 2, "memory_mb": 4096})
            container.Box("j", "u1", "img", workdir="/w", network="net", task={"cpus": 4, "memory_mb": 1024}).start()
            a = seen[-1]
            self.assertEqual((a[a.index("--cpus") + 1], a[a.index("--memory") + 1], a[a.index("--memory-swap") + 1]), ("2", "1024m", "1024m"))
            container.cap({})
            container.Box("j", "u1", "img", workdir="/w", network="net", task={"memory_mb": 1024}).start()
            self.assertNotIn("--memory-swap", seen[-1])       # no machine constraint: the task's flags as they were

    def test_the_engine_is_asked_before_the_first_unit(self):
        with mock.patch.object(container.subprocess, "run", return_value=mock.Mock(stdout="engine=29.9.3|Engine,|\ncpus=max 100000 \nprocesses=64 \n")):
            self.assertEqual(list(container.unenforced("img", {"cpus": 2, "processes": 64, "gpu_ids": [0]})), ["cpus"])
        with mock.patch.object(container.subprocess, "run", return_value=mock.Mock(stdout="ncpu=4\n")):           # no engine answers
            self.assertEqual(container.unenforced("img", {"processes": 64}), {"processes": "no Docker or Podman answers"})
        self.assertEqual(container.unenforced("img", {"gpu_ids": [0]}), {})                 # no container: no probe


class ARun(unittest.TestCase):
    def setUp(self):
        self.addCleanup(container.cap, {})
        self.tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, install, "HOME", install.HOME); install.HOME = os.path.join(self.tmp, "home")
        self.b, self.a = definitions.get("benchmark", "fakebench", ROOT), definitions.get("agent", "fake", ROOT)
        self.env = {"MODEL_API_URL": "http://127.0.0.1:9/v1"}

    def test_a_run_takes_its_machine_s_constraints(self):
        with mock.patch.object(container, "unusable", return_value=None):
            r = run.Run(self.b, self.a, model="m", provider="custom", env=self.env, backend="docker", concurrency=8,
                        jobs_root=self.tmp, constraints=ALL)
        self.assertEqual(r.concurrency, 3)
        self.assertEqual(r.container_limits, constraints.container(ALL))
        local = run.Run(self.b, self.a, model="m", provider="custom", env=self.env, unsandboxed=True, jobs_root=self.tmp,
                        constraints=ALL)
        # an own image's container takes them on the local backend too; a scope once ready
        self.assertEqual((local.constraints, local.container_limits, local.scope_limits), (ALL, constraints.container(ALL), {}))
        with self.assertRaisesRegex(run.Refused, "not a constraint"):
            run.Run(self.b, self.a, model="m", provider="custom", env=self.env, unsandboxed=True, constraints={"disk_gb": 1})

    def test_a_local_run_caps_each_own_image_and_checks_its_engine_first(self):
        from tests.test_standard_own_image import bench
        b = bench()
        given = {"cpus": 2, "memory_mb": 4096}
        with mock.patch.object(container, "unusable", return_value=None):
            r = run.Run(b, definitions.control(b, "oracle"), model="m", provider="custom", env=self.env, backend="local",
                        jobs_root=self.tmp, constraints=given)
        with mock.patch.object(container, "unenforced", return_value={"memory_mb": "max max"}) as asked, \
                mock.patch.object(constraints, "scope_check", return_value={"why": "", "enforced": ["cpus", "memory_mb"], "refused": {}}), \
                self.assertRaisesRegex(run.Refused, r"engine does not apply Memory \(Asked 4 GB, the container had no limit\)"):
            r.ready()
        self.assertEqual(asked.call_args.args, (constraints.PROBE_IMAGE, given))
        boxes = []
        with mock.patch.object(container, "unusable", return_value=None), \
                mock.patch.object(r, "own_image", return_value="img"), \
                mock.patch.object(container, "image_config", return_value={"workdir": "/w", "path": "/bin"}), \
                mock.patch.object(container, "Box", lambda *a, **k: boxes.append(k) or mock.Mock()):
            r.own_box("u1", {"image": {"pull": "x/y@sha256:" + "a" * 64, "memory_mb": 1024}}, None, self.tmp)
        self.assertEqual((container._CEILING, boxes[0]["task"]), (given, {"memory_mb": 1024}))

    def test_a_local_run_records_them_and_its_driver_sees_its_gpus(self):
        seen = {}
        real = run.jsonl.Proc

        def proc(cmd, cwd, env, log, stopped=None):
            seen.update(env)
            return real(cmd, cwd, env, log, stopped=stopped)
        with mock.patch.object(run.jsonl, "Proc", proc):
            job = run.run(self.b, self.a, model="m", provider="custom", jobs_root=self.tmp, env=self.env, limit=1,
                          say=lambda s: None, unsandboxed=True, constraints={"gpu_ids": [1], "concurrency": 1})
        with open(os.path.join(job, "config.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["standard"]["constraints"], {"gpu_ids": [1], "concurrency": 1})
        self.assertEqual(cfg["n_concurrent_trials"], 1)
        self.assertEqual(seen["CUDA_VISIBLE_DEVICES"], "1")

    def test_a_local_run_starts_its_driver_and_agent_in_scopes(self):
        stub = exec_stub(self)
        seen = []
        for mod, name in ((run.jsonl, "Proc"), (run.acp, "Client")):
            real = getattr(mod, name)
            patch = mock.patch.object(mod, name, lambda cmd, *a, real=real, **k: seen.append(cmd) or real(cmd, *a, **k))
            patch.start(); self.addCleanup(patch.stop)
        ok = {"why": "", "enforced": ["processes"], "refused": {}}
        with mock.patch.object(constraints, "SYSTEMD_RUN", (stub,)), mock.patch.object(constraints, "scope_check", return_value=ok):
            job = run.run(self.b, self.a, model="m", provider="custom", jobs_root=self.tmp, env=self.env, limit=1,
                          say=lambda s: None, unsandboxed=True, constraints={"processes": 4096})
        self.assertTrue(len(seen) >= 2 and all(cmd[:4] == [stub, "-p", "TasksMax=4096", "--"] for cmd in seen), seen)
        with open(os.path.join(job, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["constraints"], {"processes": 4096})


def exec_stub(test, body='while [ $# -gt 0 ] && [ "$1" != -- ]; do shift; done; shift; exec "$@"') -> str:
    """A ``systemd-run`` that runs its command as a scope does, in its own process (``exec``)."""
    d = tempfile.mkdtemp(); test.addCleanup(shutil.rmtree, d, True)
    path = os.path.join(d, "systemd-run")
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


class Scope(unittest.TestCase):
    """A systemd scope for each process a run starts here."""

    def test_the_argv_has_a_property_per_value_set_and_none_other(self):
        self.assertEqual(constraints.scoped(ALL), {"cpus": 2, "memory_mb": 512, "processes": 100, "cpu_set": "0-1"})
        self.assertEqual(constraints.scope_argv({"cpus": 1.5, "memory_mb": 256, "processes": 64, "cpu_set": "0,2", "gpu_ids": [0]}),
                         ["systemd-run", "--user", "--scope", "-q", "-p", "CPUQuota=150%", "-p", "MemoryMax=256M",
                          "-p", "MemorySwapMax=0", "-p", "TasksMax=64", "-p", "AllowedCPUs=0,2", "--"])
        self.assertEqual(constraints.scope_argv({"cpus": 0.01}), [*constraints.SYSTEMD_RUN, "-p", "CPUQuota=1%", "--"])
        self.assertEqual(constraints.wrap(["a"], {"X": "1"}, {"concurrency": 2, "gpu_ids": []}), (["a"], {"X": "1"}))   # nothing to hold

    def test_systemd_run_finds_the_user_manager_and_the_command_does_not_see_how(self):
        d = tempfile.mkdtemp(dir="/tmp"); self.addCleanup(shutil.rmtree, d, True)
        self.assertEqual(constraints.session({"XDG_RUNTIME_DIR": d}), {"XDG_RUNTIME_DIR": d})            # no bus there
        bus = socket.socket(socket.AF_UNIX); self.addCleanup(bus.close); bus.bind(os.path.join(d, "bus"))
        self.assertEqual(constraints.session({"XDG_RUNTIME_DIR": d}),
                         {"XDG_RUNTIME_DIR": d, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={d}/bus"})
        self.assertEqual(constraints.session({})["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")         # derived when unset
        self.assertEqual(constraints.session({"DBUS_SESSION_BUS_ADDRESS": "unix:abstract=x"})["DBUS_SESSION_BUS_ADDRESS"], "unix:abstract=x")
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d}), mock.patch.object(constraints, "_literal", lambda p: ()):
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            cmd, env = constraints.wrap(["a", "b"], {"PATH": "/bin"}, {"processes": 8})
            self.assertEqual(cmd, [*constraints.SYSTEMD_RUN, "-p", "TasksMax=8", "--", "env", "-u", "XDG_RUNTIME_DIR",
                                   "-u", "DBUS_SESSION_BUS_ADDRESS", "a", "b"])
            self.assertEqual(env, {"PATH": "/bin", "XDG_RUNTIME_DIR": d, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={d}/bus"})
            cmd, env = constraints.wrap(["a"], {"XDG_RUNTIME_DIR": "/mine"}, {"processes": 8})          # its own stays, and stays its own
            self.assertEqual((cmd[-4:], env["XDG_RUNTIME_DIR"]), (["env", "-u", "DBUS_SESSION_BUS_ADDRESS", "a"], "/mine"))

    def test_a_dollar_in_the_command_reaches_it_as_written(self):
        newer = exec_stub(self, 'echo "  --expand-environment=BOOL  Control expansion"')
        older = exec_stub(self, 'echo "  --scope  Run this as scope"')
        constraints._literal.cache_clear(); self.addCleanup(constraints._literal.cache_clear)
        self.assertEqual((constraints._literal(newer), constraints._literal(older), constraints._literal("/nonexistent")),
                         ((constraints.LITERAL,), (), ()))
        with mock.patch.object(constraints, "SYSTEMD_RUN", (newer, "--user")):
            self.assertEqual(constraints.wrap(["a"], {"XDG_RUNTIME_DIR": "/r", "DBUS_SESSION_BUS_ADDRESS": "b"}, {"processes": 8})[0],
                             [newer, "--user", constraints.LITERAL, "-p", "TasksMax=8", "--", "a"])
        self.assertIn("-q $x -p TasksMax=8 --", constraints.scope_probe({"processes": 8}))        # the probe asks its own machine's

    def test_a_scope_keeps_the_exit_code_the_streams_and_the_environment(self):
        stub = exec_stub(self)
        with mock.patch.object(constraints, "SYSTEMD_RUN", (stub,)):
            cmd, env = constraints.wrap(["sh", "-c", 'echo "$FOO ${DBUS_SESSION_BUS_ADDRESS:-none}"; exit 3'],
                                        {"PATH": os.environ["PATH"], "FOO": "bar"}, {"processes": 8})
        r = run.run_group(cmd, 30, env=env, stdout=subprocess.PIPE, text=True)
        self.assertEqual((r.returncode, r.stdout), (3, "bar none\n"))

    def test_a_unit_past_its_timeout_is_killed_with_its_group(self):
        stub, d = exec_stub(self), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(constraints, "SYSTEMD_RUN", (stub,)):
            cmd, env = constraints.wrap(["sh", "-c", f"sleep 60 & echo $! >{d}/child; sleep 60"], {"PATH": os.environ["PATH"]}, {"cpus": 1})
        with self.assertRaises(subprocess.TimeoutExpired):
            run.run_group(cmd, 1, env=env)
        with open(f"{d}/child") as f:
            child = int(f.read())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):                  # the background child went with its group
            os.kill(child, 0)

    def probe(self, values=None, ssh=False, systemctl=0, linger="yes", runs="exit 0", bus=True, booted=True, v2=True, tools=True):
        """``scope_check`` against stand-ins for systemd's programs and files."""
        bin_, run_dir, fs = (tempfile.mkdtemp(dir="/tmp") for _ in range(3))
        for x in (bin_, run_dir, fs):
            self.addCleanup(shutil.rmtree, x, True)
        for prog in ("sh", "id", "tr", "cat"):
            os.symlink(shutil.which(prog), os.path.join(bin_, prog))
        stubs = {"systemctl": f"exit {systemctl}", "loginctl": f"echo {linger}", **({"systemd-run": runs} if tools else {})}
        for name, body in stubs.items():
            with open(os.path.join(bin_, name), "w") as f:
                f.write("#!/bin/sh\n" + body + "\n")
            os.chmod(os.path.join(bin_, name), 0o755)
        if bus:
            s = socket.socket(socket.AF_UNIX); self.addCleanup(s.close); s.bind(os.path.join(run_dir, "bus"))
        if v2:
            open(os.path.join(fs, "cgroup.controllers"), "w").close()
        env = {"PATH": bin_, "XDG_RUNTIME_DIR": run_dir, **({"SSH_CONNECTION": "10.0.0.1 1 10.0.0.2 22"} if ssh else {})}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(constraints, "CGROUP", fs), \
                mock.patch.object(constraints, "BOOTED", fs if booted else os.path.join(fs, "absent")):
            return constraints.scope_check(values or {"cpus": 1})

    def test_each_reason_a_scope_cannot_start_is_its_own(self):
        uid = os.getuid()
        for kw, why in (({"tools": False}, "systemd-run is not on this machine"), ({"booted": False}, "systemd does not run this machine"),
                        ({"v2": False}, "no cgroup v2"), ({"systemctl": 3}, f"user@{uid}.service is not active"),
                        ({"bus": False}, "no session bus at "), ({"ssh": True, "linger": "no"}, "lingering is off: limits would end with the SSH session"),
                        ({"runs": "echo 'Failed to connect to bus: No medium found' >&2; exit 1"}, "Failed to connect to bus: No medium found")):
            got = self.probe(**kw)
            self.assertTrue(got["why"].startswith(why), (kw, got)); self.assertEqual(got["enforced"], [])
        self.assertEqual(self.probe(linger="no")["why"], "")                          # not under SSH: no lingering needed
        self.assertEqual(self.probe(ssh=True)["why"], "")

    def test_each_value_is_read_back_from_its_scope(self):
        # what a delegated cpu, memory and pids controller and an undelegated cpuset read back
        said = {"CPUQuota": "150000 100000", "MemoryMax": "268435456 0", "TasksMax": "max", "AllowedCPUs": "cat: cpuset.cpus.effective: No such file"}
        body = 'case "$*" in true) exit 0;; ' + " ".join(f"*{k}=*) echo '{v}';;" for k, v in said.items()) + " esac"
        got = self.probe({"cpus": 1.5, "memory_mb": 256, "processes": 64, "cpu_set": "0"}, runs=body)
        self.assertEqual((got["why"], got["enforced"], sorted(got["refused"])), ("", ["cpus", "memory_mb"], ["cpu_set", "processes"]))
        self.assertEqual(constraints.explain("processes", 64, got["refused"]["processes"], scope=True), "Asked 64 processes, the scope had no limit")
        self.assertTrue(constraints.explain("cpu_set", "0", got["refused"]["cpu_set"], scope=True).startswith("Asked CPUs 0: systemd did not apply it (cat:"))
        swap = self.probe({"memory_mb": 256}, runs='case "$*" in true) exit 0;; *) echo "268435456 max";; esac')
        self.assertEqual(constraints.explain("memory_mb", 256, swap["refused"]["memory_mb"], scope=True), "Asked 256 MB, the scope could also use swap")
        # a cpuset without CPU 0: the scope asks for the first CPU it has
        pinned = self.probe({"cpu_set": constraints.FIRST_CPU}, runs='case "$*" in *Cpus_allowed_list*) echo "n:4 a:6-9 m:max";; '
                            '*AllowedCPUs=6\\ *) echo 6;; *AllowedCPUs=*) echo "cat: cpuset.cpus.effective: Invalid argument";; esac')
        self.assertEqual((pinned["enforced"], pinned["refused"]), (["cpu_set"], {}))
        # one process and thread: the read-back starts nothing (builtins, then exec)
        inside = constraints.scope_probe({"processes": 1}).split("sh -c ")[-1].split(" 2>&1")[0]
        self.assertIn("exec cat pids.max", inside); self.assertNotIn("$(", inside)

    def test_a_run_holds_the_caps_or_is_refused_and_the_cpu_set_falls_back_to_its_affinity(self):
        held = {"why": "", "enforced": ["cpus", "processes"], "refused": {"cpu_set": "cat: no such file"}}
        with mock.patch.object(constraints, "scope_check", return_value=held):
            self.assertEqual(run.Run.scope_ready({"cpus": 2, "processes": 64, "cpu_set": "0"}), {"cpus": 2, "processes": 64, "cpu_set": "0"})
        dropped = dict(held, enforced=["cpus"], refused={"processes": "max"})
        with mock.patch.object(constraints, "scope_check", return_value=dropped), \
                self.assertRaisesRegex(run.Refused, r"systemd does not enforce on this machine's processes Processes and threads \(Asked 64 processes, the scope had no limit\)"):
            run.Run.scope_ready({"cpus": 2, "processes": 64})
        none = {"why": "lingering is off: limits would end with the SSH session", "enforced": [], "refused": {}}
        with mock.patch.object(constraints, "scope_check", return_value=none):
            with self.assertRaisesRegex(run.Refused, r"CPUs \(lingering is off"):
                run.Run.scope_ready({"cpus": 2})
            self.assertEqual(run.Run.scope_ready({"cpu_set": "0"}), {})                 # the affinity alone: nothing wrapped
            self.assertEqual(run.Run.scope_ready({"cpus": 2}, refuse=False), {})        # the container backend: the driver unlimited, the run goes on
        with mock.patch.object(constraints, "scope_check", return_value=dropped):
            self.assertEqual(run.Run.scope_ready({"cpus": 2, "processes": 64}, refuse=False), {"cpus": 2, "processes": 64})   # what holds, holds
        self.assertEqual(run.Run.scope_ready({}), {})

    def test_what_settings_and_review_say_of_a_scope(self):
        scope = lambda **sd: {"capacity": {"cpus": 4}, "engine": {}, "enforced": [], "refused": {},
                              "systemd": {"why": "", "enforced": [], "refused": {}, **sd}}
        ok, off = scope(enforced=["cpus", "memory_mb", "processes"]), scope(why="no cgroup v2")
        self.assertTrue(constraints.scope_ok(ok)); self.assertFalse(constraints.scope_ok(off)); self.assertFalse(constraints.scope_ok({}))
        values = {"cpus": 2, "processes": 64, "cpu_set": "0"}
        self.assertEqual(constraints.not_enforced(values, ok), {})                      # no engine: a local run holds them
        self.assertEqual(list(constraints.not_enforced(values, off)), ["cpus", "processes"])  # cpu_set: the affinity
        self.assertEqual(constraints.warnings(values, ok), {})
        self.assertEqual(constraints.warnings(values, off), {})                         # no engine, no scope: off in Settings
        engine = dict(off, engine={"kind": "docker", "version": "29"}, enforced=["cpus", "processes", "cpu_set"])
        rows = lambda found, backend="local", own=False: Review().rows(values, backend=backend, found=found, own=own)["constraints"]
        self.assertEqual(rows(ok)["state"], "ok")
        self.assertEqual(rows(off)["detail"], "systemd does not enforce CPUs, Processes and threads here (no cgroup v2): test it in Settings")
        self.assertEqual(rows(engine, "container")["state"], "ok")                      # the container backend: its drivers run unlimited
        self.assertEqual(rows(engine)["state"], "missing")                              # the local backend: its agents need the scope
        self.assertEqual(rows({"capacity": {"cpus": 4}, "enforced": ["cpus", "processes", "cpu_set"]}, "container")["state"], "ok")   # scope never tested
        self.assertEqual(rows(dict(ok, engine={"kind": "docker"}, enforced=["processes"]), own=True)["detail"],   # own images, local backend
                         "this machine's engine does not enforce CPUs, CPUs Nullius may use: test it in Settings")
        # one function: Settings marks what Review refuses, on either backend
        self.assertEqual(constraints.not_enforced(values, engine), {"cpus": "systemd", "processes": "systemd"})
        self.assertEqual(constraints.warnings(values, engine)["cpus"], "Local processes here do not enforce it: run in a container, or remove it.")


class Cli(unittest.TestCase):
    def refused(self, *words):
        app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-m", "nullius.standard.cli", "run", "humaneval", "nop", *words],
                           capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=app), timeout=120)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        return r.stdout

    def test_a_constraint_the_machine_cannot_take_refuses_the_run(self):
        self.assertIn("refused: constraints: Processes and threads: a whole number of at least 1",
                      self.refused("--constraint", "processes=0"))
        if hasattr(os, "sched_setaffinity"):
            self.assertIn("refused: constraints: cpu_set:", self.refused("--constraint", "cpu_set=8191"))


class Compile(unittest.TestCase):
    def test_each_machine_s_constraints_go_on_its_command(self):
        defs = {k: definitions.load(k, ROOT)[0] for k in ("benchmark", "agent", "capability")}
        r = {"version": 1, "harness": "standard", "name": "fake-0101-120000", "title": "t", "benchmark": "fakebench",
             "dataset": "all", "other": "fake", "provider": "openai", "model": "gpt-5-mini", "concurrency": 4,
             "unsandboxed": True, "where": ["local", "me@bench"]}
        with mock.patch.dict(os.environ, {constraints.SETTING: '{"me@bench": {"cpus": 2, "gpu_ids": []}}'}):
            out = recipe.compile(r, defs)
        here, there = (sh["command"] for sh in out["shards"])
        self.assertNotIn("--constraint", here)
        self.assertIn("--constraint cpus=2 --constraint gpu_ids= --from-run", there)
        with mock.patch.dict(os.environ, {constraints.SETTING: '{"me@bench": {"cpus": 0}}'}):
            said = [e["message"] for e in recipe.compile(r, defs)["errors"]]
        self.assertTrue(any(m.startswith("HOST_CONSTRAINTS must be valid constraints") for m in said), said)   # never run without them


class Review(unittest.TestCase):
    GPU = {"cuda_gpu": {"summary": "a GPU", "check": ["nvidia-smi"]}}

    def rows(self, values, runner="standard", backend="container", found=None, setups=None, where="local", own=False):
        with mock.patch.dict(os.environ, {constraints.SETTING: json.dumps({where: values})}):
            return {r["name"]: r for r in prerequisites.constraint_rows(where, runner, backend, setups, found=found or {}, own=own)}

    def test_rows(self):
        self.assertEqual(self.rows({}), {})
        tested = {"capacity": {"cpus": 4}, "engine": {"kind": "docker"}}
        self.assertEqual(self.rows({"cpus": 2}, found=dict(tested, enforced=["cpus"]))["constraints"]["state"], "ok")
        lost = self.rows({"cpus": 2}, found=dict(tested, enforced=[]))["constraints"]
        self.assertEqual((lost["state"], lost["detail"]), ("missing", "this machine's engine does not enforce CPUs: test it in Settings"))
        self.assertEqual(self.rows({"cpus": 2}, backend="local", found=dict(tested, enforced=[]))["constraints"]["state"], "ok")
        self.assertEqual(self.rows({"cpus": 2}, found={"test": {"ok": False}, "enforced": []})["constraints"]["state"], "ok")   # not reached: not measured
        with mock.patch.dict(os.environ, {constraints.SETTING: '{"local": {"cpus": 0}}'}):
            bad = prerequisites.constraint_rows("local", "standard", "local")
            self.assertEqual((bad[0]["state"], bad[0]["detail"][:40]), ("missing", "HOST_CONSTRAINTS must be valid constrain"))
            self.assertEqual(prerequisites.constraint_rows("local", "harbor", "local"), [])     # Harbor takes none
        self.assertEqual(self.rows({"cpus": 2}, runner="harbor")["constraints"]["detail"], "not applied to Harbor runs")
        self.assertEqual(self.rows({"gpu_ids": []}, setups=self.GPU, where="bench")["gpu"]["state"], "missing")
        self.assertNotIn("gpu", self.rows({"gpu_ids": [0]}, setups=self.GPU, where="bench"))
        self.assertNotIn("gpu", self.rows({"gpu_ids": []}, setups=self.GPU))        # here, the setup's own row says it (hostreq)
        with mock.patch.dict(os.environ, {constraints.SETTING: '{"local": {"gpu_ids": []}}'}):
            here = {r["name"]: r for r in prerequisites.check_here([], self.GPU, which=lambda p: True)}
        self.assertEqual((here["setup:cuda_gpu"]["state"], here["setup:cuda_gpu"]["detail"]), ("missing", "this machine allows Nullius no GPU"))


if __name__ == "__main__":
    unittest.main()
