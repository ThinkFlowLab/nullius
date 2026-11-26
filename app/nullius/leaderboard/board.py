"""One workspace's composite scores, evidence, coverage and rank requirements."""
from __future__ import annotations

import hashlib
import json
import copy
from math import isfinite
from nullius.standard.contract import definitions

from . import configuration, scoring, weights, scopes


def project(jobs, agents, benchmarks, settings, benchmark, version, *, all_scores=False):
    """A comparison projection: missing changed evidence invalidates its affected axis."""
    draft = copy.deepcopy(settings)
    affected = set()
    for name, axis in draft["axes"].items():
        for entry in axis["entries"]:
            if entry.get("benchmark") == benchmark:
                entry["version"] = version
                if entry["weight"] > 0:
                    affected.add(name)
    result = build(jobs, agents, benchmarks, draft, all_scores=all_scores)
    for row in result["rows"]:
        for name in affected:
            axis = row["axes"][name]
            if any(e.get("benchmark") == benchmark and e["weight"] > 0 and e["score"] is None for e in axis["entries"]):
                axis["score"] = None
                axis["share"] = 0
                if axis["weight"] > 0:
                    row["overall"] = None
        row["rank"] = None
    return result


def row_identity(job):
    identity = job.identity
    # a run names its agent as the user chose it: "codex", or "codex@2.2.2" for a version of it
    agent, version = definitions.split(identity.get("agent", job.agent_name))
    # which tasks, how many at a time, the whole run's spending cap and retries after an infrastructure
    # failure change coverage, never how a task is done
    limits = {k: v for k, v in identity.get("limits", {}).items()
              if k not in ("repeat", "n_tasks", "task_names", "sample", "seed", "concurrency", "max_usd", "retries")}
    return {"agent": agent, "version": version or identity.get("agent_version", job.agent_version),
            "model": identity.get("model", job.model_name), "effort": job.reasoning_effort,
            "provider": identity.get("provider"), "options": identity.get("agent_options"),
            "backend": identity.get("backend") or job.launch.get("environment"), "limits": limits}


def key_for(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _empty_metrics():
    return {name: {"value": None, "count": 0} for name in ("time", "cost", "steps", "calls")}


def _entry_key(entry):
    return tuple(entry.get(k, "") for k in ("benchmark", "dataset", "version", "metric"))


def build(jobs, agents, benchmarks, settings, *, all_scores=False, workspace_open=True, metric_sources=None):
    """Pure board projection. Inputs belong to a single workspace and profile."""
    config = configuration.validate(settings, benchmarks, allow_unavailable=True)
    agents = [a for a in agents if not a.get("example") and a["key"] not in ("nop", "oracle")]
    agents_by = {a["key"]: a for a in agents}
    benchmarks_by = {(b["key"], definitions.version_identity(b)): b for b in benchmarks}
    customs = {c["id"]: c for c in config["custom"]}
    grouped, identities = {}, {}
    for job in jobs:
        identity = row_identity(job)
        if not scoring.eligible(job) or identity["agent"] not in agents_by:
            continue
        key = key_for(identity)
        grouped.setdefault(key, []).append(job)
        identities[key] = identity
    measured_agents = {identity["agent"] for identity in identities.values()}
    for agent in agents:
        if agent.get("_version", "") != agent.get("_default", agent.get("_version", "")):
            continue
        if agent["key"] in measured_agents:
            continue
        identity = {"agent": agent["key"], "version": agent.get("_version", ""), "catalogue_only": True}
        key = key_for(identity)
        grouped[key], identities[key] = [], identity
    rows = []
    for key, runs in grouped.items():
        identity = identities[key]
        agent = agents_by[identity["agent"]]
        row = {"key": key, "agent": identity["agent"], "name": agent["label"], "company": agent.get("company", ""),
               "version": identity["version"], "identity": identity, "has_runs": bool(runs), "axes": {},
               "metrics": _empty_metrics(), "reasons": [], "custom_names": []}
        matching = next((a for a in agents if a["key"] == identity["agent"] and a.get("_version", "") == identity["version"]), None)
        row["version_label"] = definitions.version_label(matching) if matching else identity["version"]
        trials = {}
        for name in configuration.AXES:
            axis = config["axes"][name]
            entries = []
            for entry in axis["entries"]:
                evidence = dict(entry, score=None, share=0, tasks=None, covered=0, runs=[], reason="not run", unverified=False, available=True)
                if "custom" in entry:
                    custom = customs[entry["custom"]]
                    value = custom["scores"].get(key)
                    evidence.update(name=custom["name"], score=value / 100 if all_scores and value is not None else None,
                                    reason="not verified" if all_scores and value is not None else "not run",
                                    unverified=all_scores and value is not None and entry["weight"] > 0)
                else:
                    bench = benchmarks_by.get((entry["benchmark"], entry["version"]))
                    part = next((c["label"] for c in scopes.choices(bench) if c["id"] == entry["dataset"]), entry["dataset"]) if bench else entry["dataset"]
                    # one benchmark feeds an axis through several datasets and metrics: each names its own
                    label = bench["label"] if bench else entry["benchmark"]
                    evidence["name"] = " · ".join(filter(None, (label, part if part != label else "",
                                                                 entry["metric"] if entry["metric"] != "Score" else "")))
                    evidence["available"] = bench is not None
                    if bench is None:
                        evidence["reason"] = "Benchmark version unavailable"
                    else:
                        manifests, normalized, dataset, sizes = scopes.select(bench, entry["dataset"], runs)
                        got = scoring.contribution(normalized, benchmark=entry["benchmark"], version=entry["version"], datasets=manifests,
                                                   dataset=dataset, sizes=sizes, metric=entry["metric"], graded=bench.get("score", {}).get("kind") == "score")
                        evidence.update(score=got.score, tasks=got.total, covered=got.covered, excluded=got.excluded,
                                        runs=list(got.run_ids), reason=got.reason, note=got.note)
                        if entry["weight"] > 0 and got.score is not None:
                            for run_id, _, attempts in got.tasks:
                                for trial in attempts:
                                    if not trial.is_infra:
                                        trials[(run_id, trial.trial_name)] = trial
                entries.append(evidence)
            score, shares = weights.combine((e["score"], e["weight"]) for e in entries)
            for evidence, share in zip(entries, shares):
                evidence["share"] = share
                if evidence["unverified"] and evidence["name"] not in row["custom_names"]:
                    row["custom_names"].append(evidence["name"])
            row["axes"][name] = {"score": score, "weight": axis["weight"], "share": 0, "entries": entries,
                                  "unverified": any(e["unverified"] and e["share"] > 0 for e in entries)}
        row["overall"], shares = weights.combine((a["score"], a["weight"]) for a in row["axes"].values())
        for axis, share in zip(row["axes"].values(), shares):
            axis["share"] = share
        row["unverified"] = any(a["unverified"] and a["share"] > 0 for a in row["axes"].values())
        for metric, field in (("time", "dur_agent"), ("cost", "cost_usd")):
            values = [getattr(t, field) for t in trials.values() if getattr(t, field) is not None and isfinite(getattr(t, field)) and getattr(t, field) >= 0]
            row["metrics"][metric] = {"value": sum(values) / len(values) if values else None, "count": len(values)}
        rows.append(row)
        if metric_sources is not None:
            metric_sources[key] = tuple(dict.fromkeys(t.trajectory_path for t in trials.values()))
    required = {name: {_entry_key(e) for row in rows for e in row["axes"][name]["entries"]
                       if "custom" not in e and e["available"] and e["weight"] > 0 and e["runs"]}
                for name in configuration.AXES if config["axes"][name]["weight"] > 0}
    for row in rows:
        for name, entries in required.items():
            for entry in row["axes"][name]["entries"]:
                if _entry_key(entry) not in entries or "custom" in entry:
                    continue
                reason = (f"{entry['name']}: {entry['reason']}" if entry["score"] is None and entry["runs"] else
                          f"{entry['name']} not run" if entry["score"] is None else
                          f"{entry['name']} full task coverage unavailable" if entry["tasks"] is None else
                          f"{entry['name']} {entry['covered']} of {entry['tasks']} tasks" if entry["covered"] < entry["tasks"] else "")
                if reason and reason not in row["reasons"]:
                    row["reasons"].append(reason)
        for family, axes in (("capabilities", configuration.CAPABILITIES), ("domains", configuration.DOMAINS)):
            needed = [a for a in axes if required.get(a)]
            present = sum(row["axes"][a]["score"] is not None for a in needed)
            if present < len(needed):
                row["reasons"].append(f"{present} of {len(needed)} {family}")
    rows = weights.rank(rows)
    averages = {}
    for name in configuration.AXES:
        values = [r["axes"][name]["score"] for r in rows if r["axes"][name]["score"] is not None]
        averages[name] = {"score": sum(values) / len(values) if values else None, "count": len(values)}
    return {"rows": rows, "axes": list(configuration.AXES), "domains": list(configuration.DOMAINS),
            "capabilities": list(configuration.CAPABILITIES), "averages": averages,
            "selected": rows[0]["key"] if rows else None, "all_scores": all_scores,
            "empty": "No workspace open" if not workspace_open else "No scored runs in this workspace" if not any(r["has_runs"] for r in rows) else ""}
