"""An environment that is each task's own image: its schema, the setup's
image and steps, the tag from what is built, where an agent's start command
finds its runtime, the ``docker exec`` that carries no secret - read without
docker - and a run of the fixture (``fixtures/standard/ownbench``: one task
in its own image, checked there), which needs a usable docker, the base image
and ``ubuntu:24.04`` already here, and is skipped otherwise."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from nullius.runs import classify
from tests import docker_tier
from nullius.standard import container, definitions, install, run

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "ownbench")


def bench() -> dict:
    d, bad = definitions._read(os.path.join(HERE, "standard.json"), "benchmark", "ownbench")
    assert not bad, bad
    return definitions.resolve(d)


def worker() -> dict:
    with open(os.path.join(HERE, "agent", "agent.json")) as f:
        return dict(json.load(f), _here=os.path.join(HERE, "agent"), _kind="agent")


class Schema(unittest.TestCase):
    def problems(self, envs, family="agent"):
        d = dict(bench(), environments=envs, family=family)
        return definitions.problems(d, "benchmark")

    def test_the_fixture_is_valid_and_its_environment_is_an_own_image(self):
        self.assertEqual(self.problems(bench()["environments"]), [])
        self.assertTrue(definitions.own_image(bench()["environments"]["task"]))
        self.assertFalse(definitions.own_image({"summary": "s", "dockerfile": "x"}))

    def test_an_own_image_has_no_dockerfile_here_and_no_local_run(self):
        own = {"summary": "s", "image": "own"}
        self.assertTrue(any(".dockerfile: not with an own image" in p for p in self.problems({"task": dict(own, dockerfile="D")})))
        self.assertTrue(any(".local_ok: an own image runs only in a container" in p for p in self.problems({"task": dict(own, local_ok=True)})))
        self.assertTrue(any(".image: 'own'" in p for p in self.problems({"task": dict(own, image="theirs")})))
        self.assertTrue(any("a capability benchmark has none" in p for p in self.problems({"task": own}, family="capability")))

    def test_its_image_is_the_units_to_name(self):
        with self.assertRaises(ValueError):
            container.image_for(bench(), {"environment": "task"})


class Setup(unittest.TestCase):
    def test_an_image_is_built_from_a_folder_or_pulled_by_digest(self):
        self.assertEqual(container.spec_problems({"build": "tasks/one/environment", "memory_mb": 2048, "cpus": 1}), [])
        self.assertEqual(container.spec_problems({"pull": "docker.io/x/y@sha256:" + "a" * 64}), [])
        self.assertTrue(any("not pinned by digest" in p for p in container.spec_problems({"pull": "x/y:latest"})))
        self.assertTrue(any("one of build" in p for p in container.spec_problems({})))
        self.assertTrue(any("one of build" in p for p in container.spec_problems({"build": "a", "pull": "b"})))
        self.assertTrue(any("memory_mb" in p for p in container.spec_problems({"build": "a", "memory_mb": "lots"})))

    def test_a_step_names_its_files_command_and_results(self):
        step = {"copy": {"t": "/tests"}, "run": ["sh", "/tests/x.sh"], "collect": "/logs", "seconds": 5, "network": True}
        self.assertEqual(container.step_problems(step, collect=True), [])
        self.assertTrue(any("collect: not a field" in p for p in container.step_problems(step, collect=False)))
        self.assertTrue(any("run: the command" in p for p in container.step_problems(dict(step, run="sh x"), collect=True)))
        self.assertTrue(any("copy:" in p for p in container.step_problems(dict(step, copy={"t": "tests"}), collect=True)))
        # a file the driver has by its text (an eval script, a patch the dataset carries): written in before run
        self.assertEqual(container.step_problems(dict(step, write={"/eval.sh": "#!/bin/bash\n"}), collect=True), [])
        self.assertTrue(any("write:" in p for p in container.step_problems(dict(step, write={"eval.sh": "x"}), collect=True)))
        self.assertTrue(any("write:" in p for p in container.step_problems(dict(step, write={"/eval.sh": 3}), collect=True)))

    def test_a_driver_names_the_tasks_files_and_nothing_else(self):
        self.assertTrue(container.within(HERE, "tasks/one/tests").endswith("tasks/one/tests"))
        for bad in ("../ownbench/../../..", "/etc", "tasks/../../envbench", "tasks/none"):
            with self.assertRaises(ValueError):
                container.within(HERE, bad)

    def test_the_tag_is_the_hash_of_the_folder_built(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ctx = os.path.join(tmp, "task-a", "environment")
        shutil.copytree(os.path.join(HERE, "tasks", "one", "environment"), ctx)
        b = bench()
        a = container.own_tag(b, "task-a/environment", ctx)
        self.assertRegex(a, r"^nullius/own-ownbench-task-a-environment:[0-9a-f]{12}$")
        self.assertEqual(a, container.own_tag(b, "task-a/environment", ctx))
        with open(os.path.join(ctx, "extra.txt"), "w") as f:
            f.write("x")
        self.assertNotEqual(a, container.own_tag(b, "task-a/environment", ctx))


class Runtime(unittest.TestCase):
    RT = {"node": ("/host/node", "/opt/nullius/node"), "python": ("/host/py", "/opt/nullius/python")}

    def test_the_agent_runs_on_its_runtime_and_the_task_keeps_its_own(self):
        self.assertEqual(container.in_runtime(["python3", "/x/agent.py"], self.RT), ["/opt/nullius/python/bin/python3", "/x/agent.py"])
        self.assertEqual(container.in_runtime(["sh", "/x/run.sh"], self.RT), ["sh", "/x/run.sh"])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        script = os.path.join(tmp, "agent-acp")
        with open(script, "w") as f:
            f.write("#!/usr/bin/env node\nconsole.log(1)\n")
        self.assertEqual(container.in_runtime([script, "--flag"], self.RT), ["/opt/nullius/node/bin/node", script, "--flag"])
        self.assertEqual(container.in_runtime([script], {}), [script])        # no runtime asked for: as it is

    def test_what_an_agent_needs_follows_its_install(self):
        r = run.Run.__new__(run.Run)
        r.inst = ""
        r.other = {"install": {"npm": ["a@1"]}, "start": {"acp": ["{install}/node_modules/.bin/a"]}}
        self.assertEqual(r.runtimes_needed(), ["node"])
        r.other = {"start": {"acp": ["python3", "{here}/agent.py"]}}
        self.assertEqual(r.runtimes_needed(), ["python"])
        r.other = {"start": {"command": ["sh", "{here}/x.sh", "{prompt}"]}}
        self.assertEqual(r.runtimes_needed(), [])
        # mini-swe-agent, terminus-2: their environment's own interpreter, whatever it links to
        r.other = {"start": {"command": ["{install}/.venv/bin/python", "{here}/run.py", "{prompt}"]}}
        self.assertEqual(r.runtimes_needed(), [])

    def test_an_environments_interpreter_is_never_swapped_for_the_runtime(self):
        venv_py = "/inst/mini-swe-agent/.venv/bin/python"
        self.assertEqual(container.in_runtime([venv_py, "/x/run.py"], self.RT), [venv_py, "/x/run.py"])

    def test_exec_passes_variables_by_name_and_the_runtimes_read_only(self):
        b = container.Box("job", "u0001", "img", workdir="/app", runtimes=self.RT)
        argv = b.exec_argv(["node", "x"], {"HOME": "/h", "NULLIUS_RELAY_KEY": "placeholder", "PATH": "/p"}, "/usr/bin:/opt/nullius/node/bin")
        self.assertEqual(argv[:6], ["docker", "exec", "-i", "-w", "/app", "-e"])
        self.assertIn("PATH=/usr/bin:/opt/nullius/node/bin", argv)
        self.assertIn("NULLIUS_RELAY_KEY", argv)
        self.assertNotIn("placeholder", " ".join(argv))
        self.assertEqual(argv[-3:], [b.name, "node", "x"])


def _docker_ready() -> str:
    return docker_tier.why_not("ubuntu:24.04")


class LocalRun(unittest.TestCase):
    def test_a_check_without_an_own_image_is_refused_and_an_agent_needs_docker(self):
        tmp = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.addCleanup(setattr, install, "HOME", install.HOME)
        install.HOME = os.path.join(tmp, "home")
        env = {"PATH": os.environ.get("PATH", os.defpath), "MODEL_API_URL": "http://127.0.0.1:9/v1", "MODEL_API_KEY": "sk-test-not-real"}
        job_dir = run.run(bench(), worker(), model="m", provider="custom", jobs_root=tmp, env=env, say=lambda s: None,
                          unsandboxed=True, backend="local")
        got = {}
        for name in os.listdir(job_dir):
            p = os.path.join(job_dir, name, "result.json")
            if os.path.isfile(p):
                with open(p) as f:
                    r = json.load(f)
                got[r["standard"]["unit"]] = r["exception_info"]
        self.assertIn("needs the docker backend", got["one"]["exception_message"])
        self.assertIn("verify runs in an own image", got["base"]["exception_message"])
        self.assertEqual(classify.from_verdict(got["base"]["exception_type"], None), classify.SETUP_ERROR)


@unittest.skipIf(_docker_ready(), _docker_ready() or "")
class DockerRun(unittest.TestCase):
    """The fixture's task in its own image: the agent works in the image's
    working directory without the check, which then finds its work there; the
    controls run in it too, the reference solution passing and nothing failing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, install, "HOME", install.HOME)
        install.HOME = os.path.join(self.tmp, "home")
        self.env ={"PATH": os.environ.get("PATH", os.defpath), "MODEL_API_URL": "http://127.0.0.1:9/v1",
                    "MODEL_API_KEY": "sk-test-not-real"}

    def trial(self, other, backend):
        job_dir = run.run(bench(), other, model="m", provider="custom", jobs_root=self.tmp, env=self.env, say=lambda s: None,
                          backend=backend, limit=1)
        name = next(n for n in os.listdir(job_dir) if n.startswith("one__"))
        with open(os.path.join(job_dir, name, "result.json")) as f:
            return os.path.join(job_dir, name), json.load(f)

    @classmethod
    def tearDownClass(cls):
        # the fixture's image goes, and so does this process's memory of having built it
        for line in subprocess.run(["docker", "images", "-q", "--filter", "label=nullius.own=ownbench"],
                                   stdout=subprocess.PIPE, text=True).stdout.split():
            subprocess.run(["docker", "rmi", "-f", line], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for tag in [t for t in container._built if t.startswith("nullius/own-ownbench-")]:
            container._built.pop(tag)
            container._fetched.discard(tag)

    def test_the_agent_works_in_the_tasks_container_and_is_checked_there(self):
        where, r = self.trial(worker(), "docker")
        self.assertEqual(r["verifier_result"]["rewards"]["reward"], 1.0, r.get("exception_info"))
        self.assertRegex(r["standard"]["image"], r"^nullius/own-ownbench-tasks-one-environment:[0-9a-f]{12}$")
        with open(os.path.join(where, "verifier", "test-stdout.txt")) as f:
            said = f.read()
        self.assertIn("checked in /work", said)
        self.assertIn("proxy=http://egress:3128", said)          # its way out is the proxy, named to it alone
        with open(os.path.join(where, "verifier", "stopped.txt")) as f:   # the agent's loop, stopped before the check
            self.assertIn("sh", f.read())
        with open(os.path.join(where, "agent", "container-changes.txt")) as f:
            changes = f.read()
        self.assertIn("A /work/done.txt", changes)
        self.assertNotIn("/tests", changes)                    # taken before the check is copied in
        with open(os.path.join(where, ".home", "where.txt")) as f:       # its home, written as the image's user, is the user's again
            self.assertTrue(f.read().startswith("home="))
        self.assertEqual(os.stat(os.path.join(where, ".home", "where.txt")).st_uid, os.getuid())

    def test_the_controls_run_in_it_without_a_model(self):
        b = bench()
        _, r = self.trial(definitions.control(b, "oracle"), "local")
        self.assertEqual(r["verifier_result"]["rewards"]["reward"], 1.0, r.get("exception_info"))
        _, r = self.trial(definitions.control(b, "nop"), "local")
        self.assertEqual(r["verifier_result"]["rewards"]["reward"], 0.0, r.get("exception_info"))


if __name__ == "__main__":
    unittest.main()


class ImagesKept(unittest.TestCase):
    """A run's images stay for the next run (another agent on the same tasks);
    removed only when asked (SWE-bench Verified: about 4 GB per unit)."""

    def run_of(self, remove=True, repeat=1):
        r = run.Run.__new__(run.Run)
        r._said, r._image_users, r.keep_images = threading.Lock(), {}, not remove or repeat > 1
        return r

    def test_an_image_this_run_fetched_goes_when_its_last_unit_ends_if_asked(self):
        dropped = []
        r = self.run_of()
        r._image_users = {"img:1": 2, "theirs:1": 1}
        with mock.patch.object(container, "fetched", lambda image: image == "img:1"), \
                mock.patch.object(container, "drop_image", dropped.append):
            r.release_image("img:1")
            self.assertEqual(dropped, [])                         # another unit is still in it
            r.release_image("img:1")
            r.release_image("theirs:1")                           # there before the run: the user's
        self.assertEqual(dropped, ["img:1"])

    def test_an_image_found_present_is_never_counted_as_fetched(self):
        """present() remembers every image it finds; fetched() read that, so
        a task image already on the machine was removed after its last unit
        (alexgshaw/cancel-async-tasks, on a Terminal-Bench oracle run)."""
        calls, have = [], {"theirs@sha256:1"}

        def docker(args, **kw):
            calls.append(args[:2])
            if args[:2] == ["image", "inspect"]:
                return mock.Mock(returncode=0 if args[2] in have else 1, stdout="", stderr="")
            if args[0] == "pull":
                have.add(args[1])
            return mock.Mock(returncode=0, stdout="", stderr="")
        self.addCleanup(container._fetched.difference_update, {"theirs@sha256:1", "pulled@sha256:2"})
        self.addCleanup(lambda: [container._built.pop(t, None) for t in ("theirs@sha256:1", "pulled@sha256:2")])
        with mock.patch.object(container, "_docker", docker), mock.patch.object(container, "_rootless", None):
            self.assertEqual(container.own_image({"key": "b"}, {"pull": "theirs@sha256:1"}, "/r"), "theirs@sha256:1")
            self.assertEqual(container.own_image({"key": "b"}, {"pull": "pulled@sha256:2"}, "/r"), "pulled@sha256:2")
            self.assertFalse(container.fetched("theirs@sha256:1"))
            self.assertTrue(container.fetched("pulled@sha256:2"))
            container.drop_image("theirs@sha256:1")
            container.drop_image("pulled@sha256:2")
        self.assertEqual([c for c in calls if c[0] == "rmi"], [["rmi", "pulled@sha256:2"]])

    def test_a_run_keeps_its_images_unless_asked_and_always_with_a_repeat(self):
        for r in (self.run_of(remove=False), self.run_of(repeat=3)):
            r._image_users = {"img:1": 1}
            with mock.patch.object(container, "fetched", lambda image: True), \
                    mock.patch.object(container, "drop_image", lambda image: self.fail("dropped")):
                r.release_image("img:1")
