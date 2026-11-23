# Task sets

```bash title="Commands"
nullius standard units <benchmark> --dataset D [--bench-option NAME=VALUE]... [--search TEXT] [--category FIELD=VALUE]... [--json]
```

```bash title="Examples"
nullius standard units osworld1 --dataset test_nogdrive --search tab --category domain=chrome
```

## Listing a dataset's tasks

| Part | Detail |
| --- | --- |
| What it gives | Each task's id, title, text and categories, the number matched of the dataset's, and each category's values with their counts |
| Title, text, categories | From the driver's [`describe`](../standard.md#driver-ops), read from the dataset's files. A driver without it gives ids alone; a search then reads the ids |
| `--search TEXT` | The tasks whose id, title or text hold each word, whatever the case |
| `--category FIELD=VALUE` | Values of one field widen the match, fields narrow it. A field no task has is refused. Each field's values are counted over the tasks the search and the other fields match |
| `--bench-option` | The options the driver lists under; the others at their defaults |
| What it needs | The benchmark's upstream, its dataset file and its driver's environment, prepared as a run prepares them when missing; with [`driver.list_command`](../standard.md#driver-ops), the upstream and the dataset file alone. No unit, VM or container is started; no licence or key is given |
| A first listing | One line on stderr names what it fetches. A program it needs that this machine lacks (`git`, `uv`, the light command's) is refused before anything is fetched |
| Before a listing | `nullius.standard.runner.units.state(benchmark, dataset, options)` says, starting and fetching nothing: `cached`, `light` (what it fetches), `full` (the driver's environment, and what it fetches), or `refused` (the reason) |
| The cache | The user cache (`nullius paths`): the same benchmark, driver, dataset and options are listed once |

Exit codes: 0 listed (no match included), 2 a usage error or a refusal (an option or a category not known, a program not on this machine, a driver that could not list), 3 no such benchmark or dataset.
