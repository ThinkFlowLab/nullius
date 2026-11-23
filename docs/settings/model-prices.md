# Model prices

What a run's cost is estimated from, in USD per million tokens. Nullius ships no price: a new profile shows no estimate until
its list is filled once.

| Card | Does |
| --- | --- |
| Your prices | A model's price of your own: input, output and optionally cached input, cache writes and one-hour cache writes |

| Rule | |
| --- | --- |
| The list | LiteLLM's public `model_prices_and_context_window.json` |
| Fetched | Only when **Update** or `nullius prices update` asks; a run reads the saved list and never the network |
| Your prices | Win over the list. An update leaves them as they are |
| One run | `--price MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]` on [`nullius standard run`](../cli/run.md) prices that run only, over both |
| A call's cost | The provider's own figure when its reply carries one (OpenRouter's does); else the price of exactly that model name; else none, shown as — |
| Exact names | `gpt-4.1-nano` is not priced as `gpt-4.1` |
| Recorded | A job's `config.json` keeps the prices of the models it calls, each with its source (`list`, `own`, or `run` for `--price`) |
| Docker and hosts | A run on a host uses this machine's list |
| An estimate | List prices, not a bill: [Metrics](../field-guide.md) |
| A spending cap | Needs these prices: [Run a benchmark](../cli/run.md#a-spending-cap) |

## From a shell

```bash
nullius prices                       # the list's source and size, and your own prices
nullius prices list gpt-4o --json    # those models' prices
nullius prices update                # fetch the public list
nullius prices update --from FILE    # another list in the same format: a mirror, a saved copy
nullius prices set my-model 1.5 6 --cached 0.15
nullius prices remove my-model
```

Optional `--price` fields may be omitted or left empty. One-hour writes use
`cache_write_1h` when present, otherwise `cache_write`, then the input rate.
A reply without write-duration usage uses `cache_write`. `nullius prices set` also accepts `--cache-write` and
`--cache-write-1h`.

`NULLIUS_PRICES` names another price list file.
