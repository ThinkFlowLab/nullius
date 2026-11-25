"""The standard from a shell.

    PYTHONPATH=app python3 -m nullius.standard.cli list
    PYTHONPATH=app python3 -m nullius.standard.cli check [PATH]
    PYTHONPATH=app python3 -m nullius.standard.cli fit BENCHMARK[@VERSION]
    PYTHONPATH=app python3 -m nullius.standard.cli pins BENCHMARK[@VERSION] [--tag TAG] [--limit N]
    PYTHONPATH=app python3 -m nullius.standard.cli new benchmark|agent|capability KEY [--family F] [--port P]
    PYTHONPATH=app python3 -m nullius.standard.cli new version KEY VERSION
    PYTHONPATH=app python3 -m nullius.standard.cli conform AGENT|CAPABILITY --model M [--provider P] [--port PORT ...] [--attempts K] [--option NAME=VALUE ...] [--unsandboxed | --backend container | --backend boat]
    PYTHONPATH=app python3 -m nullius.standard.cli run BENCHMARK[@VERSION] AGENT|CAPABILITY --model M [--provider P] [--units FILE] [--limit N] [-j N] [--option NAME=VALUE ...] [--bench-option NAME=VALUE ...] [--price MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H] ...] [--max-usd USD] [--max-usd-per-unit USD] [--timeout PHASE=SECONDS ...] [--max-turns N] [--max-calls N] [--constraint KEY=VALUE ...] [--served-by [MODEL=]SLUG[,SLUG...] ...] [--exchanges whole|text|off] [--exchanges-max-mb N] [--unsandboxed | --backend container | --backend boat [--boat-size SIZE]]
    PYTHONPATH=app python3 -m nullius.standard.cli run BENCHMARK[@VERSION] nop|oracle [--dataset D] [--units FILE] [--limit N]
    PYTHONPATH=app python3 -m nullius.standard.cli prove BENCHMARK[@VERSION] [--dataset D] [--limit N] [--bench-option NAME=VALUE ...] [--job nop|oracle=JOB_DIR ...]
    PYTHONPATH=app python3 -m nullius.standard.cli run ... --shard I/N [--launch ID]
    PYTHONPATH=app python3 -m nullius.standard.cli merge JOB_DIR... -o OUT [--force]
    PYTHONPATH=app python3 -m nullius.standard.cli fidelity AGENT --model M [--provider P] [--option NAME=VALUE ...] [--passes N] --unsandboxed
    PYTHONPATH=app python3 -m nullius.standard.cli replay JOB_DIR TRIAL --call N --times K --max-usd USD [changes] [--read CMD] [-j N] [--dry-run] [--out DIR] [--json]
    PYTHONPATH=app python3 -m nullius.standard.cli replay JOB_DIR TRIAL --list [--read CMD] [--json]
    PYTHONPATH=app python3 -m nullius.standard.cli study DIR --times K --max-usd USD [--targets FILE] [--job JOB_DIR --trial TRIAL ... [--every N] [--where REGEX]] [--arm NAME=CHANGES ...] [--read CMD] [--yes REGEX] [--compare A B] [-j N] [--stop-after N] [--dry-run] [--json]
    PYTHONPATH=app python3 -m nullius.standard.cli recount DIR [--read CMD] [--yes REGEX] [--compare A B] [--json]

An agent runs commands as you on the local backend, which is not a sandbox:
an agent's run or conformance check there is refused without ``--unsandboxed``.
``--backend container`` (``docker`` alias) runs each unit's agent or capability in a container whose
only way out is the unit's relay, or a public web proxy when its environment
declares ``network`` (``standard/backends/container.py``), and needs no such yes.
``--backend boat`` runs each unit's agent or capability in a boat.dev VM of its
own, whose only way out is a tunnel to the unit's relay on this machine
(``standard/backends/boat.py``); its API key is Settings → Sandbox's, or ``BOAT_API_KEY``.
A benchmark's control (``nop``, ``oracle``: its ``controls``) runs no agent
and calls no model - only a judged benchmark's judge - so it asks for neither.
``prove`` runs a benchmark's controls and checks its units and that nothing
leaks, and writes what passed into its ``proven`` (``standard/proofs/prove.py``).

A benchmark, an agent or a capability is named by its key (its default
version) or ``key@version``. ``list`` shows every definition - each version - and which
pairs fit; ``check`` checks the
definitions (or one file); ``new`` writes a definition to fill in, or a new
version of an agent or a capability (``new version``: the default's pins, with
the version replaced, and nothing proven); ``fit`` says, for one benchmark, what can run it and
why the rest cannot; ``conform`` runs the kits and writes what passed into
``proofs.json`` - the port, and each skill of its kit shown or not (``--attempts K``:
each check K times, held by a majority); ``run`` runs a benchmark into a job under ``--jobs`` (``jobs_standard/``);
``fidelity`` drives an agent over
ACP and through its own command, compares the requests its model was sent, and
writes the verdict into ``proofs.json`` (``standard/proofs/fidelity.py``); ``replay`` sends one
recorded call of a trial again K times under a spending cap, as it was or changed, and
counts the answers (``standard/runner/replay.py``); ``study`` does it for calls of several trials under named arms of
changes, one cap for all, and compares the arms call by call, and ``recount`` reads a kept study or replay
again by another rule (``standard/runner/study.py``). ``--shard I/N`` runs one
shard of the dataset's units (``standard/runner/shard.py``: by the unit id's hash, then
``--limit``), the job recording its shard and its launch (``--launch``); ``--units FILE``
runs only the units a file lists (``run.units_file``: one id a line), read on
the machine the command runs on, ``--from-run`` or not; ``merge``
puts a launch's shard jobs together into one job (``standard/runner/merge.py``). ``--option``
sets one of the agent's or capability's options (``options`` in its definition);
the rest keep the vendor's defaults, and the job's config records them all.
``--served-by [MODEL=]SLUG[,SLUG...]`` lets only those serving providers answer
a model on OpenRouter, in that order (``run.served_by``); each call's record
names the provider that served it. ``--exchanges`` says what each unit's relay
keeps of its agent's calls (``standard/calls/exchanges.py``): ``whole`` by default,
``text`` without the pictures' bytes, or ``off``; ``--exchanges-max-mb`` bounds
a unit's.
A run says each stage before its first unit, and each unit's end, as one
``[nullius] stage=...`` line on stderr and in the job's ``progress.jsonl``
(``nullius/progress.py``).

Keys come from the process environment, then the saved user settings. Nothing is printed of them. With ``--from-run`` (a run
the builder or a page started, ``standard/pages/recipe.py``) saved settings are not
read: the key is the one the run was handed, ``OPENAI_API_KEY``, and the
endpoint ``OPENAI_BASE_URL`` when it names one (``run_env``). The licences a
benchmark's environments declare are never in either: the run reads them from
the benchmark's store (``nullius.secrets``) on this machine, or, on a host, from
the one line its launcher handed it (``licences``). A data provider's token the
benchmark declares is the exception: a run without ``--from-run`` takes the
provider's variable (``HF_TOKEN``) from the environment first.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import tempfile
import time

from nullius.standard.runner.retries import MAX_RETRIES, RETRIES
from nullius import constraints as limits, paths, prices as price_list, progress, providers, secrets, settings
# The legacy module command needs saved path overrides before install.HOME
# and other runtime defaults are computed.
if __name__ == "__main__" and "--from-run" not in sys.argv[1:]:
    settings.load(paths.settings_file())

from nullius.standard.backends import boat
from nullius.standard.calls import exchanges
from nullius.standard.contract import definitions, match, ports, proofs
from nullius.standard.proofs import conform, fidelity, pins, prove
from nullius.standard.runner import job, merge, run, shard

ENV_FILE = paths.settings_file()


def _all():
    got, bad = {}, []
    for kind in definitions.KINDS:
        got[kind], b = definitions.load(kind, every=True)
        bad += b
    return got, bad


def _other(key):
    o = definitions.get("agent", key) or definitions.get("capability", key)
    if not o:
        sys.exit(f"no agent or capability {key!r}: {[d['_ref'] for k in ('agent', 'capability') for d in definitions.load(k, every=True)[0]]}")
    return o


def run_env(provider: str, env=os.environ) -> dict:
    """The environment a run started by Nullius reads its provider from:
    ``execution``'s launcher hands every run its one key as ``OPENAI_API_KEY``
    and its endpoint as ``OPENAI_BASE_URL`` (empty for OpenAI's own), so they
    are named here as the provider's own settings and nothing else is - but
    the Boat key, handed the same way to a run on Boat: on a host, the only
    way it has one."""
    out = {k: v for k, v in env.items() if k in run.KEEP or k == boat.NAME}
    if env.get("OPENAI_API_KEY"):
        out[providers.key_setting(provider)] = env["OPENAI_API_KEY"]
    if provider == "custom" and env.get("OPENAI_BASE_URL"):
        out["MODEL_API_URL"] = env["OPENAI_BASE_URL"]
    return out


def licences(bench: dict, env=os.environ, from_run: bool = False):
    """(``run.Licences``, the folder to remove after) for a run on a host,
    whose launcher handed it the benchmark's licences as one line
    (``secrets.HANDED``, ``execution``): a licence file is written to
    ``NULLIUS_LICENCE_DIR`` - in memory, ``/dev/shm`` - and nowhere else.
    The line leaves this process's environment once read. On this machine
    the run reads the benchmark's own store; a headless run (not
    ``from_run``) first takes a data provider's token the benchmark declares
    from ``env``, under the provider's variable (``HF_TOKEN``)."""
    line = env.pop(secrets.HANDED, None)
    if not line:
        return (None if from_run else run.Licences(bench, environ=env)), None
    shm = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
    spill = env.get("NULLIUS_LICENCE_DIR") or os.path.join(shm, f"nullius-{os.getpid()}")
    return run.Licences.handed(bench, secrets.unpack(line), spill), spill


def older(d: dict, ports_=None) -> list:
    """``agent.tools/v1 proven on kit v1; kit v2 is current``, for each port
    ``d`` offers (of ``ports_``, when given) on a proof older than its kit."""
    proven = d.get("proven") or {}
    out = [(p, definitions.older_kit(p, proven.get(p))) for p in d.get("offers") or [] if ports_ is None or p in ports_]
    return [f"{p} {note}" for p, note in out if note]


def skills(d: dict, ports_=None) -> list:
    """``agent.tools/v1 skills: decoys ✗ · chain ✓ · retry ✗``, for each port
    ``d`` offers (of ``ports_``, when given) whose kit has skills."""
    from nullius.standard.pages.recipe import skills_of
    proven, out = d.get("proven") or {}, []
    for p in d.get("offers") or []:
        got = skills_of(p, proven.get(p)) if ports_ is None or p in ports_ else []
        if got:
            out.append(f"{p} skills: " + " · ".join(f"{s['id']} {'✓' if s['shown'] else '✗'}" for s in got))
    return out


def cmd_pins(a):
    if a.limit is not None and a.limit < 1:
        print("--limit must be a positive whole number", file=sys.stderr)
        return 2
    if a.tag is not None and not pins.TAG.fullmatch(a.tag):
        print("invalid image tag", file=sys.stderr)
        return 2
    bench = definitions.get("benchmark", a.benchmark)
    if bench is None:
        print("no such benchmark", file=sys.stderr)
        return 2
    try:
        refs = pins.references(bench["_here"])
    except (OSError, ValueError, SyntaxError):
        print("not checked: cannot read image pins from images.json or driver.py", file=sys.stderr)
        return 2
    if not refs:
        print("not checked: no image pins found", file=sys.stderr)
        return 2
    code = 0
    for name, ref in refs[:a.limit]:
        state, detail = pins.check(ref, a.tag)
        print(f"{name}: {state}: {detail}", flush=True)
        code = max(code, 2 if state == "not checked" else 0 if state == "same" else 1)
    return code


def cmd_list(a):
    got, bad = _all()
    for kind in definitions.KINDS:
        print(definitions.KINDS[kind]["dir"] + ":")
        for d in got[kind]:
            side = d.get("needs") if kind == "benchmark" else d.get("offers")
            print(f"  {d.get('_ref') or d['key']:<18} {('needs ' if kind == 'benchmark' else 'offers ') + ', '.join(side or []) or 'nothing proven yet'}")
            up = d.get("upstream") or {}
            if up.get("repository"):
                print(f"  {'':<18}   upstream {up['repository'].removeprefix('https://')} @ "
                      + (f"{up['tag']} ({str(up.get('tested', ''))[:12]})" if up.get("tag") else str(up.get("tested", ""))[:12]))
            if kind == "benchmark" and d.get("needs_skills"):
                print(f"  {'':<18}   and the skills " + "; ".join(f"{', '.join(s)} of {p}" for p, s in d["needs_skills"].items()))
            for note in ([] if kind == "benchmark" else older(d) + skills(d)):
                print(f"  {'':<18}   {note}")
    t = match.table(got["benchmark"], got["agent"], got["capability"])
    print("pairs:")
    for b, row in t.items():
        print(f"  {b}: " + ", ".join(f"{o} {'fits' if not why else 'no'}" for o, why in row.items()))
    for b in bad:
        print("problem:", b)
    return 1 if bad else 0


def catalogue_problems(benches) -> list:
    """A standard benchmark is listed by the catalogue only through its entry:
    benchmark.json beside it, naming the standard among its harnesses, and the
    whole catalogue consistent (nullius.catalogue.catalog.validate)."""
    from nullius.catalogue import catalog
    bad = []
    for b in benches:
        entry = os.path.join(b["_here"], "benchmark.json")
        if not os.path.isfile(entry):
            bad.append(f"benchmark {b['key']}: no benchmark.json beside standard.json (the catalogue's entry)")
            continue
        with open(entry) as f:
            if "standard" not in (json.load(f).get("harness") or []):
                bad.append(f"benchmark {b['key']}: benchmark.json does not name 'standard' in harness")
    # each benchmark's settings inventory, against every version of it (definitions.settings_problems)
    every = definitions.load("benchmark", every=True)[0]
    for key in dict.fromkeys(b["key"] for b in benches):
        bad += [f"benchmark {key}: {p}" for p in definitions.settings_problems([v for v in every if v["key"] == key])]
    bad += [f"catalogue: {p}" for p in catalog.validate(catalog.load())]
    return bad


def cmd_check(a):
    if a.path:
        with open(a.path) as f:
            d = definitions.names.definition(json.load(f))
        kind = next((k for k, v in definitions.KINDS.items() if v["schema"] == d.get("schema")), None)
        if not kind:
            print(f"schema: one of {[v['schema'] for v in definitions.KINDS.values()]}")
            return 1
        try:
            d = definitions.read_document(a.path, kind)
            bad = definitions.problems(dict(d, _here=os.path.dirname(os.path.abspath(a.path))), kind)
        except (OSError, ValueError) as e:
            bad = [str(e)]
    else:
        got, bad = _all()
        for k in definitions.kits():
            bad += [f"kit {k['key']}: {p}" for p in definitions.problems(k, "benchmark")]
        bad += catalogue_problems(got["benchmark"])
    for b in bad:
        print(b)
    print("ok" if not bad else f"{len(bad)} problem(s)")
    return 1 if bad else 0


def cmd_fit(a):
    b = definitions.get("benchmark", a.benchmark)
    if not b:
        sys.exit(f"no benchmark {a.benchmark!r}")
    print(f"{b['label']} ({b['family']} benchmark) needs: " + ", ".join(b["needs"]))
    for p in b["needs"]:
        print(f"  {p}: {ports.PORTS[p]['summary']}")
        have = definitions.port_skills(p)
        for s in (b.get("needs_skills") or {}).get(p) or []:
            print(f"    and its {s} skill: {have.get(s, '')}")
    others = definitions.load("agent" if b["family"] == "agent" else "capability", every=True)[0]
    for o in others:
        why = match.fit(b, o)
        notes = [] if why else older(o, b["needs"]) + skills(o, b["needs"])
        print(f"  {'✓' if not why else '✗'} {o['_ref']}" + ("" if not why else ": " + "; ".join(why))
              + ("" if not notes else " (" + "; ".join(notes) + ")"))
    return 0


TEMPLATES = {
    "benchmark": {
        "standard.json": {
            "schema": "nullius.benchmark/v1", "key": "<KEY>", "label": "<KEY>", "summary": "SAY WHAT ONE UNIT IS AND HOW IT IS SCORED.",
            "family": "{family}", "needs": ["{port}"],
            "upstream": {"repository": "https://github.com/OWNER/REPO", "tested": "FULL 40-CHARACTER COMMIT", "license": "LICENSE"},
            "driver": {"command": ["python3", "{here}/driver.py"]},
            "datasets": [{"id": "main", "label": "main", "path": "PATH/IN/THE/UPSTREAM", "sha256": "SHA256 OF THE FILE", "units": 1}],
            "units": {"noun": "task", "nouns": "tasks"},
            "score": {"kind": "pass", "says": "WHAT A REWARD OF 1 MEANS"},
        },
        "driver.py": '''"""{key}'s driver for the Nullius standard: JSON lines on stdin and stdout.

$NULLIUS_DATASET is the dataset file (checked by its sha256), $NULLIUS_UPSTREAM
the upstream at its tested commit. Each request is one line, {{"op": ...}};
each reply one line, a JSON object, or {{"error": "..."}}.
"""
import json
import os
import sys


def units(dataset):
    return {{"units": []}}                                   # every unit's id, in order


def setup(unit, dir):
    # fill dir, the unit's workspace; return the prompt (agent family) or
    # {{"memory": {{"add": [...], "recall": [...]}}}} (a memory capability)
    return {{"prompt": ""}}
{extra}

def score(unit, dir, transcript, answer):
    return {{"reward": 0.0, "detail": {{}}}}                  # 1.0 is a pass; a score benchmark gives 0..1


HANDLERS = {{"units": units, "setup": setup, "score": score{handlers}}}
for line in sys.stdin:
    try:
        req = json.loads(line)
        out = HANDLERS[req.pop("op")](**req)
    except Exception as e:
        out = {{"error": f"{{type(e).__name__}}: {{e}}"}}
    print(json.dumps(out), flush=True)
''',
    },
    "agent": {
        "agent.json": {
            "schema": "nullius.agent/v1", "key": "<KEY>", "label": "<KEY>", "summary": "SAY WHAT THE AGENT IS AND HOW IT IS DRIVEN.",
            "upstream": {"package": "PACKAGE", "version": "VERSION", "license": "LICENSE"},
            "install": {"npm": ["PACKAGE@VERSION"]},
            "start": {"acp": ["{install}/node_modules/.bin/PROGRAM"]},
            "model": {"apis": ["chat"], "env": {"OPENAI_BASE_URL": "{base_url}", "OPENAI_API_KEY": "{key}", "MODEL": "{model}"}},
        },
    },
    "capability": {
        "capability.json": {
            "schema": "nullius.capability/v1", "key": "<KEY>", "label": "<KEY>", "summary": "SAY WHAT THE CAPABILITY KEEPS AND WHAT IT GIVES BACK.",
            "kind": "memory", "start": {"command": ["python3", "{here}/capability.py"]},
        },
        "capability.py": '''"""{key}: cap.memory/v1 in JSON lines.

add {{session}}      keep a session: {{id, date, turns: [{{role, text}}], text}}
recall {{question}}  -> {{"memory": "what was kept that bears on the question"}}
"""
import json
import sys

for line in sys.stdin:
    req = json.loads(line)
    try:
        if req["op"] == "add":
            out = {{}}
        elif req["op"] == "recall":
            out = {{"memory": ""}}
        else:
            out = {{"error": "unknown op " + req["op"]}}
    except Exception as e:
        out = {{"error": f"{{type(e).__name__}}: {{e}}"}}
    print(json.dumps(out), flush=True)
''',
    },
}
PRODUCE = '''

def produce(unit, dir, recalled, model, base_url, key):
    # a capability benchmark's own step: answer from what the capability recalled,
    # calling the model at base_url (the relay) with key (a placeholder)
    return {{"answer": ""}}
'''


def new_version(key: str, version: str) -> str:
    """Add ``version`` to an agent's or a capability's file: a copy of its default
    version's block with the old version replaced in its pins, nothing proven.
    A file without ``versions`` is turned into one first, its one version kept
    as it was. Existing evidence stays in ``proofs.json``; the new version has
    none. Returns the definition's path."""
    if not definitions._KEY.match(key):
        sys.exit(f"no agent, capability or benchmark {key!r}")
    kind = next((k for k in ("agent", "capability", "benchmark")
                 if (folder := definitions.entry_dir(k, key))
                 and os.path.isfile(definitions.definition_path(folder, k))), None)
    if not kind:
        sys.exit(f"no agent, capability or benchmark {key!r}")
    if not definitions._VERSION.match(version):
        sys.exit("a version is letters, digits and . + _ -")
    source = definitions.definition_path(definitions.entry_dir(kind, key), kind)

    def check(d):
        if "versions" not in d and not (d.get("upstream") or {}).get("version"):
            sys.exit(f"{source}: upstream.version is not set, so its one version has no name")
        if version in definitions.versions(d):
            sys.exit(f"{key} has a version {version} already")

    check(definitions.read_document(source, kind))
    folder = definitions.writable_dir(kind, key)
    path = definitions.definition_path(folder, kind)
    with definitions._locked(path):
        d = definitions.read_document(path, kind)
        check(d)
        evidence = proofs.document(d, kind)
        d = proofs.strip(d)
        if "versions" not in d:
            up = dict(d.get("upstream") or {})
            old = str(up.pop("version"))
            # A benchmark shares its execution settings until an author says what differs.
            block = {} if kind == "benchmark" else {"install": d.pop("install", {})}
            d["upstream"] = up
            d["default"], d["versions"] = old, {old: block}
        old = d["default"]
        copy = json.loads(json.dumps(d["versions"][old]).replace(old, version))
        d["versions"] = {version: copy, **d["versions"]}
        # Old-version evidence is valid for both manifests; publish it first so
        # interruption cannot discard proofs while stripping their legacy fields.
        proofs.write(folder, evidence)
        with open(path + ".part", "w") as f:
            json.dump(d, f, indent=2)
            f.write("\n")
        os.replace(path + ".part", path)
    print(f"wrote {path}: {key}@{version} beside {', '.join(v for v in d['versions'] if v != version)} (default still {old})")
    if kind == "benchmark":
        print(f"next: say what differs in it (upstream, datasets, driver, score, limits, controls, environments), then: "
              f"cli check, and cli prove {key}@{version} --limit 3; make it the default by setting \"default\": \"{version}\"")
        return path
    print(f"next: check its pins, then: cli check, and cli conform {key}@{version} --model gpt-5-mini"
          + (" --unsandboxed" if kind == "agent" else "") + f"; make it the default by setting \"default\": \"{version}\"")
    return path


def cmd_new(a):
    """A definition to fill in, in the catalogue folder it belongs in."""
    if a.kind == "version":
        if not a.version:
            sys.exit("new version KEY VERSION")
        new_version(a.key, a.version)
        return 0
    kind, key = a.kind, a.key
    if not definitions._KEY.match(key):
        sys.exit("a key is lower case letters, digits and dashes")
    name = definitions.KINDS[kind]["file"]
    selected_dir = definitions.entry_dir(kind, key)
    selected = os.path.join(selected_dir, name) if selected_dir else None
    if selected and os.path.exists(selected):
        sys.exit(f"{selected} exists already")
    folder = definitions.writable_dir(kind, key)
    os.makedirs(folder, exist_ok=True)
    family = a.family or "agent"
    port = a.port or ("agent.task/v1" if family == "agent" else "cap.memory/v1")
    for fname, body in TEMPLATES[kind].items():
        if isinstance(body, dict):
            text = json.dumps(body, indent=2).replace('"<KEY>"', json.dumps(key)).replace('"{family}"', json.dumps(family)) \
                .replace('"{port}"', json.dumps(port))
        else:
            feat = family == "capability"
            text = body.format(key=key, extra=PRODUCE.format() if feat else "", handlers=', "produce": produce' if feat else "")
        path = os.path.join(folder, fname)
        if os.path.exists(path):
            print("kept", path)
            continue
        with open(path, "w") as f:
            f.write(text + ("\n" if not text.endswith("\n") else ""))
        print("wrote", path)
    if kind == "benchmark":
        then = f"cli run {key} reference --model gpt-5-mini --limit 2 --unsandboxed"
    else:
        then = f"cli conform {key} --model gpt-5-mini" + (" --unsandboxed" if kind == "agent" else "")
    print(f"next: fill in the CAPITALS{', add benchmark.json beside it (the catalogue entry)' if kind == 'benchmark' else ''}, "
          f"then: cli check, and {then}")
    return 0


def options(pairs, flag: str = "--option", form: str = "NAME=VALUE") -> dict:
    """``--option name=value``, repeatable, as {name: value}."""
    out = {}
    for p in pairs or []:
        name, eq, value = p.partition("=")
        if not eq or not name:
            sys.exit(f"{flag} {p!r}: {form}")
        out[name.strip()] = value.strip()
    return out


def timeouts(pairs) -> dict:
    """``--timeout PHASE=SECONDS``, repeatable, as {phase: seconds}; ``run`` checks the phases and the values."""
    return options(pairs, "--timeout", "PHASE=SECONDS")


def constraint_values(pairs) -> dict:
    """``--constraint KEY=VALUE``, repeatable: what the machine allows the run (``nullius.constraints``)."""
    out = {}
    for p in pairs or []:
        key, eq, value = p.partition("=")
        if not eq or key not in limits.BY_KEY:
            sys.exit(f"--constraint {p!r}: KEY=VALUE, KEY one of {', '.join(limits.BY_KEY)}")
        try:
            out[key] = limits.cli_value(key, value)
        except ValueError:
            sys.exit(f"--constraint {p!r}: not a value of {key}")
    return out


def prices(given) -> dict:
    """``--price MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]``, repeatable: this run's own prices, over the user's list."""
    out = {}
    for p in given or []:
        try:
            name, row = price_list.parse(p)
        except ValueError as e:
            sys.exit(str(e))
        out[name] = row
    return out


def _retries(value: str) -> int:
    """``--retries``: a whole number from 0 to ``MAX_RETRIES``."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number")
    if not 0 <= n <= MAX_RETRIES:
        raise argparse.ArgumentTypeError(f"{n} is not between 0 and {MAX_RETRIES}")
    return n


def cmd_conform(a):
    o = _other(a.other)
    if a.max_usd_per_unit is not None:
        try:
            price_list.update()
        except ValueError as e:
            print("refused: capped conformance needs current model prices:", e)
            return 2
    try:
        got = conform.check(o, model=a.model, provider=a.provider, which=a.port or None, jobs_root=a.jobs, say=print,
                            unsandboxed=a.unsandboxed, stamp=a.stamp, backend=a.backend, env=run_env(a.provider) if a.from_run else None,
                            options=options(a.option), repeat=a.repeat, fixture_image=a.fixture_image,
                            max_usd_per_unit=a.max_usd_per_unit, max_calls=a.max_calls)
    except run.Refused as e:
        print("refused:", e)
        return 2
    return 0 if all(r["ok"] for r in got.values()) else 1


def _bench(key):
    """A benchmark of the catalogue, or a conformance kit by its key."""
    b = definitions.get("benchmark", key) or next((k for k in definitions.kits() if k["key"] == key), None)
    if not b:
        sys.exit(f"no benchmark {key!r}")
    return b


def cmd_run(a):
    b = _bench(a.benchmark)
    if a.other in definitions.CONTROLS:
        other = definitions.control(b, a.other)
        if other is None:
            sys.exit(f"{b['label']} declares no {a.other} control (controls in its standard.json)")
    elif a.other in definitions.SETUPS:
        other, why = definitions.setup(b, a.other, a.model or "", a.provider, options(a.bench_option).get("agent", ""))
        if other is None:
            print("refused:", why)
            return 2
    elif not a.model:
        sys.exit("--model: the model the agent or the capability is run with")
    else:
        other = _other(a.other)
    given = constraint_values(a.constraint)
    why = "; ".join(limits.problems(given))
    if not why:
        try:
            limits.confine(given)                # before any thread: every process this one starts inherits it
        except OSError as e:
            why = f"cpu_set: {e.strerror or e}"
    if why:
        print("refused: constraints:", why)
        return 2
    if a.backend == "boat" and definitions.boots_vms(b):
        return _boat_units(a, b, other)
    # a cap counts each call at its model's price: the public list fetched first (Settings, Model prices); a machine
    # that cannot reach it runs uncapped where a model has no price, and says so (run.Run)
    unreachable = None
    if a.max_usd is not None or a.max_usd_per_unit is not None:
        try:
            price_list.update()
        except ValueError as e:
            unreachable = str(e)
    held, spill = licences(b, from_run=a.from_run)
    try:
        job, failed = run.run_and_report(b, other, model=a.model or "", provider=a.provider, dataset=a.dataset, limit=a.limit,
                      concurrency=a.concurrency, jobs_root=a.jobs, name=a.name, unsandboxed=a.unsandboxed, judge_model=a.judge_model, backend=a.backend,
                      boat_size=a.boat_size, relay_container=a.relay_container, simulator_model=a.simulator_model, options=options(a.option), shard=_shard(a.shard), launch=a.launch,
                      shard_by=a.shard_by, licences=held, bench_options=options(a.bench_option), repeat=a.repeat, retries=a.retries,
                      remove_images=a.remove_images,
                      prices=prices(a.price), max_usd=a.max_usd, max_usd_per_unit=a.max_usd_per_unit, prices_unreachable=unreachable,
                      constraints=given,
                      timeouts=timeouts(a.timeout), max_turns=a.max_turns, max_calls=a.max_calls,
                      exchanges=a.exchanges, exchanges_max_mb=a.exchanges_max_mb,
                      **({"units_file": run.units_file(a.units)} if a.units is not None else {}),
                      **({"served_by": run.served_by(a.served_by, a.model or "")} if a.served_by else {}),
                      **({"env": run_env(a.provider)} if a.from_run else {}))
    except run.Refused as e:
        print("refused:", e)
        return 2
    finally:
        if spill:
            shutil.rmtree(spill, ignore_errors=True)
    print(job)
    # no unit reached a verdict (a refusing endpoint, every unit an error): the run failed, and says so
    # by its exit status, so a launch of several agents pauses rather than running the next into the same error.
    # A run its spending cap stopped did what was asked of it: no failure
    return 3 if failed else 0


def boat_credentials(a, bench, other, vals) -> dict:
    """Only the selected provider and declared benchmark licences cross SSH stdin."""
    import base64
    needs = definitions.option_needs(bench, vals)
    out = {}
    if other["_kind"] != "control" or (bench.get("score") or {}).get("judge") or needs["embeddings"] or needs["reranker"]:
        base, key = run.provider_endpoint(a.provider, run_env(a.provider) if a.from_run else os.environ)
        out.update(OPENAI_API_KEY=key, OPENAI_BASE_URL=base)
    held, spill = licences(bench, from_run=a.from_run)
    try:
        held = held or run.Licences(bench)
        values = {}
        for name in held.kinds:
            item = held.get(name)
            if item:
                if item["kind"] == "file":
                    with open(item["path"], "rb") as f:
                        value = f.read()
                else:
                    value = item["value"].encode()
                values[name] = base64.b64encode(value).decode()
        if values:
            out[secrets.HANDED] = base64.b64encode(json.dumps(values).encode()).decode()
    finally:
        if spill:
            shutil.rmtree(spill, ignore_errors=True)
    return out


def _boat_units(a, b, other) -> int:
    """A benchmark whose units boot VMs of their own, on Boat: each unit whole in a Boat VM (``boat_units``)."""
    from nullius.standard.runner import boat_units
    if a.units or a.shard:
        print("refused: whole-task Boat runs do not accept --units or --shard; choose a dataset and --limit")
        return 2
    ds = a.dataset or b["datasets"][0]["id"]
    vals, bad = definitions.bench_options_of(b, ds, options(a.bench_option),
                                            a.other if other["_kind"] == "upstream" else "yours",
                                            kind=other["_kind"])
    if bad:
        print("refused:", "; ".join(bad))
        return 2
    words = [b["key"] if "_version" not in b else definitions.ref_of(b), a.other, "--provider", a.provider,
             "--dataset", ds, "--attempts", str(a.repeat), "--retries", str(a.retries)]
    for field in ("model", "judge_model", "simulator_model", "max_usd_per_unit", "max_turns", "max_calls"):
        value = getattr(a, field)
        if value is not None:
            words += ["--" + field.replace("_", "-"), str(value)]
    for field in ("bench_option", "option", "timeout", "constraint", "served_by", "price"):
        for value in getattr(a, field) or []:
            words += ["--" + field.replace("_", "-"), value]
    # Saved price overrides belong to the launching profile, not the fresh VM.
    needs = definitions.option_needs(b, vals)
    models = [a.model, other.get("sent"), a.judge_model or providers.served_as((b.get("score", {}).get("judge") or {}).get("model", ""), a.provider),
              a.simulator_model or providers.served_as((b.get("simulator") or {}).get("model", ""), a.provider),
              *[providers.served_as(m, a.provider) for m in needs["embeddings"] + needs["reranker"]]]
    explicit = prices(a.price)
    for model, row in price_list.given(models, price_list.table()).items():
        if model not in explicit:
            words += ["--price", model + "=" + ",".join(str(row.get(k, "")) for k in price_list.RATES).rstrip(",")]
    if a.remove_images:
        words.append("--remove-images")
    try:
        _, wanted = boat_units.plan(b, ds, a.limit)
        if a.max_usd is not None:
            if not 0 < a.max_usd < float("inf"):
                raise boat_units.Refused("--max-usd must be finite and above zero")
            # Independent VMs cannot share a ledger: each gets an equal, bounded share.
            words += ["--max-usd", str(a.max_usd / len(wanted))]
        creds = boat_credentials(a, b, other, vals)
    except (run.Refused, boat_units.Refused) as e:
        print("refused:", e)
        return 2
    stamp = time.strftime("%Y-%m-%d__%H-%M-%S")
    name = a.name or f"{stamp}__{definitions.job_word(b)}__{definitions.job_word(other)}"
    try:
        job_dir, failed = boat_units.run(b, words, dataset=a.dataset or b["datasets"][0]["id"], limit=a.limit,
                                         concurrency=a.concurrency, jobs_root=a.jobs, name=name, size=a.boat_size,
                                         launch=a.launch or shard.launch_id(definitions.job_word(b), definitions.job_word(other), ""),
                                         needs=definitions.option_needs(b, vals)["host"], credentials=creds, say=lambda s: print(s, flush=True),
                                         env={**os.environ, **run_env(a.provider)} if a.from_run else os.environ)
    except (boat_units.Refused, boat.Unavailable, merge.Refused) as e:
        print("refused:", e)
        return 2
    print(job_dir)
    return 3 if failed else 0


def _shard(spec):
    if not spec:
        return None
    try:
        return shard.parse(spec)
    except ValueError as e:
        sys.exit(str(e))


def cmd_traces(a):
    said = {"full": "with model calls and tool calls", "partial": "without tool times: the trial kept no ACP events",
            "empty": "nothing recorded", "control": "controls, with nothing to show", "has_trace": "already have a trace, left as they are",
            "not_standard": "not standard runs", "unreadable": "could not be read"}
    counts = {}
    for trial in job.trial_dirs(a.root):
        try:
            got = job.backfill_trace(trial, write=not a.dry_run)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            got = "unreadable"
            print(f"{trial}: {e}", file=sys.stderr)
        counts[got] = counts.get(got, 0) + 1
    made = counts.get("full", 0) + counts.get("partial", 0)
    print(f"{'would write' if a.dry_run else 'wrote'} {made} traces")
    for name in ("full", "partial", "empty", "control", "has_trace", "not_standard", "unreadable"):
        if counts.get(name):
            print(f"  {counts[name]:>5}  {said[name]}")
    return 1 if counts.get("unreadable") else 0


def cmd_merge(a):
    try:
        got = merge.merge(a.jobs, a.out, force=a.force)
    except merge.Refused as e:
        print("refused:", e)
        return 2
    print(f"{got['out']}: {got['trials']} trials from {len(got['merged_from'])} shards")
    for line in got["notes"]:
        print("  " + line)
    return 0


def cmd_fidelity(a):
    o = definitions.get("agent", a.other)
    if not o:
        sys.exit(f"no agent {a.other!r}: {[d['_ref'] for d in definitions.load('agent', every=True)[0]]}")
    try:
        got = fidelity.check(o, model=a.model, provider=a.provider, jobs_root=a.jobs, passes=a.passes, say=print,
                             unsandboxed=a.unsandboxed, options=options(a.option),
                             env=run_env(a.provider) if a.from_run else None)
    except run.Refused as e:
        print("refused:", e)
        return 2
    return 0 if got["verdict"] == "same" else 1


def cmd_prove(a):
    b = _bench(a.benchmark)
    jobs = options(a.job)
    for c in jobs:
        if c not in definitions.CONTROLS:
            sys.exit(f"--job {c}=...: a control, {' or '.join(definitions.CONTROLS)}")
    held, spill = licences(b, from_run=a.from_run)
    try:
        got = prove.prove(b, dataset=a.dataset, limit=a.limit, jobs_root=a.jobs, provider=a.provider, judge_model=a.judge_model,
                          stamp=a.stamp, concurrency=a.concurrency, say=print, shard=_shard(a.shard), launch=a.launch,
                          env=run_env(a.provider) if a.from_run else None, licences=held,
                          bench_options=options(a.bench_option), jobs=jobs, remove_images=a.remove_images)
    except run.Refused as e:
        print("refused:", e)
        return 2
    finally:
        if spill:
            shutil.rmtree(spill, ignore_errors=True)
    return 0 if all(r["ok"] for checks in got.values() for r in checks.values()) else 1


REPLAY_EPILOG = """\
changes, applied in this order, each recorded with the request's sha256 before and after:
  --through CMD, --system FILE, --tools FILE, --tool-description NAME TEXT, --replace OLD NEW,
  --drop-last K, --set FIELD=JSON, --model M, --served-by SLUG[,SLUG...], --provider P
  (a TEXT, OLD or NEW that starts with @ is the text of that file)

examples:
  nullius standard replay jobs_standard/JOB TRIAL --list
  nullius standard replay jobs_standard/JOB TRIAL --call 8 --times 8 --max-usd 0.5 --dry-run
  nullius standard replay jobs_standard/JOB TRIAL --call 8 --times 8 --max-usd 0.5 --json
  nullius standard replay jobs_standard/JOB TRIAL --call 8 --times 8 --max-usd 0.5 --replace @old.txt @new.txt

exit codes:
  0  done: every send made under the cap, or the dry run or the list printed
  1  unexpected error (a traceback: report it)
  2  usage, or refused: a change that does not apply, a stored conversation, no price, no key, a folder in use,
     a send whose most cost has no bound or passes the cap
  3  input not found: no such job, trial, exchange record or call
  4  the operation could not be completed: a request that is not whole in the record, or no send answered
With --json, stdout is exactly one JSON document; everything else goes to stderr.
"""


def _text_arg(v: str) -> str:
    """An option's text, or the text of the file it names after ``@``."""
    from nullius.standard.runner import replay as rp
    if not v.startswith("@"):
        return v
    try:
        with open(v[1:], encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise rp.Refused(f"cannot read {v[1:]}: {e.strerror or e}")


def replay_changes(a) -> list:
    """The changes a replay's options name, in the order the help gives."""
    from nullius.standard.runner import replay as rp
    out = []
    if a.through:
        out.append(rp.through(a.through))
    if a.system:
        out.append(rp.system_text(_text_arg("@" + a.system)))
    if a.tools:
        try:
            out.append(rp.tools(json.loads(_text_arg("@" + a.tools))))
        except ValueError as e:
            raise rp.Refused(f"--tools {a.tools}: not JSON ({e})")
    for name, text in a.tool_description or []:
        out.append(rp.tool_description(name, _text_arg(text)))
    for old, new in a.replace or []:
        out.append(rp.replace_text(_text_arg(old), _text_arg(new)))
    if a.drop_last is not None:
        out.append(rp.drop_last(a.drop_last))
    for pair in a.set or []:
        name, eq, value = pair.partition("=")
        try:
            value = json.loads(value) if eq else ValueError
        except ValueError:
            value = ValueError
        if value is ValueError:
            raise rp.Refused(f"--set {pair!r}: FIELD=JSON, the value as JSON (a number, true, null, \"text\")")
        out.append(rp.set_field(name.strip(), value))
    if a.model:
        out.append(rp.model(a.model))
    if a.served_by is not None:
        out.append(rp.served_by([s.strip() for s in a.served_by.split(",") if s.strip()]))
    if a.provider:
        out.append(rp.provider(a.provider))
    return out


def _cut_line(text: str, n: int = 150) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _said_replay(doc: dict, dry: bool) -> None:
    from nullius.standard.calls.ledger import usd
    s, r = doc["source"], doc["request"]
    print(("dry run, nothing sent: " if dry else "") + f"call {s['call']} of {s['trial']}, POST {r['path']}, "
          f"{r['bytes']} bytes, sha256 {r['sha256'][:16]}" + (" (the recorded one)" if r["unchanged"] else " (changed)"))
    print(f"  {r['model']} on {r['provider']} ({r['endpoint']})" + (f", served by {','.join(r['served_by'])}" if r["served_by"] else "")
          + f"; key from {r['key_setting']} ({r['key_header']})")
    for c in doc["changes"]:
        print(f"  change {c['name']}: {c['sha256_before'][:16]} -> {c['sha256_after'][:16]}")
    e = doc["estimate_usd"]
    print(f"  estimate {usd(e['per_call'])} USD a call, {usd(e['all'])} USD for {doc['times']} (no upper bound), cap {usd(doc['max_usd'])} USD"
          + ("" if doc.get("fits") is None else ": fits" if doc["fits"] else ": over the cap, the last sends would be refused")
          + (f"; the recorded call cost {usd(e['recorded_call'])} USD" if isinstance(e.get("recorded_call"), (int, float)) else ""))
    b = doc["booked_usd"]
    print(f"  booked {usd(b['per_call'])} USD a send until its answer, the most it can cost ({b['output_tokens']} output tokens)"
          if b["per_call"] is not None else "  no bound on an answer's length: a send is refused until one is set")
    if dry:
        return
    n, sp = doc["sends"], doc["spend"]
    print(f"  {n['answered']} answered, {n['failed']} failed, {n['refused']} refused, {n['not_sent']} not sent; "
          f"spent {usd(sp.get('spent_usd') or 0.0)} USD of {usd(doc['max_usd'])}" + (f"; stopped: {doc['stopped']}" if doc.get("stopped") else ""))
    print(f"  rule: {doc['rule'].get('name')}; the recorded answer is marked *")
    for row in doc["counts"]:
        print(f"  {row['count']:>5} {'*' if row['recorded'] else ' '} {_cut_line(row['label'])}")
    print(f"  folder: {doc['out']}")


def cmd_replay(a):
    from nullius.standard.runner import replay as rp
    try:
        if a.list:
            rows = rp.calls(a.job_dir, a.trial, rule=rp.command_rule(a.read) if a.read else None)
            if a.json:
                json.dump(rows, sys.stdout, indent=1)
                print()
            else:
                print(f"{'n':>4}  {'status':>6}  {'bytes':>10}  whole  model" + ("  recorded answer" if a.read else ""))
                for c in rows:
                    print(f"{c['n']:>4}  {str(c['status']):>6}  {str(c['bytes'] or '-'):>10}  {'yes' if c['whole'] else 'no ':<5}  "
                          f"{c['model'] or '-'}" + (" (side)" if c["side"] else "") + (f"  {c['why']}" if c["why"] else "")
                          + (f"  {_cut_line(c['label'], 100)}" if a.read else ""))
            return 0
        given = {}
        for p in a.price or []:
            try:
                name, row = price_list.parse(p)
            except ValueError as e:
                raise rp.Refused(str(e))
            given[name] = row
        src = rp.source(a.job_dir, a.trial, a.call)
        prep = rp.prepare(src, replay_changes(a), prices=given)
        if a.dry_run:
            doc = dict(prep.summary(a.times, a.max_usd), dry_run=True)
        else:
            doc = rp.send(prep, a.times, a.max_usd, a.out or rp.default_out(src),
                          rule=rp.command_rule(a.read) if a.read else None, concurrency=a.concurrency)
    except rp.NotFound as e:
        print(str(e), file=sys.stderr)
        return 3
    except rp.NotWhole as e:
        print(str(e), file=sys.stderr)
        return 4
    except rp.Refused as e:
        print("refused:", e, file=sys.stderr)
        return 2
    if a.json:
        json.dump(doc, sys.stdout, indent=1, ensure_ascii=False)
        print()
    else:
        _said_replay(doc, a.dry_run)
    return 0 if a.dry_run or doc["sends"]["answered"] else 4


STUDY_EPILOG = """\
targets: --targets FILE (lines JOB_DIR TRIAL CALL), and the calls of --job's trials (--trial, repeatable):
  every one, every Nth (--every N: its number a multiple of N), or those whose recorded answer the rule
  (--read) labels with a match of --where REGEX
arms: the call as recorded, then each --arm NAME=CHANGES; CHANGES is a JSON list of [NAME, ARG...], or @FILE:
  ["replace_text", OLD, NEW], ["system_text", TEXT], ["tool_description", TOOL, TEXT], ["tools", [...]],
  ["drop_last", K], ["set_field", FIELD, VALUE], ["model", M], ["served_by", [SLUG...]], ["provider", P],
  ["through", CMD]
the same command again goes on with a study that stopped: only what was not answered is sent, under the
same cap or a new --max-usd above what was spent; a study whose targets, arms, rule or model changed is refused

examples:
  nullius standard study studies/s1 --job jobs_standard/JOB --trial TRIAL --every 4 --times 4 --max-usd 0.6 \\
      --arm 'short=[["replace_text", "Act now.", "Act now. Answer in one action."]]' --dry-run
  nullius standard study studies/s1 --targets targets.txt --arm short=@short.json --times 4 --max-usd 0.6 --json
  nullius standard recount studies/s1 --read 'python3 read.py' --json

exit codes:
  0  done: every send answered, or stopped as --stop-after asked, or the dry run printed
  1  unexpected error (a traceback: report it)
  2  usage, or refused: a change that does not apply, a plan unlike the study's, no price, no key, a folder in use,
     a send whose most cost has no bound or does not fit what is left, a cap not above what was spent
  3  input not found: no such job, trial, exchange record, call, targets file or study
  4  the operation could not be completed: a request not whole in the record, or the study stopped before its end
With --json, stdout is exactly one JSON document; everything else goes to stderr.
"""

RECOUNT_EPILOG = """\
examples:
  nullius standard recount studies/s1 --read 'python3 read.py'
  nullius standard recount replays/JOB/TRIAL/call-8__TIME --read 'python3 read.py' --json

exit codes:
  0  counted: the counts written to recounts/ in the folder, beside the earlier ones
  1  unexpected error (a traceback: report it)
  2  usage, or refused: a pattern that is no regular expression, arms the study does not have
  3  input not found: the folder holds no study and no replay
With --json, stdout is exactly one JSON document; everything else goes to stderr.
"""


def _prices_given(given) -> dict:
    from nullius.standard.runner import replay as rp
    out = {}
    for p in given or []:
        try:
            name, row = price_list.parse(p)
        except ValueError as e:
            raise rp.Refused(str(e))
        out[name] = row
    return out


def _said_table(doc: dict) -> None:
    arms = [a["name"] for a in doc["plan"]["arms"]] if "plan" in doc else list(doc["table"][0]["arms"]) if doc["table"] else []
    width = max([len(f"{r['target']} {r['name']}") for r in doc["table"]] + [6])
    print(f"  {'target':<{width}}  " + "  ".join(f"{a:>9}" for a in arms) + "  recorded answer")
    for r in doc["table"]:
        print(f"  {r['target'] + ' ' + r['name']:<{width}}  " + "  ".join(f"{c['yes']}/{c['answered']:<4}".rjust(9) for c in r["arms"].values())
              + f"  {_cut_line(r['recorded'] or '-', 60)}")
    t = doc.get("test")
    if t:
        ties = f" ({', '.join(t['tie_names'])})" if t["tie_names"] else ""
        print(f"  {t['b']} against {t['a']}: {len(t['higher'])} higher, {len(t['lower'])} lower, {len(t['ties'])} "
              f"tie{'' if len(t['ties']) == 1 else 's'}{ties}" + (f", {len(t['not_counted'])} not counted" if t["not_counted"] else "")
              + f"; sign test, two-sided p = {t['p_two_sided']:.4g}")
    print("  counts (* the recorded answer):")
    for c in doc["counts"]:
        for arm, rows in c["arms"].items():
            print(f"  {c['target']} {arm}: " + " | ".join(f"{x['count']}{'*' if x['recorded'] else ''} {_cut_line(x['label'], 80)}"
                                                    for x in rows if x["count"] or x["recorded"]))


def _said_study(doc: dict) -> None:
    from nullius.standard.calls.ledger import usd
    if doc.get("dry_run"):
        s = doc["sends"]
        print(f"dry run, nothing sent: {len(doc['targets'])} targets x {len(doc['arms'])} arms x {doc['times']} sends = {s['all']}; "
              f"{s['to_send']} to send" + (f" (the study goes on: {s['answered']} answered)" if doc["goes_on"] else ""))
        for t in doc["targets"]:
            cost = t.get("recorded_cost_usd")
            print(f"  {t['target']} {t['trial']} call {t['call']}: {t['bytes']} bytes"
                  + (f", the recorded call cost {usd(cost)} USD" if isinstance(cost, (int, float)) else "") + "; "
                  + ", ".join(f"{a['arm']} {usd(a['estimate_usd'])} USD a call" + ("" if a["unchanged"] else " (changed)")
                              + (f", booked at {usd(a['booked_usd'])}" if a["booked_usd"] is not None else ", no bound on its answer")
                              for a in t["arms"]))
        print(f"  estimate {usd(doc['estimate_usd'])} USD for {s['to_send']} (an estimate, no upper bound), cap {usd(doc['max_usd'])} USD, "
              f"spent {usd(doc['spent_usd'])}" + (": fits" if doc["fits"] else ": over the cap, the last sends would be refused"))
        return
    n = sum(r["arms"][a]["answered"] for r in doc["table"] for a in r["arms"])
    sp = doc["spend"]
    print(f"study {doc['out']}: {n} of {n + doc['left']} answered ({doc['state']}), spent {usd(sp.get('spent_usd') or 0.0)} USD "
          f"of {usd(doc['max_usd'])}" + (f"; stopped: {doc['starts'][-1]['stopped']}" if doc["left"] and doc["starts"][-1].get("stopped") else ""))
    print(f"  rule: {doc['plan']['rule'].get('command') or doc['plan']['rule'].get('name')}; yes: {doc['yes'].get('pattern') or doc['yes']['name']}")
    _said_table(doc)


def _on_term_interrupt():
    """A study stopped by TERM ends as by Ctrl-C: what is in flight is answered and written, nothing more is sent."""
    def stopped(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stopped)


def cmd_study(a):
    from nullius.standard.runner import replay as rp, study as sd
    try:
        rule = rp.command_rule(a.read) if a.read else None
        targets = sd.read_targets(a.targets) if a.targets else []
        if a.job:
            targets += sd.targets(a.job, a.trial, every=a.every or 1, where=a.where, rule=rule)
        if not targets:
            raise rp.Refused("no target: the trials named hold no call that can be sent again" + (" and that --where finds" if a.where else ""))
        arms = []
        for spec in a.arm or []:
            name, eq, text = spec.partition("=")
            if not eq:
                raise rp.Refused(f"--arm {spec!r}: NAME=CHANGES, CHANGES a JSON list of [NAME, ARG...] or @FILE")
            try:
                changes = json.loads(_text_arg(text))
            except ValueError as e:
                raise rp.Refused(f"--arm {name}: not JSON ({e})")
            arms.append(sd.Arm(name.strip(), sd.changes_of(changes)))
        if not a.dry_run:
            _on_term_interrupt()
        doc = sd.study(a.out, targets, arms, a.times, a.max_usd, rule=rule, yes=a.yes, compare=a.compare,
                       prices=_prices_given(a.price), concurrency=a.concurrency, stop_after=a.stop_after, dry_run=a.dry_run,
                       say=lambda line: print(line, file=sys.stderr, flush=True))
    except rp.NotFound as e:
        print(str(e), file=sys.stderr)
        return 3
    except rp.NotWhole as e:
        print(str(e), file=sys.stderr)
        return 4
    except rp.Refused as e:
        print("refused:", e, file=sys.stderr)
        return 2
    if a.json:
        json.dump(doc, sys.stdout, indent=1, ensure_ascii=False)
        print()
    else:
        _said_study(doc)
    if a.dry_run or doc["state"] == "done":
        return 0
    return 0 if str(doc["starts"][-1].get("stopped") or "").startswith("stopped after") else 4


def cmd_recount(a):
    from nullius.standard.runner import replay as rp, study as sd
    try:
        doc = sd.recount(a.folder, rule=rp.command_rule(a.read) if a.read else None, yes=a.yes, compare=a.compare)
    except rp.NotFound as e:
        print(str(e), file=sys.stderr)
        return 3
    except rp.Refused as e:
        print("refused:", e, file=sys.stderr)
        return 2
    if a.json:
        json.dump(doc, sys.stdout, indent=1, ensure_ascii=False)
        print()
        return 0
    print(f"recount of the {doc['of']} in {a.folder}: {doc['read']} answers read again, rule "
          f"{doc['rule'].get('command') or doc['rule'].get('name')}; written to {doc['file']}")
    if doc["of"] == "study":
        _said_table(doc)
    else:
        for row in doc["counts"]:
            print(f"  {row['count']:>5} {'*' if row['recorded'] else ' '} {_cut_line(row['label'])}")
    return 0


def _exit_on_term():
    """Stop sends SIGTERM, whose default skips cleanup and can leave containers running. The first TERM is an exit, which runs every ``finally``; a
    second one (Stop signals the group, then the process) is ignored so it
    cannot cut that cleanup short."""
    def stopped(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stopped)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nullius.standard.cli", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every definition, and which pairs fit")
    c = sub.add_parser("check", help="check the definitions, or one file")
    c.add_argument("path", nargs="?")
    f = sub.add_parser("fit", help="what can run a benchmark, and why the rest cannot")
    f.add_argument("benchmark")
    p = sub.add_parser("pins", help="compare image pins with registry tags without changing them")
    p.add_argument("benchmark")
    p.add_argument("--tag", help="tag to compare (default: embedded tag, or latest)")
    p.add_argument("--limit", type=int, help="check only the first N pins")
    n = sub.add_parser("new", help="a definition to fill in, in its catalogue folder")
    n.add_argument("kind", type=definitions.names.kind, choices=[*definitions.KINDS, "version"])
    n.add_argument("key")
    n.add_argument("version", nargs="?", help="new version: the version to add")
    n.add_argument("--family", type=definitions.names.kind, choices=list(definitions.FAMILIES), help="a benchmark's family (agent by default)")
    n.add_argument("--port", type=definitions.names.port, help="the port a new benchmark needs")
    helps = {"conform": "run the kits, record what passed", "run": "run a benchmark into a job",
             "fidelity": "drive an agent over ACP and through its own command: does its model get the same requests?"}
    for name in ("conform", "run", "fidelity"):
        p = sub.add_parser(name, help=helps[name])
        if name == "run":
            p.add_argument("benchmark")
        p.add_argument("other", help="an agent or a capability: KEY (its default version) or KEY@VERSION"
                       + ("; or nop, oracle: a control the benchmark declares; or original, model: one of its setups" if name == "run" else ""))
        p.add_argument("--model", required=name == "conform",
                       help="the model (a control needs none; original takes one of the paper's, its first by default)" if name == "run" else None)
        p.add_argument("--provider", default="openai", choices=sorted(providers.PROVIDERS))
        p.add_argument("--jobs", default="jobs_standard", help="where jobs are written (default jobs_standard/, gitignored)")
        p.add_argument("--unsandboxed", action="store_true",
                       help="allow an agent, or a setup check without Docker, to execute code on this machine as you")
        p.add_argument("--backend", default="local", type=definitions.names.backend, choices=list(run.BACKENDS),
                       help="local processes (the default); container: each unit in a container, its relay beside it "
                            "(docker: its earlier name); boat: each unit's agent in a boat.dev VM of its own")
        if name == "run":
            p.add_argument("--relay-container", action="store_true",
                           help="run the relay in a separate container while task processes run on this host")
            p.add_argument("--boat-size", default=boat.SIZE, choices=list(boat.SIZES),
                           help="on the boat backend, its VMs' machine type (default small: 2 vCPU, 4 GB)")
        p.add_argument("--from-run", action="store_true",
                       help="started by Nullius: the key is OPENAI_API_KEY (and OPENAI_BASE_URL), saved settings are not read")
        p.add_argument("--option", action="append", metavar="NAME=VALUE",
                       help="one of the agent's or capability's options (repeatable); the rest keep their defaults")
        if name == "fidelity":
            p.add_argument("--passes", type=int, default=2, help="runs of each way (default 2: the second shows the noise)")
        elif name == "conform":
            p.add_argument("--fixture-image", help="digest-pinned root image for the workspace kit")
            p.add_argument("--max-usd-per-unit", type=float, help="spending cap per kit check")
            p.add_argument("--max-calls", type=int, help="model call cap per kit check")
            p.add_argument("--port", type=definitions.names.port, action="append", help="only this port's kit (repeatable)")
            p.add_argument("--stamp", help="the time stamp the kit jobs are named with (default: now)")
            p.add_argument("--attempts", "--repeat", dest="repeat", type=int, default=1, metavar="K",
                           help="run each check K times; a check passes when it passes in most of them (default 1)")
        else:
            p.add_argument("--dataset")
            p.add_argument("--units", metavar="FILE",
                           help="only the units this file lists, one id a line (# starts a comment): in the dataset's order, "
                                "before --shard and --limit; the job's config records the file's name, sha256 and units")
            p.add_argument("--limit", type=int)
            p.add_argument("-j", "--concurrency", type=int, default=4)
            p.add_argument("--judge-model", help="a benchmark scored by a model: this judge instead of its official one")
            p.add_argument("--simulator-model", help="a benchmark whose user a model plays: this model instead of its official one")
            p.add_argument("--attempts", "--repeat", dest="repeat", type=int, default=1, metavar="K",
                           help="run each unit K times, each an attempt counted whatever its result "
                                "(the upstream's --num-trials, pass^k); default 1")
            p.add_argument("--retries", type=_retries, default=RETRIES, metavar="N",
                           help="run a failed attempt again, up to N times, after the other units: never after a timeout, "
                                f"a wrong key or model, spent credit or a cap; 0 to {MAX_RETRIES}, default {RETRIES}")
            p.add_argument("--remove-images", action="store_true",
                           help="remove each image this run pulls or builds when its last unit ends (default: kept for the next run)")
            p.add_argument("--name")
            p.add_argument("--shard", metavar="I/N", help="only shard I of N of the units: by the unit id's hash, before --limit")
            if name == "run":
                p.add_argument("--shard-by", choices=list(shard.BY), default="id",
                               help="split by the unit id's hash (id, the default) or by its place in the dataset (position)")
            p.add_argument("--launch", help="the launch id every shard records (default: the day, the benchmark, the agent, the model)")
            p.add_argument("--bench-option", action="append", metavar="NAME=VALUE",
                           help="one of the benchmark's own options (repeatable: a search, a mode); the rest keep their defaults")
            p.add_argument("--price", action="append", metavar="MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]",
                           help="this run's price for a model in USD per million tokens, over your price list (repeatable; nullius prices)")
            p.add_argument("--max-usd", type=float, metavar="USD",
                           help="the run's spending cap: no call goes out once the spend plus the calls in flight would pass it")
            p.add_argument("--constraint", action="append", metavar="KEY=VALUE",
                           help="what the machine allows the run, repeatable: cpus, memory_mb, processes and cpu_set on its "
                                "containers and, through a systemd scope, on its processes here; concurrency; gpu_ids (empty for none)")
            p.add_argument("--served-by", action="append", metavar="[MODEL=]SLUG[,SLUG...]",
                           help="on OpenRouter, the serving providers that may answer a model, tried in this order and no "
                                "other (repeatable; without MODEL=, the run's model); each call records which one did")
            p.add_argument("--max-usd-per-unit", type=float, metavar="USD",
                           help="each unit's own spending cap: a unit that reaches it is cut off there, and the run goes on")
            p.add_argument("--timeout", action="append", metavar="PHASE=SECONDS",
                           help="a unit's time limit for one phase, forced over the task's, the benchmark's and the default "
                                "(repeatable): setup, image, agent (its whole work on the unit), verify, score")
            p.add_argument("--max-turns", type=int, metavar="N",
                           help="the most agent turns in a conversation, over the benchmark's limits.turns")
            p.add_argument("--max-calls", type=int, metavar="N",
                           help="the most model calls a unit's agent may make, the whole unit through: past it the relay refuses "
                                "the next call and the unit is the agent's error, as a timeout is")
            p.add_argument("--exchanges", default=exchanges.DEFAULT, choices=exchanges.MODES,
                           help="what each unit's relay keeps of its agent's calls: the request as sent and the reply as it "
                                "arrived (whole, the default), the same without the pictures' bytes (text), or the call record alone (off)")
            p.add_argument("--exchanges-max-mb", type=int, default=exchanges.MAX_MB, metavar="N",
                           help="a unit's bound on what it keeps of its calls, in MB: past it a call's line says cut and the call "
                                f"record goes on (default {exchanges.MAX_MB})")
    v = sub.add_parser("prove", help="prove a benchmark: its controls, its units, no leak; record what passed")
    v.add_argument("benchmark")
    v.add_argument("--dataset", help="one dataset (default: each)")
    v.add_argument("--limit", type=int, help="the first N units of each dataset (the record says so)")
    v.add_argument("--provider", default="openai", choices=sorted(providers.PROVIDERS), help="a judged benchmark's: the judge's provider")
    v.add_argument("--judge-model", help="a judged benchmark: this judge instead of its official one")
    v.add_argument("-j", "--concurrency", type=int, default=4)
    v.add_argument("--jobs", default="jobs_standard", help="where the control jobs are written (default jobs_standard/)")
    v.add_argument("--stamp", help="the time stamp the control jobs are named with (default: now)")
    v.add_argument("--shard", metavar="I/N", help="the controls and the leak check on shard I of N only")
    v.add_argument("--launch", help="the launch id the control jobs of every shard record")
    v.add_argument("--bench-option", action="append", metavar="NAME=VALUE",
                   help="one of the benchmark's own options (repeatable), for the checks' driver and the controls' runs")
    v.add_argument("--job", action="append", metavar="CONTROL=JOB_DIR",
                   help="a finished control job over the whole dataset, checked against its bound instead of running the control")
    v.add_argument("--remove-images", action="store_true",
                   help="remove each image this proof pulls or builds after its unit, so the oracle pulls it again (default: kept)")
    g = sub.add_parser("merge", help="merge the shard jobs of one launch into one job")
    g.add_argument("jobs", nargs="+", metavar="JOB_DIR")
    g.add_argument("-o", "--out", required=True, help="the merged job's directory (its name is the job's)")
    g.add_argument("--force", action="store_true", help="merge shards of different launches, versions, models or agents")
    tr = sub.add_parser("traces", help="give past standard trials a trace, from the files they kept")
    tr.add_argument("root", metavar="DIR", help="a job, or a folder of jobs")
    tr.add_argument("--dry-run", action="store_true", help="say what it would write, and write nothing")
    v.add_argument("--from-run", action="store_true",
                   help="started by Nullius: the key is OPENAI_API_KEY (and OPENAI_BASE_URL), saved settings are not read")
    rp = sub.add_parser("replay", help="send one recorded model call of a trial again K times under a spending cap, and count the answers",
                        description="Send one recorded model call of a trial again, as it was or changed, K times under a "
                                    "spending cap; read each answer by a rule and count the distinct answers. Everything "
                                    "is kept in one folder outside the run's tree. The key comes from the environment, "
                                    "then the saved settings, by the provider's setting name.",
                        epilog=REPLAY_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    rp.add_argument("job_dir", metavar="JOB_DIR", help="a job's folder, whose trials saved their exchange record")
    rp.add_argument("trial", metavar="TRIAL", help="a trial's name (an earlier try: NAME.try-N)")
    rp.add_argument("--list", action="store_true",
                    help="list the trial's recorded calls instead: number, status, size, and whether the request is whole")
    rp.add_argument("--call", type=int, metavar="N", help="the call to send again, as the call record numbers it (n)")
    rp.add_argument("--times", type=int, metavar="K", help="how many times to send it")
    rp.add_argument("--max-usd", type=float, metavar="USD",
                    help="the replay's spending cap: each send is booked at the most it can cost, and none ends past it")
    rp.add_argument("--through", metavar="CMD",
                    help="a command that reads the request's JSON on stdin and prints the request to send (run without a key)")
    rp.add_argument("--system", metavar="FILE", help="the system text replaced by this file's text")
    rp.add_argument("--tools", metavar="FILE", help="the tools replaced by this file's JSON list, in the request's own form")
    rp.add_argument("--tool-description", nargs=2, action="append", metavar=("NAME", "TEXT"),
                    help="the description of the tool NAME replaced by TEXT (repeatable)")
    rp.add_argument("--replace", nargs=2, action="append", metavar=("OLD", "NEW"),
                    help="OLD replaced by NEW in the system text or a message; OLD must appear exactly once (repeatable)")
    rp.add_argument("--drop-last", type=int, metavar="K", help="the last K messages left out")
    rp.add_argument("--set", action="append", metavar="FIELD=JSON",
                    help="a top-level parameter set (temperature=0, max_tokens=2048); null removes it (repeatable)")
    rp.add_argument("--model", help="another model; the request's pin is left out unless --served-by gives one")
    rp.add_argument("--served-by", metavar="SLUG[,SLUG...]",
                    help="on OpenRouter, only these serving providers, in this order; an empty value: no pin")
    rp.add_argument("--provider", choices=sorted(providers.PROVIDERS),
                    help="another provider than the job's; one that does not route to serving providers takes no pin")
    rp.add_argument("--price", action="append", metavar="MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]",
                    help="a price in USD per million tokens, over the job's and your price list (repeatable)")
    rp.add_argument("--read", metavar="CMD",
                    help="the reading rule: a command that reads one reply's body on stdin and prints its label "
                         "(default: the first tool call's name and arguments; run without a key)")
    rp.add_argument("-j", "--concurrency", type=int, default=1, metavar="N", help="sends in flight at once (default 1)")
    rp.add_argument("--dry-run", action="store_true", help="print what would be sent, its estimated cost and its booking; send nothing")
    rp.add_argument("--out", metavar="DIR",
                    help="the replay's folder, new or empty, outside the job (default replays/<job>/<trial>/call-<N>__<time>)")
    rp.add_argument("--json", action="store_true", help="one JSON document on stdout")
    st = sub.add_parser("study", help="send recorded model calls of several trials again, as recorded and changed, and compare them call by call",
                        description="Send recorded model calls of several trials again, each K times as recorded and under "
                                    "each named arm of changes, under one spending cap; read each answer by one rule kept "
                                    "before the first send; count the answers by target and arm and compare two arms with "
                                    "an exact sign test. Everything is kept in one folder outside every run's tree.",
                        epilog=STUDY_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    st.add_argument("out", metavar="DIR", help="the study's folder: new, or the study's own to go on with it")
    st.add_argument("--targets", metavar="FILE", help="a file of lines JOB_DIR TRIAL CALL, one target each (# starts a comment)")
    st.add_argument("--job", metavar="JOB_DIR", help="a job whose trials (--trial) give targets")
    st.add_argument("--trial", action="append", metavar="TRIAL", help="a trial of --job: its calls are targets (repeatable)")
    st.add_argument("--every", type=int, metavar="N", help="only every Nth call of the trials: its number a multiple of N")
    st.add_argument("--where", metavar="REGEX", help="only the calls whose recorded answer the rule labels with a match")
    st.add_argument("--arm", action="append", metavar="NAME=CHANGES",
                    help="an arm after the call as recorded: its changes as a JSON list of [NAME, ARG...], or @FILE (repeatable)")
    st.add_argument("--times", type=int, metavar="K", help="sends of each target under each arm")
    st.add_argument("--max-usd", type=float, metavar="USD",
                    help="the study's spending cap, over all its starts, each send booked at the most it can cost; a new one above the spend replaces it")
    st.add_argument("--price", action="append", metavar="MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]",
                    help="a price in USD per million tokens, over the job's and your price list (repeatable)")
    st.add_argument("--read", metavar="CMD",
                    help="the reading rule: a command that reads one answer as JSON on stdin and prints its label "
                         "(default: the first tool call's name and arguments; run without a key)")
    st.add_argument("--yes", metavar="REGEX", help="an answer reads yes when its label matches (default: the label is the recorded answer's)")
    st.add_argument("--compare", nargs=2, metavar=("A", "B"), help="the two arms of the sign test (default: the first two)")
    st.add_argument("-j", "--concurrency", type=int, default=1, metavar="N", help="sends in flight at once (default 1)")
    st.add_argument("--stop-after", type=int, metavar="N", help="at most N sends in this start; the same command again goes on")
    st.add_argument("--dry-run", action="store_true", help="print the number of sends and the estimate; send nothing, write nothing")
    st.add_argument("--json", action="store_true", help="one JSON document on stdout")
    rc = sub.add_parser("recount", help="count a kept study or replay again with another rule, with no send",
                        description="Read the answers a study or a replay kept again by another rule, with no send and no "
                                    "key; the counts are written to recounts/ in its folder, beside the earlier ones.",
                        epilog=RECOUNT_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    rc.add_argument("folder", metavar="DIR", help="a study's folder (study.json) or a replay's (replay.json)")
    rc.add_argument("--read", metavar="CMD", help="the reading rule: a command, as study --read (default: the first tool call)")
    rc.add_argument("--yes", metavar="REGEX", help="a study: an answer reads yes when its label matches (default: the study's reading)")
    rc.add_argument("--compare", nargs=2, metavar=("A", "B"), help="a study: the two arms of the sign test (default: the study's)")
    rc.add_argument("--json", action="store_true", help="one JSON document on stdout")
    # Bootstrap decides whether settings may be read before these modules are
    # imported. Keep --from-run exact here too, so --from-r cannot bypass it.
    for parser in (ap, *sub.choices.values()):
        parser.allow_abbrev = False
    a = ap.parse_args(argv)
    if a.cmd == "replay" and not a.list and (a.call is None or a.times is None or a.max_usd is None):
        rp.error("--call N, --times K and --max-usd USD go together (or --list)")
    if a.cmd == "replay" and a.list and (a.call is not None or a.dry_run):
        rp.error("--list goes alone, with --read and --json or without")
    if a.cmd == "study" and (a.times is None or a.max_usd is None or not (a.targets or a.job)):
        st.error("--times K, --max-usd USD and the targets (--targets FILE, or --job JOB_DIR --trial TRIAL) go together")
    if a.cmd == "study" and bool(a.job) != bool(a.trial):
        st.error("--job JOB_DIR and --trial TRIAL go together")
    if a.cmd == "study" and (a.every or a.where) and not a.job:
        st.error("--every and --where take the calls of --job's trials")
    if a.cmd in ("run", "prove", "conform", "fidelity"):
        _exit_on_term()
        progress.ECHO = sys.stderr           # each stage as a line (nullius/progress.py): stdout stays the job and its units
    if not getattr(a, "from_run", False):
        settings.load(ENV_FILE)
    return {"list": cmd_list, "check": cmd_check, "fit": cmd_fit, "pins": cmd_pins, "new": cmd_new, "conform": cmd_conform, "run": cmd_run,
            "fidelity": cmd_fidelity, "prove": cmd_prove, "merge": cmd_merge, "traces": cmd_traces,
            "replay": cmd_replay, "study": cmd_study, "recount": cmd_recount}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
