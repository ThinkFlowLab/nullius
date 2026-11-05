---
name: import-agent
description: >-
  Bring an agent into the Nullius standard (docs/standard.md): find its own
  ACP support or a maintained ACP adapter (else its own non-interactive
  command), pin it, point its model at the relay, write agent.json, check it,
  prove its ports with the conformance kits, compare ACP with its own CLI, and
  report decision first. Uses the agent as its vendor ships it; never writes
  an agent loop of its own. Use whenever someone wants to add, import,
  onboard, wrap, integrate or "plug in" an agent - a coding agent, a CLI
  agent, an ACP adapter, a new release of an agent already here - including
  a pasted "Add the agent NAME to Nullius. Source: LINK. Remark:
  TEXT." Not for a benchmark (import-standard, import-benchmark), nor a
  memory or retriever (import-capability).
---

# Import an agent

An assistant without skills reads this file directly and follows it as
written; "the question tool" is whatever multiple-choice prompt it has, else
a plain question that waits for the answer.

The user wants one thing: their agent offered in Nullius against every
benchmark it fits. `cli` below is `nullius standard`; `python -m nullius standard`
also works with a Python that has Nullius installed. The source form remains
`PYTHONPATH=app python3 -m nullius.standard.cli` from the checkout root. Jobs
go to `jobs_standard/` relative to the working directory, or `--jobs PATH`;
keep them outside package resources. Read `docs/standard.md` ("An agent",
"Versions", "Options", "Fidelity") when available in a checkout, otherwise
the installed schema and examples below. An installation needs no `app/` tree.

## Locate the runtime and writable catalogue

Run `nullius paths --json` with the same profile/environment as the app.
`NULLIUS_HOME` selects `config/`, `data/` and `cache/` under a profile directory;
otherwise platform user paths apply. `NULLIUS_CATALOGUE` overrides the user
catalogue (Linux default: `~/.local/share/nullius/catalogue/`). Keep settings,
secrets and mutable data outside the installed package.

For reading modules or using their APIs, select the interpreter behind the
`nullius` launcher, not an arbitrary `python3`. For a POSIX `uv tool` install,
check `command -v nullius`, set `NULLIUS_PY="$(uv tool dir)/nullius/bin/python"`,
and verify `"$NULLIUS_PY" -c 'import sys, nullius; print(sys.executable); print(nullius.__file__)'`.
For a different installation, inspect its launcher or virtual environment.
For source inspection, use `NULLIUS_PY=python3` and prefix the command below
with `PYTHONPATH="$PWD/app"` from the checkout root:

```bash
"$NULLIUS_PY" - <<'PY'
from nullius import paths, settings
settings.load(paths.settings_file())
from nullius.standard import definitions, howto
print("schema:", definitions.__file__)
print("examples:", howto.__file__)
print("catalogue:", paths.catalogue_dir())
PY
```

`app/nullius/` references below identify source modules; read their resolved
installed equivalents when no checkout exists. Path APIs return strings:
join with `os.path.join` or `Path(value)`. In the same settings-loaded
interpreter, `definitions.entry_dir("agent", key)` locates the selected
entry; `definitions.writable_dir("agent", key)` copies a bundled entry's
whole folder into the user catalogue before a manual edit. A user folder
replaces the bundled folder in full, including wrappers and model templates.
Never edit `site-packages` or the private `_nullius_app` UI package.

Default to a personal import. No source checkout, repository tests, generated
docs or sprite edits are required. For an explicitly requested bundled
contribution, author/prove in a user or scratch catalogue, then copy only the
intended entry files into `app/nullius/catalogue/agents/<key>/` and use the
repository gates below. CLI mutations protect the bundled tree in either layout.

## The rule: facilitator, not re-implementer

- **Run the agent as its vendor ships it.** Its own ACP, or a maintained ACP
  adapter (the vendor's first), or its own non-interactive command. Never an
  agent loop, tool set, prompt or model client of ours. The reference agent
  in the catalogue is Nullius's own test agent, not a template for wrapping a
  real one.
- **Its dependencies are its own.** Installed by its `install` block into its
  own folder, pinned; never into the app (Flask only).
- **Our code is glue.** At most a thin start-up wrapper beside the definition
  (`{here}/...`) that sets up the agent's own home or services, starts its
  own entry point, and passes ACP or its output through - each change it
  makes justified by the protocol or the agent's own documentation.
- **Ask before re-implementing.** If anything seems to need our own version
  of what the agent or its adapter does (an adapter we would write, a patched
  prompt, a tool, a loop), stop: say what, why nothing upstream does it, and
  what it changes about the agent being measured; ask with the question
  tool, and write nothing of it before the yes. Without a way to ask (a
  subagent), report it as an open question and leave it unwritten.

## 1. Read the input (minutes, no install)

The input is usually a name, a source link (a git repository, a package page
on npm or PyPI, a Hugging Face page, a website) and a description. From the
link and the agent's own docs, find, in this order:

1. **Already here?** `cli list` reads bundled and user entries together. An
   agent already in the catalogue gets a version, not a second folder (step 5).
2. **ACP.** Does the agent speak ACP itself (an `acp` subcommand or flag)?
   Else is there a maintained adapter - the vendor's own, then one the ACP
   project lists? Note its package, latest release and date, and whether it
   pins the agent's core or takes a range.
3. **Without ACP:** its own non-interactive command (a `run`, `exec` or
   `--print` mode that takes the task and exits). It can then offer only
   `agent.task/v1` and `agent.workspace/v1`; say so.
4. **Its model client:** which variables or config file set its endpoint,
   key and model; which APIs it speaks (`chat`, `responses`, `messages`).
5. **Licence**, and whether it has a CLI of its own (for fidelity).

Say the route in one line before going on: `<agent> -> ACP itself | adapter
<package>@<version> | command template`. With no ACP, no adapter and no
non-interactive mode, stop: it cannot be driven without a loop of ours, and
that is the user's question, not a default.

## Native interfaces and agent-capability combinations

Inspect the pinned upstream version for callable native operations and supported
extension points. Record the interface and source evidence, including settings
in the existing adapter that disable a native capability. Test what is exposed
through thin adapters only. Do not replace native memory with conversation
history, file search, an added retriever or our own implementation and label it
native. Passing recall does not prove updates, forgetting or persistence.

When asked whether agent X can use capability Y, distinguish **native**,
**augmented** (Y is added) and **replacement** (the native path is disabled or
redirected to Y). Use only upstream-supported configuration, plugins or tools.
Do not silently fall back to native behavior. Prove Y independently, then check
the combination with evidence that X actually calls Y and uses its results.
Record both pins and the configuration; Y's proof alone does not prove X+Y.

Report **not exposed**, **not tested** or **blocked** separately from a check
that actually ran and **failed**. These are report terms, not schema fields.
The current runner and matcher separate agents from capabilities: no generic
swap recipe exists. Report that integration gap instead of inventing a port,
writing unsupported definition fields or claiming a combination was tested.
A new operation needs a generic contract and kit; a harder use of the same
contract can be a kit skill. Follow the existing approval rule for new ports.
See `docs/standard.md`, "Native capabilities and composition".

## 2. Scaffold and fill in

`cli new agent KEY` (lower case letters, digits, dashes) writes
`agents/KEY/agent.json` under the active user catalogue and prints its path.
Fill it in, starting from
the shape `app/nullius/standard/howto.py` shows (`AGENT_JSON`,
`AGENT_OPTIONS`) and the catalogue's agents (their shape, never their
contents):

- `upstream`: `package`, `version`, `license` (or `repository` and a full
  `tested` commit); when an adapter wraps a core, `core` says which. Pin the
  latest release (a package's latest version; a repository's latest release
  tag, recorded as `upstream.tag`, its commit in `tested`), a commit only
  when there is no release or the user asks. Never patch the agent's or the
  adapter's bugs: keep its behaviour, say it in the report (a one-clause note
  in `summary` only when someone choosing it must know), ask when it blocks.
- `install` pinned: `npm` as `name@version`, `pip` as `name==version`
  (`python` for its version). An adapter whose core floats (a caret range)
  gets the core pinned beside it.
- `start.acp`: the ACP program, with `{install}` (its install folder) or
  `{here}` (the definition's folder). Without ACP, `start.command` carrying
  `{prompt}`.
- `model`: `apis`, and `env` and/or `files` (config templates beside the
  definition, written into its home) with `{base_url}`, `{key}`, `{model}`,
  `{home}`, under the names the agent itself reads, so every call goes to
  the relay. `side_models` only for models its side jobs call that the
  provider serves.
- `caches`: what it downloads under `{home}/` that no one reads after the
  unit; better, switch the download off in its config.
- `options`: what a run may set of its behaviour, each `default` the
  vendor's own, its source said in `summary`, filled as `{opt_<name>}`.
  Every setting the other agents declare (`reasoning_effort`) that its
  upstream has, same name; `default` leaves it unset. Proof: the relay's
  `calls.jsonl`.
- `summary`, the agent's and each option's: one short sentence (a tooltip).
- `fidelity.command`: its own non-interactive command, carrying `{prompt}`,
  when it has a CLI.
- Leave `offers` and `proven` empty: `cli conform` writes them.

Then `cli check` - no problems.

## 3. Ask before running it

`--unsandboxed` runs the agent on this machine as the user: it is the
user's word, not yours. Ask once with the question tool before the first
run, saying what it means, and offer `--backend docker` (each unit in a
container, its relay beside it) when docker is usable. Without a way to ask,
run only if your instructions already say yes.

## 4. Prove

1. `cli conform KEY --model <cheap model> --unsandboxed` (or `--backend
   docker`): every kit it can take; `--port PORT` for one kit, `--repeat 3`
   to hold a flaky check by the majority.
2. **The relay saw its calls:**
   `cat jobs_standard/*__kit__*__<key>*/*/agent/calls.jsonl | head -3`. Empty
   means it called its vendor directly: declare the relay as a provider of
   its own in its config file (`model.files`), never patch the agent.
3. Read every failure before believing it (`agent/acp.jsonl`,
   `agent/agent.log`, `agent/calls.jsonl`, `verifier/score.json`): the
   agent's, the adapter's, the kit's or the definition's. Fix only what is
   ours; a model's answer is never edited, and an upstream bug is an issue
   for its repository, asked about first.
4. With a CLI: `cli fidelity KEY --model <cheap model> --unsandboxed` - does
   ACP send its model the same requests as its own command? A `differs` is
   reported field by field, and fixed in the definition (an option written
   explicitly), not in the agent.
5. A benchmark trial: `cli run <a benchmark it fits> KEY --model <cheap
   model> --limit 2 --unsandboxed`; `cli fit <benchmark>` says which fit.
6. `cli check`, then open the jobs with `nullius serve /absolute/path/to/jobs_standard
   --no-debug` using the same profile/catalogue. Check the Agents tab, the
   builder's eligible choices and a trial's evidence. From source, use
   `uv run --no-project --with-requirements app/requirements.txt app/app.py
   /absolute/path/to/jobs_standard --no-debug`.
7. **Bundled contributions only:** from the checkout root, refresh
   `python3 app/tests/catalogue_docs.py --write`, run `python3 app/tests/parallel.py`,
   the route suite (`uv run --no-project --with-requirements app/requirements.txt
   python app/tests/parallel.py app/ui_tests`), and
   `python3 .claude/skills/docs-drift/scripts/check_docs.py`. Use an isolated
   profile with an empty user catalogue so personal overrides cannot mask the
   contributed files. Personal imports finish with definition checks, authorized
   proofs/trials and focused UI checks; report any unavailable prerequisites.

## 5. A new release

An agent already here: `cli new version KEY VERSION`, check the pins it was
given (a package that moved changes its name too), then `cli conform
KEY@VERSION ...` and `cli fidelity KEY@VERSION ...`. Set `default` to make it
the one a bare key names - ask first. These commands write the user copy,
preserving all companion files when the original entry was bundled.

## 6. Report - decision first

```
agent <key>: <offered | not yet>   route: <ACP itself | adapter pkg@ver | command>
destination: <personal catalogue folder | bundled contribution folder>
pins: <install, core>      relay: <n calls seen | empty: why>
proven: <port passed n/n, ...>      fidelity: <same | differs: fields | no CLI>
trial: <job name>  <n> units  <result>  <tokens, cost>
ours: <file: what it does, why the upstream could not>, ...   (or: none beyond agent.json)
```

Then one table row per thing that does not work or differs, with **skip**
(said in its summary) / **local change** (ours, said) / **issue** (the
agent's or adapter's repository) for each; the questions, with the question
tool. No account of what was read. Everything stays local: pull requests,
issues and anything upstream are asked about once it works, never by
default.

## Catalogue copy

Write the entry's summary as one short sentence, ideally under 100 characters.
Name what it is and its distinguishing purpose. Keep installation requirements,
protocol mechanics and limitations in their dedicated fields or documentation;
do not rely on the card's line clamp to shorten a paragraph.
