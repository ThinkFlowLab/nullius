"""A job's identity: one UUID that stays the same wherever its folder is.

A job folder is copied, moved, mirrored from a host and opened in several
workspaces; its name is not unique either, since two roots can each hold a
job of one name. What Nullius records about a job (a judgement) is kept
under this identity, so it follows the job and not the folder.

- **declared**: the producer wrote one. A Nullius standard job carries
  ``standard.uuid`` in its ``config.json``, made at creation, kept by a run
  resumed into the same folder and new for a merge; an import carries its
  source's ``nullius.source``, made at the source's first conversion and kept
  by every reconversion.
- **derived**: none was written (Harbor, imports and standard jobs from before
  their ids). A ``uuid5`` over fixed facts read from the files: the
  format, the folder name, what ran and when it started. No path is a fact,
  so a copy or a move keeps it; a renamed folder is a new job.
- **pending**: nothing fixed to derive from yet: a job with no trial and no
  start (a Harbor job preparing), or a standard job from before
  ``standard.uuid`` without its ``result.json`` (still running, or killed).
  Its start is the one ``result.json`` will record; the feed's start is
  never read, since it differs from the result's and would change the uid.

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

def _declared(cfg: dict):
    std = cfg.get("standard") if isinstance(cfg.get("standard"), dict) else {}
    own = cfg.get("nullius") if isinstance(cfg.get("nullius"), dict) else {}
    for got in (std.get("uuid"), own.get("source")):
        try:
            return str(uuid.UUID(str(got)))
        except (ValueError, TypeError, AttributeError):
            continue
    return None


def _has_trial(job_dir: str) -> bool:
    try:
        with os.scandir(job_dir) as entries:
            return any(e.is_dir() and os.path.isfile(os.path.join(e.path, "result.json")) for e in entries)
    except OSError:
        return False


def facts(job_dir: str, cfg: dict, res: dict):
    """The fixed facts a derived identity is made of, as strings, or None while
    pending. The start is ``result.json``'s only, which nothing later rewrites."""
    std = cfg.get("standard") if isinstance(cfg.get("standard"), dict) else {}
    agents = [a for a in cfg.get("agents") or [] if isinstance(a, dict)]
    if isinstance(cfg.get("harness"), str):          # an import from before nullius.source: its folder names its source
        return {"v": 1, "format": cfg["harness"], "folder": os.path.basename(job_dir)}
    start = str(res["started_at"]) if res.get("started_at") else None
    if std and not res:
        return None
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
