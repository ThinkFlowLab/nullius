"""Catalogue and profile inputs shared by the Leaderboard page and command."""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import replace
from urllib.parse import quote

from nullius.standard.contract import definitions
from . import board, configuration, metrics, weights, scopes


def catalogue():
    return definitions.load("agent", every=True)[0], definitions.load("benchmark", every=True)[0]


def revision():
    try:
        raw = configuration.location().read_bytes()
    except OSError:                                                    # missing, unreadable, or not a file
        raw = b""
    return hashlib.sha256(raw).hexdigest()


def qualified(jobs):
    """The jobs of several roots, those that share a name told apart by the folder that holds each."""
    names = Counter(j.job_id for j in jobs)
    def under(job, name):
        return os.path.join(os.path.dirname(os.path.abspath(job.dir)), name) if names[name] > 1 else name
    return [replace(j, job_id=under(j, j.job_id), merged_from=[under(j, s) for s in j.merged_from]) for j in jobs]


def options(benchmarks):
    return [{"key": b["key"], "label": b["label"], "version": definitions.version_identity(b),
             "version_label": (definitions.version_label(b) or definitions.version_identity(b)[:7]), "metrics": ["Score", "pass^3"] if b.get("score", {}).get("kind") == "pass" else ["Score"],
             "datasets": scopes.choices(b), "default": b.get("_version") == b.get("_default")}
            for b in benchmarks]


def document(jobs, *, workspace="", workspace_open=False, deferred=False):
    agents, benchmarks = catalogue()
    settings, warning = configuration.load(configuration.location(), benchmarks)
    sources = {}
    result = board.build(jobs, agents, benchmarks, settings, workspace_open=workspace_open, metric_sources=sources)
    context = {"workspace": workspace, "settings": settings,
               "jobs": [(j.job_id, j.uid, str(j.finished_at), len(j.trials), j.identity, j.dir) for j in jobs]}
    result["context"] = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    result["warning"], result["revision"] = warning, revision()
    # a version to compare with is one that changes something: another than the one every entry of its benchmark reads
    mapped = {}
    for axis in settings["axes"].values():
        for entry in axis["entries"]:
            mapped.setdefault(entry.get("benchmark"), set()).add(entry.get("version"))
    result["versions"] = [{"benchmark": b["key"], "version": definitions.version_identity(b),
                            "label": b["label"] + " · " + ((definitions.version_label(b) or definitions.version_identity(b)[:7]) or "Official")}
                           for b in benchmarks if mapped.get(b["key"], set()) - {definitions.version_identity(b)}]
    labels = {(b["key"], definitions.version_identity(b)): definitions.version_label(b) or definitions.version_identity(b)[:7]
              for b in benchmarks}
    for row in result["rows"]:
        for axis in row["axes"].values():
            for entry in axis["entries"]:
                # a reader names a version by its release or short commit, never the whole pin
                entry["version_label"] = labels.get((entry["benchmark"], entry["version"]), entry["version"][:7])
                # a run told apart by its folder (qualified) is still opened by its name
                entry["links"] = [{"id": id, "url": "/job/" + quote(os.path.basename(id) if os.path.isabs(id) else id, safe="")
                                   + ("?workspace=" + quote(workspace, safe="") if workspace else "")} for id in entry["runs"]]
    if deferred:
        metrics.CACHE.complete(result, sources)
    return result
