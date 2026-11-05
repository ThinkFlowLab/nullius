# Contributing

Welcome to the Nullius community! Contributions are welcome, whether you are
reporting a bug, improving documentation, bringing in a benchmark, agent or
capability, or contributing code. If this is your first contribution, this guide
will help you get started.

## Before you start

- **Look for an existing issue or pull request first.** A duplicate splits the
  evidence. If there is none, open one with the Bug or Enhancement form; the
  forms ask for what the fixer and the PR judge read.
- **Say what you intend to do on the issue** before a large change, so nobody
  builds the same thing twice and the shape can be agreed early.
- **A security problem goes to the address in [SECURITY.md](SECURITY.md)**,
  never to a public issue.

## Setting up

Python 3.12 or later and [uv](https://docs.astral.sh/uv/). Flask is the only
dependency, and nothing is installed into the application's environment.

To use a release, follow the [uv installation guide](docs/getting-started.md).
To contribute, run from a checkout:

```bash
git clone https://github.com/thinkflowlab/nullius.git
cd nullius
uv run --no-project --with-requirements app/requirements.txt app/app.py --no-debug
```

The app opens on [127.0.0.1:5057](http://127.0.0.1:5057). Docker is needed only
for the docker backend and for Harbor recipes.

## Where things go

- `app/` is the application. New code goes under it, never loose at the root:
  `app/nullius/` for the backend, `app/web/` for routes, `app/templates/` and
  `app/static/` for the interface.
- `docs/` is the documentation, published as the site.
- `app/nullius/catalogue/` holds the benchmarks, agents and capabilities, one folder
  each.

[AGENTS.md](AGENTS.md) is the map of the repository and the rules every
contributor and coding assistant follows. Read it before your first change.

Three rules are load-bearing and each has a test: the backend under
`app/nullius/` imports no Flask; there is exactly one implementation of
`nullius`, nothing installed; and the fields the templates render are pinned
in `app/tests/field_contract.json`. If a test guarding one of these fails, the
layering has broken, not the test.

## Bringing in a benchmark, an agent or a capability

Use the import skills rather than writing an entry by hand: `/import-benchmark`,
`/import-standard`, `/import-agent` and `/import-capability` under
`.claude/skills/`. Each pins the upstream, wraps the upstream's own code and
proves what fits. Nullius is a facilitator, not a re-implementer: anything
beyond glue is asked about first. The *+ Add* button on the Benchmarks, Agents
and Capabilities pages writes the prompt for any assistant.

## Running the tests

From the repository root. The backend suite needs nothing installed; only the
route suite needs Flask.

```bash
python3 app/tests/parallel.py
uv run --no-project --with-requirements app/requirements.txt python app/tests/parallel.py app/ui_tests
```

Two pins are refreshed by hand after a deliberate change, and the suite fails
while they are stale:

```bash
# after changing a class the templates render
python3 app/tests/field_contract.py --write

# after changing a definition under app/nullius/catalogue
python3 app/tests/catalogue_docs.py --write
```

A change to the interface also gets a look in a browser: the pages it touches,
in both themes, at the widths it affects. The owner's rules for every page are
in `.claude/rules/ui.md`; the short version is less text, names rather than
keys, every choice a card, and no em dashes.

A change to what a metric means updates the field guide,
`docs/field-guide.md`, in the same change.

## Release candidates

The [release guide](app/packaging/README.md) covers building verified downloads
and preparing a draft prerelease. The manual workflow runs the tests and
attaches the wheel, source archive and checksums; publication is a separate
decision by the repository owner.

## Pull requests

- **Small and targeted.** One change per pull request; no refactoring of
  unrelated areas on the way.
- **Fill in the template.** The PR judge compares the description with the
  diff and with the linked issue's acceptance list, and posts an experimental
  verdict. It is never a required check, but it reads what you wrote.
- **Reference the issue** so it shows in the issue's Development panel.
- **Say what you verified.** Tests run, browser checks made, widths tried.
  "Not verified" is an honest answer.
- **No key, no address, no private link** in a body, a commit or a screenshot.

Commit messages are about the change and why it was made. No machine
attribution: no `Co-Authored-By: Claude`, no session metadata, no generated-by
signature. A hook rejects them; do not bypass it. A real person who contributed
is named as a co-author as usual.

Nothing merges without the repository owner's word.
