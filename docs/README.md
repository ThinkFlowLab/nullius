# Introduction

Nullius runs AI evaluations on your machines and opens every score into the evidence behind it.

| Do | How |
| --- | --- |
| Run | A benchmark against agents, capabilities or its own setup, on the Nullius standard or Harbor |
| Open | Nullius and Harbor jobs; ATIF and OpenTelemetry exports, converted on open |
| Inspect | Trials, conversations, traces, verifier reports, judgements; no model key |
| Compare | Runs on their shared tasks |
| Bring in | A benchmark, agent or capability, by one prompt or by hand |

<div class="grid cards" markdown>

- [Quickstart](getting-started.md)
- [Core concepts](concepts.md)
- [Run builder](builder.md)
- [Read results](results.md)

</div>

## Sections

| Section | Holds |
| --- | --- |
| [Settings](settings/README.md) | Model providers, judges, agent keys, run hosts |
| [Benchmarks](benchmarks/supported/README.md) | Every benchmark, and how to add one |
| [Agents](agents/supported/README.md) | Every agent, its proven ports, how to add one |
| [Capabilities](capabilities/supported/README.md) | Every capability, its port, how to add one |
| [The standard](standard.md) | Ports, kits, setups, controls and proofs |
| [CLI](cli/README.md) | Every command, by topic |
| [Release notes](releases/v0.1.0rc1.md) | Highlights, changelog and installation notes by version |
| [Architecture](how-it-works.md) | Execution paths and files on disk |
| [Metric field guide](field-guide.md) | Every metric and outcome; *Reference → Metrics* on the website |

## This folder

| Path | Holds |
| --- | --- |
| `settings/`, `cli/`, `benchmarks/`, `agents/`, `capabilities/` | The sections above |
| `releases/` | Reviewed notes for each release |
| `examples/new-benchmark/` | A worked Harbor entry to copy |
| `assets/` | The screenshots these pages use |
| `diagrams/` | The figures these pages use, drawn by `app/site/diagrams.py` |
