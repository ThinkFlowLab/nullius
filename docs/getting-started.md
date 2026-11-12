# Quickstart

Launch Nullius, open results and inspect a trial, with no model key.

| Need | For |
| --- | --- |
| Python 3.12 or later, and [uv](https://docs.astral.sh/uv/) | Running the application |
| A model provider key | Running a benchmark |
| Docker or Podman, answering `docker ps` or `podman ps` | The standard's docker backend |
| Docker, answering `docker ps` | Harbor recipes |

## Install and launch

The default installation uses `uv` and the PyPI package `nullius-eval`.
The application and its command remain `nullius`. The package named `nullius`
on PyPI is a different project.

1. Install the latest version, then start:

    ```bash title="From a terminal"
    uv tool install nullius-eval
    nullius serve
    ```

2. Open [127.0.0.1:5057](http://127.0.0.1:5057). Nullius opens on **Runs**; a first launch offers a tour and a *First steps* checklist.

| Option | Sets |
| --- | --- |
| `--host`, `--port` | Address and port; `APP_HOST` and `APP_PORT` environment variables do the same |
| `--debug` / `--no-debug` | Enable or disable the development debugger and reloader; off by default. `DEBUG=1` also enables them |
| Roots after `serve` | Job directories to open at start |

### Upgrade an installation

Update to the latest version with:

```bash
uv tool upgrade nullius-eval
```

To select a specific version instead, use `uv tool install
nullius-eval==0.1.0rc2`. If replacing an existing installation, add `--force`.
An explicit version pin is retained by `uv tool upgrade`; change the pin with
`uv tool install --force nullius-eval==VERSION`, or remove it with
`uv tool install --force nullius-eval`.

Settings, catalogue additions and runs stay outside the installed package
and are preserved when the tool is replaced. If you installed an earlier
private build of this application under the package name `nullius`, remove
that tool with `uv tool uninstall nullius` before installing `nullius-eval`.
This removes its tool environment, not the user storage shown by `nullius paths`.

### Install a wheel

Wheel downloads are listed under
[Releases](https://github.com/thinkflowlab/nullius/releases) and follow the
repository's access permissions. From the folder containing the file:

```bash
uv tool install ./nullius_eval-0.1.0rc2-py3-none-any.whl
nullius serve
```

Add `--force` to replace an existing `nullius-eval` installation.

### Build a wheel from source

From a checkout, build the wheel, then install the resulting file:

```bash
uv build --wheel --out-dir dist
uv tool install ./dist/nullius_eval-0.1.0rc2-py3-none-any.whl
nullius serve
```

Use the filename produced by the build if the version changes. To use an
existing Python environment instead, `uv pip install --python /path/to/venv/bin/python
/path/to/nullius.whl` installs the same wheel; that interpreter supports
`python -m nullius` with the same subcommands.

### Work from source

```bash
git clone https://github.com/thinkflowlab/nullius.git
cd nullius
uv run --no-project --with-requirements app/requirements.txt app/app.py
```

The backend commands also work without installation:
`PYTHONPATH=app python3 -m nullius --help`. See the [CLI](cli/README.md).
`--no-project` keeps the source command from also installing the project into
a second Python location. Flask is the application's only direct runtime
dependency; benchmark and agent environments stay separate.

### Your settings and data

Save provider credentials in **Settings**. The owner-only `settings.json` lives
in the user configuration directory, and environment variables override it.
`nullius paths` lists settings, licences, user catalogue, workspaces, runs and
cache paths without showing credentials. Linux defaults use XDG directories;
see [Settings](settings/README.md#storage).

For a separate profile, use the same `NULLIUS_HOME` for each command:

```bash
NULLIUS_HOME=/path/to/profile nullius paths
NULLIUS_HOME=/path/to/profile nullius serve
```

That profile keeps `config/`, `data/` and `cache/` below the chosen directory.
An install or upgrade does not replace this storage.

### Migrate an existing checkout

Preview `nullius migrate --from /path/to/old-checkout`, then repeat with
`--apply`. This copies settings and catalogue entries without deleting the
source. Keep raw run directories available. See [migration rules](settings/README.md#migrate-an-existing-checkout).

## Open existing results

=== "Nullius jobs"

    A run started from Review opens in **Runs** by itself; a job made elsewhere is a job directory:

    ```bash title="Open a job directory at start"
    nullius serve /path/to/jobs_standard
    ```

    Several directories open as one workspace. Shards of one launch are grouped until merged.

=== "Harbor jobs"

    The same command, on a Harbor job directory.

=== "ATIF or OpenTelemetry"

    In the workspace menu, choose **New workspace**. The root is converted into the workspace's `imported/`; the source files are left as they are.

Then [read the results](results.md).

## Run a new evaluation

1. In **Settings**, set the provider you will use ([Model providers](settings/model-providers.md)).
2. In **Run builder**, choose a runner, a benchmark and a dataset.
3. Choose an agent and a model, with a small task limit.
4. Choose this machine or a host in **Where**.
5. Check the recipe and its prerequisites in **Review**, then press **Run**.

!!! warning "A run calls paid model APIs"
    Only **Run** from Review starts a job.

## If the app is on another machine

Keep Nullius on loopback and forward its port over SSH:

```bash title="On the laptop"
ssh -N -L 5057:127.0.0.1:5057 user@your-host
```

Then open [localhost:5057](http://localhost:5057). To run benchmarks elsewhere and keep the application here, add a [run host](settings/run-hosts.md) instead.

<div class="grid cards" markdown>

- [Core concepts](concepts.md)
- [Run builder](builder.md)
- [Read results](results.md)

</div>
