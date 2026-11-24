# Task sets

A named list of one dataset's units, frozen once made, that a run names and its job records.

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
nullius standard set create humaneval misses --from-job jobs_standard/JOB --outcome failed,infra
nullius standard run humaneval reference --model gpt-5-mini --set first-ten --unsandboxed
```

| Rule | Detail |
| --- | --- |
| `units` | Each task's id, title, text and categories; ids alone when the driver has no `describe`. |
| A listing | Prepares the upstream, the dataset and, without `driver.list_command`, the driver's environment; starts no unit. |
| `--search`, `--category` | Every word in the id, title or text; one field's values widen, two fields narrow. |
| The entry | `sets/<benchmark>/NAME.json` in the user catalogue, a plain file to copy or keep. |
| Frozen | No command changes a set's list; `rename` changes its label; jobs find it by id. |
| Its name | Up to 80 characters with a letter or a digit; commands take its slug. |
| `--from-job` | The units a standard job ran, or only those of `--outcome`'s outcomes and each `--unit`. |
| `import` | Adds an entry, or a job's set; another id under that name is refused. |
| A run | Takes the set's dataset and units; another `--dataset`, `--units` or an unlisted id is refused. |
| Another version | Refused before any unit when the run's dataset lacks one of the set's tasks. |
| The record | `config.json` keeps `standard.task_set`: the set's id, name, hash, units and version. |
| On a host | The app sends a recipe's sets with the runner; by hand, copy or `import` the entry. |
| The pages | Save, rename and delete a set only from a browser on the application host. |

Exit codes: 0 done, 2 a usage error or a refusal, 3 no such benchmark, dataset, set, file or job ([Exit codes and output](exit-codes.md)).
