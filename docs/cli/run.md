# Run a benchmark

One benchmark and one agent, capability, control or setup, into a job.

```bash title="Run"
nullius standard run <benchmark>[@version] <other> [--model M] [--dataset D] [--units FILE] [--limit N] [-j N] \
    [--unsandboxed | --backend container] [--option NAME=VALUE]... [--bench-option NAME=VALUE]... \
    [--price MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]]... [--served-by [MODEL=]SLUG[,SLUG...]]... \
    [--max-usd USD] [--max-usd-per-unit USD] \
    [--repeat K] [--remove-images] [--judge-model M] [--simulator-model M] [--name NAME] [--jobs DIR] [--provider P]
```

```bash title="Examples"
nullius standard run humaneval reference --model gpt-5-mini --limit 2 --unsandboxed
nullius standard run humaneval oracle --limit 6
nullius standard run humaneval oracle --units tasks.txt
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
| `--units FILE` | Only the units the file lists: [below](#a-list-of-units) |
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

## A list of units

`--units tasks.txt` runs only the units the file lists.

```text title="tasks.txt"
# one unit id a line, as the benchmark names it
HumanEval/0
HumanEval/12
```

| Rule | Detail |
| --- | --- |
| The file | One id a line, the spaces around it dropped; blank lines and lines that start with `#` are skipped |
| Refused before any unit starts | An id the dataset does not hold, the ids named; an id listed twice; a file that lists none |
| Order | The dataset's, whatever the file's; each unit keeps its place in the whole dataset (`HumanEval/12` is `u0012`) |
| With `--limit N` | The first N of the listed units, in the dataset's order |
| With `--shard I/N` | The listed units of that shard; `standard.shard.units_total` counts the listed units; `merge` compares the units each list holds and refuses shards whose lists differ, even with `--force` |
| The record | `config.json`: `standard.units_file`, the file's name, its sha256 and its units |
| Where it is read | On the machine the command runs on, from the directory it starts in, with `--from-run` or without |
| The builder | A recipe has no field for a list |

## Serving providers

On OpenRouter one model is served by several providers. They may differ in precision, speed and price. `--served-by deepinfra` lets only DeepInfra answer the run's model; `--served-by m=deepinfra/fp8,together` lets DeepInfra's fp8 endpoint answer the model `m`, then Together when it cannot.

```bash title="One provider for the run's model"
nullius standard run humaneval reference --provider openrouter --model meta-llama/llama-3.3-70b-instruct --served-by deepinfra --unsandboxed
```

| Rule | Detail |
| --- | --- |
| A slug | As the model's page on OpenRouter names its provider: `deepinfra`, or a provider and its variant, a quantisation (`deepinfra/fp8`), a speed (`deepinfra/turbo`) or a region (`google-vertex/us-central1`). Several are tried in their order, and no other provider answers |
| Without `MODEL=` | The run's model. With it, any model the run calls: its judge, its simulated user, an option's embeddings |
| What is sent | `provider: {"order": [...], "allow_fallbacks": false}` on every request sent as that model, on every route, in place of the agent's own routing field. A pinned Chat Completions or Responses request to OpenRouter also asks for its routing facts (`X-OpenRouter-Metadata: enabled`). A Responses reply names the provider that served it there alone. A Messages request is sent without the header, since its reply names its provider. A model with no pin is sent as before |
| Refused before any unit | A provider other than `openrouter`; a model the run does not call (a control calls only its judge's or an option's, never a simulated user's); a slug that is empty, malformed or given twice; a model named twice. The run, the command line and the relay's own command make the same check |
| No provider can serve it | OpenRouter answers 404 (no endpoint found) and nothing else is tried; the call fails as any refused call does |
| The record | `config.json`: `standard.served_by`, the slugs by model. Each call in `agent/calls.jsonl`: `request.routing` and, when the agent sent its own, `request.routing_asked`; `reply.served_by`, the provider the reply names (for a Responses reply, the endpoint its routing facts mark as selected), `reply.end` (one per choice, by index, when a reply has several) and `reply.error`, an error a reply holds under a 200: the top-level one, else that of the first choice that has one, its code, type and message alone. These are read apart from the rest of the record and cleaned of credentials. The call keeps its status, and the unit's verdict reads it as before |
| Who served the run | Each trial's `result.json`, `standard.served`, and the job's `result.json`, `served`: the answered calls of every route (the agent's, the judge's, the simulated user's, the benchmark's own relay's) by serving provider, those that named none, and for each pinned model `held`: `true` when every answered call names a provider that reads as a pinned slug, `unknown` when one names none or another name, `null` with no answered call. A name reads as a slug lowercased with its spaces as dashes (`DeepInfra` is `deepinfra`, the first part of `deepinfra/turbo`), and nothing else is folded. The tokens and the cost stay the agent's own calls' |
| What `held` reads | The provider's name alone: a reply names no endpoint variant, and `held` for `deepinfra/fp8` is true for any endpoint of `deepinfra`. A reply gives the provider's display name. Where that name is not the slug once lowercased, `held` reads `unknown` though the provider served the call (`Google` for `google-vertex`, `SambaNova` for `sambanova-turbo`) |
| Shards | `merge` refuses shards whose pins differ, unless `--force`; a model pinned differently in the merged shards reads `held: unknown` |
| The builder | *Model* step, **Served by** under each model field, on OpenRouter alone: the recipe's `served_by`, by field (`model`, `simulator`, `judge`), compiled into `--served-by` for each job that calls that model. Its list of a model's providers comes from OpenRouter (`/models/{maker}/{model}/endpoints`), asked as the model list is; when it cannot be had, a slug typed by hand is taken |

<div class="grid cards" markdown>

- [The standard](../standard.md#a-run)

    Unit by unit.

- [Prove a benchmark](prove.md)

    The controls first.

- [Shard and merge](shard-and-merge.md)

    Many machines.

</div>
