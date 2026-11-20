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
import time
import unittest
import urllib.error
from unittest import mock

from nullius.standard import boat, definitions, recipe, run

from .test_standard_recipe import a_recipe, defs
from .test_standard_run import ROOT, Echo
from http.server import ThreadingHTTPServer
from nullius.runs import parse
from nullius.standard import install, job


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
        self.deletes = []                        # answers to DELETE before it succeeds: (status, body)

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
            return Answer({"ok": True, "maxActiveSandboxes": 2, "machineTypes": [
                {"type": "small", "available": True, "reason": None}, {"type": "default", "available": True, "reason": None},
                {"type": "large", "available": False, "reason": "not during the free trial"}]})
        if method == "DELETE" and self.deletes:
            status, d = self.deletes.pop(0)
            raise urllib.error.HTTPError(req.full_url, status, "no", {}, io.BytesIO(json.dumps(d).encode()))
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

    def test_a_size_the_account_cannot_start_says_boats_reason(self):
        client = boat.Client("k", opener=FakeAPI([]))
        self.assertEqual(boat.machine_types(client), {"small": None, "default": None, "large": "not during the free trial"})
        self.assertIsNone(boat.size_problem(client, "default"))
        self.assertEqual(boat.size_problem(client, "large"), "not during the free trial")
        self.assertIn("not one of Boat's sizes", boat.size_problem(client, "huge"))

    def test_a_vm_is_asked_for_in_its_size(self):
        api = FakeAPI([MADE])
        boat.Client("k", opener=api).create({}, size="default")
        self.assertEqual(api.calls[0][2]["type"], "default")

    def test_a_run_longer_than_the_accounts_starts_is_said(self):
        limits = lambda day, hour: boat.Client("k", opener=lambda req, timeout=None: Answer(
            {"ok": True, "starts": {"day": {"remaining": day}, "hour": {"remaining": hour}}}))
        self.assertIn("50 of the 100 units will end without running", boat.starts_problem(limits(50, 9), 100))
        self.assertIn("9 more VMs this hour", boat.starts_problem(limits(50, 9), 20))
        self.assertIsNone(boat.starts_problem(limits(50, 9), 9))
        self.assertIsNone(boat.starts_problem(boat.Client("k", opener=lambda req, timeout=None: Answer({"ok": True})), 500))

    def test_slots_is_the_accounts_limit_of_vms_at_once(self):
        self.assertEqual(boat.slots(boat.Client("k", opener=FakeAPI([]))), 2)

    def test_one_answer_of_its_limits_says_both_its_sizes_and_its_vms_at_once(self):
        api = FakeAPI([])
        client = boat.Client("k", opener=api)
        limits = client.limits()
        self.assertEqual((boat.slots(client, limits), boat.machine_types(client, limits)["large"]), (2, "not during the free trial"))
        self.assertEqual(len(api.calls), 1)
        self.assertIsNone(boat.slots(client, {"maxActiveSandboxes": True}))

    def test_a_size_fits_a_units_own_vm_only_with_more_memory_than_it(self):
        # measured: OSWorld's 4 GB guest is refused on small (4 GB, 2 free) and boots on default (8 GB)
        self.assertFalse(boat.fits("small", 4))
        self.assertTrue(boat.fits("default", 4))
        self.assertTrue(boat.fits("small", 0))
        self.assertTrue(boat.fits("large", 4))                 # its memory unmeasured: not refused here
        self.assertEqual((boat.spec("small"), boat.spec("large")), ("2 vCPU, 4 GB", ""))

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
            self.assertIn("Settings", boat.unusable(env={}))
            self.assertIsNone(boat.unusable(env={boat.NAME: "k"}, which=lambda p: "/usr/bin/" + p))
            self.assertIn("rsync", boat.unusable(env={boat.NAME: "k"}, which=lambda p: None if p == "rsync" else p))


class VM(unittest.TestCase):
    def setUp(self):
        self.keys = FakeKeys()
        self.addCleanup(self.keys.close)
        self.ssh = []
        ok = lambda argv, **kw: self.ssh.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b"")
        patcher = mock.patch.object(boat.subprocess, "run", side_effect=ok)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_vm_lives_as_long_as_its_unit_and_a_trials_two_hours_at_most(self):
        trial = (400, {"ok": False, "code": "trial_auto_stop_required", "message": "Free-trial auto-stop is 2 hours maximum."})
        api = FakeAPI([trial, MADE])
        vm = boat.VM(boat.Client("k", opener=api), self.keys, "j", sleep=lambda s: None, ttl=5 * 3600)
        self.assertEqual([c[2]["ttlSeconds"] for c in api.calls if c[1] == "/sandboxes"], [5 * 3600, boat.TTL])
        self.assertEqual(vm.ttl, boat.TTL)
        api = FakeAPI([MADE])
        self.assertEqual(boat.VM(boat.Client("k", opener=api), self.keys, "j", sleep=lambda s: None, ttl=60).ttl, boat.TTL)

    def test_a_full_account_is_waited_for_then_the_vm_is_locked_in(self):
        api = FakeAPI([SLOT_FULL, SLOT_FULL, MADE])
        slept = []
        vm = boat.VM(boat.Client("k", opener=api), self.keys, "job.1", sleep=slept.append)
        self.assertEqual(vm.id, "bx_9")
        self.assertEqual(len([c for c in api.calls if c[1] == "/sandboxes"]), 3)
        self.assertEqual(slept[:2], [5.0, 7.5])
        self.assertIn("StrictHostKeyChecking=yes", vm.ssh)
        self.assertEqual(vm.ssh[1:3], ["-F", "/dev/null"])
        with open(os.path.join(self.keys.dir, "known-bx_9")) as f:
            self.assertEqual(f.read(), "bx_9 ssh-ed25519 AAAA\n")
        setup = self.ssh[-1][-1]
        self.assertIn(f"useradd -m -s /bin/bash {boat.USER}", setup)
        # each rule goes in at 1: the last inserted is matched first, so DNS is refused before loopback is accepted
        rules = [r.split(" -m owner")[0].split("-I OUTPUT 1")[1].strip() for r in setup.split("sudo -n $t ")[1:]][:4][::-1]
        self.assertEqual(rules, ["-o lo -p udp --dport 53", "-o lo -p tcp --dport 53", "-o lo", ""])
        self.assertIn("-o lo -m owner --uid-owner $U -j ACCEPT", setup)

    def test_another_refusal_or_a_stop_ends_the_wait(self):
        with self.assertRaisesRegex(boat.Unavailable, "refused"):
            boat.VM(boat.Client("k", opener=FakeAPI([(402, {"code": "no_credit", "message": "no credit"})])), self.keys, "j",
                    sleep=lambda s: None)
        stopped = threading.Event()
        stopped.set()
        with self.assertRaisesRegex(boat.Unavailable, "in time"):
            boat.VM(boat.Client("k", opener=FakeAPI([SLOT_FULL])), self.keys, "j", stopped=stopped, sleep=lambda s: None)

    def test_a_stop_while_the_vm_comes_up_ends_the_wait_and_deletes_it(self):
        stopped = threading.Event()
        stopped.set()
        api = FakeAPI([MADE])
        with self.assertRaisesRegex(boat.Unavailable, "stopped"):
            boat.VM(boat.Client("k", opener=api), self.keys, "j", stopped=stopped, sleep=lambda s: self.fail("waited"))
        self.assertEqual([c[1] for c in api.calls if c[0] == "DELETE"], ["/sandboxes/bx_9"])
        self.assertEqual(self.ssh, [])

    def test_a_delete_boat_refuses_is_tried_again_and_said_and_one_already_gone_is_done(self):
        api = FakeAPI([MADE])
        vm = boat.VM(boat.Client("k", opener=api), self.keys, "j", sleep=lambda s: None)
        api.deletes = [(500, {"message": "busy"}), (500, {"message": "still busy"})]
        self.assertEqual(vm.delete(), "still busy")
        self.assertEqual(len([c for c in api.calls if c[0] == "DELETE"]), 2)
        api.deletes = [(500, {"message": "busy"})]
        self.assertIsNone(vm.delete())               # the second try went through
        api.deletes = [(404, {"message": "not found"})]
        self.assertIsNone(vm.delete())               # already gone
        said = []
        api.deletes = [(500, {"message": "busy"})]
        self.assertEqual(boat.sweep("job.1", boat.Client("k", opener=api), said.append), 0)
        self.assertEqual(len(said), 1)
        self.assertIn("did not delete VM bx_1: busy", said[0])

    def test_a_vm_that_fails_its_setup_is_deleted(self):
        api = FakeAPI([MADE])
        fail = lambda argv, **kw: subprocess.CompletedProcess(argv, 1 if "useradd" in argv[-1] else 0, b"", b"no")
        with mock.patch.object(boat.subprocess, "run", side_effect=fail), self.assertRaises(boat.Unavailable):
            boat.VM(boat.Client("k", opener=api), self.keys, "j", sleep=lambda s: None)
        self.assertEqual([c[1] for c in api.calls if c[0] == "DELETE"], ["/sandboxes/bx_9"])

    def test_the_agent_runs_as_its_user_with_its_environment_alone_and_the_relay_tunnelled(self):
        vm = boat.VM(boat.Client("k", opener=FakeAPI([MADE])), self.keys, "j", sleep=lambda s: None)
        given = []

        def env_file(argv, stdin=None, **kw):
            self.ssh.append(argv)
            given.append(stdin.read().decode() if stdin is not None else None)
            return subprocess.CompletedProcess(argv, 0, b"/tmp/.nullius-env.ab12\n", b"")
        with mock.patch.object(boat.subprocess, "run", side_effect=env_file):
            argv = vm.argv(["/inst/bin/agent", "--acp"], {"PATH": "/inst/bin:" + boat.VM_PATH, "HOME": "/h",
                                                          "LICENCE": "s3cret value", "bad name": "x"}, "/w s", 41234)
        self.assertEqual(argv[argv.index("-R") + 1], "127.0.0.1:41234:127.0.0.1:41234")
        self.assertIn("ExitOnForwardFailure=yes", argv)
        # the environment goes as a file on stdin, read and removed by the agent's shell: never on a command line
        self.assertEqual(given[-1], f"export PATH=/inst/bin:{boat.VM_PATH}\nexport HOME=/h\nexport LICENCE='s3cret value'\n")
        self.assertIn(f"sudo -n chown {boat.USER}", self.ssh[-1][-1])
        self.assertNotIn("s3cret", " ".join(argv + self.ssh[-1]))
        self.assertEqual(argv[-1], f"cd '/w s' && exec sudo -n -u {boat.USER} env -i /bin/bash -c "
                                   "'. \"$0\" && rm -f \"$0\" && exec \"$@\"' /tmp/.nullius-env.ab12 /inst/bin/agent --acp")

    def test_an_ssh_that_hangs_while_the_vm_boots_is_tried_again(self):
        tries = []

        def hangs_once(argv, **kw):
            tries.append(argv)
            if len(tries) == 1:
                raise subprocess.TimeoutExpired(argv, 60)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        with mock.patch.object(boat.subprocess, "run", side_effect=hangs_once):
            boat.VM(boat.Client("k", opener=FakeAPI([MADE])), self.keys, "j", sleep=lambda s: None)
        self.assertGreaterEqual(len(tries), 3)

    def test_a_command_or_a_copy_with_no_answer_is_the_units_unavailable(self):
        vm = boat.VM(boat.Client("k", opener=FakeAPI([MADE])), self.keys, "j", sleep=lambda s: None)
        with mock.patch.object(boat.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 900)):
            with self.assertRaisesRegex(boat.Unavailable, "no answer"):
                vm.sh("true")
            with self.assertRaisesRegex(boat.Unavailable, "did not come back"):
                vm.fetch("/jobs/t/workspace")

    def test_the_vms_own_refusal_is_said_and_a_folder_that_cannot_be_packed_is_too(self):
        vm = boat.VM(boat.Client("k", opener=FakeAPI([MADE])), self.keys, "j", sleep=lambda s: None)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        refused = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"disk full")
        with mock.patch.object(boat.subprocess, "run", side_effect=refused):
            with self.assertRaisesRegex(boat.Unavailable, "disk full"):
                vm.send([tmp])
        drained = lambda argv, stdin=None, **kw: (stdin.read(), subprocess.CompletedProcess(argv, 0, b"", b""))[1]
        with mock.patch.object(boat.subprocess, "run", side_effect=drained):
            with self.assertRaisesRegex(boat.Unavailable, "could not pack.*(No such file|Cannot stat)"):
                vm.send([os.path.join(tmp, "gone")])

    def test_the_workspace_comes_back_without_links_that_leave_it(self):
        vm = boat.VM(boat.Client("k", opener=FakeAPI([MADE])), self.keys, "j", sleep=lambda s: None)
        vm.fetch("/jobs/t/workspace/")
        argv = self.ssh[-1]
        self.assertEqual(argv[0], "rsync")
        for flag in ("--safe-links", "--no-devices", "--no-specials", "--delete"):
            self.assertIn(flag, argv)
        self.assertNotIn("-a", argv)
        self.assertEqual(argv[-2:], [f"user@vm.example:/jobs/t/workspace/", "/jobs/t/workspace/"])


class Backend(unittest.TestCase):
    """One run's boat backend: a unit's VM held by its thread, brought back and always deleted."""

    def setUp(self):
        self.trial = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.trial, True)
        StandInVM.made.clear(); StandInVM.deleted.clear(); StandInVM.fetched.clear(); StandInVM.sent.clear()
        for name, value in (("VM", StandInVM), ("Keys", FakeKeys)):
            p = mock.patch.object(boat, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.said = []
        self.backend = boat.Backend("job", client=boat.Client("k", opener=FakeAPI([])), say=self.said.append)

    def test_a_unit_longer_than_a_trials_vm_is_said_once(self):
        StandInVM.asked.clear()
        for _ in range(2):
            with self.backend.unit(self.trial, "/t/workspace", "/t/.home", seconds=3 * 3600):
                pass
        self.assertEqual(len(self.said), 1)
        self.assertIn("2 hours", self.said[0])
        # the second unit asks for the trial's auto-stop at once: no refused create counts against its starts
        self.assertEqual(StandInVM.asked, [3 * 3600 + boat.DRIVER, boat.TTL])
        with open(os.path.join(self.trial, "agent", "boat.json")) as f:
            self.assertEqual(json.load(f)["ttl_seconds"], boat.TTL)

    def test_a_vm_boat_would_not_delete_is_said_and_kept_with_the_units_evidence(self):
        StandInVM.refuse = "busy"
        self.addCleanup(setattr, StandInVM, "refuse", None)
        with self.backend.unit(self.trial, "/t/workspace", "/t/.home"):
            pass
        self.assertEqual(len(self.said), 1)
        self.assertIn("did not delete VM bx_0: busy", self.said[0])
        with open(os.path.join(self.trial, "agent", "boat.json")) as f:
            self.assertEqual(json.load(f)["delete_error"], "busy")

    def test_after_a_stop_nothing_is_brought_back(self):
        stopped = threading.Event()
        with self.assertRaises(RuntimeError):
            with self.backend.unit(self.trial, "/t/workspace", "/t/.home", stopped=stopped):
                stopped.set()
                raise RuntimeError("stopped")
        self.assertEqual((StandInVM.fetched, StandInVM.deleted), ([], ["bx_0"]))

    def test_the_label_is_this_runs_and_the_vm_comes_back_then_goes(self):
        self.assertTrue(self.backend.label.startswith("job.") and self.backend.label != boat.Backend("job").label)
        with self.backend.unit(self.trial, "/t/workspace", "/t/.home"):
            self.assertIsNotNone(self.backend._held.vm)
        self.assertEqual(StandInVM.made, [self.backend.label])
        self.assertEqual(StandInVM.fetched, ["/t/workspace", "/t/.home"])
        self.assertEqual(StandInVM.deleted, ["bx_0"])
        self.assertIsNone(self.backend._held.vm)
        with self.assertRaisesRegex(boat.Unavailable, "no Boat VM"):
            self.backend.place(["a"], {}, "/t", inst=None, owned=[], relay_url=None)

    def test_the_install_is_sent_before_the_unit_begins_and_its_home_as_the_agent_starts(self):
        with self.backend.unit(self.trial, "/t/workspace", "/t/.home", inst=self.trial, here=self.trial):
            self.assertEqual(StandInVM.sent, [([self.trial, self.trial], [])])
            self.backend.place(["a"], {}, "/t/workspace", inst=None, owned=["/t/workspace", "/t/.home"], relay_url="http://127.0.0.1:5000/u0/v1")
            self.assertEqual(StandInVM.sent[-1], ([], ["/t/workspace", "/t/.home"]))
        with open(os.path.join(self.trial, "agent", "boat.json")) as f:
            self.assertIn("sent_seconds", json.load(f))

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
        os.symlink("python", os.path.join(venv, "python3"))
        self.assertEqual(boat.outside_roots(os.path.join(tmp, "inst")), [os.path.join(tmp, "uv", "python", "cpython-3.12")])

    def test_a_link_to_this_users_own_files_keeps_the_install_here(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        home = os.path.join(tmp, "home")
        for d in (".ssh", "lib", ".local/share/uv/python/cpython-3.12/bin", ".local/bin", ".config/gh"):
            os.makedirs(os.path.join(home, d))
        for f in (".gitconfig", ".ssh/id_ed25519", ".local/share/uv/python/cpython-3.12/bin/python3", ".local/bin/tool",
                  ".config/gh/hosts.yml"):
            open(os.path.join(home, f), "w").close()
        inst = os.path.join(tmp, "inst")
        os.makedirs(inst)
        env = {"HOME": home, "NULLIUS_HOME": os.path.join(tmp, "elsewhere")}     # Nullius's storage outside the home
        with mock.patch.dict(os.environ, {"HOME": home}):
            os.symlink(os.path.join(home, "lib"), os.path.join(inst, "lib"))
            os.symlink(os.path.join(home, ".local/share/uv/python/cpython-3.12/bin/python3"), os.path.join(inst, "python"))
            self.assertEqual(boat.outside_roots(inst, env), [os.path.join(home, "lib"),
                                                             os.path.join(home, ".local/share/uv/python/cpython-3.12")])
            # a file in ~/.local/bin would send ~/.local (keyrings); a tool's login under ~/.config
            for target in (".gitconfig", ".ssh/id_ed25519", ".local/bin/tool", ".config/gh/hosts.yml"):
                link = os.path.join(inst, "x")
                os.symlink(os.path.join(home, target), link)
                with self.assertRaisesRegex(boat.Unavailable, "this user's own files"):
                    boat.outside_roots(inst, env)
                os.remove(link)

    def test_the_vm_is_given_its_own_paths_and_none_of_this_machines(self):
        env = {"PATH": "/home/me/.nvm/bin:/usr/bin", "HOME": "/jobs/t/.home", "LANG": "C.UTF-8", "TMPDIR": "/home/me/tmp",
               "NVM_DIR": "/home/me/.nvm", "OPENAI_BASE_URL": "http://127.0.0.1:5000/u0000/v1"}
        got = boat.remote_env(env, ["/inst/node_modules/.bin"], run.KEEP)
        self.assertEqual(got, {"PATH": "/inst/node_modules/.bin:" + boat.VM_PATH, "HOME": "/jobs/t/.home", "LANG": "C.UTF-8",
                               "OPENAI_BASE_URL": "http://127.0.0.1:5000/u0000/v1"})


class StandInVM:
    """A unit's VM that runs its command on this machine: what the run sends, starts and takes back."""
    made, deleted, sent, fetched, sizes, ready, asked = [], [], [], [], [], [], []
    refuse = None                                # why a delete failed, or None

    def __init__(self, client, keys, job, size=boat.SIZE, stopped=None, ttl=boat.TTL):
        StandInVM.sizes.append(size)
        StandInVM.asked.append(ttl)
        self.ttl = boat.TTL                      # a free trial's
        self.id, self.seconds = f"bx_{len(StandInVM.made)}", 0.1
        StandInVM.made.append(job)

    def send(self, read, owned=()):
        StandInVM.sent.append((list(read), list(owned)))
        if not owned:
            time.sleep(0.05)
            StandInVM.ready.append(job.iso(time.time()))

    def fetch(self, path):
        StandInVM.fetched.append(path)

    def argv(self, cmd, env, workdir, port):
        assert port, "the relay's port is tunnelled"
        return ["env", "-i", *[f"{k}={v}" for k, v in env.items() if k != "PATH"], f"PATH={os.environ['PATH']}", *cmd]

    def delete(self):
        StandInVM.deleted.append(self.id)
        return StandInVM.refuse


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
        for v in (StandInVM.made, StandInVM.deleted, StandInVM.sent, StandInVM.fetched, StandInVM.sizes, StandInVM.ready):
            v.clear()
        self.swept = []
        for name, value in (("unusable", lambda env=None, which=None: None), ("slots", lambda client: 1),
                            ("starts_problem", lambda client, n: f"Boat can start 1 more VMs today on this account: {n - 1} of the {n} units will end without running"),
                            ("size_problem", lambda client, size: "not during the free trial" if size == "large" else None),
                            ("VM", StandInVM), ("Keys", FakeKeys), ("sweep", lambda job, client, say=None: self.swept.append(job))):
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
        self.assertEqual(len(StandInVM.made), len(trials))
        self.assertEqual(sorted(StandInVM.deleted), sorted(f"bx_{i}" for i in range(len(trials))))
        self.assertIn("Boat runs 1 VMs at once on this account: 1 units at a time, not 2", said)
        self.assertIn(f"Boat can start 1 more VMs today on this account: {len(trials) - 1} of the {len(trials)} units will end without running", said)
        self.assertEqual(len(set(StandInVM.made)), 1)
        self.assertEqual(self.swept, StandInVM.made[:1])
        installs = [read for read, owned in StandInVM.sent if not owned]
        self.assertEqual(len(installs), len(trials))
        for read in installs:
            self.assertIn(a["_here"], read)
            self.assertNotIn(b["_here"], read)
        for read, owned in StandInVM.sent:
            if owned:
                self.assertEqual((read, [os.path.basename(p) for p in owned]), ([], ["workspace", ".home"]))
        self.assertEqual(sorted(os.path.basename(p) for p in StandInVM.fetched), sorted(["workspace", ".home"] * len(trials)))
        began = []
        for t in os.listdir(job_dir):
            if "__u" in t:
                with open(os.path.join(job_dir, t, "agent", "boat.json")) as f:
                    self.assertEqual(json.load(f)["size"], boat.SIZE)
                with open(os.path.join(job_dir, t, "result.json")) as f:
                    began.append(json.load(f)["agent_execution"]["started_at"])
        # one VM at a time: each agent's time starts once its VM is up and given its install
        for start, ready in zip(sorted(began), sorted(StandInVM.ready)):
            self.assertGreaterEqual(start, ready)
        with open(os.path.join(job_dir, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["backend"], "boat")
        for root, _, files in os.walk(job_dir):
            for n in files:
                with open(os.path.join(root, n), errors="replace") as f:
                    self.assertNotIn("sk-real", f.read(), os.path.join(root, n))

    def test_the_runs_size_is_every_vms_and_one_the_account_cannot_start_is_refused(self):
        b = definitions.get("benchmark", "fakebench", ROOT)
        a = definitions.get("agent", "fake", ROOT)
        job_dir = run.run(b, a, model="m-1", provider="custom", jobs_root=self.tmp, concurrency=1, env=self.env,
                          say=lambda s: None, backend="boat", boat_size="default", limit=2, name="sized")
        self.assertEqual(StandInVM.sizes, ["default", "default"])
        with open(os.path.join(job_dir, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["boat_size"], "default")
        with self.assertRaisesRegex(run.Refused, "Boat cannot start a large VM: not during the free trial"):
            run.Run(b, a, model="m", provider="custom", env=self.env, jobs_root=self.tmp, backend="boat", boat_size="large")

    def test_a_check_is_refused_not_run_here_and_no_key_means_no_run(self):
        b = definitions.get("benchmark", "fakebench", ROOT)
        b["controls"] = {"nop": {"source": "nullius", "from": "nothing said"}}
        nop = definitions.control(b, "nop")
        with self.assertRaisesRegex(run.Refused, "runs on a machine, not in a Boat VM"):
            run.Run(b, nop, model="m", provider="custom", env=self.env, jobs_root=self.tmp, backend="boat")
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
    def test_boat_needs_no_yes_and_runs_here_or_on_a_host(self):
        out = recipe.compile(a_recipe(backend="boat", unsandboxed=False), defs(), boat_unusable=lambda here=True: None)
        self.assertEqual(out["errors"], [])
        self.assertIn("--backend boat --boat-size small", out["script"]); self.assertNotIn("--unsandboxed", out["script"])
        self.assertFalse(out["docker"])
        sized = recipe.compile(a_recipe(backend="boat", boat_size="default"), defs(), boat_unusable=lambda here=True: None)
        self.assertIn("--boat-size default", sized["script"])
        self.assertIn("Boat sandboxes · one Default VM per ", sized["review"][-1][2])
        self.assertEqual([e["step"] for e in recipe.compile(a_recipe(backend="boat", boat_size="huge"), defs())["errors"]],
                         [recipe.STEP["where"]])
        # as many units at once as the account runs, unless the recipe says fewer; the run caps it (boat.slots)
        every = recipe.compile(a_recipe(backend="boat", concurrency=""), defs(), boat_unusable=lambda here=True: None)
        self.assertIn(f"-j {recipe.MAX_CONCURRENT} ", every["script"])
        self.assertIn("as many at a time as Boat allows", next(r[2] for r in every["review"] if r[1] == "Scope"))
        fewer = recipe.compile(a_recipe(backend="boat", concurrency=3), defs(), boat_unusable=lambda here=True: None)
        self.assertIn("-j 3 ", fewer["script"])

    def test_hosts_and_sandboxes_do_not_mix(self):
        asked = []
        for where in ("me@bench", ["local", "me@bench"]):
            out = recipe.compile(a_recipe(backend="boat", where=where), defs(), boat_unusable=lambda here=True: asked.append(here))
            self.assertEqual([e["message"] for e in out["errors"]], ["Hosts and Boat sandboxes do not mix: choose Hosts, or Sandboxes alone."])
        self.assertEqual(asked, [])

    def test_a_size_the_account_cannot_start_is_the_where_steps_error(self):
        problem = lambda size: "not during the free trial" if size == "large" else None
        out = recipe.compile(a_recipe(backend="boat", boat_size="large"), defs(), boat_unusable=lambda here=True: None, boat_size_problem=problem)
        self.assertEqual([e["step"] for e in out["errors"]], [recipe.STEP["where"]])
        self.assertIn("large VM on this account: not during the free trial", out["errors"][0]["message"])
        out = recipe.compile(a_recipe(backend="boat", boat_size="default"), defs(), boat_unusable=lambda here=True: None, boat_size_problem=problem)
        self.assertEqual(out["errors"], [])

    def test_a_benchmark_booting_its_own_vms_wants_a_size_that_holds_them_and_never_a_container(self):
        # OSWorld's Original setup, on the bundled catalogue: the size is judged from its definition's VM, not its name
        base = {"harness": recipe.HARNESS, "name": "os", "benchmark": "osworld2", "datasets": ["offline36"], "others": ["original"],
                "model": "MiniMax-M3", "provider": "openrouter", "where": "local"}
        errors = lambda **k: [e["message"] for e in recipe.compile(dict(base, **k), boat_unusable=lambda here=True: None)["errors"]
                              if e["step"] == recipe.STEP["where"]]
        small = errors(backend="boat", boat_size="small")
        self.assertIn("A Small VM is too small for OSWorld2, whose units boot a 4 GB VM: choose a larger size.", small)
        # Model-calling setups and controls use a relay container inside each whole-task VM.
        self.assertNotIn("Original cannot run in a Boat VM yet: choose Hosts.", small)
        self.assertEqual(errors(backend="boat", boat_size="default", others=["original", "nop"]), [])
        self.assertFalse([m for m in errors(backend="boat", boat_size="default") if "too small" in m])
        self.assertIn("OSWorld2 starts each unit's VM on the machine: choose local processes.", errors(backend="container"))
        self.assertFalse([m for m in errors(backend="local") if "VM" in m])
        self.assertTrue(definitions.boots_vms(definitions.get("benchmark", "osworld1")))
        self.assertFalse(definitions.boots_vms(definitions.get("benchmark", "gsm8k")))

    def test_without_its_key_the_where_step_says_so(self):
        out = recipe.compile(a_recipe(backend="boat"), defs(), boat_unusable=lambda here=True: "no Boat API key: set it in Settings → Sandbox")
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
