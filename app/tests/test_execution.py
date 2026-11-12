"""Launching runs: credentials, prerequisites, hosts, recipes, logs and transport."""
from __future__ import annotations

import json
import shlex
import os
import shutil
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from nullius import execution, hosts, prices as price_list, settings
from nullius.harbor import source
from nullius import providers

from tests.execution_support import Base, HostBase, LocalHost, gone


class Here(Base):
    """Local runs are real: bash, setsid, the exit file, the log."""

    def test_the_script_runs_detached_with_the_local_paths_and_the_key_as_environment(self):
        script = 'echo "harbor=$NULLIUS_HARBOR tasks=$NULLIUS_TASKS jobs=$NULLIUS_JOBS model=$API_MODEL_NAME"; echo "key=${#API_TOKEN}/${#LLM_API_KEY} chars url=${API_URL:-none}"; echo "ppid=$PPID"'
        plan = execution.Plan(name="tau3-x3", script=script, tasks=str(self.tmp), model="openai/gpt-5")
        ex = self.wait(execution.Execution(plan, root=str(self.tmp / "runs"), key="sk-verysecretkey12345", url="https://llm.example/v1").start())
        self.assertEqual((ex.state, ex.exit_code, ex.error), ("done", 0, None), ex.lines)
        text = "\n".join(ex.lines)
        self.assertIn(f"harbor=harbor tasks={self.tmp} jobs={self.tmp / 'runs' / 'tau3-x3' / 'jobs'} model=openai/gpt-5", text)   # the harbor on PATH
        self.assertIn("key=21/21 chars url=https://llm.example/v1", text)                                 # the key reached the run
        d = self.tmp / "runs" / "tau3-x3"
        self.assertEqual((d / "run-run.sh").read_text(), script + "\n"); self.assertEqual((d / "run-run.exit").read_text().strip(), "0")
        self.assertNotIn("sk-verysecretkey12345", (d / "run-run.sh").read_text()); self.assertNotIn("sk-verysecretkey12345", (d / "run.json").read_text())
        rec = json.loads((d / "run.json").read_text())
        self.assertEqual((rec["state"], rec["exit_code"], rec["plan"]["name"], rec["plan"]["host"]), ("done", 0, "tau3-x3", None))

    def test_missing_setsid_uses_python_and_records_the_real_exit_code(self):
        # Simulate macOS command lookup while retaining the real shell/processes.
        lookup = ('command() { if [ "$1" = -v ] && [ "$2" = setsid ]; then '
                  'return 1; else builtin command "$@"; fi; }; ')
        for code in (0, 7):
            with self.subTest(code=code):
                script = (shlex.quote(execution.sys.executable) +
                          " -c 'import os; print(os.getsid(0))'; exit " + str(code))
                ex = execution.Execution(execution.Plan(name=f"fallback-{code}", script=script), root=str(self.tmp))
                real = ex._sh
                ex._sh = lambda command, timeout=600.0, stdin=None: real(lookup + command, timeout, stdin)
                self.wait(ex.start())
                self.assertEqual(ex.exit_code, code, ex.lines)
                folder = self.tmp / f"fallback-{code}"
                self.assertEqual((folder / "run-run.log").read_text().strip(),
                                 (folder / "run-run.pid").read_text().strip())
                self.assertEqual((folder / "run-run.exit").read_text().strip(), str(code))

    def test_stop_ends_a_part_without_setsid(self):
        ex = execution.Execution(execution.Plan(name="fallback-stop", script="sleep 30; echo late"), root=str(self.tmp))
        real = ex._sh
        lookup = ('command() { if [ "$1" = -v ] && [ "$2" = setsid ]; then '
                  'return 1; else builtin command "$@"; fi; }; ')
        ex._sh = lambda command, timeout=600.0, stdin=None: real(lookup + command, timeout, stdin)
        ex.start(); self.addCleanup(lambda: (ex.stop(), ex.thread.join(10)))
        self.until(ex, "running")
        pid = self.pid_of(self.tmp / "fallback-stop", "run")
        ex.stop(); self.wait(ex)
        self.assertEqual(ex.state, "stopped")
        self.assertTrue(gone(pid), "the fallback process outlived Stop")
        self.assertNotIn("late", "\n".join(ex.lines))

    def test_no_key_on_this_machine_is_said_before_anything_runs(self):
        plan = execution.Plan(name="r", script='"$NULLIUS_HARBOR" run', model="m", key_var="OPENAI_API_KEY", url_var="")
        ex = self.wait(execution.Execution(plan, root=str(self.tmp), key="", env={}).start())
        self.assertEqual((ex.state, ex.step), ("failed", "credentials")); self.assertIn("OPENAI_API_KEY is not set", ex.error)
        self.assertFalse((self.tmp / "r" / "run-run.log").exists())

    def test_a_run_gets_its_own_key_and_no_other_secret(self):
        """``settings.load`` puts every key of app/.env into this process's
        environment; a run's commands get none of them - only what the
        launcher exports from stdin: the key and endpoint of the run's own
        provider."""
        self.assertLessEqual({"MODEL_API_KEY", "MODEL_API_URL", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"}
                             | set(execution.KEY_NAMES + execution.URL_NAMES), set(execution.withheld()))
        self.assertLessEqual({"API_KEY_TYPESAFE", "API_TOKEN"}, set(execution.withheld()))     # old spellings of a secret
        self.assertEqual({"HOST", "PORT"} & set(execution.withheld()), set())                 # old names of start-up options are no secret
        held = {n: "sk-held-" + n.lower() for n in execution.withheld()}
        plan = execution.Plan(name="own", script="env", model="m", key_var="OPENROUTER_API_KEY", url_var="", base_url=providers.PROVIDER_BASE["openrouter"])
        with mock.patch.dict(os.environ, held):
            self.assertEqual([k for k in execution._clean_env() if k in held], [])
            ex = self.wait(execution.Execution(plan, root=str(self.tmp)).start())
        self.assertEqual((ex.state, ex.error), ("done", None), ex.lines)
        env = dict(l.split("=", 1) for l in (self.tmp / "own" / "run-run.log").read_text().splitlines() if "=" in l)
        self.assertEqual({k: v for k, v in env.items() if v.startswith("sk-held-")}, {n: held["OPENROUTER_API_KEY"] for n in execution.KEY_NAMES})
        self.assertEqual({n: env.get(n) for n in execution.URL_NAMES}, {n: providers.PROVIDER_BASE["openrouter"] for n in execution.URL_NAMES})

    def test_a_local_endpoint_without_a_key_is_sent_the_placeholder(self):
        """Many local servers check no key, but an OpenAI-compatible client
        will not start without one: MODEL_API_KEY unset, the run is handed
        the placeholder. The log, which hides a key, leaves that word alone."""
        plan = execution.Plan(name="loc", script='echo "key=$API_TOKEN/$OPENAI_API_KEY/$LLM_API_KEY url=$API_URL"', model="m")
        ex = self.wait(execution.Execution(plan, root=str(self.tmp), env={"MODEL_API_URL": "http://127.0.0.1:8000/v1"}).start())
        self.assertEqual((ex.state, ex.error), ("done", None), ex.lines)
        p = settings.MODEL_KEY_PLACEHOLDER
        self.assertEqual((self.tmp / "loc" / "run-run.log").read_text(), f"key={p}/{p}/{p} url=http://127.0.0.1:8000/v1\n")
        ex.log("uv: ~/.local/bin/uv")
        self.assertEqual(ex.lines[-1], "uv: ~/.local/bin/uv")
        keyed = execution.Execution(execution.Plan(name="k", script="true"), root=str(self.tmp), env={"MODEL_API_KEY": "sk-local-0123456789"})
        self.assertEqual(keyed._key, "sk-local-0123456789")                                  # a key set is the key sent

    def test_a_relative_task_folder_is_the_one_the_check_found(self):
        """The check reads a relative folder from where the server runs; the
        part runs from its run directory, where the same words name nothing."""
        (self.tmp / "here" / "rel-tasks" / "task-1").mkdir(parents=True)
        cwd = os.getcwd()
        os.chdir(self.tmp / "here")
        self.addCleanup(os.chdir, cwd)
        plan = execution.Plan(name="rel", script='ls "$NULLIUS_TASKS"', tasks="rel-tasks", keyless=True)
        ex = self.wait(execution.Execution(plan, root=str(self.tmp / "runs"), env={}).start())
        self.assertEqual((ex.state, ex.error), ("done", None), ex.lines)
        self.assertIn("task-1", "\n".join(ex.lines))

    def test_a_local_endpoint_without_its_url_is_said_before_anything_runs(self):
        """A local or self-hosted model has no default endpoint: MODEL_API_URL
        unset fails the credentials check, for Harbor and for the standard alike."""
        env = {"MODEL_API_KEY": "sk-local-0123456789"}
        for plan in (execution.Plan(name="h", script='"$NULLIUS_HARBOR" run', model="m"),
                     execution.Plan(name="std", script="", parts=[{"id": "run", "label": "run", "script": "true"}], runner="standard")):
            with self.subTest(plan.runner):
                self.assertEqual((plan.key_var, plan.url_var), ("MODEL_API_KEY", "MODEL_API_URL"))
                ex = self.wait(execution.Execution(plan, root=str(self.tmp), env=env).start())
                self.assertEqual((ex.state, ex.step), ("failed", "credentials"))
                self.assertIn("MODEL_API_URL is not set in Settings; a local or self-hosted endpoint has no default", ex.error)
                self.assertFalse(any(p.name.startswith("run-") and p.suffix == ".log" for p in (self.tmp / plan.name).iterdir()))
        ex = self.wait(execution.Execution(execution.Plan(name="ok", script='echo "url=$API_URL"', model="m"), root=str(self.tmp),
                                           env=dict(env, MODEL_API_URL="http://127.0.0.1:8000/v1")).start())
        self.assertEqual(ex.state, "done", ex.lines); self.assertIn("url=http://127.0.0.1:8000/v1", "\n".join(ex.lines))

    def test_a_failing_script_is_a_failed_run(self):
        ex = self.wait(execution.Execution(execution.Plan(name="r", script="exit 3"), root=str(self.tmp)).start())
        self.assertEqual((ex.state, ex.exit_code, ex.error), ("failed", 3, "the run exited 3"))


class DockerHere(Base):
    """A Harbor run here asks docker first, as a run on a host does - and
    so does the standard on its docker backend - and starts nothing when it
    is missing or does not answer: with Docker Desktop quit, /usr/bin/docker
    is still on the PATH. The standard's local backend asks nothing of docker,
    and a restored run is not asked again."""

    def setUp(self):
        super().setUp()
        mock.patch.stopall()                                       # the real check, against a docker of the test's own
        self.bin = self.tmp / "bin"; self.bin.mkdir()
        path = mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(self.tmp)})
        path.start(); self.addCleanup(path.stop)
        self.enterContext(mock.patch.object(execution.container, "DOCKER", "docker"))     # the docker of the test's own, wherever podman is

    def docker(self, body):
        (self.bin / "docker").write_text("#!/bin/sh\n" + body + "\n"); (self.bin / "docker").chmod(0o755)

    def start(self, plan):
        return self.wait(execution.Execution(plan, root=str(self.tmp / "runs"), key="k" * 20, url="").start())

    def test_no_docker_here_stops_a_harbor_run_before_it_starts(self):
        only = self.tmp / "only"; only.mkdir(); (only / "bash").symlink_to(shutil.which("bash"))
        with mock.patch.dict(os.environ, {"PATH": str(only)}):                     # bash and nothing else: the check is all that runs
            ex = self.start(execution.Plan(name="r", script='"$NULLIUS_HARBOR" run'))
        self.assertEqual((ex.state, ex.step, ex.error), ("failed", "docker", "docker is not on this machine; a Harbor run needs it"))
        self.assertFalse((self.tmp / "runs" / "r" / "run-run.sh").exists())          # no part written, none started

    def test_the_standards_docker_backend_asks_its_own_engine(self):
        asked = []
        sh = lambda cmd, timeout: asked.append(cmd) or (0, "@down\nError: no storage\n")
        why = execution.docker_here(sh, "this recipe runs its units in containers", "podman")
        self.assertIn("podman ps", asked[0])
        self.assertEqual(why, "podman is on this machine but does not answer (podman ps failed): Error: no storage")

    def test_a_docker_that_does_not_answer_stops_it_too_and_says_why(self):
        self.docker('echo "The command \'docker\' could not be found in this WSL 2 distro." >&2; exit 1')
        ex = self.start(execution.Plan(name="r", script='"$NULLIUS_HARBOR" run'))
        self.assertEqual((ex.state, ex.step), ("failed", "docker"))
        self.assertIn("docker ps failed", ex.error); self.assertIn("could not be found in this WSL 2 distro", ex.error)
        self.assertFalse((self.tmp / "runs" / "r" / "run-run.sh").exists())

    def test_a_docker_that_answers_lets_the_run_go(self):
        self.docker("exit 0")
        ex = self.start(execution.Plan(name="r", script="echo ran"))
        self.assertEqual((ex.state, ex.error), ("done", None), ex.lines)

    def test_the_standard_on_its_docker_backend_asks_too_and_off_it_does_not(self):
        self.docker("echo asked >> " + shlex.quote(str(self.tmp / "asked")) + "; exit 1")
        parts = [{"id": "run", "label": "run", "script": "true"}]
        ex = self.start(execution.Plan(name="std", script="", parts=parts, runner="standard", docker=True, keyless=True))
        self.assertEqual((ex.state, ex.step), ("failed", "docker"))
        self.assertTrue(ex.error.startswith("docker is on this machine but does not answer"), ex.error)
        self.assertFalse((self.tmp / "runs" / "std" / "run-run.sh").exists())
        (self.tmp / "asked").unlink()
        ex = self.start(execution.Plan(name="std-local", script="", parts=parts, runner="standard", keyless=True))   # the local backend: no containers
        self.assertNotEqual(ex.step, "docker", ex.lines)
        self.assertFalse((self.tmp / "asked").exists())


class Parts(Base):
    """Agents run one after another (the builder orders them, controls first);
    a failure with more to come pauses for a decision; trials that end are
    said as the feed says them."""
    PARTS = [{"id": "nop", "label": "nop run", "script": "echo nop"}, {"id": "oracle", "label": "oracle run", "script": "echo oracle; exit 3"},
             {"id": "custom-0", "label": "my_agent run", "script": "echo custom"}]

    def test_a_failure_pauses_and_continue_goes_on(self):
        ex = execution.Execution(execution.Plan(name="r", script="", parts=self.PARTS), root=str(self.tmp)).start()
        self.until_recorded(ex, "paused"); self.assertEqual(ex.state, "paused")
        self.assertIn("oracle run exited 3", ex.question); self.assertIn("my_agent run", ex.question)
        self.assertFalse((self.tmp / "r" / "run-custom-0.log").exists())                                   # the third waits
        ex.continue_(); self.wait(ex)
        self.assertEqual((ex.state, ex.exit_code), ("done", 0)); self.assertIn("custom", (self.tmp / "r" / "run-custom-0.log").read_text())
        self.assertIn("— continuing with my_agent run", ex.lines)

    def test_stop_while_paused_ends_the_run(self):
        ex = execution.Execution(execution.Plan(name="r", script="", parts=self.PARTS), root=str(self.tmp)).start()
        self.until(ex, "paused"); ex.stop(); self.wait(ex)
        self.assertEqual(ex.state, "stopped"); self.assertFalse((self.tmp / "r" / "run-custom-0.log").exists())

    def test_a_last_part_failing_is_a_failed_run_with_no_question(self):
        parts = [dict(self.PARTS[0]), {"id": "last", "label": "last", "script": "exit 4"}]
        ex = self.wait(execution.Execution(execution.Plan(name="r", script="", parts=parts), root=str(self.tmp)).start())
        self.assertEqual((ex.state, ex.exit_code, ex.question), ("failed", 4, None))

    def test_each_agent_s_job_ends_as_its_own_part_did(self):
        """An agent that finished is done though a later one failed: its page
        and its row say its own result, and the run's error names the part."""
        parts = [dict(self.PARTS[0]), {"id": "last", "label": "last run", "script": "exit 3"}]
        jobs = [{"name": "r-nop", "agent": "nop"}, {"name": "r-last", "agent": "last"}, {"name": "r-never", "agent": "never"}]
        ex = execution.Execution(execution.Plan(name="r", script="", parts=parts, jobs=jobs), root=str(self.tmp))
        execution.EXECUTIONS[ex.id] = ex
        self.wait(ex.start())
        self.assertEqual((ex.state, ex.error), ("failed", "last run exited 3"))
        self.assertEqual(ex.job_state("r-nop")[0], "done"); self.assertEqual(ex.job_state("r-last")[0], "failed")
        self.assertEqual(ex.job_state("r-never")[0], "failed")                                           # no part of its own ran: the run's
        self.assertEqual(execution.ended_for("r-nop")[0], "done")
        back = execution.Execution(execution.Plan(name="r", script="", parts=parts, jobs=jobs), root=str(self.tmp),
                                   record=json.loads((self.tmp / "r" / "run.json").read_text()))
        self.assertEqual(back.job_state("r-nop")[0], "done")                                              # a restart reads it back
        harbor = execution.Execution(execution.Plan(name="h", script="", parts=parts, jobs=[{"name": "h-nop", "agent": "nop-arg"}]), root=str(self.tmp))
        self.assertEqual(harbor.part_for("h-nop"), "nop")                                                 # a Harbor job: by its name

    def test_trials_are_said_as_the_feed_says_them(self):
        job = self.tmp / "r" / "jobs" / "r-nop"; job.mkdir(parents=True)
        (job / "events.jsonl").write_text('{"event":"job.start","job":"r-nop"}\n{"event":"trial.end","trial":"t1__abc","task":"tau3-bench__tau3-airline-0","agent":"nop","outcome":"unresolved","reward":0.0,"durations":{"env_setup":7.8,"agent_run":3.3,"verify":80.5}}\n{"event":"trial.end","trial":"t2__def","task":"tau3-bench__tau3-retail-0","agent":"nop","outcome":"resolved","reward":1.0,"durations":{"agent_run":3}}\n')
        ex = self.wait(execution.Execution(execution.Plan(name="r", script="true"), root=str(self.tmp)).start())
        said = [l for l in ex.lines if "tau3-bench__" in l]
        self.assertEqual(said, ["  ✕ r-nop · tau3-bench__tau3-airline-0 · unresolved · reward 0.0 · 1:31", "  ✓ r-nop · tau3-bench__tau3-retail-0 · resolved · reward 1.0 · 0:03"])
        ex._trial_lines(); self.assertEqual(len([l for l in ex.lines if "tau3-bench__" in l]), 2)                 # said once

    def test_ssh_noise_stays_out_of_the_log(self):
        ex = self.wait(execution.Execution(execution.Plan(name="r", script="echo '** WARNING: connection is not using a post-quantum key exchange algorithm.'; echo real"), root=str(self.tmp)).start())
        self.assertNotIn("post-quantum", "\n".join(ex.lines)); self.assertIn("real", ex.lines)


class Transport(unittest.TestCase):
    def test_bytes_that_are_not_utf_8_never_end_a_run(self):
        """What a target prints is read as UTF-8, a byte that is not one read
        as U+FFFD: a run is never failed by how its output was spelt."""
        rc, out = execution.subprocess_run(["bash", "-c", "printf 'a\\x81b\\xe2\\x94'"])
        self.assertEqual((rc, out), (0, "a\ufffdb\ufffd"))


class SshWarnsAtEveryConnection(LocalHost):
    """OpenSSH 10 warns on stderr at every connection to a host without a
    post-quantum key exchange: three lines, 222 bytes with their \\r\\n,
    ahead of whatever the command prints. A part's log is read on from where
    the last look ended, by the log's own bytes: the warning is no part of
    it, so it moves nothing, and a look never starts inside a character.
    Found on a τ³ run whose results table - box drawing, three bytes a
    character - ended the run with a UnicodeDecodeError, its lines skipped."""
    TABLE = "━" * 40
    LINES = ["Downloading litellm (35.1MiB)", " Downloaded botocore", f"  20/20 Mean: 0.000 {TABLE} 0:00:08", "┃ Trials ┃ Exceptions ┃", "done"]

    def setUp(self):
        super().setUp()
        (self.home / "pq").write_text("")

    def script(self, gate=None):
        said = [f"echo {shlex.quote(l)}; sleep 0.2" for l in self.LINES[:-1]]
        wait = [f"while [ ! -e {gate} ]; do sleep 0.02; done"] if gate else []
        return "; ".join(said + wait + [f"echo {self.LINES[-1]}"])

    def part_lines(self, ex):
        """The part's own lines, in order: what Nullius says between them (a Stop asked for, the part's end) left out."""
        i = ex.lines.index(self.LINES[0])
        return [l for l in ex.lines[i:] if not l.startswith(("— ", "✓ ", "✕ ", "× "))]

    def test_a_followed_part_s_log_arrives_whole(self):
        ex = self.wait(execution.Execution(self.plan([{"id": "a", "label": "a run", "script": self.script()}]), root=str(self.tmp)).start(), 30)
        self.assertEqual((ex.state, ex.exit_code), ("done", 0), ex.lines)
        self.assertEqual(self.part_lines(ex), self.LINES)
        self.assertNotIn("post-quantum", "\n".join(ex.lines))

    def test_the_rest_of_the_log_a_stop_reads_arrives_whole(self):
        execution.SYNC_EVERY = 60                          # one look while it runs, then the Stop reads the rest
        gate = self.tmp / "gate"; self.addCleanup(lambda: gate.exists() or gate.write_text(""))
        ex = execution.Execution(self.plan([{"id": "a", "label": "a", "script": self.script(gate)}, {"id": "b", "label": "b", "script": "true"}]),
                                 root=str(self.tmp), key="k" * 20)
        looked, real = threading.Event(), ex._sh
        def sh(command, timeout=600.0, stdin=None):
            out = real(command, timeout, stdin)
            if "__nullius__" in command and out[0] == 0:
                looked.set()                                # the run has looked at the part while it ran
            return out
        ex._sh = sh
        execution.EXECUTIONS[ex.id] = ex
        ex.start()
        self.assertTrue(looked.wait(20), ex.lines)
        gate.write_text("")
        for _ in range(500):                               # a ends by itself, before the run looks again
            if (self.on_host() / "run-a.exit").exists() and (self.on_host() / "run-a.exit").read_text().strip():
                break
            time.sleep(0.02)
        self.assertEqual((self.on_host() / "run-a.exit").read_text().strip(), "0")
        ex.stop(); self.wait(ex, 20)
        self.assertEqual((ex.state, ex.exit_code), ("stopped", 0), ex.lines)
        self.assertEqual(self.part_lines(ex), self.LINES)


class KeyCutShort(LocalHost):
    """The launcher reads the key and the endpoint from its stdin, and an end
    line after them. A stdin cut short - Nullius killed as it sent them, an
    ssh dropped - is never taken for an empty key or endpoint: the launcher
    starts nothing and says so, and the run fails at that part. Here and on
    a host alike."""
    KEY, URL = "sk-cut-0123456789abcdef", "http://127.0.0.1:8000/v1"

    def cut_to(self, keep):
        """The command runner, the launcher's stdin cut to what ``keep`` leaves of it."""
        def run(argv, timeout=600.0, stdin=None):
            if "setsid nohup" in argv[-1]:
                stdin = keep(stdin)
            return execution.subprocess_run(argv, timeout, stdin)
        return run

    def test_a_launcher_whose_stdin_was_cut_short_starts_nothing(self):
        cuts = {"nothing": lambda s: "", "part of the key": lambda s: s[:5], "the key alone": lambda s: s.split("\n")[0] + "\n",
                "the key and endpoint, no end line": lambda s: "\n".join(s.split("\n")[:2]) + "\n"}
        for target in ("here", "host"):
            for n, (case, keep) in enumerate(cuts.items()):
                with self.subTest(target, cut=case):
                    name = f"{target}-{n}"; root = self.tmp / name; marker = root / "ran"
                    parts = [{"id": "a", "label": "a", "script": f"echo ran > {marker}"}]
                    plan = (execution.Plan(name=name, script="", parts=parts) if target == "here" else self.plan(parts, name))
                    ex = self.wait(execution.Execution(plan, root=str(root), run=self.cut_to(keep), key=self.KEY, url=self.URL).start())
                    self.assertEqual((ex.state, ex.step), ("failed", "run"), ex.lines)
                    self.assertIn("the key and endpoint reached its launcher cut short; nothing was started", ex.error)
                    d = root / name if target == "here" else self.on_host(name)
                    self.assertEqual(sorted(p.name for p in d.iterdir() if p.name.startswith("run-a.")), ["run-a.sh"])   # not claimed, not started
                    self.assertFalse(marker.exists())
                    for f in [*root.rglob("*"), *self.home.rglob("*")]:
                        if f.is_file():
                            self.assertNotIn(self.KEY, f.read_text(errors="replace"), f)
                    self.assertNotIn(self.KEY, "\n".join(ex.lines))

    def test_a_harbor_run_gets_the_agent_keys_and_the_standard_none(self):
        # an agent key reaches the part's environment over the launcher's stdin, here and on a host; it is never
        # inherited, never in a log, and the standard's part gets none
        secret = "cur-0123456789abcdef"
        for target, runners in (("here", ("harbor", "standard")), ("host", ("harbor",))):
            for runner in runners:
                with self.subTest(target, runner=runner):
                    name = f"keys-{target}-{runner}"; root = self.tmp / name; marker = root / "seen"
                    parts = [{"id": "a", "label": "a", "script": f'echo "cursor=${{CURSOR_API_KEY:-none}}" > {marker}; echo "$CURSOR_API_KEY"'}]
                    plan = execution.Plan(name=name, script="", parts=parts) if target == "here" else self.plan(parts, name)
                    plan.runner = runner
                    with patch.dict(os.environ, {"AGENT_KEYS": "CURSOR_API_KEY", "CURSOR_API_KEY": secret}):
                        ex = self.wait(execution.Execution(plan, root=str(root), key=self.KEY, url=self.URL).start())
                    self.assertEqual(ex.state, "done", ex.lines)
                    self.assertEqual(marker.read_text().strip(), f"cursor={secret}" if runner == "harbor" else "cursor=none")
                    self.assertNotIn(secret, "\n".join(ex.lines))                                      # redacted from the log

    def test_a_standard_run_on_a_host_is_handed_its_benchmarks_licences_and_here_none(self):
        # on a host: the declared names that are set, one stdin line, in the part's environment and never on disk
        # but for the in-memory folder a licence file is written to, which the part's shell removes when it ends;
        # on this machine nothing is sent - the run reads the benchmark's store itself
        from nullius import secrets
        value, blob = "lic-0123456789abcdef", b"FAKE-LICENCE-FILE\n\x00bytes"
        for target in ("here", "host"):
            with self.subTest(target):
                name = f"lic-{target}"; root = self.tmp / name; seen = root / "seen"
                script = (f'echo "line=${{#NULLIUS_LICENCES}} dir=${{NULLIUS_LICENCE_DIR:-none}}" > {seen}; '
                          f'[ -n "$NULLIUS_LICENCE_DIR" ] && mkdir -p "$NULLIUS_LICENCE_DIR" && echo x > "$NULLIUS_LICENCE_DIR/F"; '
                          f'echo "$NULLIUS_LICENCES" > {root}/line; echo "{value}"')
                parts = [{"id": "a", "label": "a", "script": script}]
                plan = execution.Plan(name=name, script="", parts=parts) if target == "here" else self.plan(parts, name)
                plan.runner = execution.STANDARD
                plan.licences = {"benchmark": "envbench", "names": ["A", "F", "UNSET"]}
                ex = self.wait(execution.Execution(plan, root=str(root), key=self.KEY, url=self.URL,
                                                   licences={"A": value.encode(), "F": blob, "NOT_DECLARED": b"no"}).start())
                self.assertEqual(ex.state, "done", ex.lines)
                said = seen.read_text().strip()
                if target == "here":
                    self.assertEqual(said, "line=0 dir=none")
                    continue
                shm = f"/dev/shm/nullius-{name}"
                self.assertTrue(said.startswith("line=") and said.endswith(f"dir={shm}"), said)
                self.assertEqual(secrets.unpack((root / "line").read_text().strip()), {"A": value.encode(), "F": blob})
                self.assertFalse(os.path.exists(shm))                                   # removed when the part ended
                self.assertNotIn(value, "\n".join(ex.lines))                            # redacted from the log the page shows
                self.assertNotIn(value, (root / name / "nullius.log").read_text())
                self.assertNotIn(blob.decode(errors="replace"), (root / name / "run.json").read_text())

    def test_a_stopped_run_on_a_host_leaves_no_licence_file_behind(self):
        name = "lic-stop"; root = self.tmp / name; ready = root / "ready"
        script = f'mkdir -p "$NULLIUS_LICENCE_DIR" && echo x > "$NULLIUS_LICENCE_DIR/F"; touch {ready}; sleep 30'
        plan = self.plan([{"id": "a", "label": "a", "script": script}], name)
        plan.runner, plan.licences = execution.STANDARD, {"benchmark": "envbench", "names": ["F"]}
        ex = execution.Execution(plan, root=str(root), key=self.KEY, url=self.URL, licences={"F": b"FAKE-LICENCE-FILE"}).start()
        for _ in range(250):
            if ready.exists():
                break
            time.sleep(0.02)
        self.assertTrue(os.path.exists(f"/dev/shm/nullius-{name}/F"))
        ex.stop()
        self.wait(ex)
        self.assertEqual(ex.state, "stopped", ex.lines)
        self.assertFalse(os.path.exists(f"/dev/shm/nullius-{name}"))

    def test_a_launch_the_host_never_answered_is_asked_once_more_and_starts_once(self):
        # the first launcher is lost on the way (an ssh that stalls); the second finds no claim and starts the part.
        # A launcher that did run before its answer was lost leaves the claim: the second says @claimed instead.
        for first_ran in (False, True):
            with self.subTest(first_ran=first_ran):
                name = f"stall-{int(first_ran)}"; root = self.tmp / name; marker = root / "ran"
                launches = []
                def run(argv, timeout=600.0, stdin=None):
                    if "setsid nohup" in argv[-1]:
                        launches.append(1)
                        if len(launches) == 1:
                            if first_ran:
                                execution.subprocess_run(argv, timeout, stdin)
                            return 124, f"no answer within {int(timeout)} s"
                    return execution.subprocess_run(argv, timeout, stdin)
                parts = [{"id": "a", "label": "a", "script": f"echo ran >> {marker}"}]
                ex = self.wait(execution.Execution(self.plan(parts, name), root=str(root), run=run, key=self.KEY, url=self.URL).start())
                self.assertEqual(ex.state, "done", ex.lines)
                self.assertEqual(len(launches), 2)
                self.assertTrue(any("did not answer the launch within 60 s; asking once more" in l for l in ex.lines))
                self.assertEqual(marker.read_text(), "ran\n")                                     # started once, whoever started it

    def test_a_launch_never_answered_twice_fails_saying_so(self):
        def run(argv, timeout=600.0, stdin=None):
            if "setsid nohup" in argv[-1]:
                return 124, f"no answer within {int(timeout)} s"
            return execution.subprocess_run(argv, timeout, stdin)
        ex = self.wait(execution.Execution(self.plan([{"id": "a", "label": "a", "script": "true"}], "stall-twice"),
                                           root=str(self.tmp / "stall-twice"), run=run, key=self.KEY, url=self.URL).start())
        self.assertEqual((ex.state, ex.step), ("failed", "run"))
        self.assertIn("did not answer the launch, twice, within 60 s each; nothing is known to have started there", ex.error)

    def test_a_key_or_endpoint_with_a_line_break_is_refused_for_what_it_is(self):
        """A line break the launcher's lines cannot carry - possible only from
        the process environment, since the Settings file refuses one - is
        refused before anything is sent, naming the variable and never the
        value; a restored run refuses it before it launches a part. Never
        taken for a stdin cut short."""
        secret = "0123456789secret"
        for target in ("here", "host"):
            for which, var in (("key", "MODEL_API_KEY"), ("url", "MODEL_API_URL")):
                for brk in ("\n", "\r", "\r\n"):
                    for start in ("fresh", "restored"):
                        with self.subTest(target, var=var, brk=repr(brk), start=start):
                            name = f"{target}-{which}-{len(brk)}{brk == chr(13)}-{start}"; root = self.tmp / name; marker = root / "ran"
                            key, url = (f"sk-{brk}{secret}", self.URL) if which == "key" else (self.KEY, f"http://127.0.0.1:8000{brk}/v1{secret}")
                            parts = [{"id": "a", "label": "a", "script": f"echo ran > {marker}"}]
                            plan = (execution.Plan(name=name, script="", parts=parts) if target == "here" else self.plan(parts, name))
                            sent = []
                            def run(argv, timeout=600.0, stdin=None, sent=sent):
                                sent.append(argv[-1])
                                return execution.subprocess_run(argv, timeout, stdin)
                            if start == "fresh":
                                ex = self.wait(execution.Execution(plan, root=str(root), run=run, key=key, url=url).start())
                                self.assertEqual(sent, [])                                          # nothing sent, nothing asked
                            else:
                                # a record of a part about to be launched, restored with that key or endpoint
                                execution.Execution(plan, root=str(root), key=self.KEY, url=self.URL, record={"state": "running", "part": 0})._save()
                                execution.EXECUTIONS.clear()
                                back = execution.restore(str(root), run=run, key=key, url=url)
                                self.assertEqual(len(back), 1); ex = self.wait(back[0])
                                self.assertFalse(any("setsid nohup" in c for c in sent))             # the launcher never asked
                            self.assertEqual((ex.state, ex.step), ("failed", "credentials"), ex.lines)
                            self.assertTrue(ex.error.startswith(f"{var} holds a line break; "), ex.error)
                            self.assertNotIn("cut short", ex.error)
                            d = root / name if target == "here" else self.on_host(name)
                            self.assertFalse(d.exists() and any(p.name.startswith("run-a.") for p in d.iterdir()))   # not claimed, not started
                            self.assertFalse(marker.exists())
                            self.assertNotIn(secret, "\n".join(ex.lines))
                            for f in root.rglob("*"):
                                if f.is_file():
                                    self.assertNotIn(secret, f.read_text(errors="replace"), f)

    def test_the_whole_message_starts_the_part_with_its_key_and_endpoint(self):
        for target in ("here", "host"):
            with self.subTest(target):
                parts = [{"id": "a", "label": "a", "script": 'echo "key=${#API_TOKEN} url=$API_URL"'}]
                plan = execution.Plan(name=target, script="", parts=parts) if target == "here" else self.plan(parts, target)
                ex = self.wait(execution.Execution(plan, root=str(self.tmp / "runs"), key=self.KEY, url=self.URL).start())
                self.assertEqual((ex.state, ex.error), ("done", None), ex.lines)
                self.assertIn(f"key={len(self.KEY)} url={self.URL}", ex.lines)

    def test_nullius_gone_as_it_sent_the_key_leaves_the_part_to_the_restart(self):
        """Nullius killed before the key reached the launcher: the launcher
        starts nothing, so the restart finds nothing there and starts the
        part itself, once, with the key."""
        marker = self.tmp / "ran"
        parts = [{"id": "a", "label": "a", "script": f'echo "key=${{#API_TOKEN}}" >> {marker}'}]
        ex = execution.Execution(execution.Plan(name="r", script="", parts=parts), root=str(self.tmp / "runs"), key=self.KEY, url=self.URL)
        real = ex._sh
        def sh(command, timeout=600.0, stdin=None):
            if "setsid nohup" not in command:
                return real(command, timeout, stdin)
            real(command, timeout, "")                                     # what reached the launcher before Nullius went
            ex._save = lambda: None; ex.log = lambda line: None
            raise RuntimeError("Nullius is gone")
        ex._sh = sh
        self.wait(ex.start())
        execution.EXECUTIONS.clear()
        back = execution.restore(str(self.tmp / "runs"), key=self.KEY, url=self.URL)
        self.assertEqual(len(back), 1); again = self.wait(back[0])
        self.assertEqual((again.state, again.error), ("done", None), again.lines)
        self.assertEqual(marker.read_text(), f"key={len(self.KEY)}\n")                  # ran once, with its key


class NameOnHost(LocalHost):
    """A run's name is its directory on the target. Here the route refuses a
    name already taken; on a host the run itself asks, before it sends
    anything - a leftover directory of the same name would otherwise read as
    this run's parts, started already."""

    def leftover(self):
        d = self.on_host(); d.mkdir(parents=True)
        for name, text in (("run-a.sh", "echo old\n"), ("run-a.launch", ""), ("run-a.pid", "999999\n"), ("run-a.log", "old\n"), ("run-a.exit", "0\n")):
            (d / name).write_text(text)
        return d, sorted(p.name for p in d.iterdir())

    def test_a_new_run_whose_directory_is_on_the_host_already_is_refused_before_anything_is_sent(self):
        d, was = self.leftover()
        ex = self.wait(execution.Execution(self.plan([{"id": "a", "label": "a", "script": f"echo new > {self.tmp / 'ran'}"}]),
                                           root=str(self.tmp / "runs"), key="k" * 20).start())
        self.assertEqual((ex.state, ex.error), ("failed", "a run named r exists already on bench; press Run again for a new name"), ex.lines)
        self.assertEqual(sorted(p.name for p in d.iterdir()), was)                 # nothing sent, nothing started
        self.assertFalse((self.tmp / "ran").exists())
        self.assertEqual(execution.name_taken("r"), "a run named r exists already; press Run again for a new name")   # the route's words

    def test_a_restored_run_is_not_refused_its_own_directory(self):
        """Nullius gone while a run on a host was being prepared: its
        directory there is its own, and the run goes on."""
        ex = execution.Execution(self.plan([{"id": "a", "label": "a", "script": "echo a-out"}]), root=str(self.tmp / "runs"), key="k" * 20)
        ex._save()
        self.on_host().mkdir(parents=True)                                         # the run's own, made before Nullius went
        execution.EXECUTIONS.clear()
        back = execution.restore(str(self.tmp / "runs"), key="k" * 20)
        self.assertEqual(len(back), 1); self.wait(back[0])
        self.assertEqual((back[0].state, back[0].error), ("done", None), back[0].lines)
        self.assertIn("a-out", back[0].lines)


class Recipe(Base):
    def test_the_recipe_is_part_of_the_record_and_survives_a_restart(self):
        """What the builder posted, kept beside the script so the run can be
        opened again as a recipe; a plan from before has none."""
        recipe = {"version": 1, "harness": "harbor", "tasks": "", "customAgents": ["my_agent:MyAgent"], "agents": ["custom-0", "tau3-llm-agent"]}
        ex = self.wait(execution.Execution(execution.Plan(name="fin", script="true", recipe=recipe), root=str(self.tmp)).start())
        execution.EXECUTIONS.clear()
        execution.restore(str(self.tmp))
        self.assertEqual(execution.EXECUTIONS[ex.id].plan.recipe, recipe)
        self.assertEqual(execution.Plan.from_dict({"name": "old", "script": "true"}).recipe, {})
        # a record from when a plan could name an installed Harbor release still loads
        old = execution.Plan.from_dict({"name": "old", "script": "true", "harbor_version": "0.22.0", "wheels": ["/w.whl"], "requires_python": ">=3.12"})
        self.assertEqual((old.name, old.runner), ("old", "harbor"))


class Refused(Base):
    """A run refused before it starts leaves a job that was never written:
    the run names why - from the job's folder, where the standard's cli says
    so, or from its own failure before any part was launched - and the runs
    page reads it as refused, never as a job elsewhere."""

    def plan(self):
        return execution.Plan(name="std-r", script="", runner="standard", jobs=[{"name": "std-r"}],
                              parts=[{"id": "ref", "label": "Reference", "script": "true"}])

    def test_the_cli_s_refusal_in_the_job_folder_is_the_reason(self):
        from nullius import progress
        ex = execution.Execution(self.plan(), root=str(self.tmp / "runs"), record={"id": "r", "state": "failed", "launched": "ref",
                                                                                  "ends": {"ref": {"rc": 2, "at": 1.0, "label": "Reference"}}})
        self.assertIsNone(ex.refused("std-r"))                                       # it ran, and its job is not here: not a refusal
        progress.Reporter(os.path.join(ex.jobs, "std-r")).emit("refused", detail="the docker backend: no Node build pinned")
        self.assertEqual(ex.refused("std-r"), "the docker backend: no Node build pinned")
        self.assertEqual(sorted(os.listdir(os.path.join(ex.jobs, "std-r"))), ["progress.jsonl"])   # kept, and nothing reads it as a job

    def test_a_run_that_failed_before_any_part_was_refused(self):
        ex = execution.Execution(self.plan(), root=str(self.tmp / "runs"), key="", env={})
        ex.plan.key_var = "OPENAI_API_KEY"
        ex._main()
        self.assertEqual((ex.state, ex.step), ("failed", "credentials"))
        self.assertIn("is not set in Settings", ex.refused("std-r"))
        alive = execution.Execution(self.plan(), root=str(self.tmp / "runs2"), record={"id": "a", "state": "running"})
        self.assertIsNone(alive.refused("std-r"))                                    # still going: nothing to say yet


class TasksOnHost(unittest.TestCase):
    def test_two_folders_of_one_name_never_share_a_copy_and_one_folder_keeps_its_own(self):
        a, b = execution._tasks_place("/data/projA/tasks", "r1"), execution._tasks_place("/data/projB/tasks", "r2")
        self.assertNotEqual(a, b)
        self.assertEqual((a[-1], b[-1]), ("tasks", "tasks"))              # the dataset still reads as the folder's name
        self.assertEqual(a, execution._tasks_place("/data/projA/tasks/", "r3"))


class OnHost(HostBase):
    def test_the_part_runs_detached_with_the_host_s_harbor_and_the_job_mirrored_back(self):
        ex = self.wait(execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer, key="sk-verysecretkey12345", url="").start())
        self.assertEqual((ex.state, ex.exit_code, ex.error), ("done", 0, None), ex.lines)
        self.assertIn("  ecs-bench: docker yes · harbor 0.22.0", ex.lines)                                       # the harbor the host has, said
        cmds = self.ssh_commands()
        self.assertFalse(any("astral.sh" in c or "uv venv" in c for c in cmds))                                  # nothing installed for Harbor, uv or not
        rsyncs = [c for c, _ in self.calls if c[0] == "rsync"]
        place = "/".join(execution._tasks_place(str(self.tmp / "tasks"), "x"))
        self.assertEqual(rsyncs[0][-2:], [str(self.tmp / "tasks") + "/", f"bench:~/.nullius/{place}"])             # the tasks went over, under the folder's own name
        self.assertRegex(place, r"^tasks/[0-9a-f]{10}/tasks$")
        self.assertTrue(all(c[-2].startswith("bench:~/.nullius/runs/tau3-x3/jobs/") for c in rsyncs[1:]))           # the job came back
        self.assertGreaterEqual(len(rsyncs), 2)
        landed = next(s for c, s in self.calls if c[0] == "ssh" and "cat > " in c[-1] and "run-run.sh" in c[-1])
        self.assertEqual(landed, self.plan.script + "\n")                                                            # the part's script, over stdin
        launch = next((c, s) for c, s in self.calls if c[0] == "ssh" and "setsid nohup" in c[-1])
        self.assertIn("BatchMode=yes", launch[0]); self.assertIn("read -r K", launch[0][-1])
        self.assertIn("NULLIUS_HARBOR=harbor ", launch[0][-1]); self.assertIn(f'NULLIUS_TASKS="$HOME/.nullius/{place}"', launch[0][-1])
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', launch[0][-1])                                     # where a uv-installed harbor lives
        self.assertIn('NULLIUS_JOBS="$HOME/.nullius/runs/tau3-x3/jobs"', launch[0][-1]); self.assertIn("API_MODEL_NAME=openai/gpt-5", launch[0][-1])
        self.assertNotIn("sk-verysecretkey12345", launch[0][-1]); self.assertEqual(launch[1], "sk-verysecretkey12345\n\n@end\n")   # stdin: the key, the endpoint, the end line
        self.assertIn("harbor: starting", ex.lines); self.assertNotIn("sk-verysecretkey12345", "\n".join(ex.lines))
        self.assertEqual(json.loads((self.tmp / "runs" / "tau3-x3" / "run.json").read_text())["plan"]["host"], "bench")

    def test_no_tasks_nothing_sent(self):
        self.plan.tasks = ""
        ex = self.wait(execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer, key="k" * 20).start())
        self.assertEqual(ex.state, "done")
        self.assertFalse(any("tasks" in c[-1] for c, _ in self.calls if c[0] == "rsync"))

    def test_no_docker_no_run(self):
        def answer(argv, timeout=0, stdin=None):
            return 0, "ok\nuv=yes\ndocker=no\nharbor=no\nhostname=x\n"
        ex = self.wait(execution.Execution(self.plan, root=str(self.tmp / "runs"), run=answer, key="k" * 20).start())
        self.assertEqual((ex.state, ex.step), ("failed", "connect")); self.assertIn("docker is not on the host", ex.error)

    def test_no_harbor_on_the_host_no_run_and_nothing_sent(self):
        self.have_harbor = False
        ex = self.wait(execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer, key="k" * 20).start())
        self.assertEqual((ex.state, ex.step), ("failed", "connect")); self.assertIn("harbor is not on the host's PATH", ex.error)
        self.assertFalse(any(c[0] == "rsync" for c, _ in self.calls))                                         # the tasks never left
        self.assertFalse(any("astral.sh" in c for c in self.ssh_commands()))

    def test_a_run_of_a_runner_no_longer_offered_is_refused_on_a_host_and_nothing_sent(self):
        self.plan.runner, self.plan.tasks = "longmemeval", ""          # A run record with an unsupported runner.
        ex = self.wait(execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer, key="k" * 20).start())
        self.assertEqual((ex.state, ex.step), ("failed", "connect"))
        self.assertIn("is no longer offered", ex.error)
        self.assertFalse(any("setsid nohup" in c for c in self.ssh_commands()))

    def test_stop_kills_the_detached_process_on_the_host(self):
        polls = {"n": 0}
        def answer(argv, timeout=0, stdin=None):
            self.calls.append((argv, stdin)); cmd = argv[-1]
            if argv[0] == "rsync": return 0, ""
            if "printf 'ok" in cmd: return 0, "ok\nuv=yes\ndocker=yes\nharbor=yes\ndocker_ok=yes\nhostname=x\n"
            if "setsid nohup" in cmd: return 0, "777\n"
            if "__nullius__" in cmd: polls["n"] += 1; return 0, "\n__nullius__ alive"
            return 0, ""
        ex = execution.Execution(self.plan, root=str(self.tmp / "runs"), run=answer, key="k" * 20).start()
        self.until(ex, "running")
        for _ in range(50):
            if polls["n"]: break
            time.sleep(0.02)
        ex.stop(); self.wait(ex)
        self.assertEqual(ex.state, "stopped"); self.assertTrue(self.killed(777))


class StandardOnHost(HostBase):
    """A standard recipe's part on a host: python3 and git there, docker when
    its units run in containers - checked before anything is sent - then this
    runtime's payload sent as a task folder is, and the part run
    from it with the key over stdin."""
    COMMIT = "c0ffee" * 6 + "c0ff"

    def setUp(self):
        super().setUp()
        self.plan = execution.Plan(name="std-shard-2of2", script="", host=hosts.parse("bench"), runner="standard", model="gpt-5-mini",
                                   parts=[{"id": "fake", "label": "Fake agent · shard 2/2",
                                           "script": 'cd "${NULLIUS_STANDARD:-/repo}"\nexec python3 -m nullius.standard.cli run fakebench fake --shard 2/2 --from-run\n'}],
                                   jobs=[{"name": "std-shard-2of2", "agent": "fake", "dataset": "fakebench"}])
        self.plan.key_var, self.plan.url_var = "OPENAI_API_KEY", ""
        (self.tmp / "standard-app" / self.COMMIT / "app").mkdir(parents=True)        # unpacked here already, once for this commit
        payload = patch.object(execution.standard_payload, "build", return_value=(self.COMMIT, str(self.tmp / "standard-app" / self.COMMIT)))
        payload.start(); self.addCleanup(payload.stop)
        self.have_docker, self.missing, self.podman = True, "", ""

    def answer(self, argv, timeout=0, stdin=None):
        if argv[0] == "git":
            self.calls.append((argv, stdin))
            return 0, (self.COMMIT + "\n") if "rev-parse" in argv else ""
        if argv[0] != "rsync" and "printf 'ok" in argv[-1]:
            self.calls.append((argv, stdin))
            return 0, f"ok\nuv=yes\ndocker={'yes' if self.have_docker else 'no'}\n{self.podman}harbor=no\nhostname=gpu-1\n"
        if argv[0] != "rsync" and "@missing" in argv[-1]:
            self.calls.append((argv, stdin))
            return 0, "".join(f"@missing {m}\n" for m in self.missing.split())
        return super().answer(argv, timeout, stdin)

    def run_it(self):
        return self.wait(execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer, key="sk-verysecretkey12345", url="").start())

    def test_app_is_sent_at_its_content_address_and_the_part_runs_from_it(self):
        ex = self.run_it()
        self.assertEqual((ex.state, ex.exit_code), ("done", 0), ex.lines)
        rsyncs = [c for c, _ in self.calls if c[0] == "rsync"]
        self.assertEqual(rsyncs[0][-2:], [str(self.tmp / "standard-app" / self.COMMIT) + "/", f"bench:~/.nullius/app/{self.COMMIT}"])
        self.assertEqual({c[-2] for c in rsyncs[1:]}, {"bench:~/.nullius/runs/std-shard-2of2/jobs/",
                                                    "bench:~/.nullius/runs/std-shard-2of2/catalogue/"})
        launch = next((c, s) for c, s in self.calls if c[0] == "ssh" and "setsid nohup" in c[-1])
        self.assertIn(f'NULLIUS_STANDARD="$HOME/.nullius/app/{self.COMMIT}"', launch[0][-1])
        self.assertIn('NULLIUS_CATALOGUE="$HOME/.nullius/runs/std-shard-2of2/catalogue"', launch[0][-1])
        self.assertTrue(any("cp -R" in c and f"app/{self.COMMIT}/app/nullius/catalogue" in c
                            and "catalogue.part" in c for c in self.ssh_commands()))
        self.assertNotIn("sk-verysecretkey12345", launch[0][-1]); self.assertEqual(launch[1], "sk-verysecretkey12345\n\n@end\n")
        self.assertFalse(any("astral.sh" in c for c in self.ssh_commands()))                        # uv was there
        self.assertEqual(json.loads((self.tmp / "runs" / "std-shard-2of2" / "run.json").read_text())["app_on_target"],
                         f"~/.nullius/app/{self.COMMIT}")
        self.assertEqual(json.loads((self.tmp / "runs" / "std-shard-2of2" / "run.json").read_text())["catalogue_on_target"],
                         "~/.nullius/runs/std-shard-2of2/catalogue")

    def test_gaias_hugging_face_token_is_handed_from_data_providers_alone(self):
        # over the launcher's stdin as a licence is, read from Settings → Data providers; a value GAIA's own folder
        # holds under that name is never sent
        from nullius import secrets
        from nullius.standard import definitions, recipe
        gaia = definitions.get("benchmark", "gaia")
        out = recipe.compile({"version": 1, "harness": "standard", "name": "gaia-nop", "title": "t", "benchmark": "gaia",
                              "dataset": "gaia-validation", "other": "nop", "provider": "openai", "model": "", "limit": 1,
                              "concurrency": 1, "unsandboxed": False, "where": "local"}, {"benchmark": [gaia], "agent": [], "capability": []})
        self.assertEqual(out["licences"], {"benchmark": "gaia", "names": ["HF_TOKEN"], "providers": {"HF_TOKEN": "huggingface"}})
        secrets.set("gaia", "HF_TOKEN", "hf-stale-not-real")
        secrets.set(secrets.HUGGINGFACE, "HF_TOKEN", "hf-synthetic-not-real")
        self.plan.licences = out["licences"]
        ex = self.run_it()
        self.assertEqual((ex.state, ex.exit_code), ("done", 0), ex.lines)
        stdin = next(s for c, s in self.calls if c[0] == "ssh" and "setsid nohup" in c[-1])
        line = next(l for l in stdin.splitlines() if l.startswith(secrets.HANDED + "="))
        self.assertEqual(secrets.unpack(line.split("=", 1)[1]), {"HF_TOKEN": b"hf-synthetic-not-real"})
        self.assertNotIn("hf-synthetic-not-real", "\n".join(ex.lines))

    def test_the_price_list_goes_with_the_run_even_empty_so_the_host_s_own_is_never_read(self):
        ex = self.run_it()
        sent = next(s for c, s in self.calls if c[0] == "ssh" and "cat > " in c[-1] and "/prices.json" in c[-1])
        self.assertEqual((json.loads(sent)["models"], json.loads(sent)["own"]), ({}, {}))  # no list here: an empty one
        launch = next(c for c, _ in self.calls if c[0] == "ssh" and "setsid nohup" in c[-1])
        self.assertIn("NULLIUS_PRICES=", launch[-1])
        self.calls.clear(); self.polls = 0
        price_list.set_own("gpt-5-mini", price_list.row(1, 2))
        self.plan.name = "std-priced"
        ex = self.run_it()
        self.assertEqual((ex.state, ex.exit_code), ("done", 0), ex.lines)
        sent = next(s for c, s in self.calls if c[0] == "ssh" and "cat > " in c[-1] and "/prices.json" in c[-1])
        self.assertEqual(json.loads(sent)["own"], {"gpt-5-mini": {"input": 1.0, "output": 2.0}})
        launch = next(c for c, _ in self.calls if c[0] == "ssh" and "setsid nohup" in c[-1])
        self.assertIn('NULLIUS_PRICES="$HOME/.nullius/runs/std-priced/prices.json"', launch[-1])

    def test_what_it_does_is_said_in_stages_its_own_and_the_cli_s(self):
        """Getting the host ready is said stage by stage in the run's log, and
        the cli's own stages come through the part's log: the page folds them."""
        from nullius import progress
        self.plan.docker = True
        self.part_log = "".join(progress.line(*a, **kw) + "\n" for a, kw in [
            (("plan",), {"stages": "image,install,units"}), (("image", "start"), {"detail": "building nullius/base:abc (first time on this machine)"}),
            (("image", "done"), {"seconds": 95}), (("install", "cached"), {"agent": "reference"}),
            (("units", "start"), {"n": 2, "noun": "problems"}), (("unit", "done"), {"i": 1, "of": 2, "unit": "p/1", "outcome": "passed", "reward": 1.0})])
        ex = self.run_it()
        self.assertEqual(ex.state, "done", ex.lines)
        said = [progress.parse(l) for l in ex.lines if progress.parse(l)]
        self.assertEqual([(e["stage"], e.get("state")) for e in said][:6],
                         [("plan", None), ("connect", "start"), ("connect", "done"), ("uv", "cached"), ("app", "start"), ("app", "done")])
        got = ex.summary()["stages"]
        self.assertEqual([(s["key"], s["state"]) for s in got["stages"]],
                         [("connect", "done"), ("uv", "cached"), ("app", "done"), ("image", "done"), ("install:reference", "cached"), ("units", "running")])
        self.assertEqual((got["units"]["n"], got["units"]["done"], got["units"]["passed"]), (2, 1, 1))
        back = execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer,
                                   record=json.loads((self.tmp / "runs" / "std-shard-2of2" / "run.json").read_text()))
        self.assertEqual(back.summary()["stages"], got)                                  # read back from the log on a restart

    def test_a_docker_recipe_on_a_host_without_docker_sends_nothing(self):
        self.plan.docker, self.have_docker = True, False
        ex = self.run_it()
        self.assertEqual((ex.state, ex.step), ("failed", "connect")); self.assertIn("runs its units in containers", ex.error)
        self.assertEqual(ex.summary()["stages"]["stages"][0]["state"], "failed")                  # the stage it failed at, said so
        self.assertFalse(any(c[0] == "rsync" for c, _ in self.calls))
        self.assertFalse(any("cat > " in c for c in self.ssh_commands()))

    def test_a_docker_recipe_on_a_host_with_podman_alone_runs_when_podman_answers(self):
        self.plan.docker, self.have_docker, self.podman = True, False, "podman=yes\npodman_ok=no\n"
        ex = self.run_it()
        self.assertEqual((ex.state, ex.step, ex.error), ("failed", "connect", "podman is on the host but this user cannot use it (podman ps failed)"))
        self.calls.clear(); execution.EXECUTIONS.clear(); self.podman = "podman=yes\npodman_ok=yes\n"
        self.plan.name = "std-podman"
        self.assertEqual(self.run_it().state, "done")

    def test_a_docker_recipe_here_asks_the_backends_own_engine(self):
        self.plan.host, self.plan.docker = None, True
        with patch.object(execution.container, "DOCKER", "podman"), patch.object(execution, "docker_here", return_value=None) as asked:
            self.assertEqual(self.run_it().state, "done")
        self.assertEqual(asked.call_args.args[1:], ("this recipe runs its units in containers", "podman"))

    def test_a_local_recipe_needs_no_docker_but_python3_and_git(self):
        self.have_docker = False
        self.assertEqual(self.run_it().state, "done")
        self.calls.clear(); execution.EXECUTIONS.clear(); self.missing = "python3"
        self.plan.name = "std-again"
        ex = self.run_it()
        self.assertEqual((ex.state, ex.step), ("failed", "connect")); self.assertIn("python3 not on the host", ex.error)
        self.assertFalse(any(c[0] == "rsync" for c, _ in self.calls))

    def test_on_this_machine_it_runs_from_this_repository_and_nothing_is_packed(self):
        self.plan.host = None
        ex = self.run_it()
        self.assertEqual(ex.state, "done", ex.lines)
        launch = next(c for c, _ in self.calls if "setsid nohup" in c[-1])
        self.assertIn("NULLIUS_STANDARD=" + shlex.quote(execution.REPO), launch[-1])
        self.assertFalse(any(c[0] in ("git", "rsync") for c, _ in self.calls))

    def test_an_installed_local_runner_uses_a_snapshot_and_its_catalogue(self):
        self.plan.host = None
        with patch.object(execution, "REPO", str(self.tmp / "site-packages")):
            ex = self.run_it()
        self.assertEqual(ex.state, "done", ex.lines)
        launch = next(c for c, _ in self.calls if "setsid nohup" in c[-1])
        here = self.tmp / "standard-app" / self.COMMIT
        self.assertIn("NULLIUS_STANDARD=" + shlex.quote(str(here)), launch[-1])
        self.assertIn("NULLIUS_CATALOGUE=" + shlex.quote(str(self.tmp / "catalogue")), launch[-1])
        self.assertFalse(any(c[0] in ("git", "rsync") for c, _ in self.calls))

    def test_an_unsafe_payload_stops_before_any_copy_or_launch(self):
        with patch.object(execution.standard_payload, "build", side_effect=ValueError("runner resources cannot be symlinks")):
            ex = self.run_it()
        self.assertEqual((ex.state, ex.step), ("failed", "app"))
        self.assertFalse(any(c[0] == "rsync" or "setsid nohup" in c[-1] for c, _ in self.calls))

    def test_job_evidence_and_remote_proofs_are_mirrored_beside_the_run(self):
        remote = self.tmp / "remote-run"
        (remote / "jobs/proof").mkdir(parents=True)
        (remote / "jobs/proof/result.json").write_text('{"fixture": "job evidence"}')
        (remote / "catalogue/agents/example").mkdir(parents=True)
        (remote / "catalogue/agents/example/agent.json").write_text('{"proven": {"fixture-proof": {"job": "proof"}}}')
        ex = execution.Execution(self.plan, root=str(self.tmp / "runs"), key="fixture")
        ex.catalogue_on_target = "~/.nullius/runs/std-shard-2of2/catalogue"
        def copy(argv, timeout):
            group = "catalogue" if argv[-2].endswith("/catalogue/") else "jobs"
            shutil.copytree(remote / group, argv[-1], dirs_exist_ok=True)
            return 0, ""
        self.assertEqual(ex._copy_back(run=copy), (0, ""))
        self.assertEqual(json.loads((Path(ex.jobs) / "proof/result.json").read_text()), {"fixture": "job evidence"})
        self.assertEqual(json.loads((Path(ex.dir) / "catalogue/agents/example/agent.json").read_text())["proven"]["fixture-proof"],
                         {"job": "proof"})
        self.assertFalse((self.tmp / "catalogue").exists())
        ex._save()
        recovered = execution.Execution(self.plan, root=str(self.tmp / "runs"),
                                        record=json.loads((Path(ex.dir) / "run.json").read_text()))
        self.assertEqual(recovered.catalogue_on_target, ex.catalogue_on_target)


class PinnedHarbor(HostBase):
    """A plan that carries the Harbor its recipe pins (harbor/source.py):
    here it runs through a ``harbor`` script in the run directory that calls
    uvx; on a host the same, with a repository's wheels built here sent over
    first and uv provisioned - and harbor need not be on any PATH."""
    COMMIT = "c" * 40

    def setUp(self):
        super().setUp()
        self.pins = self.tmp / "pins"
        self.env = {"HARBOR_PINS": str(self.pins)}

    def seed(self, pin, record):
        d = self.pins / source.key_of(pin)
        (d / "wheels").mkdir(parents=True)
        for name in [record.get("wheel")] + [p["wheel"] for p in record.get("private", [])]:
            if name:
                (d / "wheels" / name).write_text("a wheel")
        (d / "pin.json").write_text(json.dumps(record))

    def test_a_release_pin_runs_through_uvx_here(self):
        pin = {"kind": "release", "version": "0.24.0", "extras": ["dspy"]}
        self.seed(pin, {"kind": "release", "version": "0.24.0", "requires_python": ">=3.12", "extras": ["dspy"], "wheels": [], "private": []})
        bin_ = self.tmp / "bin"; bin_.mkdir()
        (bin_ / "uvx").write_text('#!/bin/sh\necho "uvx $*"\n'); (bin_ / "uvx").chmod(0o755)
        plan = execution.Plan(name="pinned", script='"$NULLIUS_HARBOR" run --agent nop; echo "harbor=$NULLIUS_HARBOR"', harbor_pin=pin)
        # a home of its own: the run puts ~/.local/bin first on its PATH, where a real uvx may live
        with patch.dict(os.environ, {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.tmp)}):
            ex = self.wait(execution.Execution(plan, root=str(self.tmp / "runs"), key="k" * 20, url="", env=self.env).start())
        self.assertEqual((ex.state, ex.error), ("done", None), ex.lines)
        text = "\n".join(ex.lines)
        run_dir = self.tmp / "runs" / "pinned"
        self.assertIn(f"uvx --isolated --python >=3.12 --from harbor[dspy]==0.24.0 python {run_dir / 'harbor'}-feed.py run --agent nop", text)   # the pin, with its extras and the live feed
        self.assertIn("def install(Job", (run_dir / "harbor-feed.py").read_text())
        self.assertIn(f"harbor={self.tmp / 'runs' / 'pinned' / 'harbor'}", text)
        self.assertTrue(any(l.startswith('[nullius] stage=harbor state=start detail="harbor 0.24.0 [dspy] from PyPI"') for l in ex.lines))

    def test_a_repository_pin_on_a_host_sends_its_wheels_and_needs_no_harbor_there(self):
        self.have_harbor = False
        pin, _ = source.pin_of({"kind": "repo", "repo": "file:///srv/git/harbor.git", "revisionKind": "tag", "revision": "v1", "commit": self.COMMIT})
        self.seed(pin, {"kind": "repo", "url": pin["url"], "repo": pin["repo"], "commit": self.COMMIT, "version": "0.22.0+fork.1", "requires_python": ">=3.12",
                        "wheel": "harbor-0.22.0+fork.1-py3-none-any.whl", "private": [{"name": "priv-core", "wheel": "priv_core-0.1.9-py3-none-any.whl", "direct": False}], "extras": []})
        self.plan.harbor_pin = pin
        ex = self.wait(execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer, key="k" * 20, env=self.env).start())
        self.assertEqual((ex.state, ex.error), ("done", None), ex.lines)
        self.assertIn("  ecs-bench: docker yes · uv no", ex.lines)                                       # no harbor asked of the host
        self.assertTrue(any("astral.sh/uv/install.sh" in c for c in self.ssh_commands()))               # uv, which runs the pin
        wheels = [c for c, _ in self.calls if c[0] == "rsync" and c[-1].endswith("/wheels")]
        self.assertEqual(wheels[0][-2:], [str(self.pins / ("git-" + self.COMMIT) / "wheels") + "/", f"bench:~/.nullius/harbor/git-{self.COMMIT}/wheels"])
        script = next(s for c, s in self.calls if c[0] == "ssh" and 'cat > "$HOME/.nullius/runs/tau3-x3/harbor"' in c[-1])
        self.assertIn(f'w="$HOME/.nullius/harbor/git-{self.COMMIT}"/wheels', script)
        self.assertIn('uvx --isolated --python \'>=3.12\' --find-links "$w" --from "harbor @ $w/"harbor-0.22.0+fork.1-py3-none-any.whl python "$0-feed.py" "$@"', script)
        sent = next(s for c, s in self.calls if c[0] == "ssh" and 'cat > "$HOME/.nullius/runs/tau3-x3/harbor-feed.py"' in c[-1])
        self.assertIn("def install(Job", sent)                                                         # the live feed, beside it
        launch = next(c[-1] for c, _ in self.calls if c[0] == "ssh" and "setsid nohup" in c[-1])
        self.assertIn('NULLIUS_HARBOR="$HOME/.nullius/runs/tau3-x3/harbor"', launch)

    def test_a_pin_that_cannot_be_built_stops_the_run_before_the_host(self):
        pin, _ = source.pin_of({"kind": "repo", "repo": "owner/harbor", "commit": self.COMMIT})
        self.plan.harbor_pin = pin
        with patch.object(source, "ensure", side_effect=source.BuildError("fetching owner/harbor failed: Repository not found")):
            ex = self.wait(execution.Execution(self.plan, root=str(self.tmp / "runs"), run=self.answer, key="k" * 20, env=self.env).start())
        self.assertEqual((ex.state, ex.step), ("failed", "harbor")); self.assertIn("Repository not found", ex.error)
        self.assertTrue(any('detail="harbor from owner/harbor, commit cccccccccccc: building it here, once for this commit"' in l for l in ex.lines))
        self.assertEqual(self.calls, [])                                                                 # nothing asked of the host

    def test_the_pin_is_kept_in_the_record_and_an_old_record_has_none(self):
        pin = {"kind": "release", "version": "0.24.0", "extras": []}
        back = execution.Plan.from_dict(execution.Plan(name="p", script="true", harbor_pin=pin).to_dict())
        self.assertEqual(back.harbor_pin, pin)
        self.assertEqual(execution.Plan.from_dict({"name": "old", "script": "true", "host": None}).harbor_pin, {})   # from before the pin: harbor on PATH


if __name__ == "__main__":
    unittest.main()
