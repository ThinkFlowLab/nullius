# Definitions

Every definition, whether it is sound, which pairs fit, and a new one to fill in.

```bash title="Definitions"
nullius standard list
nullius standard check [path]
nullius standard fit <benchmark>
nullius standard pins <benchmark> [--tag TAG] [--limit N]
nullius standard new {benchmark,agent,capability} <key> [--family {agent,capability}] [--port PORT]
nullius standard new version <key> <version>
```

| Subcommand | Does |
| --- | --- |
| `list` | Every definition, which pairs fit, each `proven` record (kit job, date, model, score, kit version, skills) |
| `check [path]` | The definitions, or one file, against the catalogue and the kits |
| `fit <benchmark>` | What can run it, and why the rest cannot |
| `pins <benchmark>` | Compare `images.json`, or literal image pins in `driver.py`, with registry tags; report same, moved (both digests), gone, or not checked |
| `new benchmark KEY` | `standard.json` and `driver.py` under `benchmarks/<key>/` in the user catalogue; `--family capability --port PORT` for a capability benchmark |
| `new agent KEY`, `new capability KEY` | A definition to fill in, in its user catalogue folder |
| `new version KEY VERSION` | The default's block repinned, nothing proven; a file without `versions` becomes one, proofs kept |

`nullius paths` locates the user catalogue. `new`, `prove`, `conform` and
`fidelity` write there; editing an existing bundled entry first copies its
whole folder. A user entry replaces the bundled entry of the same kind and
key, including its local resources.

`pins` reads registry manifest headers without downloading images or changing
pins. It compares each embedded tag, or `latest` when absent; `--tag` overrides
it and `--limit` checks only the first N pins. Exit codes: 0 all checked pins
match, 1 a tag moved or is gone, 2 a check could not complete (including auth
or rate limits). Anonymous registry authentication is supported; private
credentials are not loaded. Refreshing pins and reproving controls stays manual.

!!! note "A new version runs nothing"
    Until `conform KEY@VERSION` passes its kits.

<div class="grid cards" markdown>

- [The standard](../standard.md#import-by-hand)

    Importing by hand.

- [Conform an agent or a capability](conform.md)

    Prove its ports.

</div>
