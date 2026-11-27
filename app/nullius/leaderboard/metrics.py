"""Deferred trajectory counts, cached by file identity and content digest."""
from __future__ import annotations

import hashlib
from pathlib import Path
from threading import RLock

from nullius.traces import trajectory


class Cache:
    def __init__(self):
        self._entries = {}
        self._lock = RLock()

    def read(self, path):
        missing = {"steps": None, "calls": None}
        try:
            target = Path(path)
            info = target.stat()
            signature = (info.st_dev, info.st_ino, hashlib.sha256(target.read_bytes()).hexdigest())
        except OSError:
            return missing
        with self._lock:
            previous = self._entries.get(path)
            if previous and previous[0] == signature:
                return dict(previous[1])
            try:
                trace = trajectory.load(path)
                result = missing if trace.error else {"steps": trace.n_steps, "calls": trace.n_tool_calls}
                # A writer may have changed the file while its parser read it.
                if hashlib.sha256(target.read_bytes()).hexdigest() != signature[2]:
                    return missing
            except Exception:                                          # a file of any shape: its counts are unavailable
                result = missing
            self._entries[path] = (signature, result)
            return dict(result)

    def complete(self, document, sources):
        for row in document["rows"]:
            observations = [self.read(path) for path in sources.get(row["key"], ())]
            for field in ("steps", "calls"):
                values = [o[field] for o in observations if o[field] is not None]
                row["metrics"][field] = {"value": sum(values) / len(values) if values else None, "count": len(values)}


CACHE = Cache()
