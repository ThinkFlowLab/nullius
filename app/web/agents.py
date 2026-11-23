"""The Agents, Capabilities and Kits catalogues: the standard's agents and capabilities as cards,
each opening a page of its own - what it has proven, what it can run, and a
check of its ports.

Each entry is its definition in the catalogue (``catalogue/agents/<key>/``,
``catalogue/capabilities/<key>/``, ``docs/standard.md``): its port proofs and benchmark compatibility drive the page. *Prove its ports* runs its conformance
kits as a run of this machine (``standard/recipe.py``, ``conformance``), the
record rewritten by what passes.

A versioned entry is one card; ``?v=`` picks the version its page shows.
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
from nullius.standard import conform, definitions, howto, ports
from nullius.standard import recipe as standard_recipe

from .core import app
from . import shared
from . import execute
from .filters import agent_mark_url
from . import settings as settings_web


def _page(kind: str):
    example = os.path.join(definitions.CATALOGUE, "agents", "reference") if kind == "agent" else \
        os.path.join(definitions.CATALOGUE, "capabilities", "keyword-memory")
    found = standard_recipe.entries(kind, own=True)
    return render_template("agents/standard.html", kind=kind, how=howto.steps(kind), prompts=howto.prompts(),
                           catalogue_dir=paths.catalogue_dir(), example_dir=example, problems=found["problems"],
                           items=[_card(kind, e) for e in found["entries"]], choices=_choices(kind, found["entries"]), only=ONLY.get(kind, {}))


def _choices(kind: str, entries: list) -> dict:
    """A list's filters: the benchmarks each entry runs; for
    agents also how Nullius talks to it, the ports it proves and where it comes from."""
    runs_on = list(dict.fromkeys((f["key"], f["label"]) for e in entries for f in e["fits"]))
    if kind != "agent":
        return {"runs_on": runs_on} if runs_on else {}
    proven = {p["port"] for e in entries for p in e["ports"] if p["state"] == "proven"}
    have = {e["d"].get("from") for e in entries}
    return {"protocol": [(x, x) for x in dict.fromkeys(e["protocol"] for e in entries)],
            "proves": [(p, spec.get("label") or p) for p, spec in ports.PORTS.items() if p in proven],
            "from": [(k, v) for k, v in definitions.FROM.items() if k in have], "runs_on": runs_on}


# The From filter's default: the catalogue's own agents; benchmark-owned agents also show when Runs on selects their benchmark.
ONLY = {"agent": {"from": ["bundled", "yours"]}}


def _runs_on(e: dict) -> list:
    """The benchmarks an entry runs, by its fits: the Runs on filter's values."""
    return [{"text": f["label"], "value": f["key"]} for f in e["fits"] if not f["why"]]


def _card(kind: str, e: dict) -> dict:
    """An agent's or a capability's card on its list (templates/_cards.html). A benchmark's own agent's
    opens its benchmark's page and shows its published models in place of ports, installs and versions."""
    d = e["d"]
    if d.get("from") == "benchmark":
        computer = any(p.startswith("agent.computer/") for b in definitions.load("benchmark")[0]
                       if b["key"] == d["benchmark"] for p in b.get("needs") or [])
        return {"href": url_for("benchmark_page", key=d["benchmark"]), "id": d["_ref"].replace(":", "-"),
                "search": " ".join([d["label"], d["key"], d["benchmark_label"], e["protocol"], d.get("summary", "")]),
                "protocol": e["protocol"], "icon": agent_mark_url(d["key"]), "title": d["label"], "summary": d.get("summary"),
                "from": {"text": d["benchmark_label"], "value": "benchmark"},
                "uses": "Computer use" if computer else None, "models": ", ".join(d.get("models") or []),
                "runs_on": _runs_on(e), "unless": {"from": "runs_on"}, "hide": ["ports", "runs", "installs"]}
    version, n = (d.get("upstream") or {}).get("version"), len(d.get("_versions") or ())
    return {"href": url_for("agent_page" if kind == "agent" else "feature_page", key=d["key"]), "id": d["key"],
            "search": " ".join([d["label"], d["key"], d.get("kind", ""), e["protocol"], d.get("summary", "")]),
            "kind": d.get("kind") if kind == "capability" else None, "protocol": e["protocol"],
            "example": {"text": "example", "title": "Nullius's own, to read or copy - not an agent to compare"} if d.get("example") else None,
            "version": ("v" + str(version) if version else "") + (f" · {n} versions" if n > 1 else ""),
            "icon": agent_mark_url(d["key"]) if kind == "agent" else None, "title": {"text": d["label"], "after": str(version or "")},
            "summary": d.get("summary"), "from": {"text": definitions.FROM[d["from"]], "value": d["from"]} if d.get("from") else None,
            "proves": [{"text": p["label"], "value": p["port"]} for p in e["ports"] if p["state"] == "proven"],
            "ports": "{}/{}".format(sum(p["state"] == "proven" for p in e["ports"]), len(e["ports"])),
            "runs": "{}/{}".format(sum(not f["why"] for f in e["fits"]), len(e["fits"])), "runs_on": _runs_on(e),
            "installs": "yes" if e["install"] else "no"}


def _entry(kind: str, key: str):
    every = standard_recipe.entries(kind, every=True)["entries"]
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
                           title=definitions.title(e["d"]), providers=[(p, providers.label(p)) for p in have],
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
    provider, endpoint = providers.split(request.form.get("provider", ""))
    backend = definitions.names.backend(request.form.get("backend", "local"))
    stamp = time.strftime(conform.STAMP)
    got = standard_recipe.conformance(kind, f"{key}@{version}" if version else key, model=request.form.get("model", "").strip(), provider=provider,
                                      unsandboxed=request.form.get("unsandboxed") == "yes", stamp=stamp, backend=backend)
    problem = " ".join(got["errors"]) or execute.provider_problem(provider, endpoint)
    if not problem and backend == "container":
        problem = execute.docker_problem()
    if problem:
        flash(problem, "error")
        return redirect(back)
    word = f"{key}-{version}" if version else key
    plan = execution.Plan(name=f"check-{word}-{time.strftime('%m%d-%H%M%S')}", script=got["script"], runner=standard_recipe.HARNESS, docker=backend == "container",
                          **execute.credentials_for(provider, endpoint), model=request.form.get("model", "").strip(), jobs=got["jobs"],
                          parts=[{"id": word, "label": got["label"] + "'s kits", "script": got["script"]}])
    problem = plan.check() or (execution.name_taken(plan.name) if os.path.exists(os.path.join(execution.runs_root(), plan.name)) else None)
    if problem:
        flash(problem, "error")
        return redirect(back)
    ex = execution.start(plan)
    shared.adopt_runs([ex])
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
    return render_template("agents/kits.html", items=[_kit_card(e) for e in _kit_entries()], entry=None)


def _kit_card(e: dict) -> dict:
    """A kit's card on its list (templates/_cards.html)."""
    d = e["d"]
    checks = d.get("check_details", {})
    return {"href": url_for("kit_page", key=d["key"]), "keys": d["key"],
            "search": " ".join([e["label"], e["port"], d["summary"], *checks, *(str(v) for c in checks.values() for v in c.values())]),
            "family": d["family"], "version": f"v{e['version']}", "title": e["label"], "port": e["port"],
            "checks": sum(x["units"] for x in d["datasets"])}


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
    return render_template("agents/kits.html", entry=entry)
