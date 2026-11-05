"""Recovering runs after a restart: records, processes, results and exactly-once launches."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nullius import execution

from tests.execution_support import Base, HostBase, gone


class Restart(Base):
    """A run outlives Nullius: the record on disk, the part still running
    detached, and restore() picking it up."""

    def abandon(self, ex):
        """What a dead Nullius looks like to the run: its thread never acts again
        - a look it had in flight answered or not, it writes no record and no log."""
        ex._sh = lambda *a, **k: threading.Event().wait() or (0, "")
        ex._save = lambda: None; ex.log = lambda line: None
        execution.EXECUTIONS.clear()

    def recorded(self, d, key):
        for _ in range(200):
            try:
                v = json.loads((d / "run.json").read_text()).get(key)
                if v:
                    return v
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        self.fail(f"{key} never recorded")

    def test_a_running_part_is_followed_again_after_a_restart(self):
        d = self.tmp / "r"
        parts = [{"id": "slow", "label": "slow run", "script": "sleep 0.6; echo slow done"}, {"id": "next", "label": "next run", "script": "echo next"}]
        ex = execution.Execution(execution.Plan(name="r", script="", parts=parts), root=str(self.tmp)).start()
        self.until(ex, "running"); self.recorded(d, "pid")
        rec = json.loads((d / "run.json").read_text()); self.assertEqual((rec["state"], rec["part"]), ("running", 0))
        self.abandon(ex)                                       # Nullius goes away; the process on the target lives on
        back = execution.restore(str(self.tmp))
        self.assertEqual([e.plan.name for e in back], ["r"]); again = back[0]
        self.wait(again)
        self.assertEqual((again.state, again.exit_code), ("done", 0), again.lines)
        text = "\n".join(again.lines)
        self.assertIn("Nullius is back; picking the run up at slow run", text); self.assertIn("slow done", text); self.assertIn("next", (d / "run-next.log").read_text())

    def test_a_trial_line_is_said_once_across_a_restart(self):
        """The trials already said are part of the record: a restart says the
        ones that end after it, not again the ones the log holds already."""
        d, gate = self.tmp / "r", self.tmp / "gate"
        self.addCleanup(lambda: gate.exists() or gate.write_text(""))
        end = lambda n: f'{{"event":"trial.end","trial":"t{n}","task":"task-{n}","outcome":"resolved","reward":1.0}}'
        parts = [{"id": "a", "label": "a", "script": f"mkdir -p $NULLIUS_JOBS/r-a; printf '%s\\n' '{end(1)}' '{end(2)}' >> $NULLIUS_JOBS/r-a/events.jsonl"},
                 {"id": "b", "label": "b", "script": f"while [ ! -e {gate} ]; do sleep 0.02; done; mkdir -p $NULLIUS_JOBS/r-b; echo '{end(3)}' >> $NULLIUS_JOBS/r-b/events.jsonl"}]
        ex = execution.Execution(execution.Plan(name="r", script="", parts=parts), root=str(self.tmp)).start()
        for _ in range(500):                                   # a said, b launched: the record of a run between trials
            rec = json.loads((d / "run.json").read_text())
            if rec["part"] == 1 and rec["pid"]:
                break
            time.sleep(0.02)
        self.assertEqual([l for l in ex.lines if "task-" in l], ["  ✓ r-a · task-1 · resolved · reward 1.0", "  ✓ r-a · task-2 · resolved · reward 1.0"])
        self.abandon(ex)
        again = execution.restore(str(self.tmp))[0]
        gate.write_text(""); self.wait(again)
        self.assertEqual((again.state, again.exit_code), ("done", 0), again.lines)
        said = ["  ✓ r-a · task-1 · resolved · reward 1.0", "  ✓ r-a · task-2 · resolved · reward 1.0", "  ✓ r-b · task-3 · resolved · reward 1.0"]
        self.assertEqual([l for l in again.lines if "task-" in l], said)
        self.assertEqual([l for l in (d / "nullius.log").read_text().splitlines() if "task-" in l], said)
        # a record from before the trials were kept loads, with none said yet: each is said again, as before
        old = json.loads((d / "run.json").read_text()); old.pop("seen_trials", None)
        before = execution.Execution(execution.Plan(name="r", script="", parts=parts), root=str(self.tmp), record=old)
        before._trial_lines()
        self.assertEqual(len([l for l in before.lines if "task-" in l]), 6)

    def test_a_pause_waits_again_after_a_restart(self):
        parts = [{"id": "a", "label": "a", "script": "exit 2"}, {"id": "b", "label": "b", "script": "echo b"}]
        ex = execution.Execution(execution.Plan(name="r", script="", parts=parts), root=str(self.tmp)).start()
        self.until_recorded(ex, "paused"); self.abandon(ex)
        again = execution.restore(str(self.tmp))[0]
        self.until(again, "paused"); self.assertIn("a exited 2", again.question)
        again.continue_(); self.wait(again)
        self.assertEqual(again.state, "done"); self.assertIn("b", (self.tmp / "r" / "run-b.log").read_text())

    def test_a_finished_run_is_listed_and_a_gone_process_is_a_failure(self):
        ex = self.wait(execution.Execution(execution.Plan(name="fin", script="true"), root=str(self.tmp)).start())
        execution.EXECUTIONS.clear()
        # a record that says running with a pid nothing answers to, and no exit file
        gone = self.tmp / "gone"; gone.mkdir(); (gone / "run-run.sh").write_text("true\n"); (gone / "run-run.log").write_text("")
        (gone / "run.json").write_text(json.dumps({"id": "gone-1", "plan": execution.Plan(name="gone", script="true").to_dict(), "state": "running", "part": 0, "pid": 999999, "log_offset": 0}))
        back = execution.restore(str(self.tmp))
        self.assertEqual([e.plan.name for e in back], ["gone"]); self.wait(back[0])
        self.assertEqual(back[0].state, "failed"); self.assertIn("gone without an exit code", "\n".join(back[0].lines))
        self.assertEqual(execution.EXECUTIONS["fin-" + str(int(ex.started))].state if f"fin-{int(ex.started)}" in execution.EXECUTIONS else "done", "done")

    def test_an_ended_runs_log_is_read_when_its_page_asks_not_at_start(self):
        from unittest import mock
        ex = self.wait(execution.Execution(execution.Plan(name="fin", script="echo said-once"), root=str(self.tmp)).start())
        execution.EXECUTIONS.clear()
        with mock.patch.object(execution, "progress") as prog:
            execution.restore(str(self.tmp))
            prog.parse.assert_not_called()                          # nothing of the log read at start-up
        again = execution.EXECUTIONS[ex.id]
        self.assertIn("said-once", "\n".join(again.lines))          # read on first asking, all of it
        self.assertEqual(again.summary()["lines"], len(again.lines))

    def test_a_record_from_before_the_fixed_endpoint_still_restores(self):
        """A plan written before ``base_url`` - an OpenRouter run then took the
        model key and endpoint - loads with none, and goes on with the
        settings it named."""
        old = execution.Plan(name="old", script="true", model="m").to_dict(); del old["base_url"]
        self.assertEqual((old["key_var"], old["url_var"]), ("MODEL_API_KEY", "MODEL_API_URL"))
        (self.tmp / "old").mkdir()
        (self.tmp / "old" / "run.json").write_text(json.dumps({"id": "old-1", "plan": old, "state": "done", "exit_code": 0}))
        execution.restore(str(self.tmp), env={"MODEL_API_KEY": "sk-router-0123456789", "MODEL_API_URL": "https://router.example/v1"})
        ex = execution.EXECUTIONS["old-1"]
        self.assertEqual((ex.state, ex.plan.base_url), ("done", ""))
        self.assertEqual((ex._key, ex._url), ("sk-router-0123456789", "https://router.example/v1"))


class CrashBetweenParts(Base):
    """Nullius can die at any instant. Each record a run saves after its
    first part ended - with the run directory as it was at that save - is
    restored on its own, and must end as the run did uninterrupted: asked
    where it was asked, nothing asked once answered, each part's own result."""

    def live(self, parts):
        """The run uninterrupted, a Continue for its pause; every save kept
        with a copy of the run directory: (the run, the records, the pid each
        part ran as, how many records came before the Continue)."""
        snaps = self.tmp / "snaps"
        ex = execution.Execution(execution.Plan(name="r", script="", parts=parts), root=str(self.tmp / "live"))
        records, pids, real_save, real_launch = [], {}, ex._save, ex._launch
        def save():
            real_save()
            dest = snaps / str(len(records)) / "r"
            shutil.copytree(ex.dir, dest)
            records.append(json.loads((dest / "run.json").read_text()))
        def launch(part):
            ok = real_launch(part); pids[part["id"]] = ex.pid
            return ok
        ex._save, ex._launch = save, launch
        ex.start()
        answered = None
        for _ in range(500):
            if not ex.thread.is_alive():
                break
            if any(r["state"] == "paused" for r in records) and answered is None:
                answered = len(records); ex.continue_()
            ex.thread.join(0.02)
        self.wait(ex)
        return ex, records, pids, answered

    def drive(self, ex):
        """A restored run to its end, answering a pause as the live one was: (the run, whether it asked)."""
        asked = False
        for _ in range(500):
            if not ex.thread.is_alive():
                break
            if ex.state == "paused" and not asked:
                asked = True; ex.continue_()
            ex.thread.join(0.02)
        return self.wait(ex), asked

    def test_a_restart_after_any_save_between_parts_ends_as_the_run_would(self):
        cases = {"both pass": [{"id": "a", "label": "a", "script": "true"}, {"id": "b", "label": "b", "script": "echo b"}],
                 "first fails": [{"id": "a", "label": "a", "script": "exit 2"}, {"id": "b", "label": "b", "script": "echo b"}],
                 "last fails": [{"id": "a", "label": "a", "script": "true"}, {"id": "b", "label": "b", "script": "exit 4"}]}
        for case, parts in cases.items():
            with self.subTest(case):
                self.tmp = Path(tempfile.mkdtemp(prefix="nullius-crash-")); self.addCleanup(shutil.rmtree, self.tmp, True)
                live, records, pids, answered = self.live(parts)
                ends = {k: v["rc"] for k, v in live.ends.items()}
                window = [k for k, rec in enumerate(records) if rec["state"] in execution.ALIVE and "a" in rec["ends"]
                          and (rec["part"] == len(parts) or rec["pid"] != pids[parts[rec["part"]]["id"]])]
                self.assertTrue(window)
                for k in window:
                    execution.EXECUTIONS.clear()
                    back = execution.restore(str(self.tmp / "snaps" / str(k)))
                    self.assertEqual(len(back), 1, (k, records[k]))
                    again, asked = self.drive(back[0])
                    self.assertEqual((again.state, again.exit_code, again.error), (live.state, live.exit_code, live.error), (k, records[k], again.lines))
                    self.assertEqual({p: v["rc"] for p, v in again.ends.items()}, ends, (k, records[k]))
                    self.assertEqual(asked, answered is not None and k < answered, (k, records[k]))
                for k, rec in enumerate(records):
                    # a pid recorded is the pid of the part the record says is running, never one that ended
                    if rec["pid"] is not None:
                        self.assertLess(rec["part"], len(parts), (k, rec))
                        self.assertEqual(rec["pid"], pids[parts[rec["part"]]["id"]], (k, rec))

    def test_a_record_from_before_a_part_s_end_was_one_save_ends_as_the_run_would(self):
        """A record can name part N+1 while holding part N's pid and no ``ends``.
        With only N's exit code on the target, restore resumes the next part."""
        cases = {"both pass": [{"id": "a", "label": "a", "script": "true"}, {"id": "b", "label": "b", "script": "echo b"}],
                 "first fails": [{"id": "a", "label": "a", "script": "exit 2"}, {"id": "b", "label": "b", "script": "echo b"}],
                 "last fails": [{"id": "a", "label": "a", "script": "true"}, {"id": "b", "label": "b", "script": "exit 4"}]}
        for case, parts in cases.items():
            self.tmp = Path(tempfile.mkdtemp(prefix="nullius-old-")); self.addCleanup(shutil.rmtree, self.tmp, True)
            live, _, pids, _ = self.live(parts)
            ran = self.tmp / "live" / "r"
            for n in range(len(parts)):                                 # the save as part n ended
                for with_ends in (True, False):
                    with self.subTest(case, ended=parts[n]["id"], ends=with_ends):
                        d = self.tmp / f"old-{n}-{with_ends}" / "r"; d.mkdir(parents=True)
                        for p in parts[:n + 1]:                         # what the old launcher left: no pid file
                            for ext in ("sh", "log", "exit"):
                                shutil.copy(ran / f"run-{p['id']}.{ext}", d / f"run-{p['id']}.{ext}")
                        rec = {"id": "r-1", "plan": execution.Plan(name="r", script="", parts=parts).to_dict(), "state": "running", "step": "run",
                               "part": n + 1, "pid": pids[parts[n]["id"]], "log_offset": (d / f"run-{parts[n]['id']}.log").stat().st_size,
                               "exit_code": None, "error": None, "question": None, "started": live.started, "finished": None,
                               "harbor": "harbor", "tasks_on_target": ""}
                        if with_ends:
                            rec["ends"] = {p["id"]: dict(live.ends[p["id"]]) for p in parts[:n + 1]}
                        (d / "run.json").write_text(json.dumps(rec))
                        execution.EXECUTIONS.clear()
                        back = execution.restore(str(d.parent))
                        self.assertEqual(len(back), 1)
                        again, asked = self.drive(back[0])
                        self.assertEqual((again.state, again.exit_code, again.error), (live.state, live.exit_code, live.error), again.lines)
                        self.assertEqual({p: v["rc"] for p, v in again.ends.items()},
                                         {p: v["rc"] for p, v in live.ends.items() if with_ends or p in [q["id"] for q in parts[n:]]})
                        self.assertEqual(asked, n + 1 < len(parts) and live.ends[parts[n]["id"]]["rc"] != 0)   # asked where the run asked
                        self.assertNotIn("gone without an exit code", "\n".join(again.lines))
                        self.assertEqual([l for l in again.lines if l.startswith("— running ")], ["— running b on this machine"] if n == 0 else [])   # b launched once, or not again


class StaleProcess(Base):
    """A pid kept in a record outlives the process it named: by the time the
    record is read again it can be anybody's. A Stop signals a pid only while
    it still runs the part's script in the run directory."""

    def stranger(self):
        """An unrelated process, a session leader of its own as a part's detached shell is."""
        p = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def write(self, parts, files, **rec):
        d = self.tmp / "r"; d.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (d / name).write_text(text)
        base = {"id": "r-1", "plan": execution.Plan(name="r", script="", parts=parts).to_dict(), "step": "run", "log_offset": 0,
                "exit_code": None, "error": None, "question": None, "started": time.time(), "finished": None, "harbor": "harbor", "tasks_on_target": ""}
        (d / "run.json").write_text(json.dumps(dict(base, **rec)))
        execution.EXECUTIONS.clear()

    def test_an_old_paused_record_s_pid_is_never_signalled(self):
        """A paused record's ended-part pid must not be signalled on Stop."""
        other = self.stranger()
        parts = [{"id": "a", "label": "a", "script": "exit 2"}, {"id": "b", "label": "b", "script": "echo b"}]
        self.write(parts, {"run-a.sh": "exit 2\n", "run-a.log": "", "run-a.exit": "2\n"}, state="paused", part=1, pid=other.pid,
                   ends={"a": {"rc": 2, "at": time.time(), "label": "a"}}, question="a exited 2. Continue with b, or stop?")
        back = execution.restore(str(self.tmp))
        again = self.until(back[0], "paused")
        record = lambda: json.loads((self.tmp / "r" / "run.json").read_text())
        for _ in range(250):                                                               # the pid is dropped, then saved
            if record()["pid"] is None:
                break
            time.sleep(0.02)
        self.assertIsNone(record()["pid"]); self.assertIsNone(again.pid)                  # the pause's pid dropped: its part ended
        again.stop(); self.wait(again)
        self.assertEqual(again.state, "stopped")
        time.sleep(0.2); self.assertIsNone(other.poll(), "a Stop signalled a process that was not the part's")
        self.assertFalse((self.tmp / "r" / "run-b.log").exists())

    def test_a_running_part_s_pid_given_to_another_process_is_never_signalled(self):
        """The part died while Nullius was away and its pid went to another
        process: a Stop leaves that process alone."""
        other = self.stranger()
        parts = [{"id": "a", "label": "a", "script": "sleep 30"}]
        self.write(parts, {"run-a.sh": "sleep 30\n", "run-a.log": "", "run-a.pid": f"{other.pid}\n"}, state="stopping", part=0, pid=other.pid)
        back = execution.restore(str(self.tmp))
        self.wait(back[0])
        self.assertEqual(back[0].state, "stopped")
        time.sleep(0.2); self.assertIsNone(other.poll(), "a Stop signalled a process that was not the part's")


class CrashAtLaunch(Base):
    """Nullius can die after a part's detached shell has started on the
    target and before its pid is in the record. A restart follows that part
    by the pid it wrote beside its log; it never starts it a second time."""
    CRASH = type("Crash", (Exception,), {})

    def die_after_launching(self, ex, part_id):
        """The run dies the moment the launcher for ``part_id`` has returned:
        the detached shell is running, its pid never saved."""
        real, printed = ex._sh, []
        def sh(command, timeout=600.0, stdin=None):
            out = real(command, timeout, stdin)
            if "setsid nohup" in command and f"run-{part_id}.sh" in command:
                printed.append(int(out[1].strip()))
                ex._save = lambda: None; ex.log = lambda line: None             # nothing more reaches the record or the log
                raise self.CRASH("Nullius is gone")
            return out
        ex._sh = sh
        return printed

    def test_a_part_started_before_its_pid_was_saved_runs_once(self):
        for crashed, settled in (("a", "log"), ("b", "exit")):      # still running at the restart; ended by then
            with self.subTest(crashed):
                root = self.tmp / crashed; marker = root / "ran"
                parts = [{"id": "a", "label": "a", "script": f"echo a >> {marker}; echo a-out; sleep 0.5"},
                         {"id": "b", "label": "b", "script": f"echo b >> {marker}; echo b-out; exit 3"}]
                ex = execution.Execution(execution.Plan(name="r", script="", parts=parts), root=str(root))
                printed = self.die_after_launching(ex, crashed)
                self.wait(ex.start())
                rec = json.loads((root / "r" / "run.json").read_text())
                self.assertEqual((rec["state"], rec["part"], rec["pid"]), ("running", parts.index(next(p for p in parts if p["id"] == crashed)), None))
                for _ in range(1000):                                   # what the target has done by the time Nullius is back
                    if (root / "r" / f"run-{crashed}.{settled}").exists():
                        break
                    time.sleep(0.01)
                else:
                    self.fail(f"run-{crashed}.{settled} never appeared")
                execution.EXECUTIONS.clear()
                back = execution.restore(str(root))
                self.assertEqual(len(back), 1); again = self.wait(back[0])
                self.assertEqual((again.state, again.exit_code, again.error), ("failed", 3, "b exited 3"), again.lines)
                self.assertEqual({p: v["rc"] for p, v in again.ends.items()}, {"a": 0, "b": 3})
                self.assertEqual(marker.read_text(), "a\nb\n")                         # each part ran once
                self.assertIn(f"{crashed}-out", again.lines)                           # its log read from its start
                self.assertIn(f"— following {crashed} again on this machine", again.lines)
                # the pid the detached shell wrote is the one the launcher printed: the one followed, and killed by a Stop
                self.assertEqual(int((root / "r" / f"run-{crashed}.pid").read_text()), printed[0])


class RestoreFindsThePart(Base):
    """A restart that finds a record it cannot take at its word - a part being
    launched, an old record's pid, an old part with no pid file - asks the
    target what is there: the part's claim and pid file, and the processes
    running its script in its run directory."""
    CRASH = type("Crash", (Exception,), {})
    OLD_LAUNCHER = ("cd {d} || exit 1; setsid nohup bash -c 'bash run-{p}.sh </dev/null >run-{p}.log 2>&1; echo $? >run-{p}.exit' "
                    "</dev/null >/dev/null 2>&1 & echo $!")              # A running part without a pid file.

    def setUp(self):
        super().setUp()
        self.marker = self.tmp / "ran"

    def record(self, d, parts, **rec):
        base = {"id": "r-1", "plan": execution.Plan(name="r", script="", parts=parts).to_dict(), "state": "running", "step": "run", "log_offset": 0,
                "exit_code": None, "error": None, "question": None, "started": time.time(), "finished": None, "harbor": "harbor", "tasks_on_target": ""}
        (d / "run.json").write_text(json.dumps(dict(base, **rec)))
        execution.EXECUTIONS.clear()

    def old_part(self, d, part):
        """``part`` started on the target by the launcher from before the pid file."""
        (d / f"run-{part['id']}.sh").write_text(part["script"] + "\n")
        out = subprocess.run(["bash", "-c", self.OLD_LAUNCHER.format(d=d, p=part["id"])], capture_output=True, text=True).stdout
        for _ in range(250):                                                # its detached shell has opened the log
            if (d / f"run-{part['id']}.log").exists():
                return int(out.strip())
            time.sleep(0.02)
        self.fail("the old launcher did not start the part")

    def dead_pid(self):
        p = subprocess.Popen(["true"]); p.wait()
        return p.pid

    def crash_at(self, ex, before_running=False, edit=lambda c: c):
        """The run dies at its first part's launcher: once it has answered, or
        - ``before_running`` - while it is still on its way to the target,
        which it reaches later all the same. (command, stdin) of that launcher."""
        real, sent = ex._sh, []
        def sh(command, timeout=600.0, stdin=None):
            if "setsid nohup" not in command:
                return real(command, timeout, stdin)
            sent.append((edit(command), stdin))
            if not before_running:
                real(edit(command), timeout, stdin)
            ex._save = lambda: None; ex.log = lambda line: None
            raise self.CRASH("Nullius is gone")
        ex._sh = sh
        return sent

    def restored(self, root):
        back = execution.restore(str(root))
        self.assertEqual(len(back), 1)
        self.addCleanup(back[0].stop)
        return back[0]

    # 3 - a restart before the part has written its pid

    def test_a_restart_before_the_part_wrote_its_pid_follows_it_and_never_starts_it_again(self):
        parts = [{"id": "a", "label": "a", "script": f"echo a >> {self.marker}; echo a-out"}]
        ex = execution.Execution(execution.Plan(name="r", script="", parts=parts), root=str(self.tmp))
        # its detached shell slow to write its pid: the restart looks before it has
        self.crash_at(ex, edit=lambda c: c.replace("bash -c 'echo $$ >", "bash -c 'sleep 1; echo $$ >"))
        self.wait(ex.start())
        self.assertFalse((self.tmp / "r" / "run-a.pid").exists())
        again = self.wait(self.restored(self.tmp))
        self.assertEqual((again.state, again.exit_code), ("done", 0), again.lines)
        self.assertEqual(self.marker.read_text(), "a\n")                                     # it ran once
        self.assertIn("a-out", again.lines)
        self.assertIn("— following a again on this machine", again.lines)

    def test_a_launcher_that_reaches_the_target_after_the_restart_started_the_part_starts_nothing(self):
        """Nullius went while the launcher was on its way; the restart found
        nothing and started the part; the launcher then arrives."""
        parts = [{"id": "a", "label": "a", "script": f"echo a >> {self.marker}"}]
        ex = execution.Execution(execution.Plan(name="r", script="", parts=parts), root=str(self.tmp))
        sent = self.crash_at(ex, before_running=True)
        self.wait(ex.start())
        again = self.wait(self.restored(self.tmp))
        self.assertEqual((again.state, again.exit_code), ("done", 0), again.lines)
        command, stdin = sent[0]
        late = subprocess.run(["bash", "-c", command], input=stdin, capture_output=True, text=True).stdout.split()
        for word in late:
            if word.isdigit():
                self.assertTrue(gone(int(word), 10))
        self.assertEqual(self.marker.read_text(), "a\n")                                     # the late launcher started nothing

    def test_a_part_claimed_by_a_launcher_that_never_started_it_is_started_once(self):
        """The claim, and nothing after it - no pid, no process - however long
        the restart waits: the launcher died before it started the part."""
        self.addCleanup(setattr, execution, "LAUNCH_GRACE", execution.LAUNCH_GRACE); execution.LAUNCH_GRACE = 0.6
        d = self.tmp / "r"; d.mkdir()
        parts = [{"id": "a", "label": "a", "script": f"echo a >> {self.marker}"}]
        (d / "run-a.sh").write_text(parts[0]["script"] + "\n"); (d / "run-a.launch").write_text("")
        self.record(d, parts, part=0, pid=None)
        again = self.wait(self.restored(self.tmp))
        self.assertEqual((again.state, again.exit_code), ("done", 0), again.lines)
        self.assertEqual(self.marker.read_text(), "a\n")
        self.assertEqual([l for l in again.lines if l.startswith("— running ")], ["— running a on this machine"])

    # 4 - an old record saved as the next part was launched

    def test_an_old_record_saved_as_the_next_part_was_launched_follows_that_part(self):
        """A record names b but holds a's pid, and b has no pid file.
        Restore finds b by its command line, follows it to completion and
        reads its log from the start."""
        for ended in (False, True):
            for with_ends in (True, False):
                with self.subTest(ended=ended, ends=with_ends):
                    root = self.tmp / f"old-{ended}-{with_ends}"; d = root / "r"; d.mkdir(parents=True); gate = root / "gate"
                    self.addCleanup(lambda g=gate: g.exists() or g.write_text(""))                 # b never outlives the test
                    parts = [{"id": "a", "label": "a", "script": "echo a-out"},
                             {"id": "b", "label": "b", "script": f"while [ ! -e {gate} ]; do sleep 0.02; done; echo b-out; exit 0"}]
                    for name, text in (("run-a.sh", "echo a-out\n"), ("run-a.log", "a-out\n"), ("run-a.exit", "0\n")):
                        (d / name).write_text(text)
                    b = self.old_part(d, parts[1])
                    if ended:
                        gate.write_text(""); self.assertTrue(gone(b, 10)); self.assertTrue((d / "run-b.exit").exists())
                    rec = {"part": 1, "pid": self.dead_pid(), "log_offset": len("a-out\n")}
                    if with_ends:
                        rec["ends"] = {"a": {"rc": 0, "at": time.time(), "label": "a"}}
                    self.record(d, parts, **rec)
                    again = self.restored(root)
                    if not ended:                                                    # b runs on while the restart follows it, then ends
                        for _ in range(500):
                            if "— following b again on this machine" in again.lines or not again.thread.is_alive():
                                break
                            time.sleep(0.02)
                        again.thread.join(0.5); gate.write_text("")
                    self.wait(again)
                    self.assertEqual((again.state, again.exit_code, again.error), ("done", 0, None), again.lines)
                    self.assertEqual({p: v["rc"] for p, v in again.ends.items()}, {"a": 0, "b": 0})
                    self.assertIn("b-out", again.lines)                                  # b's log, from its start
                    self.assertNotIn("gone without an exit code", "\n".join(again.lines))
                    self.assertFalse(any(l.startswith("— running ") for l in again.lines))   # b never started again

    # 5 - an old part gone without an exit code

    def test_an_old_part_gone_without_an_exit_code_fails_and_the_run_goes_on(self):
        """No pid file, no exit code, no process running its script: the part
        is gone, exit 1, as a live run says of a pid that vanished - and the
        run asks, as it would have. So too for a pid since given to another
        process."""
        other = subprocess.Popen(["sleep", "60"], start_new_session=True); self.addCleanup(lambda: (other.kill(), other.wait()))
        parts = [{"id": "a", "label": "a", "script": "echo a-out"}, {"id": "b", "label": "b", "script": "echo b-out"}]
        for case, pid in (("no pid", None), ("a pid now another's", other.pid)):
            with self.subTest(case):
                root = self.tmp / case.replace(" ", "-"); d = root / "r"; d.mkdir(parents=True)
                (d / "run-a.sh").write_text("echo a-out\n"); (d / "run-a.log").write_text("a-out\n")
                self.record(d, parts, part=0, pid=pid)
                again = self.restored(root)
                self.until(again, "paused", 5)
                self.assertIn("a exited 1. Continue with b", again.question)
                self.assertIn("  a is gone without an exit code", again.lines)
                again.continue_(); self.wait(again)
                self.assertEqual({p: v["rc"] for p, v in again.ends.items()}, {"a": 1, "b": 0})
                self.assertIsNone(other.poll())


class RestartOnHost(HostBase):
    """A restart asks the host what a part left in its run directory before
    it trusts the record."""

    def recorded(self, plan, **fields):
        ex = execution.Execution(plan, root=str(self.tmp / "runs"), run=self.answer, key="k" * 20)
        ex._save()
        path = self.tmp / "runs" / plan.name / "run.json"
        rec = json.loads(path.read_text()); rec.update(fields); path.write_text(json.dumps(rec))
        execution.EXECUTIONS.clear(); self.calls.clear()

    def holding(self, files):
        """The host, its run directory holding ``files`` ({name: content})."""
        def answer(argv, timeout=0, stdin=None):
            if argv[0] == "ssh" and "@file" in argv[-1]:
                self.calls.append((argv, stdin))
                return 0, "".join(f"@file {n} {v}\n" for n, v in files.items() if n in argv[-1])
            return self.answer(argv, timeout, stdin)
        return answer

    def test_a_part_started_before_its_pid_was_saved_is_followed_by_the_pid_it_wrote(self):
        self.recorded(self.plan, state="running", step="run", part=0, pid=None, harbor="harbor")
        back = execution.restore(str(self.tmp / "runs"), run=self.holding({"run-run.pid": "4242", "run-run.log": ""}), key="k" * 20)
        self.assertEqual(len(back), 1); self.wait(back[0])
        self.assertEqual((back[0].state, back[0].exit_code), ("done", 0), back[0].lines)
        self.assertFalse(any("setsid nohup" in c[-1] for c, _ in self.calls))                                  # not started a second time
        follows = [c[-1] for c, _ in self.calls if "__nullius__" in c[-1]]
        self.assertTrue(follows and all("for p in 4242 " in c and execution._script_rx("run-run") in c for c in follows))   # 4242, while it runs the part
        self.assertIn("harbor: starting", back[0].lines)                                                         # its log, from its start

    def test_a_stop_cut_short_before_the_pid_was_saved_kills_the_part_it_finds(self):
        self.recorded(self.plan, state="stopping", step="run", part=0, pid=None, harbor="harbor")
        back = execution.restore(str(self.tmp / "runs"), run=self.holding({"run-run.pid": "4242", "run-run.log": ""}), key="k" * 20)
        self.wait(back[0])
        self.assertEqual(back[0].state, "stopped")
        self.assertTrue(self.killed(4242))
        self.assertFalse(any("setsid nohup" in c[-1] for c, _ in self.calls))

    def test_a_record_from_before_a_part_s_end_was_one_save_asks_then_launches_the_next_part(self):
        """The record names b with a's pid and no ``ends``. Only a's exit code
        is on the host; restore asks before launching b."""
        self.plan.parts = [{"id": "a", "label": "a", "script": "exit 2"}, {"id": "b", "label": "b", "script": "true"}]
        self.recorded(self.plan, state="running", step="run", part=1, pid=4242, harbor="harbor")
        back = execution.restore(str(self.tmp / "runs"), run=self.holding({"run-a.pid": "4242", "run-a.log": "", "run-a.exit": "2"}), key="k" * 20)
        again = self.until(back[0], "paused")
        self.assertIn("a exited 2. Continue with b", again.question)
        self.assertFalse(any("__nullius__" in c[-1] for c, _ in self.calls))                                  # b never followed by a's pid
        again.continue_(); self.wait(again)
        self.assertEqual((again.state, again.exit_code), ("done", 0), again.lines)
        self.assertEqual([c[-1].count("run-b.sh") for c, _ in self.calls if "setsid nohup" in c[-1]], [1])        # b launched, once
        self.assertEqual({p: v["rc"] for p, v in again.ends.items()}, {"a": 2, "b": 0})

    def test_a_part_that_left_nothing_is_launched(self):
        self.recorded(self.plan, state="running", step="run", part=0, pid=None, harbor="harbor")
        back = execution.restore(str(self.tmp / "runs"), run=self.holding({}), key="k" * 20)
        self.wait(back[0])
        self.assertEqual(back[0].state, "done", back[0].lines)
        launch = [c[-1] for c, _ in self.calls if "setsid nohup" in c[-1]]
        self.assertEqual(len(launch), 1); self.assertIn("rm -f run-run.exit run-run.pid; ", launch[0])
        self.assertIn("bash -c 'echo $$ >run-run.pid; [ -e run-run.stop ] && exit 130; bash run-run.sh </dev/null >run-run.log 2>&1;", launch[0])   # its pid, a look for a Stop, then the log


class PartPidsWithoutProc(unittest.TestCase):
    """A target with no /proc (not Linux) cannot prove a pid is still the
    part's; it falls back to whether the pid lives, so a running part is
    never reported gone there."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nullius-pids-"); self.addCleanup(shutil.rmtree, self.dir, True)
        Path(self.dir, "run-a.sh").write_text("sleep 30\n")
        self.part = subprocess.Popen(["bash", "run-a.sh"], cwd=self.dir, start_new_session=True)
        self.addCleanup(self.end_part)
        Path(self.dir, "run-a.pid").write_text(f"{self.part.pid}\n")

    def end_part(self):
        """The part's whole session - its bash and the sleep under it."""
        try:
            os.killpg(self.part.pid, 9)
        except ProcessLookupError:
            pass
        self.part.wait()

    def ask(self, pids=()):
        out = subprocess.run(["bash", "-c", execution._part_pids("run-a", pids)], cwd=self.dir, capture_output=True, text=True, timeout=10)
        return [int(l) for l in out.stdout.split()]

    def test_a_running_part_is_found_with_proc_and_without(self):
        self.assertEqual(self.ask(), [self.part.pid])
        with patch.object(execution, "PROC", os.path.join(self.dir, "no-proc")):
            self.assertIn(self.part.pid, self.ask())

    def test_without_proc_a_dead_pid_is_not_taken_for_the_part(self):
        dead = subprocess.Popen(["true"]); dead.wait()
        with patch.object(execution, "PROC", os.path.join(self.dir, "no-proc")):
            self.assertNotIn(dead.pid, self.ask([dead.pid]))


if __name__ == "__main__":
    unittest.main()
