"""The CLIs, driven the way a shell drives them — `docs/cli.md` pinned.

`nullius.importers.cli`, `nullius.runs.cli` and the others are run as
subprocesses (`python -m`, `PYTHONPATH=app`) for what only a real process
shows: the help text, the exit code, what lands on stdout against stderr, and
what the command left behind on disk. The paths that need a fake — a socket
that must never open — call `main()` in-process with the fake patched in.

Fixtures: `fixtures/harbor-jobs` is a small hand-made Harbor job. The errored
trial (an exception carrying a credential-shaped string) is synthetic, built
in a temp directory by the test that reads it.

Nothing here opens a socket, reads a dotenv file, or writes outside the temp
directory a test hands the command.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nullius import workspace, workspace_cli, paths
from nullius.compare import cli as compare_cli
from nullius.importers import cli as import_cli
from nullius.runs import cli as runs_cli
from nullius.runs import parse

APP = Path(__file__).resolve().parents[1]
FIXTURES = APP / "tests" / "fixtures"
JOBS = FIXTURES / "harbor-jobs"

# Credential-shaped, obviously fake, and matched by every pattern the
# redactor has: this is what must never reach a terminal.
SECRET = "sk-test-0000FAKE0000FAKE0000"


def run(module: str, *args, cwd: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """One CLI as a shell runs it: `-m` and the package root on the path."""
    clean = dict(os.environ)
    clean.update(env or {})
    clean["PYTHONPATH"] = str(APP)
    return subprocess.run([sys.executable, "-m", f"nullius.{module}", *map(str, args)],
                          cwd=cwd, env=clean, capture_output=True, text=True)


def snapshot(root) -> list:
    """Every file under *root* with its size and mtime: what "unchanged" means."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            found.append((os.path.relpath(path, root), st.st_size, st.st_mtime_ns))
    return found


def errored_job(root: str, job_id: str = "errored-job") -> str:
    """A Harbor-shaped job whose one trial died in an API error, the message
    carrying a credential. Synthetic: the shape `parse_trial` reads, no more."""
    job = os.path.join(root, job_id)
    trial = os.path.join(job, "task-a__x1")
    os.makedirs(trial)
    with open(os.path.join(job, "result.json"), "w", encoding="utf-8") as fh:
        json.dump({"started_at": "2026-09-01T00:00:00+00:00", "n_total_trials": 1,
                   "stats": {"n_errored_trials": 1}}, fh)
    with open(os.path.join(job, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"agents": [{"name": "agent-x", "model_name": "local/m"}],
                   "tasks": [{"name": "some-dataset/task-a"}]}, fh)
    with open(os.path.join(trial, "result.json"), "w", encoding="utf-8") as fh:
        json.dump({"task_name": "some-dataset/task-a", "trial_name": "task-a__x1",
                   "exception_info": {"exception_type": "ApiError",
                                      "exception_message": f"401 for api_key={SECRET}"}}, fh)
    return job


class Scratch(unittest.TestCase):
    """A fresh cwd per test, so "left nothing behind" is a real assertion."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(self.cwd)

    def sh(self, module, *args, env=None):
        return run(module, *args, cwd=self.cwd, env=env)

    def assertCwdUntouched(self):
        self.assertEqual(os.listdir(self.cwd), [], "the command wrote into the cwd")


class Help(Scratch):
    def test_each_cli_has_help_with_examples_and_the_exit_codes(self):
        for module, args in (("importers.cli", []), ("runs.cli", []), ("runs.cli", ["show"]),
                             ("compare.cli", []), ("workspace_cli", []), ("workspace_cli", ["list"])):
            with self.subTest(module=module, args=args):
                out = self.sh(module, *args, "--help")
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn("usage:", out.stdout)
                self.assertIn(f"python3 -m nullius.{module}", out.stdout)
                self.assertIn("examples:", out.stdout)
                self.assertIn("exit codes:", out.stdout)
                self.assertIn("--json", out.stdout)
                for code in ("0  done", "2  usage", "3  input not found", "4  the"):
                    self.assertIn(code, out.stdout)
                self.assertEqual(out.stderr, "")
        self.assertIn("TRIAL", self.sh("runs.cli", "show", "--help").stdout)
        self.assertIn("--matched", self.sh("compare.cli", "--help").stdout)
        self.assertCwdUntouched()

    def test_the_subcommands_have_help_of_their_own(self):
        for sub in ("list", "show"):
            out = self.sh("runs.cli", sub, "--help")
            self.assertEqual(out.returncode, 0)
            self.assertIn("--json", out.stdout)
            self.assertIn("exit codes:", out.stdout)


class ExitCodes(Scratch):
    """docs/cli.md's table, one line at a time. The modules must agree on the
    numbers, and each documented case must produce its code."""

    def test_usage_errors_are_2_and_say_so_on_stderr(self):
        cases = [
            ("importers.cli", ["--bogus"]),
            ("importers.cli", []),                                  # run_dir missing
            ("importers.cli", [str(JOBS), "--harness", "nope"]),
            ("runs.cli", []),                                       # no command
            ("runs.cli", ["list"]),                                 # no root
            ("runs.cli", ["list", "--bogus", str(JOBS)]),
            ("runs.cli", ["show", str(JOBS)]),               # no job id
        ]
        for module, args in cases:
            with self.subTest(module=module, args=args):
                out = self.sh(module, *args)
                self.assertEqual(out.returncode, 2, out.stderr)
                self.assertIn("usage:", out.stderr)
                self.assertEqual(out.stdout, "")
        self.assertCwdUntouched()

    def test_a_missing_path_is_3(self):
        missing = os.path.join(self.tmp, "nowhere")
        cases = [
            ("importers.cli", [missing, "-o", os.path.join(self.tmp, "out")]),
            ("importers.cli", [missing, "--harness", "atif", "-o", os.path.join(self.tmp, "out")]),
            ("runs.cli", ["list", missing]),
            ("runs.cli", ["show", missing, "1"]),
        ]
        for module, args in cases:
            with self.subTest(module=module, args=args):
                out = self.sh(module, *args)
                self.assertEqual(out.returncode, 3, out.stderr)
                self.assertIn("no such directory", out.stderr)
                self.assertEqual(out.stdout, "")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out")))
        self.assertCwdUntouched()

    def test_an_empty_directory_is_3_for_reads_and_says_what_was_looked_for(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        out = self.sh("runs.cli", "list", empty)
        self.assertEqual(out.returncode, 3)
        self.assertIn("no job directory found", out.stderr)
        out = self.sh("runs.cli", "list", empty, "--json")
        self.assertEqual((out.returncode, out.stdout), (3, ""))
        out = self.sh("importers.cli", empty, "-o", os.path.join(self.tmp, "out"))
        self.assertEqual(out.returncode, 3)
        self.assertIn("no importer recognised", out.stderr)
        self.assertEqual(os.listdir(empty), [])
        self.assertCwdUntouched()

    def test_a_refused_operation_is_4(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        # the importer named outright finds nothing it can convert
        out = self.sh("importers.cli", empty, "--harness", "atif", "-o", os.path.join(self.tmp, "out"))
        self.assertEqual(out.returncode, 4, out.stderr)
        self.assertIn("no ATIF trajectories", out.stderr)
        self.assertEqual(out.stdout, "")
        self.assertCwdUntouched()


class Runs(Scratch):
    """`runs.cli`: what the runs list and the job page show, from the shell."""


    def test_an_errored_trial_shows_its_exception_and_never_the_credential(self):
        errored_job(self.tmp)
        with open(os.path.join(self.tmp, "errored-job", "task-a__x1", "result.json")) as fh:
            self.assertIn(SECRET, fh.read())   # it is on disk
        for args in (["show", self.tmp, "errored-job"], ["show", self.tmp, "errored-job", "--json"],
                     ["list", self.tmp, "--json"]):
            with self.subTest(args=args):
                out = self.sh("runs.cli", *args)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertNotIn(SECRET, out.stdout + out.stderr)
        out = self.sh("runs.cli", "show", self.tmp, "errored-job")
        self.assertRegex(out.stdout, r"(?m)^  task-a__x1\s+api_error\s+ApiError$")
        doc = json.loads(self.sh("runs.cli", "show", self.tmp, "errored-job", "--json").stdout)
        self.assertEqual(doc["trials"][0]["exception_message"], "401 for api_key=[REDACTED]")
        self.assertEqual(doc["counts"]["api_error"], 1)
        self.assertEqual(doc["n_infra"], 1)
        self.assertIsNone(doc["adjusted_solve_rate"])


# ------------------------------------------------------- the coverage gaps
# The trial view, the comparison and the workspace list. Same table, same rules:
# help with the exit codes, --json on stdout alone, 3 for what is not
# there, no credential on either stream, and nothing written by a read.

TRIAL_1 = "some-trial"                                # any name: a usage error comes first


def workspace_home(root: str, roots, label: str = "Failed jobs") -> tuple:
    """A workspaces home holding one workspace opened over *roots*, its
    parse cache filled the way the app fills it. (home, workspace)."""
    home = os.path.join(root, "workspaces")
    ws = workspace.create(home, [str(r) for r in roots], label)
    workspace.load_jobs(ws, ws.roots)
    return home, ws


class MoreExitCodes(Scratch):
    def test_usage_errors_are_2(self):
        cases = [
            ("compare.cli", []),                                       # no root
            ("compare.cli", [str(JOBS)]),                       # no job
            ("compare.cli", [str(JOBS), "1"]),                  # one job
            ("compare.cli", [str(JOBS), "1", "1"]),             # the same job twice
            ("compare.cli", [str(JOBS), "1", "2", "--bogus"]),
            ("workspace_cli", []),                                     # no command
            ("workspace_cli", ["list", "--bogus"]),
            ("workspace_cli", ["delete", "x"]),                        # not a command here
            ("runs.cli", ["show", str(JOBS), "1", TRIAL_1, "extra"]),
        ]
        for module, args in cases:
            with self.subTest(module=module, args=args):
                out = self.sh(module, *args)
                self.assertEqual(out.returncode, 2, out.stderr)
                self.assertIn("usage:", out.stderr)
                self.assertEqual(out.stdout, "")
        self.assertCwdUntouched()


class TrialShow(Scratch):
    """`runs.cli show <root> <job> <trial>`: the trial page from the shell."""

    def test_a_trial_without_a_trajectory_shows_its_exception_and_never_the_credential(self):
        errored_job(self.tmp)
        with open(os.path.join(self.tmp, "errored-job", "task-a__x1", "result.json")) as fh:
            self.assertIn(SECRET, fh.read())
        out = self.sh("runs.cli", "show", self.tmp, "errored-job", "task-a__x1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn(SECRET, out.stdout + out.stderr)
        self.assertIn("exception: ApiError: 401 for api_key=[REDACTED]", out.stdout)
        self.assertIn("evidence: trajectory absent · trace absent · reward absent", out.stdout)
        self.assertNotIn("steps:", out.stdout)
        out = self.sh("runs.cli", "show", self.tmp, "errored-job", "task-a__x1", "--json")
        self.assertNotIn(SECRET, out.stdout + out.stderr)
        doc = json.loads(out.stdout)
        self.assertEqual(doc["trial"]["exception_message"], "401 for api_key=[REDACTED]")
        self.assertIsNone(doc["trajectory"])
        self.assertEqual(doc["steps"], [])

    def test_a_credential_in_the_trajectory_is_redacted_on_the_way_out(self):
        # a synthetic trajectory whose message, tool name, target and
        # observation each carry the credential: the reader redacts every
        # string it keeps, and the document keeps no other
        job = errored_job(self.tmp)
        agent = os.path.join(job, "task-a__x1", "agent")
        os.makedirs(agent)
        with open(os.path.join(agent, "trajectory.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema_version": "ATIF-v1.5", "steps": [
                {"step_id": 1, "source": "user", "message": f"use {SECRET}"},
                {"step_id": 2, "source": "agent", "message": "(tool use)",
                 "tool_calls": [{"tool_call_id": "c1", "function_name": f"Bash {SECRET}",
                                 "arguments": {"command": f"curl -H 'x: {SECRET}'"}}],
                 "observation": {"results": [{"source_call_id": "c1", "content": f"leaked {SECRET}"}]}},
            ]}, fh)
        for args in (["show", self.tmp, "errored-job", "task-a__x1"],
                     ["show", self.tmp, "errored-job", "task-a__x1", "--json"]):
            with self.subTest(args=args):
                out = self.sh("runs.cli", *args)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertNotIn(SECRET, out.stdout + out.stderr)
                self.assertIn("[REDACTED]", out.stdout)


class Workspaces(Scratch):
    """`workspace_cli list`: the workspaces table, from the shell."""


    def test_the_home_is_workspaces_dir_when_not_named(self):
        home, ws = workspace_home(self.tmp, [JOBS])
        out = self.sh("workspace_cli", "list", env={"WORKSPACES_DIR": home})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(ws.name, out.stdout)
        # The per-user location is independent of cwd; use an empty profile.
        profile = os.path.join(self.tmp, "empty-profile")
        out = self.sh("workspace_cli", "list", env={"WORKSPACES_DIR": "", "NULLIUS_HOME": profile})
        self.assertEqual(out.returncode, 3)
        self.assertIn("no such directory: " + paths.workspaces_dir({"NULLIUS_HOME": profile}), out.stderr)
        self.assertCwdUntouched()

    def test_listing_opens_nothing_writes_nothing_and_prints_no_credential(self):
        # a workspace whose cached job carries the credential in its exception:
        # the cache holds the redacted message (parse redacts on the way in),
        # and the listing prints ids and counts, never a record
        errored_job(self.tmp)
        home, ws = workspace_home(self.tmp, [self.tmp], "Errored")
        before = snapshot(home)
        no_socket = patch.object(socket, "socket", side_effect=AssertionError("socket opened"))
        here = os.getcwd()
        os.chdir(self.cwd)
        try:
            with no_socket, patch.object(workspace, "load_jobs", side_effect=AssertionError("parsed")), \
                    patch.object(workspace, "touch", side_effect=AssertionError("opened")), \
                    patch.object(workspace, "save", side_effect=AssertionError("saved")):
                for argv in (["list", "--home", home], ["list", "--home", home, "--json"]):
                    with redirect_stdout(io.StringIO()) as so, redirect_stderr(io.StringIO()) as se:
                        self.assertEqual(workspace_cli.main(argv), 0)
                    self.assertNotIn(SECRET, so.getvalue() + se.getvalue())
                    self.assertIn("errored-job", so.getvalue())
        finally:
            os.chdir(here)
        self.assertEqual(snapshot(home), before)
        self.assertEqual(ws.opened, workspace.load(home, ws.name).opened)
        self.assertCwdUntouched()


class CompareScoredRuns(unittest.TestCase):
    """A benchmark that scores (an F1) and gives no pass or fail: the CLI
    carries the mean score, its breakdown and each task's score, as the
    page's rows do, and counts nothing as solved."""

    def runs(self):
        from datetime import datetime, timedelta
        from nullius.compare import comparison

        def scored(task, groups):
            n = sum(c for _, c in groups.values())
            return parse.Trial(
                job_id="run", dir="/unused", task_name=task, trial_name=task, task_base=task,
                source="locomo10", model_name="model", outcome="scored", reward=None,
                exception_type="", exception_message="", patch_exists=None, patch_applied=None,
                f2p_pass=0, f2p_fail=0, p2p_pass=0, p2p_fail=0, n_input_tokens=100,
                n_output_tokens=10, n_cache_tokens=0, cost_usd=None, dur_total=60, dur_env=0,
                dur_agent_setup=0, dur_agent=60, dur_verifier=0,
                score=sum(s for s, _ in groups.values()) / n, n_parts=n,
                part_groups={g: list(v) for g, v in groups.items()}, parts_by="category")

        def job(job_id, groups):
            start = datetime(2026, 9, 1)
            return parse.Job(job_id=job_id, dir="/unused", model_name="model", dataset="locomo10",
                             n_tasks=1, started_at=start, finished_at=start + timedelta(minutes=10),
                             trials=[scored("conv-26", groups)], agent_name="reader")

        a, b = job("a", {"1": (3.0, 4), "2": (1.0, 4)}), job("b", {"1": (2.0, 4), "2": (3.0, 4)})
        return comparison.build(tuple(comparison.SelectedRun(comparison.RunRef("ws", j.job_id), j, "ws")
                                      for j in (a, b)), "whole")

    def test_the_document_carries_the_scores(self):
        from nullius.compare import cli as compare_cli
        doc = compare_cli.document(self.runs())
        totals = [c["totals"] for c in doc["runs"]]
        self.assertEqual([t["mean_score"] for t in totals], [0.5, 0.625])
        self.assertEqual([[(g["group"], g["mean"]) for g in t["score_by_group"]] for t in totals],
                         [[("1", 0.75), ("2", 0.25)], [("1", 0.5), ("2", 0.75)]])
        self.assertTrue(all(t["scored_only"] for t in totals))
        self.assertEqual(doc["tasks"][0]["scores"], [0.5, 0.625])

    def test_the_table_prints_the_score_rows_and_nothing_solved(self):
        from nullius.compare import cli as compare_cli
        out = io.StringIO()
        with redirect_stdout(out):
            compare_cli._print(self.runs())
        text = out.getvalue()
        self.assertRegex(text, r"(?m)^  Mean score\s+0\.50\s+0\.62$")
        self.assertRegex(text, r"(?m)^  Category 1\s+0\.75\s+0\.50$")
        self.assertRegex(text, r"(?m)^  Category 2\s+0\.25\s+0\.75$")
        self.assertRegex(text, r"(?m)^  Solved\s+—\s+—$")
        self.assertIn("scored 0.50", text)


if __name__ == "__main__":
    unittest.main()
