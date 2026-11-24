"""A job's identity: one UUID that stays the same wherever its folder is.

A job folder is copied, moved, mirrored from a host and opened in several
workspaces; its name is not unique either, since two roots can each hold a
job of one name. What Nullius records about a job (a judgement) is kept
under this identity, so it follows the job and not the folder.

- **declared**: the producer wrote one. A Nullius standard job carries
  ``standard.uuid`` in its ``config.json``, made at creation, kept by a run
  resumed into the same folder and new for a merge.
- **derived**: none was written (Harbor, imports, standard jobs from before
  ``standard.uuid``). A ``uuid5`` over fixed facts read from the files: the
  format, the folder name, what ran and when it started. No path is a fact,
  so a copy or a move keeps it; a renamed folder is a new job.
- **pending**: a job with no trial and no start yet (a Harbor job
  preparing): nothing fixed to derive from.

Old files are never rewritten to gain an id: a derived id is a pure function
of the files. The facts carry ``"v": 1``; a change of their canonical form
bumps it and keeps this function as the reader of ``v`` 1.
"""
from __future__ import annotations

import json
import os
import uuid

#: fixed for good: changing one changes every derived identity
NS_JOB = uuid.UUID("6f0f3a52-5c1e-4d39-9a2b-6a4c1f0e7d21")

FEED = "events.jsonl"


def _start(job_dir: str, res: dict):
    """When the job started, earliest source first so a later one never
    precedes it: the feed's ``job.start`` (written as the job begins), else
    ``result.json``'s ``started_at`` (a standard job writes it at the end),
    else None."""
    try:
        with open(os.path.join(job_dir, FEED), encoding="utf-8") as fh:
            for _, line in zip(range(32), fh):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("event") == "job.start" and ev.get("ts"):
                    return str(ev["ts"])
    except OSError:
        pass
    return str(res["started_at"]) if res.get("started_at") else None


def _declared(cfg: dict):
    std = cfg.get("standard") if isinstance(cfg.get("standard"), dict) else {}
    try:
        return str(uuid.UUID(str(std["uuid"])))
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _has_trial(job_dir: str) -> bool:
    try:
        return any(e.is_dir() and os.path.isfile(os.path.join(e.path, "result.json")) for e in os.scandir(job_dir))
    except OSError:
        return False


def facts(job_dir: str, cfg: dict, res: dict):
    """The fixed facts a derived identity is made of, as strings, or None for
    a job with no trial and no start."""
    start = _start(job_dir, res)
    std = cfg.get("standard") if isinstance(cfg.get("standard"), dict) else {}
    agents = [a for a in cfg.get("agents") or [] if isinstance(a, dict)]
    if isinstance(cfg.get("harness"), str):          # an import: its folder names its source
        return {"v": 1, "format": cfg["harness"], "folder": os.path.basename(job_dir)}
    if start is None and not _has_trial(job_dir):
        return None
    if std:
        return {"v": 1, "format": "standard", "folder": os.path.basename(job_dir),
                "benchmark": str(std.get("benchmark") or ""), "plugged": str(std.get("plugged") or ""),
                "model": str((agents[0] if agents else {}).get("model_name") or ""),
                "launch": str(std.get("launch") or ""),
                "shard": json.dumps(std.get("shard"), sort_keys=True) if std.get("shard") else "",
                "start": start or ""}
    return {"v": 1, "format": "harbor", "folder": os.path.basename(job_dir), "job_name": str(cfg.get("job_name") or ""),
            "agents": json.dumps([[str(a.get("name") or a.get("import_path") or ""), str(a.get("model_name") or "")]
                                  for a in agents]),
            "datasets": json.dumps([str(d.get("name") or d.get("path") or "") for d in cfg.get("datasets") or []
                                    if isinstance(d, dict)]),
            "start": start or ""}


def of(job_dir: str, cfg: dict, res: dict) -> tuple:
    """``(uid, source)``: the job's identity and where it came from,
    ``declared``, ``derived`` or ``pending`` (uid "")."""
    got = _declared(cfg)
    if got:
        return got, "declared"
    f = facts(job_dir, cfg, res)
    if f is None:
        return "", "pending"
    return str(uuid.uuid5(NS_JOB, json.dumps(f, sort_keys=True, separators=(",", ":")))), "derived"
