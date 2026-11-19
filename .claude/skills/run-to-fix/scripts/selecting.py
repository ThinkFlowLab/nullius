"""Stage 4, `loop.py select`: the tasks of the trial run and their price, the build, the written run command."""
from __future__ import annotations

import argparse
import hashlib
import os
import random
from typing import Dict, List, Optional, Tuple

from adapter import Adapter, source_of
from checking import load_checks
from checkout import is_ancestor, resolve, worktree, write_paid_on_ref
from classifying import read_rows
from comparing import TARGET_TRIALS, trial_spent
from pricing import describe, price_of, task_costs, usd
from reading import adapter_of
from rounds import (LoopError, ledger_round, load_ledger, now, open_round, read_input, read_json, run_command,
                    save_ledger, write_json, write_paid)
from trials import run_names, solved_tasks

#: canaries are drawn with this seed, so the same round proposes the same canaries
SEED = 20261117
SIZES = ("targets", "guards", "canaries")


def kept_candidates(round_dir: str, entry: dict, adapter: Adapter) -> List[dict]:
    if entry.get("kept") is None:
        raise LoopError("stop 1 has no answer yet: record it with loop.py answer --stop 1")
    if not entry["kept"]:
        raise LoopError("the answer of stop 1 kept no candidate, so there is nothing to try")
    return [dict(load_checks(round_dir, cid, adapter), id=cid) for cid in entry["kept"]]


def guards_of(kept: List[dict], rows_by_trial: Dict[str, Tuple[str, bool]], targets: List[str]) -> Tuple[List[str], Optional[str]]:
    """Passing tasks whose recorded trial ran the changed code: trials the replay marks `touched`, and trials whose
    label a kept fix changes."""
    ran = [checks["replay"] for checks in kept if checks["replay"]["ran"]]
    if not ran:
        return [], "no replay ran for the kept candidates"

    def passing(names: set) -> set:
        return {rows_by_trial[t][0] for t in names if t in rows_by_trial and rows_by_trial[t][1]} - set(targets)

    touched = passing({t for replay in ran for t in replay["touched"] or []})
    changed = passing({t for replay in ran for t in replay["changed_trials"]})
    if touched or changed:
        return sorted(touched | changed), None
    given = any(replay["touched"] is not None for replay in ran)
    return [], ("touched marks no passing trial outside the targets" if given
                else "no replay of the kept candidates gives touched") + \
        ", and the kept fixes change the label of no passing trial outside the targets"


def build_on(adapter: Adapter, loop_dir: str, round_dir: str, sha: str, timeout_s: float) -> dict:
    line = adapter.commands["build"]
    if line is None:
        return {"ref": sha, "path": None, "sha256": None, "reason": "commands.build is null: the run installs the checkout"}
    if "build" in adapter.paid:
        path = os.path.join(round_dir, "commands", f"build-{sha[:12]}.sh")
        write_paid_on_ref(path, adapter.checkout, loop_dir, sha, line, {})
        return {"ref": sha, "path": None, "sha256": None,
                "reason": f"commands.build costs money: written to {path}, not started"}
    log = os.path.join(round_dir, "logs", "build.log")
    with worktree(adapter.checkout, loop_dir, sha) as tree:
        ran = run_command(line, {"worktree": tree}, tree, log, timeout_s)
        if ran.code != 0:
            raise LoopError(f"commands.build exited with code {ran.code}; the log is {log}:\n{ran.tail}")
        with open(log, encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh.read().splitlines()[1:] if ln.strip()]
        built = lines[-1] if lines else ""
        inside = os.path.abspath(built).startswith(os.path.abspath(tree) + os.sep)
    if not built or not os.path.isabs(built) or inside or not os.path.exists(built):
        raise LoopError(f"commands.build must print, as its last line, the absolute path of what it built, outside "
                        f"{{worktree}}; it printed {built!r}")
    digest = None
    if os.path.isfile(built):
        with open(built, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    return {"ref": sha, "path": built, "sha256": digest, "reason": None}


def cmd_select(args: argparse.Namespace) -> None:
    adapter = adapter_of(args.loop)
    round_dir = open_round(args.loop)
    record = read_input(round_dir)
    ledger = load_ledger(args.loop)
    entry = ledger_round(ledger, os.path.basename(round_dir))
    kept = kept_candidates(round_dir, entry, adapter)
    ref = resolve(source_of(adapter, "select"), args.ref)
    for checks in kept:
        if not is_ancestor(adapter.checkout, checks["ref"], ref):
            raise LoopError(f"{args.ref} does not contain the kept candidate {checks['id']} ({checks['ref'][:12]}): "
                            "merge the kept candidates into one commit of the work branch and select that")

    rows = read_rows(record["run"]["job_dir"], float(record["pass_threshold"]))
    solved, _ = solved_tasks(rows)
    if any(p.get("effect") not in ("outcome", "report") for p in entry["predictions"]):
        raise LoopError("a prediction of stop 1 names no effect; record the answer of stop 1 again")
    targets = sorted({t for p in entry["predictions"] if p["effect"] == "outcome" for t in p["tasks"]})
    guards, no_guards = guards_of(kept, {r.trial: (r.task, r.passed) for r in rows}, targets)
    pool = sorted(t for t, ok in solved.items() if ok and t not in targets and t not in guards)
    canaries = sorted(random.Random(SEED).sample(pool, min(args.canaries, len(pool))))
    sizes = {"targets": targets, "guards": targets + guards, "canaries": targets + guards + canaries}
    names = {"targets": "targets", "guards": "targets and guards", "canaries": "targets, guards and canaries"}
    threshold = float(record["pass_threshold"])
    on_ref = [r for t in entry.get("trial_runs") or [] if t["ref"] == ref for r in read_rows(t["job_dir"], threshold)]
    sources = [task_costs(on_ref), task_costs(rows)]
    price = {k: price_of(v, TARGET_TRIALS, sources) for k, v in sizes.items()}

    print(f"targets ({len(targets)} tasks of the kept causes): {', '.join(targets)}")
    print(f"guards: {', '.join(guards)}" if guards else f"guards: none ({no_guards})")
    print(f"canaries ({len(canaries)} passing tasks, seed {SEED}): {', '.join(canaries) or 'none'}")
    for k in SIZES:
        print(f"{names[k]}: {price[k].tasks} tasks, {price[k].trials} trials, {describe(price[k])}")
    if args.size is None:
        print("choose a size with --size targets|guards|canaries --budget-usd B --text \"the person's words\"")
        return

    if args.budget_usd is None or not args.text:
        raise LoopError("--size needs --budget-usd and --text: the budget and the person's words")
    left = float(record["budget_usd"]) - trial_spent(entry)
    if args.budget_usd > left:
        raise LoopError(f"the budget of the trial run, {usd(args.budget_usd)}, is over what the round has left, "
                        f"{usd(left)} of {usd(float(record['budget_usd']))}")
    chosen = price[args.size]
    if chosen.usd is not None and chosen.usd > args.budget_usd:
        raise LoopError(f"{names[args.size]} cost {describe(chosen)}, over the budget of {usd(args.budget_usd)}")
    tasks = sizes[args.size]
    units = os.path.join(round_dir, "trial-tasks.txt")
    with open(units, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(t + "\n" for t in run_names(rows, tasks)))
    build = build_on(adapter, args.loop, round_dir, ref, args.command_timeout_s)
    write_json(os.path.join(round_dir, "build.json"), build)
    entry["selection"] = {"ref": ref, "size": args.size, "targets": targets,
                          "guards": guards if args.size != "targets" else [],
                          "canaries": canaries if args.size == "canaries" else [], "repeat": TARGET_TRIALS,
                          "seed": SEED, "price_usd": chosen.usd, "unpriced_tasks": chosen.unpriced_tasks,
                          "budget_usd": args.budget_usd,
                          "guards_reason": no_guards}
    entry["answers"].append({"about": "the trial run: its size and budget", "date": now(), "text": args.text})
    line = adapter.commands["run_tasks"]
    if line is None:
        written = "commands.run_tasks is null: start the run of trial-tasks.txt by other means"
    else:
        values = {"units_file": units, "repeat": str(TARGET_TRIALS), "jobs_dir": os.path.join(round_dir, "jobs"),
                  "budget_usd": str(args.budget_usd), "build": build["path"] or "", "ref": ref}
        written = "start it: " + write_paid(round_dir, "run_tasks", line, values, adapter.checkout)
    save_ledger(args.loop, ledger)
    print(f"wrote {round_dir}: trial-tasks.txt ({len(tasks)} tasks), build.json; {written}")
