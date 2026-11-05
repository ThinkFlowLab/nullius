---
name: import-benchmark
description: >-
  Bring a benchmark into Nullius - the entry point and the workflow. Finds the
  route first, and asks the user which - Harbor or Nullius (the standard):
  already in the catalogue (refresh it), in Harbor's registry, Hub or Harbor's
  adapters, or container tasks an agent works in (Harbor, via a new adapter),
  or scripts that answer and score (Nullius: its own scripts run as published
  as its Original setup, beside any agent or capability that fits; a user's fork
  is a version of it). Writes a Harbor catalogue entry when asked; for
  Nullius, hands over to import-standard. Use whenever someone wants to add,
  import, onboard or "support" a benchmark, a paper's repo or their own fork.
  Not for converting existing run output (nullius import).
---

# Import a benchmark

The user wants one thing: the benchmark in Nullius. Find the route, say it,
ask only what is theirs to decide.

Nullius is a facilitator, not a re-implementer: every route runs what the
upstream publishes - Harbor's dataset or adapter, the benchmark's own scripts,
its own scorer and prompts - pinned, in its own environment, never in the app.
A route that would need our own copy of upstream logic is not taken by
default: say what and why, and ask with the question tool before anything of
it is written.

Two skills share the work (an agent goes to `import-agent`, a capability to
`import-capability`):

- **`import-benchmark`** (this one) - the route, a Harbor catalogue entry, and
  the workflow below;
- **`import-standard`** - a benchmark on the Nullius standard: its driver, its
  ports, its setups (the paper's own scripts as published, the `original`
  setup), its options, its versions (a user's fork is one), the proofs.

A request for "the paper's scripts as published" goes to the standard's
Original setup.

## Locate the installation and catalogue

Use `nullius paths --json` with the same environment/profile as the app.
`NULLIUS_HOME` selects a profile's `config/`, `data/` and `cache/`; otherwise
platform user paths apply. `NULLIUS_CATALOGUE` overrides the user catalogue,
whose Linux default is `~/.local/share/nullius/catalogue/`. Personal settings,
secrets, run data and caches belong outside the installation.

For Python API calls, use the interpreter behind the `nullius` launcher.
A `uv tool` installation is separate from the shell's `python3`. On POSIX,
check `command -v nullius`, set `NULLIUS_PY="$(uv tool dir)/nullius/bin/python"`,
and verify `"$NULLIUS_PY" -c 'import sys, nullius; print(sys.executable); print(nullius.__file__)'`.
For another installation, inspect its launcher or virtual environment. From
a source checkout root, use `NULLIUS_PY=python3` with `PYTHONPATH="$PWD/app"`
on the command below:

```bash
"$NULLIUS_PY" - <<'PY'
from nullius import paths, settings
settings.load(paths.settings_file())
from nullius.catalogue import catalog, harbor_entry
from nullius.runs import verifier
for module in (catalog, harbor_entry, verifier):
    print(module.__name__, module.__file__)
print("catalogue:", paths.catalogue_dir())
for entry in catalog.load().benchmarks:
    print(entry.key, catalog.entry_dir(entry.key))
PY
```

Read the installed modules at those paths when no source tree is present;
references below to `app/nullius/` are their source-checkout locations.
Path APIs return strings; join with `os.path.join` or `Path(value)`.
`catalog.entry_dir(key)` finds the selected benchmark folder, whether bundled
or personal. Before editing, use `catalog.writable_dir(key)` in the same
settings-loaded interpreter: it copies the entire bundled folder to the user
catalogue and returns the writable path. For a new entry, create that folder,
or use `harbor_entry.save` after its validation. User folders replace bundled
folders in full, even when incomplete; retain `standard.json`, drivers and
other resources when adding a Harbor route. Never edit `site-packages` or
the private `_nullius_app` UI package.

Default to a personal import under the active catalogue's `benchmarks/KEY/`.
No source checkout, repository tests, generated docs or sprite change is
required. An explicitly requested bundled contribution instead ends in
`app/nullius/catalogue/benchmarks/<key>/`: author in a user/scratch catalogue,
then copy the intended entry files to the checkout and follow its repository
gates. CLI/API write helpers protect the bundled tree in either layout.

Use `nullius standard` for the standard workflow and `nullius serve --no-debug`
for the installed app. `python -m nullius` supports the same commands only
with a Python that has Nullius. From a checkout root the legacy
`PYTHONPATH=app python3 -m nullius.standard.cli` form remains valid. An absent
`app/` folder is normal for an installation, not a reason to stop the import.

## Route first

Answer the route before any deep work, from the catalogue
(`catalog.load()`, bundled entries plus whole-folder user overrides), the benchmark's README and its
repository's layout: minutes, no download.

| what you find | route | recommend |
|---|---|---|
| it is in the catalogue already | refresh that entry: a newer version, the current repository; on the standard, a new version (`import-standard`) | strongly - never a second way in for a benchmark the app runs |
| it is in Harbor's registry or Hub, Harbor has an adapter for it (`adapters/<name>/` in Harbor's repository), or its README runs `harbor run` | Harbor: a catalogue entry for that dataset | strongly, for Harbor's agents; the standard too when any agent or capability should run it |
| container tasks: an agent works in an environment, a verifier scores inside it | Harbor style (an adapter, `harbor adapter init`), or the standard's own images (`import-standard`) | Harbor when it has the tasks already |
| scripts that answer and score: a QA set, a memory benchmark, a paper's evaluation script | Nullius: the paper's pipeline as its `original` setup, and a port for any agent or capability | as published; a user's fork is a version of the same entry |

Then ask the route - with the question tool, before any deep work, even when
the table is clear: the user picks how it runs. Two options, the recommended
one first with the table's reason in its description:

- **Harbor** - its tasks as Harbor trials (a catalogue entry for a dataset
  Harbor has, or an adapter in Harbor's repository);
- **Nullius** - through the Nullius standard: the paper's own scripts as
  published (its Original setup) and any agent or capability that proved the
  ports it needs: hand over to `import-standard`.

Leave out a route that cannot carry it, and say why in one line. If the user
already named one (the prompt or the conversation), do not ask again: say
which you took. Both can sit on one entry, so "both" is an answer too.

A user's own repository - a fork they fixed or extended - is a version of the
benchmark's one entry on the standard (`import-standard`, "the user's own
source"), never a second entry.

## Citation: the benchmark's papers only

On every route, `citation` holds the BibTeX, verbatim from the official
repository (its README or a CITATION file), of the papers that introduce or
define this benchmark - nothing else: not work the README credits or
acknowledges (task fixes, a tool, another dataset), not related work, not a
mode the entry does not run. With no BibTeX upstream, the benchmark's paper.
`citation_source` is the URL it came from.

## The Harbor routes

The active user catalogue holds a personal entry; the source repository holds
a bundled contribution. An adapter goes to Harbor's repository through its
own review. Say the route and stop there, unless the user asks for the entry.
When they do, write it locally:

- **Versions first.** List every version there is, wherever it is served: the
  Harbor registry, the Harbor Hub, the upstream organization's other
  repositories (a later version often lives in a repository of its own), tags
  and releases. With more than one, ask which, with the question tool.
- **The entry.** `benchmark.json` in the writable benchmark folder located
  above, named after the key, mirroring an existing Harbor benchmark's:
  `harness: ["harbor"]`, its `origin` per the docstring of
  `app/nullius/catalogue/catalog.py` (`official`, the publisher's run as it
  certifies, or `experiment`, adapted here), its `scoring` in two or three words (the
  card's tag: `Tests`, `Lean compiler`), and its `datasets` nested in it, one by
  registry or Hub name (or local path) with its task count, `official` link,
  and `verifier` - and its `citation` and `citation_source`. The builder takes a benchmark's first dataset as its default, so a
  second version is a benchmark entry of its own.
- **What reads its trials.** The `source` a trial records (the registry name,
  the Hub's `org/name`, or a `-p` folder's name) must reach the benchmark's
  report block - its `sources` in `benchmark.json`, matched exactly; a
  verifier that writes its own report also gets the block's `file`, pass and
  fail fields and `words` (`app/nullius/runs/verifier.py` says the shape).
  Keep the description in the entry's fields. The legacy `DATASETS` text in
  `app/web/shared.py` has a fallback for unknown datasets; a personal import
  does not require editing that module.
- **Personal checks.** In the same settings-loaded interpreter, run
  `catalog.validate(catalog.load())` and resolve every reported problem in
  the changed entry. Use `nullius standard check` too when it also has a
  standard route. Open `nullius serve --no-debug` with the same profile and
  check its benchmark page, datasets and builder choices. Use an
  existing trial to check report rendering when one is available; adding a
  catalogue entry does not authorize a benchmark run.
- **Bundled contributions only.** Add its mark to
  `app/static/catalogue/marks.svg`, update affected exact lists/counts in
  `app/tests` and `app/ui_tests`, and the legacy dataset description where
  relevant. Refresh `python3 app/tests/catalogue_docs.py --write`; run the
  backend and route suites from AGENTS.md, and
  `python3 .claude/skills/docs-drift/scripts/check_docs.py`. Use an isolated
  profile and an empty user catalogue so personal overrides cannot mask the
  bundled entry. These repository gates do not apply to a personal addition;
  it uses the fallback icon. Ask what, if anything, goes upstream only when
  publication was not already authorized.

## The workflow

```
"add X" → route (above), asked
  ├─ Harbor → say the route; the entry when asked
  └─ Nullius → import-standard (its setups, ports, proofs)
"a new version of X", "my fork of X" → import-standard, a version of X
```

Nothing is built before the user's yes, and every change is made locally
first. A route that ends here - Harbor, a refresh - reports decision first:

```
benchmark <key>: <route>   <one line why>
destination: <personal catalogue folder | bundled contribution folder>
entry: <written | not asked>   datasets: <each, tasks>   left out: <what, why>
ours: none | <file: what, why>
```

then one row per thing that cannot run or differs from the paper (skip /
local change / issue), and the questions, with the question tool.

## Catalogue presentation

Keep the benchmark description and each option summary to a short sentence.
Put per-value explanations in `about` and prerequisites in `needs` / `host`,
rather than repeating them in the summary. Preserve exact model restrictions,
scoring rules and comparison limits in the setup definition: the page keeps
the full method under **How it runs**, with model and comparison scope visible.
Do not add custom prose-heavy layout or one full-width row per simple value.
Check the shared benchmark page at desktop and mobile widths; simple options,
detailed prerequisites and single/multiple datasets should remain readable.


## Participant and model choices

The catalogue presents one Original procedure. Put a published Harbor agent in
`benchmark.json.published.agent` so the builder promotes it to the reference
group. For a standard benchmark, follow import-standard's `reference_agent`
and paper-model declarations. Keep participant choice on Agent/Memory and
model filtering on Model; do not add separate Original/your-model setup rows.
