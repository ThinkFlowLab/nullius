"""The Agents, Capabilities and Kits catalogues: the standard's agents and capabilities as cards,
each opening a page of its own - what it has proven, what it can run, and a
check of its ports.

Each entry is its definition in the catalogue (``catalogue/agents/<key>/``,
``catalogue/capabilities/<key>/``, ``docs/standard.md``): how Nullius talks to it,
what it installs, the ports it offers with the record of the kit that proved
each - the skills of that kit it showed or not, and the job that shows it,
when the open workspace holds that job - and
every benchmark of its family, the ones it fits linked to a builder recipe for
the pair and the others with the reasons. *Prove its ports* runs its conformance
kits as a run of this machine (``standard/recipe.py``, ``conformance``), the
record rewritten by what passes.

An agent or a capability with several versions is one card, its default
version's; its page shows one version at a time (``?v=``, the default
without it), with the list of its versions and what each has proven.
"""
from __future__ import annotations

import io
import keyword
import os
import tokenize
import time
from pathlib import Path

from flask import abort, flash, redirect, render_template, request, url_for
from nullius import execution, paths, providers
from nullius.catalogue import storage
from nullius.standard import conform, definitions, howto, match, ports
from nullius.standard import recipe as standard_recipe

from .core import app
from . import shared
from . import execute
from . import settings as settings_web


def _install_words(o: dict) -> list:
    """What a definition installs, in a few words each."""
    inst, up = o.get("install") or {}, o.get("upstream") or {}
    out = [f"npm {p}" for p in inst.get("npm") or []] + [f"pip {p}" for p in inst.get("pip") or []]
    if inst.get("project"):
        extras = "[" + ",".join(inst["extras"]) + "]" if inst.get("extras") else ""
        out.append(f"{inst['project']}{extras} from {up.get('repository', '').removeprefix('https://')} at {(up['tag'] + ' (' + str(up.get('tested', ''))[:12] + ')') if up.get('tag') else str(up.get('tested', ''))[:12]}")
    if inst.get("python"):
        out.append(f"on Python {inst['python']}")
    out += [f"{', '.join(x['packages'])} from {x['url'].removeprefix('https://')}" for x in inst.get("indexes") or []]
    out += [f"the model {m}" for m in inst.get("prefetch") or []]
    return out


def _entries(kind: str, every: bool = False) -> dict:
    others, bad = definitions.load(kind, every=every)
    benches = [b for b in definitions.load("benchmark")[0] if b["family"] == kind]
    jobs = {j.job_id for j in shared._jobs_for_request()}
    out = []
    for o in others:
        cand = set(conform.candidates(o))
        rows = []
        for p, spec in ports.PORTS.items():
            if ports.family(p) != kind or (kind == "capability" and p.split(".", 1)[1].split("/")[0] != o.get("kind")):
                continue
            rec = (o.get("proven") or {}).get(p) if p in (o.get("offers") or []) else None
            state = "proven" if rec else ("not offered" if p in cand else ("no kit yet" if not spec["kit"] else "not for it"))
            kit = definitions.kit(p) if spec["kit"] else None
            rows.append({"port": p, "label": spec.get("label") or p, "summary": spec["summary"], "state": state, "record": rec,
                         "proof_kit": definitions.proof_kit(rec) if rec else None,
                         "kit": {"says": kit["summary"], "checks": kit["datasets"][0]["units"], "key": kit["key"], "version": definitions.kit_version(kit)} if kit else None,
                         "job": rec["job"] if rec and rec.get("job") in jobs else None,
                         "older_kit": definitions.older_kit(p, rec),
                         "skills": standard_recipe.skills_of(p, rec)})
        fits = [{"key": b["key"], "label": b["label"], "needs": b["needs"], "why": match.fit(b, o)} for b in benches]
        fid = (o.get("proven") or {}).get("fidelity")
        out.append({"d": o, "protocol": standard_recipe.protocol(o), "ports": rows, "fits": fits,
                    "install": _install_words(o),
                    # its options, each with its default and what else it takes; its fidelity proof, when it has one
                    "options": [{"name": n, "summary": x.get("summary", ""), "default": x.get("default"),
                                 "values": list(x.get("values") or []), "pattern": x.get("pattern", "")}
                                for n, x in (o.get("options") or {}).items()],
                    "fidelity": fid if isinstance(fid, dict) else None})
    out.sort(key=lambda e: (not e["d"].get("example", False), definitions.title(e["d"]).casefold()))
    return {"entries": out, "problems": bad}


def _page(kind: str):
    example = os.path.join(definitions.CATALOGUE, "agents", "reference") if kind == "agent" else \
        os.path.join(definitions.CATALOGUE, "capabilities", "keyword-memory")
    return render_template("agents/standard.html", kind=kind, how=howto.steps(kind), prompts=howto.prompts(),
                           catalogue_dir=paths.catalogue_dir(), example_dir=example, **_entries(kind))


def _entry(kind: str, key: str):
    every = _entries(kind, every=True)["entries"]
    mine = [x for x in every if x["d"]["key"] == key]
    v = request.args.get("v")
    e = next((x for x in mine if x["d"]["_version"] == v), None) if v else \
        next((x for x in mine if x["d"]["_version"] == x["d"]["_default"]), None)
    if e is None:
        abort(404)
    versions = [{"v": x["d"]["_version"], "default": x["d"]["_version"] == x["d"]["_default"], "current": x is e,
                 "proven": sum(1 for r in x["ports"] if r["state"] == "proven"), "ports": len(x["ports"])} for x in mine]
    have = providers.available(os.environ)
    return render_template("agents/entry.html", kind=kind, e=e, versions=versions if len(mine) > 1 else [],
                           restore_bundle=storage.can_restore(definitions.KINDS[kind]["dir"], key, definitions.CATALOGUE),
                           title=definitions.title(e["d"]), providers=[(p, providers.PROVIDER_LABEL[p]) for p in have],
                           local=settings_web._local_client(), csrf=shared._csrf_token())


@app.route("/agents")
def agents_page():
    return _page("agent")


@app.route("/features")
@app.route("/capabilities")
def features_page():
    return _page("capability")


@app.route("/agents/<key>")
def agent_page(key):
    return _entry("agent", key)


@app.route("/features/<key>")
@app.route("/capabilities/<key>")
def feature_page(key):
    return _entry("capability", key)


def _check(kind: str, key: str):
    """Start the conformance kits of one agent or capability, here, as a run;
    then its first kit's job page, which follows the run."""
    shared._require_csrf()
    if not settings_web._local_client():
        abort(403, description="Checks are started from a browser on the application host only.")
    version = request.form.get("version", "")
    back = url_for("agent_page" if kind == "agent" else "feature_page", key=key, **({"v": version} if version else {}))
    provider = request.form.get("provider", "")
    backend = definitions.names.backend(request.form.get("backend", "local"))
    stamp = time.strftime(conform.STAMP)
    got = standard_recipe.conformance(kind, f"{key}@{version}" if version else key, model=request.form.get("model", "").strip(), provider=provider,
                                      unsandboxed=request.form.get("unsandboxed") == "yes", stamp=stamp, backend=backend)
    problem = " ".join(got["errors"]) or execute.provider_problem(provider)
    if not problem and backend == "container":
        problem = execute.docker_problem()
    if problem:
        flash(problem, "error")
        return redirect(back)
    word = f"{key}-{version}" if version else key
    plan = execution.Plan(name=f"check-{word}-{time.strftime('%m%d-%H%M%S')}", script=got["script"], runner=standard_recipe.HARNESS, docker=backend == "container",
                          **execute.credentials_for(provider), model=request.form.get("model", "").strip(), jobs=got["jobs"],
                          parts=[{"id": word, "label": got["label"] + "'s kits", "script": got["script"]}])
    problem = plan.check() or (execution.name_taken(plan.name) if os.path.exists(os.path.join(execution.runs_root(), plan.name)) else None)
    if problem:
        flash(problem, "error")
        return redirect(back)
    ex = execution.start(plan)
    shared.adopt_roots([ex.jobs])
    n = len(got["jobs"])
    flash(f"Checking {got['label']}'s ports: " + (f"{n} kits, one after the other." if n > 1 else "its one kit."), "ok")
    return redirect(url_for("job", job_id=got["jobs"][0]["name"]))


@app.route("/agents/<key>/check", methods=["POST"])
def agent_check(key):
    return _check("agent", key)


@app.route("/features/<key>/check", methods=["POST"])
@app.route("/capabilities/<key>/check", methods=["POST"])
def feature_check(key):
    return _check("capability", key)


def _kit_entries():
    shipped = {k["key"]: k for k in definitions.kits()}
    return [dict(port=port, label=p["label"], d=shipped[p["kit"]],
                 version=definitions.kit_version(shipped[p["kit"]]))
            for port, p in ports.PORTS.items() if p.get("kit") in shipped]


@app.route("/kits")
def kits_page():
    return render_template("agents/kits.html", entries=_kit_entries(), entry=None)


def _python_tokens(code):
    """Colour source without executing it or introducing trusted HTML."""
    offsets = [0]
    for line in code.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    chunks, cursor = [], 0
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, IndentationError):
        return [{"kind": "", "text": code}]
    for token in tokens:
        start = offsets[min(token.start[0] - 1, len(offsets) - 1)] + token.start[1]
        end = offsets[min(token.end[0] - 1, len(offsets) - 1)] + token.end[1]
        if end <= start or start < cursor:
            continue
        if start > cursor:
            chunks.append({"kind": "", "text": code[cursor:start]})
        kind = ("comment" if token.type == tokenize.COMMENT else
                "string" if token.type == tokenize.STRING or tokenize.tok_name[token.type].startswith("FSTRING") else
                "number" if token.type == tokenize.NUMBER else
                "keyword" if token.type == tokenize.NAME and keyword.iskeyword(token.string) else
                "operator" if token.type == tokenize.OP else "")
        chunks.append({"kind": kind, "text": code[start:end]})
        cursor = end
    if cursor < len(code):
        chunks.append({"kind": "", "text": code[cursor:]})
    return chunks


@app.route("/kits/<key>")
def kit_page(key):
    entry = next((e for e in _kit_entries() if e["d"]["key"] == key), None)
    if entry is None:
        abort(404)
    # Read only shipped source files after resolving a known kit; never run a driver to render a page.
    folder = Path(definitions.KITS) / entry["d"]["key"]
    entry["sources"] = [{"name": source.name, "code": source.read_text(encoding="utf-8")}
                        for source in sorted(folder.glob("*.py"))]
    for source in entry["sources"]:
        source["tokens"] = _python_tokens(source["code"])
    return render_template("agents/kits.html", entry=entry, entries=[])
