"""Bounded, redacted views of named trial records. No writes or arbitrary paths."""
from __future__ import annotations

import os

from nullius.redact import redact

FILES = {
    "result": ("Result record", "result.json"),
    "config": ("Run configuration", "config.json"),
    "exception": ("Exception", "exception.txt"),
    "trial-log": ("Trial log", "trial.log"),
    "gateway": ("Gateway log", "agent/gateway.log"),
    "reward": ("Recorded reward", "verifier/reward.txt"),
    "reward-json": ("Recorded reward (rewardkit)", "verifier/reward.json"),
    "reward-details": ("Reward breakdown (rewardkit)", "verifier/reward-details.json"),
    # Harbor's record of what it copied out of the container after the run —
    # one entry per requested path with its status (`ok`, `failed`, `empty`,
    # `skipped` — Harbor's `ArtifactManifestEntry`). The last of the nine
    # files a trial writes to reach a page: the others were already
    # here, or are read at parse.
    "manifest": ("Artifact manifest", "artifacts/manifest.json"),
}
CHUNK_SIZE = 8_000
MAX_READ = 16 * 1024 * 1024


def path_for(trial_dir, key):
    if key not in FILES:
        raise LookupError(key)
    base = os.path.realpath(trial_dir)
    path = os.path.realpath(os.path.join(base, FILES[key][1]))
    if os.path.commonpath([base, path]) != base:
        raise LookupError(key)
    return path


def listing(trial_dir):
    records = []
    for key, (label, filename) in FILES.items():
        try:
            path = path_for(trial_dir, key)
            exists = os.path.isfile(path)
            size = os.path.getsize(path) if exists else None
        except (OSError, LookupError):
            exists, size = False, None
        records.append(dict(key=key, label=label, filename=filename, exists=exists, size=size))
    return records


def read(trial_dir, key, page=1):
    path = path_for(trial_dir, key)
    with open(path, encoding="utf-8", errors="replace") as handle:
        raw = handle.read(MAX_READ + 1)
    # Redact before slicing so credentials cannot straddle visible chunks.
    text = redact(raw[:MAX_READ])
    pages = max(1, (len(text) + CHUNK_SIZE - 1) // CHUNK_SIZE)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = min(pages, max(1, page))
    start = (page - 1) * CHUNK_SIZE
    return dict(key=key, label=FILES[key][0], filename=FILES[key][1], page=page,
                pages=pages, text=text[start:start + CHUNK_SIZE], empty=not text,
                truncated=len(raw) > MAX_READ, characters=len(text), start=start,
                end=min(start + CHUNK_SIZE, len(text)))
