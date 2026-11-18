"""Stage 1, `loop.py read`: health and metrics of the run and its baseline, through run-analysis."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from adapter import Adapter, load_adapter
from rounds import (LoopError, call_runs, last_result, ledger_round, load_ledger, now, round_for_read, runs_py,
                    save_ledger, sha256_of, write_json)

#: verdicts of `runs.py health` that make a run not analysable, matched by their opening words
REFUSING = ("no trials ran", "every trial raised an exception", "systemic failure", "incomplete:",
            "the job never wrote a finish time")


def adapter_of(loop_dir: str) -> Adapter:
    return load_adapter(os.path.join(loop_dir, "adapter.json"))


def split_spec(spec: str) -> Tuple[str, Optional[str]]:
    """PATH or PATH::JOB, with PATH made absolute; an archive is refused."""
    path, _, job = spec.partition("::")
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(path):
        raise LoopError(f"{path} is a file; unpack the archive first and pass its folder")
    if not os.path.isdir(path):
        raise LoopError(f"{path} is not a folder")
    return path, job or None


def full_spec(spec: str) -> str:
    path, job = split_spec(spec)
    return path + (f"::{job}" if job else "")


@dataclass(frozen=True)
class Health:
    text: str
    job: str
    dir: str
    agents: List[str]
    refused: List[str]


def health(label: str, spec: str, threshold: float) -> Health:
    """The health of the one job `spec` names: the printed report, its agents, and the verdicts that refuse it."""
    path, job_name = split_spec(spec)
    text = f"# {label}: {spec}\n" + call_runs(["health", "--pass-threshold", str(threshold), path])
    report = json.loads(call_runs(["health", "--json", "--pass-threshold", str(threshold), path]))[0]
    jobs = [j for j in report["jobs"] if job_name is None or j["job"] == job_name]
    if len(jobs) > 1:
        raise LoopError(f"{path} holds several jobs: {[j['job'] for j in jobs]}. Name one with PATH::JOB_NAME.")
    job = jobs[0] if jobs else {}
    verdicts = list(report["verdicts"]) + list(job.get("verdicts") or [])
    if not jobs and not any(v.startswith("no trials ran") for v in verdicts):
        verdicts.append(f"no trials ran: no job {job_name!r} under {path}")
    refused = []
    for v in verdicts:
        if v.startswith(REFUSING):
            refused.append(f"{label}: {v}")
            refused += [f"{label}: e.g. {example}" for message, example in (job.get("error_examples") or {}).items()
                        if example and message in v]
    return Health(text=text, job=str(job.get("job") or ""), dir=str(job.get("dir") or ""), agents=list(job.get("agents") or []), refused=refused)


def analyze(runs: List[Tuple[str, str]], threshold: float, out: str) -> str:
    """`runs.py analyze` of (label, spec) pairs into `out`; the first is the base of every gap."""
    paths = [full_spec(spec) for _, spec in runs]
    labels = [x for label, _ in runs for x in ("--label", label)]
    return call_runs(["analyze"] + paths + labels + ["--pass-threshold", str(threshold), "--out", out])


def gate(loop_dir: str, words: Optional[str]) -> Optional[dict]:
    """The stop rule of the last finished round. A stop needs the person's words to go on; they are returned
    as an answer for the ledger."""
    last = last_result(loop_dir)
    if last is None:
        return None
    name, result = last
    rule = result.get("stop_rule") or {}
    if not rule.get("stop"):
        return None
    if not words:
        raise LoopError(f"the stop rule of {name} says stop: " + "; ".join(rule.get("reasons") or [])
                        + ". To start another round anyway, pass the person's words with --continue-after-stop.")
    return {"about": f"continue after the stop rule of {name}", "date": now(), "text": words}


def cmd_read(args: argparse.Namespace) -> None:
    adapter = adapter_of(args.loop)
    runs = runs_py()
    specs = [("run", args.run)] + ([("baseline", args.baseline)] if args.baseline else [])
    for _, spec in specs:
        split_spec(spec)
    answer = gate(args.loop, args.continue_after_stop)
    round_dir = round_for_read(args.loop)
    name = os.path.basename(round_dir)
    for stale in ("metrics.json", "input.json", "health.txt"):
        if os.path.exists(os.path.join(round_dir, stale)):
            os.remove(os.path.join(round_dir, stale))
    ledger = load_ledger(args.loop)
    entry = ledger_round(ledger, name)
    if answer is not None:
        entry["answers"].append(answer)
    save_ledger(args.loop, ledger)

    checks = {label: health(label, spec, args.pass_threshold) for label, spec in specs}
    with open(os.path.join(round_dir, "health.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(h.text for h in checks.values()))
    refused = [line for h in checks.values() for line in h.refused]
    record = {
        "schema": "run-to-fix.input/v1", "created": now(), "round": name,
        "run": {"spec": args.run, "job_dir": None},
        "baseline": {"spec": args.baseline, "job_dir": None} if args.baseline else None,
        "adapter_sha256": adapter.sha256, "budget_usd": args.budget_usd, "pass_threshold": args.pass_threshold,
        "run_analysis": {"runs_py": runs, "sha256": sha256_of(runs)}, "refused": bool(refused),
        "reason": "; ".join(refused) or None,
    }
    if refused:
        write_json(os.path.join(round_dir, "input.json"), record)
        raise LoopError("the run is not analysable, so the loop stops here. health.txt has the report.\n  "
                        + "\n  ".join(refused))
    if checks["run"].agents != [adapter.agent]:
        raise LoopError(f"adapter.json is for the agent {adapter.agent!r}, and the trials of the run name "
                        + ", ".join(repr(a) for a in checks["run"].agents))

    metrics = os.path.join(round_dir, "metrics.json")
    order = ([("baseline", args.baseline)] if args.baseline else []) + [("run", args.run)]
    print(analyze(order, args.pass_threshold, metrics))
    with open(metrics, encoding="utf-8") as fh:
        by_label = {r["label"]: r for r in json.load(fh)["runs"]}
    record["run"]["job_dir"] = by_label["run"]["dir"]
    if args.baseline:
        record["baseline"]["job_dir"] = by_label["baseline"]["dir"]
    write_json(os.path.join(round_dir, "input.json"), record)
    print(f"wrote {round_dir}: input.json, health.txt, metrics.json")
