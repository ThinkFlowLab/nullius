"""Catalogue and profile inputs shared by the Leaderboard page and command."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from urllib.parse import quote

from nullius.standard.contract import definitions
from nullius import charts
from . import board, configuration, metrics, weights, scopes


def catalogue():
    return definitions.load("agent", every=True)[0], definitions.load("benchmark", every=True)[0]


def revision():
    try:
        raw = configuration.location().read_bytes()
    except FileNotFoundError:
        raw = b""
    return hashlib.sha256(raw).hexdigest()


def options(benchmarks):
    return [{"key": b["key"], "label": b["label"], "version": definitions.version_identity(b),
             "version_label": (definitions.version_label(b) or definitions.version_identity(b)[:7]), "metrics": ["Score", "pass^3"] if b.get("score", {}).get("kind") == "pass" else ["Score"],
             "datasets": scopes.choices(b)}
            for b in benchmarks]


def document(jobs, *, workspace="", workspace_open=False, all_scores=False, deferred=False):
    agents, benchmarks = catalogue()
    settings, warning = configuration.load(configuration.location(), benchmarks)
    sources = {}
    result = board.build(jobs, agents, benchmarks, settings, all_scores=all_scores, workspace_open=workspace_open, metric_sources=sources)
    context = {"workspace": workspace, "settings": settings, "all_scores": all_scores,
               "jobs": [(j.job_id, j.uid, str(j.finished_at), len(j.trials), j.identity, j.dir) for j in jobs]}
    result["context"] = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    result["warning"], result["revision"] = warning, revision()
    result["versions"] = [{"benchmark": b["key"], "version": definitions.version_identity(b),
                            "label": b["label"] + " · " + ((definitions.version_label(b) or definitions.version_identity(b)[:7]) or "Official")}
                           for b in benchmarks if any(e.get("benchmark") == b["key"] for a in settings["axes"].values() for e in a["entries"])]
    for row in result["rows"]:
        for axis in row["axes"].values():
            for entry in axis["entries"]:
                entry["links"] = [{"id": id, "url": "/job/" + quote(id, safe="") + ("?workspace=" + quote(workspace, safe="") if workspace else "")} for id in entry["runs"]]
    if deferred:
        metrics.CACHE.complete(result, sources)
    result["scatter"] = {}
    for field, label in (("time", "Time (seconds)"), ("cost", "Cost (USD)")):
        x = charts.Metric(label, tuple(charts.Value(r["name"], r["metrics"][field]["value"], str(r["metrics"][field]["value"]),
                                                  note=r["version_label"], tag=r["name"], key=r["key"]) for r in result["rows"]), bounds=(0, None))
        y = charts.Metric("Overall", tuple(charts.Value(r["name"], r["overall"], weights.percent(r["overall"])) for r in result["rows"]),
                          fmt=lambda v, step, top: weights.percent(v), bounds=(0, 1))
        result["scatter"][field] = asdict(charts.scatter(x, y, tiles=True))
    return result
