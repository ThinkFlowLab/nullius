"""The boat backend (``standard/boat.py``): its API client, a unit's VM, what it is sent, and a run
whose agent goes to a VM - here a stand-in that runs it on this machine, so nothing reaches boat.dev."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

from nullius.standard import boat, definitions, recipe, run

from .test_standard_recipe import a_recipe, defs
from .test_standard_run import ROOT, Echo
from http.server import ThreadingHTTPServer
from nullius.runs import parse
from nullius.standard import install


class Answer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class FakeAPI:
    """boat.dev's API as a list of answers to POST /sandboxes, and a VM ready at once."""

    def __init__(self, creates):
        self.creates, self.calls = list(creates), []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url.split("/api/v1", 1)[1], body, dict(req.header_items())))
        method, path = self.calls[-1][:2]
        if method == "POST" and path == "/sandboxes":
            status, d = self.creates.pop(0)
            if status != 200:
                raise urllib.error.HTTPError(req.full_url, status, "no", {}, io.BytesIO(json.dumps(d).encode()))
            return Answer(d)
        if method == "GET" and path.startswith("/sandboxes/"):
            return Answer({"ok": True, "sandbox": {"state": "ready"}})
        if path.endswith("/sshkey"):
            return Answer({"ok": True, "sshEndpoint": "vm.example:2201", "hostKey": "ssh-ed25519 AAAA", "sshUser": "user"})
        if method == "GET" and path.startswith("/sandboxes?"):
            return Answer({"ok": True, "sandboxes": [{"id": "bx_1", "labels": {boat.LABEL: "job.1"}},
                                                     {"id": "bx_2", "labels": {boat.LABEL: "other"}}]})
        if method == "GET" and path == "/limits":
            return Answer({"ok": True, "maxActiveSandboxes": 2})
        return Answer({"ok": True})


class FakeKeys:
    def __init__(self):
        self.dir, self.public = tempfile.mkdtemp(), "ssh-ed25519 AAAA nullius-boat"

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


SLOT_FULL = (429, {"ok": False, "code": "limit_reached", "message": "2 active"})
MADE = (200, {"ok": True, "sandbox": {"id": "bx_9"}})


class Client(unittest.TestCase):
    def test_the_key_is_sent_to_boat_alone_and_never_in_a_body(self):
        api = FakeAPI([MADE])
        boat.Client("boat_secret", opener=api).create({boat.LABEL: "j"})
        method, path, body, headers = api.calls[0]
        self.assertEqual((method, path), ("POST", "/sandboxes"))
        self.assertEqual(headers["Authorization"], "Bearer boat_secret")
        self.assertNotIn("boat_secret", json.dumps(body))
        self.assertEqual(body["labels"], {boat.LABEL: "j"})

    def test_a_refusal_is_an_answer_not_an_exception(self):
        d = boat.Client("k", opener=FakeAPI([SLOT_FULL])).create({})
        self.assertEqual((d["ok"], d["status"], d["code"]), (False, 429, "limit_reached"))

    def test_slots_is_the_accounts_limit_of_vms_at_once(self):
        self.assertEqual(boat.slots(boat.Client("k", opener=FakeAPI([]))), 2)

    def test_sweep_deletes_only_this_jobs_vms(self):
        api = FakeAPI([])
        self.assertEqual(boat.sweep("job.1", boat.Client("k", opener=api)), 1)
        deletes = [c for c in api.calls if c[0] == "DELETE"]
        self.assertEqual([c[1] for c in deletes], ["/sandboxes/bx_1"])
        self.assertEqual(deletes[0][3]["X-ascii-confirm-delete"], "bx_1")

    def test_the_key_comes_from_the_environment_first_then_the_store(self):
        with mock.patch.object(boat.secrets, "value", return_value=b"boat_stored\n"):
            self.assertEqual(boat.key({}), "boat_stored")
            self.assertEqual(boat.key({boat.NAME: "boat_env"}), "boat_env")
        with mock.patch.object(boat.secrets, "value", return_value=None):
            self.assertIn("Settings", boat.unusable({}))
            self.assertIsNone(boat.unusable({boat.NAME: "k"}, which=lambda p: "/usr/bin/" + p))
            self.assertIn("rsync", boat.unusable({boat.NAME: "k"}, which=lambda p: None if p == "rsync" else p))


class VM(unittest.TestCase):
    def setUp(self):
        self.keys = FakeKeys()
        self.addCleanup(self.keys.close)
        self.ssh = []
        ok = lambda argv, **kw: self.ssh.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b"")
        patcher = mock.patch.object(boat.subprocess, "run", side_effect=ok)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_full_account_is_waited_for_then_the_vm_is_locked_in(self):
        api = FakeAPI([SLOT_FULL, SLOT_FULL, MADE])
        slept = []
        vm = boat.VM(boat.Client("k", opener=api), self.keys, "job.1", sleep=slept.append)
        self.assertEqual(vm.id, "bx_9")
        self.assertEqual(len([c for c in api.calls if c[1] == "/sandboxes"]), 3)
        self.assertEqual(slept[:2], [5.0, 7.5])
        # the host key pinned, no config of this user's read
        self.assertIn("StrictHostKeyChecking=yes", vm.ssh)
        self.assertEqual(vm.ssh[1:3], ["-F", "/dev/null"])
        with open(os.path.join(self.keys.dir, "known-bx_9")) as f:
            self.assertEqual(f.read(), "bx_9 ssh-ed25519 AAAA\n")
        setup = self.ssh[-1][-1]
        self.assertIn(f"useradd -m -s /bin/bash {boat.USER}", setup)
        self.assertIn("-I OUTPUT 1 -m owner --uid-owner $U -j REJECT", setup)
        self.assertIn("--dport 53", setup)

    def test_another_refusal_or_a_stop_ends_the_wait(self):
        with self.assertRaisesRegex(boat.Unavailable, "refused"):
            boat.VM(boat.Client("k", opener=FakeAPI([(402, {"code": "no_credit", "message": "no credit"})])), self.keys, "j",
                    sleep=lambda s: None)
        stopped = threading.Event()
        stopped.set()
        with self.assertRaisesRegex(boat.Unavailable, "in time"):
            boat.VM(boat.Client("k", opener=FakeAPI([SLOT_FULL])), self.keys, "j", stopped=stopped, sleep=lambda s: None)

    def test_a_vm_that_fails_its_setup_is_deleted(self):
        api = FakeAPI([MADE])
        fail = lambda argv, **kw: subprocess.CompletedProcess(argv, 1 if "useradd" in argv[-1] else 0, b"", b"no")
        with mock.patch.object(boat.subprocess, "run", side_effect=fail), self.assertRaises(boat.Unavailable):
            boat.VM(boat.Client("k", opener=api), self.keys, "j", sleep=lambda s: None)
        self.assertEqual([c[1] for c in api.calls if c[0] == "DELETE"], ["/sandboxes/bx_9"])

    def test_the_agent_runs_as_its_user_with_its_environment_alone_and_the_relay_tunnelled(self):
        vm = boat.VM(boat.Client("k", opener=FakeAPI([MADE])), self.keys, "j", sleep=lambda s: None)
        argv = vm.argv(["/inst/bin/agent", "--acp"], {"PATH": "/inst/bin:" + boat.VM_PATH, "HOME": "/h"}, "/w s", 41234)
        self.assertEqual(argv[argv.index("-R") + 1], "127.0.0.1:41234:127.0.0.1:41234")
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertEqual(argv[-1], f"cd '/w s' && exec sudo -n -u {boat.USER} env -i PATH=/inst/bin:{boat.VM_PATH} HOME=/h /inst/bin/agent --acp")

    def test_the_workspace_comes_back_without_links_that_leave_it(self):
        vm = boat.VM(boat.Client("k", opener=FakeAPI([MADE])), self.keys, "j", sleep=lambda s: None)
        vm.fetch("/jobs/t/workspace/")
        argv = self.ssh[-1]
        self.assertEqual(argv[0], "rsync")
        for flag in ("--safe-links", "--no-devices", "--no-specials", "--delete"):
            self.assertIn(flag, argv)
        self.assertNotIn("-a", argv)                   # no owner, group or permissions of the VM's
        self.assertEqual(argv[-2:], [f"user@vm.example:/jobs/t/workspace/", "/jobs/t/workspace/"])


class Backend(unittest.TestCase):
    """One run's boat backend: a unit's VM held by its thread, brought back and always deleted."""

    def setUp(self):
        self.trial = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.trial, True)
        StandInVM.made.clear(); StandInVM.deleted.clear(); StandInVM.fetched.clear()
        for name, value in (("VM", StandInVM), ("Keys", FakeKeys)):
            p = mock.patch.object(boat, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.backend = boat.Backend("job", client=boat.Client("k", opener=FakeAPI([])))

    def test_the_label_is_this_runs_and_the_vm_comes_back_then_goes(self):
        self.assertTrue(self.backend.label.startswith("job.") and self.backend.label != boat.Backend("job").label)
        with self.backend.unit(self.trial, "/t/workspace", "/t/.home"):
            self.assertIsNotNone(self.backend._held.vm)
        self.assertEqual(StandInVM.made, [self.backend.label])
        self.assertEqual(StandInVM.fetched, ["/t/workspace", "/t/.home"])
        self.assertEqual(StandInVM.deleted, ["bx_0"])
        self.assertIsNone(self.backend._held.vm)
        with self.assertRaisesRegex(boat.Unavailable, "no Boat VM"):    # no VM held outside a unit
            self.backend.place(["a"], {}, "/t", inst=None, here=None, owned=[], relay_url=None)

    def test_the_agents_own_failure_stands_and_a_failed_copy_back_is_the_units(self):
        failing = lambda self, path: (_ for _ in ()).throw(boat.Unavailable("no way back"))
        with mock.patch.object(StandInVM, "fetch", failing):
            with self.assertRaisesRegex(RuntimeError, "the agent's"):
                with self.backend.unit(self.trial, "/t/workspace", "/t/.home"):
                    raise RuntimeError("the agent's")
            with self.assertRaisesRegex(boat.Unavailable, "no way back"):
                with self.backend.unit(self.trial, "/t/workspace", "/t/.home"):
                    pass
        self.assertEqual(StandInVM.deleted, ["bx_0", "bx_1"])


class Sent(unittest.TestCase):
    def test_a_runtime_an_install_links_to_goes_with_it_and_the_systems_does_not(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        py = os.path.join(tmp, "uv", "python", "cpython-3.12", "bin")
        os.makedirs(py)
        open(os.path.join(py, "python3.12"), "w").close()
        venv = os.path.join(tmp, "inst", "venv", "bin")
        os.makedirs(venv)
        os.symlink(os.path.join(py, "python3.12"), os.path.join(venv, "python"))
        os.symlink("/bin/sh", os.path.join(venv, "sh"))
        os.symlink("python", os.path.join(venv, "python3"))                # inside the install
        self.assertEqual(boat.outside_roots(os.path.join(tmp, "inst")), [os.path.join(tmp, "uv", "python", "cpython-3.12")])

    def test_the_vm_is_given_its_own_paths_and_none_of_this_machines(self):
        env = {"PATH": "/home/me/.nvm/bin:/usr/bin", "HOME": "/jobs/t/.home", "LANG": "C.UTF-8", "TMPDIR": "/home/me/tmp",
               "NVM_DIR": "/home/me/.nvm", "OPENAI_BASE_URL": "http://127.0.0.1:5000/u0000/v1"}
        got = boat.remote_env(env, ["/inst/node_modules/.bin"], run.KEEP)
        self.assertEqual(got, {"PATH": "/inst/node_modules/.bin:" + boat.VM_PATH, "HOME": "/jobs/t/.home", "LANG": "C.UTF-8",
                               "OPENAI_BASE_URL": "http://127.0.0.1:5000/u0000/v1"})


class StandInVM:
    """A unit's VM that runs its command on this machine: what the run sends, starts and takes back."""
    made, deleted, sent, fetched = [], [], [], []

    def __init__(self, client, keys, job, stopped=None):
        self.id, self.seconds = f"bx_{len(StandInVM.made)}", 0.1
        StandInVM.made.append(job)

    def send(self, read, owned=()):
        StandInVM.sent.append((list(read), list(owned)))

    def fetch(self, path):
        StandInVM.fetched.append(path)

    def argv(self, cmd, env, workdir, port):
        assert port, "the relay's port is tunnelled"
        return ["env", "-i", *[f"{k}={v}" for k, v in env.items() if k != "PATH"], f"PATH={os.environ['PATH']}", *cmd]

    def delete(self):
        StandInVM.deleted.append(self.id)


class BoatRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.env = {"MODEL_API_URL": f"http://127.0.0.1:{cls.up.server_address[1]}/v1", "MODEL_API_KEY": "sk-real"}

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, install, "HOME", install.HOME)
        install.HOME = os.path.join(self.tmp, "home")
        Echo.status, Echo.refused, Echo.limited = 200, None, 0
        for v in (StandInVM.made, StandInVM.deleted, StandInVM.sent, StandInVM.fetched):
            v.clear()
        self.swept = []
        for name, value in (("unusable", lambda env=None, which=None: None), ("slots", lambda client: 1),
                            ("VM", StandInVM), ("Keys", FakeKeys), ("sweep", lambda job, client: self.swept.append(job))):
            p = mock.patch.object(boat, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_each_unit_runs_in_a_vm_of_its_own_and_the_rest_stays_here(self):
        said = []
        b = definitions.get("benchmark", "fakebench", ROOT)
        a = definitions.get("agent", "fake", ROOT)
        job_dir = run.run(b, a, model="m-1", provider="custom", jobs_root=self.tmp, concurrency=2, env=self.env,
                          say=said.append, backend="boat")
        trials = {t.trial_name.split("__")[0]: t for t in parse.parse_job(job_dir).trials}
        # the units end as their scripts say, but for one: no file of this machine is served to an agent in a VM
        # (acp fs), so the fake agent's write through it is refused and its answer never lands, as in a container
        self.assertEqual({k: (t.reward, t.exception_type) for k, t in trials.items()},
                         {"good": (0.0, ""), "wrong": (0.0, ""), "model": (0.0, ""), "slow": (None, "AgentTimeoutError")})
        with open(os.path.join(job_dir, next(t for t in os.listdir(job_dir) if t.startswith("good__")), "agent", "acp.jsonl")) as f:
            self.assertNotIn('"fs": {"readTextFile": true', f.read())
        # no yes asked, one VM per unit, each deleted, the account's limit of one at a time said
        self.assertEqual(len(StandInVM.made), len(trials))
        self.assertEqual(sorted(StandInVM.deleted), sorted(f"bx_{i}" for i in range(len(trials))))
        self.assertIn("Boat runs 1 VMs at once on this account: 1 units at a time, not 2", said)
        # the run's label on every VM, swept at the end
        self.assertEqual(len(set(StandInVM.made)), 1)
        self.assertEqual(self.swept, StandInVM.made[:1])
        # what goes: the agent's definition (and install), its home and workspace; never the benchmark's folder
        for read, owned in StandInVM.sent:
            self.assertIn(a["_here"], read)
            self.assertNotIn(b["_here"], read)
            self.assertEqual([os.path.basename(p) for p in owned], ["workspace", ".home"])
        self.assertEqual(sorted(os.path.basename(p) for p in StandInVM.fetched), sorted(["workspace", ".home"] * len(trials)))
        for t in os.listdir(job_dir):
            if "__u" in t:
                with open(os.path.join(job_dir, t, "agent", "boat.json")) as f:
                    self.assertEqual(json.load(f)["size"], boat.SIZE)
        with open(os.path.join(job_dir, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["backend"], "boat")
        for root, _, files in os.walk(job_dir):
            for n in files:
                with open(os.path.join(root, n), errors="replace") as f:
                    self.assertNotIn("sk-real", f.read(), os.path.join(root, n))

    def test_a_check_runs_here_and_no_key_means_no_run(self):
        b = definitions.get("benchmark", "fakebench", ROOT)
        b["controls"] = {"nop": {"source": "nullius", "from": "nothing said"}}
        nop = definitions.control(b, "nop")
        self.assertEqual(run.Run(b, nop, model="m", provider="custom", env=self.env, jobs_root=self.tmp, backend="boat").backend, "local")
        a = definitions.get("agent", "fake", ROOT)
        with mock.patch.object(boat, "unusable", lambda env=None, which=None: "no Boat API key: set it in Settings → Sandbox"):
            with self.assertRaisesRegex(run.Refused, "Boat backend cannot run here: no Boat API key"):
                run.Run(b, a, model="m", provider="custom", env=self.env, jobs_root=self.tmp, backend="boat")

    def test_an_environment_a_vm_cannot_give_is_not_run(self):
        self.assertIn("public web", boat.refusal({"network": {"probe": "https://example.org"}}))
        self.assertIn("LICENSE", boat.refusal({"needs_secrets": [{"name": "LICENSE", "to": "agent", "kind": "file"}]}))
        self.assertIsNone(boat.refusal({"needs_secrets": [{"name": "KEY", "to": "agent", "kind": "value"}]}))
        self.assertIsNone(boat.refusal({"local_ok": True}))


class BoatRecipe(unittest.TestCase):
    def test_boat_needs_no_yes_and_runs_from_this_machine(self):
        out = recipe.compile(a_recipe(backend="boat", unsandboxed=False), defs(), boat_unusable=lambda: None)
        self.assertEqual(out["errors"], [])
        self.assertIn("--backend boat", out["script"]); self.assertNotIn("--unsandboxed", out["script"])
        self.assertFalse(out["docker"])
        self.assertIn("boat backend", out["review"][-1][2])
        out = recipe.compile(a_recipe(backend="boat", where="me@bench"), defs())
        self.assertEqual([e["step"] for e in out["errors"]], [recipe.STEP["where"]])
        self.assertIn("This machine", out["errors"][0]["message"])

    def test_without_its_key_the_where_step_says_so(self):
        out = recipe.compile(a_recipe(backend="boat"), defs(), boat_unusable=lambda: "no Boat API key: set it in Settings → Sandbox")
        self.assertEqual([e["step"] for e in out["errors"]], [recipe.STEP["where"]])
        self.assertIn("Settings → Sandbox", out["errors"][0]["message"])

    def test_a_conformance_check_on_boat_says_its_backend(self):
        agent = definitions.load("agent", ROOT)[0][0]
        with mock.patch.object(definitions, "get", return_value=agent):
            got = recipe.conformance("agent", agent["key"], model="m", provider="openai", unsandboxed=False,
                                     stamp="2026-10-10__12-00-00", backend="boat")
        self.assertEqual(got["errors"], [])
        self.assertIn("--backend boat", got["script"]); self.assertNotIn("--unsandboxed", got["script"])


if __name__ == "__main__":
    unittest.main()
