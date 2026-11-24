# Launch a recipe

A saved recipe of the standard, launched from a shell as the builder's **Run** launches it.

```bash title="Recipes"
nullius recipes list [--json]
nullius recipes launch <recipe> [--where MACHINE]... [--workspace NAME] [--server URL] [--yes] [--wait] [--json]
```

```bash title="Examples"
nullius recipes launch "OSWorld1 nop" --where gpu-box --workspace trials
nullius recipes launch recipe.json --wait --json
```

| Argument | Meaning |
| --- | --- |
| `<recipe>` | A saved recipe by its name or id ([Saved recipes](../builder.md#saved-recipes-and-drafts)), or a recipe file as the builder exports it |
| `--where MACHINE` | `local`, the server's machine, or a host's entry in Settings, in place of the recipe's *Where*; repeated, one shard each |
| `--workspace NAME` | The workspace the launch goes into, made when there is none; the open workspace stays open. Default: the open one, as Run |
| `--server URL` | The Nullius server on this machine (default `http://127.0.0.1:` and `APP_PORT`, else 5057) |
| `--yes` | Answers the question Run asks before a run of every unit |
| `--wait` | Returns when the launch has ended, or paused on the question its page asks; exits 4 unless it ended done |
| `--json` | The server's answer as one document |

| Rule | Detail |
| --- | --- |
| The server | `launch` needs `nullius serve` running here: the server starts the launch and follows it, as one started from the page. `nullius standard run` runs in the shell instead ([Run a benchmark](run.md)) |
| What it sends | The builder's requests: `GET /new-run`, `POST /new-run/runner`, `POST /new-run/prerequisites`, `POST /run` |
| Refused | An error of the compile, a machine missing a prerequisite, the question without `--yes`, a Harbor recipe: nothing starts |
| The name | The recipe's name and the second, as each Run names a new job |
| What it prints | `launch run:<id>`, the launch's key; per run its machine, its state, its jobs folder and its job pages |
| The result | `nullius runs show <jobs folder> <job id>`: [Read jobs](read-jobs.md) |

<div class="grid cards" markdown>

- [Run builder](../builder.md)

    The recipe and its Run.

- [Pages from a shell](pages.md)

    The other pages' commands.

</div>
