# Read jobs

List the jobs under a root, open one, compare several, list the workspaces.

```bash title="The reading commands"
nullius runs      list <root>... [--json]
nullius runs      show <root> <job id> [<trial>] [--json]
nullius runs      show <root> <job id> <trial> --call N --request
nullius runs      show <root> <job id> <trial> --calls
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
| `nullius runs show <root> <id>` | A job: trials, outcomes, counts, solve rates, mean scores |
| `nullius runs show <root> <job> <trial>` | A trial: outcome, checks, usage, exception, steps. Tool arguments and observations stay on the page |
| `nullius runs show <root> <job> <trial> --call N --request` | Call N's request as sent, rebuilt from the [exchange record](run.md#the-exchange-record); exit 4 when its hash differs |
| `nullius runs show <root> <job> <trial> --calls` | The trial's Conversation by model call, as JSON, redacted |
| `nullius compare <root> <id> <id>... [--matched]` | `/compare`'s cohort: whole, or matched on the tasks every job ran |
| `nullius workspace list [--home DIR]` | The workspaces, as `/workspaces` lists them |

<div class="grid cards" markdown>

- [Read results](../results.md)

    The same reads on the pages.

- [Exit codes and output](exit-codes.md)

    What every command promises.

</div>

## Give past standard runs a trace

Standard runs write `agent/trace.json`, or `agent/trace.error.txt` if trace construction fails. `nullius standard traces` gives an earlier job the same file, from what its trials kept. It only adds that file: an existing trace is never replaced, and Harbor jobs and controls are left alone.

```bash title="See what it would write, then write it"
nullius standard traces ./jobs --dry-run
nullius standard traces ./jobs
```

A trial that ended early kept no ACP events: its trace has the model calls, and its tool calls at their start, with no duration.

## Leaderboard

```bash
nullius leaderboard --help
nullius leaderboard --json
nullius leaderboard ./jobs --json
```

Supply the result roots of one workspace. This reads completed jobs; it never
launches work. Without roots, catalogue rows have no recorded-run measurements. Exit 0 means
the document was read; an invalid root exits 3, invalid arguments 2.

The page and command use the same profile's [Scoring settings](../field-guide.md#leaderboard).
JSON includes agent/company/version and execution identity, benchmark entry
versions, all capability and domain weights, effective shares, ranks and
coverage notes, task evidence and selected run IDs. Scores are fractions;
text percentages use one decimal when needed.

Time and cost include observed counts. The command also reads selected
trajectories for Steps and Tool calls; a missing or malformed trace leaves those
metrics unavailable. Invalid scoring settings use defaults with the same warning
as the page, without replacing the file. See the [scoring contract](../field-guide.md#leaderboard).
