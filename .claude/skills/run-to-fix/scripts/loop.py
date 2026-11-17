#!/usr/bin/env python3
"""Counting and bookkeeping of the run-to-fix loop. Standard library only, Python 3.8+.

  read      stage 1: health and metrics of the run and its baseline; refuses a run that is not analysable
  classify  stage 2: one class and signature per trial, groups and their warnings

Every file is written under the loop folder; see references/files.md.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

from adapter import Adapter, AdapterError, load_adapter
from found import UNREADABLE, Rule, RuleError, apply, load_rules
from rounds import (LoopError, call_runs, now, open_round, read_input, read_json, round_for_read, run_command,
                    runs_py, sha256_of, write_json, write_paid)
from trials import CLASSES, TrialError, TrialRow, classify, read_trial, rules_off, signature, solved_tasks, trial_dirs

#: verdicts of `runs.py health` that make a run not analysable (they are matched by their opening words)
REFUSING = ("no trials ran", "every trial raised an exception", "systemic failure", "incomplete:",
            "the job never wrote a finish time")
FREQUENT_MIN_TRIALS = 3
FREQUENT_MIN_SHARE = 0.05
BASE_COLUMNS = ["trial", "task", "class", "signature", "reward", "pair", "cost_usd", "model_calls", "agent_s",
                "max_completion_tokens", "exception_type"]


def adapter_of(loop_dir: str) -> Adapter:
    return load_adapter(os.path.join(loop_dir, "adapter.json"))


def split_spec(spec: str) -> Tuple[str, Optional[str]]:
    path, _, job = spec.partition("::")
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(path):
        raise LoopError(f"{path} is a file; unpack the archive first and pass its folder")
    if not os.path.isdir(path):
        raise LoopError(f"{path} is not a folder")
    return path, job or None


def health_job(spec: str, threshold: float) -> Tuple[dict, List[str]]:
    """The health report of the one job `spec` names, and the verdicts that refuse it."""
    path, job = split_spec(spec)
    reports = json.loads(call_runs(["health", "--json", "--pass-threshold", str(threshold), path]))
    report = reports[0]
    jobs = report["jobs"]
    if job is not None:
        jobs = [j for j in jobs if j["job"] == job]
    if len(jobs) > 1:
        raise LoopError(f"{path} holds several jobs: {[j['job'] for j in jobs]}. Name one with PATH::JOB_NAME.")
    verdicts = list(report["verdicts"]) + (jobs[0]["verdicts"] if jobs else [])
    if not jobs and not any(v.startswith("no trials ran") for v in verdicts):
        verdicts.append(f"no trials ran: no job {job!r} under {path}")
    refusing = [v for v in verdicts if v.startswith(REFUSING)]
    return (jobs[0] if jobs else {}), refusing


def cmd_read(args: argparse.Namespace) -> None:
    adapter = adapter_of(args.loop)
    runs = runs_py()
    specs = [("run", args.run)] + ([("baseline", args.baseline)] if args.baseline else [])
    for _, spec in specs:
        split_spec(spec)
    round_dir = round_for_read(args.loop)
    for stale in ("metrics.json", "input.json", "health.txt"):
        if os.path.exists(os.path.join(round_dir, stale)):
            os.remove(os.path.join(round_dir, stale))

    texts, refused, agents = [], [], {}
    for label, spec in specs:
        path, _ = split_spec(spec)
        texts.append(f"# {label}: {spec}\n" + call_runs(["health", "--pass-threshold", str(args.pass_threshold), path]))
        job, refusing = health_job(spec, args.pass_threshold)
        agents[label] = job.get("agents") or []
        for v in refusing:
            refused.append(f"{label}: {v}")
            for message, example in (job.get("error_examples") or {}).items():
                if message in v and example:
                    refused.append(f"{label}: e.g. {example}")
    with open(os.path.join(round_dir, "health.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(texts))

    record = {
        "schema": "run-to-fix.input/v1", "created": now(), "round": os.path.basename(round_dir),
        "run": {"spec": args.run, "job_dir": None}, "baseline": {"spec": args.baseline, "job_dir": None} if args.baseline else None,
        "adapter_sha256": adapter.sha256, "budget_usd": args.budget_usd, "pass_threshold": args.pass_threshold,
        "run_analysis": {"runs_py": runs, "sha256": sha256_of(runs)}, "refused": bool(refused),
        "reason": "; ".join(refused) or None,
    }
    if refused:
        write_json(os.path.join(round_dir, "input.json"), record)
        raise LoopError("the run is not analysable, so the loop stops here. health.txt has the report.\n  "
                        + "\n  ".join(refused))
    if agents["run"] != [adapter.agent]:
        raise LoopError(f"adapter.json is for the agent {adapter.agent!r}, and the trials of the run name "
                        + ", ".join(repr(a) for a in agents["run"]))

    metrics = os.path.join(round_dir, "metrics.json")
    order = ([("baseline", args.baseline)] if args.baseline else []) + [("run", args.run)]
    paths, labels = [], []
    for label, spec in order:
        path, job = split_spec(spec)
        paths.append(path + (f"::{job}" if job else ""))
        labels += ["--label", label]
    print(call_runs(["analyze"] + paths + labels + ["--pass-threshold", str(args.pass_threshold), "--out", metrics]))
    by_label = {r["label"]: r for r in read_json(metrics)["runs"]}
    record["run"]["job_dir"] = by_label["run"]["dir"]
    if args.baseline:
        record["baseline"]["job_dir"] = by_label["baseline"]["dir"]
    write_json(os.path.join(round_dir, "input.json"), record)
    print(f"wrote {round_dir}: input.json, health.txt, metrics.json")


def pair_of(task: str, run_solved: Dict[str, bool], base_solved: Optional[Dict[str, bool]]) -> str:
    if base_solved is None or task not in base_solved:
        return ""
    r, b = run_solved[task], base_solved[task]
    return "both" if r and b else "run_only" if r else "baseline_only" if b else "neither"


def cross_check(metrics: dict, rows: List[TrialRow], base_rows: Optional[List[TrialRow]]) -> None:
    """Stop when the counts of these trial readers and of run-analysis differ."""
    by_label = {r["label"]: r for r in metrics["runs"]}
    problems = []
    for label, got in (("run", rows), ("baseline", base_rows)):
        if got is None:
            continue
        want = by_label[label]
        mine = (len(got), sum(r.passed for r in got))
        theirs = (want["trials"], want["trials_passed"])
        if mine != theirs:
            problems.append(f"{label}: trials and passes {mine} here, {theirs} in metrics.json")
    if base_rows is not None:
        run_solved, _ = solved_tasks(rows)
        base_solved, _ = solved_tasks(base_rows)
        common = set(run_solved) & set(base_solved)
        counts = collections.Counter(pair_of(t, run_solved, base_solved) for t in common)
        pair = metrics["pairs"][0]
        mine_p = (counts["both"], counts["baseline_only"], counts["run_only"], counts["neither"])
        theirs_p = (pair["both"], pair["first_only"], pair["second_only"], pair["neither"])
        if mine_p != theirs_p:
            problems.append(f"pairs (both, baseline only, run only, neither) {mine_p} here, {theirs_p} in metrics.json")
    if problems:
        raise LoopError("the trials read here disagree with run-analysis, so nothing is written: " + "; ".join(problems))


def agent_classes(adapter: Adapter, round_dir: str, job_dir: str, timeout_s: float) -> dict:
    line = adapter.commands["classes"]
    if line is None:
        return {"ran": False, "reason": "commands.classes is null: the agent's own classes did not run", "trials": {}}
    out_json = os.path.join(round_dir, "agent-classes.json")
    values = {"job_dir": job_dir, "out_json": out_json}
    if "classes" in adapter.paid:
        path = write_paid(round_dir, "classes", line, values, adapter.checkout)
        return {"ran": False, "reason": f"commands.classes costs money: written to {path}, not started", "trials": {}}
    ran = run_command(line, values, adapter.checkout, os.path.join(round_dir, "logs", "classes.log"), timeout_s)
    if ran.code != 0:
        status = "did not finish in time" if ran.code is None else f"exited with code {ran.code}"
        raise LoopError(f"commands.classes {status}; the log is {ran.log}:\n{ran.tail}")
    data = read_json(out_json)
    trials = data.get("trials") if isinstance(data, dict) else None
    if not isinstance(trials, dict) or not all(
            isinstance(v, dict) and isinstance(v.get("classes", []), list) and isinstance(v.get("flags", []), list)
            for v in trials.values()):
        raise LoopError(f"{out_json} must be {{\"trials\": {{\"<trial_name>\": {{\"classes\": [...], \"flags\": [...]}}}}}}")
    return {"ran": True, "reason": None, "trials": trials}


def warnings_of(group_rows: List[TrialRow], failures: int, by_task: Dict[str, List[TrialRow]],
                base_solved: Optional[Dict[str, bool]]) -> List[dict]:
    out = []
    n = len(group_rows)
    if n >= FREQUENT_MIN_TRIALS and n >= FREQUENT_MIN_SHARE * failures:
        out.append({"kind": "frequent", "trials": n,
                    "text": f"{n} of {failures} failing trials share this signature. Read two or three of them."})
    tasks = sorted({r.task for r in group_rows})
    if base_solved is not None and all(t in base_solved and not base_solved[t] for t in tasks):
        out.append({"kind": "fails_on_baseline", "tasks": len(tasks),
                    "text": f"All {len(tasks)} tasks of this group also fail on the baseline, so the benchmark is a "
                            "suspect. Check one task's verifier and expected answer before any claim that the "
                            "benchmark is defective."})
    repeated = [t for t in tasks if len(by_task[t]) > 1]
    every = [t for t in repeated if not any(r.passed for r in by_task[t])]
    if every:
        out.append({"kind": "fails_in_every_repeat", "tasks": len(every),
                    "text": f"{len(every)} of the {len(tasks)} tasks of this group fail in every one of their trials."})
    return out


def cmd_classify(args: argparse.Namespace) -> None:
    adapter = adapter_of(args.loop)
    round_dir = open_round(args.loop)
    record = read_input(round_dir)
    rules: List[Rule] = load_rules(args.loop)
    threshold = float(record["pass_threshold"])
    rows = [read_trial(d, threshold) for d in trial_dirs(record["run"]["job_dir"])]
    base_rows = ([read_trial(d, threshold) for d in trial_dirs(record["baseline"]["job_dir"])]
                 if record["baseline"] else None)
    cross_check(read_json(os.path.join(round_dir, "metrics.json")), rows, base_rows)
    agent = agent_classes(adapter, round_dir, record["run"]["job_dir"], args.command_timeout_s)

    run_solved, by_task = solved_tasks(rows)
    base_solved = solved_tasks(base_rows)[0] if base_rows is not None else None
    own_classes = sorted({c for v in agent["trials"].values() for c in v.get("classes", [])})
    own_flags = sorted({f for v in agent["trials"].values() for f in v.get("flags", [])})
    columns = (BASE_COLUMNS + [f"found:{r.id}" for r in rules] + [f"agent_class:{c}" for c in own_classes]
               + [f"agent_flag:{f}" for f in own_flags])

    table, classes, signatures = [], {}, {}
    found_counts = {r.id: collections.Counter() for r in rules}
    for row in rows:
        cls = classify(row, adapter.limits)
        sig = signature(row, cls)
        classes[row.trial], signatures[row.trial] = cls, sig
        cells = {"trial": row.trial, "task": row.task, "class": cls, "signature": sig,
                 "reward": "" if row.reward is None else row.reward, "pair": pair_of(row.task, run_solved, base_solved),
                 "cost_usd": "" if row.cost_usd is None else row.cost_usd,
                 "model_calls": "" if row.model_calls is None else row.model_calls,
                 "agent_s": "" if row.agent_s is None else round(row.agent_s, 1),
                 "max_completion_tokens": "" if row.max_completion_tokens is None else row.max_completion_tokens,
                 "exception_type": row.exception_type or ""}
        for rule in rules:
            value = apply(rule, row.dir)
            cells[f"found:{rule.id}"] = value
            found_counts[rule.id][value] += 1
            if value == "1" and cls != "pass":
                found_counts[rule.id]["failing"] += 1
        own = agent["trials"].get(row.trial) or {}
        for c in own_classes:
            cells[f"agent_class:{c}"] = int(c in own.get("classes", []))
        for f in own_flags:
            cells[f"agent_flag:{f}"] = int(f in own.get("flags", []))
        table.append(cells)

    failing = [r for r in rows if classes[r.trial] != "pass"]
    by_sig: Dict[str, List[TrialRow]] = collections.defaultdict(list)
    for r in failing:
        by_sig[signatures[r.trial]].append(r)
    ordered = sorted(by_sig.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    groups = [{
        "id": f"g{i + 1:02d}", "class": classes[members[0].trial], "signature": sig, "trials": len(members),
        "tasks": len({m.task for m in members}), "trial_names": sorted(m.trial for m in members),
        "task_names": sorted({m.task for m in members}),
        "warnings": warnings_of(members, len(failing), by_task, base_solved),
    } for i, (sig, members) in enumerate(ordered)]

    with open(os.path.join(round_dir, "trials.csv"), "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(table)
    count = collections.Counter(classes.values())
    write_json(os.path.join(round_dir, "groups.json"), {
        "schema": "run-to-fix.groups/v1",
        "counts": "trials of the run; each trial has one class, and a group is the failing trials of one signature",
        "trials": len(rows), "failures": len(failing),
        "classes": {c: count[c] for c in CLASSES}, "rules_off": rules_off(adapter.limits),
        "groups": groups,
        "found_classes": {r.id: {"meaning": r.meaning, "file": r.file, "trials": found_counts[r.id]["1"],
                                 "failing_trials": found_counts[r.id]["failing"],
                                 "unreadable": found_counts[r.id][UNREADABLE]} for r in rules},
        "agent_classes": {"ran": agent["ran"], "reason": agent["reason"],
                          "classes": {c: sum(c in v.get("classes", []) for v in agent["trials"].values()) for c in own_classes},
                          "flags": {f: sum(f in v.get("flags", []) for v in agent["trials"].values()) for f in own_flags}},
    })
    print(f"wrote {round_dir}: trials.csv, groups.json")
    print(f"{len(rows)} trials of the run, {len(failing)} failing, in {len(groups)} groups")
    for c in CLASSES:
        print(f"  {c:16s} {count[c]:5d} trials")
    for g in groups[:15]:
        marks = ", ".join(w["kind"] for w in g["warnings"])
        print(f"  {g['id']} {g['trials']:4d} trials  {g['signature'][:110]}" + (f"  [{marks}]" if marks else ""))
    if not agent["ran"]:
        print(f"  check not run: {agent['reason']}")


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rd = sub.add_parser("read", help="stage 1: read the run and its baseline")
    rd.add_argument("--loop", required=True, help="the loop folder a person named; it holds adapter.json")
    rd.add_argument("--run", required=True, help="the job folder of the run, or a folder holding it as PATH::JOB")
    rd.add_argument("--baseline", help="the job folder the run is compared with, as for --run")
    rd.add_argument("--budget-usd", type=float, required=True, help="the budget the person named for this round")
    rd.add_argument("--pass-threshold", type=float, default=1.0, help="reward at or above this is a pass (default 1)")
    rd.set_defaults(func=cmd_read)
    cl = sub.add_parser("classify", help="stage 2: classes, signatures and groups of the open round")
    cl.add_argument("--loop", required=True)
    cl.add_argument("--command-timeout-s", type=float, default=3600.0,
                    help="an adapter command still running after this many seconds is stopped (default 3600)")
    cl.set_defaults(func=cmd_classify)
    args = ap.parse_args(argv)
    try:
        args.func(args)
    except (LoopError, AdapterError, RuleError, TrialError) as exc:
        print(f"run-to-fix: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
