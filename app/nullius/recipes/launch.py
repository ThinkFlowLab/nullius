"""Read-only launch vocabulary for Harbor recipes; no harness imports.

The catalogue owns benchmark facts. This module owns only launch choices and
parameter metadata: the agents the builder offers by name and the parameters
it knows for them. The browser compiles recipes locally; this module never
executes commands, and nothing here imports a harness or a runtime.

Every name here has to match what Harbor accepts - an agent's import path, the
``--ak`` a parameter is sent as - and this repository has no Harbor source to
check them against. What is checked here is the other half:
``app/ui_tests/test_command_builder.py`` pins the recipes this module
produces, so a change made *here* cannot silently alter what the builder emits.
"""
from dataclasses import asdict

# `kind` groups the builder's agent list: the reference agent a benchmark
# brings and the controls that bracket it. Harbor's own agents join them once
# the pinned Harbor is scanned (`builtin`); an agent under evaluation is
# typed per recipe, as an import path.
AGENTS = [
    # A benchmark's own reference agent: lives in Harbor's source tree, not its package,
    # so it needs the adapter directory on PYTHONPATH (see running.md). The path is
    # anchored on the run target's home, not on a sibling of a checkout: a run from
    # the builder starts in its own run directory, on this machine or a host, and
    # `../harbor-src` from there pointed nowhere. A recipe may name another.
    {"id": "tau3-llm-agent", "label": "tau3-llm-agent (τ³ reference)", "arg": "tau3_llm_agent:Tau3LLMAgent",
     "control": False, "benchmarks": ["tau3-bench"], "pythonpath": "~/harbor-src/adapters/tau3-bench", "kind": "reference",
     "summary": "τ³'s own reference agent, from Harbor's source tree."},
    {"id": "oracle", "label": "oracle", "arg": "oracle", "control": True, "kind": "control",
     "summary": "Applies the task's reference solution; never calls a model. A ceiling, never ranked."},
    {"id": "nop", "label": "nop", "arg": "nop", "control": True, "kind": "control",
     "summary": "Does nothing. The floor a verifier must reject."},
]


def parameter(key, label, group, kind, default=None, *, help="", **extra):
    return dict(key=key, label=label, group=group, type=kind, default=default, help=help, **extra)


PARAMETERS = [
    # Sent to the agent its group names: the τ³ reference agent's tool timeout,
    # emitted as `--ak tool_timeout_sec=`.
    parameter("mcp_timeout_s", "Tool call timeout · seconds", "tau3-llm-agent", "integer", 300, min=0,
              help="How long one MCP tool call may take."),
]


# Local launch paths are examples from this repository, not claims about disk state.
LAUNCH = {
    "swe-bench-verified": {"local": "../harbor-datasets/swe-bench-verified"},
    "terminal-bench": {"local": "../harbor-datasets/terminal-bench"},
    # A generator: the program `uv run` starts in a checkout of the repository that holds it, writing to --output-dir.
    "tau3-bench": {"local": "../harbor-datasets/tau3-bench",
                   "generate": {"program": "tau3-bench", "checkout": "tau2-bench checkout · dev/tau3 branch", "example": "../tau2-bench"}},
}


def payload(cat, jobs, workspace_name=""):
    from nullius import providers
    from nullius.standard import recipe as standard
    models = sorted({j.model_name for j in jobs if j.model_name})
    dated = [j for j in jobs if j.model_name and j.started_at]
    latest = max(dated, key=lambda j: j.started_at.timestamp()).model_name if dated else ""
    return {
        "version": 1, "workspace": workspace_name or "unassigned", "agents": AGENTS,
        # Harbor and the Nullius standard: the page offers choices, the core and standard/recipe.py compile them.
        "runners": [standard.RUNNER, providers.HARBOR],
        # why a saved recipe of a runner no longer offered does not open (a benchmark's own scripts: its Original setup)
        "runnerGone": providers.GONE,
        # the standard's benchmarks, agents and capabilities, and why a pair does not fit (standard/recipe.py, choices)
        "standard": standard.choices(),
        "parameters": PARAMETERS, "benchmarks": [asdict(b) | {"label": b.label} for b in cat.benchmarks],
        "datasets": [asdict(d) | LAUNCH.get(d.key, {}) for d in cat.datasets],
        "models": models, "defaultModel": latest,
        "seenLocal": sorted({j.dataset_path for j in jobs if j.dataset_path}),
        "seenRegistry": sorted({j.dataset for j in jobs if "@" in j.dataset}),
    }

