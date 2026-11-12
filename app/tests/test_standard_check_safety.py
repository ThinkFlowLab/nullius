"""An own image's check runs with nothing of the agent's left running and
with a way out to public addresses only.

Without docker: the ``keep_processes`` field of a step; the script that stops
what the agent left (``container.SETTLE``) and the one that finds the
keep-alive (``container.KEEPER``), run by ``sh`` over a made-up ``/proc``
with ``kill`` recorded rather than sent; the order a check's step is taken
in (stopped, copied in, online, run with the proxy named); the proxy's rule
(``egress.public``), its parsing, and its refusals, served here; and
Terminal-Bench's driver keeping processes for the tasks whose tests need a
service the agent left running, and preparing every task with tmux; a unit's
preparation (root, its proxy closed and what it left stopped before the
agent). With docker (skipped otherwise): a loop left running in a box is gone
after ``settle``, a check's proxy refuses the metadata address and a private
one while it reaches a public host, and a preparation's proxy, once closed,
leaves no ``egress`` name and no route out."""
from __future__ import annotations

import json
import os
import runpy
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from nullius.standard import container, definitions, egress, run
from tests import docker_tier

TB = os.path.join(definitions.CATALOGUE, "benchmarks", "terminal-bench")


class Step(unittest.TestCase):
    BASE = {"copy": {"t": "/tests"}, "run": ["sh", "/tests/x.sh"], "collect": "/logs", "network": True}

    def test_a_check_may_keep_what_the_agent_left_running(self):
        self.assertEqual(container.step_problems(dict(self.BASE, keep_processes=True), collect=True), [])
        self.assertEqual(container.step_problems(dict(self.BASE, keep_processes=False), collect=True), [])
        self.assertTrue(any("keep_processes: true or false" in p
                            for p in container.step_problems(dict(self.BASE, keep_processes="yes"), collect=True)))

    def test_only_the_check_has_processes_to_keep(self):
        solve = {"copy": {"s": "/solution"}, "run": ["sh", "/solution/solve.sh"], "keep_processes": True}
        self.assertTrue(any("keep_processes: not a field" in p for p in container.step_problems(solve, collect=False)))

    def test_the_settle_command_names_the_keep_alive(self):
        self.assertEqual(container.Box.settle_argv("7"), ["sh", "-c", container.SETTLE, "settle", "7"])


def _fake_proc(root: str, procs: dict):
    """A /proc of our own: pid -> (comm, state, ppid)."""
    for pid, (comm, state, ppid) in procs.items():
        d = os.path.join(root, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "stat"), "w") as f:
            f.write(f"{pid} ({comm}) {state} {ppid} {pid} 1 0 -1 4194560\n")
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")


class Scripts(unittest.TestCase):
    """The scripts, over a made-up /proc: ``kill`` and ``sleep`` are shell
    functions here (a function comes before a regular builtin), so nothing of
    this machine is signalled."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proc = os.path.join(self.tmp, "proc")
        self.log = os.path.join(self.tmp, "kills")
        os.makedirs(self.proc)

    def sh(self, script: str, *args: str, dies: bool = True):
        prelude = (f'kill() {{ echo "$1 $2" >> {self.log}; '
                   + (f'[ "$1" = -KILL ] && rm -rf "{self.proc}/$2"; ' if dies else "")
                   + 'return 0; }\nsleep() { :; }\n')
        body = prelude + script.replace("/proc", self.proc)
        return subprocess.run(["sh", "-c", body, "sh", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=60)

    def kills(self):
        try:
            with open(self.log) as f:
                return f.read().split("\n")
        except OSError:
            return []

    def test_everything_but_init_and_the_keep_alive_is_stopped_then_killed(self):
        _fake_proc(self.proc, {1: ("docker-init", "S", 0), 7: ("sh", "S", 1), 8: ("sleep", "S", 7),
                               20: ("my ) agent", "S", 1), 21: ("gone", "Z", 1), 30: ("server", "S", 20)})
        r = self.sh(container.SETTLE, "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(l for l in r.stdout.splitlines()), ["stopped 20 my ) agent", "stopped 30 server"])
        k = self.kills()
        self.assertLess(k.index("-STOP 20"), k.index("-KILL 20"))          # stopped first: a stopped process cannot fork
        self.assertLess(k.index("-STOP 30"), k.index("-KILL 20"))
        for spared in ("1", "7", "8", "21"):
            self.assertFalse([l for l in k if l.endswith(" " + spared)], spared)
        self.assertEqual(sorted(os.listdir(self.proc)), ["1", "21", "7", "8"])

    def test_a_box_with_nothing_left_is_left_alone(self):
        _fake_proc(self.proc, {1: ("docker-init", "S", 0), 7: ("sh", "S", 1), 8: ("sleep", "S", 7)})
        r = self.sh(container.SETTLE, "7")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(self.kills(), [])

    def test_what_will_not_die_is_said_and_fails(self):
        _fake_proc(self.proc, {1: ("docker-init", "S", 0), 7: ("sh", "S", 1), 40: ("stubborn", "D", 1)})
        r = self.sh(container.SETTLE, "7", dies=False)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines()[-1], "still running: 40")

    def test_the_keep_alive_is_the_one_child_of_init(self):
        _fake_proc(self.proc, {1: ("docker-init", "S", 0), 7: ("sh", "S", 1), 8: ("sleep", "S", 7)})
        r = self.sh(container.KEEPER)
        self.assertEqual(r.stdout.split(), ["7"])


class FakeBox:
    def __init__(self, fail_settle: bool = False, fail_offline: bool = False, exit: int = 0):
        self.did, self.fail_settle, self.egress = [], fail_settle, None
        self.fail_offline, self.exit, self.users = fail_offline, exit, []

    def settle(self):
        self.did.append("settle")
        if self.fail_settle:
            raise container.Unavailable("what the agent left running could not be stopped: still running: 40")
        return ["20 server"]

    def copy_in(self, src, dest):
        self.did.append("copy " + dest)

    def online(self):
        self.did.append("online")
        return {"HTTPS_PROXY": "http://egress:3128"}

    def offline(self):
        self.did.append("offline")
        if self.fail_offline:
            raise container.Unavailable("the way out (x-egress) could not be removed")

    def write_in(self, dest, text):
        self.did.append("write " + dest)

    def run(self, cmd, seconds, *, user=None, env=None):
        self.did.append(("run", cmd[0], dict(env or {})))
        self.users.append(user)
        return {"exit": self.exit, "output": "ok", "timed_out": False}

    def egress_said(self):
        return '{"event": "refused", "host": "169.254.169.254", "port": 80}\n'

    def copy_out(self, src, dest):
        return True


class Order(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "tests"))
        self.r = run.Run.__new__(run.Run)
        self.r.up, self.r.bench, self.r.turn_seconds = self.tmp, {"_here": self.tmp}, 60
        self.step = {"copy": {"tests": "/tests"}, "run": ["bash", "/tests/test.sh"], "collect": "/logs", "network": True}

    def test_stopped_then_copied_then_online_and_the_proxy_named_to_the_check_alone(self):
        box = FakeBox()
        trial = os.path.join(self.tmp, "trial")
        v = self.r.verify(self.step, box, trial)
        self.assertEqual(box.did[:3], ["settle", "copy /tests", "online"])
        self.assertEqual(box.did[-1], ("run", "bash", {"HTTPS_PROXY": "http://egress:3128"}))
        self.assertEqual(v["exit"], 0)
        with open(os.path.join(trial, "verifier", "stopped.txt")) as f:
            self.assertEqual(f.read(), "20 server\n")
        with open(os.path.join(trial, "verifier", "egress.log")) as f:
            self.assertIn("refused", f.read())

    def test_a_check_that_keeps_processes_is_not_settled(self):
        box = FakeBox()
        self.r.verify(dict(self.step, keep_processes=True), box, os.path.join(self.tmp, "trial"))
        self.assertNotIn("settle", box.did)

    def test_a_check_without_the_network_has_no_proxy(self):
        box = FakeBox()
        self.r.verify(dict(self.step, network=False), box, os.path.join(self.tmp, "trial"))
        self.assertNotIn("online", box.did)
        self.assertEqual(box.did[-1], ("run", "bash", {}))

    def test_what_cannot_be_stopped_is_no_check(self):
        box = FakeBox(fail_settle=True)
        with self.assertRaises(run._Ended) as e:
            self.r.verify(self.step, box, os.path.join(self.tmp, "trial"))
        self.assertEqual(e.exception.kind, "VerifierError")
        self.assertIn("still running", e.exception.said)
        self.assertEqual(box.did, ["settle"])                  # nothing copied in, nothing online

    def test_a_reference_solution_is_not_settled(self):
        box = FakeBox()
        self.r.in_box(box, {"copy": {"tests": "/solution"}, "run": ["sh", "/solution/solve.sh"]}, collect=False, what="oracle")
        self.assertNotIn("settle", box.did)


class Prepare(unittest.TestCase):
    """A unit's preparation: run as root before anyone works in the box, its
    way out the check's proxy, closed and what it left stopped before the
    agent starts; any failure is the environment's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.r = run.Run.__new__(run.Run)
        self.r.up, self.r.bench, self.r.turn_seconds = self.tmp, {"_here": self.tmp}, 60
        self.step = {"write": {"/opt/p.sh": "true"}, "run": ["sh", "/opt/p.sh"], "network": True}
        self.trial = os.path.join(self.tmp, "trial")

    def test_online_then_run_as_root_then_closed_then_settled(self):
        box = FakeBox()
        self.r.prepare(self.step, box, self.trial)
        self.assertEqual(box.did, ["write /opt/p.sh", "online", ("run", "sh", {"HTTPS_PROXY": "http://egress:3128"}),
                                   "offline", "settle"])
        self.assertEqual(box.users, ["0"])
        with open(os.path.join(self.trial, "setup", "prepare.txt")) as f:
            self.assertEqual(f.read(), "ok")
        with open(os.path.join(self.trial, "setup", "egress.log")) as f:
            self.assertIn("refused", f.read())

    def test_without_the_network_it_has_no_proxy(self):
        box = FakeBox()
        self.r.prepare(dict(self.step, network=False), box, self.trial)
        self.assertNotIn("online", box.did)
        self.assertEqual(box.did[1], ("run", "sh", {}))

    def test_a_failed_preparation_is_a_setup_error(self):
        box = FakeBox(exit=2)
        with self.assertRaises(run._Ended) as e:
            self.r.prepare(self.step, box, self.trial)
        self.assertEqual(e.exception.kind, "EnvironmentSetupError")
        self.assertIn("exit 2", e.exception.said)
        self.assertIn("offline", box.did)                  # the way out is closed all the same

    def test_a_way_out_that_cannot_be_closed_is_a_setup_error(self):
        box = FakeBox(fail_offline=True)
        with self.assertRaises(run._Ended) as e:
            self.r.prepare(self.step, box, self.trial)
        self.assertEqual(e.exception.kind, "EnvironmentSetupError")
        self.assertIn("could not be removed", e.exception.said)
        self.assertNotIn("settle", box.did)


class AgentInternet(unittest.TestCase):
    setUp = Prepare.setUp

    def test_task_policy_closes_before_check_on_success_failure_and_cancellation(self):
        rl = container.Unit.__new__(container.Unit)
        rl.doors, rl.reach = [("benchmark-tools", 9000, "/socket")], {"licence:80": "licence-server-1:80"}
        for error in (None, RuntimeError("agent failed"), KeyboardInterrupt()):
            box = FakeBox()
            online = box.online
            def proxy():
                return dict(online(), NO_PROXY="localhost,127.0.0.1,::1")
            box.online = proxy
            try:
                with self.r.agent_internet({"internet": True, "verify": {"keep_processes": True}}, box, rl, self.trial):
                    self.assertEqual(box.agent_proxy["NO_PROXY"], "localhost,127.0.0.1,::1,relay,benchmark-tools,licence-server-1")
                    self.r.in_box(box, {"run": ["solve"]}, collect=False, what="oracle")
                    self.assertEqual(box.did[-1][2], box.agent_proxy)
                    if error:
                        raise error
            except (RuntimeError, KeyboardInterrupt) as caught:
                self.assertIs(caught, error)
            self.assertEqual(box.did[-1], "offline")
            self.assertEqual(box.agent_proxy, {})
            self.r.verify({"run": ["check"], "keep_processes": True}, box, self.trial)
            self.assertNotIn("settle", box.did)
            self.assertEqual(box.did[-1], ("run", "check", {}))
            with open(os.path.join(self.trial, "agent", "egress.log")) as f:
                self.assertIn("refused", f.read())

    def test_unrequested_internet_does_nothing_and_failed_teardown_prevents_verification(self):
        for policy in ({}, {"internet": False}):
            box = FakeBox()
            with self.r.agent_internet(policy, box, None, self.trial):
                pass
            self.assertEqual(box.did, [])
        box = FakeBox(fail_offline=True)
        box.online = lambda: {"NO_PROXY": "localhost"}
        with self.assertRaises(run._Ended) as caught:
            with self.r.agent_internet({"internet": True}, box, None, self.trial):
                pass
            self.fail("verification must not start")
        self.assertEqual(caught.exception.kind, "VerifierError")
        self.assertEqual(box.agent_proxy, {})

    def test_an_existing_proxy_is_not_started_twice(self):
        box = container.Box("test", "u0001", "img", workdir="/")
        with mock.patch.object(container, "base_image", return_value="base"), \
                mock.patch.object(container, "_docker") as docker, mock.patch.object(container, "wait_listening") as ready:
            first = box.online()
            started = list(docker.call_args_list)
            self.assertEqual(box.online(), first)
            self.assertEqual(docker.call_args_list, started)
        ready.assert_called_once()


class Rule(unittest.TestCase):
    def test_only_global_addresses_are_reached(self):
        for no in ("169.254.169.254", "100.100.100.200", "10.1.2.3", "172.17.0.1", "192.168.1.1", "127.0.0.1",
                   "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00::1", "::ffff:169.254.169.254", "not an ip"):
            self.assertFalse(egress.public(no), no)
        for yes in ("1.1.1.1", "151.101.0.223", "2606:4700:4700::1111"):
            self.assertTrue(egress.public(yes), yes)

    def test_a_request_names_its_destination(self):
        self.assertEqual(egress.target(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443")[:3], ("CONNECT", "pypi.org", 443))
        m, host, port, head = egress.target(b"GET http://archive.example/ubuntu/x?y=1 HTTP/1.1\r\nHost: archive.example\r\n"
                                            b"Proxy-Connection: keep-alive\r\nUser-Agent: apt")
        self.assertEqual((m, host, port), ("GET", "archive.example", 80))
        self.assertTrue(head.startswith(b"GET /ubuntu/x?y=1 HTTP/1.1\r\n"))
        self.assertIn(b"Connection: close\r\n", head)
        self.assertNotIn(b"Proxy-Connection", head)
        self.assertTrue(head.endswith(b"\r\n\r\n"))
        with self.assertRaises(ValueError):
            egress.target(b"GET /relative HTTP/1.1\r\nHost: x")


class Refusals(unittest.TestCase):
    """The proxy itself, served on this machine: a private destination, by
    address or by a name resolving to one, is refused before any connection."""

    def setUp(self):
        said = mock.patch.object(egress, "_say", lambda event, **kw: self.said.append((event, kw)))
        self.said = []
        said.start()
        self.addCleanup(said.stop)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.addCleanup(self.srv.close)

        def loop():
            while True:
                try:
                    c, _ = self.srv.accept()
                except OSError:
                    return
                threading.Thread(target=egress._serve, args=(c,), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()

    def ask(self, raw: bytes) -> bytes:
        with socket.create_connection(self.srv.getsockname(), timeout=10) as s:
            s.sendall(raw)
            out = b""
            while True:
                d = s.recv(4096)
                if not d:
                    return out
                out += d

    def test_metadata_private_and_this_machine_are_refused(self):
        for raw in (b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: 169.254.169.254\r\n\r\n",
                    b"CONNECT 100.100.100.200:443 HTTP/1.1\r\n\r\n",
                    b"GET http://10.0.0.1/ HTTP/1.1\r\n\r\n",
                    b"CONNECT localhost:%d HTTP/1.1\r\n\r\n" % self.srv.getsockname()[1]):
            self.assertTrue(self.ask(raw).startswith(b"HTTP/1.1 403"), raw)
        self.assertEqual([e for e, _ in self.said], ["refused"] * 4)

    def test_what_is_not_a_proxy_request_is_refused(self):
        self.assertTrue(self.ask(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 400"))


class TerminalBench(unittest.TestCase):
    """The driver's setup, over a made-up upstream: processes are kept for the
    tasks whose tests need a service the agent left running, and only them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for t in ("mailman", "regex-log", "build-pmars", "nginx-request-logging"):
            os.makedirs(os.path.join(self.tmp, t))
            with open(os.path.join(self.tmp, t, "instruction.md"), "w") as f:
                f.write("do it\n")
            with open(os.path.join(self.tmp, t, "task.toml"), "w") as f:
                f.write("[verifier]\ntimeout_sec = 300\n")

    def setup_of(self, unit):
        r = subprocess.run(["python3", os.path.join(TB, "driver.py")], input=json.dumps({"op": "setup", "unit": unit, "dir": self.tmp}) + "\n",
                           env={"PATH": os.environ["PATH"], "NULLIUS_UPSTREAM": self.tmp}, stdout=subprocess.PIPE, text=True, timeout=60)
        return json.loads(r.stdout)

    def test_a_service_task_keeps_its_processes_and_the_rest_do_not(self):
        kept = self.setup_of("mailman")["verify"]
        self.assertIs(kept["keep_processes"], True)
        self.assertEqual(container.step_problems(kept, collect=True), [])
        plain = self.setup_of("regex-log")["verify"]
        self.assertNotIn("keep_processes", plain)
        self.assertEqual(container.step_problems(plain, collect=True), [])
        self.assertNotIn("internet", self.setup_of("regex-log"))
        self.assertIs(self.setup_of("build-pmars")["internet"], True)
        self.assertIs(self.setup_of("nginx-request-logging")["verify"]["keep_processes"], True)

    def test_all_tasks_have_image_pins_and_only_the_36_added_tasks_get_internet(self):
        data = runpy.run_path(os.path.join(TB, "driver.py"), run_name="metadata")
        self.assertEqual((len(data["ALL"]), len(data["INTERNET"]), len(data["OFFLINE"])), (89, 36, 53))
        self.assertFalse(set(data["INTERNET"]) & set(data["OFFLINE"]))
        self.assertEqual(set(data["ALL"]), set(data["IMAGES"]))
        for image in data["IMAGES"].values():
            self.assertEqual(container.spec_problems({"pull": image}), [])
        self.assertEqual(data["DATASETS"]["full"], data["ALL"])

    def test_every_task_is_prepared_with_tmux_through_the_proxy(self):
        for unit in ("mailman", "regex-log"):
            prep = self.setup_of(unit)["prepare"]
            self.assertEqual(container.step_problems(prep, collect=False), [])
            self.assertIs(prep["network"], True)
            self.assertIn("tmux", prep["run"][-1])

    def test_the_tmux_script_leaves_an_image_that_has_it_alone(self):
        script = self.setup_of("regex-log")["prepare"]["run"][-1]
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        for name in ("tmux", "apt-get"):                      # tmux here; apt-get would record being called
            with open(os.path.join(bin_, name), "w") as f:
                f.write(f"#!/bin/sh\necho {name} called >> {self.tmp}/called\n")
            os.chmod(os.path.join(bin_, name), 0o755)
        r = subprocess.run(["sh", "-c", script], env={"PATH": bin_ + ":/usr/bin:/bin"}, stdout=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("already here", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "called")))


def _docker_ready() -> str:
    return docker_tier.why_not()


def _online() -> bool:
    try:
        urllib.request.urlopen("https://pypi.org/simple/", timeout=10).close()
        return True
    except OSError:
        return False


@unittest.skipIf(_docker_ready(), _docker_ready() or "")
class InDocker(unittest.TestCase):
    """A box of the base image (it has curl): what an agent leaves running is
    gone before the check, and the check's way out reaches public addresses only."""

    def setUp(self):
        self.box = container.Box("nullius-test-check-safety", "u0001", container.base_tag(), workdir="/").start()
        self.addCleanup(self.box.stop)

    def test_a_loop_left_running_is_gone_before_the_check(self):
        self.box.run(["sh", "-c", "(while :; do date > /tmp/alive; sleep 0.2; done) >/dev/null 2>&1 & "
                                  "(sh -c 'while :; do sleep 1; done' &) ; echo left"], 30)
        stopped = self.box.settle()
        self.assertTrue(any(s.endswith(" sh") for s in stopped), stopped)
        r = self.box.run(["sh", "-c", "rm -f /tmp/alive; sleep 1; test ! -e /tmp/alive && echo gone"], 30)
        self.assertEqual(r["output"].strip(), "gone")
        self.assertEqual(self.box.settle(), [])             # and the keep-alive still keeps the box up
        self.assertEqual(self.box.run(["true"], 30)["exit"], 0)

    def test_the_checks_way_out_reaches_public_addresses_only(self):
        env = self.box.online()
        curl = lambda url, *extra: self.box.run(["curl", "-sS", *extra, "-o", "/dev/null", "-w", "%{http_code}",
                                                 "--max-time", "15", url], 30, env=env)["output"].strip()[-3:]
        for url in ("http://169.254.169.254/latest/meta-data/", "http://100.100.100.200/", "http://10.0.0.1/",
                    "https://169.254.169.254/"):
            self.assertIn(curl(url), ("403", "000"), url)
        self.assertIn("refused", self.box.egress_said())
        self.assertNotEqual(curl("http://1.1.1.1/", "--noproxy", "*"), "200")       # no route but the proxy
        if _online():
            self.assertEqual(curl("https://pypi.org/simple/"), "200")

    def test_a_preparations_way_out_is_gone_before_the_agent(self):
        name = self.box.name + "-egress"
        env = self.box.online()
        self.assertEqual(self.box.run(["getent", "hosts", "egress"], 30)["exit"], 0)
        self.box.offline()
        self.assertIsNone(self.box.egress)
        listed = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^/{name}$"], stdout=subprocess.PIPE, text=True)
        self.assertEqual(listed.stdout.strip(), "")
        self.assertNotEqual(self.box.run(["getent", "hosts", "egress"], 30)["exit"], 0)     # no egress name left
        r = self.box.run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10",
                          "https://pypi.org/simple/"], 30, env=env)
        self.assertNotEqual(r["output"].strip()[-3:], "200")                                 # nor a route out
        self.assertNotEqual(self.box.run(["curl", "-sS", "--max-time", "10", "--noproxy", "*", "http://1.1.1.1/"], 30)["exit"], 0)


if __name__ == "__main__":
    unittest.main()
