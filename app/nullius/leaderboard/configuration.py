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
# Each family has its own Overall: the capabilities' weights, and the domains' (equal) weights.
FAMILIES = {"capabilities": CAPABILITIES, "domains": DOMAINS}
OVERALL = (.12, .12, .08, .15, .15, .10, .08, .20) + (.14,) * len(DOMAINS)
SCHEMA = 2
# Each axis's benchmarks, from what their tasks were counted to need: a direct
# entry weighs 1, a partial one 0.5. A whole-benchmark flag over several disjoint
# datasets is shared out among them.
DIRECT, PARTIAL = 1, .5
TAU = ("airline", "retail", "telecom", "banking_knowledge")
VITA = ("cross", "delivery", "instore", "ota")
CLAW = ("general", "multimodal", "multi_turn")
DEFAULT_ENTRIES = {
    "Safety": [("tau3-bench", "retail", "Score", DIRECT), ("tau3-bench", "airline", "Score", PARTIAL),
               ("tau3-bench", "banking_knowledge", "Score", PARTIAL), ("osworld1", "test_infeasible", "Score", PARTIAL),
               ("osworld2", "test_v2", "Score", PARTIAL)]
              + [("claw-eval", d, "Score", DIRECT / len(CLAW)) for d in CLAW],
    "Consistency": [("tau3-bench", d, "pass^3", PARTIAL / len(TAU)) for d in TAU]
                   + [("vitabench", d, "pass^3", PARTIAL / len(VITA)) for d in VITA]
                   + [("claw-eval", d, "pass^3", PARTIAL / len(CLAW)) for d in CLAW],
    "Personalization": [("mypcbench", "final", "Score", DIRECT)]
                       + [("vitabench", d, "Score", PARTIAL / len(VITA)) for d in VITA],
    "Retrieval": [("gaia", "gaia-validation", "Score", DIRECT), ("tau3-bench", "banking_knowledge", "Score", DIRECT),
                  ("osworld2", "test_v2", "Score", PARTIAL), ("mypcbench", "final", "Score", DIRECT),
                  ("claw-eval", "general", "Score", PARTIAL / 2), ("claw-eval", "multimodal", "Score", PARTIAL / 2),
                  ("medagentbench", "v2", "Score", PARTIAL), ("healthagentbench", "full", "Score", PARTIAL)],
    "Communication": [("tau3-bench", d, "Score", DIRECT / len(TAU)) for d in TAU]
                     + [("vitabench", d, "Score", DIRECT / len(VITA)) for d in VITA]
                     + [("claw-eval", "multi_turn", "Score", DIRECT), ("mypcbench", "final", "Score", PARTIAL)],
    "Execution": [("osworld1", "test_nogdrive", "Score", DIRECT), ("osworld2", "test_v2", "Score", DIRECT),
                  ("swe-bench-verified", "verified", "Score", DIRECT), ("terminal-bench", "full", "Score", DIRECT),
                  ("gaia", "gaia-validation", "Score", PARTIAL), ("gsm8k", "test", "Score", PARTIAL),
                  ("humaneval", "humaneval", "Score", PARTIAL), ("mypcbench", "final", "Score", DIRECT),
                  ("costbench", "test", "Score", DIRECT), ("clawmark", "all", "Score", DIRECT),
                  ("medagentbench", "v2", "Score", DIRECT), ("healthagentbench", "full", "Score", DIRECT)]
                 + [("tau3-bench", d, "Score", DIRECT / len(TAU)) for d in TAU]
                 + [("vitabench", d, "Score", DIRECT / len(VITA)) for d in VITA]
                 + [("claw-eval", d, "Score", DIRECT / len(CLAW)) for d in CLAW],
    "Travel": [("tau3-bench", "airline", "Score", DIRECT), ("vitabench", "ota", "Score", DIRECT),
               ("vitabench", "cross", "Score", PARTIAL), ("costbench", "test", "Score", DIRECT)],
    "Dining": [("vitabench", "delivery", "Score", DIRECT / 2), ("vitabench", "instore", "Score", DIRECT / 2),
               ("vitabench", "cross", "Score", PARTIAL)],
    "Shopping": [("tau3-bench", "retail", "Score", DIRECT), ("vitabench", "instore", "Score", PARTIAL),
                 ("clawmark", "ecommerce", "Score", PARTIAL)],
    "Money": [("tau3-bench", "banking_knowledge", "Score", DIRECT), ("osworld2", "websites35", "Score", PARTIAL),
              ("clawmark", "insurance", "Score", PARTIAL / 2), ("clawmark", "investment_analyst", "Score", PARTIAL / 2)],
    "Health": [("medagentbench", "v2", "Score", DIRECT), ("healthagentbench", "full", "Score", DIRECT),
               ("clawmark", "clinical_assistant", "Score", PARTIAL)],
    "Work": [("swe-bench-verified", "verified", "Score", DIRECT), ("terminal-bench", "full", "Score", DIRECT),
             ("osworld2", "test_v2", "Score", DIRECT), ("osworld1", "test_nogdrive", "Score", PARTIAL),
             ("humaneval", "humaneval", "Score", PARTIAL), ("open-telco", "teleqna-lite", "Score", PARTIAL),
             ("clawmark", "all", "Score", DIRECT), ("mypcbench", "final", "Score", PARTIAL),
             ("claw-eval", "general", "Score", PARTIAL)],
}


def location():
    return Path(paths.config_dir()) / "scoring.json"


def defaults(benchmarks):
    defaults_of = {b["key"]: b for b in benchmarks if b.get("_version") == b.get("_default")}
    versions = {k: definitions.version_identity(b) for k, b in defaults_of.items()}
    declared = lambda key, dataset: not dataset or dataset in {c["id"] for c in scopes.choices(defaults_of[key])}
    axes = {axis: {"weight": weight, "entries": [
        {"benchmark": key, "dataset": dataset, "version": versions[key], "metric": metric, "weight": weight}
        for key, dataset, metric, weight in DEFAULT_ENTRIES.get(axis, [])
        if key in versions and declared(key, dataset)]} for axis, weight in zip(AXES, OVERALL)}
    for names in FAMILIES.values():
        _zero_empty(axes, names)
    return {"schema": SCHEMA, "axes": axes}


def _zero_empty(axes, names):
    """An axis with no benchmark weighs nothing in its family's Overall, unless none of the family has one."""
    if any(axes[n]["entries"] and axes[n]["weight"] > 0 for n in names):
        for n in names:
            if not axes[n]["entries"]:
                axes[n]["weight"] = 0


def _number(value, label, upper=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (upper is not None and value > upper):
        raise ValueError(f"{label}: enter a finite number between 0 and {upper}" if upper is not None else f"{label}: enter a finite nonnegative number")


def validate(document, benchmarks, *, previous=None, allow_unavailable=False):
    """Validate every field before a write; preserve removed catalogue references."""
    if not isinstance(document, dict) or set(document) != {"schema", "axes"} or document["schema"] != SCHEMA:
        raise ValueError("Invalid scoring document")
    doc = copy.deepcopy(document)
    if not isinstance(doc["axes"], dict) or set(doc["axes"]) != set(AXES):
        raise ValueError("Scoring requires all fifteen axes")
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
    for names in FAMILIES.values():
        _zero_empty(doc["axes"], names)                                # a row with no benchmark weighs nothing
    if not any(axis["weight"] > 0 for axis in doc["axes"].values()):
        raise ValueError("At least one Weight in Overall must be positive")
    return doc


def _upgrade(document):
    """A file saved before schema 2: its custom benchmarks and their rows are dropped, and its domains, which then
    weighed nothing, take the default domain weight when they have a benchmark. The rest is kept."""
    if not isinstance(document, dict) or document.get("schema") != 1 or not isinstance(document.get("axes"), dict):
        return document
    document = {k: v for k, v in document.items() if k != "custom"} | {"schema": SCHEMA}
    for axis in document["axes"].values():
        if isinstance(axis, dict) and isinstance(axis.get("entries"), list):
            axis["entries"] = [e for e in axis["entries"] if not (isinstance(e, dict) and "custom" in e)]
    axes = document["axes"]
    if all(isinstance(axes.get(n), dict) and isinstance(axes[n].get("entries"), list) and not axes[n].get("weight") for n in DOMAINS):
        for name, weight in zip(AXES, OVERALL):
            if name in DOMAINS:
                axes[name]["weight"] = weight
        _zero_empty(axes, DOMAINS)
    return document


def load(path, benchmarks):
    try:
        return validate(_upgrade(json.loads(Path(path).read_text())), benchmarks, allow_unavailable=True), ""
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
