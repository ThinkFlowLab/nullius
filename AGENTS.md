# Nullius

Bring any benchmark, agent or capability, run it, and open every score into the
evidence behind it: the agent's failure or the benchmark's.

- Runs through the Nullius standard (`docs/standard.md`) or Harbor, only on an
  explicit Run. Also reads Harbor, ATIF and OpenTelemetry results.
- `app/nullius/` is the backend: standard library only, an entry's own code aside.
  `app/web/` is Flask, the only runtime dependency.

The user's instructions come first, then this file, then `.claude/rules/`. This
file holds rules for any assistant: no file lists, no history.

## How we work

- **Small diffs.** No refactoring on the way. A defect found in passing becomes
  an issue, not a wider change.
- **Nearby code over assumptions.** Describe what is. Remove obsolete code and
  historical comments unless asked to keep them.
- **Facilitator, not re-implementer.** A benchmark, agent or capability runs its
  upstream's own code at a pin, in its own environment. Ask before writing more
  than glue.
- **Reuse before adding.** In the backend, for example, `paths`, `ondisk`,
  `redact`, `settings`, `providers`; on a page `_tables.html`, `_cards.html`,
  `tables.js`, `model-picker.js`, `disclosures.js`, `_icons.svg`.
- **No machine attribution** in a commit or a pull request: no assistant
  `Co-Authored-By`, session link or generated-by line. Human co-authors stay.
  Existing commits are not rewritten for it.
- **Nothing merges without the user's word.**

## Three rules, each with a test

1. `app/nullius/` imports no Flask (`test_layering`). A backend test that needs
   Flask means the layering broke.
2. One provider of `nullius` per interpreter (`test_dependency_surface`): a local
   package can shadow an installed one. Source commands use `uv run --no-project`.
3. Fields the templates read are pinned, for the modules `field_contract.py` lists
   (`test_field_contract`). A rename is a blank cell, not an import error.

Bundled files resolve from `__file__`, personal ones through `nullius.paths`.
No personal data in the package.

## Where code lives

New code goes under `app/`, never at the root. A module's docstring says what it
is: `grep -m1 '"""' app/nullius/runs/*.py`.

| Path | Holds |
| --- | --- |
| `app/nullius/runs/`, `traces/`, `compare/` | Reading one job tree, a trial's trace, several runs |
| `app/nullius/catalogue/` | A folder per entry under `benchmarks/`, `agents/`, `capabilities/`: its definition and its own code |
| `app/nullius/recipes/`, `harbor/` | What the builder offers and saves; the Harbor a recipe pins |
| `app/nullius/importers/`, `judge/` | Foreign results in; questions to a judge model |
| `app/nullius/standard/` | The standard: `contract/`, `requirements/`, `protocols/`, `calls/`, `backends/`, `runner/`, `proofs/`, `pages/`, importing one way (`test_layering`), and the conformance `kits/` |
| `app/web/` | One module per capability; `core` is the application, `shared` what pages share. Use another module's names as attributes (`shared.STATE`): patch one, patched for all |
| `app/templates/`, `app/static/` | The interface, under the same capability names |
| `app/tests/`, `app/ui_tests/` | Backend and route suites; fixtures are small and hand-made |
| `app/site/`, `docs/` | The website; the documentation it publishes |
| `.agents/skills/` | Skills; `.claude/skills` links here |

## Commands

From the repository root:

```bash
# backend suite; --docker adds the tests that run containers
python3 app/tests/parallel.py

# route suite
uv run --no-project --with-requirements app/requirements.txt \
  python app/tests/parallel.py app/ui_tests

# the app
uv run --no-project --with-requirements app/requirements.txt app/app.py
```

- The backend suite needs git and uv on PATH. Without Node, the tests of the
  pages' scripts skip.
- unittest by hand: `NULLIUS_HOME` a temporary folder, personal catalogue and
  storage variables unset.
- A page change: its tests and focused browser checks, not the full sweep.
- CI on non-draft pull requests: the tests, and the PR judge, which never gates.

| Changed | Refresh |
| --- | --- |
| A class the templates render | `python3 app/tests/field_contract.py --write` |
| A catalogue definition | `python3 app/tests/catalogue_docs.py --write` |
| A documentation figure | `python3 app/site/diagrams.py --write` |
| The README's animated SVGs | `python3 app/site/readme_media.py --write` |
| What a metric means | `docs/field-guide.md`, by hand |

## Tests and docs

A change owes neither.

- **A test** for a distinct regression no existing test catches: behavior, a
  contract, a failure mode. Not wording, structure or catalogue contents. Extend
  a nearby case first.
- **Removing a test:** name the assertions that still protect its behavior, and
  keep its edge cases and fixture isolation.
- **Docs** when what they say became wrong, or a reader needs them to choose or
  act. The code already says how it works. No prerequisite Review already checks.
- **In `docs/`:** keep guarantees, requirements, refusals and their fix; drop
  mechanism, narration and history. One fact, one page. A table cell is a line.
  No validations, captures or rendered reports.
- Headings of `docs/standard.md` are never renamed: the import skills cite them.

## Catalogue

- One entry per benchmark; what it supports is an option. A version is an
  upstream release or a requested fork, never a change of our driver.
- Definitions hold configuration. Proof runs write evidence to `proofs.json`
  beside them and leave them untouched. Read both through `definitions`; never
  infer proven ports from raw JSON.
- An agent's native capability is a callable its pinned upstream exposes, tested.
  A component's proof does not prove an agent uses it.

## Issues and pull requests

The `file-issue` skill files both. The templates hold the shape and an example.

- Search issues and pull requests first, here and where the cause lives.
- A pull request references its issue, with `Closes` when it finishes it.
- Title: `type(scope): summary`, lowercase except names, no final period. A squash
  makes it the commit on `main`, and the body its message.
  - Types: `feat` `fix` `perf` `refactor` `docs` `test` `ci` `build` `chore` `revert`
  - Scopes: `standard` `catalogue` `runs` `traces` `compare` `judge` `builder`
    `web` `execution` `settings` `docs` `site` `ci` `release`
- No key, email address, personal message or private link.
- A security problem goes to Nullius's contact address,
  `nullius.thinkflowlab@proton.me`, never to an issue (`SECURITY.md`).

## Writing

Short and plain: issues, pull requests, their comments, docs.

- The point first, then why. One fact per line. Fragments are fine.
- Cut facts, not grammar: a stranger must be able to act on it.
- Numbers and quotes, not adjectives. No background, file tour, hedging or
  restated diff.

| No | Yes |
| --- | --- |
| This change introduces a comprehensive improvement to model search, which previously could fail to surface relevant results in certain cases. | `Qwen 32b` found no model. Words now match in any order. |

## Read first

| Working on | Read |
| --- | --- |
| What runs, reading a result | `.claude/rules/running.md` |
| The trial page's trace | `.claude/rules/traces.md` |
| Any page | `.claude/rules/ui.md`; `/ui-sweep` only when asked |
| Vocabulary | `docs/concepts.md` |
| The standard, its ports and kits | `docs/standard.md` |
| Adding an entry | `/import-benchmark`, `/import-standard`, `/import-agent`, `/import-capability`; by hand, `docs/examples/new-benchmark/` |
| Removing an entry | `/remove-integration` |
| How the catalogue loads | Docstring of `app/nullius/catalogue/catalog.py` |
| The run builder | `docs/builder.md`; `app/nullius/recipes/launch.py`; `app/static/builder/command-builder-core.js` |
| A pinned Harbor | Docstring of `app/nullius/harbor/source.py` |
| Running a recipe, hosts | Docstrings of `app/nullius/execution.py` and `app/nullius/hosts.py` |
| The live board | Docstring of `app/nullius/runs/live.py` |
| Settings and storage | `docs/settings/README.md`; `app/nullius/settings.py` |
| Judgement; the PR judge | `docs/judgement.md`; docstring of `.github/scripts/judge_pr.py` |
| A run that looks wrong, comparing runs | `/run-analysis` |
| Stale docs | `/docs-drift` |
| The website; a release | `app/site/README.md`; `app/packaging/README.md` |
| This machine | `.claude/environment.local.md`, gitignored |
