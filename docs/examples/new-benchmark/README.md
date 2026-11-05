# Adding a benchmark by hand

A worked Harbor entry to copy: `harbor/` holds a `benchmark.json`.

Made up: its names and links show the shape. `app/tests/test_catalog.py` (`Example`) loads it with the catalogue's own checks.

!!! tip "The same path, without the typing"
    *+ Add a benchmark* writes the `/import-benchmark` prompt, or a user catalogue folder from a Harbor dataset (*Add a Harbor benchmark*).

## Choose the route

| The benchmark | Route |
| --- | --- |
| Ships scripts that answer and score | **Nullius standard**: its `original` setup ([the standard](../../standard.md#a-benchmark), the `/import-standard` skill) |
| On the Harbor registry or hub, or with a Harbor adapter (task folders: environment and scoring test) | **Harbor**: a catalogue entry |
| Data only | Neither yet: a Harbor adapter comes first |

## Copy the folder

1. Copy the example under the benchmark's key in your user catalogue:

    ```bash title="With a source checkout available to read the example"
    nullius_catalogue=$(nullius paths --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["catalogue"])')
    mkdir -p "$nullius_catalogue/benchmarks"
    test ! -e "$nullius_catalogue/benchmarks/my-bench" && \
      cp -r /path/to/nullius/docs/examples/new-benchmark/harbor "$nullius_catalogue/benchmarks/my-bench"
    ```

2. Edit `benchmark.json`; every dataset key is unique across the catalogue.

An installed application can create the same entry through the Harbor form;
it needs no source checkout. Leave installed package files unchanged. A user
folder replaces the entire bundled entry of the same key, and survives upgrades.

| Field | Says |
| --- | --- |
| `key`, `title`, `category`, `domain`, `scoring` | The folder's name; the name shown; one tag (Coding, Memory); one line on the tasks; how a task is scored, in two or three words |
| `harness` | `["harbor"]`, plus `"standard"` when it runs there too |
| `origin` | `official` (default): the publisher's, run as it certifies; `experiment`: adapted here, flaws still to find |
| `upstream_limitations` | What the benchmark as published gets wrong or leaves out: not Nullius's to fix |
| `limitations` | Nullius's own: where running it here differs, or what is not offered yet |
| `datasets` | `key`, `title`, `tasks`, `official` (upstream link), `source` (a phrase), `registry` (`name@version`) or `hub` (`org/name`), `verifier` |
| `report` | Optional: how the verifier's report is read, shaped by `app/nullius/runs/verifier.py` |
| `citation`, `citation_source` | BibTeX, and its source |

Full rules: the docstring of `app/nullius/catalogue/catalog.py`.

## Tasks a program generates

For a personal entry, run the adapter separately and select its task directory
in the builder. A contribution to the bundled catalogue can add a `LAUNCH`
line in `app/nullius/recipes/launch.py`; the builder then offers *Generate tasks*:
`uv run <program> --output-dir <folder>` in that checkout.

```python title="app/nullius/recipes/launch.py · one dataset"
"my-bench-v1": {"local": "../harbor-datasets/my-bench",
                "generate": {"program": "my-bench", "checkout": "Harbor checkout · adapters/my-bench",
                             "example": "../harbor/adapters/my-bench"}},
```

## The mark

Personal entries use the default catalogue icon, or a logo uploaded through
the benchmark form. No installed sprite edit is needed. A bundled contribution
adds a `<symbol id="bm-my-bench" viewBox="0 0 24 24">` in
`app/static/catalogue/marks.svg`, the maker's logo or a line icon.

## Check, then run

For a personal entry, inspect `/benchmarks/my-bench`, its datasets and
builder choices. Run its controls (`nop` and `oracle`), then
a few tasks, and inspect the resulting jobs.

For a bundled contribution, copy the finished folder into
`app/nullius/catalogue/benchmarks/`, add the mark and run the repository checks:

```bash title="Bundled contribution checks, from the repository root"
python3 app/tests/catalogue_docs.py --write
python3 app/tests/parallel.py
uv run --no-project --with-requirements app/requirements.txt python app/tests/parallel.py app/ui_tests
```

!!! note "A run changes nothing in the entry"
    `origin` says where the benchmark comes from, not whether it has run.

<div class="grid cards" markdown>

- [Supported benchmarks](../../benchmarks/supported/README.md)

- [Add a benchmark manually](../../benchmarks/add-manually.md)

</div>
