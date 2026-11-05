---
name: ui-sweep
description: Capture and review Nullius's builder and catalogue screenshots against the owner's UI rules, flagging page JavaScript errors and clipped text. Use for an explicitly requested full UI sweep, reviewing a nightly sweep, or a focused benchmark check with --only. Routine repository UI changes require relevant tests and focused browser checks, not a full sweep.
---

# UI sweep

For routine repository changes to templates, JavaScript, CSS, labels, options
or bundled catalogue entries, run the relevant unit and route tests and check the affected pages and
interactions in a browser against `.claude/rules/ui.md`. Cover the themes and
viewport sizes the change affects. A full screenshot sweep is for nightly or
explicitly requested manual coverage, not a condition for completing every UI
change; `--only KEY` remains available for a focused benchmark check.

For an installed user's catalogue addition, follow its import skill's
definition checks and inspect the affected pages. No repository tests,
generated docs or sprite edits are required. The sweep script is a separate
skill resource, not part of the installed Nullius wheel; use it when this
complete skill is available, or make the focused browser checks directly.

The full sweep visits every benchmark in both builder runners, their steps and
settings tabs, light and dark, the standard runner's Agent step at phone width,
and the Benchmarks, Agents and Capabilities pages.

## GitHub Actions

`.github/workflows/ui-sweep.yml` is configured for a nightly run at 02:17 UTC
(`17 2 * * *`) and `workflow_dispatch`, with no push or pull-request trigger.
Once the workflow is on the default branch, open **Actions → UI sweep → Run
workflow**, choose the branch, and optionally enter a benchmark key. Leave it
empty for the full sweep; the nightly run uses full coverage.

The workflow starts Flask and Playwright in an isolated runner and prints
`report.json` to the run's log; nothing is kept as an artifact, since
artifacts count against the account's storage. Its automated checks fail on
page JavaScript errors or clipped text; there is no pixel-baseline
comparison. Screenshots are the local sweep's, and a clean report still
needs that visual review for clutter, misplaced settings and the other UI
rules.

## Local CLI

1. Select the profile to inspect with `nullius paths --json`. For a clean
   sweep, choose a scratch `NULLIUS_HOME` so configuration, data and cache stay
   separate; `WORKSPACE=ui-sweep` alone is only a label. Explicit directory
   overrides such as `NULLIUS_CATALOGUE`, `RUNS_DIR` and `WORKSPACES_DIR` still
   take precedence, so check the resolved paths in that environment. For a
   personal entry, use its profile intentionally or stage its whole folder in
   the scratch catalogue; partial folders hide the bundled entry in full.

   Run the installed app (restart it after a definition change when needed):

   ```bash
   NULLIUS_HOME=/absolute/scratch/profile nullius serve --port 8791 --no-debug
   ```

   To check source changes, run from the checkout root instead (`--no-debug`
   caches templates, so restart after template changes):

   ```bash
   NULLIUS_HOME=/absolute/scratch/profile uv run --no-project --with-requirements app/requirements.txt app/app.py --port 8791 --no-debug
   ```

   `python -m nullius serve` also works with an interpreter that has Nullius;
   the shell's Python may differ from a `uv tool` installation. The private
   `_nullius_app` namespace is not a user-facing launch command.

2. Sweep (a few minutes; add `--only KEY` for one benchmark's builder steps;
   catalogue pages are still checked). Install Chromium if needed with
   `uv run --no-project --with playwright python -m playwright install chromium`, or pass
   `--browser PATH` for an existing Chromium executable:

   ```bash
   uv run --no-project --with playwright python /absolute/path/to/ui-sweep/scripts/sweep.py \
     --url http://127.0.0.1:8791 --out <scratch dir>/sweep
   ```

   It exits nonzero for page JavaScript errors or clipped text and prints the
   affected screenshots; `report.json` also lists each one's red warning lines.
   Resolve the script relative to this skill; in a checkout it is
   `.claude/skills/ui-sweep/scripts/sweep.py`. Keep captures outside installed
   package resources.

3. Look at every screenshot of what changed, with `.claude/rules/ui.md`
   beside it: a clean report cannot see clutter or a misplaced setting.

4. Report decision first: clean, or which rule breaks where.
