"""One more part in an ended run's folder (``Execution.again``): chosen tasks of
its job run again where the run ran, here or on its host, the key read again and
sent as a run's, the job's own end kept."""
from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path
from unittest import mock

from nullius import execution, hosts
from nullius.standard.contract import definitions
from nullius.standard.pages import recipe
from nullius.standard.runner import run

from tests.execution_support import Base, HostBase


def part(script, n=1, job="j1", trials=("t1",)):
    return {"id": f"again-{n}", "label": f"{len(trials)} task of {job} again", "again": job, "trials": list(trials), "script": script}


class Here(Base):
    """A run on this machine, ended, then one more part in its folder: real bash, setsid, files."""

    def plan(self):
        made = 'mkdir -p "$NULLIUS_JOBS/j1" && echo "{}" > "$NULLIUS_JOBS/j1/result.json"'
        return execution.Plan(name="std-here", script="", runner="standard", model="m-1", key_var="OPENAI_API_KEY", url_var="",
                              parts=[{"id": "fake", "label": "Fake", "script": made}], jobs=[{"name": "j1", "agent": "fake/0.1"}])

    def test_a_part_runs_again_in_the_run_s_folder_with_the_key_read_now(self):
        ex = self.wait(execution.Execution(self.plan(), root=str(self.tmp / "runs"), key="sk-first-key-0001").start())
        self.assertEqual(ex.state, "done", ex.lines)
        first = ex.job_state("j1")
        self.assertIsNone(ex.holds("j1"))
        gate = self.tmp / "go"
        script = f'until [ -e {shlex.quote(str(gate))} ]; do sleep 0.02; done; echo "again key=${{#API_TOKEN}} jobs=$NULLIUS_JOBS"'
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-second-key-2"}):
            ex.again(part(script))
        self.until(ex, "running")
        # while it runs, the job is going again: no end of its own, the run's state for it, its tasks named
        self.assertIsNone(ex.job_state("j1"))
        self.assertEqual(ex.live_state("j1"), "running")
        self.assertEqual(ex.again_of("j1")["trials"], ["t1"])
        with self.assertRaises(ValueError):
            ex.again(part("true", n=2))                                       # one part at a time
        gate.write_text("")
        self.wait(ex)
        self.assertEqual((ex.state, ex.exit_code), ("done", 0), ex.lines)
        d = self.tmp / "runs" / "std-here"
        self.assertEqual((d / "run-again-1.exit").read_text().strip(), "0")
        self.assertEqual((d / "run-fake.exit").read_text().strip(), "0")     # the first part's files are its own
        self.assertIn(f"again key=15 jobs={d / 'jobs'}", "\n".join(ex.lines))   # the key Settings holds now
        self.assertNotIn("sk-second-key-2", (d / "run.json").read_text())
        # the job's end is still its own part's; the run's record holds both parts
        self.assertEqual(ex.job_state("j1"), first)
        self.assertIsNone(ex.again_of("j1"))
        rec = json.loads((d / "run.json").read_text())
        self.assertEqual([p["id"] for p in rec["plan"]["parts"]], ["fake", "again-1"])
        self.assertEqual(sorted(rec["ends"]), ["again-1", "fake"])
        self.assertEqual(ex.part_for("j1"), "fake")
        # a restart reads it as it was
        execution.EXECUTIONS.clear()
        back = execution.Execution(execution.Plan.from_dict(rec["plan"]), root=str(self.tmp / "runs"), record=rec)
        self.assertEqual((back.state, back.job_state("j1")), ("done", first))

    def test_a_part_run_again_that_fails_fails_the_run_and_not_the_job(self):
        ex = self.wait(execution.Execution(self.plan(), root=str(self.tmp / "runs"), key="sk-first-key-0001").start())
        first = ex.job_state("j1")
        self.wait(ex.again(part("echo 'refused: no such trial' >&2; exit 2"), env={"OPENAI_API_KEY": "sk-first-key-0001"}))
        self.assertEqual((ex.state, ex.exit_code), ("failed", 2))
        self.assertIn("refused: no such trial", "\n".join(ex.lines))
        self.assertEqual(ex.job_state("j1"), first)
        self.assertIsNone(ex.refused("j1"))
        self.assertFalse(ex.never_ran("j1"))

    def test_the_cli_s_rerun_is_the_command_and_its_words_reach_it(self):
        """The generated command runs precisely the named tasks of a finished job."""
        dest = self.tmp / "catalogue/benchmarks/fakebench"
        shutil.copytree(Path(__file__).parent / "fixtures/standard/catalogue/benchmarks/fakebench", dest)
        spec = json.loads((dest / "standard.json").read_text())
        spec["controls"] = {"nop": {"source": "nullius", "from": "Empty answer."}}
        (dest / "standard.json").write_text(json.dumps(spec))
        (dest / "driver.py").write_text(
            'from nullius.standard.protocols.jsonl import serve\n'
            'serve({"units": lambda dataset: {"units": ["t1", "t 2", "untouched"]}, '
            '"setup": lambda unit, dir: {"prompt": ""}, '
            '"score": lambda **kw: {"reward": 0.0}})\n')
        bench = definitions.get("benchmark", "fakebench", str(self.tmp / "catalogue"))
        plan = self.plan()
        plan.parts[0]["script"] = "true"
        ex = self.wait(execution.Execution(plan, root=str(self.tmp / "runs"), key="sk-first-key-0001").start())
        path = Path(run.run(bench, definitions.control(bench, "nop"), model="", provider="custom",
                            jobs_root=ex.jobs, name="j1", env={}, concurrency=1, say=lambda s: None))
        names = {json.loads((p / "result.json").read_text())["standard"]["unit"]: p.name
                 for p in path.iterdir() if (p / "result.json").is_file()}
        untouched = (path / names["untouched"] / "result.json").read_bytes()
        self.wait(ex.again(recipe.again("j1", ["t1", "t 2"], reason="the provider's 'failure'", n=1), env={"OPENAI_API_KEY": "sk-first-key-0001"}))
        self.assertEqual((ex.state, ex.exit_code), ("done", 0), ex.lines)
        (record,) = json.loads((path / "result.json").read_text())["reruns"]
        self.assertEqual(record["trials"], [names["t1"], names["t 2"]])
        self.assertEqual([r["unit"] for r in record["replaced"]], ["t1", "t 2"])
        self.assertEqual(record["reason"], "the provider's 'failure'")
        self.assertEqual(sorted(p.name for p in (path / "retried").iterdir()),
                         sorted(names[u] + ".try-1" for u in ("t1", "t 2")))
        self.assertEqual((path / names["untouched"] / "result.json").read_bytes(), untouched)

    def test_a_job_no_longer_here_is_said(self):
        ex = self.wait(execution.Execution(self.plan(), root=str(self.tmp / "runs"), key="sk-first-key-0001").start())
        os.remove(self.tmp / "runs" / "std-here" / "jobs" / "j1" / "result.json")
        self.assertEqual(ex.holds("j1"), "This machine no longer holds j1")
        self.assertEqual(ex.holds("j2"), "std-here wrote no job j2")


class Recipe(Base):
    def test_the_part_names_the_job_the_trials_the_reason_and_the_cap(self):
        got = recipe.again("job-1", ["a__u0001", "b c"], reason="it's flaky", max_usd=0.25, n=3)
        self.assertEqual((got["id"], got["again"], got["trials"], got["label"]), ("again-3", "job-1", ["a__u0001", "b c"], "2 tasks of job-1 again"))
        line = next(l for l in got["script"].splitlines() if l.startswith("exec nullius standard "))
        self.assertEqual(shlex.split(line.replace('"${NULLIUS_JOBS:-jobs_standard}"', "JOBS"))[3:],
                         ["rerun", "JOBS/job-1", "--unit", "a__u0001", "--unit", "b c", "--reason", "it's flaky", "--max-usd", "0.25", "--from-run"])
        self.assertNotIn("--reason", recipe.again("job-1", ["a"])["script"])
        self.assertNotIn("--max-usd", recipe.again("job-1", ["a"])["script"])


class OnHost(HostBase):
    """A standard run on a host, ended, then one more part sent there: its folder is the run's own, the
    server's tree and the key go as a run's, the job comes back as a run's does."""
    COMMIT = "c0ffee" * 6 + "c0ff"

    def setUp(self):
        super().setUp()
        self.plan = execution.Plan(name="std-host", script="", host=hosts.parse("bench"), runner="standard", model="m-1",
                                   parts=[{"id": "fake", "label": "Fake", "script": "exec true\n"}],
                                   jobs=[{"name": "std-host", "agent": "fake/0.1"}])
        self.plan.key_var, self.plan.url_var = "OPENAI_API_KEY", ""
        (self.tmp / "standard-app" / self.COMMIT / "app").mkdir(parents=True)
        payload = mock.patch.object(execution.standard_payload, "build", return_value=(self.COMMIT, str(self.tmp / "standard-app" / self.COMMIT)))
        payload.start(); self.addCleanup(payload.stop)
        self.held = True

    def answer(self, argv, timeout=0, stdin=None):
        if argv[0] != "rsync" and "printf 'ok" in argv[-1]:
            self.calls.append((argv, stdin))
            return 0, "ok\nuv=yes\ndocker=yes\nharbor=no\nhostname=gpu-1\n"
        if argv[0] != "rsync" and "@held" in argv[-1]:
            self.calls.append((argv, stdin))
            return 0, "@held\n" if self.held else "@gone\n"
        return super().answer(argv, timeout, stdin)

    def test_the_part_is_sent_to_the_run_s_host_in_its_folder_with_the_key_and_the_job_comes_back(self):
        ex = self.wait(execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer, key="sk-first-key-0001", url="").start())
        self.assertEqual(ex.state, "done", ex.lines)
        self.assertIsNone(ex.holds("std-host"))
        self.calls.clear(); self.polls = 0
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-again-key-00003"}):
            ex.again(recipe.again("std-host", ["t1"], reason="flaky", n=1))
        self.wait(ex)
        self.assertEqual((ex.state, ex.exit_code), ("done", 0), ex.lines)
        commands = self.ssh_commands()
        self.assertFalse(any("@taken" in c for c in commands))               # its folder is its own: no name asked
        rsyncs = [c for c, _ in self.calls if c[0] == "rsync"]
        self.assertEqual(rsyncs[0][-2:], [str(self.tmp / "standard-app" / self.COMMIT) + "/", f"bench:~/.nullius/app/{self.COMMIT}"])
        self.assertIn("bench:~/.nullius/runs/std-host/jobs/", {c[-2] for c in rsyncs[1:]})     # the job mirrored back
        sent = next(c for c in commands if "cat > " in c and "run-again-1.sh" in c)
        self.assertIn('"$HOME/.nullius/runs/std-host/run-again-1.sh"', sent)
        script = next(s for c, s in self.calls if c[0] == "ssh" and "run-again-1.sh" in c[-1] and "cat > " in c[-1])
        self.assertIn('rerun "${NULLIUS_JOBS:-jobs_standard}"/std-host --unit t1 --reason flaky --from-run', script)
        launch, stdin = next((c, s) for c, s in self.calls if c[0] == "ssh" and "setsid nohup" in c[-1])
        self.assertIn('NULLIUS_JOBS="$HOME/.nullius/runs/std-host/jobs"', launch[-1])
        self.assertIn(f'NULLIUS_STANDARD="$HOME/.nullius/app/{self.COMMIT}"', launch[-1])
        self.assertIn("run-again-1.sh", launch[-1])
        self.assertEqual(stdin, "sk-again-key-00003\n\n@end\n")              # the key as a run's: stdin, read now
        self.assertNotIn("sk-again-key-00003", launch[-1])

    def test_a_host_that_no_longer_holds_the_job_is_said_and_nothing_is_sent(self):
        ex = self.wait(execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer, key="sk-first-key-0001", url="").start())
        self.held = False
        self.calls.clear()
        self.assertEqual(ex.holds("std-host"), "bench no longer holds std-host: nothing was started")
        self.assertEqual([c for c, _ in self.calls if c[0] == "rsync" or "setsid" in c[-1]], [])
        self.assertIn('[ -f "$HOME/.nullius/runs/std-host/jobs/std-host/result.json" ]', self.ssh_commands()[0])
