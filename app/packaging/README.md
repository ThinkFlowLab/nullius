# Preparing and publishing a release candidate

The PyPI distribution is `nullius-eval`. The application, Python import package
and executable remain `nullius`. The separate package named `nullius` on PyPI
is not this application.

The `draft release` workflow prepares GitHub drafts with an installable wheel,
a source distribution and `SHA256SUMS`. It does not publish the release or
upload to PyPI. A separate manual `publish to PyPI` workflow uploads reviewed
GitHub release assets. Neither workflow runs on a push or tag event.

The repository stays private; publication to PyPI makes the uploaded package
and its included source public, independently of GitHub access.

`pyproject.toml` owns the package version. The release tag is that version
prefixed with `v`: version `0.1.0rc1` corresponds to tag `v0.1.0rc1`. Release
notes live in `docs/releases/<tag>.md`.

## Build the downloads locally

From the repository root, with Python 3.12 or later and `uv` available:

```bash
python3 app/packaging/release.py build \
  --out-dir /tmp/nullius-release \
  --tag v0.1.0rc1
```

The tag must match the package version, and the output directory must be absent
or empty. Existing downloads are never overwritten. The output contains:

```text
nullius_eval-0.1.0rc1-py3-none-any.whl
nullius_eval-0.1.0rc1.tar.gz
SHA256SUMS
```

The wheel is built from the source distribution, so both downloads participate
in validation. The build uses the existing installed-package check in
`app/packaging/verify.py`. That check installs into temporary environments,
checks the backend without Flask, then checks the installed web UI and CLI.
It verifies the resource inventory and preservation of user storage across
tool replacement. It does not run a benchmark or read personal settings.

To check a wheel independently:

```bash
python3 app/packaging/verify.py /tmp/nullius-release/nullius_eval-0.1.0rc1-py3-none-any.whl
```

To validate the complete downloaded set, including checksums and package
metadata, without rebuilding it:

```bash
python3 app/packaging/release.py verify \
  --dist-dir /tmp/nullius-release \
  --tag v0.1.0rc1
```

The source distribution is a Python build input. Neither archive is a
standalone Windows, macOS or Linux desktop installer. Current release checks
run on Linux with Python 3.12; do not infer additional platform support from
the wheel's `py3-none-any` filename.

## Prepare the GitHub draft

1. Review and merge the version, release notes and intended changes into `main`.
2. Open **Actions → draft release → Run workflow**, select `main`, and provide
   the matching tag, such as `v0.1.0rc1`.
3. The workflow runs the backend and route suites, builds and verifies the
   packages, then creates a draft prerelease for the exact commit it checked
   out. Its assets are the wheel, source distribution and checksum file.
4. Review the draft's target commit, notes, assets and installation instructions.
   Download and test the wheel before requesting permission to publish.

The workflow refuses to overwrite an existing release. It has no push or
scheduled trigger. Starting it prepares a draft; it does not authorize
publication. Publish only after the repository owner explicitly approves the
reviewed release.

Drafts are for maintainers with write access. To offer the downloads to other
repository readers, publish the reviewed prerelease; it need not be a stable
release.

Once a release is published, keep its tag and history stable. Put subsequent
changes in new commits and a new version; do not amend the initial commit or
replace a published package under the same version. One squash commit per PR
remains compatible with this workflow.

## Configure PyPI publishing once

This checkout prepares the workflow and package metadata; it does not create
PyPI projects, register publishers or change GitHub environments.

1. Check that `nullius-eval` is still available on PyPI, or that the publishing
   account controls the project if it already exists.
2. Create a GitHub environment named `pypi` in `ThinkFlowLab/nullius`, restricted
   to `main`. Configure required reviewers where the repository's GitHub plan
   supports them. Naming an environment does not itself create an approval gate.
3. In the intended PyPI owner's account, configure a pending Trusted Publisher
   for project `nullius-eval`, GitHub owner `ThinkFlowLab`, repository `nullius`,
   workflow filename `publish.yml`, and environment `pypi`. For an existing
   project, configure the same Trusted Publisher on that project instead.

The workflow uses GitHub's short-lived OpenID Connect identity through
`uv publish --trusted-publishing always`; no PyPI API token is stored in the
repository. A pending publisher does not reserve a package name. See
[PyPI's pending-publisher guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

## Publish the reviewed candidate

1. Obtain the repository owner's approval for the exact candidate and files.
   Confirm the package version, release notes, PyPI description in
   `app/packaging/PYPI.md`, wheel and source archive are ready to be public.
2. Publish the reviewed GitHub draft as a prerelease. It remains subject to the
   repository's access permissions. The PyPI workflow requires a published
   GitHub release and does not publish a draft for you.
3. Open **Actions → publish to PyPI → Run workflow**, select `main`, enter the
   release `tag` and enable `confirm`. Confirmation defaults to false.
4. The validation job downloads the release's wheel, source archive and
   checksums, validates their names, versions and hashes, runs isolated
   installation checks, and runs `twine check --strict` on the distributions.
5. The publishing job uses the `pypi` environment and downloads the exact asset
   IDs selected by validation. It compares their bytes with the validated
   SHA-256 hashes before uploading those two distributions. It does not rebuild
   the package, and its OpenID Connect permission is separate from validation.

After a successful upload, verify an installation outside the source checkout:

```bash
uv tool install nullius-eval
nullius --version
nullius serve
```

For a previously installed private build under the old distribution name,
uninstall that tool with `uv tool uninstall nullius` before installing
`nullius-eval`. User settings, catalogue additions and runs are outside the
tool environment and remain in place.

The default command installs the latest version without a pin. To select this
specific candidate, use `uv tool install nullius-eval==0.1.0rc1`. Add `--force`
when replacing an existing installation. `uv tool upgrade nullius-eval`
updates an unpinned installation; it retains an explicit version constraint.
