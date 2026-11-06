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
**New code goes under `app/`**, never loose at the root. `README.md`
and `AGENTS.md` sit at the top because that is where people look for them;
there is no `CLAUDE.md`, assistants read this file. Root `pyproject.toml`
builds the `nullius-eval` distribution: `nullius` is the backend, `_nullius_app` is the UI
and its resources. The source layout stays under `app/`.

- `app/app.py` — the entry point: the application object from `app/web/core.py`,
  the capability modules imported so each registers its routes, `main()`.
- `app/web/` — the Flask side, one module per capability: `core` (the application object), `shared` (the open
  workspace and its jobs, path resolution, the catalogue cache, the query
  helpers, the form-token guard), `filters` (template filters, page context),
  then `runs`, `traces`, `compare`, `builder`, `catalogue`, `agents`,
  `settings`, `workspaces`, `execute`, `judge`, `feasibility`, `reconcile`, `judge_tasks`, `onboarding` (`agents` is the Agents
  and Capabilities tabs and conformance Kits catalogue). Cross-module names are reached as
  attributes (`shared.STATE`, `traces._trial_evidence`): patch one, patched for all.
- `app/nullius/` — its backend, one package per purpose: `runs/` (reading one
  Harbor job tree: `parse`, `classify`, `live`, `artifacts`,
  `feasibility`, `launches`, `verifier` - a verifier's report read by its
  benchmark's report block - and the `cli`), `traces/` (`trace`, `trajectory`,
  `conversation`, `charts`, `behavior`, `otel`), `compare/` (`comparison` and its `cli`),
  `catalogue/` (`catalog`, which loads one folder per benchmark
  under `catalogue/benchmarks/`, named by its key: its `benchmark.json` with its
  datasets nested in it, its `report` block and its `standard.json` when it runs on the standard;
  `verifier` in `runs/` reads a verifier's report by that block; `harbor_entry`,
  a Harbor benchmark added from the Benchmarks page's form; `citation`, an entry's
  BibTeX read into the paper its page shows; `storage`, whole-folder user overlays and restoring bundled entries; `sources`, where each dataset's data comes
  from for each way it runs), `recipes/`
  (`launch`, the builder's vocabulary), `providers` (where a run's model calls go, and the builder's steps),
  `harbor/` (`source`: the Harbor a recipe pins - built, kept, scanned for its agents;
  `feed`: the live feed any Harbor writes, from its own trial hooks),
  `importers/` (`atif` ATIF, `otel` OpenTelemetry, and the `cli`), `judge/` (`questions`, `state`, `client`, `store`, `batch`, `reconcile`, `lanes`),
  `standard/` (the Nullius standard, `docs/standard.md`: `ports`, `names` (capability vocabulary and legacy aliases), `definitions` (with an agent's or capability's versions), `match`,
  `jsonl`, `acp`, `cancellation` (interruptible protocol waits), `relay`, `install`, `run`, `container` (the docker backend), `forward` (a licence server's one door from it, or a benchmark's MCP server's), `mcp_machine` and `mcp_bridge` (a benchmark's MCP server run on the machine, in its driver's environment, and the agent's bridge to it), `hostreq` (what a benchmark's options need of the machine its driver runs on: the check and the install) and `egress` (an own image's check's proxy to public addresses), `job`, `conform`, `fidelity` (ACP against the agent's own CLI), `prove` (a benchmark's controls and proofs), `shard` and `merge` (one run over many machines), `recipe` (the builder's standard recipe), `howto` (the How to add steps) and the `cli`; its
  `payload` snapshots the running backend and effective catalogue for a remote host;
  conformance kits under `standard/kits/`, each a benchmark: a `driver` and, for the
  tools kit, an MCP server `mcp_kit`) - the definitions it reads are in the catalogue:
  a benchmark's `standard.json` and `driver` in its folder (τ³-bench's also the MCP
  server of its domains' tools, `mcp_server`: the upstream's own, run on the machine), `catalogue/agents/<key>/`
  (the reference `agent`; JiuwenSwarm's `agent` and mini-swe-agent's `run`, the wrappers that start them) and `catalogue/capabilities/<key>/` (the keyword-memory `capability`); `workspace` (and
  `workspace_cli`, its shell command),
  `cli` and `__main__` (the unified `nullius` and `python -m nullius` commands),
  `paths` (user config, data and cache locations), `migrate` (explicit copying of legacy checkout storage), `settings`, `hosts`, `execution`, `progress` (what a run is doing, one line per stage), `prerequisites` (what a recipe needs of each machine it runs on, asked before Run and again as it starts), `onboarding` (the Getting started checklist: what this machine
  says is done, and where each step is done), `gitrefs` (a repository's branches and tags,
  by `git ls-remote`), `secrets` (a benchmark's licences, kept per benchmark outside the repository), `sandbox` (Settings' docker: its Docker Hub login and the images runs keep) and `redact` at the top.
- `app/templates/`, `app/static/` — the interface, grouped by the same capability
  names (`runs/`, `traces/`, `builder/`, …; `base.html`, `style.css` and the fonts at the top), including the command
  builder's JavaScript, which owns the generated recipes.
- `app/tests/`, `app/ui_tests/` — the backend suite and the route suite.
  Beside the backend tests sit scripts run by hand:
  `app/tests/field_contract.py`, which refreshes the `field_contract.json` the
  suite checks, and `app/tests/catalogue_docs.py`, which writes the Supported
  sections under `docs/` from explicit bundled catalogue roots, never user overlays: each section's README block and
  a page per benchmark, agent and capability (`test_catalogue_docs.py` fails
  when they are stale). The fixtures under `app/tests/fixtures/` are small and
  hand-made; no recorded run is kept.
- `app/site/` — the static website: MkDocs configuration and
  source staging. It publishes `docs/`, including the metric field guide.
  `diagrams.py` draws the figures under `docs/diagrams/`
  (`--write` after a change; `test_site.py` fails when they are stale).
  `readme_media.py` draws the README’s animated SVGs under
  `docs/assets/nullius/readme/` (`--write` to refresh, `--check` for drift).
  Build dependencies are isolated in `app/site/requirements.txt`;
  build, preview and publishing instructions are in `app/site/README.md`.
- `docs/` — the introduction, quickstart, concepts and results guides; `settings/`,
  `benchmarks/`, `agents/` and `capabilities/` hold focused guides; plus topic pages (`builder.md`, `judgement.md`, `settings/README.md`,
  `cli/`, one page per topic of the CLI) and the example of a new benchmark. Validations, captures and rendered reports are not kept
  here; tests are the proof.

Benchmark integrations expose one current entry with supported behavior as options.
Internal driver changes do not create selectable versions. Reserve benchmark versions
for explicit upstream releases or requested forks; retain only applicable proofs.

### Installation and user storage

**Flask is the application's only direct runtime dependency.** Nullius itself
is installed with `uv tool install nullius-eval` from PyPI, selecting the latest
version unless one is explicitly pinned. Downloaded wheels and source builds
remain available; the executable is `nullius` and storage paths keep that name. The
Harbor a recipe runs is its pin's, run through `uvx` (or the target's own, for
a recipe without a pin), and the viewer never imports it.

`nullius paths` lists persistent locations. On Linux, settings and benchmark
licences live under `~/.config/nullius/`, user catalogue entries, workspaces,
runs and standard installs under `~/.local/share/nullius/`, and caches under
`~/.cache/nullius/`. XDG variables move those roots; `NULLIUS_HOME` selects a
separate profile with `config/`, `data/` and `cache/`. Existing storage overrides
still apply. Settings saves owner-only JSON; process environment values win.
No personal data belongs inside the installed package. Tests isolate these
locations with temporary profiles. See `docs/getting-started.md` for installation
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

- **`app/nullius/` imports no Flask.** The backend is exercisable from a bare
  shell or a notebook. `test_charts.NoFlask` checks every module in the
  package, not one file. If a test in `app/tests` needs Flask, the layering
  has broken, not the test.
- **One name, one implementation per interpreter.**
  `app/tests/test_dependency_surface.py` counts *providers* of `nullius` and
  fails when there is more than one. A local package can shadow an installed
  distribution, so successful imports alone do not prove this constraint.
  Source tests use the checkout; wheel checks use an isolated interpreter
  outside it. The wheel carries the backend once, separate from `_nullius_app`.
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
# no Python dependencies; git and uv/uvx are test tools
python3 app/tests/parallel.py

# with the docker tier: the tests that build and run containers for real
# (app/tests/docker_tier.py), skipped otherwise; CI runs them nightly
python3 app/tests/parallel.py --docker

# only the route suite needs Flask
uv run --no-project --with-requirements app/requirements.txt \
  python app/tests/parallel.py app/ui_tests

# docs drift is the /docs-drift skill (.claude/skills/docs-drift/SKILL.md),
# run by hand: its script checks links and paths, then the prose is read

# the installed app (or use the source command below)
nullius serve

# from a source checkout
uv run --no-project --with-requirements app/requirements.txt app/app.py jobs

# the templates' field contract: refresh the pin after a deliberate change to a
# rendered class (the backend suite checks it; the refresh refuses while any
# module cannot be imported)
python3 app/tests/field_contract.py --write

# the Supported sections, a page per benchmark, agent and capability: refresh them after
# a change to a definition under app/nullius/catalogue (the backend suite checks them)
python3 app/tests/catalogue_docs.py --write

# the importer registry: ATIF and OpenTelemetry
PYTHONPATH=app python3 -m nullius.importers.cli --list
```

Debug mode is off by default. Use `--debug` only for development; `--no-debug`
overrides an explicit `DEBUG=1` environment setting.

**CI** runs the experimental PR judge, backend and route suites, and a temporary
wheel install/reinstall check on pull requests. `app/packaging/verify.py` checks
runtime assets, one backend provider, installed commands and pages, and user
storage preservation. The `tests` workflow also runs on pushes to `main` and by hand.
The manual `draft release` workflow (`.github/workflows/release.yml`) runs both
suites and uses `app/packaging/release.py` to build and verify a release
candidate's wheel and source archive, then attaches them and `SHA256SUMS` to a
draft prerelease. It does not publish or upload to PyPI; see
`app/packaging/README.md` and the versioned notes under `docs/releases/`.
The separate manual `publish to PyPI` workflow (`.github/workflows/publish.yml`)
requires confirmation and a reviewed, published GitHub prerelease. It verifies
the downloaded assets before a separate `pypi` environment job uploads the
same bytes through Trusted Publishing. PyPI and environment configuration
must be completed before that workflow can publish.
The `docker tests` workflow (`.github/workflows/docker-tests.yml`) builds the
standard's base image and runs the backend suite with the docker tier, nightly
and by hand, never on a pull request. The `documentation site` workflow
is run by hand and only checks: the host builds and publishes the site from
`docs-release`, advanced to the release tag only after the PyPI upload succeeds.
Preview deployments are disabled; `bash app/site/host-build.sh` is the same check locally. See
`app/site/README.md`.

**UI checks:** routine interface changes require relevant unit and route tests
plus focused browser checks of the affected pages and interactions, including
the themes and viewport sizes the change affects. Full screenshot coverage is
nightly or manually requested, not required after every UI change. The
`UI sweep` workflow (`.github/workflows/ui-sweep.yml`) is configured for 02:17 UTC
nightly (`17 2 * * *`) and `workflow_dispatch`, with no push or pull-request
trigger. Once it is on the default branch, use **Actions → UI sweep → Run
workflow**, optionally supplying a benchmark key; leave it empty for full
coverage. The isolated Flask/Playwright runner prints the report to the log,
keeps no artifact (artifacts count against the account's storage), and fails
on page JavaScript errors or clipped text. Screenshots are the local sweep's
and need visual review; there is no pixel-baseline comparison.
The `/ui-sweep` skill (`.claude/skills/ui-sweep/SKILL.md`) documents the local CLI
and optional `--only KEY` for focused checks.

**The PR judge** (`.github/workflows/judge-pr.yml`, running
`.github/scripts/judge_pr.py`, standard library only, nothing of the
application imported) comments on every pull request: two ratings, then a
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
counted by the script when the diff touches what is visible. A PR that links
no issue reads `–` on the issue rows. Folded: the per-criterion table and the
script's own checks (no machine attribution trailer, new code under `app/`,
no Flask import under the backend, no key-shaped string). Everything the
judge reads has been through `redact` first: key-shaped strings and email
addresses never leave the runner. The verdict is a new comment on every run
with the earlier ones collapsed as outdated, never deleted, and the step
summary (cost and calls there, never in the comment). Automatic judging skips
draft PRs; a `/judge` comment from an owner, member or collaborator asks again
on the same commit, including a draft. An eligible newer judge job cancels the
older one for that PR; other comments cannot cancel it. The test workflow also
cancels older runs for the same ref. Experimental:
never a required check; without the secret `TYPESAFE_API_KEY` (or
`API_KEY_TYPESAFE`, the older name) it reports "not configured" and stays
green. Offline: `python3 .github/scripts/judge_pr.py --pr-file pr.md
--diff-file pr.diff [--issue-file issue.md] --dry` builds the states and asks
nothing.

**Run analysis** is the `/run-analysis` skill
(`.claude/skills/run-analysis/SKILL.md`): triage of a run that failed or looks
wrong, an analysis of one job, or a comparison of several, written up as one
self-contained page. Its script, `.claude/skills/run-analysis/scripts/runs.py`
(standard library only, no import from `app/`, reads the job trees and writes
only what `--out` names), prints health verdicts, computes the metrics and
renders the page from the template beside it; the findings and the prose are
the assistant's, by the procedure in the SKILL.md. Nothing in it is specific to
one assistant or one benchmark.

## Conventions

Use `nullius.thinkflowlab@proton.me` for Nullius-owned contact email addresses,
including security reports.

### Attribution

**No machine attribution in commit messages or pull request descriptions** — no
`Co-Authored-By: Claude …` or any assistant variant, no `Claude-Session:` or
other session metadata, no generated-by signature. Keep the message about the
change and why it was made.

**Legitimate human co-authors are preserved.** A `Co-Authored-By` naming a real
person who contributed is correct and expected; this rule removes machine
trailers, not people.

Two things enforce it, so it does not depend on anyone remembering:
`.claude/settings.json` sets `attribution` to empty for `commit` and `pr` with
`sessionUrl` off, and a `commit-msg` hook in the shared Git directory rejects
machine signatures while allowing human co-authors. **Do not bypass the hook**
(no `--no-verify`).

Existing commits are **not** rewritten. Do not amend, rebase or force-push to
strip trailers from history; what is already there is the repository
owner's to audit. The rule applies to the next commit, not the last one.

The metric field guide (`docs/field-guide.md`, published in the documentation
under Reference → Metrics) is a living document: a change that alters what it
defines updates it in the same change. `app/tests/test_field_guide.py` fails
when a field it names leaves the backend, when its stated counts stop
matching its tables, or when its outcome vocabulary stops being the
classifier's; what each metric means is still read by a person.

The builder saves and imports recipes as JSON. Review runs them through the
execution backend; script previews and downloads are not part of the interface.
`app/ui_tests/test_recipe_execution_fixtures.py` pins the compiled execution plans.

### Issues

Before opening anything, **search existing issues *and* pull requests** —
across the repositories involved, not just this one. A duplicate costs more
than the search does, because it splits the evidence.

An issue carries enough to act on without a conversation. The forms under
`.github/ISSUE_TEMPLATE/` (Bug, Enhancement) hold the fields: **Priority**
(how deep it reaches: low the surface, medium the web layer or the
catalogue, high the backend, extreme what runs and proves), **Components**,
**Evidence** (what was observed, quoted rather than characterised), a
**Screenshot** when it is visible, **Reproduction** (the smallest steps or
input that show it), **Expected**, **Acceptance** (one checkbox per
criterion; the judge gives each its own row) and **Dependencies and
related**. A pull request's body follows `.github/pull_request_template.md`:
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

- **Set the native Type and Priority when the issue is created.** An empty
  field is invisible in a list; where the priority is genuinely undecided,
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

One line each; the code and the pages carry the rest.

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
  run hosts, keys and the connection test — `app/nullius/hosts.py`
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
