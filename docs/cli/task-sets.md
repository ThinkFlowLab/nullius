# Task sets

A group of a benchmark's units, frozen once made and run as the benchmark's own dataset is: a run names the set, gives its own options, and its job records which set ran. A set holds no option and no setup. Another list of tasks is another set.

```bash title="Commands"
nullius standard units <benchmark> --dataset D [--bench-option NAME=VALUE]... [--search TEXT] [--category FIELD=VALUE]... [--json]
nullius standard set create <benchmark>[@version] "WORDS" [--dataset D] (--units FILE | --search TEXT | --category FIELD=VALUE... \
    | --from-job JOB_DIR [--outcome O,...]... [--unit ID]...) [--description TEXT]
nullius standard set list [<benchmark>] [--json]
nullius standard set show <benchmark> NAME [--json]
nullius standard set rename <benchmark> NAME "WORDS"
nullius standard set delete <benchmark> NAME
nullius standard set import PATH
nullius standard run <benchmark> <other> --set NAME ...
```

```bash title="Examples"
nullius standard units osworld1 --dataset test_nogdrive --search tab --category domain=chrome
nullius standard set create humaneval "first ten" --dataset humaneval --units ten.txt
nullius standard set create osworld1@044bc33 "Chrome tabs" --dataset test_nogdrive --search tab --category domain=chrome
nullius standard set create humaneval misses --from-job jobs_standard/JOB --outcome failed,infra
nullius standard set rename humaneval misses "misses of June"
nullius standard run humaneval reference --model gpt-5-mini --set first-ten --unsandboxed
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

## The set

| Part | Detail |
| --- | --- |
| The entry | `sets/<benchmark>/NAME.json` in the user catalogue (`nullius paths`, `NULLIUS_CATALOGUE`): a plain file to copy or keep in git |
| What it holds | An id made at creation, the label and the name made from it, the description, the benchmark, the version of the benchmark it was made at, the dataset, and one list in `revisions`: its number, date, note, unit ids and a content hash over the benchmark, the dataset and the sorted ids. An entry holding any other field is refused where it is read. One of the earlier format, which kept a run's options: delete it and make it again |
| Frozen | Nothing changes a set's list once made. |
| Its name | Any words, at most 80 characters, with a letter or a digit: the label every page shows. The name commands take is made from it: its Latin letters and digits in lower case, every other run of characters one hyphen ("custom task set" gives `custom-task-set`), `-2` and on for a second equal one |
| Its version | The version of the benchmark it was made at: the one `create` names (`osworld1@044bc33`), the page listed, or the job ran. The same list may hold other tasks at another version |
| Identity | Two runs ran the same set when their jobs name the same id. Two sets of equal lists stay two sets |

| Command | Rule |
| --- | --- |
| `create --units FILE` | The file's ids, read as [`run --units`](run.md#a-list-of-units) reads them; `--dataset` is needed |
| `create --search`, `--category` | The ids [the listing](#listing-a-datasets-tasks) matches that day, under the benchmark's default options; `--dataset` is needed. The list's note keeps the filter. No match is refused |
| `create --from-job JOB_DIR` | The units a standard job ran, by each trial's full unit id; the job's dataset. `--outcome` takes the trials of those outcomes ([outcome names](../field-guide.md#outcomes), or `passed`, `failed`, `infra`), `--unit` adds the units named; neither takes every unit. A unit is taken when any of its attempts is |
| `show NAME` | Its label, version, id, date and units |
| `rename NAME WORDS` | The same set under another label, its id and list kept, and the name made from it again: a job that ran it finds it by its id and names it by its new label |
| `delete NAME` | The entry removed. A job that ran it keeps its record |
| `import PATH` | An entry's file, or a job that ran a set (its description then empty, its list dated by the import), written when no set of that name is here; the same id with the same list adds nothing. Refused: another id under that name, or the same id with another list |
| Not checked | The ids, until a run: the driver lists the units then |

## A run of a set

| Rule | Detail |
| --- | --- |
| Its dataset | The set's. A `--dataset` of another is refused before any unit |
| Its options | The run's own, any `--bench-option` as on the whole dataset |
| `--units` | Refused beside `--set` |
| The units | The set's ids, as a [list of units](run.md#a-list-of-units): ids the driver does not list under the run's options refuse the run before any unit, the message giving how many and the first five; then `--shard`, then `--limit` |
| Another version | A run at another version than the set's, whose dataset lacks one of its tasks, is refused before any unit: the message names the task and both versions |
| The record | `config.json`: `standard.task_set`, the set's id, name, label, the number and hash of its list, its units and its version |
| Shards | `merge` refuses shards of two sets, or a set and none, unless `--force`; the merged job keeps the first shard's set ([Shard and merge](shard-and-merge.md)) |
| On a host | From the app: the runner snapshot sent to the host carries the recipe's sets, and the run there reads them from its copy of the catalogue. By hand: copy the entry, or `set import` a job that ran it |

Exit codes: 0 done, 2 a usage error or a refusal, 3 no such benchmark, set, file or job ([Exit codes and output](exit-codes.md)).
