"""Canonical names, with read compatibility for earlier catalogues, jobs and recipes."""
from __future__ import annotations

BACKENDS = ("local", "container")


def backend(value):
    """``docker``, the container backend's earlier name, reads as ``container``."""
    return "container" if value == "docker" else value


def kind(value: str) -> str:
    return "capability" if value == "feature" else value


def port(value: str) -> str:
    return "cap." + value[len("feature."):] if isinstance(value, str) and value.startswith("feature.") else value


def definition(value):
    """Normalize only schema vocabulary; preserve commands, paths and user options."""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    if result.get("schema") == "nullius.feature/v1":
        result["schema"] = "nullius.capability/v1"
    if result.get("family") == "feature":
        result["family"] = "capability"
    for field in ("needs", "offers"):
        if isinstance(result.get(field), list):
            result[field] = [port(p) for p in result[field]]
    for field in ("proven", "needs_skills"):
        if isinstance(result.get(field), dict):
            result[field] = {port(p): record for p, record in result[field].items()}
    if isinstance(result.get("versions"), dict):
        result["versions"] = {v: definition(block) for v, block in result["versions"].items()}
    return result
