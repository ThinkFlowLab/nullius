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

Zero-cost validation on the pinned Django SWE-bench image currently stops in
SWE-ReX 1.2.1's shell startup (`pexpect.exceptions.TIMEOUT`, waiting for
`SHELLPS1PREFIX`), before tool installation. This is a failed startup check,
not a model failure. No upstream workaround or patch is included.
