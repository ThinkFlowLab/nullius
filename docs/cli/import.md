# Import a run

Convert a foreign run tree into a job root the pages and the reading commands open.

```bash title="Import"
nullius import <run tree> -o <job root> [--json]
nullius import --list
```

| Flag | Does |
| --- | --- |
| `<run tree>` | ATIF trajectories, or an OpenTelemetry span export; unrecognised exits 3 |
| `-o <job root>` | Where the job is written, the only place written |
| `--list` | Names the importers |
| `--json` | One JSON document on stdout |

The app converts a foreign root the same way ([Architecture](../how-it-works.md)); unpack a zip first.

<div class="grid cards" markdown>

- [Read jobs](read-jobs.md)

    Open what was imported.

- [Exit codes and output](exit-codes.md)

    What every command promises.

</div>
