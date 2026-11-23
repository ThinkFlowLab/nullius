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
| Shards by the unit's id | `sha256(unit id) mod N`: every unit in exactly one shard, new units leave the rest in place; changing N moves most. Order: `units`, the units `--units FILE` lists, the shard, then `--limit` (`--shard 1/2 --limit 6`: the first six of shard 1) |
| A unit keeps its place | `u0042` is the 43rd unit of the dataset in every shard; shard jobs never share a trial name |
| One launch | Each shard's `config.json` records `standard.shard` (`index`, `of`, `units_total`, `partition`, `planned`, `limit`) and `standard.launch`: `--launch`, or the day (UTC), benchmark, agent or capability and model. The runs page groups them until merged, then counts the merged job alone; source job files are kept |
| `merge JOB_DIR... -o OUT` | One job with its `result.json` recomputed. Different launches, versions, datasets, agents, models, backends, N or task sets refused unless `--force`; lists of different units and two trials of one unit always. Units no shard ran are named |
| A list of units | `--units FILE` on every machine: [Run a benchmark](run.md#a-list-of-units). A list's file name, comments and sha256 may differ from shard to shard; the merged job records the first shard's list |
| A task set | `--set NAME` on every machine, each with the entry: shards of two sets, or a set and none, are refused unless `--force`; the merged job keeps the first shard's set ([Task sets](task-sets.md)) |
| A spending cap | Each shard's own; the builder shares a recipe's cap over its shards. `merge` adds them into one `budget` when every shard had one |
| `prove --shard I/N` | Controls and leak on that shard, units on all; recorded as `shard` |

The builder's *Where* does the same: one shard per machine.

<div class="grid cards" markdown>

- [Run a benchmark](run.md)

    The command each machine runs.

- [The standard](../standard.md#many-machines)

    The same rules.

</div>
