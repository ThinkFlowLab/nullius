"""A driver's check never runs the agent's code, or follows its links, on this machine when the agent ran in docker.

The driver stays on the machine on either backend; on docker the agent's
workspace is bind-mounted into its container, so whatever the agent left
there is the agent's, not ours. The workspace kit's fix check runs the test
through a throwaway container of the unit's image (``NULLIUS_CHECK_IMAGE``),
and no check reads through a link the agent planted.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nullius.standard import container, definitions, jsonl
from tests import APP

KIT = os.path.join(definitions.KITS, "agent-workspace-v1", "driver.py")
HUMANEVAL = os.path.join(APP, "nullius", "catalogue", "benchmarks", "humaneval", "driver.py")


class WorkspaceKit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-kit-check-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = os.path.join(self.tmp, "ws"); os.makedirs(self.ws)
        self.bin = os.path.join(self.tmp, "bin"); os.makedirs(self.bin)
        self.argv_log = os.path.join(self.tmp, "docker-argv.json")
        with open(os.path.join(self.bin, "docker"), "w") as f:          # records how it was asked, answers as the test would
            f.write(f"#!{sys.executable}\nimport json, sys\njson.dump(sys.argv[1:], open({self.argv_log!r}, 'w'))\nprint('OK')\n")
        os.chmod(os.path.join(self.bin, "docker"), 0o755)

    def driver(self, **env):
        d = jsonl.Proc([sys.executable, KIT], self.tmp, dict(os.environ, PYTHONPATH=str(APP), **env),
                       os.path.join(self.tmp, "driver.log")).start()
        self.addCleanup(d.close)
        return d

    def plant_calc(self):
        marker = os.path.join(self.tmp, "ran-on-the-host")
        with open(os.path.join(self.ws, "calc.py"), "w") as f:
            f.write(f"open({marker!r}, 'w').write('x')\ndef add(a, b):\n    return a + b\n")
        return marker

    def test_on_docker_the_fix_check_runs_in_a_container_of_the_units_image(self):
        d = self.driver(**{container.CHECK_IMAGE: "nullius/base:test", "PATH": self.bin + os.pathsep + os.environ["PATH"]})
        d.request("setup", unit="fix", dir=self.ws, timeout=10)
        marker = self.plant_calc()
        reply = d.request("score", unit="fix", dir=self.ws, transcript="", answer="", timeout=10)
        self.assertFalse(os.path.exists(marker))                        # the agent's calc.py was never imported here
        self.assertTrue(reply["passed"])
        argv = json.loads(Path(self.argv_log).read_text())
        self.assertEqual(argv[:2], ["run", "--rm"])
        self.assertIn("none", argv[argv.index("--network") + 1:argv.index("--network") + 2])
        self.assertIn(f"{self.ws}:{self.ws}:rw", argv)
        self.assertIn("nullius/base:test", argv)

    def test_locally_the_fix_check_runs_here_as_the_agent_did(self):
        d = self.driver()
        d.request("setup", unit="fix", dir=self.ws, timeout=10)
        marker = self.plant_calc()
        self.assertTrue(d.request("score", unit="fix", dir=self.ws, transcript="", answer="", timeout=10)["passed"])
        self.assertTrue(os.path.exists(marker))
        self.assertFalse(os.path.exists(self.argv_log))

    def test_a_planted_link_is_not_followed(self):
        secret = os.path.join(self.tmp, "settings.json")
        with open(secret, "w") as f:
            f.write("nullius-ok")                                       # would even pass, read through the link
        os.symlink(secret, os.path.join(self.ws, "result.txt"))
        reply = self.driver().request("score", unit="write", dir=self.ws, transcript="", answer="", timeout=10)
        self.assertEqual((reply["passed"], reply["detail"]["result.txt"]), (False, None))
        bashrc = Path(self.tmp, "bashrc"); bashrc.write_text("mine\n")
        os.symlink(bashrc, os.path.join(self.ws, "test_calc.py"))      # the test is rewritten in place, never through a link
        self.driver().request("score", unit="fix", dir=self.ws, transcript="", answer="", timeout=10)
        self.assertEqual(bashrc.read_text(), "mine\n")


class HumanEval(unittest.TestCase):
    def test_a_linked_solution_is_no_solution(self):
        src = Path(HUMANEVAL).read_text()
        self.assertIn("os.path.islink(path) or not os.path.isfile(path)", src)   # the upstream it imports is not installed here


if __name__ == "__main__":
    unittest.main()


class AcpFiles(unittest.TestCase):
    """An agent in a container has its workspace mounted: serving its file
    requests from this machine would let it swap a folder for a link between
    the check and the open, and read or write any file of the user's."""

    def client_fs(self, backend):
        from unittest import mock
        from nullius.standard import acp, run
        seen = {}
        class Stop(Exception):
            pass
        def client(*a, **kw):
            seen.update(kw)
            raise Stop
        r = run.Run.__new__(run.Run)
        r._stopped = __import__("threading").Event()
        r.other = {"start": {"acp": ["agent-acp"]}, "_here": "/x"}
        r.inst, r.model, r.options, r.backend = "/inst", "m", {}, backend
        rl = mock.Mock(unit_url="http://relay")
        tmp = tempfile.mkdtemp(prefix="nullius-acp-fs-"); self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(run, "agent_env", return_value={"PATH": "/bin"}), \
                mock.patch.object(run.Run, "place", lambda self, rl, cmd, env, ws, home, box=None: (cmd, env)), \
                mock.patch.object(acp, "Client", client), self.assertRaises(Stop):
            r.drive_agent("u", os.path.join(tmp, "ws"), os.path.join(tmp, "home"), "t", {}, [], rl)
        return seen["fs"]

    def test_files_are_served_to_an_agent_here_only(self):
        self.assertTrue(self.client_fs("local"))
        self.assertFalse(self.client_fs("docker"))


class PlantedLinks(unittest.TestCase):
    """What the agent could write (a folder mounted into its container, what a
    check collects out of it) is read and written here afterwards."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-planted-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.victim = self.tmp / "bashrc"; self.victim.write_text("mine\n")

    def test_only_regular_files_and_folders_stay(self):
        v = self.tmp / "verifier"; (v / "sub").mkdir(parents=True)
        (v / "reward.txt").symlink_to("/proc/sys/net/ipv4/ip_forward")
        (v / "sub" / "out").symlink_to(self.victim)
        (v / "up").symlink_to(self.tmp)
        (v / "kept.txt").write_text("1\n")
        self.assertEqual(sorted(container.plain_only(str(v))), ["reward.txt", "sub/out", "up"])
        self.assertEqual(sorted(p.name for p in v.rglob("*")), ["kept.txt", "sub"])

    def test_a_trial_is_never_written_through_a_link(self):
        from nullius.standard import job
        trial = self.tmp / "unit__1"; (trial / "verifier").mkdir(parents=True); (trial / "agent").mkdir()
        (trial / "verifier" / "score.json").symlink_to(self.victim)
        (trial / "verifier" / "reward.txt").symlink_to(self.victim)
        bench = {"key": "b", "family": "agent", "needs": [], "score": {"kind": "pass", "says": "passed"}}
        job.write_trial(str(trial), job="j", bench=bench, other={"key": "a", "_kind": "agent"}, model="m", unit="u",
                        t0=0, t1=1, produce=(0, 1), score={"reward": 0.0, "detail": {"said": "$(touch pwned)"}},
                        exception=None, calls=[], trajectory=None)
        self.assertEqual(self.victim.read_text(), "mine\n")
        self.assertFalse((trial / "verifier" / "score.json").is_symlink())
        self.assertEqual((trial / "verifier" / "reward.txt").read_text(), "0.0\n")


class CommandAgentGroup(unittest.TestCase):
    """mini-swe-agent, terminus-2, any start.command agent: what it starts in
    the background ends with it, on a timeout and at a normal end alike."""

    def background(self, then: str) -> str:
        tmp = tempfile.mkdtemp(prefix="nullius-group-"); self.addCleanup(shutil.rmtree, tmp, True)
        pidfile = os.path.join(tmp, "child.pid")
        return pidfile, ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; {then}"]

    def alive(self, pidfile) -> bool:
        import time
        pid = int(Path(pidfile).read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            if Path(f"/proc/{pid}/stat").exists() and Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
                return False
            time.sleep(0.02)
        return True

    def test_a_normal_end_ends_what_it_left_running(self):
        from nullius.standard import run
        pidfile, cmd = self.background("echo done")
        r = run.run_group(cmd, 10, stdout=subprocess.PIPE, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "done"))
        self.assertFalse(self.alive(pidfile))

    def test_a_timeout_ends_the_whole_group(self):
        from nullius.standard import run
        pidfile, cmd = self.background("sleep 30")
        with self.assertRaises(subprocess.TimeoutExpired):
            run.run_group(cmd, 0.5, stdout=subprocess.PIPE, text=True)
        self.assertFalse(self.alive(pidfile))
