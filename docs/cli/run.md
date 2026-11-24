# Run a benchmark

One benchmark and one agent, capability, control or setup, into a job.

```bash title="Run"
nullius standard run <benchmark>[@version] <other> [--model M] [--dataset D] [--units FILE | --set NAME] [--limit N] [--start-with N|FILE|set:NAME|ids:ID,ID...] [-j N] \
    [--unsandboxed | --backend container|boat] [--option NAME=VALUE]... [--bench-option NAME=VALUE]... \
    [--price MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]]... [--max-usd USD] [--max-usd-per-unit USD] [--timeout PHASE=SECONDS]... [--max-turns N] [--max-calls N] \
    [--constraint KEY=VALUE]... \
    [--served-by [MODEL=]SLUG[,SLUG...]]... \
    [--attempts K] [--retries N] [--remove-images] [--judge-model M] [--simulator-model M] [--name NAME] [--jobs DIR] [--provider P]
```

```bash title="Examples"
nullius standard run humaneval reference --model gpt-5-mini --limit 2 --unsandboxed
nullius standard run humaneval oracle --limit 6
nullius standard run humaneval oracle --units tasks.txt
nullius standard run humaneval oracle --set first-ten
nullius standard run humaneval oracle --start-with 10
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
| `--set NAME` | Only the units of a [task set](task-sets.md), in its dataset |
| `--start-with N\|FILE\|set:NAME\|ids:ID,ID...` | Plan the whole, begin with a part: [below](#plan-the-whole-begin-with-a-part) |
| `--unsandboxed` | An agent runs as you; else `--backend container` |
| `--backend` | `local` (default); `container`, each unit in a container with its relay; or [`boat`](../standard.md#the-boat-backend), a VM per unit |
| `--boat-size` | Boat's VM size: `small` (default), `default`, `large`, `xlarge` |
| `--option NAME=VALUE` | An agent's or capability's option (repeatable) |
| `--bench-option NAME=VALUE` | A benchmark's option (repeatable) |
| `--price MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]` | This run's price for a model in USD per million tokens (repeatable), over your [price list](../settings/model-prices.md) |
| `--max-usd USD`, `--max-usd-per-unit USD` | A spending cap for the run, for each unit: [below](#a-spending-cap) |
| `--timeout PHASE=SECONDS`, `--max-turns N` | A unit's time limit for a phase, a conversation's most turns, forced: [below](#time-limits) |
| `--max-calls N` | The most model calls a unit's agent may make: [below](#a-cap-on-model-calls) |
| `--constraint KEY=VALUE` | What the machine allows the run (repeatable): `cpus`, `memory_mb`, `processes`, `cpu_set`, `concurrency`, `gpu_ids` (indices, empty for none); [constraints](../settings/run-hosts.md#constraints). A recipe passes each machine's own |
| `--served-by [MODEL=]SLUG[,SLUG...]` | On OpenRouter, the serving providers that may answer a model (repeatable): [below](#serving-providers) |
| `--attempts K` | Each unit K times; `result.json` carries `pass_hat_k` |
| `--retries N` | Run a failed attempt again, up to N times: [below](#retries) |
| `--remove-images` | Remove each image the run pulls or builds when its last unit ends (never with `--attempts`); by default they stay for the next run |
| `--judge-model` | Instead of the official judge |
| `--simulator-model` | Instead of the official simulated user |
| `--name` | The job's name |
| `--jobs` | Where jobs are written (default `jobs_standard/` relative to the current directory) |
| `--from-run` | Started by Nullius: `OPENAI_API_KEY` and `OPENAI_BASE_URL` only, saved settings unread |
| `--shard I/N`, `--launch` | One shard of a launch: [Shard and merge](shard-and-merge.md) |

| Rule | Detail |
| --- | --- |
| The pair must fit | Else refused, naming the missing port |
| The model | Refused when the provider's model list lacks it |
| Keys | Environment, then user `settings.json`; the agent never gets one |
| A data provider's token | `HF_TOKEN` from the environment, then Settings → Data providers, for a benchmark that declares Hugging Face; only its driver gets it. Not with `--from-run` |
| A control | No `--model`, no `--unsandboxed`; only a judge is called |
| Machine programs | An option needing them is refused without them |
| Progress | stderr, one line per stage (`[nullius] stage=install ...`, `stage=units n=10`, `stage=done`, `stage=refused`), also `progress.jsonl` |
| The job | Opens in the app |

## Time limits

Each phase of a unit has a time limit. `--timeout PHASE=SECONDS` (repeatable) forces one, the same for every participant, over the task's, the setup's, the benchmark's and the default.

| Phase | Default | A unit past it |
| --- | --- | --- |
| `setup` | The benchmark's `limits.setup_seconds` for an agent's unit, else 600 s; an own image's `prepare` step its `seconds` | Environment setup error |
| `image` | 3600 s: an own image's pull or build | Environment setup error |
| `agent` | The task's `agent_seconds`, else the Original setup's `turn_seconds`, else `limits.turn_seconds`, else 900 s: the agent's whole work on the unit, every turn of a conversation and the simulated user's between them; each add and each recall of a capability (its reader has `limits.turn_seconds`); an Original setup's answerer; the oracle | Agent timeout |
| `verify` | The verify step's `seconds`, else the agent's default | Verifier error |
| `score` | The benchmark's `limits.score_seconds`, else 600 s: the driver's `score` | Verifier error |

`--max-turns N` caps a conversation's agent turns over `limits.turns` (default 20); the conversation is scored as it stands. `config.json` records what was forced under `standard.timeouts` and `standard.max_turns`.

## A cap on model calls

`--max-calls 60` lets a unit's agent make at most 60 model calls, the whole unit through, every turn of a conversation. Agents only: a capability run refuses it.

| Rule | Detail |
| --- | --- |
| What is counted | Each call the provider answered on the unit's agent route. A refused call (a 429, an error) is not one; the judge's and the simulated user's never are |
| Past it | The unit ends as `UnitCallCapError`, an Agent error, and is not checked. The run goes on |
| Not exact | Calls in flight together may pass it by the few already sent |
| The upstream's own | A step limit of the benchmark's (τ³-bench's `--max-steps`, an OSWorld agent's `--max_steps`) still holds; this cap is on top |
| The result | `config.json` records it under `standard.max_calls` |

## A spending cap

`--max-usd 5` stops a run at 5 USD and `--max-usd-per-unit 0.50` one unit at 0.50.

| Rule | Detail |
| --- | --- |
| Prices first | The [price list](../settings/model-prices.md) is fetched first; a model it lacks needs `--price`, else the run is refused. Offline and unpriced: no cap, and the run says so |
| What is counted | Each reply's cost, and an estimate for each call in flight |
| At the run's cap | The call that does not fit is refused by the relay (HTTP 402), and every call after it |
| Its units | One cut off is the outcome Spending cap, outside the adjusted rate; those not begun are not run |
| At a unit's cap | Its own calls and its simulated user's count, over all its tries; its judge's do not. A unit that reaches its cap is an Agent error; the run goes on |
| Not exact | A call that costs more than its estimate can take the spend past a cap by up to the difference |
| Cached input | Estimated as if not cached, so a unit can be cut off short of its cap |
| The result | `result.json` carries `budget` |
| From the builder | *Limits*: one cap shared in equal parts by the run's jobs, and one per unit |
| Later parts | The job's one cap: each part has what the parts before it left ([Run tasks again](rerun.md)) |
| Shards | A shard's cap is its own: [Shard and merge](shard-and-merge.md) |

## Retries

`--retries N` (at most 9) runs a failed attempt again after the other units; the last try counts.

| Failure | Retried |
| --- | --- |
| Rate limit | After the [pause](../standard.md#a-run) |
| Provider, network or setup error; agent crash; setup timeout | Yes |
| Run or check out of time | Only if the provider was refusing the calls |
| Wrong key, spent credit | No; three in a row stop the run |
| A setup this machine lacks, a missing secret, a cap reached, Stop, a unit a guard asked to stop, a full context window, failed task | No |

`conform` and `prove` retry a rate limit once. Chosen tasks of an ended job run again by hand: [Run tasks again](rerun.md).

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
| Where it is read | On the machine the command runs on, from the directory it starts in |
| The builder | A recipe has no field for a list |
| A named list | A [task set](task-sets.md) keeps one, frozen: `--set NAME` |

## Plan the whole, begin with a part

`--start-with` plans the whole run and begins only a part of it; the rest stay planned, run later in the same job by [`rerun`](rerun.md).

| The part | Begins |
| --- | --- |
| `N` | The plan's first N units, in its order |
| `FILE` | The units a file lists, as [a list of units](#a-list-of-units) |
| `set:NAME` | The units of a [task set](task-sets.md) of the run's dataset |
| `ids:ID,ID,...` | The units named, separated by commas with no space |

| Rule | Detail |
| --- | --- |
| Refused | A unit outside the plan, more units than it holds, another dataset's set, or `--shard`. |
| Attempts | A unit begun gets all its `--attempts`; a unit not begun has none. |
| The record | `config.json` keeps the plan's size, `datasets[0].n_tasks`, and the part, `standard.start_with`. |
| The rates | Over the tasks run; the job's page and `runs show` say how many have run. |

## Serving providers

On OpenRouter one model is served by several providers. They may differ in precision, speed and price. `--served-by deepinfra` lets only DeepInfra answer the run's model; `--served-by m=deepinfra/fp8,together` lets DeepInfra's fp8 endpoint answer the model `m`, then Together when it cannot.

```bash title="One provider for the run's model"
nullius standard run humaneval reference --provider openrouter --model meta-llama/llama-3.3-70b-instruct --served-by deepinfra --unsandboxed
```

| Rule | Detail |
| --- | --- |
| A slug | As the model's page on OpenRouter names its provider: `deepinfra`, or a provider and its variant, a quantisation (`deepinfra/fp8`), a speed (`deepinfra/turbo`) or a region (`google-vertex/us-central1`). Several are tried in their order, and no other provider answers |
| Without `MODEL=` | The run's model. With it, any model the run calls: its judge, its simulated user, an option's embeddings |
| What is sent | `provider: {"order": [...], "allow_fallbacks": false}` on every request sent as that model, on every route, in place of the agent's own routing field. |
| Refused before any unit | A provider other than `openrouter`; a model the run does not call; a slug that is empty, malformed or given twice; a model named twice |
| No provider can serve it | OpenRouter answers 404 (no endpoint found) and nothing else is tried; the call fails as any refused call does |
| The record | `config.json`: `standard.served_by`, the slugs by model. Each call in `agent/calls.jsonl`: `request.routing`, `request.routing_asked` when the agent sent its own, `reply.served_by`, `reply.end` and `reply.error` ([Who served a call](../standard.md#a-run)) |
| Who served the run | Each trial's `result.json`, `standard.served`, and the job's `result.json`, `served`: the answered calls of every route (the agent's, the judge's, the simulated user's, the benchmark's own relay's) by serving provider, those that named none, and for each pinned model `held`: `true` when every answered call names a provider that reads as a pinned slug, `unknown` when one names none or another name, `null` with no answered call. The tokens and the cost stay the agent's own calls' |
| What `held` reads | The provider's name alone: a reply names no endpoint variant, and `held` for `deepinfra/fp8` is true for any endpoint of `deepinfra`. A reply gives the provider's display name. Where that name is not the slug once lowercased, `held` reads `unknown` though the provider served the call (`Google` for `google-vertex`, `SambaNova` for `sambanova-turbo`) |
| Shards | `merge` refuses shards whose pins differ, unless `--force`; a model pinned differently in the merged shards reads `held: unknown` |
| The builder | *Model* step, **Served by** after each model field, on OpenRouter alone: the recipe's `served_by`, by field (`model`, `simulator`, `judge`), compiled into `--served-by` for each job that calls that model. |

<div class="grid cards" markdown>

- [The standard](../standard.md#a-run)

    Unit by unit.

- [Prove a benchmark](prove.md)

    The controls first.

- [Shard and merge](shard-and-merge.md)

    Many machines.

</div>
