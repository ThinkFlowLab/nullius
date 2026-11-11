# Shard and merge

N machines run the same command, each with `--shard I/N`, and one `merge`.

```bash title="Two machines, one merge"
nullius standard run humaneval oracle --shard 1/2 --limit 6   # machine 1
nullius standard run humaneval oracle --shard 2/2 --limit 6   # machine 2
nullius standard merge jobs_standard/*__shard-* -o jobs_standard/humaneval-oracle
```

| Rule | Detail |
| --- | --- |
| No central relay | Each unit's relay runs on its machine with its own key; a provider's limit is per key |
| Shards by the unit's id | `sha256(unit id) mod N` (`app/nullius/standard/shard.py`): every unit in exactly one shard, new units leave the rest in place; changing N moves most. Order: `units`, the shard, then `--limit` (`--shard 1/2 --limit 6`: the first six of shard 1) |
| A unit keeps its place | `u0042` is the 43rd unit of the dataset in every shard; shard jobs never share a trial name |
| One launch | Each shard's `config.json` records `standard.shard` (`index`, `of`, `units_total`, `partition`, `planned`, `limit`) and `standard.launch`: `--launch`, or the day (UTC), benchmark, agent or capability and model. The runs page groups them until merged |
| `merge JOB_DIR... -o OUT` | One job (`app/nullius/standard/merge.py`): trials hard-linked, each `result.json` naming the merged job and shard; the job's `result.json` recomputed; `config.json` with `merged_from` for `shard`. Different launches, versions, datasets, agents, models, backends or N refused unless `--force`; two trials of one unit always. Units no shard ran are named |
| A spending cap | `--max-usd` is each shard's own; the builder shares a recipe's cap over its shards in equal parts. `merge` adds the caps and what was spent into the merged job's `budget`, with every unit cut off or not run, when every shard had a cap. A cap per unit, the same on every shard, is kept with the units that reached it |
| `prove --shard I/N` | Controls and leak on that shard, units on all; recorded as `shard` |

The builder's *Where* does the same: one shard per machine.

<div class="grid cards" markdown>

- [Run a benchmark](run.md)

    The command each machine runs.

- [The standard](../standard.md#many-machines)

    The same rules.

</div>
