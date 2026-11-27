# Task sets

A named list of one or several datasets' units, frozen once made, that a run names and its job records.

```bash title="Commands"
nullius standard units <benchmark> --dataset D [--bench-option NAME=VALUE]... [--search TEXT] [--category FIELD=VALUE]... [--json]
nullius standard set create <benchmark>[@version] "WORDS" [--dataset D] (--units FILE | --search TEXT | --category FIELD=VALUE... \
    | --sample N [--seed S] | --from-job JOB_DIR [--outcome O,...]... [--unit ID]...) [--description TEXT]
nullius standard set create <benchmark>[@version] "WORDS" (--dataset D --units FILE)... [--description TEXT]
nullius standard set list [<benchmark>] [--json]
nullius standard set show <benchmark> NAME [--json]
nullius standard set rename <benchmark> NAME "WORDS"
nullius standard set delete <benchmark> NAME
nullius standard set import PATH
nullius standard run <benchmark> <other> --set NAME [--dataset D] ...
```

```bash title="Examples"
nullius standard units osworld1 --dataset test_nogdrive --search tab --category domain=chrome
nullius standard set create humaneval "first ten" --dataset humaneval --units ten.txt
nullius standard set create humaneval misses --from-job jobs_standard/JOB --outcome failed,infra
nullius standard set create gaia twenty --dataset gaia-validation --sample 20 --seed 7
nullius standard set create open-telco mix --dataset teleqna-lite --units qna.txt --dataset telemath-lite --units math.txt
nullius standard run open-telco original --model M --set mix --dataset telemath-lite
nullius standard run humaneval reference --model gpt-5-mini --set first-ten --unsandboxed
```

| Rule | Detail |
| --- | --- |
| `units` | Each task's id, title, text and categories; ids alone when the driver has no `describe`. |
| A listing | Prepares the upstream, the dataset and, without `driver.list_command`, the driver's environment; starts no unit. |
| Its licences | The driver gets its `to: driver` secrets, as a run's; one not set is refused first. |
| Driver output | Supplied secrets are redacted before a listing is stored or its logs and errors are kept or shown. |
| `--search`, `--category` | Every word in the id, title or text; one field's values widen, two fields narrow. |
| `--sample N` | N of the listed or matched tasks at random; `--seed S` repeats a draw, and the note keeps it. |
| The entry | `sets/<benchmark>/NAME.json` in the user catalogue, a plain file to copy or keep. |
| Frozen | No command changes a set's list; `rename` changes its label; jobs find it by id. |
| Its name | Up to 80 characters with a letter or a digit; commands take its slug. |
| `--from-job` | The units a standard job ran, or only those of `--outcome`'s outcomes and each `--unit`. |
| `import` | Adds an entry, or a job's set; another id under that name is refused. |
| Several datasets | Each `--dataset D --units FILE` a part; one hash over all. The page keeps ticks per dataset until saved. |
| A run | Takes the set's dataset and units; another `--dataset`, `--units` or an unlisted id is refused. |
| A run of several | `--dataset` picks one part, a job of its own; the builder runs each. The job records the whole set. |
| Another version | Refused before any unit when the run's dataset lacks one of the set's tasks. |
| The record | `config.json` keeps `standard.task_set`: the set's id, name, hash, units and version. |
| On a host | The app sends a recipe's sets with the runner; by hand, copy or `import` the entry. |
| The pages | Save, rename and delete a set only from a browser on the application host. |

Exit codes: 0 done, 2 a usage error or a refusal, 3 no such benchmark, dataset, set, file or job ([Exit codes and output](exit-codes.md)).
