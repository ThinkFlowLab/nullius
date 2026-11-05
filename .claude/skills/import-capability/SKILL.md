---
name: import-capability
description: >-
  Bring a capability - a memory layer, a retriever, a component a benchmark
  calls - into the Nullius standard (docs/standard.md): install the real
  library or repository pinned, wrap it in a thin JSON-lines capability.py that
  calls it per its port, send its model calls through the relay, write
  capability.json, try it by hand, check it, prove its port, run a capability
  benchmark on a few units, and report decision first. Calls the library as
  published; never re-implements it. Use whenever someone wants to add,
  import, onboard, wrap, integrate or "plug in" a memory, a retriever or
  another capability, including a pasted "Add the capability NAME to Nullius.
  Source: LINK. Remark: TEXT." Not for an agent (import-agent), nor
  a benchmark (import-standard, import-benchmark).
---

# Import a capability

An assistant without skills reads this file directly and follows it as
written; "the question tool" is whatever multiple-choice prompt it has, else
a plain question that waits for the answer.

The user wants one thing: their memory (or other capability) offered in Nullius
against every benchmark of its kind. `cli` below is `nullius standard`;
`python -m nullius standard` also works with a Python that has Nullius
installed. The source form remains `PYTHONPATH=app python3 -m nullius.standard.cli`
from the checkout root. Jobs go to `jobs_standard/` relative to the working
directory, or `--jobs PATH`; keep them outside package resources. Read
`docs/standard.md` ("A capability", "Versions") when available in a checkout,
otherwise the installed schema and examples below. An installation needs no
`app/` tree.

## Locate the runtime and writable catalogue

Use `nullius paths --json` in the application's profile/environment.
`NULLIUS_HOME` selects `config/`, `data/` and `cache/` under a profile directory;
otherwise platform user paths apply. `NULLIUS_CATALOGUE` overrides the user
catalogue (Linux default: `~/.local/share/nullius/catalogue/`). Keep personal
configuration, secrets and mutable data outside installed package resources.

Use the interpreter behind `nullius` for Python API calls. A `uv tool` install
does not make Nullius importable by the shell's `python3`. For POSIX, check
`command -v nullius`, set `NULLIUS_PY="$(uv tool dir)/nullius/bin/python"`, and
verify `"$NULLIUS_PY" -c 'import sys, nullius; print(sys.executable); print(nullius.__file__)'`.
For another installation, inspect its launcher or virtual environment. For
source inspection, use `NULLIUS_PY=python3` with `PYTHONPATH="$PWD/app"` on the
command below from the checkout root:

```bash
"$NULLIUS_PY" - <<'PY'
from nullius import paths, settings
settings.load(paths.settings_file())
from nullius.standard import definitions, howto, ports
for module in (definitions, howto, ports):
    print(module.__name__, module.__file__)
print("catalogue:", paths.catalogue_dir())
PY
```

Read source references to `app/nullius/` at those resolved module paths
when installed. Path APIs return strings; use `os.path.join` or `Path(value)`.
In the same settings-loaded interpreter, `definitions.entry_dir("capability", key)`
locates the selected entry. Before editing it, use
`definitions.writable_dir("capability", key)` to copy the entire bundled folder
to the user catalogue. A user folder replaces its bundled counterpart in
full; keep its wrapper, JSON and other resources together. Never edit
`site-packages` or the private `_nullius_app` UI package.

Default to a personal import: no source checkout, repository tests, generated
docs or sprite edits are needed. For a requested bundled contribution,
author/prove in a user or scratch catalogue, then copy only the intended entry
files into `app/nullius/catalogue/capabilities/<key>/` and use the repository gates
below. CLI mutations protect bundled resources in source checkouts too.

## The rule: facilitator, not re-implementer

- **Call the real thing.** `capability.py` imports the capability's own library
  (or runs its own entry point) at the pinned version or commit, and does
  only what the port needs: turn a JSON line into its own `add` and search
  calls, and its result back into a line. Its storage, indexing, ranking,
  embeddings and prompts stay its own - nothing copied, rewritten or
  approximated.
- **Its dependencies are its own.** Installed by its `install` block into its
  own environment, pinned; never into the app (Flask only).
- **Ask before re-implementing.** If the library cannot be called for a step
  (no API for it, a service it needs that cannot run here, a prompt it keeps
  private) and our own version of that step seems needed, stop: say what,
  why, and what it changes about what is measured; ask with the question
  tool and write nothing of it before the yes. Without a way to ask (a
  subagent), report it as an open question and leave it unwritten.
- **A kind with no port yet** (only `cap.memory/v1` exists today; see
  `app/nullius/standard/ports.py`) is a generic change: a new port with a
  kit, worded for every capability of its kind. Ask before adding it; this is a
  source contribution, not a user catalogue edit to installed core code.

## 1. Read the input (minutes, no install)

The input is usually a name, a source link (a git repository, a PyPI or npm
page, a Hugging Face page) and a description. Find:

1. **Already here?** `cli list` reads bundled and user entries together. A
   new release of one already here is a version (step 5).
2. **The port** it fits, and its seam: the library's calls to store a
   session and to answer a question with recalled text.
3. **How it installs:** a pinned package (its latest release), or a
   repository at its latest release tag (recorded as `upstream.tag`, its
   commit in `tested`; a commit of the default branch only when it has no
   release or the user asks: `git ls-remote --tags`), its Python project folder, extras, Python version,
   packages from another index (a CPU torch build), hub models to fetch at
   install so a run needs no network. Never patch the library's bugs: keep
   its behaviour, say it in the report (a one-clause note in `summary` only
   when someone choosing it must know), ask when it blocks.
4. **Its model calls:** does it call a model (to extract, summarise,
   embed remotely)? Which variables set its endpoint, key and model?
5. **Licence.**

Say the route in one line: `<capability> -> <port>, installed from <package@ver
| repository@commit>, model: <none | through the relay>`.

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

## 2. Scaffold, wrap, fill in

1. `cli new capability KEY` writes `capabilities/KEY/capability.json` under the active
   user catalogue, and a `capability.py` that already speaks `cap.memory/v1`.
   Use the writable paths the command prints.
2. **Wrap** in `capability.py` (the shape `FEATURE_PY` in
   `app/nullius/standard/howto.py` shows): `add` gets a session `{id, date,
   turns, text}` and stores it with the library's own call, replying `{}`;
   `recall` gets the question and replies `{"memory": <text>}` from the
   library's own search; an error replies `{"error": ...}`. Send the
   library's own output to stderr: stdout carries only the JSON lines.
   **Everything it provides, and what it did.** Each setting the library
   offers that changes its behaviour (extraction on or off, embedder, search
   depth and threshold, reranking) becomes an option, its default the
   library's own. Install the optional pieces its full behaviour needs when it
   only warns without them (Mem0: keyword search, entity matching), pinned.
   Each reply also carries `trace`, what the library did: what it wrote, each
   hit with its scores, its own warnings (a failure it swallows shows only
   there). Mem0's entry is the example.
3. **Try it by hand** with two lines in, two out (`TRY_FEATURE` in
   `howto.py`), with the library installed in a throwaway environment
   (`uv run --no-project --with <pins> python /absolute/path/to/capability.py`),
   before Nullius is involved; never install the capability into Nullius's tool
   environment.
4. **`capability.json`** (`FEATURE_JSON`, and `FEATURE_FROM_REPO` for one
   installed from a repository): `upstream` (`package` and `version`, or
   `repository` and a full `tested` commit; `license`), `kind`, `install`
   (`pip` pinned as `name==version`, or `project`, `extras`, `python`,
   `indexes`, `prefetch`, `wheels` for a wheel served only by URL, such as a
   spaCy model, pinned by its sha256), `start.command` running the wrapper in that
   environment (`{install}`, `{here}`), `env` for its own variables, and a
   `model` block (`apis`, `env` with `{base_url}`, `{key}`, `{model}` under
   the names it reads) when it calls a model, so those calls reach the relay.
   `options` as an agent's, the agents' settings (`reasoning_effort`) among
   them when it calls a model. Leave `offers` and `proven` empty.
5. `cli check` - no problems.

## 3. Prove

1. `cli conform KEY --model <cheap model>` - its port's kit (a capability needs
   no `--unsandboxed`; `--backend docker` runs it in a container).
2. When it calls a model: the kit trial's `agent/calls.jsonl` is not empty.
   Empty means it went around the relay: point its own config at the relay,
   never patch the library.
3. A capability benchmark: `cli list` shows which benchmarks need its port; run
   one with `cli run BENCHMARK KEY --model <cheap model> --limit 10`, or
   fewer. A trial shows the pipe works; it does not measure.
4. Read every failure before believing it: the capability's, the wrapper's, the
   kit's or the definition's. Fix only what is ours; an upstream bug is an
   issue for its repository, asked about first.
5. `cli check` and the focused UI check below complete a personal import,
   together with the authorized proofs/trial. Report unavailable prerequisites
   without claiming a proof.
6. **Bundled contributions only:** from the checkout root, refresh
   `python3 app/tests/catalogue_docs.py --write`, run `python3 app/tests/parallel.py`,
   the route suite (`uv run --no-project --with-requirements app/requirements.txt
   python app/tests/parallel.py app/ui_tests`), and
   `python3 .claude/skills/docs-drift/scripts/check_docs.py`. Use an isolated
   profile with an empty user catalogue so personal overrides cannot mask the
   contributed files. These repository gates do not apply to personal entries.

## 4. Open it

`nullius serve /absolute/path/to/jobs_standard --no-debug`, with the same
profile/catalogue: the capability shows on the Capabilities tab with what it proved,
on the builder's Memory (or its kind's) step, and the trial page shows what was
recalled for each question. From source, use `uv run --no-project
--with-requirements app/requirements.txt app/app.py /absolute/path/to/jobs_standard
--no-debug`. Check these pages in the browser.

## 5. A new release

`cli new version KEY VERSION`, check the pins it was given, then `cli
conform KEY@VERSION --model <cheap model>`. Set `default` to make it the one
a bare key names - ask first. The commands write the user copy, preserving
companion files when the original entry was bundled.

## 6. Report - decision first

```
capability <key>: <offered | not yet>   port: <port>   from: <package@ver | repository@commit>
destination: <personal catalogue folder | bundled contribution folder>
proven: <port passed n/n>      relay: <n calls | no model | empty: why>
trial: <job name>  <n> units  <result>  <tokens, cost>
ours: <capability.py: the add/recall glue>, <any other file: what, why the library could not>
```

Then one table row per thing that does not work or differs, with **skip**
(said in its summary) / **local change** (ours, said) / **issue** (the
capability's repository) for each; the questions, with the question tool. No
account of what was read. Everything stays local: pull requests, issues and
anything upstream are asked about once it works, never by default.

## Catalogue copy

Write the entry's summary as one short sentence, ideally under 100 characters.
Name what it is and its distinguishing purpose. Keep installation requirements,
protocol mechanics and limitations in their dedicated fields or documentation;
do not rely on the card's line clamp to shorten a paragraph.
