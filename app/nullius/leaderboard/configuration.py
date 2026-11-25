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
# Each axis's benchmarks, from what their tasks were counted to need: a direct
# entry weighs 1, a partial one 0.5. A whole-benchmark flag over several disjoint
# datasets is shared out among them.
DIRECT, PARTIAL = 1, .5
TAU = ("airline", "retail", "telecom", "banking_knowledge")
DEFAULT_ENTRIES = {
    "Safety": [("tau3-bench", "retail", "Score", DIRECT), ("tau3-bench", "airline", "Score", PARTIAL),
               ("tau3-bench", "banking_knowledge", "Score", PARTIAL), ("osworld1", "test_infeasible", "Score", PARTIAL),
               ("osworld2", "test_v2", "Score", PARTIAL)],
    "Consistency": [("tau3-bench", d, "pass^3", PARTIAL / len(TAU)) for d in TAU],
    "Retrieval": [("gaia", "gaia-validation", "Score", DIRECT), ("tau3-bench", "banking_knowledge", "Score", DIRECT),
                  ("osworld2", "test_v2", "Score", PARTIAL)],
    "Communication": [("tau3-bench", d, "Score", DIRECT / len(TAU)) for d in TAU],
    "Execution": [("osworld1", "test_nogdrive", "Score", DIRECT), ("osworld2", "test_v2", "Score", DIRECT),
                  ("swe-bench-verified", "verified", "Score", DIRECT), ("terminal-bench", "full", "Score", DIRECT),
                  ("gaia", "gaia-validation", "Score", PARTIAL), ("gsm8k", "test", "Score", PARTIAL),
                  ("humaneval", "humaneval", "Score", PARTIAL)]
                 + [("tau3-bench", d, "Score", DIRECT / len(TAU)) for d in TAU],
    "Travel": [("tau3-bench", "airline", "Score", DIRECT)],
    "Shopping": [("tau3-bench", "retail", "Score", DIRECT)],
    "Money": [("tau3-bench", "banking_knowledge", "Score", DIRECT), ("osworld2", "websites35", "Score", PARTIAL)],
    "Work": [("swe-bench-verified", "verified", "Score", DIRECT), ("terminal-bench", "full", "Score", DIRECT),
             ("osworld2", "test_v2", "Score", DIRECT), ("osworld1", "test_nogdrive", "Score", PARTIAL),
             ("humaneval", "humaneval", "Score", PARTIAL), ("open-telco", "teleqna-lite", "Score", PARTIAL)],
}


def location():
    return Path(paths.config_dir()) / "scoring.json"


def defaults(benchmarks):
    defaults_of = {b["key"]: b for b in benchmarks if b.get("_version") == b.get("_default")}
    versions = {k: definitions.version_identity(b) for k, b in defaults_of.items()}
    declared = lambda key, dataset: not dataset or dataset in {c["id"] for c in scopes.choices(defaults_of[key])}
    return {"schema": 1, "custom": [], "axes": {
        axis: {"weight": weight, "entries": [
            {"benchmark": key, "dataset": dataset, "version": versions[key], "metric": metric, "weight": weight}
            for key, dataset, metric, weight in DEFAULT_ENTRIES.get(axis, [])
            if key in versions and declared(key, dataset)]}
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
