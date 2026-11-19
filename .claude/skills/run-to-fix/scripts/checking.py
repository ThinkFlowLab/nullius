"""Stage 3, `loop.py candidate` and `loop.py check`: a candidate's test, the agent's suite and the replay."""
from __future__ import annotations

import argparse
import collections
import csv
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from adapter import Adapter, source_of
from checkout import diff_size, resolve, worktree, write_paid_on_ref
from classifying import read_rows
from reading import adapter_of
from rounds import LoopError, Ran, now, open_round, read_input, read_json, run_command, write_json
from trials import TrialRow

CANDIDATE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
RULE_LINE = re.compile(r"(?m)^Rule:\s*(\S+)\s*$")
TABLE = ("done_pass", "done_fail", "not_done_pass", "not_done_fail")
EFFECTS = ("outcome", "report")


def candidate_id(cid: str) -> str:
    """A candidate id names a folder of the round: lower-case letters, digits, - and _."""
    if not CANDIDATE_ID.match(cid):
        raise LoopError(f"a candidate id is lower-case letters, digits, - and _, got {cid!r}")
    return cid


def cause_trials(round_dir: str, cause: str, effect: str) -> Tuple[str, List[str], List[str]]:
    """The rule a cause names, the trials it counts and their tasks: failing trials for an effect on the outcome,
    every trial the rule matches for an effect on the report."""
    if not CANDIDATE_ID.match(cause):
        raise LoopError(f"a cause is the name of a file in causes/: lower-case letters, digits, - and _, got {cause!r}")
    path = os.path.join(round_dir, "causes", f"{cause}.md")
    if not os.path.isfile(path):
        raise LoopError(f"{path} is missing: write the cause before its candidates")
    with open(path, encoding="utf-8") as fh:
        m = RULE_LINE.search(fh.read())
    if not m:
        raise LoopError(f"{path} needs a line 'Rule: <id>' naming the group of groups.json or the found class that "
                        "counts its trials")
    rule = m.group(1)
    with open(os.path.join(round_dir, "trials.csv"), encoding="utf-8", newline="") as fh:
        table = list(csv.DictReader(fh))
    groups = {g["id"]: g for g in read_json(os.path.join(round_dir, "groups.json"))["groups"]}
    if rule in groups:
        names = set(groups[rule]["trial_names"])
    elif table and f"found:{rule}" in table[0]:
        names = {r["trial"] for r in table if r[f"found:{rule}"] == "1" and (effect == "report" or r["class"] != "pass")}
    else:
        raise LoopError(f"{path} names the rule {rule}, which is neither a group of groups.json nor a found class "
                        "of trials.csv; run loop.py classify after adding a found class")
    if not names:
        raise LoopError(f"the rule {rule} of {path} counts no failing trial" if effect == "outcome"
                        else f"the rule {rule} of {path} matches no trial")
    tasks = sorted({r["task"] for r in table if r["trial"] in names})
    return rule, sorted(names), tasks


def load_candidate(path: str) -> dict:
    """A candidate.json of this version: one that names the effect of its fix."""
    if not os.path.isfile(path):
        raise LoopError(f"{path} is missing; register the candidate with loop.py candidate")
    cand = read_json(path)
    if not isinstance(cand, dict) or cand.get("effect") not in EFFECTS:
        raise LoopError(f"{path} names no effect of its fix; register the candidate again with loop.py candidate "
                        "--effect outcome or --effect report")
    return cand


def cmd_candidate(args: argparse.Namespace) -> None:
    adapter = adapter_of(args.loop)
    round_dir = open_round(args.loop)
    read_input(round_dir)
    candidate_id(args.id)
    if (args.test is None) != (args.test_ref is None):
        raise LoopError("--test and --test-ref go together: the test file and the commit that adds it before the fix")
    if args.test is not None and (os.path.isabs(args.test) or ".." in re.split(r"[\\/]", args.test)):
        raise LoopError(f"--test is a path inside the checkout, got {args.test!r}")
    if args.effect == "report" and adapter.commands["replay"] is None:
        raise LoopError("an effect on the report is seen only in the replay, and commands.replay is null in adapter.json")
    checkout = source_of(adapter, "candidate")
    rule, trials, tasks = cause_trials(round_dir, args.cause, args.effect)
    ref = resolve(checkout, args.ref)
    test_ref = resolve(checkout, args.test_ref) if args.test_ref else None
    record = {"schema": "run-to-fix.candidate/v1", "id": args.id, "created": now(), "cause": args.cause, "rule": rule,
              "effect": args.effect, "ref": ref, "test": args.test, "test_ref": test_ref,
              "prediction": {"trials": trials, "tasks": tasks if args.effect == "outcome" else []}}
    cdir = os.path.join(round_dir, "candidates", args.id)
    stale = os.path.join(cdir, "checks.json")
    if os.path.exists(stale):
        os.remove(stale)
    write_json(os.path.join(cdir, "candidate.json"), record)
    expects = f"{len(tasks)} tasks expected to pass" if args.effect == "outcome" else "no task expected to pass"
    print(f"candidate {args.id}: {ref[:12]} for the cause {args.cause}, effect {args.effect}: {len(trials)} trials "
          f"of {rule}, {expects}")


@dataclass(frozen=True)
class Outcome:
    ran: Optional[Ran]
    reason: Optional[str]


def on_ref(adapter: Adapter, loop_dir: str, round_dir: str, name: str, sha: str, values: Dict[str, str],
           log: str, timeout_s: float) -> Outcome:
    """Run an adapter command in a worktree of `sha`, or say why it did not run."""
    line = adapter.commands[name]
    if line is None:
        return Outcome(None, f"commands.{name} is null: this check did not run")
    if name in adapter.paid:
        path = os.path.join(round_dir, "commands", f"{name}-{sha[:12]}.sh")
        write_paid_on_ref(path, adapter.checkout, loop_dir, sha, line, values)
        return Outcome(None, f"commands.{name} costs money: written to {path}, not started")
    with worktree(adapter.checkout, loop_dir, sha) as tree:
        ran = run_command(line, dict(values, worktree=tree), tree, log, timeout_s)
    if ran.code is None:
        return Outcome(None, f"commands.{name} on {sha[:12]} did not finish in {timeout_s:g} s and was stopped; "
                             f"the log is {ran.log}")
    return Outcome(ran, None)


def ensure_ok(name: str, outcome: Outcome) -> None:
    if outcome.ran is not None and outcome.ran.code != 0:
        raise LoopError(f"commands.{name} exited with code {outcome.ran.code}; the log is {outcome.ran.log}:\n"
                        f"{outcome.ran.tail}")


def check_test(adapter: Adapter, args: argparse.Namespace, round_dir: str, cdir: str, cand: dict) -> dict:
    if cand["test"] is None:
        return {"ran": False, "reason": "the candidate names no test"}
    out = {}
    for when, sha in (("before", cand["test_ref"]), ("after", cand["ref"])):
        o = on_ref(adapter, args.loop, round_dir, "test_one", sha, {"test": cand["test"]},
                   os.path.join(cdir, f"test-{when}.log"), args.command_timeout_s)
        if o.ran is None:
            return {"ran": False, "reason": o.reason}
        out[when] = {"ref": sha, "code": o.ran.code}
    return {"ran": True, "reason": None, "test": cand["test"], **out,
            "fails_before": out["before"]["code"] != 0, "passes_after": out["after"]["code"] == 0}


def suite_failures(adapter: Adapter, name: str, outcome: Outcome) -> List[str]:
    assert outcome.ran is not None and adapter.test_failed_pattern is not None
    with open(outcome.ran.log, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()[1:]
    found = sorted({m.group(1) for m in (adapter.test_failed_pattern.search(ln) for ln in lines) if m})
    if outcome.ran.code != 0 and not found:
        raise LoopError(f"commands.test on {name} exited with code {outcome.ran.code} and printed no failed test that "
                        f"test_failed_pattern takes: the suite is broken. The log is {outcome.ran.log}:\n{outcome.ran.tail}")
    return found


def check_suite(adapter: Adapter, args: argparse.Namespace, round_dir: str, cdir: str, base: str, ref: str) -> dict:
    cache = os.path.join(round_dir, "base", "suite.json")
    stored = read_json(cache) if os.path.isfile(cache) else {}
    if stored.get("ref") == base and stored.get("adapter_sha256") == adapter.sha256:
        base_failures = stored["failures"]
    else:
        o = on_ref(adapter, args.loop, round_dir, "test", base, {}, os.path.join(round_dir, "base", "suite.log"),
                   args.command_timeout_s)
        if o.ran is None:
            return {"ran": False, "reason": o.reason}
        base_failures = suite_failures(adapter, "base_ref", o)
        write_json(cache, {"ref": base, "adapter_sha256": adapter.sha256, "failures": base_failures})
    o = on_ref(adapter, args.loop, round_dir, "test", ref, {}, os.path.join(cdir, "suite.log"), args.command_timeout_s)
    if o.ran is None:
        return {"ran": False, "reason": o.reason}
    failures = suite_failures(adapter, ref[:12], o)
    return {"ran": True, "reason": None, "base_ref": base, "base_failures": base_failures, "failures": failures,
            "added": sorted(set(failures) - set(base_failures)), "removed": sorted(set(base_failures) - set(failures))}


def replay_labels(path: str, rows: List[TrialRow]) -> dict:
    data = read_json(path)
    trials = data.get("trials") if isinstance(data, dict) else None
    if not isinstance(trials, dict):
        raise LoopError(f"{path} must be {{\"ref\": ..., \"trials\": {{\"<trial_name>\": {{\"label\": ...}}}}}}")
    missing = sorted(r.trial for r in rows if not isinstance(trials.get(r.trial), dict)
                     or not isinstance(trials[r.trial].get("label"), str))
    if missing:
        raise LoopError(f"{path} has no label for {len(missing)} recorded trials: {', '.join(missing[:10])}")
    return data


def cross(data: dict, rows: List[TrialRow], success: List[str]) -> Dict[str, int]:
    table = dict.fromkeys(TABLE, 0)
    for r in rows:
        done = data["trials"][r.trial]["label"] in success
        table[("done_" if done else "not_done_") + ("pass" if r.passed else "fail")] += 1
    return table


def false_successes(data: dict, rows: List[TrialRow], success: List[str]) -> List[str]:
    return sorted(r.trial for r in rows if not r.passed and data["trials"][r.trial]["label"] in success)


def replay_file(round_dir: str, sha: str, against: str) -> str:
    return os.path.join(round_dir, "replays", f"{sha[:12]}-against-{against[:12]}.json")


def label_changes(before: dict, after: dict, names: List[str]) -> dict:
    """How many of `names` change their label between the two replays, by direction."""
    by = collections.Counter(f"{before['trials'][n]['label']} → {after['trials'][n]['label']}" for n in names
                             if before["trials"][n]["label"] != after["trials"][n]["label"])
    return {"trials": len(names), "changed": sum(by.values()), "by_direction": dict(sorted(by.items()))}


def check_replay(adapter: Adapter, args: argparse.Namespace, round_dir: str, before_ref: str, ref: str, base: str,
                 job_dir: str, rows: List[TrialRow], cause: List[str]) -> dict:
    """The replays at the ref before the fix and at the fix, each once per round; the second is given the first as
    {before_ref}, so `touched` is the code that changed between the two."""
    labels = {}
    for when, sha, against in (("before", before_ref, before_ref), ("after", ref, before_ref)):
        out = replay_file(round_dir, sha, against)
        if os.path.isfile(out):
            stored = read_json(out)
            if (stored.get("_loop_ref") == sha and stored.get("_loop_before_ref") == against
                    and stored.get("_loop_adapter_sha256") == adapter.sha256):
                labels[when] = replay_labels(out, rows)
                continue
        values = {"ref": sha, "before_ref": against, "base_ref": base, "job_dir": job_dir, "out_json": out}
        os.makedirs(os.path.dirname(out), exist_ok=True)
        o = on_ref(adapter, args.loop, round_dir, "replay", sha, values, out[:-len(".json")] + ".log",
                   args.command_timeout_s)
        if o.ran is None:
            if when == "before" and "replay" in adapter.paid:
                on_ref(adapter, args.loop, round_dir, "replay", ref,
                       dict(values, ref=ref, out_json=replay_file(round_dir, ref, before_ref)),
                       replay_file(round_dir, ref, before_ref)[:-len(".json")] + ".log", args.command_timeout_s)
            return {"ran": False, "reason": o.reason}
        ensure_ok("replay", o)
        data = replay_labels(out, rows)
        write_json(out, dict(data, _loop_ref=sha, _loop_before_ref=against, _loop_adapter_sha256=adapter.sha256))
        labels[when] = data
    success = adapter.replay_success_labels
    before, after = false_successes(labels["before"], rows, success), false_successes(labels["after"], rows, success)
    touched_given = any("touched" in v for v in labels["after"]["trials"].values())
    names = [r.trial for r in rows]
    return {"ran": True, "reason": None, "before_ref": before_ref, "ref": ref,
            "counts": "recorded trials: done and passed / done and failed / not done and passed / not done and failed",
            "before": cross(labels["before"], rows, success), "after": cross(labels["after"], rows, success),
            "false_success_before": len(before), "false_success_after": len(after),
            "false_success_added": len(after) - len(before), "false_success_new": sorted(set(after) - set(before)),
            "kept_recorded": {w: len(labels[w].get("kept_recorded") or []) for w in ("before", "after")},
            "touched": sorted(n for n, v in labels["after"]["trials"].items() if v.get("touched") is True)
            if touched_given else None,
            "changed_trials": sorted(n for n in names
                                     if labels["before"]["trials"][n]["label"] != labels["after"]["trials"][n]["label"]),
            "cause": label_changes(labels["before"], labels["after"], cause)}


def cmd_check(args: argparse.Namespace) -> None:
    adapter = adapter_of(args.loop)
    round_dir = open_round(args.loop)
    record = read_input(round_dir)
    source_of(adapter, "check")
    cdir = os.path.join(round_dir, "candidates", candidate_id(args.candidate))
    cand = load_candidate(os.path.join(cdir, "candidate.json"))
    # a check that fails part way must not leave the success of an earlier check to be read
    stale = os.path.join(cdir, "checks.json")
    if os.path.exists(stale):
        os.remove(stale)
    job_dir = record["run"]["job_dir"]
    rows = read_rows(job_dir, float(record["pass_threshold"]))
    base = resolve(adapter.checkout, adapter.base_ref)
    before_ref = cand["test_ref"] or base
    checks = {
        "schema": "run-to-fix.checks/v1", "candidate": cand["id"], "created": now(), "ref": cand["ref"],
        "test_ref": cand["test_ref"], "base_ref": base,
        "test": check_test(adapter, args, round_dir, cdir, cand),
        "suite": check_suite(adapter, args, round_dir, cdir, base, cand["ref"]),
        "replay": check_replay(adapter, args, round_dir, before_ref, cand["ref"], base, job_dir, rows,
                               cand["prediction"]["trials"]),
        "diff": diff_size(adapter.checkout, before_ref, cand["ref"]),
    }
    write_json(os.path.join(cdir, "checks.json"), checks)
    print(f"wrote {cdir}/checks.json")
    for name in ("test", "suite", "replay"):
        part = checks[name]
        print(f"  {name}: " + ("ran" if part["ran"] else f"did not run: {part['reason']}"))
