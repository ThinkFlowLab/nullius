"""Which pairs fit, and why the others do not.

A benchmark of the agent family takes an agent, one of the capability family a
capability, and the pair fits when the other side offers - and has proven -
every port the benchmark needs - and, for a port whose kit has skills, has
shown each skill the benchmark relies on (``needs_skills``). An agent with
``only_for`` - a benchmark's own reference agent - fits those benchmarks
alone, whatever it has proven. The reasons are sentences a page can show
beside a greyed-out choice.
"""
from __future__ import annotations

from typing import Dict, List

from nullius.standard import ports
from nullius.standard.definitions import CONTROLS, port_skills, ref_of, skills_shown


def fit(bench: dict, other: dict) -> List[str]:
    """Why ``other`` (an agent or a capability definition, or a control) cannot
    run ``bench``; empty when it can. A control fits the benchmark that
    declares it, whatever its family: it needs no port, it answers itself."""
    side = bench.get("family")
    kind = other.get("_kind") or ("agent" if other.get("schema", "").startswith("nullius.agent") else "capability")
    if kind == "upstream":
        return [] if other.get("key") in (bench.get("setups") or {}) else [f"{bench['label']} has no {other.get('key')} setup"]
    if kind == "control":
        declared = (bench.get("controls") or {}).get(other.get("key"))
        return [] if declared else [f"{bench['label']} declares no {other.get('key')} control"]
    if kind != side:
        return [f"{bench['label']} takes {'an agent' if side == 'agent' else 'a capability'}, and {other['label']} is {'an agent' if kind == 'agent' else 'a capability'}"]
    offers, proven = set(other.get("offers") or []), other.get("proven") or {}
    version = str((other.get("upstream") or {}).get("version") or "")
    name = f"{other['label']} {version}" if "@" in str(other.get("_ref") or "") else other["label"]
    only = other.get("only_for") or []
    if only and bench.get("key") not in only:
        return [f"{name} is the reference agent of {', '.join(only)}, and runs no other benchmark"]
    why = []
    for p in bench.get("needs") or []:
        on = str((proven.get(p) or {}).get("version") or "")
        if p not in offers:
            why.append(f"needs {p} ({ports.PORTS.get(p, {}).get('summary', '')}), which {name} does not offer")
        elif p not in proven:
            why.append(f"needs {p}, which {name} claims but has not passed its kit")
        elif on and version and on != version:
            why.append(f"needs {p}, which {name} passed on version {on}, not {version}: prove it again")
        else:
            shown, skills = skills_shown(p, proven[p]), port_skills(p)
            why += [f"{name} has not shown the {s} skill of {p}" + (f" ({skills[s]})" if s in skills else "")
                    for s in (bench.get("needs_skills") or {}).get(p) or [] if s not in shown]
    return why


def table(benches: List[dict], agents: List[dict], capabilities: List[dict]) -> Dict[str, Dict[str, List[str]]]:
    """For every benchmark, every agent or capability of its family - each
    version by its ``key@version`` when it has several - and the reasons it
    does not fit (empty: offered); then the controls it declares, ``nop`` and
    ``oracle``, which always fit. A benchmark is keyed as an agent is: its
    key for its default version, ``key@version`` for another."""
    out = {}
    for b in benches:
        others = agents if b.get("family") == "agent" else capabilities
        k = ref_of(b) if "_version" in b else b["key"]
        out[k] = {(o.get("_ref") or o["key"]): fit(b, o) for o in others}
        out[k].update({c: [] for c in CONTROLS if c in (b.get("controls") or {})})
    return out
