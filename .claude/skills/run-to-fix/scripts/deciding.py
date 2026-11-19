"""The two stops: `loop.py decide` writes a decision page, `loop.py answer` records the person's answer."""
from __future__ import annotations

import argparse
import collections
import os
import re
from typing import Dict, List, Optional

from adapter import source_of
from checking import candidate_id, load_candidate, load_checks
from checkout import resolve
from classifying import read_rows
from comparing import REPORT_NOT_READ, TARGET_TRIALS, passes, targets_of, trial_spent
from pricing import describe, price_of, task_costs, usd
from reading import adapter_of, health
from rounds import LoopError, ledger_round, load_ledger, now, open_round, read_input, read_json, save_ledger, write_json, write_paid
from trials import TrialRow, run_names

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


def changes_text(replay: dict) -> str:
    changes = replay["cause"]
    text = f"{changes['changed']} of {changes['trials']}"
    if changes["by_direction"]:
        text += ": " + ", ".join(f"{d} {n}" for d, n in changes["by_direction"].items())
    return text


def candidate_row(cand: dict, checks: Optional[dict]) -> List[str]:
    pred = cand["prediction"]
    explains = (f"{len(pred['trials'])} trials, {len(pred['tasks'])} tasks expected to pass ({cand['rule']})"
                if cand["effect"] == "outcome" else
                f"{len(pred['trials'])} trials of the rule, no task expected to pass ({cand['rule']})")
    if checks is None:
        return [cand["id"], cand["cause"], cand["effect"], explains] + ["not checked"] * 5 + [""]
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
        kept = replay["kept_recorded"]
        replay_text = (f"{table_text(replay['before'])} → {table_text(replay['after'])} "
                       f"({replay['before_ref'][:12]} → {replay['ref'][:12]}); {kept['before']} and {kept['after']} "
                       "trials keep their recorded label")
        change_text = changes_text(replay)
        false_text = f"{replay['false_success_added']:+d}" + (
            f" (new: {', '.join(replay['false_success_new'])})" if replay["false_success_new"] else "")
    else:
        replay_text, change_text, false_text = "did not run", "did not run", "did not run"
    diff_text = f"{diff['files']} files, +{diff['added']} −{diff['removed']}"
    return [cand["id"], cand["cause"], cand["effect"], explains, test_text, suite_text, replay_text, change_text,
            false_text, diff_text]


def decide_one(args: argparse.Namespace, round_dir: str, record: dict) -> None:
    base = os.path.join(round_dir, "candidates")
    ids = sorted(d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, "candidate.json"))) \
        if os.path.isdir(base) else []
    if not ids:
        raise LoopError("no candidate is registered: add one with loop.py candidate, then loop.py check")
    adapter = adapter_of(args.loop)
    rows, not_run, unchanged = [], [], []
    for cid in ids:
        cand = load_candidate(os.path.join(base, cid, "candidate.json"))
        checks_path = os.path.join(base, cid, "checks.json")
        checks = load_checks(round_dir, cid, adapter) if os.path.isfile(checks_path) else None
        rows.append("| " + " | ".join(cell(c) for c in candidate_row(cand, checks)) + " |")
        if checks is None:
            not_run.append(f"- {cid}: not checked; run loop.py check --candidate {cid}")
            continue
        for name in ("test", "suite", "replay"):
            if not checks[name]["ran"]:
                not_run.append(f"- {cid}, {name}: {checks[name]['reason']}")
        replay = checks["replay"]
        if replay["ran"] and replay["cause"]["changed"] == 0:
            unchanged.append(f"- {cid}: 0 of {replay['cause']['trials']} trials of its cause change their label "
                             f"between {replay['before_ref'][:12]} and {replay['ref'][:12]}.")
    trials = len(read_rows(record["run"]["job_dir"], float(record["pass_threshold"])))
    fill_page("decision-1.md", {
        "ROUND": os.path.basename(round_dir), "LOOP": args.loop, "TRIALS": str(trials),
        "SUMMARY": f"{len(ids)} candidates, checked against the run {record['run']['job_dir']}.",
        "ROWS": "\n".join(rows), "NOT_RUN": "\n".join(not_run) or "Every check ran.",
        "UNCHANGED": "\n".join(unchanged) or "None.",
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
    job_dir = checked.dir
    this_run: List[TrialRow] = read_rows(job_dir, threshold)
    code_ref = resolve(source_of(adapter, "decide --stop 2"), args.code_ref)
    if code_ref != selection["ref"]:
        raise LoopError(f"a trial run of this round runs the commit select chose, {selection['ref'][:12]}; --code-ref "
                        f"names {code_ref[:12]}")
    chosen = set(selection["targets"]) | set(selection["guards"]) | set(selection["canaries"])
    per_task = collections.Counter(r.task for r in this_run)
    problems = []
    outside = sorted(set(per_task) - chosen)
    if outside:
        problems.append("tasks outside the selection: " + ", ".join(outside))
    absent = sorted(set(selection["targets"]) - set(per_task))
    if absent:
        problems.append("targets it lacks: " + ", ".join(absent))
    uneven = sorted(t for t, n in per_task.items() if n != selection["repeat"])
    if uneven:
        problems.append(f"tasks without {selection['repeat']} trials: "
                        + ", ".join(f"{t} ({per_task[t]})" for t in uneven))
    if problems:
        raise LoopError("the trial job is not a trial run of the selection: " + "; ".join(problems))
    costs = [r.cost_usd for r in this_run]
    recorded = sum(c for c in costs if c is not None)
    missing = sum(c is None for c in costs)
    if missing and args.trial_cost_usd is None:
        raise LoopError(f"{missing} of {len(costs)} trials of the trial run report no cost, so the money it spent is "
                        f"unknown. Give the cost of the whole run with --trial-cost-usd, at least the recorded "
                        f"{usd(recorded)}.")
    if args.trial_cost_usd is not None and not missing:
        raise LoopError("every trial of the trial run reports its cost; --trial-cost-usd is for a run whose trials do not")
    if args.trial_cost_usd is not None and args.trial_cost_usd < recorded:
        raise LoopError(f"--trial-cost-usd {usd(args.trial_cost_usd)} is under the recorded {usd(recorded)}")
    runs = [t for t in entry.setdefault("trial_runs", []) if t["job_dir"] != job_dir]
    runs.append({"job_dir": job_dir, "ref": selection["ref"], "trials": len(this_run),
                 "cost_usd": args.trial_cost_usd if missing else recorded, "cost_recorded_usd": recorded,
                 "cost_from": "the person" if missing else "the trials", "cost_complete": not missing,
                 "code_ref": code_ref, "code_from": "the person",
                 "added": now()})
    entry["trial_runs"] = runs
    save_ledger(args.loop, ledger)
    # every trial run of this round on the same code adds its trials to the exact test of the targets
    same = [t for t in runs if t["ref"] == selection["ref"]]
    rows: List[TrialRow] = [r for t in same for r in read_rows(t["job_dir"], threshold)]
    before = read_rows(record["run"]["job_dir"], threshold)
    targets = targets_of(selection, before, rows)
    of = lambda tasks: passes([r for r in rows if r.task in set(tasks)])
    spent = trial_spent(entry)
    sources = [task_costs(rows), task_costs(before)]
    left = float(record["budget_usd"]) - spent
    rerun_tasks = selection["targets"] + selection["guards"]
    full_tasks = sorted({r.task for r in before})
    options = [{"name": "stop", "what": "nothing", "usd": 0.0, "unpriced_tasks": 0, "text": usd(0.0), "command": None}]
    for name, tasks, repeat in (("rerun", rerun_tasks, TARGET_TRIALS), ("full", full_tasks, 1)):
        units = os.path.join(round_dir, f"{name}-tasks.txt")
        with open(units, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(t + "\n" for t in run_names(before, tasks)))
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
        price = price_of(tasks, repeat, sources)
        options.append({"name": name, "what": what, "usd": price.usd, "unpriced_tasks": price.unpriced_tasks,
                        "text": describe(price), "command": command})
    build = read_json(os.path.join(round_dir, "build.json"))
    trial_record = {
        "schema": "run-to-fix.trial/v1", "created": now(), "job_dir": job_dir,
        "counts": "cost_usd is every trial run of the round; trials and the results are the trial runs on this ref",
        "trial_runs": runs, "trials": len(rows), "cost_usd": spent,
        "cost_complete": all(t["cost_complete"] for t in runs),
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
    option_rows = [f"| {o['name']} | {o['what']} | {o['text']} | "
                   f"{cell(o['command']) if o['command'] else 'none'} |" for o in options]
    fill_page("decision-2.md", {
        "ROUND": os.path.basename(round_dir), "LOOP": args.loop,
        "BUILD": f"{build['path']} from {build['ref'][:12]}" if build["path"] else f"{build['reason']} ({build['ref'][:12]})",
        "TRIAL_JOB": ", ".join(t["job_dir"] for t in same), "TRIALS": str(len(rows)),
        "CODE": f"{selection['ref'][:12]}, as the person states; a trial job does not record the commit it ran",
        "SPENT": usd(spent) + "".join(f" (the person's figure for {t['job_dir']}, whose trials record "
                                      f"{usd(t['cost_recorded_usd'])})" for t in runs if t.get("cost_from") == "the person"),
        "LEFT": usd(left), "BUDGET": usd(float(record["budget_usd"])), "RESULTS": "\n".join(results),
        "OPTIONS": "\n".join(option_rows),
        "REPORT_NOTE": "".join(f"\n- {p['candidate']}: {REPORT_NOT_READ}." for p in entry.get("predictions") or []
                               if p.get("effect") == "report"),
    }, os.path.join(round_dir, "decision-2.md"))
    print(f"wrote {round_dir}: trial.json, decision-2.md; budget left {usd(left)}")
    for o in options:
        print(f"  {o['name']}: {o['what']}, {o['text']}")


def cmd_decide(args: argparse.Namespace) -> None:
    round_dir = open_round(args.loop)
    record = read_input(round_dir)
    if args.stop == 1:
        decide_one(args, round_dir, record)
    elif args.trial_job is None or args.code_ref is None:
        raise LoopError("decide --stop 2 needs --trial-job, the job folder of the trial run, and --code-ref, the commit "
                        "the person states it ran")
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
        keep = [] if args.keep == "none" else [candidate_id(k.strip()) for k in args.keep.split(",") if k.strip()]
        cands = {k: load_candidate(os.path.join(round_dir, "candidates", k, "candidate.json")) for k in keep
                 if os.path.isfile(os.path.join(round_dir, "candidates", k, "candidate.json"))}
        missing = [k for k in keep if not os.path.isfile(os.path.join(round_dir, "candidates", k, "checks.json"))]
        if not missing:
            adapter = adapter_of(args.loop)
            for k in keep:
                load_checks(round_dir, k, adapter)
        if missing:
            raise LoopError(f"these candidates are not checked: {', '.join(missing)}")
        entry["answers"].append({"about": "stop 1: the candidates to keep", "date": now(), "text": args.text,
                                 "keep": keep})
        entry["kept"] = keep
        entry["predictions"] = []
        for k in keep:
            cand = cands[k]
            entry["predictions"].append({"candidate": k, "cause": cand["cause"], "rule": cand["rule"],
                                         "effect": cand["effect"], "trials": cand["prediction"]["trials"],
                                         "tasks": cand["prediction"]["tasks"],
                                         "expect": "pass" if cand["effect"] == "outcome" else "label_change"})
    else:
        if args.choice not in CHOICES:
            raise LoopError(f"answer --stop 2 needs --choice, one of {', '.join(CHOICES)}")
        entry["answers"].append({"about": "stop 2: what to run next", "date": now(), "text": args.text,
                                 "choice": args.choice})
        entry["next"] = args.choice
    save_ledger(args.loop, ledger)
    print(f"recorded the answer of stop {args.stop} in ledger.json")
