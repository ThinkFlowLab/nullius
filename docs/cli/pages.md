# Pages from a shell

Each page's command, or why there is none.

## Covered

| Page | Command |
| --- | --- |
| `/runs` | `nullius runs list <root>... [--json]` |
| `/job/<id>` | `nullius runs show <root> <id> [--json]` |
| `/trial/<job>/<trial>` | `nullius runs show <root> <job> <trial> [--json]` |
| `/import`, `/use-path` | `nullius import <run tree> -o <job root> [--json]`; `--list` |
| `/compare`, `/compare/pick` | `nullius compare <root> <id> <id>... [--matched] [--json]` |
| `/workspaces/open/<name>` | `nullius serve <root>...`, `WORKSPACE=<name>` |
| `/workspaces` | `nullius workspace list [--home DIR] [--json]` |
| `/settings` | User `settings.json`, or the environment; `nullius paths` locates the file |
| `/benchmarks` | `nullius standard list` for standard entries; Harbor entries on the page |
| `/agents`, `/capabilities` | `nullius standard list` includes agents, capabilities and their proven ports |
| A benchmark's *Prove it* | `nullius standard prove KEY` ([Prove](prove.md)) |
| *Prove its ports* | `nullius standard conform KEY` ([Conform](conform.md)) |
| `/run` | `nullius standard run` per dataset and choice, `--shard I/N` per machine ([Run](run.md)) |

## While the app runs

`curl localhost:5057<route>`:

| Page | Route |
| --- | --- |
| The live board | `/job/<id>/live` |
| A trial's conversation, spans, artifacts | `/trial/…/conversation`, `/trial/…/trace-span`, `/trial/…/artifact/<key>` |
| One task side by side | `/compare/task/<task>` |

## No command yet

| Page | What it would be |
| --- | --- |
| `/trial/…/save-trace` | a `harbor-trace-v1` writer |
| `/judge/…` | `judge.cli job <root> <id> --confirm` |
| Rename or delete a workspace | `nullius workspace rename`, `nullius workspace delete --yes` |
| `/new-run` | `recipes.cli`; today `POST /new-run/runner` |

## None needed

| Page | Why |
| --- | --- |
| `/runs/pulse`, refresh a workspace | commands read fresh every call |
| `/upload` | import the directory |
| Test a run host, install a key | `ssh -o BatchMode=yes <host> true`, `ssh-copy-id` |
| A Harbor recipe: run, follow, continue, stop | the generated script |

<div class="grid cards" markdown>

- [Read jobs](read-jobs.md)

    The commands.

- [Exit codes and output](exit-codes.md)

    The contract.

</div>
