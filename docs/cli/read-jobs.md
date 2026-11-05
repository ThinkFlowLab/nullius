# Read jobs

List the jobs under a root, open one, compare several, list the workspaces.

```bash title="The reading commands"
nullius runs      list <root>... [--json]
nullius runs      show <root> <job id> [<trial>] [--json]
nullius compare   <root> <job id> <job id>... [--matched] [--json]
nullius workspace list [--home DIR] [--json]
nullius standard traces <dir> [--dry-run]
```

## Inspect and compare saved jobs

1. List the jobs under a root.

    ```bash title="List"
    nullius runs list ./jobs
    ```

2. Open one; compare two on their shared tasks.

    ```bash title="Show and compare"
    nullius runs show ./jobs run-a
    nullius compare ./jobs run-a run-b --matched
    ```

3. `--json`: one document on stdout.

    ```bash title="JSON"
    nullius runs show ./jobs run-a --json > /tmp/run-a.json
    ```

!!! note "Reads only"
    None launches an evaluation.

| Command | Gives |
| --- | --- |
| `nullius runs list <root>...` | The runs, as `/runs` lists them |
| `nullius runs show <root> <id>` | A job: trials, outcomes, counts, solve rates |
| `nullius runs show <root> <job> <trial>` | A trial: outcome, checks, usage, exception, steps. Tool arguments and observations stay on the page |
| `nullius compare <root> <id> <id>... [--matched]` | `/compare`'s cohort: whole, or matched on the tasks every job ran |
| `nullius workspace list [--home DIR]` | The workspaces, as `/workspaces` lists them |

<div class="grid cards" markdown>

- [Read results](../results.md)

    The same reads on the pages.

- [Exit codes and output](exit-codes.md)

    What every command promises.

</div>

## Give past standard runs a trace

A standard run writes `agent/trace.json` as it ends. `nullius standard traces` gives an earlier job the same file, from what its trials kept. It only adds that file: an existing trace is never replaced, and Harbor jobs and controls are left alone.

```bash title="See what it would write, then write it"
nullius standard traces ./jobs --dry-run
nullius standard traces ./jobs
```

A trial that ended early kept no ACP events: its trace has the model calls, and its tool calls at their start, with no duration.
