"""Hand-made job folders in the two shapes Nullius writes, and the helpers the tests share."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
LOOP = os.path.join(SKILL, "scripts", "loop.py")
T0 = dt.datetime(2026, 11, 1, 12, 0, 0)


@dataclass
class Trial:
    name: str
    task: str
    reward: Optional[float]
    exception: Optional[Tuple[str, str]]
    agent_s: float
    calls: int
    max_output: int
    verifier_text: str
    files: Dict[str, str]


def trial(name: str, task: str, reward: Optional[float]) -> Trial:
    """A trial of 5 model calls, 60 s, 100 tokens per reply, no exception, no extra files."""
    return Trial(name=name, task=task, reward=reward, exception=None, agent_s=60.0, calls=5, max_output=100,
                 verifier_text="", files={})


def iso(t: dt.datetime) -> str:
    return t.isoformat()


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_json(path: str, data: object) -> None:
    write(path, json.dumps(data, indent=1))


def trajectory(t: Trial) -> dict:
    steps: List[dict] = [{"step_id": 0, "source": "user", "timestamp": iso(T0), "message": "task"}]
    for i in range(t.calls):
        out = t.max_output if i == t.calls - 1 else min(100, t.max_output)
        steps.append({"step_id": i + 1, "source": "agent", "timestamp": iso(T0 + dt.timedelta(seconds=i + 1)),
                      "message": "step", "metrics": {"prompt_tokens": 1000, "completion_tokens": out}})
    return {"schema_version": "ATIF-v1.2", "agent": {"name": "fixture"}, "steps": steps}


def write_trial(job_dir: str, t: Trial, agent: str, model: str, runner: str, index: int) -> None:
    d = os.path.join(job_dir, t.name)
    start = T0 + dt.timedelta(minutes=index)
    agent_start = start + dt.timedelta(seconds=10)
    agent_end = agent_start + dt.timedelta(seconds=t.agent_s)
    end = agent_end + dt.timedelta(seconds=5)
    res: dict = {
        "task_name": t.task, "trial_name": t.name, "started_at": iso(start), "finished_at": iso(end),
        "agent_info": {"name": agent, "version": "1.0", "model_info": {"name": model}},
        "environment_setup": {"started_at": iso(start), "finished_at": iso(agent_start)},
        "agent_execution": {"started_at": iso(agent_start), "finished_at": iso(agent_end)},
        "agent_result": {"n_input_tokens": 1000 * t.calls, "n_output_tokens": 100 * t.calls, "n_cache_tokens": 0,
                         "cost_usd": 0.01},
    }
    if t.reward is not None:
        res["verifier"] = {"started_at": iso(agent_end), "finished_at": iso(end)}
        res["verifier_result"] = {"rewards": {"reward": t.reward}}
    if t.exception is not None:
        res["exception_info"] = {"exception_type": t.exception[0], "exception_message": t.exception[1]}
        write(os.path.join(d, "exception.txt"), t.exception[1] + "\n")
    if runner == "standard":
        res["agent_result"]["n_model_calls"] = t.calls
        res["standard"] = {"benchmark": "made-up-bench", "unit": t.task.split("/")[-1], "plugged": f"{agent}@1.0",
                           "job": os.path.basename(job_dir)}
        if t.reward is not None:
            write_json(os.path.join(d, "verifier", "score.json"),
                       {"reward": t.reward, "reason": t.verifier_text, "says": "pass"})
    elif t.reward is not None:
        write(os.path.join(d, "verifier", "reward.txt"), f"{t.reward}\n")
        write(os.path.join(d, "verifier", "test-stdout.txt"), t.verifier_text + "\n")
    write_json(os.path.join(d, "agent", "trajectory.json"), trajectory(t))
    for rel, text in t.files.items():
        write(os.path.join(d, rel), text)
    write_json(os.path.join(d, "result.json"), res)


def write_job(root: str, name: str, agent: str, runner: str, trials: List[Trial]) -> str:
    """A finished job of `trials` under root/name; runner is "harbor" or "standard"."""
    job_dir = os.path.join(root, name)
    os.makedirs(job_dir, exist_ok=True)
    config: dict = {"job_name": name, "n_concurrent_trials": 2,
                    "agents": [{"name": agent, "model_name": "made-up-model"}]}
    if runner == "standard":
        config.update({"runner": "standard", "runner_label": "Nullius",
                       "standard": {"benchmark": "made-up-bench", "plugged": f"{agent}@1.0", "kind": "agent"}})
    write_json(os.path.join(job_dir, "config.json"), config)
    for i, t in enumerate(trials):
        write_trial(job_dir, t, agent, "made-up-model", runner, i)
    write_json(os.path.join(job_dir, "result.json"), {
        "started_at": iso(T0), "finished_at": iso(T0 + dt.timedelta(hours=1)), "n_total_trials": len(trials),
        "stats": {"n_completed_trials": len(trials), "n_cancelled_trials": 0}})
    return job_dir


def adapter(agent: str, limits: Dict[str, Optional[float]], commands: Dict[str, Optional[str]]) -> dict:
    """An adapter whose checkout is never used: every command not named in `commands` is null."""
    names = ("test", "test_one", "build", "run_tasks", "replay", "classes")
    return {
        "schema": "run-to-fix.adapter/v1",
        "agent": agent,
        "source": {"checkout": HERE, "base_ref": "HEAD", "work_branch": "fix/made-up"},
        "commands": {n: commands.get(n) for n in names},
        "test_failed_pattern": None,
        "replay_success_labels": [],
        "limits": {"step_limit": limits.get("step_limit"), "output_token_limit": limits.get("output_token_limit"),
                   "time_limit_s": limits.get("time_limit_s")},
        "paid": [],
    }


NO_LIMITS: Dict[str, Optional[float]] = {}
LIMITS: Dict[str, Optional[float]] = {"step_limit": 20, "output_token_limit": 4096, "time_limit_s": 600}


@dataclass
class Result:
    code: int
    out: str
    err: str


def run(args: List[str]) -> Result:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, LOOP] + args, capture_output=True, text=True, env=env,
                       stdin=subprocess.DEVNULL, timeout=120)
    return Result(p.returncode, p.stdout, p.stderr)


def load(path: str) -> object:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class LoopCase(unittest.TestCase):
    """A temporary folder with `jobs/` and `loop/`, removed after each test."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="run-to-fix-test-")
        self.jobs = os.path.join(self.tmp, "jobs")
        self.loop = os.path.join(self.tmp, "loop")
        os.makedirs(self.jobs)
        os.makedirs(self.loop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def set_adapter(self, data: dict) -> None:
        write_json(os.path.join(self.loop, "adapter.json"), data)

    def read(self, run_dir: str, baseline: Optional[str]) -> Result:
        args = ["read", "--loop", self.loop, "--run", run_dir, "--budget-usd", "10", "--pass-threshold", "1"]
        if baseline is not None:
            args += ["--baseline", baseline]
        return run(args)

    def read_ok(self, run_dir: str, baseline: Optional[str]) -> None:
        r = self.read(run_dir, baseline)
        self.assertEqual(r.code, 0, r.out + r.err)

    def classify_ok(self) -> None:
        r = run(["classify", "--loop", self.loop])
        self.assertEqual(r.code, 0, r.out + r.err)

    def round_file(self, name: str) -> str:
        return os.path.join(self.loop, "round-01", name)

    def rows(self) -> List[Dict[str, str]]:
        with open(self.round_file("trials.csv"), encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def by_trial(self) -> Dict[str, Dict[str, str]]:
        return {r["trial"]: r for r in self.rows()}
