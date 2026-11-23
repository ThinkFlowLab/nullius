## What this repository runs

Nullius reads run output and runs a benchmark only on an explicit **Run**
from the builder's Review step: a **Nullius standard** recipe
(`docs/standard.md`) or a **Harbor** recipe, on this machine or a Settings host
(`nullius/execution.py`). A benchmark's own scripts run as its Original
setup.

- **Harbor** runs through `uvx` with a pinned PyPI release or locally built repository commit. A Harbor run needs `docker ps` to answer.
- **The standard** runs from the current checkout or installed package; a host gets the running backend and effective catalogue, source edits and user entries included, never configuration or credentials. Several
  machines run one shard each.
- **Keys**: a run gets only its provider's key and endpoint; logs are
  redacted. The standard's agents and upstream scripts get only the relay's
  word, never a key.
- What a recipe needs of each machine is asked before Run and again as it
  starts (`nullius/prerequisites.py`). `runs/<name>/run.json` lets a
  restarted Nullius pick a run up again.

## Running the app

```bash
nullius serve

# or from the repository root
uv run --no-project --with-requirements app/requirements.txt app/app.py jobs
```

Port and host come from saved user settings or the
environment (`APP_PORT`, `APP_HOST`), or `--port`/`--host`.

`nullius paths` shows the user config, data and cache locations; `NULLIUS_HOME`
selects a separate profile. Settings saves owner-only `settings.json`, with
environment values taking precedence. Old `app/.env` is never loaded
automatically: `nullius migrate --from CHECKOUT` previews, `--apply` copies.
A set `WORKSPACES_DIR` and raw runs stay where they are; default workspaces are copied.

Migrated catalogue entries keep their versions across upgrades as user overrides; **Use bundled entry** on an entry's page removes
its user override, including local proof records, to resume bundled updates.
Existing destination entries must match exactly.
Conflicts stop migration before copying.

Links within the copied contents are refused.

Result roots open as a workspace under the user data directory's `workspaces/`
(`WORKSPACES_DIR` overrides it); ATIF and OpenTelemetry roots are converted into
its `imported/`. Review runs go under the user data directory's `runs/`
(`RUNS_DIR`). Direct standard CLI jobs default to `jobs_standard/` in the
invoking directory. User catalogue entries overlay bundled entries by whole
folder; editing a bundled definition first copies it into user storage.

## Reading a result

- Harbor's headline `mean` counts infra failures as misses; the usual one is
  `ApiRateLimitError`, a throughput problem. Read the **adjusted** rate and
  the outcome breakdown first.
- A job with no verdict has no rate (`—`), not 0%; a scored-only benchmark
  shows its mean score.
- `nop` and `oracle` are checks on the tasks, never ranked with agents.
