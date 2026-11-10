# Run a benchmark

One benchmark and one agent, capability, control or setup, into a job.

```bash title="Run"
nullius standard run <benchmark>[@version] <other> [--model M] [--dataset D] [--limit N] [-j N] \
    [--unsandboxed | --backend docker] [--option NAME=VALUE]... [--bench-option NAME=VALUE]... \
    [--price MODEL=INPUT,OUTPUT[,CACHED]]... [--max-usd USD] \
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
| `--max-usd USD` | The run's spending cap: no call goes out once the spend plus the calls in flight would pass it ([below](#a-spending-cap)) |
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

`--max-usd 5` stops a run at 5 USD, for a prepaid or shared key. Every relay of
the job books each call in one ledger before sending it
(`app/nullius/standard/ledger.py`).

| Rule | Detail |
| --- | --- |
| Prices first | Every model the run calls (its own, the judge, the simulated user, an option's embeddings, an agent's side models) needs a price in your [price list](../settings/model-prices.md) or a `--price`; else the run is refused before it starts |
| A booking | The most a call is likely to cost: its characters over three as input tokens, and as output the limit the call names, else the model's own (its price's output limit), else 4,096 tokens |
| Sent or refused | A call is sent only while what was spent, what the calls in flight have booked and its own booking stay within the cap. The first that does not fit is refused, and so is every call after it: the run stops at one point |
| Settled | A reply turns the booking into what the call cost. A provider's error gives the booking back. A reply with no token count, or cut off, keeps its whole booking as unsettled: it may have been billed |
| A refused call | Answered by the relay with HTTP 402, naming the relay and the cap, and recorded `refused`: never the provider's error |
| The units | One cut off by the cap ends as `SpendingCapError`: the outcome Spending cap, outside the agent's rate, never a miss. Units not begun are not run |
| The result | `result.json` carries `budget`: `max_usd`, `spent_usd`, `unsettled_usd` when there is some, `stopped`, `cut_off` and `not_run`; stderr says `stage=cap` |
| Several at a time | The calls in flight take room: with `-j 4`, four bookings must fit at once. A cap close to one call's booking stops the run early |
| An estimate | Prices are list prices and a booking is not a bill: a call that costs more than was booked takes the spend past the cap by the difference, and the next call is refused |
| A shard | The cap is that shard's alone: split a budget over the shards yourself |

<div class="grid cards" markdown>

- [The standard](../standard.md#a-run)

    Unit by unit.

- [Prove a benchmark](prove.md)

    The controls first.

- [Shard and merge](shard-and-merge.md)

    Many machines.

</div>
