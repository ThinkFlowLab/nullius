"""What a run needs of the machine it runs on, and whether that machine has it.

The builder's Review step asks before Run (``/new-run/prerequisites``,
``web/builder.py``): one row per need, and Run waits until every row is
ready. A run asks again as it starts (``execution.py``), because a machine
can change between the two - docker quit, a program removed.

A need is one of:

- ``docker``: on PATH **and** its engine answering this user - Docker
  Desktop's placeholder command, there while the engine is quit, is not
  docker;
- ``uv``: on a host it is installed when missing, here it must be there (a
  pinned Harbor runs through ``uvx``, a runner's install builds with it);
- ``harbor``: a recipe from before the pin runs the ``harbor`` on PATH;
- ``python3``, ``git``: the standard's cli, on a host;
- a benchmark's machine setup (``host`` in its definition): its programs,
  and the commands that install them.

Nothing here installs anything or writes anything: each check is
``command -v`` and ``docker version`` here, or one ssh probe for a host
(``hosts.test``).
"""
from __future__ import annotations

import shutil
import time
from typing import Callable, Dict, List, Optional

from nullius import hosts
from nullius.standard import container

LABEL = {"docker": "Docker", "uv": "uv", "harbor": "Harbor on PATH", "python3": "python3", "git": "git"}

# Whether docker answers here, asked at most once a minute: the builder asks on each preview.
_HERE = {"at": 0.0, "why": None}


def docker_here(fresh: bool = False) -> Optional[str]:
    """Why docker cannot run containers on this machine for this user, or
    None when it can: the engine asked, not only the command looked for.
    Kept for a minute unless ``fresh``."""
    if fresh or time.monotonic() - _HERE["at"] > 60:
        _HERE.update(at=time.monotonic(), why=container.unusable())
    return _HERE["why"]


def row(name: str, state: str, detail: str = "", install: Optional[List[str]] = None, label: str = "") -> dict:
    """One need: ``state`` ok, missing (Run waits) or install (the run puts it there)."""
    return {"name": name, "label": label or LABEL.get(name, name), "state": state, "detail": detail, "install": install or []}


def check_here(needs: List[str], setups: Optional[Dict[str, dict]] = None, *,
               which: Optional[Callable] = None, docker: Optional[Callable] = None) -> List[dict]:
    """The rows for this machine. ``setups``: a benchmark's machine setups by
    name, each ``{summary, check: [programs], install: [commands]}``. ``which``
    and ``docker`` are looked up at call time, so a test can stand in for them."""
    which = which or shutil.which
    docker = docker or docker_here
    rows = []
    for n in needs:
        if n == "docker":
            why = docker()
            rows.append(row(n, "missing", why) if why else row(n, "ok", "the engine answers"))
        elif n == "uv":
            rows.append(row(n, "ok") if which("uvx") else row(n, "missing", "uv is not on this machine's PATH", ["curl -LsSf https://astral.sh/uv/install.sh | sh"]))
        elif n == "harbor":
            rows.append(row(n, "ok") if which("harbor") else row(n, "missing", "a recipe from before the pin runs the harbor on PATH: pin a Harbor on the Harbor step"))
        else:
            rows.append(row(n, "ok") if which(n) else row(n, "missing", f"{n} is not on this machine's PATH"))
    for name, s in (setups or {}).items():
        gone = [p for p in s.get("check") or [] if not which(p)]
        rows.append(row("setup:" + name, "missing", ", ".join(gone) + " not on PATH", s.get("install"), label=name)
                    if gone else row("setup:" + name, "ok", s.get("summary", ""), label=name))
    return rows


def check_host(host: hosts.Host, needs: List[str], setups: Optional[Dict[str, dict]] = None, *,
               test: Optional[Callable] = None) -> List[dict]:
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
        if n == "docker":
            rows.append(row(n, "missing", "docker is not on the host") if not got["docker"]
                        else row(n, "missing", "docker is on the host but this user cannot use it (docker ps failed): "
                                 "add the user to the docker group there, then log in again") if got["docker_ok"] is False
                        else row(n, "ok", "the engine answers"))
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


def ready(machines: List[dict]) -> bool:
    """Whether Run may go: no row of any machine is missing."""
    return all(r["state"] != "missing" for m in machines for r in m["rows"])
