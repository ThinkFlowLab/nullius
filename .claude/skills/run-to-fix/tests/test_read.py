"""`loop.py read`: stage 1 files, the adapter check and the refusals."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys

from jobs import (LIMITS, LOOP, NO_LIMITS, LoopCase, adapter, load, run, trial, write, write_job, write_json)


def healthy(root: str, name: str, agent: str) -> str:
    return write_job(root, name, agent, "harbor", [
        trial(f"t{i}__a", f"bench/task-{i}", 1.0 if i < 4 else 0.0) for i in range(6)])


class ReadWritesStageOne(LoopCase):
    def test_writes_input_health_and_metrics(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        base_dir = healthy(self.jobs, "base", "alpha")
        self.set_adapter(adapter("alpha", LIMITS, {}))
        self.read_ok(run_dir, base_dir)

        data = load(self.round_file("input.json"))
        with open(os.path.join(self.loop, "adapter.json"), "rb") as fh:
            self.assertEqual(data["adapter_sha256"], hashlib.sha256(fh.read()).hexdigest())
        self.assertEqual(data["run"]["job_dir"], run_dir)
        self.assertEqual(data["baseline"]["job_dir"], base_dir)
        self.assertEqual(data["budget_usd"], 10.0)
        self.assertEqual(data["pass_threshold"], 1.0)

        with open(self.round_file("health.txt"), encoding="utf-8") as fh:
            health = fh.read()
        self.assertIn(run_dir, health)
        self.assertIn(base_dir, health)

        metrics = load(self.round_file("metrics.json"))
        self.assertEqual([r["label"] for r in metrics["runs"]], ["baseline", "run"])
        self.assertEqual([r["trials_passed"] for r in metrics["runs"]], [4, 4])

    def test_without_baseline(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        self.assertIsNone(load(self.round_file("input.json"))["baseline"])
        self.assertEqual([r["label"] for r in load(self.round_file("metrics.json"))["runs"]], ["run"])

    def test_a_finished_round_starts_the_next(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        write_json(self.round_file("result.json"), {"verdict": "noise"})
        self.read_ok(run_dir, None)
        self.assertTrue(os.path.isfile(os.path.join(self.loop, "round-02", "input.json")))

    def test_an_open_round_past_stage_one_is_refused(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("round-01", r.err)
        self.assertFalse(os.path.exists(os.path.join(self.loop, "round-02")))


class ReadRefuses(LoopCase):
    def test_a_systemic_failure_names_the_shared_error(self) -> None:
        trials = []
        for i in range(8):
            t = trial(f"t{i}__a", f"bench/task-{i}", None)
            t.exception = ("BadRequestError", f"Error code: 402 - insufficient credits for request {1000 + i}")
            trials.append(t)
        run_dir = write_job(self.jobs, "stopped", "alpha", "harbor", trials)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("not analysable", r.err)
        self.assertIn("systemic failure", r.err)
        self.assertIn("insufficient credits", r.err)
        self.assertTrue(load(self.round_file("input.json"))["refused"])
        self.assertTrue(os.path.isfile(self.round_file("health.txt")))
        self.assertFalse(os.path.exists(self.round_file("metrics.json")))
        c = run(["classify", "--loop", self.loop])
        self.assertNotEqual(c.code, 0)
        self.assertFalse(os.path.exists(self.round_file("trials.csv")))

    def test_a_run_of_another_agent(self) -> None:
        run_dir = healthy(self.jobs, "run", "beta")
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("'alpha'", r.err)
        self.assertIn("'beta'", r.err)

    def test_an_adapter_without_a_key(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        data = adapter("alpha", NO_LIMITS, {})
        del data["limits"]
        self.set_adapter(data)
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("limits", r.err)

    def test_an_adapter_of_another_schema(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        data = adapter("alpha", NO_LIMITS, {})
        data["schema"] = "run-to-fix.adapter/v0"
        self.set_adapter(data)
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("run-to-fix.adapter/v1", r.err)

    def test_a_command_without_a_placeholder_it_needs(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {"classes": "make-classes {job_dir}"}))
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("classes", r.err)
        self.assertIn("{out_json}", r.err)

    def test_a_placeholder_the_command_does_not_take(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {"classes": "make-classes {job_dir} {out_json} {worktree}"}))
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("{worktree}", r.err)

    def test_a_test_command_without_its_pattern(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {"test": "pytest {worktree}"}))
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("test_failed_pattern", r.err)

    def test_a_pattern_without_one_group(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        data = adapter("alpha", NO_LIMITS, {"test": "pytest {worktree}"})
        data["test_failed_pattern"] = "FAILED .*"
        self.set_adapter(data)
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("one group", r.err)

    def test_a_zip_is_refused(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        archive = shutil.make_archive(os.path.join(self.tmp, "run"), "zip", run_dir)
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        r = self.read(archive, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("unpack", r.err)


class ReadWithoutRunAnalysis(LoopCase):
    def test_names_the_missing_skill(self) -> None:
        lone = os.path.join(self.tmp, "skills", "run-to-fix")
        shutil.copytree(os.path.dirname(os.path.dirname(LOOP)), lone,
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        p = subprocess.run([sys.executable, os.path.join(lone, "scripts", "loop.py"), "read", "--loop", self.loop,
                            "--run", run_dir, "--budget-usd", "1", "--pass-threshold", "1"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("run-analysis", p.stderr)
        self.assertIn("runs.py", p.stderr)


class ShellBraces(LoopCase):
    """A {word} of lower-case letters is a placeholder; a shell variable in upper case reaches the shell."""

    def test_an_upper_case_shell_variable_is_left_to_the_shell(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {"classes": "make-classes ${HOME} {job_dir} {out_json}"}))
        self.read_ok(run_dir, None)

    def test_a_lower_case_shell_variable_is_refused(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {"classes": "make-classes ${home} {job_dir} {out_json}"}))
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("{home}", r.err)


class LoopFolderInARepository(LoopCase):
    """read refuses a loop folder that git would list as untracked, before it writes anything."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True, capture_output=True)
        self.run_dir = healthy(self.jobs, "run", "alpha")

    def loop_at(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)
        self.loop = path
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))

    def test_a_folder_git_does_not_ignore_is_refused(self) -> None:
        self.loop_at(os.path.join(self.repo, "loops", "agent"))
        r = self.read(self.run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn(os.path.realpath(self.repo), r.err)
        self.assertIn("outside the repository", r.err)
        self.assertIn(".gitignore", r.err)
        self.assertEqual(sorted(os.listdir(self.loop)), ["adapter.json"])

    def test_a_folder_git_ignores_is_read(self) -> None:
        self.loop_at(os.path.join(self.repo, "loops", "agent"))
        with open(os.path.join(self.repo, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("loops/\n")
        self.read_ok(self.run_dir, None)
        self.assertTrue(os.path.isfile(self.round_file("input.json")))

    def test_the_repository_root_is_refused(self) -> None:
        self.loop_at(self.repo)
        r = self.read(self.run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn(os.path.realpath(self.repo), r.err)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "round-01")))


STUB_GIT = """#!/bin/sh
if [ "$LC_ALL" = "C" ]; then
  echo "fatal: not a git repository (or any of the parent directories): .git" >&2
else
  echo "fatal: ni ceci ni aucun de ses repertoires parents n'est un depot git : .git" >&2
fi
exit 128
"""


class LoopFolderWithATranslatedGit(LoopCase):
    """The guard reads git's English text, so it runs git in the C locale."""

    def test_a_folder_outside_a_repository_passes_under_a_french_locale(self) -> None:
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        stub = os.path.join(bin_dir, "git")
        with open(stub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(STUB_GIT)
        os.chmod(stub, 0o755)
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", LC_ALL="fr_FR.UTF-8", LANG="fr_FR.UTF-8",
                   PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""))
        p = subprocess.run([sys.executable, LOOP, "read", "--loop", self.loop, "--run", run_dir, "--budget-usd", "1",
                            "--pass-threshold", "1"], capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(os.path.isfile(self.round_file("input.json")))


class ReplayPlaceholders(LoopCase):
    def test_the_replay_may_take_the_base_and_the_ref_before(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        data = adapter("alpha", NO_LIMITS, {"replay": "r {worktree} {ref} {job_dir} {out_json} {base_ref} {before_ref}"})
        data["replay_success_labels"] = ["done"]
        self.set_adapter(data)
        self.read_ok(run_dir, None)

    def test_another_command_may_not_take_them(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {"classes": "c {job_dir} {out_json} {base_ref}"}))
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("{base_ref}", r.err)


class BudgetHasNoDefault(LoopCase):
    def test_read_needs_the_budget(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        r = run(["read", "--loop", self.loop, "--run", run_dir, "--pass-threshold", "1"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("--budget-usd", r.err)
        self.assertFalse(os.path.exists(os.path.join(self.loop, "round-01")))


class BudgetIsAnAmount(LoopCase):
    def test_read_refuses_a_budget_that_is_not_an_amount(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(adapter("alpha", NO_LIMITS, {}))
        for value in ("nan", "inf", "-1"):
            r = run(["read", "--loop", self.loop, "--run", run_dir, "--budget-usd", value, "--pass-threshold", "1"])
            self.assertNotEqual(r.code, 0, value)
            self.assertIn("--budget-usd", r.err)
        self.assertFalse(os.path.exists(os.path.join(self.loop, "round-01")))


class NoSource(LoopCase):
    """An agent with no checkout of its own: stages 1, 2 and 5 run, the stages that touch its code refuse."""

    def adapter_without_source(self, commands: dict) -> dict:
        data = adapter("alpha", NO_LIMITS, commands)
        data["source"] = None
        return data

    def test_read_classify_and_compare_run_without_a_checkout(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        base_dir = healthy(self.jobs, "base", "alpha")
        self.set_adapter(self.adapter_without_source({}))
        self.read_ok(run_dir, base_dir)
        self.classify_ok()
        r = run(["compare", "--loop", self.loop, "--new", run_dir, "--against", "baseline"])
        self.assertEqual(r.code, 0, r.out + r.err)

    def test_a_candidate_needs_the_checkout(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(self.adapter_without_source({}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        write(self.round_file("causes/c1.md"), "# A cause\n\nRule: g01\n")
        r = run(["candidate", "--loop", self.loop, "--id", "c1", "--cause", "c1", "--ref", "HEAD", "--effect", "outcome"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("source", r.err)

    def test_stop_two_names_the_missing_checkout_before_it_reads_the_trial_job(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        self.set_adapter(self.adapter_without_source({}))
        self.read_ok(run_dir, None)
        self.classify_ok()
        r = run(["decide", "--loop", self.loop, "--stop", "2", "--trial-job", os.path.join(self.jobs, "no-such-job"),
                 "--code-ref", "HEAD"])
        self.assertNotEqual(r.code, 0)
        self.assertIn("decide --stop 2 works on the agent's code, and adapter.json has no source", r.err)

    def test_a_command_on_the_code_needs_the_checkout(self) -> None:
        run_dir = healthy(self.jobs, "run", "alpha")
        data = self.adapter_without_source({"test": "pytest {worktree}"})
        data["test_failed_pattern"] = r"^FAILED (\S+)"
        self.set_adapter(data)
        r = self.read(run_dir, None)
        self.assertNotEqual(r.code, 0)
        self.assertIn("commands.test", r.err)
        self.assertIn("source", r.err)
