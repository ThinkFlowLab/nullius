---
name: remove-integration
description: Remove a benchmark, agent or capability from the whole Nullius application, including its adapter, catalogue, proofs, documentation, tests and generated artifacts. Use for deleting or retiring an integration, not merely hiding its card or restoring a bundled entry.
---

# Remove an integration

Remove the implementation and its references, not just its UI entry. Follow
the user's requested scope, including logs and stored data when requested.

## Locate every copy

- Collect the label, key, aliases, former paths, upstream repository, unique
  package/module names, environment variables and pinned revision. Search
  filenames and directory names as well as file contents; empty directories
  are invisible to text search.
- Use `rg` across tracked and untracked source, docs, skills, comments, tests,
  fixtures and workflow files. Then scan ignored/generated files with
  `--hidden --no-ignore`, excluding `.git`; include bytecode and archive
  contents where relevant. Print matching paths rather than private log contents.
- Catalogue entries live under `app/nullius/catalogue/`. Capabilities can also
  have leftover folders under the legacy `features/` directory. Remove the
  whole entry folder: an empty folder can still be selected by the loader.
- For requested runtime cleanup, locate the application's actual profile,
  catalogue, runs, workspaces and standard installs through its settings and
  path APIs. With `uv tool`, use the installed tool's interpreter; the shell's
  Python may not import Nullius. A checkout scan does not inspect another
  machine's installation or user data.

## Remove the integration

- Delete its definition, adapter, dedicated assets, install configuration,
  options and proofs. Remove registrations and callers if any; retain shared
  dependencies and infrastructure still used elsewhere.
- Delete integration-specific tests and fixtures. Keep tests of shared
  behavior by using an existing supported entry or a generic fixture. Do not
  leave retired names in negative assertions, compatibility stubs or new notes.
- Update authored docs, examples and assistant instructions. Regenerate the
  supported catalogue pages with `python3 app/tests/catalogue_docs.py --write`;
  do not hand-edit generated tables.
- Remove stale copies from generated website output, package staging,
  distribution metadata, bytecode and local archives, or rebuild those
  artifacts from the cleaned sources. Inspect the resulting wheel and sdist,
  not only their source manifests.
- When logs/data are in scope, remove identified copies and affected records
  consistently, including their cached workspace references. Preserve unrelated
  runs and entries; do not alter results to make them appear to come from a
  different component. Inspect mixed records before deciding what to remove.
  Do not purge shared download caches based on a common dependency name.

## Verify and report

- Repeat the content and path scans using every collected identifier. Inspect
  archives separately. Zero text matches alone does not prove the catalogue
  has no stale directories or the installed package has no old copy.
- Check the loaded catalogue, builder choices and routes with an isolated user
  profile. Run affected backend and route tests, documentation drift checks,
  and a focused browser check for visible catalogue changes. Rebuild/check the
  site and package when they were affected. No paid benchmark run is needed
  solely to prove an entry was removed.
- Report what was removed and verified, plus any inaccessible installations,
  logs, stored user overrides or published docs that still need updating.
  Removing a bundled entry does not erase user copies or upgrade an existing
  `uv` installation. Source changes do not publish the documentation website.
- Git history and remote artifacts are separate from the working tree. Do not
  rewrite commits or delete remote deployments merely because source removal
  was requested; follow explicit authorization for those actions.
