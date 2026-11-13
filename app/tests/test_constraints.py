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



if __name__ == "__main__":
    unittest.main()
