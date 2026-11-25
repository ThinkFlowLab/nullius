# Nullius

Nullius runs AI evaluations and opens every score into its evidence, to tell an
agent's failure from a benchmark's.

- Runs through the Nullius standard (`docs/standard.md`) or Harbor, only when
  asked. Reads Harbor, ATIF and OpenTelemetry results.
- `app/nullius/` is the backend: standard library only, except code that runs in
  an upstream's own environment. `app/web/` is Flask, the web framework and the
  only runtime dependency.

The user's instructions come first, then this file, then `.claude/rules/`. No file
list, no history.

## How we work

- **Small diffs.** No refactoring on the way. A defect found in passing becomes
  an issue.
- **Nearby code over assumptions.** Remove the code and comments your change makes
  obsolete.
- **Facilitator, not re-implementer.** A benchmark, agent or capability runs its
  upstream's code at a pin, in its own environment. Ask before writing more
  than glue.
- **Reuse before adding**, for example `ondisk`, `redact`, `paths`, `providers`.
- **No machine attribution** in a commit or a pull request: no assistant
  `Co-Authored-By`, session link or generated-by line. Human co-authors
  and existing commits stay.
- **Nothing merges without the owner's word.**

## Pages and commands

- **Page and command agree.** Never change one alone, whichever was asked for. Read
  the other side first (`docs/cli/pages.md` pairs them): same options, words, order,
  defaults and numbers, computed once, in `app/nullius/`. The pull request names a
  side left alone, and why.
- **One look.** A thing looks the same on every page: reuse its macro and class, from
  the table in `.claude/rules/ui.md`. New CSS sets no font, size or weight and draws
  no button. None fits: ask the owner.

## Three rules, each with a test

1. No file in `app/nullius/` imports Flask, so it runs without the web app: on
   hosts, in the CLI, in tests. Code that needs it goes in `app/web/`
   (`test_layering`).
2. Only one `nullius` is importable: with two, one hides the other
   (`test_dependency_surface`). Run source with `uv run --no-project`.
3. Templates read fields by name, so a renamed one renders blank.
   `test_field_contract` fails on it, in the modules `field_contract.py` lists.

Find bundled files from `__file__`, the user's through `nullius.paths`; none of
theirs in the package.

## Where code lives

New code goes under `app/`, never at the root. A module's docstring says what it
is: `grep -m1 '"""' app/nullius/*.py app/nullius/runs/*.py`.

| Path | Holds |
| --- | --- |
| `app/nullius/` itself | What all share: settings, paths, providers, hosts, running a recipe |
| `app/nullius/runs/`, `traces/`, `compare/` | Reading one job tree, a trial's trace, several runs |
| `app/nullius/catalogue/` | A folder per entry under `benchmarks/`, `agents/`, `capabilities/`: its definition and code |
| `app/nullius/recipes/`, `harbor/` | What the builder offers and saves; the Harbor a recipe pins |
| `app/nullius/importers/`, `judge/` | Foreign results in; questions to a judge model |
| `app/nullius/standard/` | The standard: `contract/`, `requirements/`, `protocols/`, `calls/`, `backends/`, `runner/`, `proofs/`, `pages/`, whose imports never loop, and the conformance `kits/` |
| `app/web/` | One module per area; `core` is the application, `shared` what pages share. Write `shared.STATE`, so one patch reaches all |
| `app/templates/`, `app/static/` | The interface, under the same names |
| `app/tests/`, `app/ui_tests/` | Backend and route suites; fixtures are small and hand-made |
| `app/site/`, `docs/` | The website; the documentation it publishes |
| `.agents/skills/` | Skills, `/name` here; `.claude/skills` links to it |

## Commands

```bash
# backend suite, needs git and uv; --docker adds the container tests
python3 app/tests/parallel.py

# route suite
uv run --no-project --with-requirements app/requirements.txt \
  python app/tests/parallel.py app/ui_tests

# one module; a route module under uv as above, with -s app/ui_tests
NULLIUS_HOME=$(mktemp -d) python3 -m unittest discover -s app/tests -t app -p test_live.py

# the app, and the CLI
NULLIUS_HOME=$(mktemp -d) uv run --no-project --with-requirements app/requirements.txt \
  app/app.py --debug --port 8791
PYTHONPATH=app NULLIUS_HOME=$(mktemp -d) python3 -m nullius --help

# the docs' links and paths
python3 .agents/skills/docs-drift/scripts/check_docs.py
```

- Always a profile of your own (`NULLIUS_HOME`). Never stop the user's server: it
  follows their runs.
- A draft gets no CI: run the suites you touch. `/judge` in a comment asks the
  judge, which never gates.

| Changed | Refresh |
| --- | --- |
| A class the templates render | `python3 app/tests/field_contract.py --write` |
| A catalogue definition | `python3 app/tests/catalogue_docs.py --write` |
| A documentation figure | `python3 app/site/diagrams.py --write` |
| The README's animated SVGs | `python3 app/site/readme_media.py --write` |
| What a job or trial holds, outcomes included | `SCHEMA` in `app/nullius/runs/parse.py`, plus one: opened runs are cached |
| A setting | `app/.env.example`, from `settings.example()` |
| What a metric means | `docs/field-guide.md`, by hand |

## Tests and docs

Add neither by default.

- **A test** when no existing one would catch this breaking: behavior, a contract,
  a failure mode. Not wording, structure or catalogue contents. Extend a nearby
  case first.
- **Removing one:** name what still protects its behavior and edge cases.
- **Docs** when what they say became wrong, or a reader needs them to choose or act.
- **In `docs/`:** keep guarantees, requirements, refusals and their fix; drop
  mechanism, narration, history and what the builder already checks. One fact, one
  page. A table cell is a line. No validations, captures or rendered reports.
- Headings of `docs/standard.md` are never renamed: the import skills cite them.

## Catalogue

- One entry per benchmark or agent; what it supports is an option. Each version is
  **Official**, a tag, branch or commit of the official source, or **Experiment**,
  another source or someone's fixes; never a change of our driver. Bring an
  official version first.
- Definitions hold configuration. Proof runs write only `proofs.json`: a bundled
  entry's in a user copy, which then hides the bundled one. Read both through
  `contract/definitions.py`; never infer proven ports from raw JSON.

## Issues and pull requests

The `file-issue` skill files both.

- Search issues and pull requests first, here and where the cause lives.
- A pull request opens as a draft, with `Closes #n` when it finishes its issue,
  else `Refs #n`.
- Title: `type(scope): summary`, lowercase except names, no final period. A squash
  makes it the commit on `main`, and the body its message.
  - Types: `feat` `fix` `perf` `refactor` `docs` `test` `ci` `build` `chore` `revert`
  - Scopes: `standard` `catalogue` `runs` `traces` `compare` `judge` `builder`
    `web` `execution` `settings` `docs` `site` `ci` `release`
- No key, email address, personal message or private link.
- A security problem goes to the address in `SECURITY.md`, never to an issue.

## Writing

Short and plain: issues, pull requests, their comments, docs.

- The point first. One fact per line. Fragments are fine.
- Drop facts, not words: a stranger must be able to act on it.
- Numbers and quotes, not adjectives. No background, file tour, hedging or
  restated diff.

## Read first

| Working on | Read |
| --- | --- |
| What runs, reading a result | `.claude/rules/running.md` |
| The trial page's trace | `.claude/rules/traces.md` |
| Any page | `.claude/rules/ui.md`; `/ui-sweep` only when asked |
| Vocabulary | `docs/concepts.md` |
| The standard, its ports and kits | `docs/standard.md` |
| Adding an entry | `/import-benchmark`, `/import-standard`, `/import-agent`, `/import-capability`; by hand, `add-manually.md` under its `docs/` folder |
| Removing an entry | `/remove-integration` |
| The run builder | `docs/builder.md`; `app/nullius/recipes/launch.py`; `app/static/builder/command-builder-core.js` |
| A whole design | Docstrings of `execution.py`, `catalogue/catalog.py`, `harbor/source.py`, `runs/live.py` |
| A command | `docs/cli/README.md` |
| Settings and storage | `docs/settings/README.md`; `app/nullius/settings.py` |
| Judgement; the PR judge | `docs/judgement.md`; docstring of `.github/scripts/judge_pr.py` |
| A run that looks wrong, comparing runs | `/run-analysis` |
| Stale docs | `/docs-drift` |
| The website; a release | `app/site/README.md`; `app/packaging/README.md` |
| This machine | `.claude/environment.local.md`, if present |
