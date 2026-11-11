# Run a benchmark

One benchmark and one agent, capability, control or setup, into a job.

```bash title="Run"
nullius standard run <benchmark>[@version] <other> [--model M] [--dataset D] [--limit N] [-j N] \
    [--unsandboxed | --backend docker] [--option NAME=VALUE]... [--bench-option NAME=VALUE]... \
    [--price MODEL=INPUT,OUTPUT[,CACHED]]... [--max-usd USD] [--max-usd-per-unit USD] \
    [--repeat K] [--remove-images] [--judge-model M] [--simulator-model M] [--name NAME] [--jobs DIR] [--provider P]
```

```bash title="Examples"
nullius standard run humaneval reference --model gpt-5-mini --limit 2 --unsandboxed
nullius standard run humaneval oracle --limit 6
nullius standard run locomo original --model gpt-3.5-turbo-4k
nullius standard run locomo model --model gpt-4o-mini
```

| Argument | Meaning |
| --- | --- |
| `<benchmark>` | `KEY` or `KEY@VERSION`; a fork is a version |
| `<other>` | `KEY` or `KEY@VERSION`; `nop` or `oracle`; `original` or `model` |
| `--model` | None for a control; `original` defaults to the paper's first |
| `--provider` | `anthropic`, `custom`, `openai` or `openrouter` |
| `--dataset`, `--limit`, `-j` | One dataset, the first N units, N at a time |
| `--unsandboxed` | An agent runs as you; else `--backend docker` |
| `--backend` | `local` (default) or `docker`, each unit in a container with its relay |
| `--option NAME=VALUE` | An agent's or capability's option (repeatable) |
| `--bench-option NAME=VALUE` | A benchmark's option (repeatable), read as `NULLIUS_OPTION_<NAME>` |
| `--price MODEL=INPUT,OUTPUT[,CACHED]` | This run's price for a model in USD per million tokens (repeatable), over your [price list](../settings/model-prices.md) |
| `--max-usd USD`, `--max-usd-per-unit USD` | A spending cap for the run, for each unit: [below](#a-spending-cap) |
| `--repeat K` | Each unit K times; `result.json` carries `pass_hat_k` |
| `--remove-images` | Remove each image the run pulls or builds when its last unit ends (never with `--repeat`); by default they stay for the next run, listed and removed in Settings, [Sandbox](../settings/sandbox.md) |
| `--judge-model` | Instead of the official judge |
| `--simulator-model` | Instead of the official simulated user |
| `--name` | The job's name |
| `--jobs` | Where jobs are written (default `jobs_standard/` relative to the current directory) |
| `--from-run` | Started by Nullius: `OPENAI_API_KEY` and `OPENAI_BASE_URL` only, saved settings unread |
| `--shard I/N`, `--launch` | One shard of a launch: [Shard and merge](shard-and-merge.md) |

| Rule | Detail |
| --- | --- |
| The pair must fit | Else refused, naming the missing port |
| Keys | Environment, then user `settings.json`; the agent never gets one |
| A control | No `--model`, no `--unsandboxed`; only a judge is called |
| Machine programs | An option needing them is refused without them |
| Progress | stderr, one line per stage (`[nullius] stage=install ...`, `stage=units n=10`, `stage=done`, `stage=refused`), also `progress.jsonl` |
| The job | Opens in the app; `events.jsonl` feeds the live board |

## A spending cap

`--max-usd 5` stops a run at 5 USD and `--max-usd-per-unit 0.50` one unit at 0.50.

| Rule | Detail |
| --- | --- |
| Prices first | Every model the run calls needs a price ([price list](../settings/model-prices.md) or `--price`), else the run is refused |
| What is counted | Each reply's cost, and an estimate for each call in flight |
| At the run's cap | The call that does not fit is refused by the relay (HTTP 402), and every call after it |
| Its units | One cut off is the outcome Spending cap, outside the adjusted rate; those not begun are not run |
| At a unit's cap | Its own calls and its simulated user's count, its judge's do not. A unit that reaches its cap is an Agent error; the run goes on |
| Not exact | A call that costs more than its estimate can take the spend past a cap by up to the difference |
| Cached input | Estimated as if not cached, so a unit can be cut off short of its cap |
| The result | `result.json` carries `budget`, and the job page shows it |
| From the builder | *Limits*: one cap shared in equal parts by the run's jobs, and one per unit |
| Shards | A shard's cap is its own: [Shard and merge](shard-and-merge.md) |

<div class="grid cards" markdown>

- [The standard](../standard.md#a-run)

    Unit by unit.

- [Prove a benchmark](prove.md)

    The controls first.

- [Shard and merge](shard-and-merge.md)

    Many machines.

</div>
