# Conform an agent or a capability

Run each port's conformance kit; a port enters `offers` only through a pass, recorded in the definition.

```bash title="Conform"
nullius standard conform <other>[@version] --model M [--provider P] [--port PORT]... \
    [--unsandboxed | --backend container] [--option NAME=VALUE]... [--attempts K] [--jobs DIR] [--stamp STAMP] [--from-run]
```

| Flag | Does |
| --- | --- |
| `<other>` | `KEY` (its default version) or `KEY@VERSION` |
| `--model`, `--provider` | The kits' model; `anthropic`, `custom`, `openai` or `openrouter` |
| `--port PORT` | Only this port's kit (repeatable) |
| `--unsandboxed`, `--backend` | As for a run: as you on this machine, or each check in a container |
| `--option NAME=VALUE` | An agent's or capability's option (repeatable) |
| `--attempts K` | Each check K times; a majority passes it. Default 1 |
| `--jobs`, `--stamp` | Where kit jobs go (default `jobs_standard/`), and their time stamp |
| `--from-run` | Started by Nullius: `OPENAI_API_KEY` only, saved settings unread |

| Recorded | Detail |
| --- | --- |
| `proven` | Date, score, model, `kit_version`, skills shown, the kit job; with `--attempts`, `"repeat": K`, each count (`"3/3"`), each skill's `"passed"` |
| Skills | A benchmark's `needs_skills` fits only an agent whose proof shows them |
| Replaced | A pass on the current kit replaces an older proof; a fail takes the port back; a `fidelity` record stays |

Without `--port`, a kit refused for what its benchmark needs of this machine is passed over (`PORT: refused: why`); the others run and the command exits 1. When every kit is refused, nothing is written and it exits 2. A refusal of the command itself (the agent, its options, the key) and a refused port named with `--port` end it with 2 and nothing written.

!!! note "A new version runs nothing"
    Until `conform KEY@VERSION` passes; bumping a pin in a file without `versions` asks again.

<div class="grid cards" markdown>

- [The standard](../standard.md#the-ports-version-1)

    The ports, their kits, their skills.

- [Fidelity](fidelity.md)

    ACP against the agent's own command.

</div>
