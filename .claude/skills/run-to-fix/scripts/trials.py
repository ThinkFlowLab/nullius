"""One trial read from the files both run paths write, and its class and signature (`references/classes.md`)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from adapter import Limits

CLASSES = ("pass", "time_limit", "exception", "no_score", "step_limit", "output_limit", "verifier_failed")
TIMEOUT_TYPE = "AgentTimeoutError"
NUMBER = re.compile(r"\b[0-9a-f]{8,}\b|\d+(?:\.\d+)?")
FAILING_LINE = re.compile(r"\b(FAIL(?:ED|URE)?|ERROR|Error|AssertionError|assert)\b")
SCORE_TEXT_KEYS = ("reason", "error")
DETAIL_CHARS = 200
EXAMPLE_CHARS = 500
#: an opening quote: a quote mark that does not follow a letter or digit, so the apostrophe of "agent's" opens nothing
QUOTE_OPEN = re.compile(r"(?<!\w)(['\"])")
SECRETS = re.compile(r"sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}")


class TrialError(Exception):
    """A trial file cannot be read; the message names it."""


@dataclass(frozen=True)
class TrialRow:
    trial: str
    task: str
    dir: str
    reward: Optional[float]
    passed: bool
    exception_type: Optional[str]
    exception_message: Optional[str]
    agent_s: Optional[float]
    model_calls: Optional[int]
    max_completion_tokens: Optional[int]
    cost_usd: Optional[float]
    agent: Optional[str]
    verifier_ran: bool


def read_json(path: str) -> object:
    """A JSON file; a file that is not JSON raises with its path."""
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            raise TrialError(f"{path} is not JSON: {exc}") from exc


def _time(value: object) -> Optional[dt.datetime]:
    if not value:
        return None
    t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t


def _span(block: object) -> Optional[float]:
    if not isinstance(block, dict):
        return None
    a, b = _time(block.get("started_at")), _time(block.get("finished_at"))
    return (b - a).total_seconds() if a and b else None


def primary_reward(result: dict) -> Optional[float]:
    """`rewards.reward`, else the first numeric reward: the rule of run-analysis."""
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    if not isinstance(rewards, dict):
        return None
    numeric = {k: v for k, v in rewards.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if "reward" in numeric:
        return float(numeric["reward"])
    return float(next(iter(numeric.values()))) if numeric else None


def _usage(trial_dir: str) -> Tuple[Optional[int], Optional[int]]:
    """Model calls and the largest completion of one reply, from the ATIF trajectory.

    Model calls follow run-analysis: per-call usage records (`extra.raw_usage` or steps with metrics, whichever is
    more), else the agent's steps."""
    path = os.path.join(trial_dir, "agent", "trajectory.json")
    if not os.path.isfile(path):
        return None, None
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        return None, None
    agent_steps, with_metrics, outputs = 0, 0, []
    for step in data["steps"]:
        if not isinstance(step, dict) or step.get("source") != "agent":
            continue
        agent_steps += 1
        metrics = step.get("metrics")
        if isinstance(metrics, dict) and metrics.get("prompt_tokens") is not None:
            with_metrics += 1
        if isinstance(metrics, dict) and isinstance(metrics.get("completion_tokens"), int):
            outputs.append(metrics["completion_tokens"])
    raw = (data.get("extra") or {}).get("raw_usage") if isinstance(data.get("extra"), dict) else None
    raw = [u for u in raw if isinstance(u, dict)] if isinstance(raw, list) else []
    outputs += [u["output_tokens"] for u in raw if isinstance(u.get("output_tokens"), int)]
    calls = max(len(raw), with_metrics) or agent_steps or None
    return calls, (max(outputs) if outputs else None)


def read_trial(trial_dir: str, pass_threshold: float) -> TrialRow:
    result = read_json(os.path.join(trial_dir, "result.json"))
    if not isinstance(result, dict) or "task_name" not in result:
        raise TrialError(f"{trial_dir}/result.json has no task_name")
    reward = primary_reward(result)
    exc = result.get("exception_info") or {}
    calls, max_out = _usage(trial_dir)
    info = result.get("agent_info") or {}
    cost = (result.get("agent_result") or {}).get("cost_usd")
    return TrialRow(
        trial=str(result.get("trial_name") or os.path.basename(trial_dir)), task=str(result["task_name"]),
        dir=trial_dir, reward=reward, passed=reward is not None and reward >= pass_threshold,
        exception_type=str(exc.get("exception_type") or "Exception") if exc else None,
        exception_message=str(exc.get("exception_message") or "") if exc else None,
        agent_s=_span(result.get("agent_execution")), model_calls=calls, max_completion_tokens=max_out,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None, agent=info.get("name"),
        verifier_ran=reward is not None)


def classify(row: TrialRow, limits: Limits) -> str:
    """The first class of `CLASSES` whose rule holds; a null limit switches its rule off."""
    if row.passed:
        return "pass"
    if row.exception_type == TIMEOUT_TYPE or (
            limits.time_limit_s is not None and row.agent_s is not None and row.agent_s >= limits.time_limit_s):
        return "time_limit"
    if row.exception_type is not None:
        return "exception"
    if row.reward is None:
        return "no_score"
    if limits.step_limit is not None and row.model_calls is not None and row.model_calls >= limits.step_limit:
        return "step_limit"
    if (limits.output_token_limit is not None and row.max_completion_tokens is not None
            and row.max_completion_tokens >= limits.output_token_limit):
        return "output_limit"
    return "verifier_failed"


def rules_off(limits: Limits) -> List[str]:
    off = []
    if limits.time_limit_s is None:
        off.append("time_limit by the agent phase")
    if limits.step_limit is None:
        off.append("step_limit")
    if limits.output_token_limit is None:
        off.append("output_limit")
    return off


def mask_quotes(text: str) -> str:
    """Each quoted string as a quote, # and the closing quote; a quote that is not closed masks to the end."""
    out, i = [], 0
    while True:
        m = QUOTE_OPEN.search(text, i)
        if m is None:
            out.append(text[i:])
            return "".join(out)
        q = m.group(1)
        out.append(text[i:m.start()] + q + "#")
        close = re.compile(re.escape(q) + r"(?!\w)").search(text, m.end())
        if close is None:
            return "".join(out)
        out.append(q)
        i = close.end()


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def mask(text: str, quotes: bool) -> str:
    """The detail of a signature: quoted strings when `quotes`, then numbers and long ids, masked; then cut."""
    return _collapse(NUMBER.sub("#", mask_quotes(text) if quotes else text))[:DETAIL_CHARS]


def _last_line(message: str) -> str:
    lines = [ln.strip() for ln in message.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _exception_detail(row: TrialRow) -> str:
    line = _last_line(row.exception_message or "")
    prefix = f"{row.exception_type}: "
    if line.startswith(prefix):
        line = line[len(prefix):]
    return f"{row.exception_type}: {line}" if line else str(row.exception_type)


def _ctrf_failure(path: str) -> Optional[str]:
    data = read_json(path)
    tests = ((data or {}).get("results") or {}).get("tests") if isinstance(data, dict) else None
    for test in tests if isinstance(tests, list) else []:
        if isinstance(test, dict) and test.get("status") in ("failed", "error"):
            return str(test.get("name") or "")
    return None


def _verifier_detail(trial_dir: str) -> str:
    """The first failing line of the verifier's text: ctrf.json, then test-stdout.txt, then score.json."""
    verifier = os.path.join(trial_dir, "verifier")
    ctrf = os.path.join(verifier, "ctrf.json")
    if os.path.isfile(ctrf):
        name = _ctrf_failure(ctrf)
        if name:
            return name
    stdout = os.path.join(verifier, "test-stdout.txt")
    if os.path.isfile(stdout):
        with open(stdout, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if FAILING_LINE.search(line):
                    return line
    score = os.path.join(verifier, "score.json")
    if os.path.isfile(score):
        data = read_json(score)
        for key in SCORE_TEXT_KEYS:
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                return _last_line(value)
    return ""


@dataclass(frozen=True)
class Signature:
    text: str
    example: str


def signature(row: TrialRow, cls: str) -> Signature:
    """The class and its masked detail; `example` is the detail as written, with secrets masked."""
    raw = ""
    if row.exception_type is not None and cls in ("time_limit", "exception"):
        raw = _exception_detail(row)
    elif cls == "verifier_failed":
        raw = _verifier_detail(row.dir)
    detail = mask(raw, cls == "exception")
    return Signature(text=f"{cls}: {detail}" if detail else cls,
                     example=_collapse(SECRETS.sub("[redacted]", raw))[:EXAMPLE_CHARS])


def trial_dirs(job_dir: str) -> List[str]:
    """The trial folders of a job: each child holding a result.json with a task_name."""
    out = []
    for name in sorted(os.listdir(job_dir)):
        path = os.path.join(job_dir, name, "result.json")
        if os.path.isfile(path):
            data = read_json(path)
            if isinstance(data, dict) and "task_name" in data:
                out.append(os.path.join(job_dir, name))
    return out


def solved_tasks(rows: List[TrialRow]) -> Tuple[Dict[str, bool], Dict[str, List[TrialRow]]]:
    """A task is solved when at least half of its trials pass: the rule of run-analysis."""
    by_task: Dict[str, List[TrialRow]] = {}
    for r in rows:
        by_task.setdefault(r.task, []).append(r)
    return {t: sum(r.passed for r in v) / len(v) >= 0.5 for t, v in by_task.items()}, by_task
