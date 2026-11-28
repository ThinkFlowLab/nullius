# CLI

One page per command of Nullius's shell interface; `--help` on any command gives its arguments.

After [installation](../getting-started.md), use `nullius` from any directory.

From a source checkout, replace `nullius` with
`PYTHONPATH=app python3 -m nullius`.

| Page | Command |
| --- | --- |
| [Launch](../getting-started.md) | `nullius serve [roots...]` |
| [Read jobs](read-jobs.md) | `nullius runs`, `nullius compare`, `nullius leaderboard`, `nullius workspace`, `nullius standard traces` |
| [Import a run](import.md) | `nullius import` |
| [Task flags](flags.md) | `nullius flags list`, `set`, `accept`, `remove`, `copy`, `import`, `feasibility` |
| [Definitions](definitions.md) | `nullius standard list`, `check`, `fit`, `pins`, `new` |
| [Run a benchmark](run.md) | `nullius standard run` |
| [Prove a benchmark](prove.md) | `nullius standard prove` |
| [Conform an agent or a capability](conform.md) | `nullius standard conform` |
| [Fidelity](fidelity.md) | `nullius standard fidelity` |
| [Shard and merge](shard-and-merge.md) | `nullius standard run --shard`, `merge` |
| [Task sets](task-sets.md) | `nullius standard units`; `set create`, `list`, `show`, `rename`, `delete`, `import`; `run --set` |
| [Run tasks again](rerun.md) | `nullius standard rerun` |
| [Storage](../settings/README.md#storage) | `nullius paths [--json]` |
| [Model prices](../settings/model-prices.md) | `nullius prices [list\|update\|set\|remove]` |
| [Migration](../getting-started.md#migrate-an-existing-checkout) | `nullius migrate --from CHECKOUT [--secrets PATH] [--apply]` |
| [Provider records](../settings/model-providers.md#older-settings) | `nullius migrate --providers [--apply]` |
| [From another machine](../api.md) | `nullius access`, `nullius remote`, `nullius mcp` |
| [Pages from a shell](pages.md) | |
| [Exit codes and output](exit-codes.md) | |

A Harbor recipe runs by its generated script, not a command: [Run builder](../builder.md).
