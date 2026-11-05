# Prove a benchmark

Two controls and two driver checks per dataset, written into the definition.

```bash title="Prove"
nullius standard prove <benchmark>[@version] [--dataset D] [--limit N] [-j N] \
    [--judge-model M] [--provider P] [--jobs DIR] [--stamp STAMP] [--shard I/N] [--launch LAUNCH] [--remove-images] [--from-run]
```

| Check | Passes when |
| --- | --- |
| `nop` | Nothing passes, or every score at the floor (`expect`) |
| `oracle` | Everything passes, or every score at the ceiling (`expect`) |
| `units` | Two drivers give the same unique ids, as many as declared |
| `leak` | After `setup`, the oracle's answer is in neither prompt nor workspace; references under 12 characters skipped, counted |

| Flag | Does |
| --- | --- |
| `--dataset` | One dataset (default: each) |
| `--limit` | The first N units; fewer never replaces more |
| `--judge-model`, `--provider` | The judge and its provider |
| `--jobs`, `--stamp` | Control jobs' directory (default `jobs_standard/`) and time stamp |
| `--shard I/N`, `--launch` | Controls and leak on that shard, units on all; recorded as `shard` |
| `--remove-images` | Remove each image the proof pulls or builds after its unit, so the oracle pulls it again; by default they stay |
| `--from-run` | Started by Nullius: `OPENAI_API_KEY` only, saved settings unread |

Passes go under `proven.<dataset>` in `standard.json`, fails under `proven.<dataset>.failed`. `prove KEY@NAME` proves a version; `run KEY nop` runs one control ([Run a benchmark](run.md)).

<div class="grid cards" markdown>

- [The standard](../standard.md#controls-and-proofs)

    The record.

- [Conform an agent or a capability](conform.md)

    The other side of a proof.

</div>
