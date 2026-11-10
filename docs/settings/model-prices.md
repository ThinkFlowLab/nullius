# Model prices

What a run's cost is estimated from, in USD per million tokens. Under
**Evaluation**. Nullius ships no price: a new profile shows no estimate until
its list is filled once.

| Card | Does |
| --- | --- |
| Price list | How many models are priced and the day the list was fetched; **Update** fetches it again |
| Your prices | A model's price of your own: input, output and optionally cached input; **Remove** takes one out |

| Rule | |
| --- | --- |
| The list | LiteLLM's public `model_prices_and_context_window.json`, keyed by the model names the providers themselves use, kept whole with its address and day |
| Fetched | Only when **Update** or `nullius prices update` asks; a run reads the saved list and never the network |
| Your prices | Win over the list: a model it lacks, a negotiated price, a model served under your own name. An update leaves them as they are |
| One run | `--price MODEL=INPUT,OUTPUT[,CACHED]` on [`nullius standard run`](../cli/run.md) prices that run only, over both |
| A call's cost | The provider's own figure when its reply carries one (OpenRouter's does); else the price of exactly that model name; else none, shown as — |
| Exact names | `gpt-4.1-nano` is not priced as `gpt-4.1`: a model whose name only starts like another's is another model |
| Recorded | A job's `config.json` keeps the prices of the models it calls, each with its source (`list`, `own`, or `run` for `--price`), and where and when the list was fetched when a price is the list's |
| Docker and hosts | A unit's relay in a container gets its models' prices from the run; a run on a host takes this machine's list with it |
| An estimate | List prices, not a bill: [Metrics](../field-guide.md) |
| Code | `app/nullius/prices.py` |

## From a shell

```bash
nullius prices                       # the list's source and size, and your own prices
nullius prices list gpt-4o --json    # those models' prices
nullius prices update                # fetch the public list
nullius prices update --from FILE    # another list in the same format: a mirror, a saved copy
nullius prices set my-model 1.5 6 --cached 0.15
nullius prices remove my-model
```

The list is `prices.json` in the configuration directory
([Storage](README.md#storage)); `NULLIUS_PRICES` names another file.
