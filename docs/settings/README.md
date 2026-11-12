# Settings

Configure model and data providers, judges, custom keys, run hosts and Docker.
Existing results open without credentials. Model provider, agent key and host
changes save automatically; data providers and benchmark credentials use **Save**.

| Page | Sets |
| --- | --- |
| [Model providers](model-providers.md) | A run's key or endpoint |
| [System One provider](system-one.md) | The System One judgement |
| [LLM judge provider](llm-judge.md) | The second judge |
| [Custom keys](agent-keys.md) | Agent credentials, dataset keys and benchmark tool licences |
| [Model prices](model-prices.md) | What a run's cost is estimated from |
| [Run hosts](run-hosts.md) | SSH machines |
| [Data providers](data-providers.md) | Hugging Face and Docker Hub connections |
| [Sandbox](sandbox.md) | Docker on this machine |
| [General](other.md) | Tour and checklist; start-up options remain in the environment |

## Variables

| Variable | Used by |
| --- | --- |
| `MODEL_API_KEY`, `MODEL_API_URL` | A local or self-hosted endpoint; the URL required, the key optional |
| `OPENAI_API_KEY` | Runs on OpenAI |
| `OPENROUTER_API_KEY` | Runs on OpenRouter |
| `ANTHROPIC_API_KEY` | Standard runs on Anthropic; the relay alone holds it |
| `SYSTEM_ONE_PROVIDER`, `SYSTEM_ONE_MODEL`, `TYPESAFE_API_KEY` | [System One](system-one.md) |
| `LLM_JUDGE_PROVIDER`, `LLM_JUDGE_MODEL` | [LLM judge](llm-judge.md) |
| `AGENT_KEYS` | Comma-separated agent key names; Harbor runs only |
| `HOSTS` | The run hosts |

| Rule | |
| --- | --- |
| Offered | Only the providers set here ([the rule](../builder.md#which-providers)) |
| Model credentials | A run gets its selected provider's key and endpoint |
| Custom keys | Agent keys go to Harbor runs; benchmark credentials only to their declared driver or agent ([details](agent-keys.md)) |
| GitHub | No token; Git uses this machine's credentials |

## Start-up options

Set in the environment or `settings.json` before Nullius starts; address, port
and job roots also have command-line options. The app does not load `app/.env`.

| Variable | Default |
| --- | --- |
| `APP_HOST`, `APP_PORT` | `127.0.0.1`, `5057` |
| `JOB_ROOTS`, `WORKSPACES_DIR`, `WORKSPACE` | Result roots, workspaces folder, workspace to open |
| `DATASET_ROOTS` | Local dataset checkouts |
| `RUNS_DIR`, `HARBOR_PINS` | `runs/` in the user data directory; `harbor-pins/` beside the runs |
| `STANDARD_DIR`, `STANDARD_DATA_DIR`, `SECRETS_DIR` | `standard/` in the data directory; `datasets/` inside it; `secrets/` in the configuration directory |
| `NULLIUS_CATALOGUE` | `catalogue/` in the user data directory; whole-entry overrides of the bundled catalogue |
| `NULLIUS_PRICES` | `prices.json` in the configuration directory; the [model price list](model-prices.md) |
| `DEBUG` | Development debugger and reloader; off by default, enabled by `1` |

## Save

| Rule | |
| --- | --- |
| Writes | The user configuration directory's `settings.json`, mode 0600, including saved provider credentials |
| Edited by hand | Read on the next page, no restart |
| Precedence | The process environment wins; its row is read-only |
| Keys | No provider-specific length check; values must be single-line and contain no spaces. Agent key names use environment-variable syntax, without a length limit |
| Secrets | Provider values are masked; custom-key rows show their saved status, never the value |
| Guards | Its own address in the Host header, loopback browser, same origin, form token |
| Validation | One bad value, nothing written |
| Code | `app/nullius/settings.py`; `app/.env.example` documents variable names for scripts and migration |

## Storage

`nullius paths` (or `nullius paths --json`) shows the active locations without
credential values. These Linux defaults are independent of the working
directory and installation:

| Stores | Default |
| --- | --- |
| Saved settings | `~/.config/nullius/settings.json` |
| Benchmark credentials | `~/.config/nullius/secrets/<benchmark>/` |
| Hugging Face token | `~/.config/nullius/secrets/data-provider-huggingface/` |
| Model prices | `~/.config/nullius/prices.json` |
| User catalogue | `~/.local/share/nullius/catalogue/` |
| Workspaces | `~/.local/share/nullius/workspaces/` |
| Runs started from Review | `~/.local/share/nullius/runs/` |
| Standard installs and data | `~/.local/share/nullius/standard/` |
| Caches | `~/.cache/nullius/` |

`XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME` move their respective
roots. Set `NULLIUS_HOME=/path/to/profile` in the process environment to use
`config/`, `data/` and `cache/` below one separate profile instead. More specific
storage variables in the table above still override these defaults. Other
platforms use their native user directories; `nullius paths` is authoritative.

Personal settings and catalogue additions survive package upgrades. A user
catalogue folder replaces the entire bundled folder of the same kind and key;
files are never mixed between them. Standard commands that edit a bundled
definition first copy that entry to the user catalogue. On a customized benchmark,
agent or capability page, **Use bundled entry** removes that copy and its local
proof records. Saved runs, results and credentials stay unchanged. This selects
the entry shipped with Nullius; it does not create another benchmark version.

## Migrate an existing checkout

```bash
nullius migrate --from /path/to/old-checkout
nullius migrate --from /path/to/old-checkout --apply
```

| Rule | Behavior |
| --- | --- |
| Preview | File counts and conflicts; no credential values |
| Settings | Copies `app/.env`; it is never loaded automatically |
| Workspaces | Preserves an explicit `WORKSPACES_DIR`; otherwise copies the checkout's workspaces |
| Runs | Stay at their original paths; keep those directories available |
| Benchmark credentials | Include with `--secrets /path/to/old/secrets` |
| Linked folders | Selected storage roots can point to another disk; links inside copied contents are refused |
| Conflicts | Differing destination files or folder contents stop the copy |
| Catalogue | User copies override bundled updates; **Use bundled entry** on the entry's page restores the bundle |
| Source | Unchanged; migrating the running checkout copies all its entries because no independent baseline exists |

Set `NULLIUS_HOME` before both commands to migrate into a separate profile.

<div class="grid cards" markdown>

- [Run hosts](run-hosts.md)
- [Sandbox](sandbox.md)

    SSH machines.

- [Other settings](other.md)

    Start-up options.

</div>
