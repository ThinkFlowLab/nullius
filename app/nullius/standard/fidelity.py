"""Fidelity: driving an agent over ACP sends the model what its own CLI sends.

An agent reached through an adapter is not obviously the agent as its users
run it: the adapter may choose a setting the CLI leaves to the provider, or
leave out a tool. This puts the two side by side. The agent's definition names
its own non-interactive command (``fidelity.command``, a template carrying
``{prompt}``); the same units - the workspace kit's - are run both ways, each
unit at the same paths every time, its calls on a route of its own on a relay
that records each request's fields (``relay.request_fields``: everything but
the prompt, which is kept as digests). Each way runs twice, so a field that
differs between two runs of the same way is shown to be noise - an id, a time -
rather than assumed to be.

The comparison (:func:`compare`) is per field: the configuration fields over
every model request (``reasoning.effort``, ``tools[...]``, ``instructions``,
``include``, ...), and the items of each unit's first request (``input[i]``:
the context the agent opens with). A few fields are left out by name
(``IGNORED``): the vendor's request metadata and cache keys, which label a
request and are not read by the model. The verdict is ``same`` when no other
field differs, ``differs`` otherwise, and it is written into the version's
``proven`` as ``fidelity``, beside the kits' records.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Callable, Dict, List, Optional

from nullius import prices as price_list
from nullius.standard import definitions, install, jsonl, relay, run

SIDES = ("acp", "cli")
# Fields that label a request for the vendor - ids, cache keys, analytics -
# and that the model does not read.
IGNORED = ("client_metadata", "metadata", "prompt_cache_key", "user", "safety_identifier", "previous_response_id",
           "conversation")
MODEL_PATHS = ("/responses", "/chat/completions")
ABSENT = "—"
KIT_PORT = "agent.workspace/v1"


def _value(v) -> str:
    if isinstance(v, dict) and set(v) <= {"sha256", "chars"} and "sha256" in v:
        return f"sha256:{v['sha256']}" + (f" ({v['chars']} chars)" if "chars" in v else "")
    return json.dumps(v, sort_keys=True)


def flatten(fields: dict) -> Dict[str, str]:
    """A request's fields (as the relay recorded them) as one value per key,
    without its ``input``/``messages``, which only a first request is read for."""
    out = {}

    def walk(prefix, v):
        if isinstance(v, dict) and v and not ("sha256" in v and set(v) <= {"sha256", "chars"}):
            for k, x in v.items():
                walk(f"{prefix}.{k}", x)
        else:
            out[prefix] = _value(v)

    for k, v in fields.items():
        if k in IGNORED or k in ("input", "messages"):
            continue
        if k == "tools" and isinstance(v, list):
            out["tools"] = json.dumps([t.get("name") for t in v])
            for t in v:
                out[f"tools[{t.get('name')}]"] = f"sha256:{t.get('sha256')}"
        else:
            walk(k, v)
    return out


def opening(fields: dict) -> Dict[str, str]:
    """A unit's first request: what the agent opened the conversation with,
    item by item."""
    items = fields.get("input") if "input" in fields else fields.get("messages")
    items = items if isinstance(items, list) else []
    out = {"input.items": str(len(items))}
    for i, it in enumerate(items):
        out[f"input[{i}]"] = f"{it.get('type') or 'message'}/{it.get('role') or '-'} sha256:{it.get('sha256')} ({it.get('chars')} chars)"
    return out


def observe(routes: Dict[str, List[dict]]) -> Dict[str, Dict[str, set]]:
    """One pass of one way (route -> the relay's records): every key and the
    values it took, in two families - ``every`` over each model request, and
    ``first`` over each unit's first request."""
    every, first = [], []
    for recs in routes.values():
        asked = [r for r in recs if r.get("fields") and str(r.get("path", "")).endswith(MODEL_PATHS)]
        every += [flatten(r["fields"]) for r in asked]
        if asked:
            first.append(opening(asked[0]["fields"]))
    return {"every": every, "first": first}


def _values(obs: dict, fam: str, key: str) -> frozenset:
    return frozenset(f.get(key, ABSENT) for f in obs[fam])


def compare(passes: Dict[str, List[Dict[str, List[dict]]]]) -> dict:
    """``passes``: {"acp": [pass, ...], "cli": [pass, ...]}, a pass being
    route -> the relay's records. A key is **noise** when one way's passes
    disagree on it; of the rest, it is **equal** when both ways took the same
    values, else **differing**. {verdict, equal, differing, noise, ignored,
    requests}."""
    obs = {s: [observe(p) for p in passes.get(s) or []] for s in SIDES}
    keys = {fam: sorted({k for s in SIDES for o in obs[s] for f in o[fam] for k in f}) for fam in ("every", "first")}
    equal, differing, noise = [], [], []
    for fam in ("every", "first"):
        for k in keys[fam]:
            per = {s: [_values(o, fam, k) for o in obs[s]] for s in SIDES}
            varies = [s for s in SIDES if len(set(per[s])) > 1]
            if varies:
                noise.append({"field": k, "varies": varies})
                continue
            got = {s: (per[s][0] if per[s] else frozenset()) for s in SIDES}
            if got["acp"] == got["cli"]:
                equal.append(k)
            else:
                differing.append({"field": k, "acp": sorted(got["acp"]), "cli": sorted(got["cli"])})
    seen = {k for s in SIDES for p in passes.get(s) or [] for recs in p.values() for r in recs for k in (r.get("fields") or {})}
    requests = {s: [len(o["every"]) for o in obs[s]] for s in SIDES}
    if not all(requests[s] and all(requests[s]) for s in SIDES):
        verdict = "no requests"
    else:
        verdict = "same" if not differing else "differs"
    return {"verdict": verdict, "equal": equal, "differing": differing, "noise": noise,
            "ignored": [k for k in IGNORED if k in seen], "requests": requests}


def table(result: dict) -> str:
    """The comparison, short, for a shell."""
    lines = [f"requests: acp {result['requests']['acp']}, cli {result['requests']['cli']} (per pass)",
             f"equal: {len(result['equal'])} fields"]
    for d in result["differing"]:
        lines.append(f"DIFFERS {d['field']}:\n    acp: {' | '.join(d['acp']) or ABSENT}\n    cli: {' | '.join(d['cli']) or ABSENT}")
    if result["noise"]:
        lines.append("noise (varies between two runs of the same way): " + ", ".join(n["field"] for n in result["noise"]))
    if result["ignored"]:
        lines.append("left out by name (request metadata, cache keys): " + ", ".join(result["ignored"]))
    lines.append(f"verdict: {result['verdict']}")
    return "\n".join(lines)


def check(agent: dict, *, model: str, provider: str, jobs_root: str = "jobs", passes: int = 2, write: bool = True,
          say: Callable[[str], None] = print, env=None, unsandboxed: bool = False, options: Optional[dict] = None,
          stamp: Optional[str] = None) -> dict:
    """Run the workspace kit's units over ACP and through ``fidelity.command``,
    ``passes`` times each (alternating), compare, and with ``write`` record the
    result in the version's ``proven`` as ``fidelity``. Returns the comparison
    with ``job``, the folder holding ``fidelity.json`` and each pass's records."""
    if agent.get("_kind") != "agent" or "acp" not in (agent.get("start") or {}):
        raise run.Refused(f"{agent.get('label')}: fidelity compares an agent driven over ACP with its own command")
    cmd = (agent.get("fidelity") or {}).get("command")
    if not cmd:
        raise run.Refused(f"{definitions.title(agent)} names no command of its own (fidelity.command) to compare with")
    kit = definitions.kit(KIT_PORT)
    name = f"{stamp or time.strftime('%Y-%m-%d__%H-%M-%S')}__fidelity__{definitions.job_word(agent)}"
    r = run.Run(kit, agent, model=model, provider=provider, jobs_root=jobs_root, name=name, concurrency=1,
                say=say, env=env if env is not None else os.environ, proving=True, unsandboxed=unsandboxed, options=options)
    os.makedirs(os.path.join(r.job_dir, ".work"), exist_ok=True)
    say(f"fidelity {r.job_dir}")
    try:
        r.up = install.upstream(kit)
        r.data = install.dataset(kit, r.ds, r.up)
        r.inst = install.packages(agent)
    except install.InstallFailed as e:
        raise run.Refused(f"could not install what the run needs: {e}")
    units = [str(u) for u in r.driver().request("units", dataset=r.ds["id"], timeout=300)["units"]]
    # One relay for the whole comparison, on this machine: every route's
    # records side by side, with the fields fidelity reads.
    r.relay = relay.Relay(r.base, r.key, os.path.join(r.job_dir, ".work", "relay"), prices=price_list.table(), model=model,
                          side_models=(agent.get("model") or {}).get("side_models") or (),
                          key_header=run.PROVIDERS[provider].get("key_header", "authorization"), fields=True).start()
    order = [(s, n) for n in range(1, passes + 1) for s in SIDES]
    got: Dict[str, List[dict]] = {s: [] for s in SIDES}
    solved: Dict[str, List[str]] = {}
    try:
        for side, n in order:
            routes = {}
            for i, unit in enumerate(units):
                tag = f"{side}{n}-u{i:02d}"
                ok = _one(r, side, unit, i, tag, cmd, os.path.join(r.job_dir, f"{side}{n}", run._slug(unit)))
                routes[tag] = r.relay.calls(tag)
                solved.setdefault(f"{side}{n}", []).append(f"{unit} {'passed' if ok else 'failed'}")
            got[side].append(routes)
            say(f"  {side} pass {n}: " + ", ".join(solved[f'{side}{n}']))
    finally:
        for d in r.drivers:
            d.close()
        r.relay.stop()
    result = dict(compare(got), job=r.job_dir, units=units, solved=solved, options=r.options, model=model, provider=provider,
                  version=str((agent.get("upstream") or {}).get("version") or ""))
    with open(os.path.join(r.job_dir, "fidelity.json"), "w") as f:
        json.dump(result, f, indent=2)
    say(table(result))
    if write:
        rec = {"verdict": result["verdict"], "on": time.strftime("%Y-%m-%d"), "model": model, "provider": provider,
               "version": result["version"], "job": os.path.basename(r.job_dir), "passes": passes,
               "differs": result["differing"]}
        changed = {k: v for k, v in r.options.items() if v != ((agent.get("options") or {}).get(k) or {}).get("default")}
        if changed:
            rec["options"] = changed
        definitions.save(agent, lambda cur: cur.update(proven=dict(cur.get("proven") or {}, fidelity=rec)))
    return result


def _one(r: "run.Run", side: str, unit: str, i: int, tag: str, cmd: List[str], keep: str) -> bool:
    """One unit one way, in the unit's own fixed workspace and home - the same
    paths every pass, so a path in the context is no difference - and its
    agent folder kept under ``keep``. True when the kit scored it a pass."""
    base = os.path.join(r.job_dir, "unit", f"u{i:02d}")
    ws, home = os.path.join(base, "workspace"), os.path.join(base, ".home")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(ws)
    os.makedirs(home)
    subprocess.run(["git", "init", "-q", ws], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    setup = r.driver().request("setup", unit=unit, dir=ws, timeout=600)
    transcript: List[dict] = []
    answer = ""
    try:
        if side == "acp":
            r.relay.unit_url = r.relay.url(tag)
            answer = r.drive_agent(unit, ws, home, tag, setup, transcript, r.relay)[0]
        else:
            answer = _cli(r, cmd, ws, home, tag, setup["prompt"], os.path.join(base, "agent"))
    except (run._Ended, jsonl.Failed) as e:
        r.say(f"  {tag} {unit}: {e}")
    try:
        score = r.driver().request("score", unit=unit, dir=ws, transcript=transcript, answer=answer, timeout=600)
    except jsonl.Failed:
        score = {}
    if os.path.isdir(os.path.join(base, "agent")):
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        shutil.move(os.path.join(base, "agent"), keep)
    return score.get("reward") == 1.0


def _cli(r: "run.Run", cmd: List[str], ws: str, home: str, tag: str, prompt: str, agent_dir: str) -> str:
    a = r.other
    env = run.agent_env(a, r.inst, home, r.relay.url(tag), r.model, r.options)
    words = install.words(cmd, {"here": a["_here"], "install": r.inst, "prompt": prompt})
    words[0] = shutil.which(words[0], path=env["PATH"]) or words[0]
    os.makedirs(agent_dir, exist_ok=True)
    with open(os.path.join(agent_dir, "stdout.txt"), "w") as out, open(os.path.join(agent_dir, "agent.log"), "w") as err:
        try:
            p = run.run_group(words, r.turn_seconds, cwd=ws, env=env, stdout=out, stderr=err, text=True)
        except subprocess.TimeoutExpired:
            raise run._Ended("AgentTimeoutError", f"its command did not end in {r.turn_seconds} s")
    if p.returncode != 0:
        raise run._Ended("NonZeroAgentExitCodeError", f"its command: exit {p.returncode}")
    with open(os.path.join(agent_dir, "stdout.txt")) as f:
        return f.read().strip()
