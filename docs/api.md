# From another machine

Nullius serves on one machine; a script, a pipeline or an assistant on another reads its results and runs its recipes over HTTP.

| Piece | For |
| --- | --- |
| [Token](#tokens) | What the server accepts |
| [Python client](#python) | Scripts, notebooks, pipelines |
| [`nullius remote`](#shell) | A shell |
| [`nullius mcp`](#assistants) | An assistant |
| [Routes](#routes) | Any other language |

## Serve

```bash title="On the server"
nullius serve
```

Then create a token: **Settings → API access**, or `nullius access create`.

| The other machine reaches it | With |
| --- | --- |
| Over SSH | [A tunnel](getting-started.md#if-the-app-is-on-another-machine): the server is `http://127.0.0.1:5057` there |
| Over HTTPS | A TLS proxy on the server that forwards `/api/v1` only |
| On a trusted network | `nullius serve --host 0.0.0.0` |

!!! warning "The network"
    The pages have no login: with `--host`, every machine that reaches the port reads the results and changes the workspaces. Tokens and results travel in clear over `http`.

    A proxy that forwards the pages too must send `X-Forwarded-For`: a request without it is taken for a browser on the server.

## Tokens

**Settings → API access**, from a browser on the server: a name, its access, when it expires. Or:

```bash title="On the server"
nullius access create NAME [--scope read|run] [--days N]
nullius access list
nullius access revoke ID
```

| Access | Allows |
| --- | --- |
| Read, `read` (default) | Catalogue, jobs, trials, judgements, comparisons, the leaderboard, saved recipes, runs |
| Read and run, `run` | Read, and check, start, continue and stop runs, ask a judge, save recipes |

- Shown once, at creation. The server keeps its hash.
- Expires after its days, or never. Revoked or expired: refused at once.
- No token created: the API answers nothing.
- No scope changes Settings or reads a key or a licence. Model keys stay on the server. The leaderboard says the Scoring weights it used.
- The server prints a line for each request that needs `run`: the token's name and id, the address, the request, its status.

## Python

One file, standard library only: installed with Nullius, or `nullius/client.py` copied alone.

```python title="Run a saved recipe, read the result"
from nullius.client import Client

nx = Client("http://127.0.0.1:5057", token)        # or NULLIUS_SERVER and NULLIUS_TOKEN

check = nx.check("nightly")                        # a saved recipe's name or id, or a recipe dict
run = nx.start("nightly", confirm=True)["runs"][0]
for line in nx.follow(run["id"]):
    print(line)

job = nx.job(run["job_ids"][0])
job["adjusted_solve_rate"], job["counts"]
```

| Call | Gives |
| --- | --- |
| `info()`, `catalogue()`, `workspaces()` | The server, what it can run with each benchmark's [task sets](cli/task-sets.md), its workspaces |
| `jobs()`, `job(id)`, `live(id)` | As `nullius runs list` and `show --json`; the live board |
| `trial(job, trial)`, `conversation(...)`, `trace(...)`, `artifacts(...)`, `artifact(..., key)` | One trial and its evidence, redacted as on the pages |
| `compare(a, b, ..., matched=False)` | As `nullius compare --json` |
| `leaderboard()` | The [Leaderboard](field-guide.md#leaderboard)'s scores and ranks, under the server's Scoring settings; `steps` and `calls` uncounted |
| `judgements(job)`, `judgement(job, trial)` | A judge's stored answers; `lane="llm"` for the LLM judge |
| `judge(job, trial)`, `judge_job(job, confirm=False)` | Ask a judge: [below](#asking-a-judge) |
| `recipes()`, `recipe(id_or_name)`, `save_recipe(recipe)` | The builder's saved recipes |
| `check(recipe, where=None)` | What Review shows: `ready`, `errors`, `confirm`, `machines` |
| `start(recipe, where=None, name=None, confirm=False)` | `runs`: one per machine |
| `runs()`, `run(id, since=0)`, `follow(id)`, `wait(id)` | State, stages, log |
| `stop(id)`, `continue_(id)` | Stop; answer a paused run |

Jobs are the open workspace's; `workspace="NAME"` on a call that reads or judges jobs names another.

A refusal raises `ApiError`: the server's sentence, `status`, `document`. A redirect is refused: give the final address.

## Starting a run

A run starts as Review starts it.

| Refused | Until |
| --- | --- |
| The recipe has errors (400) | Fixed: `errors` names them |
| A machine lacks a prerequisite (409) | Met: `machines` lists each |
| The recipe asks a question (409), such as the whole dataset | `confirm=True` |

A recipe's `task_sets` runs a task set in place of a dataset's tasks: `{"test": "first-two"}`, a name the catalogue lists. It asks no question.

- Build a recipe in the [run builder](builder.md), save it, start it by name.
- Each start is a new run: the recipe's name and the second, or `name`.
- `where` replaces the recipe's machines: `local`, or a host of Settings.
- Standard recipes only. A Harbor recipe starts from the builder.

## Asking a judge

Every trial asked is a paid model call, on the judge set in Settings.

| Call | Asks |
| --- | --- |
| `judge(job, trial, lane="decision")` | That trial |
| `judge_job(job, lane="decision")` | Nothing: refused (409) with `todo`, how many it would ask; nothing left, it answers so |
| `judge_job(job, confirm=True)` | Every trial of the job with a trajectory that judge has not judged |

Records belong to the job, as the [pages](judgement.md) keep them.

## Shell

```bash title="On the other machine"
export NULLIUS_SERVER=http://127.0.0.1:5057 NULLIUS_TOKEN=...
nullius remote workspaces
nullius remote jobs
nullius remote job JOB [TRIAL]
nullius remote compare JOB JOB [--matched]
nullius remote leaderboard
nullius remote judgements JOB [--lane llm]
nullius remote judge JOB [TRIAL] [--lane llm] [--yes]
nullius remote check RECIPE
nullius remote run RECIPE [--where W] [--name N] [--yes] [--follow]
nullius remote status RUN
nullius remote stop RUN
```

`RECIPE`: a recipe file, or a saved recipe's name or id. `--workspace NAME` on `jobs`, `job`, `compare`, `leaderboard`, `judgements` and `judge` names another workspace. One JSON document on stdout. Exit 3 not found, 4 refused or unreachable; `run --follow` exits 4 unless the run ends `done`.

## Assistants

```bash title="An MCP server over stdio"
NULLIUS_SERVER=http://127.0.0.1:5057 NULLIUS_TOKEN=... nullius mcp
```

Tools: read jobs, trials, judgements, comparisons and the leaderboard; check, start, follow and stop runs; ask a judge about one trial. A `read` token makes it read-only. `start_run` starts only with `confirm` when the recipe asks a question.

## Routes

`Authorization: Bearer <token>` on every request, under `/api/v1`. Errors are `{"error": "..."}`.

| Route | Scope |
| --- | --- |
| `GET /api/v1` | read |
| `GET /catalogue`, `/workspaces` | read |
| `GET /jobs`, `/jobs/<job>`, `/jobs/<job>/live` | read |
| `GET /jobs/<job>/trials/<trial>`, `…/conversation`, `…/trace`, `…/artifacts`, `…/artifacts/<key>` | read |
| `GET /jobs/<job>/judgements`, `/jobs/<job>/trials/<trial>/judgement` (`?lane=decision\|llm`) | read |
| `GET /compare?job=A&job=B[&matched=1]`, `/leaderboard` | read |
| `GET /recipes`, `/recipes/<id or name>` | read |
| `GET /runs`, `/runs/<run>[?since=N]` | read |
| `POST /recipes` | run |
| `POST /runs/check`, `/runs` | run |
| `POST /runs/<run>/stop`, `/runs/<run>/continue` | run |
| `POST /jobs/<job>/trials/<trial>/judge`, `/jobs/<job>/judge` | run |

Jobs are the open workspace's; `?workspace=NAME` reads another, and judges a job there.
