"""The loop folder and its rounds, the bridge to run-analysis, and the runner of adapter commands."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
RUNS_PY = os.path.join(os.path.dirname(os.path.dirname(SCRIPTS)), "run-analysis", "scripts", "runs.py")
ROUND = re.compile(r"^round-(\d{2,})$")
TAIL_LINES = 12


class LoopError(Exception):
    """The loop cannot go on; the message says why and what to do."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_json(path: str, data: object) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=1, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path: str) -> object:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def runs_py() -> str:
    if not os.path.isfile(RUNS_PY):
        raise LoopError(f"run-to-fix needs the run-analysis skill beside it, and {RUNS_PY} is missing. "
                        "Copy the whole run-analysis skill folder next to the run-to-fix folder.")
    return RUNS_PY


def call_runs(args: List[str]) -> str:
    """Run a command of run-analysis's runs.py and return its standard output."""
    p = subprocess.run([sys.executable, runs_py()] + args, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if p.returncode != 0:
        raise LoopError(f"runs.py {args[0]} failed with exit code {p.returncode}: {(p.stderr or p.stdout).strip()}")
    return p.stdout


def rounds(loop_dir: str) -> List[str]:
    found = []
    for name in os.listdir(loop_dir):
        m = ROUND.match(name)
        if m and os.path.isdir(os.path.join(loop_dir, name)):
            found.append((int(m.group(1)), os.path.join(loop_dir, name)))
    return [p for _, p in sorted(found)]


def round_for_read(loop_dir: str) -> str:
    """The round `read` writes: the open round while it holds stage 1 only, else a new one after a finished round."""
    existing = rounds(loop_dir)
    if existing:
        last = existing[-1]
        if not os.path.exists(os.path.join(last, "result.json")):
            if os.path.exists(os.path.join(last, "trials.csv")):
                raise LoopError(f"{os.path.basename(last)} is open past stage 1. Finish it with loop.py compare, "
                                "or start a new loop folder.")
            return last
    path = os.path.join(loop_dir, f"round-{len(existing) + 1:02d}")
    os.makedirs(path)
    return path


def open_round(loop_dir: str) -> str:
    existing = rounds(loop_dir)
    if not existing or os.path.exists(os.path.join(existing[-1], "result.json")):
        raise LoopError(f"{loop_dir} has no open round; start one with loop.py read")
    return existing[-1]


def read_input(round_dir: str) -> dict:
    path = os.path.join(round_dir, "input.json")
    if not os.path.isfile(path):
        raise LoopError(f"{path} is missing; run loop.py read first")
    data = read_json(path)
    if not isinstance(data, dict):
        raise LoopError(f"{path} is not an object")
    if data.get("refused"):
        raise LoopError(f"{os.path.basename(round_dir)}: read refused this run ({data.get('reason')}); "
                        "start a new loop folder for another run")
    return data


def fill(line: str, values: Dict[str, str]) -> str:
    """The command line with each placeholder replaced by its value quoted for the shell."""
    def one(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key not in values:
            raise LoopError(f"no value for {{{key}}} in {line!r}")
        return shlex.quote(values[key])
    return re.sub(r"\{([a-z_]+)\}", one, line)


@dataclass(frozen=True)
class Ran:
    code: Optional[int]
    tail: str
    log: str


def run_command(line: str, values: Dict[str, str], cwd: str, log_path: str, timeout_s: float) -> Ran:
    """Run one adapter command with bash; its output goes to log_path. A command past timeout_s is stopped
    with its whole process group, and `code` is None."""
    filled = fill(line, values)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"$ {filled}\n")
        log.flush()
        proc = subprocess.Popen(["bash", "-c", filled], cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            code: Optional[int] = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            code = None
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    return Ran(code=code, tail="\n".join(lines[-TAIL_LINES:]), log=log_path)


def write_paid(round_dir: str, name: str, line: str, values: Dict[str, str], cwd: str) -> str:
    """Write a paid command, filled, to round-NN/commands/<name>.sh for a person to start."""
    path = os.path.join(round_dir, "commands", f"{name}.sh")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#!/usr/bin/env bash\nset -euo pipefail\n")
        fh.write(f"cd {shlex.quote(cwd)}\n{fill(line, values)}\n")
    return path


LEDGER_SCHEMA = "run-to-fix.ledger/v1"


def load_ledger(loop_dir: str) -> dict:
    path = os.path.join(loop_dir, "ledger.json")
    if not os.path.isfile(path):
        return {"schema": LEDGER_SCHEMA, "rounds": []}
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema") != LEDGER_SCHEMA or not isinstance(data.get("rounds"), list):
        raise LoopError(f"{path} is not a ledger of schema {LEDGER_SCHEMA}")
    return data


def save_ledger(loop_dir: str, ledger: dict) -> None:
    write_json(os.path.join(loop_dir, "ledger.json"), ledger)


def ledger_round(ledger: dict, name: str) -> dict:
    """The ledger entry of round `name`, added when it is not there yet."""
    for entry in ledger["rounds"]:
        if entry["round"] == name:
            return entry
    entry = {"round": name, "started": now(), "answers": [], "selection": None, "predictions": [], "trial_runs": [],
             "result": None}
    ledger["rounds"].append(entry)
    return entry


def last_result(loop_dir: str) -> Optional[Tuple[str, dict]]:
    """The name and result.json of the last finished round, when the last round is finished."""
    existing = rounds(loop_dir)
    if not existing or not os.path.isfile(os.path.join(existing[-1], "result.json")):
        return None
    data = read_json(os.path.join(existing[-1], "result.json"))
    if not isinstance(data, dict):
        raise LoopError(f"{existing[-1]}/result.json is not an object")
    return os.path.basename(existing[-1]), data
