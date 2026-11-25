---
name: docs-drift
description: Check that the documentation still describes the application — run the mechanical check (scripts/check_docs.py), then read the surfaces that describe whatever changed and fix what has drifted. Use after a refactor, a move or rename, a closed issue, or when asked "are the docs up to date?".
---

# Docs drift

Two halves. The **mechanical** half is `scripts/check_docs.py` beside this
file, run by hand. In the order `checks()`
runs them:

- relative hrefs in `docs/**/*.html`, and links in every tracked `.md`;
- repository paths named in backticks in a tracked `.md`, and files under
  `app/` or `docs/` named in a fenced command;
- the `docs/` folder map in `docs/README.md`;
- AGENTS.md "Where code lives" against the packages of `app/nullius`, both ways;
- the importer registry against the README table.

A path is only checked when its first segment is a top-level entry of the
repository, so a path under a wrong top-level directory passes silently.

The **semantic** half is this skill: a paragraph that still names a real file
but describes a shape the code no longer has.

Arguments: an optional git ref (`/docs-drift main`, `/docs-drift HEAD~5`).
Without one, the scope is everything since the branch left `main`
(`git merge-base HEAD main`). On `main` itself that scope is empty: pass a ref,
or name the merge commit and diff its first parent.

## Procedure

1. **Run the check first.** `python3 .agents/skills/docs-drift/scripts/check_docs.py`,
   on a bare `python3`.
   Fix every finding before reading anything. Remove
   obsolete references unless the user explicitly asks to keep them. An
   intentionally absent path, such as a gitignored local configuration,
   goes in the script's `KNOWN_ABSENT` dict with its reason; something shaped like a path that is not one of this
   repository's (a trial sidecar such as `agent/trace.json`) goes in
   `NOT_REPO_PATHS`. Never skip one silently.

2. **Scope what changed.** `git diff --name-only <ref>...HEAD` (and
   `git status --short` for the uncommitted). Map each changed path to the
   documents that describe it with the table below, and read *those* fully,
   not the whole set.

   | changed under | read these, asking "does this paragraph still describe the code?" |
   | --- | --- |
   | `pyproject.toml`, `app/nullius/cli.py`, `app/nullius/__main__.py`, `app/nullius/paths.py`, `app/nullius/migrate.py` | README Quickstart; `docs/getting-started.md` installation and migration; `docs/cli/README.md`; `docs/settings/README.md` storage and precedence; AGENTS "Three rules, each with a test"; preserve source commands as well as installed commands |
   | `app/nullius/catalogue/storage.py`, `app/nullius/standard/contract/definitions.py`, `app/nullius/standard/runner/payload.py` | `docs/standard.md`, `docs/builder.md`, `docs/how-it-works.md`; all import skills and manual-add guides: user overlays versus bundled contributions, whole-folder selection, and runtime snapshots sent to hosts; generated Supported pages use explicit bundled roots |
   | `app/nullius/runs/` | README "Reading a result"; `.claude/rules/running.md` "Reading a result"; `docs/field-guide.md`, what each field it names means; `docs/how-it-works.md` "Page by page"; `docs/cli/` when a CLI changed |
   | `app/nullius/traces/` | `.claude/rules/traces.md`; README "Reading a result" |
   | `app/nullius/importers/` | README "Ways to run" and "Local by design"; `.claude/rules/running.md` "Running the app"; `docs/cli/import.md` |
   | `app/nullius/compare/` | `docs/cli/read-jobs.md`; `docs/field-guide.md`; `docs/how-it-works.md`, the `/compare` row |
   | `app/nullius/catalogue/`, `app/nullius/recipes/`, `app/nullius/harbor/` | README "What it does" and "Ways to run"; `docs/builder.md` "Stages"; `.claude/rules/running.md` "What this repository runs"; for `harbor/`, the `source.py` docstring and `docs/builder.md` "Which Harbor"; for `catalogue/`, `.agents/skills/import-benchmark/SKILL.md` (a Harbor entry, the listing rule) too; for `providers.py`, the provider rows of `docs/builder.md` "Which providers" |
   | `app/nullius/execution.py`, `app/nullius/hosts.py`, `app/nullius/settings.py` | `.claude/rules/running.md` "What this repository runs"; README "What it does"; `docs/builder.md` "Run"; `docs/settings/README.md`; `docs/how-it-works.md`, Paths 2 and 3 |
   | `app/nullius/judge/`, `app/web/judge.py`, `app/web/judge_tasks.py`, `app/web/reconcile.py` | `docs/judgement.md`; README "What it does" |
   | `app/nullius/workspace.py`, `app/nullius/workspace_cli.py` | `.claude/rules/running.md` "Running the app"; `docs/how-it-works.md` "What the workspace keeps"; `docs/cli/read-jobs.md` |
   | `app/nullius/redact.py` | `.claude/rules/running.md` (logs are redacted) |
   | `app/web/`, `app/templates/`, `app/static/` | README "What it does", for the page that changed; `docs/how-it-works.md` "Page by page"; `.claude/rules/traces.md` for the trial page's strips; `app/tests/field_contract.json` when a template reads a different field (refreshed with `python3 app/tests/field_contract.py --write`) |
   | `app/tests/`, `app/ui_tests/` | a *new module* is named in AGENTS Commands only if it is run differently |
   | `.github/workflows/`, `app/requirements.txt` | AGENTS "Commands"; README "Quickstart" and "Documentation"; `.claude/rules/running.md` "Running the app" |
   | `docs/` | `docs/README.md` — the gate checks its folders, you check the one-line descriptions; `docs/cli/` against each command's `--help` |
   | `.agents/`, `.claude/` | AGENTS.md "Read first" |
   | a document itself (`README.md`, `AGENTS.md`, `docs/field-guide.md`, a rule, a topic page) | the changed paragraphs against the code they describe, and every other document in this table that says the same thing |
   | anything that closes or opens an issue | the issue's body and Development link; the README or rule sentence that cites it by number |

   When the change removes or renames something - a route, a page, a module,
   a function, a flag - also `git grep` the old names across the whole tree.
   What still names them is drift no row routes: a docstring or a comment, a
   Settings help text in `app/nullius/settings.py` (and the example settings
   file generated from it, which `app/tests/test_settings.py` compares), a
   builder script under `app/static/`. Remove historical commentary unless
   the user explicitly asks to keep it. A link in code that now leads nowhere is a bug, not
   drift: record it, in the report or as an issue.

3. **Then the surfaces everyone forgets**, whatever the scope:
   - README.md "Quickstart" and "Documentation";
   - AGENTS.md "Where code lives", "Commands" and "Read first";
   - `.claude/rules/running.md` — its commands still run as written;
   - `docs/how-it-works.md` "The pieces" — the backend list;
   - `docs/cli/pages.md` — the covered rows still run, and a command that
     gained a flag has it in the table.

4. **Fix in place**, in the same commit as the code when the code is still
   uncommitted, otherwise one `docs:` commit naming what drifted and why. A
   sentence that turned out wrong is corrected, not deleted.

5. **Report** so each drift reads at a glance, in this order:
   - three lines: what the gate found; what the reading found, and which
     documents were read; what was left and why. "0 findings" from the gate is
     not "nothing drifted".
   - one table row per drift: `file:line` | what the document says | what the
     code does | where the code shows it (`file:line`).
   - each fix as a ```` ```diff ```` block, one per file, headed by the file and
     line, the old text on `-` lines and the new on `+` - the fix made (the
     commit's change, trimmed to the lines that moved) or, when asked not to
     edit, the fix proposed. Never "old: … new: …" prose.
   - bugs found in passing (a link in code that leads nowhere, a dead route)
     in a short list of their own, outside the docs fix.

## What this is not

Not a rewrite pass: leave prose that is still true alone, even when it could
be shorter. Not the place to add a document.
