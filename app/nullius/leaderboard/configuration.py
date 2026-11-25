"""Complete, validated profile scoring documents, written atomically."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from nullius import ondisk, paths
from nullius.standard.contract import definitions
from . import scopes

DOMAINS = ("Travel", "Dining", "Shopping", "Money", "Health", "Home", "Work")
CAPABILITIES = ("Memory", "Safety", "Consistency", "Personalization", "Proactivity", "Retrieval", "Communication", "Execution")
AXES = CAPABILITIES + DOMAINS
OVERALL = (.12, .12, .08, .15, .15, .10, .08, .20, 0, 0, 0, 0, 0, 0, 0)
DEFAULT_ENTRIES = {
    "Consistency": [("tau3-bench", "", "pass^3")],
    "Retrieval": [("gaia", "", "Score"), ("tau3-bench", "banking_knowledge", "Score")],
    "Communication": [("tau3-bench", "telecom", "Score")],
    "Execution": [(k, "", "Score") for k in ("osworld1", "osworld2", "swe-bench-verified", "terminal-bench")],
    "Travel": [("tau3-bench", "airline", "Score")],
    "Shopping": [("tau3-bench", "retail", "Score")],
    "Money": [("tau3-bench", "banking_knowledge", "Score")],
    "Home": [("tau3-bench", "telecom", "Score"), ("osworld1", "os", "Score")],
    "Work": [("osworld1", "office", "Score"), ("swe-bench-verified", "", "Score"),
             ("terminal-bench", "", "Score"), ("open-telco", "teleqna-lite", "Score")],
}


def location():
    return Path(paths.config_dir()) / "scoring.json"


def defaults(benchmarks):
    versions = {b["key"]: definitions.version_identity(b) for b in benchmarks
                if b.get("_version") == b.get("_default")}
    return {"schema": 1, "custom": [], "axes": {
        axis: {"weight": weight, "entries": [
            {"benchmark": key, "dataset": dataset, "version": versions[key], "metric": metric, "weight": 1}
            for key, dataset, metric in DEFAULT_ENTRIES.get(axis, []) if key in versions]}
        for axis, weight in zip(AXES, OVERALL)}}


def _number(value, label, upper=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (upper is not None and value > upper):
        raise ValueError(f"{label}: enter a finite number between 0 and {upper}" if upper is not None else f"{label}: enter a finite nonnegative number")


def validate(document, benchmarks, *, previous=None, allow_unavailable=False):
    """Validate every field before a write; preserve removed catalogue references."""
    if not isinstance(document, dict) or set(document) != {"schema", "custom", "axes"} or document["schema"] != 1:
        raise ValueError("Invalid scoring document")
    doc = copy.deepcopy(document)
    if not isinstance(doc["axes"], dict) or set(doc["axes"]) != set(AXES) or not isinstance(doc["custom"], list):
        raise ValueError("Scoring requires all fifteen axes and a custom benchmark list")
    ids, names = set(), set()
    for custom in doc["custom"]:
        if not isinstance(custom, dict) or set(custom) != {"id", "name", "scores"}:
            raise ValueError("Invalid custom benchmark")
        key, name = custom["id"], custom["name"]
        if not isinstance(key, str) or not key or key in ids or not isinstance(name, str) or not name.strip() or name.strip().casefold() in names:
            raise ValueError("Custom benchmarks need unique IDs and nonempty unique names")
        custom["name"] = name.strip()
        ids.add(key)
        names.add(name.strip().casefold())
        if not isinstance(custom["scores"], dict) or not all(isinstance(k, str) and k for k in custom["scores"]):
            raise ValueError("Custom scores require stable row keys")
        for value in custom["scores"].values():
            if value is not None:
                _number(value, "Custom score", 100)
    available = {(b["key"], definitions.version_identity(b)): b for b in benchmarks}
    preserved = {tuple(e.get(k) for k in ("benchmark", "dataset", "version", "metric"))
                 for a in (previous or {}).get("axes", {}).values() for e in a.get("entries", []) if "benchmark" in e}
    for name, axis in doc["axes"].items():
        if not isinstance(axis, dict) or set(axis) != {"weight", "entries"} or not isinstance(axis["entries"], list):
            raise ValueError(f"{name}: invalid axis")
        _number(axis["weight"], name)
        seen = set()
        for entry in axis["entries"]:
            if not isinstance(entry, dict):
                raise ValueError(f"{name}: invalid entry")
            _number(entry.get("weight"), f"{name} entry weight")
            if "custom" in entry:
                if set(entry) != {"custom", "weight"} or entry["custom"] not in ids:
                    raise ValueError("Unknown custom benchmark")
                identity = ("custom", entry["custom"])
            else:
                if set(entry) != {"benchmark", "dataset", "version", "metric", "weight"} or not all(isinstance(entry[k], str) for k in ("benchmark", "dataset", "version", "metric")) or not entry["benchmark"]:
                    raise ValueError("Invalid benchmark entry")
                bench = available.get((entry["benchmark"], entry["version"]))
                if entry["metric"] not in ("Score", "pass^3") or (entry["metric"] == "pass^3" and bench is not None and (bench.get("score") or {}).get("kind") != "pass"):
                    raise ValueError("Choose Score or supported pass^3")
                identity = tuple(entry[k] for k in ("benchmark", "dataset", "version", "metric"))
                if bench is None and not allow_unavailable and identity not in preserved:
                    raise ValueError("Choose a catalogue benchmark version")
                if (bench is not None and entry["dataset"] and entry["dataset"] not in {c["id"] for c in scopes.choices(bench)}
                    and not allow_unavailable and identity not in preserved):
                    raise ValueError("Choose a declared dataset or task-name part")
            if identity in seen:
                raise ValueError(f"{name}: duplicate entry")
            seen.add(identity)
    if not any(axis["weight"] > 0 for axis in doc["axes"].values()):
        raise ValueError("At least one Weight in Overall must be positive")
    return doc


def remove_custom(document, key):
    doc = copy.deepcopy(document)
    doc["custom"] = [c for c in doc["custom"] if c["id"] != key]
    for axis in doc["axes"].values():
        axis["entries"] = [e for e in axis["entries"] if e.get("custom") != key]
    return doc


def load(path, benchmarks):
    try:
        return validate(json.loads(Path(path).read_text()), benchmarks, allow_unavailable=True), ""
    except FileNotFoundError:
        return defaults(benchmarks), ""
    except (OSError, ValueError, TypeError):
        return defaults(benchmarks), "Scoring settings could not be read; defaults are active."


def save(path, document, benchmarks):
    previous, _ = load(path, benchmarks)
    doc = validate(document, benchmarks, previous=previous)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ondisk.write_whole(path, json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n", mode=0o600)
    return doc
