# SWE-agent

Standard integration: **not yet offered**. No proven ports or instance result.
Harbor remains available.

- Upstream: v1.1.0, `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`, unchanged.
- Route: upstream `RunSingle`, local deployment, preexisting repository.
- Preparation: upstream `SWEEnv` and `ToolHandler.install`, no model.
- Agent dependencies: exact pins in `agent.json`; upstream tool installs
  remain upstream's own commands.
- Requires an own-image container with root as its default user.
- SWE-bench agent work remains offline; preparation's proxy is removed first.

Still required: workspace-kit proof with relay records, then a SWE-bench
Verified instance. These require a separately allocated spending cap. Passing
startup checks is not proof of agent capability.

Proposed bounded proof (not executed): two workspace checks, one attempt,
`--max-usd-per-unit 0.25 --max-calls 8`; at most $0.50 across those checks.
The subsequent single instance needs its own allocation. A cap refusal is
not a passing proof. Tool dependencies use pip's supported constraints file;
the upstream install scripts remain unchanged.

Preparation and offline tool installation pass on the pinned Django
`django-17087` image (Python 3.11), using SWE-ReX 1.2.1. Preparation removes
its temporary tool uploads while retaining installed dependencies;
preexisting tool directories are refused rather than overwritten. Offline
startup reaches the agent loop; the built-in empty-submit model fails the
default function-call format check and supplies no capability proof.

Legacy task interpreters remain unsupported: `django-10097` uses Python
3.5.6, but the default editor requires
[tree-sitter 0.21.3](https://pypi.org/project/tree-sitter/0.21.3/) (Python ≥3.8).
Nullius refuses that interpreter before startup. Supporting those images
needs a supported separation of tool and task interpreters; the benchmark's
Python is never replaced.

An intermittent shell-startup timeout is not established as a SWE-ReX
version defect; the 1.2.1 pin remains. Successful installation does not
establish a scored instance or port proof.
