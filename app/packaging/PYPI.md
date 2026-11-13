# Nullius

Bring anything. Run it. Prove it.

Nullius runs AI evaluations on your machines and opens each score into the
evidence behind it, so you know whether the agent failed or the benchmark did.\
Add your own benchmark, agent or capability with a single prompt and iterate
faster.

## Install and launch

Install the latest version with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install nullius-eval
nullius serve
```

Open [127.0.0.1:5057](http://127.0.0.1:5057). The package name is `nullius-eval`;
the application and command are `nullius`.

Python 3.12 or later is required. Reading existing results needs no model key.
Running benchmarks requires the appropriate provider credentials and benchmark
prerequisites; Docker is needed for Harbor and the standard's container backend.

Release validation targets Linux with Python 3.12. The wheel is a Python package with
a local web UI, not a standalone desktop installer.

## What it does

- Bring benchmarks, agents and capabilities into a catalogue and match them by
  the ports they have proven.
- Build evaluation recipes, review their prerequisites, then run locally,
  over SSH or across several machines.
- Inspect trial outcomes, verifier results, model calls, conversations and
  artifacts; compare the shared tasks across runs.
- Check benchmarks with controls and inspect results with optional model
  judgement.

Settings, catalogue additions and runs are stored outside the installed
package. `nullius paths` shows their locations, and reinstalling the tool
preserves them.

## Updating

Update to the latest version with:

```bash
uv tool upgrade nullius-eval
```

To select a specific version, use `uv tool install nullius-eval==0.1.0`.
Add `--force` when replacing an existing installation. Explicit version pins
are retained by `uv tool upgrade`; use `uv tool install --force nullius-eval`
to return to the latest version without a pin.

If you used an earlier private build installed under the distribution name
`nullius`, remove that tool with `uv tool uninstall nullius` before installing
`nullius-eval`. The user storage remains in place.

## Links

- [Documentation](https://thinkflowlab.ai/nullius/docs/)
- [Quickstart](https://thinkflowlab.ai/nullius/docs/getting-started/)
- [Repository](https://github.com/thinkflowlab/nullius)
- [Report an issue](https://github.com/thinkflowlab/nullius/issues)

GitHub access follows the repository's permissions. Nullius is licensed under
Apache-2.0; bundled fonts retain their own included licences.
