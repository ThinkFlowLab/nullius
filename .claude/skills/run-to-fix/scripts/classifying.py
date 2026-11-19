"""Stage 2, `loop.py classify`: one class and signature per trial, the groups, their warnings, extra columns."""
from __future__ import annotations

import argparse
import collections
import csv
import os
from typing import Dict, List, Optional, Tuple

from adapter import Adapter, Limits
from found import UNREADABLE, Rule, apply, load_rules
from reading import adapter_of
from rounds import LoopError, open_round, read_input, read_json, run_command, write_json, write_paid
from trials import CLASSES, Signature, TrialRow, classify, read_trial, rules_off, signature, solved_tasks, trial_dirs

FREQUENT_MIN_TRIALS = 3
FREQUENT_MIN_SHARE = 0.05
BENCHMARK_SUSPECT_TASKS = 3
BASE_COLUMNS = ["trial", "task", "class", "signature", "reward", "pair", "cost_usd", "model_calls", "agent_s",
                "max_completion_tokens", "exception_type"]


def read_rows(job_dir: str, threshold: float) -> List[TrialRow]:
    return [read_trial(d, threshold) for d in trial_dirs(job_dir)]


def classes_of(rows: List[TrialRow], limits: Limits) -> Tuple[Dict[str, str], Dict[str, Signature]]:
    """The class and the signature of each trial, by trial name."""
    classes, signatures = {}, {}
    for row in rows:
        cls = classify(row, limits)
        classes[row.trial], signatures[row.trial] = cls, signature(row, cls)
    return classes, signatures


def pair_of(task: str, run_solved: Dict[str, bool], base_solved: Optional[Dict[str, bool]]) -> str:
    if base_solved is None or task not in base_solved:
        return ""
    r, b = run_solved[task], base_solved[task]
    return "both" if r and b else "run_only" if r else "baseline_only" if b else "neither"


def cross_check(metrics_path: str, runs: List[Tuple[str, List[TrialRow]]]) -> None:
    """Stop when the counts of these trial readers and of run-analysis differ. `runs` is in the order of
    `runs.py analyze`, so a pair is first against second."""
    metrics = read_json(metrics_path)
    name = os.path.basename(metrics_path)
    by_label = {r["label"]: r for r in metrics["runs"]}
    problems = []
    for label, got in runs:
        mine = (len(got), sum(r.passed for r in got))
        theirs = (by_label[label]["trials"], by_label[label]["trials_passed"])
        if mine != theirs:
            problems.append(f"{label}: trials and passes {mine} here, {theirs} in {name}")
    if len(runs) == 2:
        (first, a), (second, b) = runs
        sa, sb = solved_tasks(a)[0], solved_tasks(b)[0]
        common = set(sa) & set(sb)
        mine_p = (sum(sa[t] and sb[t] for t in common), sum(sa[t] and not sb[t] for t in common),
                  sum(sb[t] and not sa[t] for t in common), sum(not sa[t] and not sb[t] for t in common))
        pair = metrics["pairs"][0]
        theirs_p = (pair["both"], pair["first_only"], pair["second_only"], pair["neither"])
        if mine_p != theirs_p:
            problems.append(f"pairs (both, {first} only, {second} only, neither) {mine_p} here, {theirs_p} in {name}")
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
        text = (f"All {len(tasks)} tasks of this group also fail on the baseline, so the benchmark is a suspect. "
                "Check one task's verifier and expected answer before any claim that the benchmark is defective."
                if len(tasks) >= BENCHMARK_SUSPECT_TASKS else
                ("The task fails on both runs." if len(tasks) == 1 else f"The {len(tasks)} tasks fail on both runs."))
        out.append({"kind": "fails_on_baseline", "tasks": len(tasks), "text": text})
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
    rows = read_rows(record["run"]["job_dir"], threshold)
    base_rows = read_rows(record["baseline"]["job_dir"], threshold) if record["baseline"] else None
    cross_check(os.path.join(round_dir, "metrics.json"),
                ([("baseline", base_rows)] if base_rows is not None else []) + [("run", rows)])
    agent = agent_classes(adapter, round_dir, record["run"]["job_dir"], args.command_timeout_s)

    run_solved, _ = solved_tasks(rows)
    base_solved = solved_tasks(base_rows)[0] if base_rows is not None else None
    own_classes = sorted({c for v in agent["trials"].values() for c in v.get("classes", [])})
    own_flags = sorted({f for v in agent["trials"].values() for f in v.get("flags", [])})
    columns = (BASE_COLUMNS + [f"found:{r.id}" for r in rules] + [f"agent_class:{c}" for c in own_classes]
               + [f"agent_flag:{f}" for f in own_flags])

    classes, signatures = classes_of(rows, adapter.limits)
    table = []
    found_counts = {r.id: collections.Counter() for r in rules}
    for row in rows:
        cls, sig = classes[row.trial], signatures[row.trial].text
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
    groups = failure_groups(rows, classes, signatures, base_solved)

    with open(os.path.join(round_dir, "trials.csv"), "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(table)
    count = collections.Counter(classes.values())
    write_json(os.path.join(round_dir, "groups.json"), {
        "schema": "run-to-fix.groups/v1",
        "counts": "trials of the run; each trial has one class, and a group is the failing trials of one signature",
        "trials": len(rows), "failures": len(failing),
        "classes": {c: count[c] for c in CLASSES}, "rules_off": rules_off(adapter.limits, rows),
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


def failure_groups(rows: List[TrialRow], classes: Dict[str, str], signatures: Dict[str, Signature],
                   base_solved: Optional[Dict[str, bool]]) -> List[dict]:
    """The failing trials grouped by signature, largest first, each with its warnings."""
    _, by_task = solved_tasks(rows)
    failing = [r for r in rows if classes[r.trial] != "pass"]
    by_sig: Dict[str, List[TrialRow]] = collections.defaultdict(list)
    for r in failing:
        by_sig[signatures[r.trial].text].append(r)
    ordered = sorted(by_sig.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [{
        "id": f"g{i + 1:02d}", "class": classes[members[0].trial], "signature": sig,
        "example": signatures[members[0].trial].example, "trials": len(members),
        "tasks": len({m.task for m in members}), "trial_names": sorted(m.trial for m in members),
        "task_names": sorted({m.task for m in members}),
        "warnings": warnings_of(members, len(failing), by_task, base_solved),
    } for i, (sig, members) in enumerate(ordered)]
