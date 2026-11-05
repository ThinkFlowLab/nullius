"""A benchmark's host requirements (``host`` in its ``standard.json``): what
the machine its driver runs on must have for some of its options - programs on
PATH, installed by ``apt`` and ``npm`` - whatever the backend, since the driver
runs on the machine, never in a container.

Each requirement is checked by its programs (:func:`missing`) and installed by the commands
:func:`install_lines` gives: shown to a person to run, or run for them on an
explicit choice (``web/execute.py``), with ``sudo -n`` so a password is never
asked for - a machine where sudo needs one is told the commands instead.
"""
from __future__ import annotations

import shlex
import shutil
from typing import Dict, List, Optional


def requirements(bench: dict, names: List[str]) -> Dict[str, dict]:
    """The named requirements, as the benchmark declares them."""
    h = bench.get("host") or {}
    return {n: h[n] for n in names if n in h}


def missing(bench: dict, names: List[str], which=shutil.which) -> Dict[str, List[str]]:
    """Each named requirement this machine lacks, with the programs it lacks."""
    out = {}
    for n, r in requirements(bench, names).items():
        gone = [p for p in r.get("check") or [] if not which(p)]
        if gone:
            out[n] = gone
    return out


def install_lines(bench: dict, names: List[str], sudo: str = "sudo") -> List[str]:
    """The commands that install the named requirements: apt first (with ``sudo``), then npm."""
    apt, npm = [], []
    for r in requirements(bench, names).values():
        apt += [p for p in (r.get("install") or {}).get("apt") or [] if p not in apt]
        npm += [p for p in (r.get("install") or {}).get("npm") or [] if p not in npm]
    lines = []
    if apt:
        lines.append(f"{sudo} apt-get install -y " + " ".join(shlex.quote(p) for p in apt))
    if npm:
        lines.append("npm install -g " + " ".join(shlex.quote(p) for p in npm))
    return lines


def install_script(bench: dict, names: List[str]) -> Optional[str]:
    """The install as one script for a person's explicit yes: ``sudo -n`` (never a password prompt),
    stopping at the first command that fails; None when there is nothing to install."""
    lines = install_lines(bench, names, sudo="sudo -n")
    if not lines:
        return None
    return "set -e\n" + "\n".join(lines) + "\n"


def all_of(bench: dict) -> Dict[str, dict]:
    """Every host requirement of the benchmark, each with the option values that need it."""
    out = {n: dict(r, needed_by=[]) for n, r in (bench.get("host") or {}).items()}
    for name, o in (bench.get("options") or {}).items():
        for v, need in (o.get("needs") or {}).items():
            for h in need.get("host") or []:
                if h in out:
                    out[h]["needed_by"].append(f"{name}={v}")
    return out
