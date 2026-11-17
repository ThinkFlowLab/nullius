"""A made-up agent in a git repository, its stub commands, and a recorded run of it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

from jobs import LoopCase, Trial, adapter, run, trial, write, write_job

SUITE = """import agent
print("FAILED test_old - a failure the suite has on every ref")
if agent.BREAKS_OTHER:
    print("FAILED test_other - broken by the fix")
print("ok test_fix_area")
"""
TEST_FIX = """import sys
sys.path.insert(0, ".")
import agent
sys.exit(0 if agent.FIXED else 1)
"""
REPLAY = """import json, os, sys
worktree, job_dir, ref, out, marker = sys.argv[1:6]
labels = json.load(open(os.path.join(worktree, "labels.json")))
names = sorted(n for n in os.listdir(job_dir) if os.path.isfile(os.path.join(job_dir, n, "result.json")))
json.dump({"ref": ref, "trials": {n: labels[n] for n in names}, "kept_recorded": labels.get("_kept", [])}, open(out, "w"))
open(marker, "a").write(ref + "\\n")
"""
BUILD = """import hashlib, os, shutil, sys
worktree, out_dir = sys.argv[1:3]
data = open(os.path.join(worktree, "agent.py"), "rb").read()
os.makedirs(out_dir, exist_ok=True)
path = os.path.join(out_dir, "agent-" + hashlib.sha256(data).hexdigest()[:8] + ".txt")
open(path, "wb").write(data)
print("built the agent")
print(path)
"""

BASE_LABELS = {"p1__r": {"label": "done"}, "p2__r": {"label": "done"}, "p3__r": {"label": "not done"},
               "f1__r": {"label": "done"}, "f2__r": {"label": "done"}, "f3__r": {"label": "not done"}}
GOOD_LABELS = {"p1__r": {"label": "done", "touched": True}, "p2__r": {"label": "done", "touched": False},
               "p3__r": {"label": "done", "touched": False}, "f1__r": {"label": "not done", "touched": True},
               "f2__r": {"label": "not done", "touched": True}, "f3__r": {"label": "not done", "touched": False}}
BAD_LABELS = dict(GOOD_LABELS, f3__r={"label": "done", "touched": True})


def git(repo: str, args: List[str]) -> str:
    p = subprocess.run(["git", "-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid"] + args,
                       capture_output=True, text=True, check=True)
    return p.stdout.strip()


def commit(repo: str, files: Dict[str, str], message: str) -> str:
    for rel, text in files.items():
        write(os.path.join(repo, rel), text)
    git(repo, ["add", "-A"])
    git(repo, ["commit", "-q", "-m", message])
    return git(repo, ["rev-parse", "HEAD"])


def agent_file(fixed: bool, breaks_other: bool) -> str:
    return f"FIXED = {fixed}\nBREAKS_OTHER = {breaks_other}\n"


@dataclass(frozen=True)
class Repo:
    path: str
    base: str
    test_ref: str
    good: str
    bad: str


def make_repo(root: str) -> Repo:
    path = os.path.join(root, "agent")
    os.makedirs(path)
    git(path, ["init", "-q", "-b", "main"])
    base = commit(path, {"agent.py": agent_file(False, False), "suite.py": SUITE,
                         "labels.json": json.dumps(BASE_LABELS)}, "base")
    test_ref = commit(path, {"tests/test_fix.py": TEST_FIX}, "test: the trial f1 ends in the wrong state")
    good = commit(path, {"agent.py": agent_file(True, False), "labels.json": json.dumps(GOOD_LABELS)}, "fix: good")
    git(path, ["checkout", "-q", "--detach", test_ref])
    bad = commit(path, {"agent.py": agent_file(True, True), "labels.json": json.dumps(BAD_LABELS)}, "fix: bad")
    git(path, ["checkout", "-q", "main"])
    return Repo(path=path, base=base, test_ref=test_ref, good=good, bad=bad)


def recorded_trials() -> List[Trial]:
    out = [trial("p1__r", "bench/p1", 1.0), trial("p2__r", "bench/p2", 1.0), trial("p3__r", "bench/p3", 1.0)]
    for name in ("f1", "f2"):
        t = trial(f"{name}__r", f"bench/{name}", 0.0)
        t.files = {"verifier/ctrf.json": '{"results": {"tests": [{"name": "test_state", "status": "failed"}]}}'}
        out.append(t)
    out.append(trial("f3__r", "bench/f3", 0.0))
    return out


class RepoCase(LoopCase):
    """A loop over a recorded run of the made-up agent, read and classified, with one cause written."""

    def commands(self) -> Dict[str, Optional[str]]:
        py = sys.executable
        return {
            "test": f"cd {{worktree}} && {py} suite.py",
            "test_one": f"cd {{worktree}} && {py} {{test}}",
            "build": f"{py} {self.stubs}/build.py {{worktree}} {self.tmp}/built",
            "run_tasks": f"{self.tmp}/no-such-runner --units {{units_file}} --repeat {{repeat}} --out {{jobs_dir}} "
                         f"--cap {{budget_usd}} --ref {{ref}} --build {{build}} --marker {self.tmp}/runner.marker",
            "replay": f"{py} {self.stubs}/replay.py {{worktree}} {{job_dir}} {{ref}} {{out_json}} {self.tmp}/replay.marker",
            "classes": None,
        }

    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.tmp)
        self.stubs = os.path.join(self.tmp, "stubs")
        write(os.path.join(self.stubs, "replay.py"), REPLAY)
        write(os.path.join(self.stubs, "build.py"), BUILD)
        self.run_dir = write_job(self.jobs, "recorded", "alpha", "harbor", recorded_trials())

    def start(self, commands: Dict[str, Optional[str]], paid: List[str]) -> None:
        data = adapter("alpha", {}, commands)
        data["source"] = {"checkout": self.repo.path, "base_ref": self.repo.base, "work_branch": "main"}
        data["test_failed_pattern"] = r"^FAILED (\S+)"
        data["replay_success_labels"] = ["done"]
        data["paid"] = paid
        self.set_adapter(data)
        self.read_ok(self.run_dir, None)
        self.classify_ok()
        write(os.path.join(self.loop, "round-01", "causes", "state.md"),
              "# The agent reports done in the wrong state\n\nRule: g01\n\nTrials: 2 (f1__r, f2__r).\n")

    def candidate(self, cid: str, ref: str, test_ref: str) -> None:
        r = run(["candidate", "--loop", self.loop, "--id", cid, "--cause", "state", "--ref", ref,
                 "--test", "tests/test_fix.py", "--test-ref", test_ref])
        self.assertEqual(r.code, 0, r.out + r.err)

    def check(self, cid: str) -> dict:
        r = run(["check", "--loop", self.loop, "--candidate", cid])
        self.assertEqual(r.code, 0, r.out + r.err)
        with open(self.round_file(os.path.join("candidates", cid, "checks.json")), encoding="utf-8") as fh:
            return json.load(fh)

    def marker(self, name: str) -> List[str]:
        path = os.path.join(self.tmp, name)
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return fh.read().split()
