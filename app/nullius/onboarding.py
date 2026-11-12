"""Getting started: the checklist a person is guided through on a first open.

Five sections, each a page of the application and its sub-tasks. A sub-task
is done by a fact this machine can check (``fact``: a key set, a run on disk,
a trial judged) or, where nothing on disk records it, by the person having
been on the page that does it (``visit``: a path pattern the browser matches
and remembers). Nothing here is stored: the facts are read on each ask, and
the visits stay in the browser that made them.

``target`` says where on a page the thing to do is, so the guide can point at
it: a list of (CSS selector, label) tried in order on the page the person is
on; when none is there, the guide opens ``page`` and tries again.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, Iterable, Optional

from nullius.standard import container

CONTROLS = ("nop", "oracle")
PROVIDER_KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "MODEL_API_URL")

SECTIONS = [
    {"id": "setup", "name": "Set up", "page": "settings_page", "where": "Settings", "subs": [
        {"id": "provider", "text": "Add a model provider key", "fact": "provider",
         "target": [["#providers", "Add a key for one provider"]]},
        {"id": "where", "text": "Choose where runs happen", "fact": "where",
         "target": [["#hosts", "Add a host, or run on this machine with Docker or Podman"]]},
        {"id": "judge", "text": "Pick a judge", "fact": "judge", "optional": True,
         "target": [["#judge", "Choose System One or an LLM judge"]]},
    ]},
    {"id": "bench", "name": "Pick a benchmark", "page": "benchmarks_page", "where": "Benchmarks", "subs": [
        {"id": "browse", "text": "Browse the catalogue", "visit": r"^/benchmarks/?$",
         "target": [[".bench-filters, #bench-find", "Filter by category, runner or origin"]]},
        {"id": "open", "text": "Open a benchmark's page", "visit": r"^/benchmarks/(?!new)[^/]+$",
         "target": [[".bench-card", "Open a benchmark"]]},
    ]},
    {"id": "check", "name": "Free check run", "page": "new_run", "where": "Run builder", "subs": [
        {"id": "builder", "text": "Open the run builder", "visit": r"^/new-run/?$",
         "target": [[".nb-steps", "Start at Benchmark, then each step in turn"]]},
        {"id": "control", "text": "Run oracle and nop on a few tasks", "fact": "control_run",
         "target": [[".nb-steps", "At the Agent step, add oracle and nop: no model, no cost"]]},
    ]},
    {"id": "read", "name": "Read the result", "page": "index", "where": "Runs", "subs": [
        {"id": "job", "text": "Open a job", "visit": r"^/job/[^/]+$",
         "target": [["main table tbody tr a[href*='/job/']", "Open a job"]]},
        {"id": "trial", "text": "Open a trial and its trace", "visit": r"^/trial/[^/]+/[^/]+$",
         "target": [["tr[data-outcome] a[href*='/trial/']", "Open a trial"],
                    ["main table tbody tr a[href*='/job/']", "Open a job, then one of its trials"]]},
        {"id": "judged", "text": "Ask a judge about a trial", "fact": "judged", "optional": True,
         "target": [["[id^='judge-all'], #judgement", "Judge the trials"],
                    ["main table tbody tr a[href*='/job/']", "Open a job to judge its trials"]]},
    ]},
    {"id": "compare", "name": "Run an agent and compare", "page": "compare", "where": "Compare", "subs": [
        {"id": "agent", "text": "Run an agent on the same tasks", "fact": "agent_run", "page": "new_run",
         "target": [[".nb-steps", "The same benchmark, then an agent at the Agent step"]]},
        {"id": "compared", "text": "Compare two runs", "visit": r"^/compare/?\?(.*&)?pick=",
         "target": [["main form, main table", "Pick two runs on the same tasks"]]},
    ]},
]


def docker_answers(timeout: float = 5.0) -> bool:
    """Whether the docker backend's engine (``container.DOCKER``: docker, or
    podman) can run a container here: the command *and* ``<engine> ps``
    answering. The command alone is not enough - Docker Desktop's WSL
    integration leaves ``/usr/bin/docker`` in place while its engine is quit."""
    if not shutil.which(container.DOCKER):
        return False
    try:
        return subprocess.run([container.DOCKER, "ps"], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def facts(env, jobs: Iterable[dict] = (), judged: bool = False, docker: Optional[Callable] = None) -> dict:
    """What this machine says is done: ``env`` the settings Nullius reads,
    ``jobs`` the open workspace's job index entries (each with its ``agent``),
    ``judged`` whether any of its trials holds a judgement."""
    from nullius.judge import client
    agents = [(j.get("agent") or "").lower() for j in jobs]
    found = (docker or docker_answers)()
    return {
        "provider": any((env.get(k) or "").strip() for k in PROVIDER_KEYS),
        "where": bool((env.get("HOSTS") or "").strip()) or found,
        "judge": bool(client.system_one(env)) or bool(client.llm_judge(env)),
        "control_run": any(a in CONTROLS for a in agents),
        "agent_run": any(a and a not in CONTROLS for a in agents),
        "judged": bool(judged),
    }


def any_judged(ws_dir: Optional[str]) -> bool:
    """Whether a workspace holds a judgement from either judge."""
    from nullius.judge import store
    for lane in store.ROOTS.values():
        root = os.path.join(ws_dir or "", lane)
        if ws_dir and os.path.isdir(root) and any(files for _, _, files in os.walk(root)):
            return True
    return False


def checklist(found: dict, url_for: Callable) -> list:
    """The sections for the guide, each sub-task with ``done`` set from a
    fact (True/False) or left None for the browser to settle by its visits."""
    out = []
    for s in SECTIONS:
        subs = [{"id": x["id"], "text": x["text"], "optional": x.get("optional", False),
                 "done": bool(found.get(x["fact"])) if "fact" in x else None, "visit": x.get("visit"),
                 "url": url_for(x.get("page", s["page"])), "target": x["target"]} for x in s["subs"]]
        out.append({"id": s["id"], "name": s["name"], "where": s["where"], "url": url_for(s["page"]), "subs": subs})
    return out
