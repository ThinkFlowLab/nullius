## What this repository runs

Nullius reads run output and runs a benchmark only on an explicit **Run**
from the builder's Review step: a **Nullius standard** recipe
(`docs/standard.md`) or a **Harbor** recipe, on this machine or a Settings host
(`nullius/execution.py`). A benchmark's own scripts run as its Original
setup. Review shows the decisions and prerequisites, then starts the recipe
through the execution backend (`docs/builder.md`).

- **Harbor** is never imported: a recipe pins a PyPI release or a repository
  commit, built here into `harbor-pins/` and run with `uvx`
  (`nullius/harbor/source.py`). A Harbor run needs `docker ps` to answer.
- **The standard** runs from the current checkout or installed package; on a
  host it sends a deterministic snapshot of the running backend and effective
  catalogue into `~/.nullius/app/<content-id>`. Source edits and user entries
  are included; configuration and credential stores are excluded. Several
  machines run one shard each.
- **Keys**: a run gets only its provider's key and endpoint, as environment
  (over stdin to a host, and the key to a relay in a container as a file); logs are
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

The default installation is `uv tool install nullius-eval`, selecting the latest
version unless one is explicitly pinned. Downloaded or locally built wheels
remain an alternative. The executable is `nullius`; Flask is its only
direct runtime dependency. Debug mode is off by
default; `--debug` or `DEBUG=1`
enables the development debugger and reloader. Port and host come from saved user settings or the
environment (`APP_PORT`, `APP_HOST`), or `--port`/`--host`.

`nullius paths` shows the user config, data and cache locations; `NULLIUS_HOME`
selects a separate profile. Settings saves owner-only `settings.json`, with
environment values taking precedence. Old `app/.env` is never loaded
automatically: `nullius migrate --from CHECKOUT` previews, `--apply` copies.
An explicit nonempty `WORKSPACES_DIR` in the old settings remains in use at its
normalized absolute path, without copying or rewriting those workspaces. When
unset or empty, the checkout's default workspaces are copied in full, including
judgements, with copied root references made absolute. Raw runs stay where they
were; migrated `RUNS_DIR` preserves their location.

Migration compares catalogue entries with the running bundle. If the source
checkout itself supplies that backend, it copies every entry because there is
no independent baseline. The preview explains that these user overrides retain
their versions across upgrades; **Use bundled entry** on an entry's page removes
its user override, including local proof records, to resume bundled updates.
An existing destination entry must match the complete directory/file inventory
and file bytes; partial or differing folders are conflicts, never merged.
Conflicts stop migration before copying. Tests use temporary profiles, never
personal storage.

Migration resolves selected storage roots, including linked directories on other
disks, before copying. Links within the copied contents are refused.

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
