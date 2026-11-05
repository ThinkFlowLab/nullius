"""The Nullius standard: benchmarks, agents and capabilities, matched by ports.

A benchmark *needs* ports, an agent or a capability *offers* them, and a pair fits
when every port the benchmark needs is offered and proven (``match``). A port
is a versioned contract (``ports``) with a conformance kit, a tiny benchmark
run against the agent or capability before its claim is written down
(``conform``). The definitions are files in the catalogue (``definitions``).

A run (``run``) sets each unit up with the benchmark's driver (``jsonl``),
drives the agent over ACP (``acp``) with the benchmark's environment as MCP
servers, sends every model call through a relay that holds the key and records
the call (``relay``), scores the unit with the driver, and writes a job in
Harbor's shape (``job``) that the rest of the application reads unchanged.
``docs/standard.md`` is the design.
"""
