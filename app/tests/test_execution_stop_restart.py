"""Finishing interrupted stops after a restart: running, paused and launching parts."""
from __future__ import annotations

import json
import subprocess
import threading
import time
import unittest

from nullius import execution, hosts

from tests.execution_support import LocalHost, gone


class StopCutShortByARestart(LocalHost):
    """A Stop that a restart cut short is finished, never resumed - and a
    part that had ended by itself meanwhile keeps its own result: its exit
    code is read from the target and recorded, as a run that saw the end
    before the Stop records it, and the job page says it. On a host too,
    though it answers slower than a Stop gives a command (``STOP_POLL``)."""
    PARTS = [{"id": "a", "label": "a", "script": "exit 3"}, {"id": "b", "label": "b", "script": "echo b"}]
    JOBS = [{"name": "r-a", "agent": "a"}, {"name": "r-b", "agent": "b"}]

    def test_a_part_that_ended_while_nullius_was_away_keeps_its_result(self):
        (self.home / "lag").write_text(str(execution.STOP_POLL * 2))
        dead = subprocess.Popen(["true"]); dead.wait()
        a_ended = {"a": {"rc": 0, "at": time.time(), "label": "a"}}
        cases = {"the first part, its pid saved": (0, dead.pid, {}, 3), "the first part, before its pid was saved": (0, None, {}, 0),
                 "the second part, its pid saved": (1, dead.pid, a_ended, 3)}
        for target in ("here", "host"):
            for n, (case, (i, pid, ends, rc)) in enumerate(cases.items()):
                with self.subTest(target, case=case):
                    name = f"{target}-{n}"; root = self.tmp / "roots" / name; (root / name).mkdir(parents=True)
                    d = root / name if target == "here" else self.on_host(name); d.mkdir(parents=True, exist_ok=True)
                    for p in self.PARTS[:i + 1]:
                        code = rc if p is self.PARTS[i] else ends[p["id"]]["rc"]
                        for ext, text in (("sh", f"exit {code}\n"), ("launch", ""), ("pid", f"{dead.pid}\n"), ("log", f"{p['id']}-out\n"), ("exit", f"{code}\n")):
                            (d / f"run-{p['id']}.{ext}").write_text(text)
                    plan = execution.Plan(name=name, script="", parts=self.PARTS, jobs=self.JOBS, host=hosts.parse("bench") if target == "host" else None)
                    rec = {"id": f"{name}-1", "plan": plan.to_dict(), "state": "stopping", "step": "run", "part": i, "pid": pid, "log_offset": 0,
                           "exit_code": None, "error": None, "question": None, "ends": ends, "started": time.time(), "finished": None,
                           "harbor": "harbor", "tasks_on_target": ""}
                    (root / name / "run.json").write_text(json.dumps(rec))
                    execution.EXECUTIONS.clear()
                    back = execution.restore(str(root))
                    self.assertEqual(len(back), 1); again = self.wait(back[0])
                    ended = self.PARTS[i]["id"]
                    self.assertEqual((again.state, again.exit_code), ("stopped", rc), again.lines)
                    self.assertEqual({p: v["rc"] for p, v in again.ends.items()}, dict({p: v["rc"] for p, v in ends.items()}, **{ended: rc}))
                    self.assertEqual(again.job_state(f"r-{ended}")[0], "done" if rc == 0 else "failed")      # the part's own result
                    if i == 0:
                        self.assertEqual(again.job_state("r-b")[0], "stopped")                                  # b never ran: the run's
                        self.assertFalse((d / "run-b.log").exists())
                    saved = json.loads((root / name / "run.json").read_text())
                    self.assertEqual((saved["state"], saved["ends"][ended]["rc"], saved["pid"]), ("stopped", rc, None))
                    self.assertFalse(any(l.startswith("— running ") for l in again.lines))
                    self.assertNotIn("gone without an exit code", "\n".join(again.lines))

    def test_a_part_still_running_is_ended_by_a_restored_stop_as_by_a_live_one(self):
        """The part had not ended when the restart came: the restored Stop
        ends it, and records what a live Stop records - exit 130, the ✕
        line, the part's job stopped, the next part never started."""
        (self.home / "lag").write_text(str(execution.STOP_POLL * 2))
        for target in ("here", "host"):
            seen = {}
            for how in ("live", "restored"):
                with self.subTest(target, how=how):
                    name = f"{target}-{how}"; root = self.tmp / "roots" / name; gate, marker = root / "gate", root / "b-ran"
                    root.mkdir(parents=True); self.addCleanup(lambda g=gate: g.exists() or g.write_text(""))   # a never outlives the test
                    parts = [{"id": "a", "label": "a", "script": f"while [ ! -e {gate} ]; do sleep 0.02; done; exit 0"},
                             {"id": "b", "label": "b", "script": f"echo b > {marker}"}]
                    plan = execution.Plan(name=name, script="", parts=parts, jobs=self.JOBS, host=hosts.parse("bench") if target == "host" else None)
                    ex = execution.Execution(plan, root=str(root), key="k" * 20)
                    execution.EXECUTIONS[ex.id] = ex
                    ex.start()
                    path = root / name / "run.json"
                    for _ in range(1000):
                        rec = json.loads(path.read_text())
                        if rec["pid"]:
                            break
                        time.sleep(0.02)
                    self.assertTrue(rec["pid"], ex.lines)
                    if how == "live":
                        ex.stop()
                    else:
                        # The Stop said and saved, then Nullius gone before it was finished: its thread never acts again.
                        stuck = threading.Event()
                        ex._save = lambda: None; ex.log = lambda line: None
                        ex._sh = lambda *a, **k: stuck.set() or threading.Event().wait()
                        self.assertTrue(stuck.wait(20))
                        rec = json.loads(path.read_text()); rec["state"] = "stopping"; path.write_text(json.dumps(rec))
                        execution.EXECUTIONS.clear()
                        back = execution.restore(str(root), key="k" * 20)
                        self.assertEqual(len(back), 1); ex = back[0]
                    self.wait(ex, 20)
                    self.assertTrue(gone(rec["pid"], 10), "the part outlived the Stop")
                    self.assertFalse((root / name / "run-b.log").exists()); self.assertFalse(marker.exists())
                    saved = json.loads(path.read_text())
                    seen[how] = (ex.state, ex.exit_code, ex.lines[-1], ex.job_state("r-a")[0], ex.job_state("r-b")[0], ex.ends,
                                 saved["state"], saved["exit_code"], saved["ends"])
            with self.subTest(target, how="the same"):
                self.assertEqual(seen["live"], ("stopped", 130, "✕ stopped during a: exit 130", "stopped", "stopped", {}, "stopped", 130, {}))
                self.assertEqual(seen["restored"], seen["live"])

    def test_a_stop_said_during_a_pause_records_the_same_restored_or_live(self):
        """No part under way: the Stop ends none, and the run's exit is the
        paused part's own - the record a restart reads names the next part,
        which has nothing on the target, and that is no part to give 130."""
        (self.home / "lag").write_text(str(execution.STOP_POLL * 2))
        for target in ("here", "host"):
            seen = {}
            for how in ("live", "restored"):
                with self.subTest(target, how=how):
                    name = f"pause-{target}-{how}"; root = self.tmp / "roots" / name; marker = root / "b-ran"; root.mkdir(parents=True)
                    parts = [{"id": "a", "label": "a", "script": "exit 2"}, {"id": "b", "label": "b", "script": f"echo b > {marker}"}]
                    plan = execution.Plan(name=name, script="", parts=parts, jobs=self.JOBS, host=hosts.parse("bench") if target == "host" else None)
                    ex = execution.Execution(plan, root=str(root), key="k" * 20)
                    execution.EXECUTIONS[ex.id] = ex
                    self.until_recorded(ex.start(), "paused", 20)
                    path = root / name / "run.json"
                    if how == "live":
                        ex.stop()
                    else:
                        ex._save = lambda: None; ex.log = lambda line: None             # Nullius gone, its thread waiting at the pause
                        rec = json.loads(path.read_text()); rec["state"] = "stopping"; path.write_text(json.dumps(rec))
                        execution.EXECUTIONS.clear()
                        back = execution.restore(str(root), key="k" * 20)
                        self.assertEqual(len(back), 1); ex = back[0]
                    self.wait(ex, 20)
                    self.assertFalse(marker.exists())
                    seen[how] = (ex.state, ex.exit_code, ex.lines[-1], ex.job_state("r-a")[0], ex.job_state("r-b")[0], {p: v["rc"] for p, v in ex.ends.items()})
            with self.subTest(target, how="the same"):
                self.assertEqual(seen["live"], ("stopped", 2, "✕ stopped before b: exit 2", "failed", "stopped", {"a": 2}))
                self.assertEqual(seen["restored"], seen["live"])


class StopCutTheLaunchShort(LocalHost):
    """A Stop that cuts a part's launch short before its launcher reached
    the target leaves nothing of the part there - as a Stop before the
    launch does. Live, the part is 130 (a step the Stop cut short); a
    restart reading the record that Stop saved records the same, since the
    record names the part whose launch began. A record from before that
    field keeps the exit the run had."""
    JOBS = [{"name": "r-a", "agent": "a"}, {"name": "r-b", "agent": "b"}]

    def test_a_restart_records_what_the_live_stop_records(self):
        for target in ("here", "host"):
            seen = {}
            for how in ("live", "restored", "old record"):
                with self.subTest(target, how=how):
                    name = f"{target}-{how.replace(' ', '-')}"; root = self.tmp / "roots" / name; marker = root / "b-ran"; root.mkdir(parents=True)
                    parts = [{"id": "a", "label": "a", "script": "exit 0"}, {"id": "b", "label": "b", "script": f"echo b > {marker}"}]
                    plan = execution.Plan(name=name, script="", parts=parts, jobs=self.JOBS, host=hosts.parse("bench") if target == "host" else None)
                    ex = execution.Execution(plan, root=str(root), key="k" * 20)
                    execution.EXECUTIONS[ex.id] = ex
                    held, real = threading.Event(), ex._sh
                    gone_for_good = threading.Event()
                    def sh(command, timeout=600.0, stdin=None, ex=ex, real=real, held=held, how=how, gone_for_good=gone_for_good):
                        if "setsid nohup" in command and "run-b.sh" in command:
                            held.set()                  # the ssh carrying b's launcher, cut short before the target ran any of it
                            (ex._stop if how == "live" else gone_for_good).wait(20)
                            return 130, "stopped"
                        return real(command, timeout, stdin)
                    ex._sh = sh
                    ex.start()
                    self.assertTrue(held.wait(20), ex.lines)
                    path = root / name / "run.json"
                    if how == "live":
                        ex.stop()
                    else:
                        # The Stop said and saved, then Nullius gone before it was finished: its thread never acts again.
                        ex._save = lambda: None; ex.log = lambda line: None
                        rec = json.loads(path.read_text()); rec["state"] = "stopping"
                        if how == "old record":
                            rec.pop("launched", None)
                        path.write_text(json.dumps(rec))
                        execution.EXECUTIONS.clear()
                        back = execution.restore(str(root), key="k" * 20)
                        self.assertEqual(len(back), 1); ex = back[0]
                    self.wait(ex, 20)
                    self.assertFalse(marker.exists())
                    seen[how] = (ex.state, ex.exit_code, ex.lines[-1], ex.job_state("r-a")[0], ex.job_state("r-b")[0], {p: v["rc"] for p, v in ex.ends.items()})
            with self.subTest(target, how="the same"):
                self.assertEqual(seen["live"][:2], ("stopped", 130))
                self.assertEqual(seen["restored"], seen["live"])
                self.assertEqual(seen["old record"][:2], ("stopped", 0))           # as before: the exit the run had, a's


if __name__ == "__main__":
    unittest.main()
