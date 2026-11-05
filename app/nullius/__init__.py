"""Nullius's own backend: everything the application does with a run.

This package owns the whole path from a directory of benchmark output to a
rendered page, one package per purpose:

- `runs/` — reading one Harbor job tree: `parse`, `classify`, `live` (the
  feed a running job appends), `artifacts`, `feasibility`, `launches`,
  `verifier` (a verifier's report, read as its catalogue entry describes it),
  and the `cli`.
- `traces/` — what a trial recorded: `trace`, `trajectory`,
  `conversation`, `charts`, `behavior`, `otel`.
- `compare/` — runs against runs.
- `catalogue/` — benchmarks and datasets: a folder per benchmark under
  `benchmarks/` (`benchmark.json`, its datasets and report block nested in it,
  and its `standard.json` when it runs on the Nullius standard).
- `recipes/` — `launch`, the vocabulary the run builder composes.
- `providers` — where a run's model calls go, and the builder's steps.
- `harbor/` — `source`: the Harbor a recipe pins, built here, kept, scanned.
- `importers/` — run trees other harnesses write: `atif`, `otel`, and the
  `cli`.
- `judge/` — judgement: `questions`, `state`, `client`, `store`, `batch`,
  `reconcile`, `lanes`.
- `execution`, `hosts`, `gitrefs` — running a recipe here or on a host, the
  hosts and their keys, a repository's branches and tags.
- `workspace`, `settings`, `redact` — cross-cutting, at the top.

**These are Nullius's own implementations**, each with the tests that pin it;
nothing in this application installs another package to run.

One name, one implementation: `app/tests/test_dependency_surface.py` counts
providers so a local package cannot shadow an installed distribution.
"""
