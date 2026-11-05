# Add a capability manually

Wrap a memory library behind `cap.memory/v1`: scaffold, try, connect, validate, prove.

With Nullius [installed or running from source](../cli/README.md) and
[a model provider](../settings/model-providers.md); know the library's storage
and recall operations.

## Create the files

```bash title="Scaffold my-memory in the user catalogue"
nullius standard new capability my-memory
nullius_catalogue=$(nullius paths --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["catalogue"])')
```

The new folder is `capabilities/my-memory/` below that catalogue. It survives
application upgrades. No package resources or repository tests need changing
for a personal addition.

```json title="capability.json · excerpt of the scaffold"
{
  "kind": "memory",
  "start": {"command": ["python3", "{here}/capability.py"]},
  "offers": [],
  "proven": {}
}
```

## Try the protocol

```bash title="Two requests to the scaffold"
python3 "$nullius_catalogue/capabilities/my-memory/capability.py" <<'JSONL'
{"op":"add","session":{"id":"s1","text":"The project codename is Cedar."}}
{"op":"recall","question":"What is the project codename?"}
JSONL
```

| | Reply |
| --- | --- |
| The scaffold | `{}` then `{"memory": ""}` |
| Implemented | `{}` then `{"memory": "The project codename is Cedar."}` |

One JSON request per line on stdin, one reply per line on stdout, logs only on stderr.

## Connect the library

| Request | The wrapper | Reply |
| --- | --- | --- |
| `add` | Stores `session.text`, or text built from `session.turns` | `{}` |
| `recall` | Searches `question`, formats the records as text | `{"memory": "..."}` |

Examples: `app/nullius/catalogue/capabilities/mem0/capability.py` calls an upstream memory library; `app/nullius/catalogue/capabilities/keyword-memory/capability.py` is the protocol alone, BM25 over sessions.

## Declare dependencies

| Source | Fields |
| --- | --- |
| Python packages | Pinned versions in `install.pip` |
| A repository | `upstream.repository`, the full commit in `upstream.tested`, `install.project` in the checkout |
| Model calls | A relay [model configuration](../agents/add-manually.md#inspect-the-launch-configuration) |

```json title="capability.json · Mem0's Python dependencies (excerpt)"
{
  "install": {
    "python": "3.12",
    "pip": ["mem0ai==2.2.1", "fastembed==0.9.1", "spacy==3.8.18"]
  }
}
```

## Validate and prove

1. Validate (`ok`, nothing run):

    ```bash title="Validate the definition"
    nullius standard check \
      "$nullius_catalogue/capabilities/my-memory/capability.json"
    ```

2. Conformance:

    ```bash title="Run conformance"
    nullius standard conform my-memory \
      --provider openai --model gpt-5-mini
    ```

3. A small benchmark:

    ```bash title="Two units of LongMemEval"
    nullius standard fit longmemeval
    nullius standard run longmemeval my-memory \
      --provider openai --model gpt-5-mini --limit 2
    ```

| Rule | |
| --- | --- |
| The empty scaffold | Fails the recall checks |
| Success | Passing checks write `offers` and `proven`, never by hand |
| Scale | Inspect the stored and recalled text before raising the limit |

To contribute the entry to the bundled catalogue, copy its whole folder into
`app/nullius/catalogue/capabilities/` in a source checkout, run the relevant suites
and refresh `python3 app/tests/catalogue_docs.py --write`. The generated docs
read only bundled definitions.

!!! warning "Conformance and a benchmark make paid model calls"
    Each check and unit calls the model through the relay.

<div class="grid cards" markdown>

- [Supported capabilities](supported/README.md)

- [The memory port](../standard.md#the-ports-version-1)

</div>
