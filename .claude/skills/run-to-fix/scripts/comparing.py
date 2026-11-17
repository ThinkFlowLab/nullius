"""Stage 5, `loop.py compare`: a new run against the round's run or baseline, the verdict of D7, the stop rule."""
from __future__ import annotations

import argparse
import os
from typing import Dict, List, Optional

from classifying import classes_of, cross_check, failure_groups, read_rows
from reading import adapter_of, analyze, health
from rounds import (LoopError, call_runs, ledger_round, load_ledger, now, open_round, read_input, read_json,
                    save_ledger, write_json)
from stats import binomial_two_sided, fisher_two_sided, verdict
from trials import TrialRow

TARGET_TRIALS = 3
GROUP_MIN_TRIALS = 3
STOP_RULE = ("No new round when the verdict is noise and no group of failing trials of the new run has 3 trials "
             "or more, or when the budget left is under the price of the cheapest option: a trial run of the tasks "
             "of the largest such group, 3 trials each, at the new run's mean cost per trial.")


def rate_of(run: dict) -> dict:
    return {"rate": run["pass_rate"], "standard_error": run["standard_error"], "tasks": run["tasks"],
            "trials": run["trials"], "trials_passed": run["trials_passed"]}


def passes(rows: List[TrialRow]) -> dict:
    return {"trials": len(rows), "passed": sum(r.passed for r in rows)}


def targets_of(selection: Optional[dict], before: List[TrialRow], new: List[TrialRow]) -> dict:
    """D7.3: passes of the target tasks' trials before and after, with Fisher's exact test."""
    targets = set((selection or {}).get("targets") or [])
    if not targets:
        return {"tasks": 0, "before": None, "after": None, "p": None, "verdict": None,
                "text": "no targets were selected in this round, so no trials of targets are compared"}
    b = passes([r for r in before if r.task in targets])
    a = passes([r for r in new if r.task in targets])
    p = fisher_two_sided(b["passed"], b["trials"] - b["passed"], a["passed"], a["trials"] - a["passed"])
    share = lambda x: x["passed"] / x["trials"] if x["trials"] else 0.0
    return {"tasks": len(targets), "before": b, "after": a, "p": p,
            "verdict": verdict(p, share(a) > share(b), share(a) < share(b)),
            "text": f"{len(targets)} tasks: {b['trials']} trials before and {a['trials']} after, "
                    f"{b['trials'] + a['trials']} trials in all; Fisher's exact two-sided test gives p = {p:.3g}"}


def predictions_of(predictions: List[dict], new: List[TrialRow]) -> List[dict]:
    """Each prediction of stage 3 beside the passes of its tasks in the new run."""
    out = []
    for pred in predictions:
        tasks = set(pred.get("tasks") or [])
        out.append(dict(pred, result=dict(passes([r for r in new if r.task in tasks]), tasks=len(tasks))))
    return out


def stop_rule(result_verdict: str, groups: List[dict], budget: float, spent: Optional[float],
              cost_per_trial: Optional[float]) -> dict:
    big = [g for g in groups if g["trials"] >= GROUP_MIN_TRIALS]
    reasons, not_checked = [], []
    if result_verdict == "noise" and not big:
        reasons.append(f"the verdict is noise and no group of the new run has {GROUP_MIN_TRIALS} failing trials or more")
    cheapest = None
    if big:
        price = big[0]["tasks"] * TARGET_TRIALS * cost_per_trial if cost_per_trial is not None else None
        cheapest = {"what": f"a trial run of the {big[0]['tasks']} tasks of {big[0]['id']}, {TARGET_TRIALS} trials each",
                    "usd": price}
    left = budget - spent if spent is not None else None
    if cheapest is not None:
        if cheapest["usd"] is None or left is None:
            not_checked.append("the budget: the new run reports no cost")
        elif left < cheapest["usd"]:
            reasons.append(f"the budget left, ${left:.2f}, is under the price of the cheapest option, ${cheapest['usd']:.2f}")
    return {"rule": STOP_RULE, "stop": bool(reasons), "reasons": reasons, "not_checked": not_checked,
            "groups_of_3_or_more": len(big), "budget_usd": budget, "spent_usd": spent, "left_usd": left,
            "cheapest_option": cheapest}


def cmd_compare(args: argparse.Namespace) -> None:
    adapter = adapter_of(args.loop)
    round_dir = open_round(args.loop)
    record = read_input(round_dir)
    threshold = float(record["pass_threshold"])
    against = record["run"] if args.against == "run" else record["baseline"]
    if against is None:
        raise LoopError(f"{os.path.basename(round_dir)} has no baseline; compare --against run, or read the run "
                        "again with --baseline")
    checked = health("new", args.new, threshold)
    if checked.refused:
        raise LoopError("the new run is not analysable, so it is not compared:\n  " + "\n  ".join(checked.refused))
    if checked.agents != [adapter.agent]:
        raise LoopError(f"adapter.json is for the agent {adapter.agent!r}, and the trials of the new run name "
                        + ", ".join(repr(a) for a in checked.agents))

    metrics_path = os.path.join(round_dir, "compare-metrics.json")
    print(analyze([("before", against["job_dir"]), ("new", args.new)], threshold, metrics_path))
    metrics = read_json(metrics_path)
    runs: Dict[str, dict] = {r["label"]: r for r in metrics["runs"]}
    new_dir = runs["new"]["dir"]
    before_rows, new_rows = read_rows(against["job_dir"], threshold), read_rows(new_dir, threshold)
    cross_check(metrics_path, [("before", before_rows), ("new", new_rows)])

    pair = metrics["pairs"][0]
    won, lost = pair["second_only"], pair["first_only"]
    p = binomial_two_sided(won, lost)
    result_verdict = verdict(p, won > lost, lost > won)
    ledger = load_ledger(args.loop)
    entry = ledger_round(ledger, os.path.basename(round_dir))
    classes, signatures = classes_of(new_rows, adapter.limits)
    groups = failure_groups(new_rows, classes, signatures, None)
    spent = 0.0 if new_dir == record["run"]["job_dir"] else runs["new"]["cost_total"]
    rule = stop_rule(result_verdict, groups, float(record["budget_usd"]), spent, runs["new"]["cost_mean"])
    result = {
        "schema": "run-to-fix.result/v1", "created": now(), "round": os.path.basename(round_dir),
        "counts": "tasks for pairs; a task is solved when at least half of its trials pass. Trials for targets.",
        "against": {"label": args.against, "job_dir": against["job_dir"]}, "new": {"spec": args.new, "job_dir": new_dir},
        "pass_rate": {"before": rate_of(runs["before"]), "new": rate_of(runs["new"])},
        "pairs": {"common_tasks": metrics["overlap"]["common_tasks"], "both": pair["both"], "won": won, "lost": lost,
                  "neither": pair["neither"]},
        "test": {"name": "exact two-sided binomial test on won and lost tasks", "p": p}, "verdict": result_verdict,
        "targets": targets_of(entry.get("selection"), before_rows, new_rows),
        "predictions": predictions_of(entry.get("predictions") or [], new_rows),
        "groups": [{k: g[k] for k in ("id", "class", "signature", "trials", "tasks")} for g in groups],
        "stop_rule": rule,
    }
    report = os.path.join(round_dir, "report.html")
    call_runs(["render", metrics_path, "--out", report])
    entry["result"] = {"verdict": result_verdict, "p": p, "won": won, "lost": lost, "stop": rule["stop"],
                       "reasons": rule["reasons"], "file": os.path.join(os.path.basename(round_dir), "result.json")}
    save_ledger(args.loop, ledger)
    write_json(os.path.join(round_dir, "result.json"), result)

    for label in ("before", "new"):
        r = result["pass_rate"][label]
        print(f"pass rate {label}: {r['rate']:.3f}, standard error {r['standard_error']:.3f}, "
              f"over {r['tasks']} tasks ({r['trials_passed']} of {r['trials']} trials passed)")
    pr = result["pairs"]
    print(f"tasks on both: {pr['common_tasks']}; solved by both {pr['both']}, won {won}, lost {lost}, "
          f"neither {pr['neither']}")
    print(f"verdict: {result_verdict} (exact two-sided binomial test on won and lost, p = {p:.3g})")
    print(f"targets: {result['targets']['text']}")
    print("stop rule: " + ("stop: " + "; ".join(rule["reasons"]) if rule["stop"] else "go on")
          + ("" if not rule["not_checked"] else "; not checked: " + "; ".join(rule["not_checked"])))
    print(f"wrote {round_dir}: compare-metrics.json, result.json, report.html (its prose slots are open: fill them "
          "with runs.py render --slots, then runs.py check)")
