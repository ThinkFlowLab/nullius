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

Zero-cost validation on the pinned Django `django-10097` image reaches
upstream tool installation with SWE-ReX 1.2.1 and 1.4.0, but cannot complete:
the task uses Python 3.5.6; the default editor requires
[tree-sitter 0.21.3](https://pypi.org/project/tree-sitter/0.21.3/) (Python ≥3.8).
Nullius now refuses that incompatible task interpreter before startup.
This is an upstream tool/task-runtime incompatibility, exposed by our local
deployment configuration; it is not a benchmark or model failure.

The earlier intermittent shell-startup timeout is not established as a
SWE-ReX version defect. Neither a dependency upgrade nor a conda prompt
setting is proven necessary; the 1.2.1 pin remains. Full preparation and its
offline repeat remain unproven. Supporting this image needs a supported way
to separate tool and task interpreters without changing upstream tools or
the benchmark environment.
