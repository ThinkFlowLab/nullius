"""The two stops: `loop.py decide` writes a decision page, `loop.py answer` records the person's answer."""
from __future__ import annotations

import argparse
import os
import re
from typing import Dict, List, Optional

from classifying import read_rows
from comparing import TARGET_TRIALS, passes, targets_of, usd
from reading import adapter_of, health
from rounds import LoopError, ledger_round, load_ledger, now, open_round, read_input, read_json, save_ledger, write_json, write_paid
from trials import TrialRow

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
CHOICES = ("stop", "rerun", "full")


def fill_page(template: str, values: Dict[str, str], out: str) -> None:
    with open(os.path.join(ASSETS, template), encoding="utf-8") as fh:
        page = fh.read()
    for key, value in values.items():
        page = page.replace("{{" + key + "}}", value)
    left = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", page)))
    if left:
        raise LoopError(f"{template}: placeholders not filled: {left}")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(page)


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def table_text(t: Dict[str, int]) -> str:
    return f"{t['done_pass']} / {t['done_fail']} / {t['not_done_pass']} / {t['not_done_fail']}"


def candidate_row(cand: dict, checks: Optional[dict]) -> List[str]:
    pred = cand["prediction"]
    explains = f"{len(pred['trials'])} trials on {len(pred['tasks'])} tasks ({cand['rule']})"
    if checks is None:
        return [cand["id"], cand["cause"], explains, "not checked", "not checked", "not checked", "not checked", ""]
    test, suite, replay, diff = checks["test"], checks["suite"], checks["replay"], checks["diff"]
    if not test["ran"]:
        test_text = "did not run"
    else:
        test_text = {(True, True): "fails, then passes", (False, True): "passes before the fix too",
                     (True, False): "still fails after the fix", (False, False): "passes before, fails after"}[
            (test["fails_before"], test["passes_after"])]
    suite_text = ("did not run" if not suite["ran"] else
                  ", ".join(suite["added"]) if suite["added"] else "none")
    if replay["ran"]:
        replay_text = f"{table_text(replay['before'])} → {table_text(replay['after'])}"
        false_text = f"{replay['false_success_added']:+d}" + (
            f" (new: {', '.join(replay['false_success_new'])})" if replay["false_success_new"] else "")
    else:
        replay_text, false_text = "did not run", "did not run"
    diff_text = f"{diff['files']} files, +{diff['added']} −{diff['removed']}"
    return [cand["id"], cand["cause"], explains, test_text, suite_text, replay_text, false_text, diff_text]


def decide_one(args: argparse.Namespace, round_dir: str, record: dict) -> None:
    base = os.path.join(round_dir, "candidates")
    ids = sorted(d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, "candidate.json"))) \
        if os.path.isdir(base) else []
    if not ids:
        raise LoopError("no candidate is registered: add one with loop.py candidate, then loop.py check")
    rows, not_run = [], []
    for cid in ids:
        cand = read_json(os.path.join(base, cid, "candidate.json"))
        checks_path = os.path.join(base, cid, "checks.json")
        checks = read_json(checks_path) if os.path.isfile(checks_path) else None
        rows.append("| " + " | ".join(cell(c) for c in candidate_row(cand, checks)) + " |")
        if checks is None:
            not_run.append(f"- {cid}: not checked; run loop.py check --candidate {cid}")
            continue
        for name in ("test", "suite", "replay"):
            if not checks[name]["ran"]:
                not_run.append(f"- {cid}, {name}: {checks[name]['reason']}")
    trials = len(read_rows(record["run"]["job_dir"], float(record["pass_threshold"])))
    fill_page("decision-1.md", {
        "ROUND": os.path.basename(round_dir), "LOOP": args.loop, "TRIALS": str(trials),
        "SUMMARY": f"{len(ids)} candidates, checked against the run {record['run']['job_dir']}.",
        "ROWS": "\n".join(rows), "NOT_RUN": "\n".join(not_run) or "Every check ran.",
    }, os.path.join(round_dir, "decision-1.md"))
    print(f"wrote {round_dir}/decision-1.md: {len(ids)} candidates, {len(not_run)} checks that did not run")


def decide_two(args: argparse.Namespace, round_dir: str, record: dict) -> None:
    adapter = adapter_of(args.loop)
    ledger = load_ledger(args.loop)
    entry = ledger_round(ledger, os.path.basename(round_dir))
    selection = entry.get("selection")
    if not selection:
        raise LoopError("no trial run was selected: run loop.py select with --size first")
    threshold = float(record["pass_threshold"])
    checked = health("trial", args.trial_job, threshold)
    if checked.refused:
        raise LoopError("the trial run is not analysable:\n  " + "\n  ".join(checked.refused))
    if checked.agents != [adapter.agent]:
        raise LoopError(f"adapter.json is for the agent {adapter.agent!r}, and the trial run names "
                        + ", ".join(repr(a) for a in checked.agents))
    job_dir = os.path.abspath(args.trial_job)
    rows: List[TrialRow] = read_rows(job_dir, threshold)
    before = read_rows(record["run"]["job_dir"], threshold)
    targets = targets_of(selection, before, rows)
    of = lambda tasks: passes([r for r in rows if r.task in set(tasks)])
    costs = [r.cost_usd for r in rows]
    spent = sum(c for c in costs if c is not None)
    per_trial = spent / len(rows) if rows else None
    left = float(record["budget_usd"]) - spent
    rerun_tasks = selection["targets"] + selection["guards"]
    full_tasks = sorted({r.task for r in before})
    options = [{"name": "stop", "what": "nothing", "usd": 0.0, "command": None}]
    for name, tasks, repeat in (("rerun", rerun_tasks, TARGET_TRIALS), ("full", full_tasks, 1)):
        units = os.path.join(round_dir, f"{name}-tasks.txt")
        with open(units, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(t + "\n" for t in tasks))
        command = None
        line = adapter.commands["run_tasks"]
        if line is not None:
            build = read_json(os.path.join(round_dir, "build.json"))
            command = write_paid(round_dir, f"run_tasks-{name}", line, {
                "units_file": units, "repeat": str(repeat), "jobs_dir": os.path.join(round_dir, "jobs"),
                "budget_usd": f"{max(left, 0.0):.2f}", "build": build["path"] or "", "ref": selection["ref"]},
                adapter.checkout)
        what = (f"the {len(tasks)} targets and guards again, {repeat} trials each" if name == "rerun"
                else f"a full pass of the {len(tasks)} tasks of the run, {repeat} trial each")
        options.append({"name": name, "what": what, "usd": len(tasks) * repeat * per_trial if per_trial is not None else None,
                        "command": command})
    build = read_json(os.path.join(round_dir, "build.json"))
    trial_record = {
        "schema": "run-to-fix.trial/v1", "created": now(), "job_dir": job_dir, "trials": len(rows),
        "cost_usd": spent, "cost_complete": all(c is not None for c in costs), "cost_per_trial_usd": per_trial,
        "budget_usd": float(record["budget_usd"]), "budget_left_usd": left, "targets": targets,
        "guards": of(selection["guards"]), "canaries": of(selection["canaries"]), "options": options,
        "build": build,
    }
    write_json(os.path.join(round_dir, "trial.json"), trial_record)
    share = lambda x: f"{x['passed']} of {x['trials']}" if x else "none"
    results = [f"| targets ({targets['tasks']}) | {share(targets['before'])} | {share(targets['after'])} | "
               f"{targets['verdict'] or 'none'}, p = {targets['p']:.3g} |" if targets["p"] is not None else
               "| targets | none | none | none |"]
    for name in ("guards", "canaries"):
        results.append(f"| {name} ({len(selection[name])}) | | {share(trial_record[name])} | |")
    option_rows = [f"| {o['name']} | {o['what']} | {usd(o['usd']) if o['usd'] is not None else 'unknown'} | "
                   f"{cell(o['command']) if o['command'] else 'none'} |" for o in options]
    fill_page("decision-2.md", {
        "ROUND": os.path.basename(round_dir), "LOOP": args.loop,
        "BUILD": f"{build['path']} from {build['ref'][:12]}" if build["path"] else f"{build['reason']} ({build['ref'][:12]})",
        "TRIAL_JOB": job_dir, "TRIALS": str(len(rows)),
        "SPENT": usd(spent) + ("" if trial_record["cost_complete"] else " at least: some trials report no cost"),
        "LEFT": usd(left), "BUDGET": usd(float(record["budget_usd"])), "RESULTS": "\n".join(results),
        "OPTIONS": "\n".join(option_rows), "PER_TRIAL": usd(per_trial) if per_trial is not None else "unknown",
    }, os.path.join(round_dir, "decision-2.md"))
    print(f"wrote {round_dir}: trial.json, decision-2.md; budget left {usd(left)}")
    for o in options:
        print(f"  {o['name']}: {o['what']}, " + (usd(o["usd"]) if o["usd"] is not None else "price unknown"))


def cmd_decide(args: argparse.Namespace) -> None:
    round_dir = open_round(args.loop)
    record = read_input(round_dir)
    if args.stop == 1:
        decide_one(args, round_dir, record)
    elif args.trial_job is None:
        raise LoopError("decide --stop 2 needs --trial-job: the job folder of the trial run")
    else:
        decide_two(args, round_dir, record)


def cmd_answer(args: argparse.Namespace) -> None:
    round_dir = open_round(args.loop)
    page = os.path.join(round_dir, f"decision-{args.stop}.md")
    if not os.path.isfile(page):
        raise LoopError(f"{page} is missing: write it with loop.py decide --stop {args.stop} before the answer")
    if not args.text.strip():
        raise LoopError("--text holds the person's words and cannot be empty")
    ledger = load_ledger(args.loop)
    entry = ledger_round(ledger, os.path.basename(round_dir))
    if args.stop == 1:
        if args.keep is None:
            raise LoopError("answer --stop 1 needs --keep: candidate ids separated by commas, or none")
        keep = [] if args.keep == "none" else [k.strip() for k in args.keep.split(",") if k.strip()]
        missing = [k for k in keep if not os.path.isfile(os.path.join(round_dir, "candidates", k, "checks.json"))]
        if missing:
            raise LoopError(f"these candidates are not checked: {', '.join(missing)}")
        entry["answers"].append({"about": "stop 1: the candidates to keep", "date": now(), "text": args.text,
                                 "keep": keep})
        entry["kept"] = keep
        entry["predictions"] = []
        for k in keep:
            cand = read_json(os.path.join(round_dir, "candidates", k, "candidate.json"))
            entry["predictions"].append({"candidate": k, "cause": cand["cause"], "rule": cand["rule"],
                                         "tasks": cand["prediction"]["tasks"], "expect": "pass"})
    else:
        if args.choice not in CHOICES:
            raise LoopError(f"answer --stop 2 needs --choice, one of {', '.join(CHOICES)}")
        entry["answers"].append({"about": "stop 2: what to run next", "date": now(), "text": args.text,
                                 "choice": args.choice})
        entry["next"] = args.choice
    save_ledger(args.loop, ledger)
    print(f"recorded the answer of stop {args.stop} in ledger.json")
