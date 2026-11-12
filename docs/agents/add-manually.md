# Add an agent manually

Copy the Goose integration as `my-agent`, point it at the relay, validate it, prove its ports.

With [a model provider](../settings/model-providers.md); the copy never
overwrites. Without Docker, `--unsandboxed` replaces `--backend container` and
runs the agent as you.

The Python snippet below uses the interpreter from a `uv tool install`. For a
Python-environment install, use that environment's Python; from source use
`PYTHONPATH=app python3`. Only the user catalogue is written.

## Create the definition

```bash title="Create my-agent, proofs cleared"
"$(uv tool dir)/nullius/bin/python" - <<'PYCODE'
import json
import shutil
from pathlib import Path
from nullius import paths, settings
from nullius.standard import definitions

settings.load(paths.settings_file())
source = Path(definitions.entry_dir("agent", "goose"))
target = Path(paths.catalogue_dir()) / "agents" / "my-agent"
shutil.copytree(source, target)
path = target / "agent.json"
definition = json.loads(path.read_text())
definition.update(key="my-agent", label="My Goose", offers=[], proven={})
path.write_text(json.dumps(definition, indent=2) + "\n")
print(path)
PYCODE
nullius_catalogue=$(nullius paths --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["catalogue"])')
```

## Inspect the launch configuration

```json title="agent.json · launch configuration excerpt"
{
  "install": {"npm": ["@aaif/goose-acp@1.53.0"]},
  "start": {
    "acp": ["{install}/node_modules/.bin/goose", "acp", "--with-builtin", "developer"]
  },
  "model": {
    "apis": ["chat"],
    "files": {"{home}/.config/goose/config.yaml": "config.yaml"}
  }
}
```

```yaml title="config.yaml · the relay as Goose's provider"
GOOSE_PROVIDER: openai
GOOSE_MODEL: "{model}"
OPENAI_HOST: "{base_url}"
OPENAI_BASE_PATH: chat/completions
```

| Slot | Filled with |
| --- | --- |
| `{install}` | The isolated installation |
| `{here}` | The definition's folder |
| `{home}` | The agent's home |
| `{base_url}` | The relay's endpoint |
| `{key}` | The relay's placeholder credential |
| `{model}` | The selected model |

Keep the whole copied definition, `model.env` and `options` included.

## Validate, prove, check the fit

1. Validate, which installs and calls nothing:

    ```bash title="Validate the definition · prints ok"
    nullius standard check \
      "$nullius_catalogue/agents/my-agent/agent.json"
    ```

2. Conformance, which installs the pinned agent and calls the model:

    ```bash title="Run conformance · Container backend"
    nullius standard conform my-agent \
      --provider openai --model gpt-5-mini --backend container --repeat 3
    ```

3. Which benchmarks it fits:

    ```bash title="Check compatibility"
    nullius standard fit humaneval
    ```

| Rule | |
| --- | --- |
| Success | Passing checks write `offers` and `proven`, never by hand; the kit jobs under `jobs_standard/` |
| The builder | The same ports and proofs as `fit` |

A personal addition does not change package files or project tests. To
contribute it to the bundled catalogue, copy its whole folder into
`app/nullius/catalogue/agents/` in a source checkout, run the relevant suites,
and refresh `python3 app/tests/catalogue_docs.py --write`.

!!! warning "Conformance makes paid model calls"
    Each check is a trial; `--repeat 3` runs each three times.

## Another agent

```bash title="A blank definition"
nullius standard new agent another-agent
```

1. Replace the capitalised placeholders: package, version, executable.
2. Set the variables or files **that agent** reads for its model endpoint, and its entry point.

| Entry point | Field | Ports it can prove |
| --- | --- | --- |
| ACP, native or an adapter | `start.acp` | Task, workspace, tools, conversation, search, as proven |
| A CLI that takes a prompt | `start.command` with `{prompt}` | Task, workspace and search only |

```json title="agent.json · a command entry point (mini-swe-agent)"
{
  "start": {
    "command": ["{install}/.venv/bin/python", "{here}/run.py", "{prompt}"]
  }
}
```

`app/nullius/catalogue/agents/mini-swe-agent/run.py` only starts and configures the upstream.

A benchmark's own reference agent, made for that benchmark rather than for any
task, names it in `only_for` (`"only_for": ["terminal-bench"]` for Terminus 2):
it is offered for those benchmarks alone, whatever ports it has proven.

## If it does not work

| Symptom | Check |
| --- | --- |
| No model calls appear | The trial's `agent/calls.jsonl`; the agent must honour the relay URL, not a vendor endpoint |
| Conformance fails | The kit's trial; setup or a kit can fail too |
| Absent from the builder | The version's proofs against the benchmark's needs |

<div class="grid cards" markdown>

- [Versions](../standard.md#versions)

- [Options](../standard.md#options)

- [Fidelity](../standard.md#fidelity)

</div>
