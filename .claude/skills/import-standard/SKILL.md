---
name: import-standard
description: >-
  Bring a benchmark, an agent or a capability into Nullius through the Nullius
  standard (docs/standard.md): ports a benchmark needs and an agent or capability
  proves, ACP to drive agents, MCP for environments, JSON-lines drivers, the
  relay for every model call. Finds the kind and the ports, writes the
  definition and its driver or wrapper, pins and checks it, runs a two-unit
  trial and the conformance kits, and reports decision first. Calls the
  upstream's own code at a pinned commit; never re-implements it without the
  user's yes. Use whenever someone wants to add, import, onboard or wrap a
  benchmark meant for any agent or any capability - even if they never say
  "standard" or "port". An agent goes to import-agent, a capability to
  import-capability (this skill still works for them). Not for Harbor benchmarks
  run by Harbor agents (import-benchmark), nor for converting existing run
  output (nullius.importers).
---

# Import into the Nullius standard

The user wants one thing: their benchmark, agent or capability to run in Nullius
against everything it fits. Read `docs/standard.md` when working in a checkout;
for an installation, read the installed schema and examples located below.
References below to sections of `docs/standard.md` are further explanation
when that documentation is available; they do not require a checkout. An
absent `app/` directory is normal in an installation.

## Runtime and edit destination

`cli` below means `nullius standard`. `python -m nullius standard` is equivalent
only with a Python that has Nullius installed. From a source checkout root,
`PYTHONPATH=app python3 -m nullius.standard.cli` remains supported. Direct
standard commands write jobs to `jobs_standard/` relative to the current
directory unless `--jobs` names another location; choose a writable location
outside the installed package. Builder runs have their own user-data root.

Use `nullius paths --json` with the application's environment/profile to find
settings, secrets, catalogue, workspaces, runs and cache. `NULLIUS_HOME` selects
a profile with `config/`, `data/` and `cache/`; otherwise platform user paths
apply (XDG on Linux). Explicit overrides such as `NULLIUS_CATALOGUE` still win.
Keep personal state outside package resources. The private `_nullius_app`
namespace holds the installed UI; launch it through `nullius serve`.

Locate modules with the interpreter that runs this Nullius. A `uv tool`
installation does not put its modules in the shell's `python3`. On POSIX, for
the tool named `nullius`, verify:

```bash
command -v nullius
NULLIUS_PY="$(uv tool dir)/nullius/bin/python"
"$NULLIUS_PY" -c 'import sys, nullius; print(sys.executable); print(nullius.__file__)'
```

If that is not the installation in use, inspect its launcher or virtual
environment and select that interpreter. For source inspection from the
checkout root, use `NULLIUS_PY=python3` with `PYTHONPATH="$PWD/app"` on each
Python command below. Read module files, never edit an installed module:

```bash
"$NULLIUS_PY" - <<'PY'
from nullius import paths, settings
settings.load(paths.settings_file())
from nullius.standard import definitions, howto, ports
from nullius.catalogue import catalog
for module in (definitions, howto, ports, catalog):
    print(module.__name__, module.__file__)
print("catalogue:", paths.catalogue_dir())
PY
```

References below to `app/nullius/` mean those modules in a checkout, or
their resolved installed files for reading. Path APIs return **strings**;
use `os.path.join` or `Path(value)` when joining. Resolve the selected entry
with `definitions.entry_dir(kind, key)` (`kind` is `benchmark`, `agent` or
`capability`); do not derive it from the package directory. Before a manual edit,
call `definitions.writable_dir(kind, key)` in the same settings-loaded
interpreter: it copies a bundled entry's whole folder to the user catalogue
and returns the writable path. For a new key, create the returned directory,
or let `cli new` create it. A user folder replaces the bundled folder **in
full**, even if incomplete: keep its JSON, drivers, wrappers, model templates
and other resources together. `new version`, `conform` and `prove` write user
copies too. Never edit `site-packages`.

Default to a personal import in `paths.catalogue_dir()` (Linux default:
`~/.local/share/nullius/catalogue/`), in `benchmarks/KEY`, `agents/KEY` or
`capabilities/KEY`. It needs no source checkout, repository tests, generated docs
or sprite edit. A requested **bundled contribution** instead ends in
`app/nullius/catalogue/<group>/KEY/`: author and prove in a writable user or
scratch catalogue, then copy only the intended entry files into the checkout.
The CLI protects bundled resources even in a checkout; do not point its write
commands at the bundled tree. Run the repository gates below with an isolated
profile and an empty user catalogue so an overlay cannot hide the contribution.

**Facilitator, not re-implementer.** Nullius runs what the upstream ships:
its loader, prompts, tools, user simulator, scorer, library, CLI or ACP
adapter - called or imported at the pinned commit or version, never copied
or rewritten. Its dependencies live in its own environment (the driver's,
the agent's, the capability's install), never in the app (Flask only) and never
in the agent's environment for a benchmark's needs. Our code is glue only: a
thin driver or wrapper that calls the upstream, the pins, the install, the
relay, the ACP client, the kits, reading results.

**Pin a release, keep its bugs.** Pin an upstream's latest release tag when
it has one (`git ls-remote --tags URL`; ignore pre-releases and side tags;
record `upstream.tag`, the tag's commit in `upstream.tested` - for an
annotated tag the `^{}` line's commit, not the tag object), and a commit
only when it has no release or the user asks. Never patch an upstream's bug
or work around it (no monkeypatch, no changed default, no dependency it does
not declare, no retry or timeout of ours): keep its behaviour, say it in the
limitations, and ask when it blocks a run. A fix is the upstream's - or the
user's fork's, brought in as a version (below).

**Ask before re-implementing.** When any piece seems to need our own version
of upstream logic (a scorer, a prompt, a tool, a simulator, a loader the
upstream has but cannot be called as is), stop: say what, why the upstream
cannot be called, and what it would cost in fidelity, and ask with the
question tool. Write nothing of it before the yes. Without a way to ask (a
subagent), report it as an open question and leave it unwritten.

## 1. Which kind, which route (minutes, no download)

| what the user has | kind | route |
|---|---|---|
| tasks and a way to score them, meant for any agent | benchmark, `agent` family | driver + `standard.json` |
| questions over a history, meant for a memory or retriever | benchmark, `capability` family | driver with `produce` + `standard.json` |
| a program that does tasks with a model (CLI agent, coding agent) | agent | the `import-agent` skill (`.claude/skills/import-agent/SKILL.md`); its Agent part below if invoked here |
| a component a benchmark calls (memory, retriever) | capability | the `import-capability` skill (`.claude/skills/import-capability/SKILL.md`); its Capability part below if invoked here |
| a benchmark Harbor already runs, for Harbor agents | not this skill | `import-benchmark` |
| the user's own source of a benchmark already here (a fork with a fix, any repository, private included) | a version of that benchmark | `versions.<name>.upstream`, below |

Say the route in one line before any deep work. Inspect the effective
catalogue with `cli list` and `catalog.load()`. If a benchmark is already
present, extend its writable copy (`standard.json` beside `benchmark.json`),
never a second key. Preserve both routes when it already runs through Harbor.

## 2. The ports - the decision that matters most

- **A benchmark needs the fewest ports that are true of it.** Work done in
  files: `agent.workspace/v1`. An environment of tools: serve them as an MCP
  server from the driver's `setup` and need `agent.tools/v1`. A user who
  speaks more than once: `agent.conversation/v1` and a `turn` in the driver;
  a user a model plays: its model in `simulator`, asked through the relay from
  `turn` (`docs/standard.md`, step 4 of "A benchmark"). A leaderboard whose
  submission rules fix the simulator, the judge, options or attempts: its
  `leaderboard` block, from those rules and its published submissions.
  Only the final message matters: `agent.task/v1`. A port too many shuts out
  agents for nothing; a port too few makes runs fail instead of being refused.
- **A port that does not exist yet is a generic change, not a special case.**
  Propose it as a new port with a kit (`app/nullius/standard/ports.py` and
  `standard/kits/<name>/`), worded for every benchmark of its kind; never
  shape it after the one at hand. Ask before adding it. This requires a
  source contribution; a user catalogue cannot extend installed core code.
- **An agent or a capability never writes `offers` by hand.** `cli conform` does.

## 3. Build

**Benchmark.** `cli new benchmark KEY [--family capability --port cap.memory/v1]`
prints the writable folder under the active user catalogue. Work there, then:
- pin `upstream.repository`, `upstream.tag` (its latest release, when it has
  one) and `upstream.tested` (the tag's full commit, else a commit of the
  default branch: `git ls-remote`); fetch that commit into the scratchpad (`git init`, `git fetch
  --depth 1 URL COMMIT`, `git checkout FETCH_HEAD`) to read the loader and
  scorer and to hash each dataset: its `path`, `sha256` and `units`; add
  `tested_date`, and `limits.turn_seconds` when a unit may take long;
- unit ids: the data's own ids where it has them, else a stable position
  (`0001`); they become trial names;
- write the driver's `units`, `setup`, `score` (and `turn`, `produce`,
  `oracle` when the ports call for them, `answer` for the `original` and
  `model` setups) as calls into the upstream's own
  loader, prompts, tools, simulator and scorer, imported or run from
  `$NULLIUS_UPSTREAM` at the pinned commit - the driver translates JSON lines
  to those calls and back, nothing more. When the upstream needs packages of
  its own, the driver gets an environment of them: an `install` block shaped
  like a capability's (`pip` pinned, or `project` from the upstream, `python`,
  `indexes`), its command using `{install}`; read the current schema
  (`cli check`, `docs/standard.md`) for the exact form before writing it.
  An MCP server of the environment's tools is the upstream's tools served
  as they are; when they need the upstream's packages, declare it in
  `mcp_servers` so it runs on the machine in the driver's environment, and
  name it from `setup`'s `mcp` entry (`"server"`) - never install the
  upstream into the agent's container (`docs/standard.md`, "A benchmark's
  MCP server on the machine"). A project with a lock file installs at its
  pins: `driver.install.locked`. Say in `score.says` and the entry's limitations whatever
  differs from the paper. `score` replies `{reward, detail}`, plus `passed`
  for a pass/fail benchmark;
- every dataset the upstream publishes for evaluation, each a dataset here
  (its default task set where it has splits; a domain or split loaded by the
  upstream's own loader is a dataset with no file); what is left out - a
  training set, a test fixture, a mode not run - is said in the limitations;
- **every setting of the upstream, inventoried - not only the ones the
  driver touches.** List every evaluation entry point the paper's numbers
  come from (each `scripts/*.sh`, each Python main, each README command, the
  CLI's run command) and, for each, every flag, config key, environment
  variable and hard-coded variant with its values and default - the values
  the upstream's own scripts use first. Write it to
  `upstream-settings.json` in the benchmark's folder (`docs/standard.md`,
  "Setups"): one row per setting, `{entry, setting, values, default, nullius}`,
  where `nullius` is exactly one of `option:<name>`, `setup:<original|model>`,
  `dataset:<id>`, `fixed:<value> - <why: the upstream's default, or why ours>`
  or `not offered - <why>`. A run of the benchmark is not imported while a
  row is missing; `cli check` reads the file. Then decide each row:
  - **the paper's own answerer** (its reader, its agent loop, its retrieval
    pipeline) with the paper's models is the `original` setup, every mode the
    paper reports a row of it (whole context and retrieval alike); the same
    pipeline with another model is the `model` setup; a pipeline the paper
    measures as a memory or retriever is also offered as a reference capability,
    so other capabilities compare with it. Models the upstream's code asks
    besides the answerer (an extraction or summary model) are the original's
    `side_models`, sent as the provider names them (`served_as`), never as
    the reader;
  - **a choice a run makes that changes what is measured** (a search, a
    retriever, a top-k, a reading method, a mode, a batch, a model list) is a
    benchmark option (`options` in `standard.json`: `values`, `default`, the
    `datasets` it applies to, a line `about` each value, what a value
    `needs`), or a setting of a setup, read by the driver as
    `NULLIUS_OPTION_<NAME>` - never a value of ours hard-coded in the driver.
    The default is the upstream's own, unless that needs a key or a machine's
    setup: then the one that needs neither, said in the `summary` with the
    upstream's. A value that calls an embedding model names it in `needs`
    (its calls go through the run's relay); one that needs programs on the
    machine names a `host` requirement (`{summary, check, install: {apt,
    npm}}`, the upstream's own pins) - the builder checks and installs it;
    the machine's own hardware (a CUDA GPU) is a `host` requirement with a
    `check` (`nvidia-smi`) and no `install`. An option's `setups` says which
    ways of running it belongs to (`original`, `model`, or `yours`, the agent
    or capability under test), and the builder's Agent step shows it under that way; an
    option that only matters under another's values (a top-k, only with a
    retrieval) names them in `when`; `label` when the name does not read
    (an acronym, a run-together word); `summary` one sentence (a tooltip),
    the detail in the limitations;
  - **a value whose calls cannot pass the relay** (an address or key the
    upstream hard-codes), or that needs what cannot be had (a retired model),
    is `not offered`, with why, and said in the limitations;
  - **a choice the driver already makes** - a template, a length, an image, a
    limit - is checked the same way: when the upstream's default differs, it
    is an option with the upstream's default, in a new version when it
    changes what earlier runs measured (the driver keeps the old behaviour by
    `$NULLIUS_VERSION`).
  "The driver does not call that code" is never a reason to leave a setting
  out: it is the reason for a setup.
- model weights the driver fetches for the upstream (an encoder the README
  has downloaded by hand): into the upstream's own cache, once, shared by
  every unit; one weight format only (the one its loader reads first:
  `model.safetensors` before `pytorch_model.bin`, never both); fetched again
  unless every file the upstream loads is there;
- a unit's own limits the upstream declares (a task's agent timeout):
  `setup` returns `agent_seconds`, never one `limits.turn_seconds` for all;
- keep tests and answers out of the workspace until `score`;
- tasks that need different software (a licensed tool, a toolchain, a
  runtime): `environments`, one Dockerfile each in the benchmark's folder, a
  layer on the base (no `FROM`, `COPY` only of its listed `files`), the
  unit's `setup` naming it (`"environment"`) or its dataset giving a default;
  a licence is a `needs_secrets` entry with `to: agent` or `to: driver` and a
  `kind` - `value` (a key), `file` (mounted at its `path`) or `server` (the
  one `host:port` the agent may reach) - never a file in the image; the user
  sets it on the benchmark's page (`docs/standard.md`, "Per-task environments");
- tasks that are containers (each its own Dockerfile and `FROM`, the task
  the container's state): one environment with `"image": "own"`; `setup`
  names the unit's `image` (`build`, a folder of the upstream, or `pull` by
  digest) and its `verify` step (tests copied in after the agent, run there,
  results collected), `score` reads `verified`, the `oracle` replies with a
  `solve` step; tasks whose solution needs the internet are left out and
  said (`docs/standard.md`, "Own images");
- an upstream whose own loop holds the conversation (it executes the tool
  calls, counts steps and errors, ends it by its rules, and its evaluator
  scores what the loop returns): wrap the ACP agent as the upstream's own
  kind of agent and run the upstream's loop in the driver - through its
  batch runner where it has one, so its seeds and its retries are its own
  (a retry answers `turn` with `restart`) - its MCP server passing each call
  to that loop; never a loop of ours that copies its limits
  (`docs/standard.md`, "A driver that holds the conversation");
- keep one integration per benchmark. Add reading, retrieval and execution choices
  as options; update the existing driver and invalidate affected proofs rather than
  creating numbered development revisions. Reserve versions for explicit upstream
  releases or user-requested forks; never copy proofs from a different implementation;
- **the user's own source** - a fork with a fix, at a tag, a branch or a
  commit, any repository (a private one is fetched with this machine's git
  credentials) - is a version too, never a second folder and never a patch
  of ours: `cli new version KEY NAME`, then `versions.NAME.upstream` =
  `{repository, tag?, tested}` (a branch pinned by its commit). Fetch it into
  the scratchpad, diff it against the pinned upstream (`git diff
  <tested>..<fork commit> --stat`, then what changed in the code the driver
  calls) and say what the fork changes; hash each dataset again (its own
  `datasets` when they differ); run `cli check` and `cli prove KEY@NAME`. The
  fork's fixes are the fork's: the published version keeps the upstream's
  behaviour;
- add `benchmark.json` with `standard` in `harness` (copy a standard
  benchmark's entry for the fields `catalog.validate` requires: `category`,
  `origin`, `citation`, the nested dataset). A benchmark's files name no other
  benchmark. A personal entry uses the UI's fallback mark; no package or
  sprite change is needed;
- **bundled contributions only:** add the line-icon mark in
  `app/static/catalogue/marks.svg`, update the list in `app/tests/test_catalog.py`
  and the card count in `app/ui_tests/test_catalogue_routes.py` when a listed
  benchmark is added, and refresh its generated catalogue documentation with
  `python3 app/tests/catalogue_docs.py --write`.

**Agent** (the `import-agent` skill has the full procedure). `cli new agent KEY`, then:
- `install` pinned (`npm` `name@version`, `pip` `name==version`);
- `start.acp` with `{install}` or `{here}`: the agent's own ACP, or a
  maintained adapter (the vendor's first); an agent without either, its own
  non-interactive command as a `start.command` carrying `{prompt}` (task and
  workspace only). Never an agent loop of ours;
- `model.env` / `model.files` with `{base_url}`, `{key}`, `{model}`, `{home}`
  so its calls reach the relay. Prove it: after a trial,
  `agent/calls.jsonl` holds its calls. Empty means it went around the relay -
  declare the relay as a provider of its own in a config file.
- A new release of an agent or a capability already in the catalogue is a
  version, not a new folder: `cli new version KEY VERSION`, check its pins,
  then `cli conform KEY@VERSION` (`docs/standard.md`, "Versions"). An adapter
  whose core floats (a caret range) gets the core pinned in `install` too.
- `options` for what a run may set (default: the vendor's own, its source
  said in `summary`), filled as `{opt_<name>}`; an agent with a CLI of its
  own gets `fidelity.command`, then `cli fidelity KEY --model <cheap model>
  --unsandboxed` (`docs/standard.md`, "Options", "Fidelity").

**Capability** (the `import-capability` skill has the full procedure).
`cli new capability KEY`, then wrap the real component in `capability.py`, calls
into its library and nothing of its logic rewritten: `add` stores, `recall` returns text. Packages pinned in
`install.pip`; one that lives in a repository installs from it
(`upstream` + `install.project`, `extras`, `python`, `indexes`, `prefetch`;
`env` for its own variables), and one that calls a model gets a `model`
block, so it reaches the relay like an agent. Send the memory's own library
output to stderr: stdout carries the JSON lines.

## 4. Prove

1. `cli check` - no problems.
2. A benchmark: `cli run KEY <a proven agent or capability> --model <cheap model> --limit 2 [--unsandboxed]`
   (2 units, 3 at most: a trial shows the pipe works, it does not measure);
   each of its setups, `cli run KEY original --model <a paper's model> --limit 2`
   (and `model`); then `cli prove KEY` (its controls, units, no leak).
   An agent or capability: `cli conform KEY --model <cheap model> [--unsandboxed]`.
   A value that needs a `host` requirement this machine lacks (a GPU) is
   proven on a machine that has it: choose a run host from Settings and
   launch the short trial from the builder's Review step. Execution snapshots
   the current standard runtime and effective catalogue, including user
   additions, and sends that payload to the host. A `git archive` of the
   repository alone would omit an installed user's definitions. User settings
   and credential stores are excluded from snapshots; credentials travel
   through the execution layer, never in entry folders or payloads.
   Run one short trial per such value; an initial driver install can take
   minutes. For a manual host workflow, use a matching Nullius installation
   and the complete entry folders under that host's user catalogue.
3. Read every failure's trial before believing it (`verifier/score.json`,
   `agent/acp.jsonl`, `agent/agent.log`, `agent/calls.jsonl`, the upstream
   script's own log in the workspace). Decide whose it is: the agent's, the
   driver's, the kit's, the definition's, the upstream's or the machine's.
   A dropped download or a network error is the machine's: run it once more
   before deciding. An upstream that stops on a model's reply it cannot
   parse, or runs out of memory at a batch it hard-codes, is the upstream's:
   left as published and said in the limitations (with how much memory, when
   it is the machine's size). Fix only what is ours; a model's answer is never
   edited.
4. Open the job in the app (`nullius serve /absolute/path/to/jobs_standard
   --no-debug`, using the same profile/catalogue) and look at the trial page.
   From a checkout, `uv run --no-project --with-requirements app/requirements.txt
   app/app.py /absolute/path/to/jobs_standard --no-debug` remains supported. The new
   definition shows on the Agents or Capabilities tab, or as a benchmark of the
   builder's *Nullius* runner, its reference participant first on the Agent
   step and paper-model filter on the Model step: check it is offered where it should be. Use a focused browser
   check; `/ui-sweep --only KEY` is available when that skill and its script
   are present.
5. **Personal import:** `cli check`, the authorized proofs/trials above, and
   focused UI inspection. Report unavailable prerequisites without claiming
   a proof. No repository test, icon or documentation edits are required.
   **Bundled contribution:** additionally refresh generated catalogue docs
   for every changed definition (`python3 app/tests/catalogue_docs.py --write`),
   run `python3 app/tests/parallel.py`, the route suite
   (`uv run --no-project --with-requirements app/requirements.txt python app/tests/parallel.py app/ui_tests`),
   and `python3 .claude/skills/docs-drift/scripts/check_docs.py` from the source
   root. Keep the test profile/catalogue isolated from personal entries.

Downloaded standard recipes run with installed `nullius standard`, or with
`--app /path/to/checkout` / `NULLIUS_STANDARD` for a source or runner-payload
tree. Their job paths stay relative to where the script is started. Harbor
recipe bundles need Nullius only for `--serve`, which uses `nullius serve` or
an explicit source `--app`. Do not bake the importing machine's package or
catalogue path into a wrapper: use `{here}`, `{install}` and the driver's
declared inputs. A recipe alone does not transfer personal entry folders;
the builder's execution payload does.

`--unsandboxed` is the user's word, not yours: the local backend runs the
agent as the user. Ask once, with the question tool, before the first agent
run; say what it means. Without a way to ask (a subagent), run an agent only
if the instructions you were given already say yes.

## 5. Report - decision first

```
<kind> <key>: <fits | does not fit yet>   route: <one line>
destination: <personal catalogue folder | bundled contribution folder>
needs: <ports>      fits: <agents or capabilities `cli fit` offers>        (a benchmark)
offers: <ports>     proven: <port passed n/n, ...>                      (an agent or a capability)
trial: <job name>  <n> units  <result>  <tokens, cost>
proven on: <host requirement: machine, value by value - works | fails (whose)>   (a value needing a machine's setup)
ours: <file: what it does, why the upstream could not do it>, ...   (every piece of our own code)
datasets: <each, units>; left out: <what, why>        options: <name: values (default)>; not offered: <what, why>
settings: <n> in upstream-settings.json - <n> options, <n> setups, <n> datasets, <n> fixed, <n> not offered
```

`ours:` lists every file or function we wrote, glue included, each with its
reason; anything there beyond glue (a scorer, a prompt, a tool, a loader)
must have the user's yes on record, else it is a question, not a result.

Then, one table row each: what runs; what cannot or differs from the paper
and why, with **skip** (left as published, said in the limitations) /
**local change** (ours, said in the limitations) / **issue** (upstream's) for each; any generic change proposed (a port, a
schema field) as a ```diff block with its file. Then the questions, with the
question tool. No account of what was read: a `checked:` list at most.

Every change is local first. Pull requests, issues and anything upstream are
asked about once it works, never by default.

## Catalogue presentation

Keep the benchmark description and each option summary to a short sentence.
Put per-value explanations in `about` and prerequisites in `needs` / `host`,
rather than repeating them in the summary. Preserve exact model restrictions,
scoring rules and comparison limits in the setup definition: the page keeps
the full method under **How it runs**, with model and comparison scope visible.
Do not add custom prose-heavy layout or one full-width row per simple value.
Check the shared benchmark page at desktop and mobile widths; simple options,
detailed prerequisites and single/multiple datasets should remain readable.

For continuous upstream settings such as temperature, use an input `pattern`
rather than an arbitrary shortlist of `values`. Say what an omitted parameter
means and retain any setup restrictions; do not imply every model accepts it.

Normal upstream scoring rules and expected use of a chosen model belong in
`benchmark.json`'s `good_to_know` list, rendered as **Good to know**. Reserve
the limitations for unsupported behavior, methodological differences and material
constraints, in two lists: `upstream_limitations` (**Upstream limitations**:
what the benchmark as published gets wrong or leaves out, a fix for its
maintainers) and `limitations` (**Nullius limitations**: where running it here
differs, or what Nullius does not offer or should fix). Never describe an
upstream rule as a Nullius defect. Show one **Original** procedure. Model selection belongs on the Model step:
**Like the paper** filters documented models; **Any of your choice** uses the
provider catalogue. Do not invent paper models. Declare `reference_agent` on
`setups.original` when upstream runs a named agent; otherwise the builder shows
**No agent · Original**, or **Original memory** for a memory benchmark. The
`original` / `model` driver methods preserve upstream model aliases; they are
not separate user-facing setups. Recipes select `others: ["original"]`, one
`model`, and `model_choice: "paper" | "any"`.
