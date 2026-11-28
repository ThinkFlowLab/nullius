# Pages from a shell

Each page's command, or why there is none.

## Covered

| Page | Command |
| --- | --- |
| `/runs` | `nullius runs list <root>... [--json]` |
| `/job/<id>` | `nullius runs show <root> <id> [--json]` |
| `/trial/<job>/<trial>` | `nullius runs show <root> <job> <trial> [--json]` |
| A trial's Conversation, by call | `nullius runs show <root> <job> <trial> --calls` |
| `/import`, `/use-path` | `nullius import <run tree> -o <job root> [--json]`; `--list` |
| `/leaderboard` | `nullius leaderboard <root>... [--json]`; `nullius remote leaderboard` against a served Nullius |
| `/scoring` | Profile `scoring.json` beside `settings.json`; `nullius paths` locates its configuration directory |
| `/compare` | `nullius compare <root> <id> <id>... [--matched] [--json]` |
| `/workspaces/open/<name>` | `nullius serve <root>...`, `WORKSPACE=<name>` |
| `/workspaces` | `nullius workspace list [--home DIR] [--json]` |
| `/settings` | User `settings.json`, or the environment; `nullius paths` locates the file |
| Settings, API access | `nullius access create`, `list`, `revoke` |
| `/benchmarks` | `nullius standard list` for standard entries; Harbor entries on the page |
| `/agents`, `/capabilities` | `nullius standard list` includes agents, capabilities and their proven ports |
| A benchmark's *Task sets* | `nullius standard set list KEY` ([Task sets](task-sets.md)) |
| `/benchmarks/<key>/tasks/<dataset>` | `nullius standard units KEY --dataset D`; a set's, `set show KEY NAME` |
| Its *List tasks* (*List questions*) | `nullius standard units KEY --dataset D`, which makes the listing |
| Its *Pick at random* | `nullius standard set create KEY NAME --dataset D --sample N [--seed S]` |
| Save, rename or delete a task set | `nullius standard set create`, `--from-job` for a job's; `set rename`, `set delete` |
| A benchmark's *Prove it* | `nullius standard prove KEY` ([Prove](prove.md)) |
| *Prove its ports* | `nullius standard conform KEY` ([Conform](conform.md)) |
| `/run` | `nullius standard run` per dataset and choice, `--shard I/N` per machine ([Run](run.md)) |
| A job's *Run selected again*, *Run the rest*, *Run a set* | `nullius standard rerun JOB_DIR --unit ID`, `--unfinished`, `--set NAME` ([Run tasks again](rerun.md)) |

## While the app runs

`curl localhost:5057<route>`:

| Page | Route |
| --- | --- |
| The live board | `/job/<id>/live` |
| A trial's conversation, spans, artifacts | `/trial/…/conversation`, `/trial/…/trace-span`, `/trial/…/artifact/<key>`, `/trial/…/artifact/<key>/download` |
| A picture the agent sent its model | `/trial/…/picture/<sha256>` |
| One task side by side | `/compare/task/<task>` |

## From another machine

`nullius remote` asks a served Nullius for the same reads, and checks, starts and stops its recipes: [From another machine](../api.md).

## No command yet

| Page | What it would be |
| --- | --- |
| `/trial/…/save-trace` | a `harbor-trace-v1` writer |
| `/judge/…` | `judge.cli job <root> <id> --confirm`; today `nullius remote judge` against a served Nullius |
| Rename or delete a workspace | `nullius workspace rename`, `nullius workspace delete --yes` |
| Unlink a workspace's root, delete its folder | `workspace.forget`, `execution.delete`; today `POST /workspaces/<name>/roots/unlink` and `/roots/delete` |
| `/new-run` | `recipes.cli`; today `POST /new-run/runner`, or `nullius remote check` and `run` against a served Nullius |
| Delete a launch | `execution.delete`; today `POST /launch/<key>/delete` |
| `/launches`, add a launch to a workspace | today `POST /launch/<key>/add` |

## None needed

| Page | Why |
| --- | --- |
| `/upload` | import the directory |
| Test a host, install a key | `ssh -o BatchMode=yes <host> true`, `ssh-copy-id` |
| A Harbor recipe: run, follow, continue, stop | the generated script |

<div class="grid cards" markdown>

- [Read jobs](read-jobs.md)

    The commands.

- [Exit codes and output](exit-codes.md)

    The contract.

</div>
