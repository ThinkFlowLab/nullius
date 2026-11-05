# Add a benchmark manually

A standard benchmark from a scaffold, its driver calling the upstream's own code; a Harbor benchmark from the form or a copied folder ([choose the route](../examples/new-benchmark/README.md#choose-the-route)).

## A standard benchmark

1. Scaffold the definition and driver in your user catalogue:

    ```bash title="Scaffold my-benchmark"
    nullius standard new benchmark my-benchmark
    nullius_catalogue=$(nullius paths --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["catalogue"])')
    ```

2. Replace the placeholders of `standard.json`:

    | Field | Supply |
    | --- | --- |
    | `upstream.repository` | Its URL |
    | `upstream.tested` | The full commit integrated |
    | `datasets[].path` | The file's path in that upstream |
    | `datasets[].sha256` | `sha256sum /path/to/dataset.jsonl` of that exact file |
    | `datasets[].units` | How many units |
    | `needs` | The ports really needed |
    | `score.says` | What a passing score means |

3. Implement the driver's three handlers by calling the benchmark's own code:

    | Handler | Reply |
    | --- | --- |
    | `units(dataset)` | `{"units": ["task-1", "task-2"]}` |
    | `setup(unit, dir)` | `{"prompt": "The task instruction"}` for an agent benchmark |
    | `score(unit, dir, transcript, answer)` | `{"reward": 1.0, "detail": {}}` for a passing binary task |

4. Add `benchmark.json` beside it: the listing.

5. Check, prove, run:

    ```bash title="Check one definition, then the whole catalogue"
    nullius standard check \
      "$nullius_catalogue/benchmarks/my-benchmark/standard.json"
    nullius standard check
    ```

    ```bash title="Run the controls on a small subset"
    nullius standard prove my-benchmark --limit 2
    ```

| Rule | |
| --- | --- |
| Placeholders | Errors while they remain; `ok` proves the shape, not the scoring |
| The scaffold | No units, an empty prompt, a zero reward until implemented |
| To copy | `app/nullius/catalogue/benchmarks/gsm8k/` |
| The upstream | Called, never copied; its bugs kept, named in the limitations |

The commands also work [from source](../cli/README.md). Additions go under the
user catalogue reported by `nullius paths`, without changing the installed
package, icon sprite or project tests. A folder with a bundled key overrides
that entire entry; copy the complete folder before editing it.

For a contribution to the bundled catalogue, copy the finished entry into
`app/nullius/catalogue/benchmarks/` in a source checkout, add its mark in
`app/static/catalogue/marks.svg`, run the relevant suites and refresh
`python3 app/tests/catalogue_docs.py --write`. Those are contribution steps,
not requirements for a personal addition.

!!! warning "A benchmark without working controls has proven nothing"
    Inspect the control jobs and the proof report before running an agent on it.

## A Harbor benchmark

*+ Add a benchmark*, *Add a Harbor benchmark*: name the dataset; the form reads its task count and limits from Harbor. Then inspect its datasets on the benchmark's page.

By hand: the [worked Harbor entry](../examples/new-benchmark/README.md).

<div class="grid cards" markdown>

- [A benchmark, in full](../standard.md#a-benchmark)

- [Controls and proofs](../standard.md#controls-and-proofs)

- [Add with an assistant](add-with-assistant.md)

</div>
