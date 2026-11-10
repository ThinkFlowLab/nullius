"""Shard jobs of one launch put back together as one job.

A launch run over several machines (``shard``) leaves one job per shard. The
pages would read each as a run of its own, so ``cli merge`` makes the one
job a reader expects: every shard's trials in it, under the names they had
(a trial's tag is its unit's place in the whole dataset, so shards never
share one), its ``result.json`` recomputed from them - the trials, the
verdicts, the earliest start and the latest end - and its ``config.json``
the shards' with ``shard`` taken out and ``merged_from`` put in.

What is merged must be one launch: the same launch id, benchmark and
version, dataset, agent or capability and model, and the same number of shards
- else it is refused, naming what differs, unless ``force``. Two trials of
one unit are always refused, with the units named: one of them would be
counted twice. Units no shard ran are not a refusal - a shard may be
missing, or stopped early - and are said: the shards absent, and the units a
shard planned and has no trial for.

Files are hard-linked into the merged job where the file system allows (a
copy where it does not), so a merge of a thousand trials costs no second
copy of their workspaces; each trial's ``result.json`` is a copy of its own,
rewritten to name the merged job. The shards are left as they were.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Dict, List, Optional

from nullius.standard import job


# What every shard of one launch shares, as (what a refusal calls it, where it is in config.json).
SAME = (("launch", ("standard", "launch")), ("benchmark", ("standard", "benchmark")),
        ("benchmark version", ("standard", "benchmark_version")), ("dataset", ("datasets", 0, "name")),
        ("agent or capability", ("standard", "plugged")), ("model", ("agents", 0, "model_name")),
        ("backend", ("standard", "backend")), ("number of shards", ("standard", "shard", "of")))


class Refused(Exception):
    """The jobs cannot be merged as asked; the message says why."""


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _at(d, path):
    for k in path:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int) and k < len(d):
            d = d[k]
        else:
            return None
    return d


def _trials(job_dir: str) -> List[str]:
    return sorted(n for n in os.listdir(job_dir)
                  if not n.startswith(".") and os.path.isfile(os.path.join(job_dir, n, "result.json")))


def _link_tree(src: str, dest: str) -> None:
    """``src`` into ``dest``, each file hard-linked (copied across devices)."""
    for d, dirs, files in os.walk(src):
        rel = os.path.relpath(d, src)
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for f in files:
            a, b = os.path.join(d, f), os.path.join(dest, rel, f)
            if os.path.islink(a):
                os.symlink(os.readlink(a), b)
                continue
            try:
                os.link(a, b)
            except OSError:
                shutil.copy2(a, b)


def check(job_dirs: List[str], force: bool = False) -> dict:
    """What merging ``job_dirs`` would make, without writing: the shards read,
    the trials by unit, the notes; ``Refused`` when they cannot be merged."""
    if not job_dirs:
        raise Refused("no jobs to merge")
    shards = []
    for d in job_dirs:
        d = os.path.abspath(d.rstrip("/"))
        cfg, res = _read(os.path.join(d, "config.json")), _read(os.path.join(d, "result.json"))
        if cfg.get("runner") != "standard":
            raise Refused(f"{d}: not a job of the Nullius standard (no config.json naming it)")
        sh = _at(cfg, ("standard", "shard"))
        if not isinstance(sh, dict) and not force:
            raise Refused(f"{d}: not a shard (its config has no standard.shard); --force merges it anyway")
        shards.append({"dir": d, "name": os.path.basename(d), "config": cfg, "result": res, "shard": sh or {}})
    if len({s["dir"] for s in shards}) != len(shards):
        raise Refused("a job is named twice")
    differ = []
    for label, path in SAME:
        seen = {json.dumps(_at(s["config"], path)) for s in shards}
        if len(seen) > 1:
            differ.append(f"{label}: " + ", ".join(f"{s['name']} {_at(s['config'], path)!r}" for s in shards))
    if differ and not force:
        raise Refused("the shards are not of one launch - " + "; ".join(differ) + " (--force merges them anyway)")
    # every trial by its unit: two of one unit are never merged, force or not
    by_unit: Dict[str, List[tuple]] = {}
    names: Dict[str, List[str]] = {}
    for s in shards:
        s["trials"] = _trials(s["dir"])
        for t in s["trials"]:
            unit = _at(_read(os.path.join(s["dir"], t, "result.json")), ("standard", "unit")) or t
            by_unit.setdefault(str(unit), []).append((s["name"], t))
            names.setdefault(t, []).append(s["name"])
    twice = {u: w for u, w in by_unit.items() if len(w) > 1}
    if twice:
        shown = "; ".join(f"{u} in {', '.join(f'{j}/{t}' for j, t in w)}" for u, w in sorted(twice.items())[:10])
        raise Refused(f"{len(twice)} unit(s) have a trial in more than one shard: {shown}"
                      + (" and more" if len(twice) > 10 else ""))
    clash = {t: w for t, w in names.items() if len(w) > 1}
    if clash:
        raise Refused("trials of the same name in several shards: " + ", ".join(sorted(clash)[:10]))
    notes, missing = [], []
    of = {s["shard"].get("of") for s in shards if s["shard"]}
    totals = {s["shard"].get("units_total") for s in shards if s["shard"]}
    if len(of) == 1:
        n = of.pop()
        have = sorted(s["shard"].get("index") for s in shards if s["shard"])
        absent = [i for i in range(1, (n or 0) + 1) if i not in have]
        if absent:
            notes.append(f"shard{'s' if len(absent) > 1 else ''} {', '.join(f'{i}/{n}' for i in absent)} not in the merge")
        dup = sorted({i for i in have if have.count(i) > 1})
        if dup:
            notes.append(f"shard{'s' if len(dup) > 1 else ''} {', '.join(f'{i}/{n}' for i in dup)} given more than once (their units differ)")
    for s in shards:
        planned = s["shard"].get("planned")
        if isinstance(planned, list):
            ran = {u for u, w in by_unit.items() if any(j == s["name"] for j, _ in w)}
            left = [u for u in planned if u not in ran]
            if left:
                missing += left
                notes.append(f"{s['name']}: {len(left)} planned unit(s) with no trial: {', '.join(left[:5])}" + (" ..." if len(left) > 5 else ""))
        if s["shard"].get("limit"):
            notes.append(f"{s['name']}: limited to its first {s['shard']['limit']} units")
    total = totals.pop() if len(totals) == 1 else None
    if isinstance(total, int):
        short = total - len(by_unit)
        if short > 0:
            notes.append(f"{short} of the dataset's {total} units are not in the merge")
    return {"shards": shards, "by_unit": by_unit, "notes": notes, "missing": missing, "units_total": total, "differ": differ}


def _budget(budgets: list) -> Optional[dict]:
    """The shards' spending caps as one (``run.Run.budget``): the caps and what was spent added up, stopped
    when any shard was, their units cut off and not run together. None unless every shard had a cap: a cap
    over part of a launch is not the launch's."""
    if not budgets or not all(isinstance(x, dict) and isinstance(x.get("max_usd"), (int, float)) for x in budgets):
        return None
    spent = [x.get("spent_usd") for x in budgets]
    unsettled = round(sum(x.get("unsettled_usd") or 0 for x in budgets), 6)
    return {"max_usd": round(sum(x["max_usd"] for x in budgets), 6),
            "spent_usd": None if any(not isinstance(v, (int, float)) for v in spent) else round(sum(spent), 6),
            **({"unsettled_usd": unsettled} if unsettled else {}), "stopped": any(x.get("stopped") for x in budgets),
            "cut_off": [u for x in budgets for u in x.get("cut_off") or []],
            "not_run": [u for x in budgets for u in x.get("not_run") or []]}


def merge(job_dirs: List[str], out: str, force: bool = False) -> dict:
    """Merge the shard jobs ``job_dirs`` into the new job ``out``.
    {out, trials, merged_from, notes, missing}."""
    got = check(job_dirs, force)
    out = os.path.abspath(out.rstrip("/"))
    if os.path.exists(out) and os.listdir(out):
        raise Refused(f"{out} exists already and is not empty")
    shards = got["shards"]
    name = os.path.basename(out)
    os.makedirs(out, exist_ok=True)
    results = []
    for s in shards:
        for t in s["trials"]:
            src, dest = os.path.join(s["dir"], t), os.path.join(out, t)
            _link_tree(src, dest)
            # the trial's record is its own copy, naming the merged job and the shard it came from
            res = _read(os.path.join(src, "result.json"))
            std = dict(res.get("standard") or {}, job=name, shard_job=s["name"])
            res["standard"] = std
            os.unlink(os.path.join(dest, "result.json"))
            with open(os.path.join(dest, "result.json"), "w", encoding="utf-8") as f:
                json.dump(res, f, indent=1)
            results.append(res)
    planned = sum(int(s["result"].get("n_total_trials") or len(s["trials"])) for s in shards)
    starts = [s["result"].get("started_at") for s in shards if s["result"].get("started_at")]
    ends = [s["result"].get("finished_at") for s in shards if s["result"].get("finished_at")]
    errored = sum(1 for r in results if r.get("exception_info"))
    rewards = [(r.get("verifier_result") or {}).get("rewards") or {} for r in results]
    scores = [w["score"] for w in rewards if "score" in w]
    budget = _budget([s["result"].get("budget") for s in shards])
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as f:
        # ISO stamps in UTC sort as their instants
        json.dump({"id": name, "started_at": min(starts) if starts else None, "finished_at": max(ends) if ends else None,
                   "n_total_trials": planned,
                   "stats": {"n_completed_trials": len(results) - errored, "n_errored_trials": errored,
                             "n_cancelled_trials": max(0, planned - len(results)), "n_running_trials": 0, "n_pending_trials": 0},
                   "standard": {"passed": sum(1 for w in rewards if w.get("reward") == 1.0),
                                "scored": len(scores), "mean_score": job.mean_score(results)},
                   **({"budget": budget} if budget else {})}, f, indent=1)
    cfg = json.loads(json.dumps(shards[0]["config"]))
    cfg["job_name"] = name
    std = cfg.setdefault("standard", {})
    std.pop("shard", None)
    std["merged_from"] = [{"job": s["name"], "trials": len(s["trials"]),
                           **({"shard": f"{s['shard'].get('index')}/{s['shard'].get('of')}"} if s["shard"] else {})} for s in shards]
    if got["units_total"] is not None:
        std["units_total"] = got["units_total"]
    if got["differ"]:
        std["merged_despite"] = got["differ"]
    if cfg.get("datasets"):
        cfg["datasets"][0]["n_tasks"] = planned
    with open(os.path.join(out, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return {"out": out, "trials": len(results), "merged_from": [s["name"] for s in shards], "notes": got["notes"],
            "missing": got["missing"]}
