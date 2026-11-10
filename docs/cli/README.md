# CLI

One page per command of Nullius's shell interface; `--help` on any command gives its arguments.

After [installation](../getting-started.md), use `nullius` from any directory.
`python -m nullius` is identical when that Python environment contains Nullius.
An installation made by `uv tool install` has its own interpreter: use its
`nullius` command, not an unrelated system Python.

From a source checkout, replace `nullius` with
`PYTHONPATH=app python3 -m nullius`. To serve from source with Flask, the existing
`uv run --no-project --with-requirements app/requirements.txt app/app.py` command
also works.

| Page | Command |
| --- | --- |
| [Launch](../getting-started.md) | `nullius serve [roots...]` |
| [Read jobs](read-jobs.md) | `nullius runs`, `nullius compare`, `nullius workspace`, `nullius standard traces` |
| [Import a run](import.md) | `nullius import` |
| [Definitions](definitions.md) | `nullius standard list`, `check`, `fit`, `new` |
| [Run a benchmark](run.md) | `nullius standard run` |
| [Prove a benchmark](prove.md) | `nullius standard prove` |
| [Conform an agent or a capability](conform.md) | `nullius standard conform` |
| [Fidelity](fidelity.md) | `nullius standard fidelity` |
| [Shard and merge](shard-and-merge.md) | `nullius standard run --shard`, `merge` |
| [Storage](../settings/README.md#storage) | `nullius paths [--json]` |
| [Model prices](../settings/model-prices.md) | `nullius prices [list\|update\|set\|remove]` |
| [Migration](../getting-started.md#migrate-an-existing-checkout) | `nullius migrate --from CHECKOUT [--secrets PATH] [--apply]` |
| [Pages from a shell](pages.md) | |
| [Exit codes and output](exit-codes.md) | |

A Harbor recipe runs by its generated script, not a command: [Run builder](../builder.md).

The module CLIs remain available from an installed interpreter or with
`PYTHONPATH=app` in a checkout. The unified command loads saved settings;
`standard --from-run` uses only the supplied run environment.
