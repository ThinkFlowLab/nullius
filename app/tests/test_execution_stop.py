"""Stopping runs: process termination, launch races, results and the final job copy."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from nullius import execution, hosts

from tests.execution_support import Base, HostBase, LocalHost, gone


class StopAsItEnds(dict):
    """A run's ``ends`` that lets the page's Stop land the moment a part's end
    is written down: after the run's own look for a Stop, before any pause."""

    def __init__(self, ex):
        super().__init__(); self.ex = ex

    def __setitem__(self, key, value):
        self.ex.stop()
        super().__setitem__(key, value)


class StopBetweenParts(Base):
    """A Stop is honoured whenever it comes: a run never pauses once one is said."""
    PARTS = [{"id": "oracle", "label": "oracle run", "script": "exit 3"}, {"id": "next", "label": "next run", "script": "echo next"}]

    def recorded_states(self, ex):
        """Every state the record took, in order."""
        states, real = [], ex._save
        def save():
            real()
            states.append(json.loads((Path(ex.dir) / "run.json").read_text())["state"])
        ex._save = save
        return states

    def test_a_stop_as_a_part_ends_is_never_paused_over(self):
        ex = execution.Execution(execution.Plan(name="r", script="", parts=self.PARTS), root=str(self.tmp))
        ex.ends = StopAsItEnds(ex)
        states = self.recorded_states(ex)
        self.wait(ex.start(), 5)
        self.assertEqual((ex.state, ex.question), ("stopped", None), ex.lines)
        self.assertNotIn("paused", states[states.index("stopping"):])
        self.assertEqual(states[-1], "stopped")
        self.assertFalse(any("waiting: continue" in l for l in ex.lines))
        self.assertEqual(ex.ends["oracle"]["rc"], 3)                                          # it did end, with its own result
        self.assertFalse((self.tmp / "r" / "run-next.log").exists())

    def test_a_run_stopped_before_any_part_says_it_was_stopped(self):
        """Stopped as the target was made ready: no part ran, the run's exit
        is the one it had (0), and its last line says it was stopped and
        before which part - never a ✓ finished line beside `stopped`."""
        ex = execution.Execution(execution.Plan(name="r", script="", parts=self.PARTS), root=str(self.tmp))
        real = ex._prepare
        ex._prepare = lambda fresh=True: ex.stop() or real(fresh)
        self.wait(ex.start(), 5)
        self.assertEqual((ex.state, ex.exit_code), ("stopped", 0), ex.lines)
        self.assertEqual(ex.lines[-1], "✕ stopped before oracle run: exit 0")
        self.assertFalse(any(l.startswith("✓") for l in ex.lines), ex.lines)
        self.assertFalse((self.tmp / "r" / "run-oracle.log").exists())

    def test_a_stop_as_a_part_is_launched_is_never_written_over(self):
        """Said after the run chose to launch the next part and before the
        launch began: the part never starts, and neither the page nor the
        record reads `running` after `stopping`."""
        ex = execution.Execution(execution.Plan(name="r", script="", parts=self.PARTS[1:]), root=str(self.tmp))
        real = ex._line_break                                      # the launch's first step, before its lock (the run's own start asks it too)
        ex._line_break = lambda: (sys._getframe(1).f_code.co_name == "_launch" and ex.stop()) or real()
        states = self.recorded_states(ex)
        self.wait(ex.start(), 5)
        self.assertEqual((ex.state, ex.exit_code), ("stopped", 0), ex.lines)
        self.assertNotIn("running", states[states.index("stopping"):])
        self.assertEqual(json.loads((self.tmp / "r" / "run.json").read_text())["state"], "stopped")
        self.assertFalse((self.tmp / "r" / "run-next.log").exists())                          # never started
        self.assertEqual(ex.lines[-1], "✕ stopped before next run: exit 0")


class StopAtLaunch(LocalHost):
    """A Stop said while a part's launcher has not answered yet - an ssh that
    takes its time - cuts the launcher short with no pid known. Its detached
    shell may have started on the target already, or may start after: either
    way the Stop ends it."""

    def script(self):
        self.ran = self.tmp / "ran"
        return f"sleep 3; echo ran > {self.ran}"

    def test_a_part_started_by_a_launcher_cut_short_here_is_stopped(self):
        ex = execution.Execution(execution.Plan(name="r", script="", parts=[{"id": "a", "label": "a", "script": self.script()}]), root=str(self.tmp))
        real = ex._sh
        ex._sh = lambda command, timeout=600.0, stdin=None: real(command + ("; sleep 5 </dev/null >/dev/null 2>&1" if "setsid nohup" in command else ""), timeout, stdin)
        ex.start()
        pid = self.pid_of(self.tmp / "r")                     # started; its launcher not back yet
        ex.stop(); self.wait(ex)
        self.assertEqual((ex.state, ex.exit_code), ("stopped", 130), ex.lines)
        self.assertTrue(gone(pid, 10), "the part outlived the Stop")
        self.assertFalse(self.ran.exists(), "the part ran to its end after the Stop")

    def test_a_part_started_by_a_launcher_cut_short_on_a_host_is_stopped(self):
        (self.home / "slow").write_text("5")
        ex = execution.Execution(self.plan([{"id": "a", "label": "a", "script": self.script()}]), root=str(self.tmp / "runs"), key="k" * 20).start()
        pid = self.pid_of(self.on_host())
        ex.stop(); self.wait(ex)
        self.assertEqual((ex.state, ex.exit_code), ("stopped", 130), ex.lines)
        self.assertTrue(gone(pid, 10), "the part outlived the Stop")
        self.assertFalse(self.ran.exists(), "the part ran to its end after the Stop")

    def test_a_launcher_that_starts_the_part_after_the_stop_starts_nothing(self):
        """The ssh is cut short before the host has run the launcher, which
        goes on there all the same and starts the part once the Stop's kill
        has been and gone: the part sees the Stop and does not run."""
        (self.home / "late").touch(); self.addCleanup((self.home / "go").touch)   # a failed test lets the launcher go
        ex = execution.Execution(self.plan([{"id": "a", "label": "a", "script": self.script()}]), root=str(self.tmp / "runs"), key="k" * 20).start()
        self.addCleanup(lambda: (ex.stop(), ex.thread.join(10)))                  # a failed test stops its run
        for _ in range(500):                                                       # a Stop before this kills the ssh, and nothing starts
            if (self.home / "launched").exists():
                break
            time.sleep(0.02)
        else:
            self.fail("the launcher never left the stand-in ssh")
        ex.stop(); self.wait(ex)
        self.assertEqual((ex.state, ex.exit_code), ("stopped", 130), ex.lines)
        self.assertFalse((self.on_host() / "run-a.pid").exists())                 # the Stop came before the part started
        (self.home / "go").touch()
        pid = self.pid_of(self.on_host())                                          # and the launcher started it after all
        self.assertTrue(gone(pid, 10), "the part outlived the Stop")
        self.assertFalse((self.on_host() / "run-a.log").exists(), "the part ran after the Stop")    # its script never began
        self.assertFalse(self.ran.exists(), "the part ran after the Stop")


class StopAfterThePartEnded(LocalHost):
    """A part that ends by itself between two of the run's looks, and a Stop
    pressed before the next look: the Stop reads the part's exit code before
    it ends anything, and that end is recorded as the look would have
    recorded it - the rest of its log, its result in ``ends``, its job done
    or failed - and the run is stopped before the next part. Here, and on a
    host slower to answer than a Stop gives a command (``STOP_POLL``)."""
    JOBS = [{"name": "r-a", "agent": "a"}, {"name": "r-b", "agent": "b"}]

    def test_a_stop_pressed_after_the_part_ended_keeps_its_result(self):
        (self.home / "lag").write_text(str(execution.STOP_POLL * 2))
        execution.SYNC_EVERY = 60                         # the run's next look is far off: the Stop comes before it
        for target in ("here", "host"):
            for rc in (0, 3):
                with self.subTest(target, rc=rc):
                    name = f"{target}-{rc}"; root = self.tmp / "roots" / name; gate, marker = root / "gate", root / "b-ran"
                    root.mkdir(parents=True); self.addCleanup(lambda g=gate: g.exists() or g.write_text(""))   # a never outlives the test
                    parts = [{"id": "a", "label": "a", "script": f"echo a-started; while [ ! -e {gate} ]; do sleep 0.02; done; echo a-out; exit {rc}"},
                             {"id": "b", "label": "b", "script": f"echo b > {marker}"}]
                    plan = execution.Plan(name=name, script="", parts=parts, jobs=self.JOBS, host=hosts.parse("bench") if target == "host" else None)
                    ex = execution.Execution(plan, root=str(root), key="k" * 20)
                    looked, real = threading.Event(), ex._sh
                    def sh(command, timeout=600.0, stdin=None, real=real, looked=looked):
                        out = real(command, timeout, stdin)
                        if "__nullius__" in command and out[0] == 0:
                            looked.set()                      # the run has looked at the part while it ran
                        return out
                    ex._sh = sh
                    execution.EXECUTIONS[ex.id] = ex
                    ex.start()
                    self.assertTrue(looked.wait(20), ex.lines)
                    d = root / name if target == "here" else self.on_host(name)
                    gate.write_text("")
                    for _ in range(500):                                         # a ends by itself, before the run looks again
                        if (d / "run-a.exit").exists():
                            break
                        time.sleep(0.02)
                    self.assertEqual((d / "run-a.exit").read_text().strip(), str(rc))
                    ex.stop(); self.wait(ex, 20)
                    self.assertEqual((ex.state, ex.exit_code), ("stopped", rc), ex.lines)
                    self.assertEqual({p: v["rc"] for p, v in ex.ends.items()}, {"a": rc})
                    self.assertEqual(ex.job_state("r-a")[0], "done" if rc == 0 else "failed")      # a's own result
                    self.assertEqual(ex.job_state("r-b")[0], "stopped")                              # b never ran: the run's
                    self.assertIn("a-out", ex.lines)                                                 # a's log read to its end
                    self.assertEqual(ex.lines[-1], f"✕ stopped before b: exit {rc}")                  # stopped: never a ✓ line
                    saved = json.loads((root / name / "run.json").read_text())
                    self.assertEqual((saved["state"], saved["exit_code"], saved["part"], saved["pid"], saved["ends"]["a"]["rc"]), ("stopped", rc, 1, None, rc))
                    self.assertFalse(any(l.startswith("— running b") for l in ex.lines))
                    self.assertFalse((d / "run-b.log").exists()); self.assertFalse(marker.exists())


class StopAsThePartEnds(LocalHost):
    """A part that ends by itself in the instant between the Stop's look for
    its exit code and its kill - here forced: the Stop's command opens the
    part's gate right after that look and waits for its exit code - keeps
    its own result: the exit code is read again after the kill. A part the
    kill ended writes none: its detached shell dies with it."""
    JOBS = [{"name": "r-a", "agent": "a"}, {"name": "r-b", "agent": "b"}]

    def test_a_part_that_ends_between_the_look_and_the_kill_keeps_its_result(self):
        for target in ("here", "host"):
            with self.subTest(target):
                name = target; root = self.tmp / "roots" / name; gate = root / "gate"; root.mkdir(parents=True)
                self.addCleanup(lambda g=gate: g.exists() or g.write_text(""))
                parts = [{"id": "a", "label": "a", "script": f"while [ ! -e {gate} ]; do sleep 0.02; done; exit 3"},
                         {"id": "b", "label": "b", "script": "exit 0"}]
                plan = execution.Plan(name=name, script="", parts=parts, jobs=self.JOBS, host=hosts.parse("bench") if target == "host" else None)
                ex = execution.Execution(plan, root=str(root), key="k" * 20)
                real = ex._run_plain
                def plain(argv, timeout=600.0, stdin=None, real=real, gate=gate):
                    if "__part__" in argv[-1]:                 # the Stop's command: the part ends after its look, before its kill
                        assert argv[-1].count(": >run-a.stop") == 1
                        argv = argv[:-1] + [argv[-1].replace(": >run-a.stop", f": >{gate}; while [ ! -e run-a.exit ]; do sleep 0.02; done; : >run-a.stop")]
                    return real(argv, timeout, stdin)
                ex._run_plain = plain
                execution.EXECUTIONS[ex.id] = ex
                ex.start()
                for _ in range(1000):
                    if ex.pid:
                        break
                    time.sleep(0.02)
                self.assertTrue(ex.pid, ex.lines)
                ex.stop(); self.wait(ex, 20)
                self.assertEqual((ex.state, ex.exit_code, {p: v["rc"] for p, v in ex.ends.items()}), ("stopped", 3, {"a": 3}), ex.lines)
                self.assertEqual(ex.job_state("r-a")[0], "failed")                          # a's own result, not the Stop's
                self.assertFalse((root / name / "run-b.log").exists())


# rsync from a host that is this machine: a copy back from the host's home, as slow to answer as
# `rsync-lag` there says (a host slower than a Stop gives a command), touching `copied` once it has copied.
FAKE_RSYNC = """#!/bin/bash
src="${@: -2:1}"; dst="${@: -1}"
[ -f %(home)s/rsync-lag ] && sleep "$(cat %(home)s/rsync-lag)"
case "$src" in *:*) src="%(home)s${src#*:\\~}"; mkdir -p "$dst" && cp -R "$src." "$dst" || exit 23; touch %(home)s/copied;; esac
exit 0
"""


class StopCopiesTheJobBack(LocalHost):
    """A Stop on a host copies the job back one last time before the run is
    stopped: a trial that ended there after the run's last look is here,
    and said. The copy goes through the plain runner, since the Stop, set
    already, would cut a host slower than ``STOP_POLL`` short. Live, and
    finished by a restart."""
    TRIAL = '{"event":"trial.end","trial":"t1","task":"task-1","outcome":"resolved","reward":1.0}'

    def setUp(self):
        super().setUp()
        (self.tmp / "bin" / "rsync").write_text(FAKE_RSYNC % {"home": self.home})
        (self.home / "rsync-lag").write_text(str(execution.STOP_POLL * 2))
        execution.SYNC_EVERY = 60                          # the run's next look is far off: the Stop comes before it

    def test_a_trial_that_ended_on_the_host_after_the_last_look_is_copied_back(self):
        root, gate, wrote = self.tmp / "runs", self.tmp / "gate", self.tmp / "wrote"
        self.addCleanup(lambda: gate.exists() or gate.write_text(""))
        script = (f"while [ ! -e {gate} ]; do sleep 0.02; done; mkdir -p \"$NULLIUS_JOBS/r-a\"; "
                  f"echo '{self.TRIAL}' >>\"$NULLIUS_JOBS/r-a/events.jsonl\"; : >{wrote}; sleep 30")
        ex = execution.Execution(self.plan([{"id": "a", "label": "a", "script": script}]), root=str(root), key="k" * 20).start()
        for _ in range(1000):                              # the run's first look has copied the job back
            if (self.home / "copied").exists():
                break
            time.sleep(0.02)
        self.assertTrue((self.home / "copied").exists(), ex.lines)
        gate.write_text("")
        for _ in range(500):
            if wrote.exists():
                break
            time.sleep(0.02)
        self.assertTrue(wrote.exists())
        ex.stop(); self.wait(ex, 20)
        self.assertEqual((ex.state, ex.exit_code), ("stopped", 130), ex.lines)
        self.assertTrue((root / "r" / "jobs" / "r-a" / "events.jsonl").exists(), ex.lines)
        said = [i for i, l in enumerate(ex.lines) if "r-a · task-1 · resolved" in l]
        self.assertEqual(len(said), 1, ex.lines); self.assertLess(said[0], len(ex.lines) - 1)   # said before the run's last line

    def test_a_stop_a_restart_finishes_copies_the_job_back(self):
        root = self.tmp / "runs"; (root / "r").mkdir(parents=True)
        d = self.on_host(); (d / "jobs" / "r-a").mkdir(parents=True)
        (d / "jobs" / "r-a" / "events.jsonl").write_text(self.TRIAL + "\n")
        dead = subprocess.Popen(["true"]); dead.wait()
        for ext, text in (("sh", "exit 0\n"), ("launch", ""), ("pid", f"{dead.pid}\n"), ("log", "a-out\n"), ("exit", "0\n")):
            (d / f"run-a.{ext}").write_text(text)
        plan = self.plan([{"id": "a", "label": "a", "script": "exit 0"}, {"id": "b", "label": "b", "script": "exit 0"}])
        (root / "r" / "run.json").write_text(json.dumps({"id": "r-1", "plan": plan.to_dict(), "state": "stopping", "step": "run", "part": 0,
                                                         "pid": None, "log_offset": 0, "ends": {}, "harbor": "harbor", "tasks_on_target": ""}))
        back = execution.restore(str(root), key="k" * 20)
        self.assertEqual(len(back), 1); again = self.wait(back[0], 20)
        self.assertEqual((again.state, again.exit_code), ("stopped", 0), again.lines)
        self.assertTrue((root / "r" / "jobs" / "r-a" / "events.jsonl").exists(), again.lines)
        self.assertEqual(sum("r-a · task-1 · resolved" in l for l in again.lines), 1, again.lines)


class Stop(HostBase):
    """The Stop button: said at once, finished by the run's own
    thread; a command in flight gives way instead of holding the stop for
    its timeout; a restart in the middle of a stop finishes it."""

    def test_a_command_in_flight_gives_way_to_a_stop(self):
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        t0 = time.monotonic()
        rc, out = execution.subprocess_run(["sleep", "30"], timeout=30, stop=stop)
        self.assertEqual((rc, out), (130, "stopped")); self.assertLess(time.monotonic() - t0, 5)
        rc, out = execution.subprocess_run(["sleep", "30"], timeout=0.4, stop=threading.Event())
        self.assertEqual(rc, 124); self.assertIn("no answer within 0 s", out)
        self.assertEqual(execution.subprocess_run(["cat"], timeout=5, stdin="fed\n", stop=threading.Event()), (0, "fed\n"))

    def test_stop_is_said_at_once_while_the_host_does_not_answer(self):
        self.have_uv = self.have_harbor = True
        held = threading.Event(); polled = threading.Event()
        def answer(argv, timeout=0, stdin=None):
            if argv[0] != "rsync" and "__nullius__" in argv[-1]:
                polled.set(); held.wait(10)                          # an ssh that hangs, until the test lets it go
                return 255, "connection lost"
            return self.answer(argv, timeout, stdin)
        ex = execution.Execution(self.plan, root=str(self.tmp / "runs"), run=answer, key="k" * 20).start()
        self.assertTrue(polled.wait(5))
        ex.stop()
        self.assertEqual(ex.state, "stopping"); self.assertIn("— stop requested", ex.lines)
        self.assertEqual(json.loads((self.tmp / "runs" / "tau3-x3" / "run.json").read_text())["state"], "stopping")
        self.assertIn(ex.state, execution.ALIVE)                    # still listed among the live runs
        self.assertTrue(ex.thread.is_alive())                      # the stop does not wait on the host
        held.set(); self.wait(ex)
        self.assertEqual((ex.state, ex.exit_code, ex.pid), ("stopped", 130, None))
        self.assertTrue(self.killed(4242))
        self.assertEqual(ex.lines[-1], "✕ stopped during the run: exit 130")
        ex.stop()                                                   # a second stop is nothing
        self.assertEqual(ex.state, "stopped")

    def test_a_stop_cut_short_by_a_restart_is_finished_not_resumed(self):
        self.have_uv = self.have_harbor = True
        ex = execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer, key="k" * 20)
        ex._save()
        rec = json.loads((self.tmp / "runs" / "tau3-x3" / "run.json").read_text())
        rec.update(state="stopping", step="run", part=0, pid=4242)
        (self.tmp / "runs" / "tau3-x3" / "run.json").write_text(json.dumps(rec))
        execution.EXECUTIONS.clear(); self.calls.clear()
        back = execution.restore(str(self.tmp / "runs"), run=self.answer, key="k" * 20)
        self.assertEqual(len(back), 1); self.wait(back[0])
        self.assertEqual(back[0].state, "stopped")
        self.assertTrue(self.killed(4242))
        self.assertFalse(any("setsid nohup" in c[-1] for c, _ in self.calls))   # nothing launched


if __name__ == "__main__":
    unittest.main()
