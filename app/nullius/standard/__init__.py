"""The Nullius standard: benchmarks, agents and capabilities, matched by ports.

A benchmark *needs* ports, an agent or a capability *offers* them, and a pair fits
when every port the benchmark needs is offered and proven (``contract.match``). A port
is a versioned contract (``contract.ports``) with a conformance kit, a tiny benchmark
run against the agent or capability before its claim is written down
(``proofs.conform``). The definitions are files in the catalogue (``contract.definitions``).

A run (``runner.run``) sets each unit up with the benchmark's driver (``protocols.jsonl``),
drives the agent over ACP (``protocols.acp``) with the benchmark's environment as MCP
servers, sends every model call through a relay that holds the key and records
the call (``calls.relay``), scores the unit with the driver, and writes a job in
Harbor's shape (``runner.job``) that the rest of the application reads unchanged.
``docs/standard.md`` is the design.

One folder per purpose: ``contract`` (what a definition may say, which pairs fit),
``requirements`` (what a run needs of the machine), ``protocols`` (ACP, JSON
lines, the computer port's server), ``calls`` (the relay and the spending cap),
``backends`` (a container or a Boat VM for each unit, and the doors through a
container), ``runner`` (a run, on one machine or several), ``proofs`` (kits,
fidelity, a benchmark's controls) and ``pages`` (the builder's recipe, the How
to add steps). ``cli`` is the shell command over all of them; ``kits/`` holds the
conformance kits, each a benchmark.

A new module goes in the folder of its purpose, not beside ``cli``, and no
folder imports one that imports it (``tests/test_layering.py``).
"""
