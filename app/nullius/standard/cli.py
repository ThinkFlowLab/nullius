"""The standard from a shell.

    PYTHONPATH=app python3 -m nullius.standard.cli list
    PYTHONPATH=app python3 -m nullius.standard.cli check [PATH]
    PYTHONPATH=app python3 -m nullius.standard.cli fit BENCHMARK[@VERSION]
    PYTHONPATH=app python3 -m nullius.standard.cli new benchmark|agent|capability KEY [--family F] [--port P]
    PYTHONPATH=app python3 -m nullius.standard.cli new version KEY VERSION
    PYTHONPATH=app python3 -m nullius.standard.cli conform AGENT|CAPABILITY --model M [--provider P] [--port PORT ...] [--repeat K] [--option NAME=VALUE ...] [--unsandboxed | --backend docker]
    PYTHONPATH=app python3 -m nullius.standard.cli run BENCHMARK[@VERSION] AGENT|CAPABILITY --model M [--provider P] [--limit N] [-j N] [--option NAME=VALUE ...] [--bench-option NAME=VALUE ...] [--price MODEL=INPUT,OUTPUT[,CACHED] ...] [--max-usd USD] [--unsandboxed | --backend docker]
    PYTHONPATH=app python3 -m nullius.standard.cli run BENCHMARK[@VERSION] nop|oracle [--dataset D] [--limit N]
    PYTHONPATH=app python3 -m nullius.standard.cli prove BENCHMARK[@VERSION] [--dataset D] [--limit N]
    PYTHONPATH=app python3 -m nullius.standard.cli run ... --shard I/N [--launch ID]
    PYTHONPATH=app python3 -m nullius.standard.cli merge JOB_DIR... -o OUT [--force]
    PYTHONPATH=app python3 -m nullius.standard.cli fidelity AGENT --model M [--provider P] [--option NAME=VALUE ...] [--passes N] --unsandboxed

An agent runs commands as you on the local backend, which is not a sandbox:
an agent's run or conformance check there is refused without ``--unsandboxed``.
``--backend docker`` runs each unit's agent or capability in a container whose
only way out is the unit's relay (``standard/container.py``), and needs no
such yes.
A benchmark's control (``nop``, ``oracle``: its ``controls``) runs no agent
and calls no model - only a judged benchmark's judge - so it asks for neither.
``prove`` runs a benchmark's controls and checks its units and that nothing
leaks, and writes what passed into its ``proven`` (``standard/prove.py``).

A benchmark, an agent or a capability is named by its key (its default
version) or ``key@version``. ``list`` shows every definition - each version - and which
pairs fit; ``check`` checks the
definitions (or one file); ``new`` writes a definition to fill in, or a new
version of an agent or a capability (``new version``: the default's pins, with
the version replaced, and nothing proven); ``fit`` says, for one benchmark, what can run it and
why the rest cannot; ``conform`` runs the kits and writes what passed into the
definition - the port, and each skill of its kit shown or not (``--repeat K``:
each check K times, held by a majority); ``run`` runs a benchmark into a job under ``--jobs`` (``jobs_standard/``),
which the application opens like any other; ``fidelity`` drives an agent over
ACP and through its own command, compares the requests its model was sent, and
writes the verdict into the definition (``standard/fidelity.py``). ``--shard I/N`` runs one
shard of the dataset's units (``standard/shard.py``: by the unit id's hash, then
``--limit``), the job recording its shard and its launch (``--launch``); ``merge``
puts a launch's shard jobs together into one job (``standard/merge.py``). ``--option``
sets one of the agent's or capability's options (``options`` in its definition);
the rest keep the vendor's defaults, and the job's config records them all.
A run says each stage before its first unit, and each unit's end, as one
``[nullius] stage=...`` line on stderr and in the job's ``progress.jsonl``
(``nullius/progress.py``): what the run's page shows while it goes.

Keys come from the process environment, then the saved user settings - the
file the Settings page edits. Nothing is printed of them. With ``--from-run`` (a run
the builder or a page started, ``standard/recipe.py``) saved settings are not
read: the key is the one the run was handed, ``OPENAI_API_KEY``, and the
endpoint ``OPENAI_BASE_URL`` when it names one (``run_env``). The licences a
benchmark's environments declare are never in either: the run reads them from
the benchmark's store (``nullius.secrets``) on this machine, or, on a host, from
the one line its launcher handed it (``licences``).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import tempfile

from nullius import paths, prices as price_list, progress, secrets, settings
# The legacy module command needs saved path overrides before install.HOME
# and other runtime defaults are computed, just like the unified CLI does.
if __name__ == "__main__" and "--from-run" not in sys.argv[1:]:
    settings.load(paths.settings_file())

from nullius.standard import conform, definitions, fidelity, job, match, merge, ports, prove, run, shard

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
    are named here as the provider's own settings and nothing else is."""
    p = run.PROVIDERS[provider]
    out = {k: v for k, v in env.items() if k in run.KEEP}
    if env.get("OPENAI_API_KEY"):
        out[p["key"]] = env["OPENAI_API_KEY"]
    if "base_var" in p and env.get("OPENAI_BASE_URL"):
        out[p["base_var"]] = env["OPENAI_BASE_URL"]
    return out


def licences(bench: dict, env=os.environ):
    """(``run.Licences``, the folder to remove after) for a run on a host,
    whose launcher handed it the benchmark's licences as one line
    (``secrets.HANDED``, ``execution``): a licence file is written to
    ``NULLIUS_LICENCE_DIR`` - in memory, ``/dev/shm`` - and nowhere else.
    The line leaves this process's environment once read. (None, None) on
    this machine: the run reads the benchmark's own store."""
    line = env.pop(secrets.HANDED, None)
    if not line:
        return None, None
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
    from nullius.standard.recipe import skills_of
    proven, out = d.get("proven") or {}, []
    for p in d.get("offers") or []:
        got = skills_of(p, proven.get(p)) if ports_ is None or p in ports_ else []
        if got:
            out.append(f"{p} skills: " + " · ".join(f"{s['id']} {'✓' if s['shown'] else '✗'}" for s in got))
    return out


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
        bad = definitions.problems(dict(d, _here=os.path.dirname(os.path.abspath(a.path))), kind)
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
            "offers": [], "proven": {},
        },
    },
    "capability": {
        "capability.json": {
            "schema": "nullius.capability/v1", "key": "<KEY>", "label": "<KEY>", "summary": "SAY WHAT THE CAPABILITY KEEPS AND WHAT IT GIVES BACK.",
            "kind": "memory", "start": {"command": ["python3", "{here}/capability.py"]}, "offers": [], "proven": {},
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
    as it was, proofs included. Returns the file's path."""
    if not definitions._KEY.match(key):
        sys.exit(f"no agent, capability or benchmark {key!r}")
    kind = next((k for k in ("agent", "capability", "benchmark")
                 if (folder := definitions.entry_dir(k, key))
                 and os.path.isfile(definitions.definition_path(folder, k))), None)
    if not kind:
        sys.exit(f"no agent, capability or benchmark {key!r}")
    if not definitions._VERSION.match(version):
        sys.exit("a version is letters, digits and . + _ -")
    path = definitions.definition_path(definitions.entry_dir(kind, key), kind)
    with open(path) as f:
        d = definitions.names.definition(json.load(f))
    if "versions" not in d:
        up = dict(d.get("upstream") or {})
        old = str(up.pop("version", "") or "")
        if not old:
            sys.exit(f"{path}: upstream.version is not set, so its one version has no name")
        # a benchmark's version holds only its proofs at first: what differs in the new one is the user's to say
        block = {"proven": d.pop("proven", {})} if kind == "benchmark" else {"install": d.pop("install", {}), "proven": d.pop("proven", {})}
        d.pop("offers", None)
        d["upstream"] = up
        d["default"], d["versions"] = old, {old: block}
    if version in d["versions"]:
        sys.exit(f"{key} has a version {version} already")
    old = d["default"]
    base = d["versions"][old]
    copy = json.loads(json.dumps({k: v for k, v in base.items() if k != "proven"}).replace(old, version))
    d["versions"] = {version: dict(copy, proven={}), **d["versions"]}
    path = os.path.join(definitions.writable_dir(kind, key), definitions.KINDS[kind]["file"])
    with open(path, "w") as f:
        json.dump(d, f, indent=2)
        f.write("\n")
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


def options(pairs) -> dict:
    """``--option name=value``, repeatable, as {name: value}."""
    out = {}
    for p in pairs or []:
        name, eq, value = p.partition("=")
        if not eq or not name:
            sys.exit(f"--option {p!r}: NAME=VALUE")
        out[name.strip()] = value.strip()
    return out


def prices(given) -> dict:
    """``--price MODEL=INPUT,OUTPUT[,CACHED]``, repeatable: this run's own prices, over the user's list."""
    out = {}
    for p in given or []:
        try:
            name, row = price_list.parse(p)
        except ValueError as e:
            sys.exit(str(e))
        out[name] = row
    return out


def cmd_conform(a):
    o = _other(a.other)
    try:
        got = conform.check(o, model=a.model, provider=a.provider, which=a.port or None, jobs_root=a.jobs, say=print,
                            unsandboxed=a.unsandboxed, stamp=a.stamp, backend=a.backend, env=run_env(a.provider) if a.from_run else None,
                            options=options(a.option), repeat=a.repeat)
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
        other, why = definitions.setup(b, a.other, a.model or "", a.provider)
        if other is None:
            print("refused:", why)
            return 2
    elif not a.model:
        sys.exit("--model: the model the agent or the capability is run with")
    else:
        other = _other(a.other)
    held, spill = licences(b)
    try:
        job, failed = run.run_and_report(b, other, model=a.model or "", provider=a.provider, dataset=a.dataset, limit=a.limit,
                      concurrency=a.concurrency, jobs_root=a.jobs, name=a.name, unsandboxed=a.unsandboxed, judge_model=a.judge_model, backend=a.backend,
                      simulator_model=a.simulator_model, options=options(a.option), shard=_shard(a.shard), launch=a.launch,
                      licences=held, bench_options=options(a.bench_option), repeat=a.repeat, remove_images=a.remove_images,
                      prices=prices(a.price), max_usd=a.max_usd, **({"env": run_env(a.provider)} if a.from_run else {}))
    except run.Refused as e:
        print("refused:", e)
        return 2
    finally:
        if spill:
            shutil.rmtree(spill, ignore_errors=True)
    print(job)
    # no unit reached a verdict (a refusing endpoint, every unit an error): the run failed, and says so
    # by its exit status, so a launch of several agents pauses rather than running the next into the same error
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
    held, spill = licences(b)
    try:
        got = prove.prove(b, dataset=a.dataset, limit=a.limit, jobs_root=a.jobs, provider=a.provider, judge_model=a.judge_model,
                          stamp=a.stamp, concurrency=a.concurrency, say=print, shard=_shard(a.shard), launch=a.launch,
                          env=run_env(a.provider) if a.from_run else None, licences=held, remove_images=a.remove_images)
    except run.Refused as e:
        print("refused:", e)
        return 2
    finally:
        if spill:
            shutil.rmtree(spill, ignore_errors=True)
    return 0 if all(r["ok"] for checks in got.values() for r in checks.values()) else 1


def _exit_on_term():
    """Stop sends SIGTERM, and its default ends the process where it stands:
    Run.start's cleanup never ran, so a docker run's unit and relay
    containers (the relay holding the key) kept running and the job had no
    result.json. The first TERM is an exit, which runs every ``finally``; a
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
        p.add_argument("--provider", default="openai", choices=sorted(run.PROVIDERS))
        p.add_argument("--jobs", default="jobs_standard", help="where jobs are written (default jobs_standard/, gitignored)")
        p.add_argument("--unsandboxed", action="store_true",
                       help="run an agent on this machine as you: the local backend is not a sandbox")
        p.add_argument("--backend", default="local", choices=list(run.BACKENDS),
                       help="local processes (the default), or docker: each unit in a container, its relay beside it")
        p.add_argument("--from-run", action="store_true",
                       help="started by Nullius: the key is OPENAI_API_KEY (and OPENAI_BASE_URL), saved settings are not read")
        p.add_argument("--option", action="append", metavar="NAME=VALUE",
                       help="one of the agent's or capability's options (repeatable); the rest keep their defaults")
        if name == "fidelity":
            p.add_argument("--passes", type=int, default=2, help="runs of each way (default 2: the second shows the noise)")
        elif name == "conform":
            p.add_argument("--port", type=definitions.names.port, action="append", help="only this port's kit (repeatable)")
            p.add_argument("--stamp", help="the time stamp the kit jobs are named with (default: now)")
            p.add_argument("--repeat", type=int, default=1, metavar="K",
                           help="run each check K times; a check passes when it passes in most of them (default 1)")
        else:
            p.add_argument("--dataset")
            p.add_argument("--limit", type=int)
            p.add_argument("-j", "--concurrency", type=int, default=4)
            p.add_argument("--judge-model", help="a benchmark scored by a model: this judge instead of its official one")
            p.add_argument("--simulator-model", help="a benchmark whose user a model plays: this model instead of its official one")
            p.add_argument("--repeat", type=int, default=1, metavar="K",
                           help="run each unit K times, each an attempt of its own (the upstream's --num-trials, pass^k); default 1")
            p.add_argument("--remove-images", action="store_true",
                           help="remove each image this run pulls or builds when its last unit ends (default: kept for the next run)")
            p.add_argument("--name")
            p.add_argument("--shard", metavar="I/N", help="only shard I of N of the units: by the unit id's hash, before --limit")
            p.add_argument("--launch", help="the launch id every shard records (default: the day, the benchmark, the agent, the model)")
            p.add_argument("--bench-option", action="append", metavar="NAME=VALUE",
                           help="one of the benchmark's own options (repeatable: a search, a mode); the rest keep their defaults")
            p.add_argument("--price", action="append", metavar="MODEL=INPUT,OUTPUT[,CACHED]",
                           help="this run's price for a model in USD per million tokens, over your price list (repeatable; nullius prices)")
            p.add_argument("--max-usd", type=float, metavar="USD",
                           help="the run's spending cap: no call goes out once the spend plus the calls in flight would pass it")
    v = sub.add_parser("prove", help="prove a benchmark: its controls, its units, no leak; record what passed")
    v.add_argument("benchmark")
    v.add_argument("--dataset", help="one dataset (default: each)")
    v.add_argument("--limit", type=int, help="the first N units of each dataset (the record says so)")
    v.add_argument("--provider", default="openai", choices=sorted(run.PROVIDERS), help="a judged benchmark's: the judge's provider")
    v.add_argument("--judge-model", help="a judged benchmark: this judge instead of its official one")
    v.add_argument("-j", "--concurrency", type=int, default=4)
    v.add_argument("--jobs", default="jobs_standard", help="where the control jobs are written (default jobs_standard/)")
    v.add_argument("--stamp", help="the time stamp the control jobs are named with (default: now)")
    v.add_argument("--shard", metavar="I/N", help="the controls and the leak check on shard I of N only")
    v.add_argument("--launch", help="the launch id the control jobs of every shard record")
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
    # Bootstrap decides whether settings may be read before these modules are
    # imported. Keep --from-run exact here too, so --from-r cannot bypass it.
    for parser in (ap, *sub.choices.values()):
        parser.allow_abbrev = False
    a = ap.parse_args(argv)
    if a.cmd in ("run", "prove", "conform", "fidelity"):
        _exit_on_term()
        progress.ECHO = sys.stderr           # each stage as a line (nullius/progress.py): stdout stays the job and its units
    if not getattr(a, "from_run", False):
        settings.load(ENV_FILE)
    return {"list": cmd_list, "check": cmd_check, "fit": cmd_fit, "new": cmd_new, "conform": cmd_conform, "run": cmd_run,
            "fidelity": cmd_fidelity, "prove": cmd_prove, "merge": cmd_merge, "traces": cmd_traces}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
