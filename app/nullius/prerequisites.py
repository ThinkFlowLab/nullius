"""What a run needs of the machine it runs on, and whether that machine has it.

The builder's Review step asks before Run (``/new-run/prerequisites``,
``web/builder.py``): one row per need, and Run waits until every row is
ready. A run asks again as it starts (``execution.py``), because a machine
can change between the two - docker quit, a program removed.

A need is one of:

- ``engine``: the standard's container backend's engine (``container.DOCKER``:
  docker, or podman) on PATH **and** answering this user - Docker Desktop's
  placeholder command, there while the engine is quit, is not docker. Its row also says when
  the engine has ``userns-remap``, which the standard's containers run outside of;
- ``docker``: the same, for Harbor, which runs the docker program;
- ``uv``: on a host it is installed when missing, here it must be there (a
  pinned Harbor runs through ``uvx``, a runner's install builds with it);
- ``harbor``: a recipe from before the pin runs the ``harbor`` on PATH;
- ``python3``, ``git``: the standard's cli, on a host;
- a benchmark's machine setup (``host`` in its definition): its programs,
  optional GPU memory minimum, and the commands that install them;
- the machine's constraints (``nullius.constraints``, ``constraint_rows``):
  a container constraint its engine, or a cap its systemd, did not enforce at
  its last test, or a setup that needs a GPU (checked by ``nvidia-smi``)
  where no GPU is allowed.

Nothing here installs anything or writes anything: each check is
``command -v``, the engine's ``version`` and, when needed, ``nvidia-smi`` here,
or one ssh probe for a host (``hosts.test``). Remote GPU memory is checked
by the run as it starts there.
"""
from __future__ import annotations

import shutil
import time
from typing import Callable, Dict, List, Optional

from nullius import constraints, hosts
from nullius.standard import container, hostreq

LABEL = {"engine": "Docker or Podman", "docker": "Docker", "uv": "uv", "harbor": "Harbor on PATH", "python3": "python3", "git": "git"}

# the docker row's detail for the standard on an engine with userns-remap (container.userns_remap)
REMAPPED = "the engine answers, and Nullius's containers run outside its userns-remap"

# Whether the engine answers here, asked at most once a minute: the builder asks on each preview.
_HERE = {"at": 0.0, "why": None}


def docker_here(fresh: bool = False) -> Optional[str]:
    """Why the container backend's engine (``container.DOCKER``) cannot run
    containers on this machine for this user, or None when it can: the
    engine asked, not only the command looked for.
    Kept for a minute unless ``fresh``."""
    if fresh or time.monotonic() - _HERE["at"] > 60:
        _HERE.update(at=time.monotonic(), why=container.unusable())
    return _HERE["why"]


def row(name: str, state: str, detail: str = "", install: Optional[List[str]] = None, label: str = "") -> dict:
    """One need: ``state`` ok, missing (Run waits) or install (the run puts it there)."""
    return {"name": name, "label": label or LABEL.get(name, name), "state": state, "detail": detail, "install": install or []}


def check_here(needs: List[str], setups: Optional[Dict[str, dict]] = None, *, standard: bool = False,
               which: Optional[Callable] = None, docker: Optional[Callable] = None,
               remap: Optional[Callable] = None) -> List[dict]:
    """The rows for this machine. ``setups``: a benchmark's machine setups by
    name, each ``{summary, check: [programs], install: [commands]}``, with an
    optional ``gpu_memory_mb`` minimum. ``standard``: the recipe is the
    standard's, whose containers Nullius starts. ``which``, ``docker`` and
    ``remap`` are looked up at call time, so a test can stand in for them."""
    which = which or shutil.which
    docker = docker or docker_here
    remap = remap or container.userns_remap
    rows = []
    for n in needs:
        if n in ("engine", "docker"):
            why = docker()
            if not why and n == "docker" and not which("docker"):
                why = "Install Docker on this machine: Harbor runs the docker program"
            rows.append(row(n, "missing", why) if why else
                        row(n, "ok", REMAPPED if standard and remap() else "the engine answers"))
        elif n == "uv":
            rows.append(row(n, "ok") if which("uvx") else row(n, "missing", "uv is not on this machine's PATH", ["curl -LsSf https://astral.sh/uv/install.sh | sh"]))
        elif n == "harbor":
            rows.append(row(n, "ok") if which("harbor") else row(n, "missing", "a recipe from before the pin runs the harbor on PATH: pin a Harbor on the Harbor step"))
        else:
            rows.append(row(n, "ok") if which(n) else row(n, "missing", f"{n} is not on this machine's PATH"))
    lacking = hostreq.problems({"host": setups or {}}, list(setups or {}), which, constraints.of(constraints.LOCAL, strict=False).get("gpu_ids"))
    for name, s in (setups or {}).items():
        rows.append(row("setup:" + name, "missing", lacking[name], s.get("install"), label=name)
                    if name in lacking else row("setup:" + name, "ok", s.get("summary", ""), label=name))
    return rows


def check_host(host: hosts.Host, needs: List[str], setups: Optional[Dict[str, dict]] = None, *,
               standard: bool = False, test: Optional[Callable] = None) -> List[dict]:
    """The rows for a host, from one ssh probe. Unreachable: one row, and nothing else is known."""
    test = test or hosts.test
    programs = [p for s in (setups or {}).values() for p in s.get("check") or []]
    programs += [n for n in needs if n in ("python3", "git")]
    got = test(host, programs=programs)
    if not got["ok"]:
        return [row("connect", "missing", got.get("error") or "no answer", label="Connection")]
    gone = set(got.get("missing") or [])
    rows = [row("connect", "ok", got.get("hostname") or host.label, label="Connection")]
    for n in needs:
        if n == "engine" and got["engine"] != "docker":
            rows.append(row(n, "missing", "neither docker nor podman is on the host") if not got["engine"]
                        else row(n, "missing", "podman is on the host but this user cannot use it (podman ps failed)")
                        if got["podman_ok"] is False else row(n, "ok", "the engine answers"))
        elif n in ("engine", "docker"):
            rows.append(row(n, "missing", "docker is not on the host") if not got["docker"]
                        else row(n, "missing", "docker is on the host but this user cannot use it (docker ps failed): "
                                 "add the user to the docker group there, then log in again") if got["docker_ok"] is False
                        else row(n, "ok", REMAPPED if standard and got.get("docker_userns") else "the engine answers"))
        elif n == "uv":
            rows.append(row(n, "ok") if got["uv"] else row(n, "install", "installed on the host when the run starts"))
        elif n == "harbor":
            rows.append(row(n, "ok", got.get("harbor_version") or "") if got["harbor"]
                        else row(n, "missing", "a recipe from before the pin runs the harbor on the host's PATH: pin a Harbor on the Harbor step"))
        else:
            rows.append(row(n, "missing", f"{n} is not on the host's PATH") if n in gone else row(n, "ok"))
    for name, s in (setups or {}).items():
        lacking = [p for p in s.get("check") or [] if p in gone]
        rows.append(row("setup:" + name, "missing", ", ".join(lacking) + " not on PATH", s.get("install"), label=name)
                    if lacking else row("setup:" + name, "ok", s.get("summary", ""), label=name))
    return rows


def constraint_rows(where: str, runner: str, backend: str, setups: Optional[Dict[str, dict]] = None,
                    found: Optional[dict] = None, own: bool = False) -> List[dict]:
    """The rows for a machine's constraints: none when it sets none. A Harbor
    run does not take them; a standard run on ``backend`` (``own``: its units
    in their own images) is refused what the machine's last test found will
    not hold (``constraints.not_enforced``; the run checks again as it
    starts), and every run is refused while the setting is not valid. A setup
    that needs a GPU is refused where the machine allows none. ``found``: its
    last test, by default the one ``constraints.save`` kept."""
    try:
        values = constraints.of(where, strict=runner != "harbor")
    except ValueError as e:
        return [row("constraints", "missing", f"{e}: fix it in Settings", label="Constraints")]
    if not values:
        return []
    if runner == "harbor":
        return [row("constraints", "ok", "not applied to Harbor runs", label="Constraints")]
    found = constraints.load().get(where) if found is None else found
    label = lambda keys: ", ".join(constraints.BY_KEY[k].label for k in keys)
    lost = constraints.not_enforced(values, found, backend, own)
    engine, unscoped = [k for k in lost if lost[k] == "engine"], [k for k in lost if lost[k] == "systemd"]
    sd = (found or {}).get("systemd") or {}
    said = ", ".join(f"{constraints.BY_KEY[k].label} {constraints.spell(v) or 'none'}"
                     + (f" {constraints.BY_KEY[k].unit}" if constraints.BY_KEY[k].unit else "") for k, v in values.items())
    why = [*([f"this machine's engine does not enforce {label(engine)}"] if engine else []),
           *([f"systemd does not enforce {label(unscoped)} here" + (f" ({sd['why']})" if sd.get("why") else "")] if unscoped else [])]
    rows = [row("constraints", "missing", "; ".join(why) + ": test it in Settings", label="Constraints") if why
            else row("constraints", "ok", said, label="Constraints")]
    # here check_here says it, with the GPU memory it checks among the GPUs allowed
    if where != constraints.LOCAL and constraints.no_gpu(values) and any("nvidia-smi" in (s.get("check") or []) for s in (setups or {}).values()):
        rows.append(row("gpu", "missing", "this machine allows no GPU: GPUs Nullius may use, in Settings", label="GPUs"))
    return rows


def ready(machines: List[dict]) -> bool:
    """Whether Run may go: no row of any machine is missing."""
    return all(r["state"] != "missing" for m in machines for r in m["rows"])
