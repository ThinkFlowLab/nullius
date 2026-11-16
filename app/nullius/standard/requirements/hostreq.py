"""A benchmark's host requirements (``host`` in its ``standard.json``): what
the machine its driver runs on must have for some of its options - programs on
PATH, installed by ``apt`` and ``npm``. Drivers for agent/capability runs stay
on the machine; Docker Original setups install those with an ``install`` in
their image and are refused the rest (a GPU).

Each requirement is checked by its programs and optional GPU memory minimum
(:func:`problems`) and installed by the commands
:func:`install_lines` gives: shown to a person to run, or run for them on an
explicit choice (``web/execute.py``), with ``sudo -n`` so a password is never
asked for - a machine where sudo needs one is told the commands instead.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
from typing import Dict, List, Optional


def requirements(bench: dict, names: List[str]) -> Dict[str, dict]:
    """The named requirements, as the benchmark declares them."""
    h = bench.get("host") or {}
    return {n: h[n] for n in names if n in h}


def in_image(bench: dict, names: List[str]) -> List[str]:
    """The named requirements a Docker Original setup's image installs (its
    ``install``); the rest, a GPU above all, no image can provide."""
    return [n for n, r in requirements(bench, names).items() if r.get("install")]


def missing(bench: dict, names: List[str], which=shutil.which) -> Dict[str, List[str]]:
    """Each named requirement this machine lacks, with the programs it lacks."""
    out = {}
    for n, r in requirements(bench, names).items():
        gone = [p for p in r.get("check") or [] if not which(p)]
        if gone:
            out[n] = gone
    return out


def gpu_memory_mb(gpu_ids: Optional[List[int]] = None) -> Optional[int]:
    """The largest GPU's total memory in MiB, among the GPUs ``gpu_ids`` allows
    (the machine's constraint; None: every GPU), or None when it cannot be
    read or no allowed GPU is there. Never add cards together or treat an
    unavailable query as zero memory."""
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.total", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=5)
        rows = [[v.strip() for v in line.split(",")] for line in result.stdout.splitlines()]
        if result.returncode == 0 and rows and all(len(r) == 2 and all(v.isascii() and v.isdecimal() for v in r) and int(r[1]) > 0
                                                   for r in rows):
            sizes = [int(m) for i, m in rows if gpu_ids is None or int(i) in gpu_ids]
            return max(sizes) if sizes else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    return None


def problems(bench: dict, names: List[str], which=None, gpu_ids: Optional[List[int]] = None) -> Dict[str, str]:
    """Why each selected requirement is missing on this machine, shared by
    the builder and run startup. Query GPU memory only when needed, once,
    among the GPUs ``gpu_ids`` allows (the machine's constraint; [] allows
    none, so a requirement a GPU's memory meets is refused)."""
    which = which or shutil.which
    out = {n: ", ".join(ps) + " not on PATH" for n, ps in missing(bench, names, which).items()}
    if gpu_ids == []:                    # a requirement nvidia-smi checks is a GPU's
        out.update({n: "this machine allows Nullius no GPU" for n, r in requirements(bench, names).items()
                    if n not in out and "nvidia-smi" in (r.get("check") or [])})
    gpu = {n: r["gpu_memory_mb"] for n, r in requirements(bench, names).items()
           if n not in out and "gpu_memory_mb" in r}
    if gpu:
        available = gpu_memory_mb(gpu_ids) if which("nvidia-smi") else None
        for n, needed in gpu.items():
            if available is None:
                out[n] = ("no GPU Nullius may use here could report its memory" if gpu_ids else
                          "nvidia-smi could not report GPU memory") if which("nvidia-smi") else "nvidia-smi not on PATH"
            elif available < needed:
                out[n] = f"needs at least {needed} MiB of GPU memory; largest GPU {'Nullius may use ' if gpu_ids else ''}has {available} MiB"
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
