"""A benchmark's per-unit environments: the schema of ``environments``, the
image's name from what it is built of, which unit gets which, the licences'
three shapes, and a run of the fixture benchmark
(``fixtures/standard/envbench``: a tool, licences - a key, a file, a server -,
nothing) with its licences in its own store - on the local backend, where
only what may run here runs, and on docker, where each unit sees its own
environment, its licence file mounted where declared and its licence server
the one address it reaches. The docker run is skipped when docker is not
usable or the base image is not built; it builds the two small
environments, once."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from nullius import secrets, settings
from nullius.runs import classify, parse
from nullius.standard import container, definitions, install, run
from tests import docker_tier

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "envbench")
SECRETS = {"FAKE_TOOL_LICENSE": "lic-0123456789-not-real", "FAKE_SCORER_TOKEN": "tok-9876543210-not-real"}
LIC_FILE = b"FAKE-LICENCE-FILE 0123456789\nnot a real licence\n"


def _load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def bench(root: str = HERE) -> dict:
    d, bad = definitions._read(os.path.join(root, "standard.json"), "benchmark", "envbench")
    assert not bad, bad
    return definitions.resolve(d)


def probe_agent() -> dict:
    with open(os.path.join(HERE, "agent", "agent.json")) as f:
        return dict(json.load(f), _here=os.path.join(HERE, "agent"), _kind="agent")


class Schema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "envbench")
        shutil.copytree(HERE, self.root)

    def problems(self, envs=None, dockerfile=None, **top):
        d = _load(os.path.join(self.root, "standard.json"))
        if envs is not None:
            d["environments"] = envs
        d.update(top)
        if dockerfile is not None:
            with open(os.path.join(self.root, "env", "tool.Dockerfile"), "w") as f:
                f.write(dockerfile)
        return definitions.problems(dict(d, _here=self.root), "benchmark")

    def test_the_fixture_is_valid(self):
        self.assertEqual(self.problems(), [])

    def test_the_dockerfile_is_a_layer_on_the_base(self):
        self.assertTrue(any("no FROM" in p for p in self.problems(dockerfile="FROM ubuntu:24.04\nRUN true\n")))
        self.assertTrue(any("COPY, not ADD" in p for p in self.problems(dockerfile="ADD https://x/y /y\n")))
        self.assertTrue(any("COPY secret.txt, which is not in files" in p for p in self.problems(dockerfile="COPY secret.txt /x\n")))
        self.assertTrue(any("--from" in p for p in self.problems(dockerfile="COPY --from=other /a /b\n")))
        self.assertEqual(self.problems(dockerfile="# no from\nRUN apt-get update \\\n && true\n"), [])

    def test_each_wrong_field_is_named(self):
        tool = {"summary": "s", "dockerfile": "env/tool.Dockerfile"}
        self.assertTrue(any("not a name" in p for p in self.problems({"base": tool})))
        self.assertTrue(any("not in the benchmark's folder" in p for p in self.problems({"x": dict(tool, dockerfile="env/none")})))
        self.assertTrue(any(".files: nope is not" in p for p in self.problems({"x": dict(tool, files=["nope"])})))
        self.assertTrue(any(".summary" in p for p in self.problems({"x": dict(tool, summary="")})))
        self.assertTrue(any(".local_ok" in p for p in self.problems({"x": dict(tool, local_ok="yes")})))
        self.assertTrue(any("is not a field of an environment" in p for p in self.problems({"x": dict(tool, base="x")})))
        self.assertTrue(any(".image: 'own'" in p for p in self.problems({"x": dict(tool, image="x")})))
        said = self.problems({"x": dict(tool, needs_secrets=[{"name": "LIC"}])})
        self.assertTrue(any(".to: one of ('agent', 'driver')" in p for p in said), said)
        said = self.problems({"x": dict(tool, needs_secrets=[{"name": "OPENAI_API_KEY", "to": "agent"}])})
        self.assertTrue(any("already the name of something else" in p for p in said), said)
        said = self.problems({"x": dict(tool, needs_secrets=[{"name": "A", "to": "agent"}, {"name": "A", "to": "driver"}])})
        self.assertTrue(any("each once" in p for p in said), said)

    def test_a_datasets_default_must_be_declared(self):
        said = self.problems(datasets=[{"id": "all", "units": 3, "environment": "gpu"}])
        self.assertIn("datasets[0].environment: one of environments (tool, licensed)", said)
        self.assertEqual(self.problems(datasets=[{"id": "all", "units": 3, "environment": "tool"}]), [])


class Tag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "envbench")
        shutil.copytree(HERE, self.root)

    def test_the_tag_is_the_hash_of_what_is_built(self):
        b = bench(self.root)
        tag = container.env_tag(b, "licensed", "nullius/base:aaa")
        self.assertRegex(tag, r"^nullius/env-envbench-licensed:[0-9a-f]{12}$")
        self.assertEqual(tag, container.env_tag(bench(self.root), "licensed", "nullius/base:aaa"))
        self.assertNotEqual(tag, container.env_tag(b, "licensed", "nullius/base:bbb"))      # a new base, a new layer
        with open(os.path.join(self.root, "env", "marker.txt"), "a") as f:
            f.write("changed\n")
        self.assertNotEqual(tag, container.env_tag(bench(self.root), "licensed", "nullius/base:aaa"))
        text, files = container.env_context(b, "licensed", "nullius/base:aaa")
        self.assertTrue(text.startswith("FROM nullius/base:aaa\n"))
        self.assertEqual(list(files), ["env/marker.txt"])                     # its context: the file it lists, nothing else

    def test_an_environment_is_built_once_and_only_when_asked(self):
        b = bench(self.root)
        calls = []

        def docker(args, **kw):
            calls.append(args)
            ok = args[:2] != ["image", "inspect"] or any(c[0] == "build" for c in calls)
            return subprocess.CompletedProcess(args, 0 if ok else 1, "built\n", "")

        container._built.clear()
        with mock.patch.object(container, "base_image", return_value="nullius/base:aaa"), \
                mock.patch.object(container, "_docker", side_effect=docker):
            work = os.path.join(self.tmp, "work")
            t1 = container.image_for(b, {"environment": "licensed"}, None, work)
            t2 = container.image_for(b, {"environment": "licensed"}, None, work)
        self.assertEqual(t1, t2)
        builds = [c for c in calls if c[0] == "build"]
        self.assertEqual(len(builds), 1)
        self.assertIn("nullius.env=envbench/licensed", builds[0])
        self.assertNotIn("FAKE_TOOL_LICENSE", " ".join(builds[0]))             # no secret reaches a build
        with open(os.path.join(work, "env-licensed.log")) as f:
            self.assertIn(t1, f.read())
        self.assertFalse(os.path.exists(os.path.join(work, "env-tool.log")))   # lazy: tool was never asked for
        container._built.clear()


class Choice(unittest.TestCase):
    def test_setup_then_the_dataset_then_the_base(self):
        b = bench()
        ds = {"id": "all", "environment": "tool"}
        self.assertEqual(definitions.environment_of(b, {"environment": "licensed"}, ds)[0], "licensed")
        self.assertEqual(definitions.environment_of(b, {}, ds)[0], "tool")
        self.assertEqual(definitions.environment_of(b, {}, {"id": "all"}), (None, None))
        self.assertEqual(definitions.environment_of(b, {"environment": "base"}, ds), (None, None))
        with self.assertRaises(ValueError):
            definitions.environment_of(b, {"environment": "gpu"}, ds)

    def test_licences_by_where_they_go_and_their_shape(self):
        env = bench()["environments"]["licensed"]
        held = {"FAKE_TOOL_LICENSE": {"kind": "value", "value": "k"}, "FAKE_SCORER_TOKEN": {"kind": "value", "value": "t"},
                "FAKE_TOOL_LICENSE_FILE": {"kind": "file", "path": "/kept/here.lic"},
                "FAKE_LICENSE_SERVER": {"kind": "server", "value": "lic.example.com:27000"}}
        self.assertEqual(definitions.missing_secrets(env, {}), ["FAKE_TOOL_LICENSE", "FAKE_TOOL_LICENSE_FILE", "FAKE_LICENSE_SERVER", "FAKE_SCORER_TOKEN"])
        self.assertEqual(definitions.missing_secrets(env, held), [])
        self.assertEqual(definitions.secret_values(env, held, "agent"),
                         {"FAKE_TOOL_LICENSE": "k", "FAKE_TOOL_LICENSE_PATH": "/kept/here.lic", "FAKE_LICENSE_SERVER": "lic.example.com:27000"})
        self.assertEqual(definitions.secret_values(env, held, "agent", inside=True)["FAKE_TOOL_LICENSE_PATH"], "/opt/licensed-tool/license.lic")
        self.assertEqual(definitions.secret_values(env, held, "driver"), {"FAKE_SCORER_TOKEN": "t"})
        self.assertEqual([(s["name"], s["kind"]) for s in definitions.secrets_of(bench())],
                         [("FAKE_TOOL_LICENSE", "value"), ("FAKE_TOOL_LICENSE_FILE", "file"), ("FAKE_LICENSE_SERVER", "server"),
                          ("FAKE_SCORER_TOKEN", "value")])

    def test_the_image_for_a_unit_without_one_is_the_base(self):
        with mock.patch.object(container, "base_image", return_value="nullius/base:x") as base, \
                mock.patch.object(container, "env_image", return_value="nullius/env-envbench-tool:y") as env:
            self.assertEqual(container.image_for(bench(), {}), "nullius/base:x")
            self.assertEqual(container.image_for(bench(), {"environment": "tool"}), "nullius/env-envbench-tool:y")
            env.assert_called_once()
            self.assertEqual(env.call_args[0][1], "tool")
        self.assertTrue(base.called)


class Shapes(unittest.TestCase):
    """``needs_secrets``' three shapes, checked by ``definitions.problems``."""

    def problems(self, *secrets_, local_ok=False):
        d = _load(os.path.join(HERE, "standard.json"))
        d["environments"]["licensed"]["needs_secrets"] = list(secrets_)
        d["environments"]["licensed"]["local_ok"] = local_ok
        return [p for p in definitions.problems(dict(d, _here=HERE), "benchmark") if "needs_secrets" in p or "one kind" in p]

    def test_each_shape_as_it_is_declared(self):
        ok = [{"name": "K", "to": "agent"}, {"name": "K2", "kind": "value", "to": "driver"},
              {"name": "F", "kind": "file", "to": "agent", "path": "/opt/tool/license.lic"},
              {"name": "F2", "kind": "file", "to": "driver", "env": "F2_PATH"},
              {"name": "S", "kind": "server", "to": "agent", "summary": "the licence server"}]
        self.assertEqual(self.problems(*ok), [])

    def test_what_each_shape_refuses(self):
        cases = [
            ({"name": "K", "kind": "secret", "to": "agent"}, ".kind: one of"),
            ({"name": "K", "to": "agent", "path": "/x"}, "path and env are a file's"),
            ({"name": "S", "kind": "server", "to": "agent", "env": "S_ADDR"}, "path and env are a file's"),
            ({"name": "F", "kind": "file", "to": "agent"}, ".path: an absolute path"),
            ({"name": "F", "kind": "file", "to": "agent", "path": "relative/x.lic"}, ".path: an absolute path"),
            ({"name": "F", "kind": "file", "to": "agent", "path": "/opt/../etc/x"}, ".path: an absolute path"),
            ({"name": "F", "kind": "file", "to": "agent", "path": "/proc/self/x"}, ".path: an absolute path"),
            ({"name": "F", "kind": "file", "to": "driver", "path": "/x.lic", "env": "F_PATH"}, "the driver runs on the machine"),
            ({"name": "F", "kind": "file", "to": "driver"}, ".env: the variable the driver finds the file by"),
            ({"name": "F", "kind": "file", "to": "agent", "path": "/x.lic", "env": "OPENAI_API_KEY"}, ".env: a variable name of its own"),
            ({"name": "F", "kind": "file", "to": "agent", "path": "/x.lic", "extra": 1}, "(name and to always)"),
        ]
        for s, said in cases:
            with self.subTest(s=s):
                got = self.problems(s)
                self.assertTrue(any(said in p for p in got), got)

    def test_a_file_on_local_processes_needs_its_variable_and_two_files_two_paths(self):
        f = {"name": "F", "kind": "file", "to": "agent", "path": "/x.lic"}
        self.assertTrue(any("on local processes" in p for p in self.problems(f, local_ok=True)))
        self.assertEqual(self.problems(dict(f, env="F_PATH"), local_ok=True), [])
        said = self.problems(f, dict(f, name="G"))
        self.assertTrue(any("/x.lic is another licence's" in p for p in said), said)

    def test_one_name_is_one_kind_across_environments(self):
        d = _load(os.path.join(HERE, "standard.json"))
        d["environments"]["tool"]["needs_secrets"] = [{"name": "FAKE_TOOL_LICENSE", "kind": "server", "to": "agent"}]
        said = definitions.problems(dict(d, _here=HERE), "benchmark")
        self.assertTrue(any("one name, one kind" in p for p in said), said)


class Withheld(unittest.TestCase):
    def test_a_licence_name_is_never_inherited_by_another_run(self):
        # licences live in their benchmark's store, never in Settings; a line an older app/.env still holds is
        # withheld from every run until it is moved
        from nullius import execution
        with mock.patch.object(definitions, "load", return_value=([bench()], [])):
            self.assertIn("FAKE_TOOL_LICENSE_FILE", execution.withheld())
        self.assertNotIn("Benchmark secrets", settings.GROUPS)


class Handed(unittest.TestCase):
    def test_a_hosts_licence_file_is_written_private_and_the_rest_kept_in_memory(self):
        spill = os.path.join(tempfile.mkdtemp(), "nullius-run")
        self.addCleanup(shutil.rmtree, os.path.dirname(spill), True)
        held = run.Licences.handed(bench(), {"FAKE_TOOL_LICENSE_FILE": b"FAKE-LICENCE-FILE\n", "FAKE_TOOL_LICENSE": b"k-1\n",
                                             "NOT_DECLARED": b"x"}, spill)
        f = held.get("FAKE_TOOL_LICENSE_FILE")
        self.assertEqual(f["path"], os.path.join(spill, "FAKE_TOOL_LICENSE_FILE"))
        self.assertEqual(oct(os.stat(f["path"]).st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(os.stat(spill).st_mode & 0o777), oct(0o700))
        self.assertEqual(held.get("FAKE_TOOL_LICENSE"), {"kind": "value", "value": "k-1"})
        self.assertIsNone(held.get("NOT_DECLARED"))
        self.assertEqual(os.listdir(spill), ["FAKE_TOOL_LICENSE_FILE"])


class LicenceServer:
    """A dummy licence server: greets every connection with LICENSE-OK."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.hits += 1
            with conn:
                conn.sendall(b"LICENSE-OK\n")

    def close(self):
        self.sock.close()


class _Runs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.addCleanup(setattr, install, "HOME", self._home)
        store = mock.patch.dict(os.environ, {"SECRETS_DIR": os.path.join(self.tmp, "secrets")})
        store.start()
        self.addCleanup(store.stop)
        self.server = LicenceServer()
        self.addCleanup(self.server.close)

    def store(self, server_host="127.0.0.1"):
        """Every licence of the fixture, in its benchmark's store."""
        for n, v in SECRETS.items():
            secrets.set("envbench", n, v)
        secrets.set("envbench", "FAKE_TOOL_LICENSE_FILE", LIC_FILE, kind="file", filename="tool.lic")
        secrets.set("envbench", "FAKE_LICENSE_SERVER", f"{server_host}:{self.server.port}", kind="server")

    def run_it(self, backend, unsandboxed=True, other=None, b=None):
        env = {"PATH": os.environ.get("PATH", os.defpath), "MODEL_API_URL": "http://127.0.0.1:9/v1", "MODEL_API_KEY": "sk-test-not-real"}
        job_dir = run.run(b or bench(), other or probe_agent(), model="m", provider="custom", jobs_root=os.path.join(self.tmp, "jobs"),
                          concurrency=3, env=env, say=lambda s: None, unsandboxed=unsandboxed, backend=backend)
        trials = {}
        for name in os.listdir(job_dir):
            p = os.path.join(job_dir, name, "result.json")
            if os.path.isfile(p):
                r = _load(p)
                score = os.path.join(job_dir, name, "verifier", "score.json")
                r["_score"] = _load(score) if os.path.isfile(score) else None
                trials[r["standard"]["unit"]] = r
        return job_dir, trials

    def assertNoSecretIn(self, job_dir):
        for root, _, files in os.walk(job_dir):
            for n in files:
                with open(os.path.join(root, n), errors="replace") as f:
                    text = f.read()
                for v in list(SECRETS.values()) + ["FAKE-LICENCE-FILE"]:
                    self.assertNotIn(v, text, os.path.join(root, n))


MISSING = "not run: needs FAKE_TOOL_LICENSE, FAKE_TOOL_LICENSE_FILE, FAKE_LICENSE_SERVER, FAKE_SCORER_TOKEN"


class LocalRun(_Runs):
    def test_without_the_licences_a_unit_is_not_run_and_charged_to_no_one(self):
        job_dir, t = self.run_it("local")
        lic = t["licensed"]["exception_info"]
        self.assertEqual(lic["exception_type"], "EnvironmentSecretMissingError")
        self.assertEqual(lic["exception_message"], MISSING)
        self.assertEqual(classify.from_verdict(lic["exception_type"], None), classify.SETUP_ERROR)
        self.assertIn(classify.SETUP_ERROR, classify.INFRA_OUTCOMES)
        tool = t["tool"]["exception_info"]
        self.assertEqual(tool["exception_type"], "EnvironmentSetupError")             # the local backend cannot give it
        self.assertIn("needs the docker backend", tool["exception_message"])
        self.assertEqual(t["tool"]["standard"]["environment"], "tool")
        self.assertEqual(t["plain"]["verifier_result"]["rewards"]["reward"], 1.0)
        self.assertNotIn("environment", t["plain"]["standard"])
        outcomes = {x.task_name.split("/")[-1]: x.outcome for x in parse.parse_job(job_dir).trials}
        self.assertEqual(outcomes["licensed"], classify.SETUP_ERROR)

    def test_with_them_each_goes_where_it_is_sent_and_nowhere_else(self):
        self.store()
        job_dir, t = self.run_it("local")
        lic = t["licensed"]
        self.assertNotIn("exception_info", lic)
        self.assertEqual(lic["verifier_result"]["rewards"]["reward"], 1.0, lic["_score"])
        detail = lic["_score"]["detail"]
        self.assertEqual((detail["secret"], detail["scorer"], detail["driver_has_scorer_token"]), ("yes", "no", True))
        self.assertEqual((detail["licfile"], detail["server"]), ("yes", "yes"))      # the file by its variable; the server reached
        plain = t["plain"]["_score"]["detail"]
        self.assertEqual((plain["secret"], plain["licfile"], plain["server"]), ("no", "no", "no"))   # another environment's stay there
        self.assertNoSecretIn(job_dir)

    def test_another_benchmarks_licences_are_not_its_own(self):
        for n, v in SECRETS.items():
            secrets.set("otherbench", n, v)
        _, t = self.run_it("local")
        self.assertEqual(t["licensed"]["exception_info"]["exception_message"], MISSING)

    def test_a_control_needs_the_licences_too_but_not_docker(self):
        b = dict(bench(), controls={"nop": {"source": "nullius", "from": "nothing"}})
        _, t = self.run_it("local", other=definitions.control(b, "nop"), b=b)
        self.assertEqual(t["licensed"]["exception_info"]["exception_type"], "EnvironmentSecretMissingError")
        self.assertNotIn("exception_info", t["tool"])                                # nothing runs in it: no docker needed


def _docker_ready() -> str:
    return docker_tier.why_not()


@unittest.skipIf(_docker_ready(), _docker_ready() or "")
class DockerRun(_Runs):
    """The fixture in containers: the tool's unit sees jq, the base unit does
    not, the licensed unit is not run without its licences and, with them,
    sees its key, its licence file where it is declared (read-only), and its
    licence server - the one address it can reach, a public one not - but
    never the driver's token."""

    def test_each_unit_runs_in_its_own_environment(self):
        job_dir, t = self.run_it("docker", unsandboxed=False)
        self.assertEqual(t["tool"]["verifier_result"]["rewards"]["reward"], 1.0, t["tool"].get("exception_info"))
        self.assertEqual(t["tool"]["_score"]["detail"]["jq"], "yes")
        self.assertRegex(t["tool"]["standard"]["image"], r"^nullius/env-envbench-tool:[0-9a-f]{12}$")
        self.assertEqual(t["plain"]["_score"]["detail"]["jq"], "no")                    # the base image has none
        self.assertTrue(t["plain"]["standard"]["image"].startswith("nullius/base:"))
        self.assertEqual(t["licensed"]["exception_info"]["exception_message"], MISSING)
        self.assertTrue(os.path.isfile(os.path.join(job_dir, ".work", "env-tool.log")) or
                        subprocess.run(["docker", "image", "inspect", t["tool"]["standard"]["image"]],
                                       stdout=subprocess.DEVNULL).returncode == 0)

        self.store(server_host="host.docker.internal")
        job_dir, t = self.run_it("docker", unsandboxed=False)
        lic = t["licensed"]
        self.assertEqual(lic["verifier_result"]["rewards"]["reward"], 1.0, lic.get("exception_info") or lic["_score"])
        d = lic["_score"]["detail"]
        self.assertEqual((d["marker"], d["scorer"]), ("yes", "no"))
        self.assertEqual((d["licfile"], d["licpath"], d["licwritable"]), ("yes", "yes", "no"))   # mounted where declared, read-only
        self.assertEqual((d["server"], d["public"]), ("yes", "no"))                             # its server, and nothing else
        self.assertGreaterEqual(self.server.hits, 1)
        self.assertEqual(t["plain"]["_score"]["detail"]["server"], "no")
        self.assertRegex(lic["standard"]["image"], r"^nullius/env-envbench-licensed:")
        self.assertNoSecretIn(job_dir)
        left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={container.LABEL}={os.path.basename(job_dir)}"],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(left, [])                                                               # the forwarder went with its unit


if __name__ == "__main__":
    unittest.main()
