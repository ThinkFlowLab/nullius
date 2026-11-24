---
description: Shared instructions for AI coding assistants in Nullius.
---

# AGENTS.md

Shared instructions for AI coding assistants working in **Nullius**.
Keep this file specific, factual, and cross-tool. Prefer nearby code over
assumptions. Describe current behavior and constraints; remove obsolete code
and historical commentary unless the user explicitly asks to keep it.

`README.md` carries the design and rationale. This file is the map.

## What This Repo Is

**Nullius is an application: bring anything, run it, prove it.** A
benchmark, an agent or any capability comes into its catalogues by one prompt
(the import skills), is paired by the ports it has proven (the Nullius
standard, `docs/standard.md`), runs here or on many machines, and every score
opens into the evidence behind it - the agent's failure or the benchmark's.
It reads Harbor's output too, from any agent, and ATIF and OpenTelemetry
exports. It runs a benchmark only on an explicit Run from the builder's Review step - a
standard recipe (a benchmark's own scripts included, as its Original setup)
or a Harbor recipe - on this machine or a host (`app/nullius/execution.py`). A Harbor recipe pins its Harbor - a
release from PyPI, or a repository (a private fork included) at a commit,
built on this machine and its wheels sent to a host - and runs it through
`uvx`, outside this application's interpreter (`app/nullius/harbor/source.py`);
a recipe without a pin runs whatever `harbor` is on the target's PATH.
The job records which one ran.

Application code lives under `app/`; topic documentation lives under `docs/`.
**New code goes under `app/`**, never loose at the root. Root `pyproject.toml`
builds the `nullius-eval` distribution: `nullius` is the backend, `_nullius_app` is the UI
and its resources.

- `app/app.py` — the entry point: the application object from `app/web/core.py`,
  the capability modules imported so each registers its routes, `main()`.
- `app/web/` — the Flask side, one module per capability: `core` (the application object), `shared` (the open
  workspace and its jobs, path resolution, the catalogue cache, the query
  helpers, the form-token guard), `filters` (template filters, page context),
  then `runs`, `traces`, `compare`, `builder`, `catalogue`, `agents`,
  `settings`, `workspaces`, `execute`, `judge`, `feasibility`, `reconcile`, `judge_tasks`, `onboarding`, `failure` (`agents` is the Agents
  and Capabilities tabs and conformance Kits catalogue). Cross-module names are reached as
  attributes (`shared.STATE`, `traces._trial_evidence`): patch one, patched for all.
- `app/nullius/` — its backend, one package per purpose: `runs/` (reading one
  Harbor job tree: `parse`, `classify`, `live`, `artifacts`, `evidence`,
  `feasibility`, `failure_analysis` - a job's failure labels and task flags, beside its result.json, written by `flags_cli` too -, `launches`, `verifier` - a verifier's report read by its
  benchmark's report block - `attempts`, a task's attempts in one job with
  their pass count and pass@k, and the `cli`), `traces/` (`trace`, the reader, with its span tree `spans`,
  `fold`, time accounting `breakdown` and layout `waterfall`; `trajectory`,
  `conversation`, `calls` (a trial's conversation by model call, from its relay's records), `charts`, `behavior`, `otel`), `compare/` (`comparison` and its `cli`),
  `catalogue/` (`catalog`, which loads one folder per benchmark
  under `catalogue/benchmarks/`, named by its key: its `benchmark.json` with its
  datasets nested in it, its `report` block and its `standard.json` when it runs on the standard;
  `harbor_entry`,
  a Harbor benchmark added from the Benchmarks page's form; `citation`, an entry's
  BibTeX read into the paper its page shows; `storage`, whole-folder user overlays and restoring bundled entries; `sources`, where each dataset's data comes
  from for each way it runs), `recipes/`
  (`launch`, the builder's vocabulary; `store`, its saved recipes), `providers` (the model provider registry: protocols, presets and each provider's record, read by every consumer; and the builder's steps),
  `harbor/` (`source`: the Harbor a recipe pins - built, kept, scanned for its agents;
  `feed`: the live feed any Harbor writes, from its own trial hooks),
  `importers/` (`atif` ATIF, `otel` OpenTelemetry, and the `cli`), `judge/` (`questions`, `state`, `client`, `store`, `batch`, `reconcile`, `lanes`),
  `standard/` (the Nullius standard, `docs/standard.md`, one folder per purpose, importing one way: `test_layering.StandardFolders`;
  `contract/`: `ports`, `names` (capability vocabulary and legacy aliases), `definitions` (with an agent's or capability's versions), `proofs` (generated evidence in sibling `proofs.json`), `match`;
  `requirements/`: `install`, `hostreq` (what a benchmark's options need of the machine its driver runs on: the check and the install), `network_requirements` (an environment's declared public web access: the connectivity check before any unit);
  `protocols/`: `jsonl`, `acp`, `cancellation` (interruptible protocol waits), `computer_port` and `computer_pipe` (the computer port's MCP server, served by a desktop benchmark's runner over the benchmark's own loop, and the session's stdio pipe to it);
  `calls/`: `relay`, `exchanges` (each model exchange of a unit's agent kept whole by its relay, and a request rebuilt from it), `ledger` (a run's spending cap);
  `backends/`: `container` (the container backend), `boat` (the boat backend: each unit's agent in a boat.dev VM), `setup_container` (an Original setup's driver and its Linux dependencies in Docker), `forward` (a licence server's one door from it, or a benchmark's MCP server's), `web_services` (HTTP fixtures a driver's setup declares, each behind a unit-local Docker door), `mcp_machine` and `mcp_bridge` (a benchmark's MCP server run on the machine, in its driver's environment, and the agent's bridge to it), `egress` (an own image's check's proxy to public addresses);
  `runner/`: `run`, `job`, `retries` (what an attempt is tried again after, and how many times), `pace` (how soon the next unit starts after a rate limit), `shard` and `merge` (one run over many machines), `payload` (a snapshot of the running backend and effective catalogue for a remote host), `boat_units` (a benchmark whose units boot a VM of their own, each unit whole in a Boat VM);
  `proofs/`: `conform`, `fidelity` (ACP against the agent's own CLI), `prove` (a benchmark's controls and proofs), `pins` (image digests checked against registry tags);
  `pages/`: `recipe` (the builder's standard recipe), `howto` (the How to add steps);
  and the `cli` at its top;
  conformance kits under `standard/kits/`, each a benchmark: a `driver` and, for the
  tools kit, an MCP server `mcp_kit`) - the definitions it reads are in the catalogue:
  a benchmark's `standard.json` and `driver` in its folder (τ³-bench's also the MCP
  server of its domains' tools, `mcp_server`: the upstream's own, run on the machine; OSWorld1's also its null control's
  agent, `nop`, and the `runner` of a unit an agent runs: the upstream's loop with the computer port's bridge; OSWorld2's also `nop`, `api_logs` (each image of its API logs kept once), `websites` (its mock
  websites: the checks before a VM start, Overleaf's lock and reset, the stack's command line), `gitlab_server` (its
  GitLab: made once on the run's machine with its token, its tasks' lock, the stack's command line), `proxy` (a
  tinyproxy of a unit's own for the tasks that ask for a proxy) and, in `startup/`,
  `sitecustomize`: its VMs' disk write limit in every process its driver starts), `catalogue/agents/<key>/`
  (the reference `agent`; DigitalMate's `run`, `gateway` and `acp` (product startup, task submission, and its fixed version's ACP sessions);
  JiuwenSwarm's `agent`, mini-swe-agent's `run` and SWE-agent's `run`, the wrappers that start them) and `catalogue/capabilities/<key>/` (the keyword-memory `capability`); `workspace` (and
  `workspace_cli`, its shell command),
  `cli` and `__main__` (the unified `nullius` and `python -m nullius` commands),
  `paths` (user config, data and cache locations), `ondisk` (a file written whole then renamed into place, a lock, a digest), `prices` (the user's model price list: fetched on request and before a capped run; what a cost is estimated from), `migrate` (explicit copying of legacy checkout storage), `settings`, `hosts`, `constraints` (what Nullius may use on each machine: one table, the container and systemd scope probes, the warnings), `execution`, `progress` (what a run is doing, one line per stage), `prerequisites` (what a recipe needs of each machine it runs on, asked before Run and again as it starts), `onboarding` (the Getting started checklist: what this machine
  says is done, and where each step is done), `gitrefs` (a repository's branches and tags,
  by `git ls-remote`), `secrets` (a benchmark's licences, kept per benchmark outside the repository, and the Hugging Face token of Data providers), `sandbox` (the Docker Hub login Data providers uses), `charts` (categorical charts, bars, dots and a scatter, and their style registry: Compare's Chart view) and `redact` at the top.
- `app/templates/`, `app/static/` — the interface, grouped by the same capability
  names (`runs/`, `traces/`, `builder/`, …; `base.html`, `style.css` and the fonts at the top), including the command
  builder's JavaScript, which owns the generated recipes. `app/templates/_tables.html` and
  `app/static/tables.js` are the table toolkit: one column list builds a table's headers, cells and controls.
  `app/templates/_cards.html` lays the same field list out as cards, one slot per field.
  `app/templates/_icons.svg` is the icon sprite, checked by `app/tests/test_icons.py`.
- `app/tests/`, `app/ui_tests/` — the backend suite and the route suite.
  Beside the backend tests sit scripts run by hand:
  `app/tests/field_contract.py`, which refreshes the `field_contract.json` the
  suite checks, and `app/tests/catalogue_docs.py`, which writes the Supported
  sections under `docs/` from explicit bundled catalogue roots, never user overlays: each section's README block and
  a page per benchmark, agent and capability (`test_catalogue_docs.py` fails
  when they are stale). The fixtures under `app/tests/fixtures/` are small and
  hand-made.
- `app/site/` — the static website: MkDocs configuration and
  source staging. It publishes `docs/`.
  `diagrams.py` draws the figures under `docs/diagrams/`
  (`--write` after a change; `test_site.py` fails when they are stale).
  `readme_media.py` draws the README’s animated SVGs under
  `docs/assets/nullius/readme/` (`--write` to refresh, `--check` for drift).
  Build dependencies are isolated in `app/site/requirements.txt`;
  build, preview and publishing instructions are in `app/site/README.md`.
- `docs/` — the introduction, quickstart, concepts and results guides; `settings/`,
  `benchmarks/`, `agents/` and `capabilities/` hold focused guides; topic pages and the example of a new benchmark. Keep validations, captures and rendered reports out of `docs/`.

Benchmark integrations expose one current entry with supported behavior as options.
Internal driver changes do not create selectable versions. Reserve benchmark versions
for explicit upstream releases or requested forks; retain only applicable proofs.
Authored definitions contain configuration; generated evidence belongs in the entry's
`proofs.json`. Proof runs leave definition files untouched. Read through
`definitions` to combine the two; do not infer proven ports from raw definition JSON.

### Installation and user storage

**Flask is the application's only direct runtime dependency.** Nullius itself
is installed with `uv tool install nullius-eval` from PyPI, selecting the latest
version unless one is explicitly pinned. The executable is `nullius`.

`nullius paths` lists persistent locations. On Linux, settings, the model price
list and benchmark licences live under `~/.config/nullius/`, user catalogue entries, workspaces,
runs, saved builder recipes and standard installs under `~/.local/share/nullius/`, and caches under
`~/.cache/nullius/`. XDG variables move those roots; `NULLIUS_HOME` selects a
separate profile with `config/`, `data/` and `cache/`. Settings saves owner-only JSON; process environment values win.
No personal data belongs inside the installed package. See `docs/getting-started.md` for installation
and migration, and `docs/settings/README.md` for paths and precedence.

## Instruction Priority

- Follow system, tool, and user instructions first, then this file, then
  the rules under `.claude/rules/` (`running.md`: what runs and installs, how
  to read a result; `traces.md`: what the pages do with a trace; `ui.md`:
  the owner's rules for every page and the scope of UI checks).
- Prefer small, targeted diffs. Do not refactor unrelated areas
  opportunistically.

## Core Architecture

Three dependency rules. Each is load-bearing and each has a test.

- **`app/nullius/` imports no Flask.** `test_layering.NoFlask` checks every module. If a test in `app/tests` needs Flask, the layering
  has broken, not the test.
- **One name, one implementation per interpreter.**
  `app/tests/test_dependency_surface.py` counts *providers* of `nullius` and
  fails when there is more than one. A local package can shadow an installed distribution, so successful imports alone do not prove it.
- **The templates' field contract is pinned.** Templates read Python data objects (such as Job, Trial and Verdict)
  by attribute name, and no import catches a renamed field —
  it surfaces as a blank cell or an `UndefinedError` at request time.
  `app/tests/field_contract.json` pins them and `test_field_contract.py`
  fails on a removal. After a deliberate change to a rendered class, refresh
  the pin with `python3 app/tests/field_contract.py --write`; it refuses when a
  module cannot be imported, so a refresh cannot bless a loss.

Bundled resources resolve from `__file__`; personal storage resolves through
`nullius.paths`. The source `app/` directory moves as a unit; moving it also
requires updating the package mapping in `pyproject.toml` and the test-discovery
paths in `.github/workflows/tests.yml`.

## Commands

The backend suite imports only the standard library; its offline Harbor build
fixtures also need git and uv/uvx on PATH. Only the route suite needs Flask,
the one requirement in `app/requirements.txt`. Use `uv run --no-project` for
source commands so the root packaging metadata does not install a second copy
of Nullius into the source test interpreter. Run everything from the
repository root. `app/tests/parallel.py` runs a suite one module per process,
several at a time; `python3 -m unittest discover -s app/tests -t app` runs the
same tests in one process.
The parallel runner selects a temporary profile before importing test modules.
When invoking unittest directly, set `NULLIUS_HOME` to a temporary directory
and unset personal catalogue/storage overrides before discovery.

```bash
python3 app/tests/parallel.py

# with the docker tier: the tests that build and run containers for real
# (app/tests/docker_tier.py), skipped otherwise
python3 app/tests/parallel.py --docker

# route suite
uv run --no-project --with-requirements app/requirements.txt \
  python app/tests/parallel.py app/ui_tests

# docs drift is the /docs-drift skill (.claude/skills/docs-drift/SKILL.md),
# run by hand: its script checks links and paths, then the prose is read

# the installed app (or use the source command below)
nullius serve

# from a source checkout
uv run --no-project --with-requirements app/requirements.txt app/app.py jobs

# refresh the templates' field contract
python3 app/tests/field_contract.py --write

# refresh the Supported sections after a catalogue definition changes
python3 app/tests/catalogue_docs.py --write

# the importer registry: ATIF and OpenTelemetry
PYTHONPATH=app python3 -m nullius.importers.cli --list
```

Debug mode is off by default. Use `--debug` only for development; `--no-debug`
overrides an explicit `DEBUG=1` environment setting.

**CI** (`.github/workflows/`):

- `tests`: backend and route suites, actionlint, and a wheel install check
  (`app/packaging/verify.py`). Non-draft pull requests, and by hand.
- `judge-pr`: the PR judge, below. Non-draft pull requests, and `/judge`.
- `docker-tests`: the backend suite with the docker tier. Nightly, and by hand.
- `draft release` (`release.yml`): builds and verifies a release's wheel and
  source archive into a draft GitHub release. By hand; never publishes.
- `publish to PyPI` (`publish.yml`): uploads a reviewed, published release's
  verified assets by Trusted Publishing. By hand, with confirmation.
- `documentation site`: checks the site build, by hand
  (`bash app/site/host-build.sh` locally). The host publishes from
  `docs-release`, advanced to the release tag after the PyPI upload.

See `app/packaging/README.md`, `docs/releases/` and `app/site/README.md`.

**UI checks:** `.claude/rules/ui.md`. The `UI sweep` workflow runs nightly and by hand. A benchmark key narrows it. It fails on page JavaScript errors or clipped text. Screenshots are the local sweep's
and need visual review.

**The PR judge** (`.github/workflows/judge-pr.yml`, running
`.github/scripts/judge_pr.py`) comments on every pull request: two ratings, then a
table of questions. **Merge risk** (minor, medium, significant, major,
severe) is the TypeSafe judge's reading of what happens if the change merges
and is wrong, floored by the script (a deleted test, a key-shaped string or a
workflow change is severe at least; the standard, execution, hosts, secrets,
the parser or the classifier is major at least). **Priority** (low, medium,
high, extreme) is how deep the diff reaches, computed from its paths alone.
The questions, asked k=3 times with medians: each acceptance criterion of the
linked issue (the `## Acceptance` checkbox list, one row per box; a body
without one is judged whole), whether the issue and the PR describe the same
change, whether the description matches the diff, whether the diff stays in
scope, whether tests or the `## Verified` section cover it, whether the docs
follow, whether `## Risk` names what could break; `## Before and after` is
counted by the script when the diff touches what is visible. Issue rows show `–` when the PR links no issue. Everything the
judge reads has been through `redact` first: key-shaped strings and email
addresses never leave the runner. `/judge` from an owner, member or collaborator asks again, a draft included. Experimental:
never a required check; without the secret `TYPESAFE_API_KEY` (or `API_KEY_TYPESAFE`) it reports "not configured" and stays
green. Offline: `python3 .github/scripts/judge_pr.py --pr-file pr.md
--diff-file pr.diff [--issue-file issue.md] --dry` builds the states and asks
nothing.

**Run analysis** is the `/run-analysis` skill
(`.claude/skills/run-analysis/SKILL.md`): triage of a run that failed or looks
wrong, an analysis of one job, or a comparison of several, written up as one
self-contained page. Its script, `.claude/skills/run-analysis/scripts/runs.py`,
prints health verdicts, computes the metrics and
renders the page from the template beside it; the findings and the prose are
the assistant's, by the procedure in the SKILL.md.

## Conventions

Use `nullius.thinkflowlab@proton.me` for Nullius-owned contact email addresses,
including security reports.

### Documentation

`docs/` is terse: fragments and tables, the fewest words that change what a
reader chooses, does or concludes.

- Keep guarantees, requirements, refusals and their fix. Drop the mechanism
  (flags, files, protocols, how a key travels), interface narration, history
  and asides.
- A table cell is a line, not a paragraph. One fact lives on one page; others link.
- A prerequisite Review already checks is not repeated.
- `docs/standard.md` headings are never renamed: the import skills cite them.

### Test design

- Test observable behavior, contracts and meaningful failure modes—not incidental
  wording, internal structure or current catalogue contents.
- Before adding a test, inspect nearby coverage. Extend an existing case or use
  subtests when the fixture and behavior are shared.
- Add a separate test when it protects a distinct regression or needs a different
  setup. A past bug alone does not require a separate test.
- Assert exact text only when that text is a contract.
- Before removing a test as redundant, identify the surviving assertions that
  protect its behavior. Shared line coverage alone is insufficient.
- Consolidation must preserve edge cases and fixture isolation. Fewer test methods
  is not itself the goal.

### Attribution

**No machine attribution in commit messages or pull request descriptions** — no
`Co-Authored-By: Claude …` or any assistant variant, no `Claude-Session:` or
other session metadata, no generated-by signature. Keep the message about the
change and why it was made.

**Legitimate human co-authors are preserved.** A `Co-Authored-By` naming a real
person who contributed is correct and expected.

Two things enforce it:
`.claude/settings.json` sets `attribution` to empty for `commit` and `pr` with
`sessionUrl` off, and a `commit-msg` hook in the shared Git directory rejects
machine signatures while allowing human co-authors. **Do not bypass the hook**
(no `--no-verify`).

Existing commits are **not** rewritten. Do not amend, rebase or force-push to
strip trailers from history.

The metric field guide (`docs/field-guide.md`) is a living document: a change that alters what it
defines updates it in the same change. `app/tests/test_field_guide.py` fails
when a field it names leaves the backend, when its stated counts stop
matching its tables, or when its outcome vocabulary stops being the
classifier's; what each metric means is still read by a person.

The builder saves and imports recipes as JSON. Review runs them through the
execution backend.
`app/ui_tests/test_recipe_execution_fixtures.py` pins the compiled execution plans.

### Issues

Before opening anything, **search existing issues *and* pull requests** —
across the repositories involved, not just this one.

An issue carries enough to act on without a conversation. The forms under
`.github/ISSUE_TEMPLATE/` (Bug, Enhancement) hold the fields: **Priority**
(how deep it reaches: low the surface, medium the web layer or the
catalogue, high the backend, extreme what runs and proves), **Components**,
**Evidence** (what was observed, quoted rather than characterised), a
**Screenshot** when it is visible, **Reproduction** (the smallest steps or
input that show it), **Expected**, **Acceptance** (one checkbox per
criterion; the judge gives each its own row) and **Dependencies and
related**. A pull request's title is a conventional commit subject, since a
squash merge makes it the commit on `main`: `type(scope): summary`, the
summary lowercase (names keep their case) without a final period. The type is
one of `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `ci`, `build`,
`chore` or `revert`; the scope is the part it touches: `standard`,
`catalogue`, `runs`, `traces`, `compare`, `judge`, `builder`, `web`,
`execution`, `settings`, `docs`, `site`, `ci`, `release`. For example
`fix(standard): give drivers only the nullius package on PYTHONPATH`.
A pull request's body follows `.github/pull_request_template.md`:
`Closes #n`, What, Touches, Before and after (two images when the change is
visible), Verified, Risk. An assistant files either with the `/file-issue`
skill (`.claude/skills/file-issue/SKILL.md`), which produces the same shape
with `gh`. No key, address, personal message or private link in any of it.

Where it goes:

- **File a bug in the repository that owns the cause**, not the one where the
  symptom appeared: a cause in Harbor is Harbor's issue, with a link from here.
- **Minor or unconfirmed observations go to the parent**, rather than becoming
  a speculative issue in a component repository.

How it is kept:

- **Set the native Type and Priority when the issue is created.** Where the priority is genuinely undecided,
  choose the lowest that is defensible and say in the body that it is a
  placeholder. Reuse the labels that exist (`bug`, `enhancement`).
- **Assign `aitianshi`** where that is the real ownership; an inappropriate
  assignee is worse than none, and a session identifier is not an account.
- **Every implementation pull request references its issue**, and the issue's
  Development panel shows it. Body text is the means, not the proof: read it
  back with `closedByPullRequestsReferences` in the GraphQL API before calling
  the work done. Stacked pull requests link the same way; a base is never
  retargeted for the panel's sake.
- **Update the metadata at every change** of scope, ownership, priority,
  dependencies, implementation PR, validation evidence or completion.

**Recording something is not a licence to fix it.** Writing an issue for a
defect found in passing is the correct response to noticing it; widening the
current change to address it is not. Repositories stay private, and nothing
merges without the user's word.

### Native capabilities and composition

When evaluating an agent's capabilities, inspect the pinned upstream's callable
interfaces; do not infer memory or retrieval from conversation support. Keep
native, augmented and replacement setups distinct. A component's port proof
does not prove an agent uses it, and adding a tool is not necessarily replacing
a native capability. The current runner has no generic agent-plus-capability
recipe. Follow `docs/standard.md`, "Native capabilities and composition", and
the import skills for evidence requirements and reporting integration gaps.

## More Detail

- The product, what each page does and why — `README.md`, then the topic
  pages `docs/builder.md`, `docs/judgement.md`, `docs/settings/README.md`, `docs/cli/README.md`
- What each metric means and how portable it is — `docs/field-guide.md`
- Open work — the GitHub issues
- The benchmark catalogue: a folder per benchmark, what an entry holds, the
  listing rule and its checks — the docstring of `app/nullius/catalogue/catalog.py`
- The builder's vocabulary and the agents it offers —
  `app/nullius/recipes/launch.py`; the recipe core `app/static/builder/command-builder-core.js`
- The Harbor a recipe pins: the pin, the build here, the cache, the scan, the
  `harbor` script a run calls — the docstring of `app/nullius/harbor/source.py`;
  `docs/builder.md`, "Which Harbor"
- Bringing a new benchmark in, either way, starts at the `/import-benchmark` skill
  (`.claude/skills/import-benchmark/SKILL.md`), which asks the route - Harbor, or
  the Nullius standard through `/import-standard`, where a paper's own scripts run
  as its Original setup; by hand, `docs/examples/new-benchmark/` - a Harbor entry
  to copy, and the steps; a Harbor benchmark also from the Benchmarks page,
  *+ Add a benchmark* - a prompt for any assistant - or by hand *Add a Harbor
  benchmark* (`app/nullius/catalogue/harbor_entry.py`)
- The Nullius standard - benchmarks, agents and capabilities matched by proven
  ports, run over ACP, MCP and the relay; importing each by hand and with the
  `/import-standard` skill for a benchmark, `/import-agent` for an agent and
  `/import-capability` for a capability (`.claude/skills/<name>/SKILL.md`; the
  Agents and Capabilities tabs' *Add* button writes the prompt for any assistant) -
  `docs/standard.md`; the code under `app/nullius/standard/`. Nullius is a
  facilitator, not a re-implementer: a benchmark, agent or capability runs its
  upstream's own code at a pin, its dependencies in its own environment, and
  any code of ours beyond glue is asked about first
- Settings, its variables and their validation — `app/nullius/settings.py`;
  hosts, keys and the connection test — `app/nullius/hosts.py`
- Removing a benchmark, agent or capability throughout the app, including
  generated artifacts and requested runtime data — `/remove-integration`
  (`.claude/skills/remove-integration/SKILL.md`)
- Running a recipe: the plan, the parts, a host, the mirror, the record a
  restart reads — the docstrings of `app/nullius/execution.py`; the routes in
  `app/web/execute.py`
- Judgement: questions, state, client, store, batch, reconcile, lanes —
  `app/nullius/judge/`
- The live board, and the four rules it must keep — the docstring of
  `app/nullius/runs/live.py`
- Machine-specific notes (confinement, disk) — `.claude/environment.local.md`,
  gitignored and per checkout
