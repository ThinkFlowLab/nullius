# Run a benchmark

One benchmark and one agent, capability, control or setup, into a job.

```bash title="Run"
nullius standard run <benchmark>[@version] <other> [--model M] [--dataset D] [--limit N] [-j N] \
    [--unsandboxed | --backend container] [--option NAME=VALUE]... [--bench-option NAME=VALUE]... \
    [--price MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]]... [--served-by [MODEL=]SLUG[,SLUG...]]... \
    [--max-usd USD] [--max-usd-per-unit USD] \
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
| `--unsandboxed` | An agent runs as you; else `--backend container` |
| `--backend` | `local` (default) or `container`, each unit in a container with its relay |
| `--option NAME=VALUE` | An agent's or capability's option (repeatable) |
| `--bench-option NAME=VALUE` | A benchmark's option (repeatable), read as `NULLIUS_OPTION_<NAME>` |
| `--price MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]` | This run's price for a model in USD per million tokens (repeatable), over your [price list](../settings/model-prices.md) |
| `--served-by [MODEL=]SLUG[,SLUG...]` | On OpenRouter, the serving providers that may answer a model (repeatable): [below](#serving-providers) |
| `--max-usd USD`, `--max-usd-per-unit USD` | A spending cap for the run, for each unit: [below](#a-spending-cap) |
| `--repeat K` | Each unit K times; `result.json` carries `pass_hat_k` |
| `--remove-images` | Remove each image the run pulls or builds when its last unit ends (never with `--repeat`); by default they stay for the next run |
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

## Serving providers

On OpenRouter one model is served by several providers, which may differ in precision, speed and price. `--served-by deepinfra` lets only DeepInfra answer the run's model; `--served-by m=deepinfra/fp8,together` lets DeepInfra's fp8 endpoint answer the model `m`, then Together when it cannot.

```bash title="One provider for the run's model"
nullius standard run humaneval reference --provider openrouter --model meta-llama/llama-3.3-70b-instruct --served-by deepinfra --unsandboxed
```

| Rule | Detail |
| --- | --- |
| A slug | As the model's page on OpenRouter names its provider: `deepinfra`, `deepinfra/fp8`. Several are tried in their order, and no other provider answers |
| Without `MODEL=` | The run's model. With it, any model the run calls: its judge, its simulated user, an option's embeddings |
| What is sent | `provider: {"order": [...], "allow_fallbacks": false}` on every request sent as that model, on every route, in place of the agent's own routing field. A Chat Completions request also asks for OpenRouter's routing facts (`X-OpenRouter-Metadata: enabled`), where its reply names the provider that served it. A model with no pin is sent as before |
| Refused before any unit | A provider other than `openrouter`; a model the run does not call; a slug that is empty, malformed or given twice; a model named twice |
| No provider can serve it | OpenRouter answers with an error and nothing else is tried; the call fails as any refused call does |
| The record | `config.json`: `standard.served_by`, the slugs by model. Each call in `agent/calls.jsonl`: `request.routing` and, when the agent sent its own, `request.routing_asked`; `reply.served_by`, `reply.end` and `reply.error` |
| Who served the run | Each trial's `result.json`, `standard.served`, and the job's `result.json`, `served`: the answered calls by serving provider, those that named none, and for each pinned model `held`: `true` when every answered call names a provider that reads as a pinned slug (lowercased, its words joined by dashes: `Google AI Studio` is `google-ai-studio`), `unknown` when one names none or another name, `null` with no answered call |
| Shards | `merge` refuses shards whose pins differ, unless `--force` |
| The builder | A recipe has no field for it yet: a pinned run starts from the command line |

<div class="grid cards" markdown>

- [The standard](../standard.md#a-run)

    Unit by unit.

- [Prove a benchmark](prove.md)

    The controls first.

- [Shard and merge](shard-and-merge.md)

    Many machines.

</div>
