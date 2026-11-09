"""Shared fixtures for execution tests: local processes, fake SSH and host responses."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nullius import execution, hosts


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-exec-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        environment = mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home"), "NULLIUS_HOME": str(self.tmp / "profile"),
                                                   "NULLIUS_CATALOGUE": str(self.tmp / "catalogue"),
                                                   "SECRETS_DIR": str(self.tmp / "secrets")})
        environment.start(); self.addCleanup(environment.stop)
        self.addCleanup(setattr, execution, "SYNC_EVERY", execution.SYNC_EVERY); execution.SYNC_EVERY = 0.05
        self.addCleanup(execution.EXECUTIONS.clear)
        docker = mock.patch.object(execution, "docker_here", return_value=None); docker.start(); self.addCleanup(docker.stop)

    def wait(self, ex, seconds=10):
        ex.thread.join(seconds); self.assertFalse(ex.thread.is_alive(), "the run did not end")
        return ex

    def until(self, ex, state, seconds=10):
        for _ in range(int(seconds / 0.02)):
            if ex.state == state:
                return ex
            ex.thread.join(0.02)
        self.fail(f"never reached {state}: {ex.state} {ex.lines}")

    def pid_of(self, d, part="a", seconds=10):
        for _ in range(int(seconds / 0.02)):
            try:
                return int((d / f"run-{part}.pid").read_text())
            except (OSError, ValueError):
                time.sleep(0.02)
        self.fail(f"run-{part}.pid never appeared")

    def until_recorded(self, ex, state, seconds=10):
        """The record is saved just after the state the page reads is set:
        wait for the record itself, by when the run's state, question and
        log line are all there."""
        for _ in range(int(seconds / 0.02)):
            try:
                if json.loads((Path(ex.dir) / "run.json").read_text())["state"] == state:
                    return ex
            except (OSError, ValueError, KeyError):
                pass
            ex.thread.join(0.02)
        self.fail(f"never recorded {state}: {ex.state} {ex.lines}")


def gone(pid, seconds=5):
    """True once ``pid`` is no process (a zombie is none: it runs nothing)."""
    for _ in range(int(seconds / 0.02)):
        try:
            with open(f"/proc/{pid}/stat") as fh:
                if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except OSError:
            return True
        time.sleep(0.02)
    return False


# ssh to a host that is this machine: the last word is the command, run as sshd runs it - a session of
# its own, under the host's own home - so a client killed mid-command leaves the command running, as a
# real host does. `late` in that home holds the seconds the launcher waits there before it starts (a slow
# connection: the client may be gone by then); `launched` appears once the launcher is on its way there;
# `slow` the seconds its answer takes to come back; `lag` the seconds every command waits before it runs
# (a host slower to answer than STOP_POLL).
FAKE_SSH = """#!/bin/bash
cmd="${@: -1}"
export HOME=%(home)s
[ -f "$HOME/pq" ] && printf '** WARNING: connection is not using a post-quantum key exchange algorithm.\\r\\n** This session may be vulnerable to "store now, decrypt later" attacks.\\r\\n** The server may need to be upgraded. See https://openssh.com/pq.html\\r\\n' >&2
[ -f "$HOME/lag" ] && sleep "$(cat "$HOME/lag")"
case "$cmd" in *"printf 'ok"*) printf 'ok\\nuv=yes\\ndocker=yes\\nharbor=yes\\nharbor_version=harbor 0.22.0\\ndocker_ok=yes\\nhostname=here\\n'; exit 0;; esac
launcher=no; case "$cmd" in *"setsid nohup"*) launcher=yes;; esac
delay=0; [ $launcher = yes ] && [ -f "$HOME/late" ] && delay=$(cat "$HOME/late")
out=$(mktemp)
setsid bash -c "sleep $delay; $cmd" <&0 >"$out" 2>&1 &
[ $launcher = yes ] && touch "$HOME/launched"
wait $!; rc=$?
[ $launcher = yes ] && [ -f "$HOME/slow" ] && sleep "$(cat "$HOME/slow")" </dev/null >/dev/null 2>&1
cat "$out"; rm -f "$out"; exit $rc
"""


class LocalHost(Base):
    """A host in Settings that is this machine: ssh and rsync stand-ins on the
    PATH, the host's home a directory of its own. What runs there runs for
    real - detached shells, pid files, processes found by their command line."""

    def setUp(self):
        super().setUp()
        # a fifth of the real poll: the slow-host cases wait STOP_POLL * 2 at every command
        self.addCleanup(setattr, execution, "STOP_POLL", execution.STOP_POLL); execution.STOP_POLL = 0.1
        self.home = self.tmp / "host"; (self.home / ".nullius").mkdir(parents=True)
        bin_ = self.tmp / "bin"; bin_.mkdir()
        (bin_ / "ssh").write_text(FAKE_SSH % {"home": self.home}); (bin_ / "ssh").chmod(0o755)
        # The standard prepares a writable catalogue from its sent payload.
        # Materialise that outgoing copy in this fixture's fake host home.
        (bin_ / "rsync").write_text(f"#!{sys.executable}\n" +
            "import pathlib, shutil, sys\nsource, target = sys.argv[-2:]\n" +
            "if ':~/.nullius/app/' in target:\n" +
            f"    destination = pathlib.Path({str(self.home)!r}) / target.split(':~/', 1)[1]\n" +
            "    shutil.copytree(source, destination, dirs_exist_ok=True)\n")
        (bin_ / "rsync").chmod(0o755)
        path = mock.patch.dict(os.environ, {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}"}); path.start(); self.addCleanup(path.stop)

    def plan(self, parts, name="r"):
        return execution.Plan(name=name, script="", parts=parts, host=hosts.parse("bench"))

    def on_host(self, name="r"):
        return self.home / ".nullius" / "runs" / name


class HostBase(Base):
    """A host that answers every command; what was asked of it is recorded."""
    part_log = "harbor: starting\n"

    def setUp(self):
        super().setUp()
        self.have_uv, self.have_harbor = False, True
        self.calls = []
        self.polls = 0
        self.plan = execution.Plan(name="tau3-x3", script='"$NULLIUS_HARBOR" run -p "$NULLIUS_TASKS" --jobs-dir "$NULLIUS_JOBS"', host=hosts.parse("bench"),
                                  tasks=str(self.tmp / "tasks"), model="openai/gpt-5")
        (self.tmp / "tasks").mkdir()
        self.plan.key_var, self.plan.url_var = "OPENAI_API_KEY", ""          # a recipe on OpenAI: no endpoint setting to need

    def answer(self, argv, timeout=0, stdin=None):
        self.calls.append((argv, stdin))
        cmd = argv[-1]
        if argv[0] == "rsync":
            return 0, ""
        if "printf 'ok" in cmd:                                       # the probe
            harbor = "harbor=yes\nharbor_version=harbor 0.22.0\n" if self.have_harbor else "harbor=no\n"
            return 0, f"ok\nuv={'yes' if self.have_uv else 'no'}\ndocker=yes\n{harbor}docker_ok=yes\nhostname=ecs-bench\n"
        if "astral.sh" in cmd:
            self.have_uv = True; return 0, "/home/me/.local/bin/uv\n"
        if "setsid nohup" in cmd:                                     # the launcher: a pid
            return 0, "4242\n"
        if "__nullius__" in cmd:                                      # a look at the running part: log, then exit or alive
            self.polls += 1
            log = self.part_log                                            # the whole log, printed on the first look
            told = log if self.polls == 1 else ""
            return 0, f"\n{execution.LOG_FROM}\n{told}\n{execution.LOG_TO} {len(log.encode())}\n" + "\n__nullius__ " + ("alive" if self.polls < 2 else "0")
        return 0, ""

    def ssh_commands(self):
        return [c[-1] for c, _ in self.calls if c[0] == "ssh"]

    def killed(self, pid, part="run"):
        """A Stop's kill went to the host for ``pid``, by its process group,
        once the host has found it still the part's own (``_own_pids``)."""
        return any("kill -TERM -- -$p" in c and f"for p in {pid}" in c and execution._script_rx(f"run-{part}") in c for c in self.ssh_commands())
