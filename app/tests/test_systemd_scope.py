"""The systemd scope for real: Nullius's own ``constraints.wrap`` and
``constraints.scope_check`` in a container that runs systemd, as a user with
lingering, and in the same image without systemd. Rootless podman runs such a
container as it is. Skipped unless ``NULLIUS_TEST_SYSTEMD`` is set;
``NULLIUS_TEST_SYSTEMD_IMAGE`` names an image already built from
``CONTAINERFILE``, else it is built."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import unittest

VAR, IMAGE_VAR = "NULLIUS_TEST_SYSTEMD", "NULLIUS_TEST_SYSTEMD_IMAGE"
IMAGE = os.environ.get(IMAGE_VAR) or "localhost/nullius-sdr-systemd"
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTAINERFILE = """FROM docker.io/library/fedora:44
RUN dnf -y install --setopt=install_weak_deps=False systemd systemd-pam dbus-broker util-linux procps-ng python3 && dnf clean all \\
 && useradd -m -u 1000 t && mkdir -p /var/lib/systemd/linger && touch /var/lib/systemd/linger/t
CMD ["/sbin/init"]
"""
ASKED = {"cpus": 1.5, "memory_mb": 256, "processes": 64, "cpu_set": "0"}

# run as the user, the package copied in: what Nullius's own code does there, as JSON
AS_USER = """
import json, os, subprocess, time
from nullius import constraints
from nullius.standard import run
asked = json.loads(os.environ["ASKED"])
out = {"check": constraints.scope_check(asked), "given": [os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")]}
plain = {"PATH": os.environ["PATH"]}
cmd, env = constraints.wrap(["sh", "-c", 'd=/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup); '
                             'cat $d/cpu.max $d/memory.max $d/memory.swap.max $d/pids.max; echo $$ $(cut -d" " -f5 /proc/$$/stat); '
                             'echo ${DBUS_SESSION_BUS_ADDRESS:-none}; echo "$1"; exit 7', "sh", "$$ ${HOME}"], plain, asked)
r = run.run_group(cmd, 60, env=env, stdout=subprocess.PIPE, text=True)
out["unit"] = [r.returncode, r.stdout.split(chr(10))]
cmd, env = constraints.wrap(["sh", "-c", "sleep 300 & echo $! >/tmp/child; sleep 300"], plain, asked)
try:
    run.run_group(cmd, 2, env=env)
    out["timed_out"] = False
except subprocess.TimeoutExpired:
    out["timed_out"] = True
time.sleep(1)
out["child_left"] = os.path.exists("/proc/" + open("/tmp/child").read().strip())
out["scopes_left"] = subprocess.run(["systemctl", "--user", "list-units", "--type=scope", "--no-legend", "--plain"],
                                    env={**os.environ, **constraints.session()}, capture_output=True, text=True).stdout.split()
os.environ["SSH_CONNECTION"] = "192.0.2.1 50000 192.0.2.2 22"
out["ssh"] = constraints.scope_check({"cpus": 1})
print(json.dumps(out))
"""


def why_not() -> str:
    if not os.environ.get(VAR):
        return f"the systemd scope runs for real with {VAR}=1 (rootless podman)"
    if not shutil.which("podman"):
        return "podman is not on PATH"
    return ""


@unittest.skipIf(why_not(), why_not())
class RealScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if subprocess.run(["podman", "image", "exists", IMAGE]).returncode:
            subprocess.run(["podman", "build", "-t", IMAGE, "-f", "-"], input=CONTAINERFILE, text=True, check=True)
        cls.name = f"nullius-sdr-{os.getpid()}"
        subprocess.run(["podman", "run", "-d", "--name", cls.name, "--cpus", "2", "--memory", "1g", IMAGE],
                       check=True, capture_output=True)
        for _ in range(60):                          # until the user's manager is up (lingering)
            if subprocess.run(["podman", "exec", cls.name, "systemctl", "is-active", "--quiet", "user@1000.service"]).returncode == 0:
                break
            time.sleep(1)
        subprocess.run(["podman", "cp", os.path.join(APP, "nullius"), f"{cls.name}:/opt/nullius"], check=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["podman", "rm", "-f", "-t", "0", cls.name], capture_output=True)

    def as_user(self, *root_first: str) -> dict:
        """``AS_USER`` as the user ``t`` after ``su -``, without the two variables systemd-run needs (as a trimmed environment is)."""
        for cmd in root_first:
            subprocess.run(["podman", "exec", self.name, "sh", "-c", cmd], check=True)
        r = subprocess.run(["podman", "exec", "-i", self.name, "su", "-", "t", "-c",
                            f"ASKED='{json.dumps(ASKED)}' PYTHONPATH=/opt env -u XDG_RUNTIME_DIR -u DBUS_SESSION_BUS_ADDRESS python3 -"],
                           input=AS_USER, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.splitlines()[-1])

    def test_the_caps_hold_the_group_is_killed_and_lingering_is_asked_under_ssh(self):
        out = self.as_user()
        self.assertEqual(out["given"], [None, None])                                  # derived, not inherited
        self.assertEqual((out["check"]["why"], out["check"]["enforced"]), ("", ["cpus", "memory_mb", "processes"]))
        self.assertIn("cpuset.cpus.effective", out["check"]["refused"]["cpu_set"])      # AllowedCPUs accepted, not applied
        code, lines = out["unit"]
        self.assertEqual(code, 7)
        self.assertEqual(lines[:4], ["150000 100000", str(256 * 1024 * 1024), "0", "64"])
        pid, pgid = lines[4].split()
        self.assertEqual(pid, pgid)                                                    # still its own group's leader
        self.assertEqual(lines[5], "none")                                             # the bus stays systemd-run's
        self.assertEqual(lines[6], "$$ ${HOME}")                                       # systemd expanded nothing
        self.assertTrue(out["timed_out"]); self.assertFalse(out["child_left"])
        self.assertFalse([u for u in out["scopes_left"] if u.startswith("run-")], out["scopes_left"])   # the scopes ended
        self.assertEqual(out["ssh"]["why"], "")                                        # lingering is on
        off = self.as_user("loginctl disable-linger t")
        self.assertEqual(off["ssh"]["why"], "lingering is off: limits would end with the SSH session")
        self.assertEqual(off["check"]["why"], "")                                      # not under SSH: not needed

    def test_without_systemd_the_reason_is_said(self):
        r = subprocess.run(["podman", "run", "--rm", "--user", "1000", "-v", f"{os.path.join(APP, 'nullius')}:/opt/nullius:ro",
                            "-e", "PYTHONPATH=/opt", "--entrypoint", "python3", IMAGE, "-c",
                            "from nullius import constraints; print(constraints.scope_check({'cpus': 1})['why'])"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.stdout.strip(), "systemd does not run this machine", r.stderr)


if __name__ == "__main__":
    unittest.main()
