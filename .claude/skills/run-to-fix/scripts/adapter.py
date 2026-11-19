"""The adapter file, `run-to-fix.adapter/v1`, read and checked against `references/adapter.md`."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional

SCHEMA = "run-to-fix.adapter/v1"
COMMANDS = ("test", "test_one", "build", "run_tasks", "replay", "classes")
#: the commands that run on the agent's code, so need source.checkout
CODE_COMMANDS = ("test", "test_one", "build", "run_tasks", "replay")
TAKES: Dict[str, FrozenSet[str]] = {
    "test": frozenset({"worktree"}),
    "test_one": frozenset({"worktree", "test"}),
    "build": frozenset({"worktree"}),
    "replay": frozenset({"worktree", "ref", "job_dir", "out_json", "base_ref", "before_ref"}),
    "classes": frozenset({"job_dir", "out_json"}),
    "run_tasks": frozenset({"units_file", "repeat", "jobs_dir", "budget_usd", "build", "ref"}),
}
NEEDS: Dict[str, FrozenSet[str]] = {
    "test": frozenset({"worktree"}),
    "test_one": frozenset({"worktree", "test"}),
    "build": frozenset({"worktree"}),
    "replay": frozenset({"worktree", "ref", "job_dir", "out_json"}),
    "classes": frozenset({"job_dir", "out_json"}),
    "run_tasks": frozenset({"units_file", "repeat", "jobs_dir"}),
}
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class AdapterError(Exception):
    """The adapter does not satisfy the contract; the message names the key."""


@dataclass(frozen=True)
class Limits:
    step_limit: Optional[int]
    output_token_limit: Optional[int]
    time_limit_s: Optional[float]


@dataclass(frozen=True)
class Adapter:
    agent: str
    checkout: Optional[str]
    base_ref: Optional[str]
    work_branch: Optional[str]
    commands: Dict[str, Optional[str]]
    test_failed_pattern: Optional["re.Pattern[str]"]
    replay_success_labels: List[str]
    limits: Limits
    paid: List[str]
    sha256: str


def _key(data: dict, key: str, where: str) -> object:
    if not isinstance(data, dict) or key not in data:
        raise AdapterError(f"adapter.json has no key {where}{key}; every key of references/adapter.md is present, "
                           "null when the agent has no value")
    return data[key]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"adapter.json: {name} must be a nonempty string, got {value!r}")
    return value


def _limit(value: object, name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise AdapterError(f"adapter.json: limits.{name} must be a positive number or null, got {value!r}")
    return value


def _command(name: str, value: object) -> Optional[str]:
    if value is None:
        return None
    line = _text(value, f"commands.{name}")
    used = set(PLACEHOLDER.findall(line))
    extra = sorted(used - TAKES[name])
    if extra:
        raise AdapterError(f"adapter.json: commands.{name} uses " + ", ".join("{" + p + "}" for p in extra)
                           + ", which it does not take; it takes " + ", ".join("{" + p + "}" for p in sorted(TAKES[name])))
    missing = sorted(NEEDS[name] - used)
    if missing:
        raise AdapterError(f"adapter.json: commands.{name} needs " + ", ".join("{" + p + "}" for p in missing))
    if name == "run_tasks" and not used & {"build", "ref"}:
        raise AdapterError("adapter.json: commands.run_tasks needs {build} or {ref}, so the run installs the code under check")
    return line


def source_of(adapter: Adapter, step: str) -> str:
    """The agent's checkout, for a step that works on its code."""
    if adapter.checkout is None or adapter.base_ref is None:
        raise AdapterError(f"loop.py {step} works on the agent's code, and adapter.json has no source; read, "
                           "classify and compare run without it")
    return adapter.checkout


def load_adapter(path: str) -> Adapter:
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"adapter.json is not JSON: {exc}") from exc
    if _key(data, "schema", "") != SCHEMA:
        raise AdapterError(f"adapter.json: schema must be {SCHEMA}, got {data.get('schema')!r}")
    agent = _text(_key(data, "agent", ""), "agent")
    source = _key(data, "source", "")
    if source is None:
        checkout = base_ref = work_branch = None
    else:
        checkout = _text(_key(source, "checkout", "source."), "source.checkout")
        base_ref = _text(_key(source, "base_ref", "source."), "source.base_ref")
        work_branch = _text(_key(source, "work_branch", "source."), "source.work_branch")
    raw_commands = _key(data, "commands", "")
    commands = {name: _command(name, _key(raw_commands, name, "commands.")) for name in COMMANDS}
    if source is None:
        on_code = [name for name in CODE_COMMANDS if commands[name] is not None]
        if on_code:
            raise AdapterError("adapter.json: " + ", ".join(f"commands.{n}" for n in on_code) + " works on the agent's "
                               "code, and source is null; give the checkout in source or make the command null")
    unknown = sorted(set(raw_commands) - set(COMMANDS))
    if unknown:
        raise AdapterError(f"adapter.json: unknown commands {unknown}; the commands are {list(COMMANDS)}")

    pattern_text = _key(data, "test_failed_pattern", "")
    pattern = None
    if pattern_text is not None:
        try:
            pattern = re.compile(_text(pattern_text, "test_failed_pattern"))
        except re.error as exc:
            raise AdapterError(f"adapter.json: test_failed_pattern is not a regular expression: {exc}") from exc
        if pattern.groups != 1:
            raise AdapterError(f"adapter.json: test_failed_pattern must have exactly one group, it has {pattern.groups}")
    if commands["test"] is not None and pattern is None:
        raise AdapterError("adapter.json: commands.test is set, so test_failed_pattern is required")

    labels = _key(data, "replay_success_labels", "")
    if not isinstance(labels, list) or not all(isinstance(x, str) and x for x in labels):
        raise AdapterError(f"adapter.json: replay_success_labels must be a list of strings, got {labels!r}")
    if commands["replay"] is not None and not labels:
        raise AdapterError("adapter.json: commands.replay is set, so replay_success_labels needs at least one label")

    raw_limits = _key(data, "limits", "")
    step = _limit(_key(raw_limits, "step_limit", "limits."), "step_limit")
    output = _limit(_key(raw_limits, "output_token_limit", "limits."), "output_token_limit")
    time_s = _limit(_key(raw_limits, "time_limit_s", "limits."), "time_limit_s")
    paid = _key(data, "paid", "")
    if not isinstance(paid, list) or not all(isinstance(x, str) for x in paid):
        raise AdapterError(f"adapter.json: paid must be a list of command names, got {paid!r}")
    strange = sorted(set(paid) - set(COMMANDS))
    if strange:
        raise AdapterError(f"adapter.json: paid names {strange}, which are not commands")
    return Adapter(
        agent=agent, checkout=checkout, base_ref=base_ref, work_branch=work_branch, commands=commands,
        test_failed_pattern=pattern, replay_success_labels=list(labels),
        limits=Limits(step_limit=None if step is None else int(step),
                      output_token_limit=None if output is None else int(output), time_limit_s=time_s),
        paid=list(paid), sha256=hashlib.sha256(raw).hexdigest())
