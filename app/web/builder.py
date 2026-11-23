"""The run builder: the recipe vocabulary as a page, directory and registry lookups."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from flask import abort, jsonify, render_template, request
from nullius.standard.contract import ports
from nullius import constraints, settings, execution, gitrefs, hosts, prerequisites, providers
from nullius.harbor import source as harbor_source
from nullius.catalogue import catalog
from nullius.recipes import launch as command_builder
from nullius.recipes import store as recipe_store
from nullius.standard.backends import boat
from nullius.standard.contract import definitions
from nullius.standard.requirements import hostreq
from nullius.standard.pages import recipe as standard_recipe

from .core import app
from .filters import agent_mark_url

# Harbor agents that call their vendor with a key of their own, beside the model's (read from Harbor's agents): each
# entry's names are alternatives, any one of them will do.
AGENT_NEEDS = {"cursor-cli": ["CURSOR_API_KEY"], "copilot-cli": ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"],
               "rovodev-cli": ["ROVODEV_USER_API_TOKEN"]}
from . import shared
from . import execute
from . import catalogue


@app.route("/new-run")
def new_run():
    """Build a Harbor recipe; never execute it from this route."""
    data = _builder_data()
    # ?run=<job id>: the recipe a run of this application was started from,
    # opened as it was. ?like=<job id>, the job page's "New run like
    # this": that recipe when the run kept one, else what the job recorded -
    # its launch, its Harbor and what its trials say ran - for the recipe core
    # to rebuild a recipe from (fromJob). ?saved=<id>: a saved recipe, as the
    # Saved recipes dialog loads it. Individual query fields also prefill the form.
    like = request.args.get("like")
    run = request.args.get("run") or (like if like and _kept_recipe(like) else None)
    if run:
        ex = execution.for_job(run)
        if ex is None or not ex.plan.recipe:
            abort(404)
        data["loadRecipe"] = ex.plan.recipe
        data["loadRun"] = run
    elif like:
        job = shared._job_for_request(like)
        if job is None:
            abort(404)
        data["loadLike"] = {"job": job.job_id, "agent": job.agent_name, "launch": job.launch,
                            "harbor": {"version": job.harbor_version, "editable": job.harbor_editable},
                            "env": next((t.env_type for t in job.trials if t.env_type), "")}
    elif request.args.get("saved"):
        data["loadSaved"] = next((x for x in recipe_store.listing() if x["id"] == request.args["saved"]), None)
        if data["loadSaved"] is None:
            abort(404)
    return render_template("builder/new_run.html", builder_data=data)


@app.route("/new-run/benchmarks")
def new_run_benchmarks():
    """The Benchmark step's grid, as a fragment the builder puts in its page: the Benchmarks page's cards
    (``catalogue.benchmark_cards``) as single-choice pick cards (templates/builder/benchmarks.html), or rows with
    ?view=table."""
    cat = shared._catalog_cached()
    # a community benchmark says so
    # a benchmark with versions carries each one's Tasks (the default's under ""), for the builder to show the chosen one's
    items = [dict(i, pick={"value": i["key"], "one": True}, id="pick-bench-" + i["key"], title={"text": i["title"], "title": i["domain"]},
                  origin=i["origin"] if i["shelf"] != "official" else None,
                  data={"tasks": json.dumps({"": i["tasks"], **i["versions"]})} if i["versions"] else None)
             for i in catalogue.benchmark_cards(cat)]
    every = catalogue.benchmark_choices(cat)
    choices = {k: [(v, t) for v, t in every[k] if any(v in _values(i[k]) for i in items)] for k in ("category", "evaluates", "runs")}
    return render_template("builder/benchmarks.html", items=items, choices={k: v for k, v in choices.items() if len(v) > 1},
                           layout="table" if request.args.get("view") == "table" else "cards")


def _values(v) -> list:
    """A card field's filter values: a value's ``value``, else its text."""
    return [x.get("value", x.get("text")) if isinstance(x, dict) else x for x in (v if isinstance(v, list) else [v])]


@app.route("/new-run/agents")
def new_run_agents():
    """The Agent step's grid for one benchmark of the standard (?benchmark=key or key@version), as a fragment the
    builder puts in its page: its agents or capabilities as pick cards (templates/builder/agents.html), or rows with
    ?view=table, from the entries the Agents page lists (``recipe.entries``), each greyed with the reasons it does not fit."""
    benches = definitions.load("benchmark", every=True)[0]
    b = definitions.find(benches, request.args.get("benchmark", ""))
    if b is None:
        abort(404)
    name, b = b["label"], dict(b, label=definitions.title(b))     # its own agents keep its plain name, whatever its version
    kind = b["family"]
    found = [e for e in standard_recipe.entries(kind, benches=[b], every=True, own=True)["entries"] if not e["d"].get("example")]
    found.sort(key=lambda e: 0 if e["d"].get("benchmark") == b["key"] else 2 if e["d"].get("benchmark") else 1)   # its own, the catalogue's, others' own
    # a benchmark with agents of its own runs them as its Original setup; another offers that setup as one more card
    setups = [] if any(e["d"].get("benchmark") == b["key"] for e in found) else [k for k in (b.get("setups") or {}) if k == "original"]
    items = [_setup_card(b, k) for k in setups] + [_pick_card(b, e) for e in found]
    names = dict(definitions.FROM, benchmark=f"{name}'s own", elsewhere="Other benchmarks' agents")
    choices = {"protocol": [(p, p) for p in ("ACP", "A command", "Its own script", "JSON lines")
                            if any(i["protocol"] and i["protocol"]["value"] == p for i in items)],
               "fit": [(v, t) for v, t in (("fit", "Can run it"), ("unfit", "Cannot")) if any(i["fit"]["value"] == v for i in items)],
               "from": [(k, v) for k, v in names.items() if any(i["from"]["value"] == k for i in items)],
               "proves": [(p, s.get("label") or p) for p, s in ports.PORTS.items() if any(x["value"] == p for i in items for x in i["proves"])]}
    # a filter only where its cards differ
    return render_template("builder/agents.html", items=items, kind=kind, choices={k: v for k, v in choices.items() if len(v) > 1},
                           layout="table" if request.args.get("view") == "table" else "cards")


def _setup_card(b: dict, key: str) -> dict:
    """The benchmark as published, run by its own code: a pick card beside the agents or capabilities."""
    x = b["setups"][key]
    title = x.get("reference_agent") or ("No agent · Original" if b["family"] == "agent" else "Original " + _kind_of(b))
    return {"pick": {"value": key}, "id": "pick-" + key, "icon": "#i-origin", "title": {"text": title, "title": x.get("summary", "")},
            "search": " ".join([title, b["label"], "as published"]), "published": "As published", "protocol": "",
            "fit": {"text": "Can run it", "value": "fit"}, "from": {"text": b["label"], "value": "benchmark"},
            "proves": [], "ticks": [], "alert": None}


def _kind_of(b: dict) -> str:
    kinds = sorted({p.split(".", 1)[1].split("/")[0] for p in b.get("needs") or []})
    return kinds[0] if kinds else "capability"


def _pick_card(b: dict, e: dict) -> dict:
    """An agent's or a capability's pick card for the benchmark ``b``: its name and mark, its protocol, where it comes
    from, and a tick for each doubt about a port ``b`` needs (that it proved them is what lets it run ``b``); greyed, why (whole in its tooltip)."""
    d, why = e["d"], e["fits"][0]["why"]
    rows = {r["port"]: r for r in e["ports"] if r["state"] == "proven"}
    needed = [rows[p] for p in b.get("needs") or [] if p in rows]
    title = definitions.title(d)
    version = str((d.get("upstream") or {}).get("version") or "")
    title += f" {version}" if version and version not in title else ""
    fid = (e["fidelity"] or {}).get("verdict")
    ticks = ([{"text": "ACP ≠ CLI", "class": "no", "title": f"ACP differs from its own CLI: {fid}"}] if fid and fid != "same" else []) \
        + [{"text": f"{s['id']} ✗", "class": "no", "title": s["says"]} for r in needed for s in r["skills"] if not s["shown"]] \
        + ([{"text": "older kit", "class": "no", "title": "proven on an older kit: worth running again"}] if any(r["older_kit"] for r in needed) else [])
    lacks = [p.split(".", 1)[-1].rsplit("/v", 1)[0].replace("-", " ").replace("_", " ") for p in b.get("needs") or [] if p not in rows]
    note = ("Belongs to " + d["benchmark_label"]) if d.get("benchmark") else "Lacks " + (", ".join(lacks) or "a port it needs")
    origin = d.get("from") or "bundled"
    there = {"text": d["benchmark_label"], "value": "benchmark" if d["benchmark"] == b["key"] else "elsewhere"} \
        if d.get("benchmark") else {"text": definitions.FROM[origin], "value": origin}
    return {"pick": {"value": d["_ref"], "disabled": bool(why)}, "id": "pick-" + d["_ref"].replace(":", "-").replace("@", "-"),
            "icon": agent_mark_url(d["key"]) if b["family"] == "agent" else "", "title": {"text": title, "title": d.get("summary", "")},
            "search": " ".join([title, d["key"], version, e["protocol"], d.get("summary", "")]),
            "protocol": {"text": e["protocol"], "value": e["protocol"]}, "published": "",
            "fit": {"text": "Cannot" if why else "Can run it", "value": "unfit" if why else "fit"}, "from": there,
            "proves": [{"text": r["label"], "value": r["port"]} for r in rows.values()],
            "ticks": ticks,
            "alert": {"text": note, "title": "; ".join(why)} if why else None}


@app.route("/recipes")
def recipes_page():
    """The saved recipes as a table, a column per step. The page checks each one as the builder checks a loaded
    recipe: a Harbor one in the recipe core, a standard one by the compile of its Review step, done here."""
    items = recipe_store.listing()
    compiled = {x["id"]: _standard_compile(x["recipe"]) for x in items if x["recipe"].get("harness") == standard_recipe.HARNESS}
    return render_template("builder/recipes.html", recipes=items, compiled=compiled, builder_data=_builder_data())


def _builder_data() -> dict:
    """What the recipe core reads (command-builder-core.js): the vocabulary, this machine and Settings."""
    data = command_builder.payload(catalog.load(), shared._load_jobs())
    # Execution sets these credential variables from Settings.
    data["credentialVars"] = {"key": "API_TOKEN", "url": "API_URL"}
    # Where a recipe can run: this machine, or the hosts named in Settings.
    # Their entries only - a key is a path, never read here.
    try:
        data["hosts"] = [{"entry": h.entry, "label": h.label} for h in hosts.parse_list(os.environ.get("HOSTS", ""))]
    except ValueError:
        data["hosts"] = []
    # The providers a run can start on, whose key (an OpenAI-compatible endpoint: its URL) is set in Settings, in the
    # order the Model step prefers them, a named endpoint as custom:<id>, with its name; and whether the endpoint at
    # MODEL_API_URL has a key of its own. Presence and names only.
    data["credentials"] = {"available": providers.available(os.environ), "model_key": bool(os.environ.get("MODEL_API_KEY")),
                           "endpoints": {e["id"]: e["name"] for e in providers.endpoints(os.environ) if e["id"]},
                           "broken": providers.broken(os.environ),           # a URL that holds a credential: fixed in Settings first
                           # the agent keys Settings holds, by name only
                           "agent_keys": sorted(settings.agent_key_values(os.environ))}
    data["agentNeeds"] = AGENT_NEEDS
    # The standard's Where step: why its container backend cannot run on this machine (None: it can); and, per
    # benchmark, the licences its store holds (nullius.secrets) - set, when, until when; never a value.
    # what this machine lacks of each benchmark's host requirements: {benchmark: {requirement: [reasons]}}
    gpus = constraints.of(constraints.LOCAL, strict=False).get("gpu_ids")
    host = {b["key"]: {n: [why] for n, why in hostreq.problems(b, list(b["host"]), gpu_ids=gpus).items()}
            for b in definitions.load("benchmark", every=True)[0] if b.get("host")}
    # boat: why its sandboxes cannot run from here (its key, this machine's programs), else what its account
    # says: the sizes it offers, each with its spec and why it cannot start one, and its VMs at once
    boat_why = boat.unusable()
    account = {} if boat_why else execute.boat_account()
    types = account.get("types") or {}
    data["standardHere"] = {"docker": execute.docker_problem(), "boat": boat_why,
                            "boatSizes": [{"key": k, "label": z["label"], "spec": boat.spec(k), "memory_gb": z.get("memory_gb"),
                                           "why": types.get(k)} for k, z in boat.SIZES.items() if not types or k in types],
                            "boatSlots": account.get("slots"), "boatKey": bool(boat.key()), "licences": _licence_states(), "host": host}
    data["csrf"] = shared._csrf_token()
    data["local"] = shared._local_client()
    data["recipesWhy"] = WRITE_HERE
    return data


@app.route("/new-run/runner", methods=["POST"])
def new_run_runner():
    """A recipe for the Nullius standard, compiled from its definitions: what
    the builder shows while one is edited. Read-only; the same compile happens
    again in /run, from the definitions, never from the page. Any other
    runner is refused: a benchmark's own scripts are its Original setup."""
    shared._require_csrf()
    body = request.get_json(silent=True) or {}
    recipe = body.get("recipe") if isinstance(body.get("recipe"), dict) else {}
    if len(json.dumps(recipe)) > 200_000:
        abort(413)
    if recipe.get("harness") == standard_recipe.HARNESS:
        return _standard_compile(recipe)
    return {"errors": [{"step": providers.STEP["runner"], "message": providers.GONE}],
            "notes": [], "review": []}, 404


def _standard_compile(recipe: dict) -> dict:
    out = standard_recipe.compile(recipe, docker_unusable=execute.docker_problem, boat_unusable=boat.unusable,
                                  boat_size_problem=execute.boat_size_problem, unpriced=execute.unpriced)
    # served_models: each model field's model as the run sends it, which Served by lists the providers of
    return {k: out[k] for k in ("errors", "notes", "review", "jobs", "confirm", "served_models")}


@app.route("/new-run/prerequisites", methods=["POST"])
def new_run_prerequisites():
    """What the recipe needs of each machine it runs on, and whether that
    machine has it (``nullius/prerequisites.py``): {machines: [{where,
    label, rows}], ready, notes}.
    A host is asked over ssh, so this is never asked on a page load."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Machines are checked from a browser on the application host only.")
    body = request.get_json(silent=True) or {}
    recipe = body.get("recipe") if isinstance(body.get("recipe"), dict) else {}
    if len(json.dumps(recipe)) > 200_000:
        abort(413)
    setups = {}
    standard = recipe.get("harness") == standard_recipe.HARNESS
    if standard:
        out = standard_recipe.compile(recipe)
        if out["errors"]:
            return jsonify(machines=[], ready=False, errors=[e["message"] for e in out["errors"]])
        wheres = [sh["where"] for sh in out["shards"]]
        needs = (["engine"] if out["docker"] else []) + (list(boat.PROGRAMS) if recipe.get("backend") == "boat" else [])
        h = out.get("host") or {}
        b = definitions.get("benchmark", h.get("benchmark", "")) if h.get("needs") else None
        setups = {n["name"]: {**n, "install": hostreq.install_lines(b, [n["name"]])}
                  for n in h.get("needs") or []} if b else {}
        host_needs = needs + ["python3", "git"]
    elif recipe.get("harness", "harbor") != "harbor":
        return jsonify(machines=[], ready=False, errors=[providers.GONE])
    else:
        pin, problem = harbor_source.pin_of(recipe.get("harbor"))
        if problem:
            return jsonify(machines=[], ready=False, errors=[problem])
        wheres, needs = [str(recipe.get("where") or "local")], ["docker", "uv" if pin else "harbor"]
        host_needs = needs
    known = {h.entry: h for h in execute._hosts()}

    runner = "standard" if recipe.get("harness") == standard_recipe.HARNESS else "harbor"
    backend = definitions.names.backend(recipe.get("backend"))
    own = runner == "standard" and bool(out["docker"])      # its containers, own images on the local backend included

    def machine(w):
        limits = prerequisites.constraint_rows(w, runner, backend, setups, own=own)
        if w == "local":
            return {"where": w, "label": "this machine", "rows": prerequisites.check_here(needs, setups, standard=standard) + limits}
        if w not in known:
            return {"where": w, "label": w, "rows": [prerequisites.row("connect", "missing", f"{w} is not one of the hosts in Settings", label="Connection")]}
        return {"where": w, "label": known[w].label, "rows": prerequisites.check_host(known[w], host_needs, setups, standard=standard) + limits}
    # one ssh probe per host, all at once: an unreachable host can hold a probe for 8–12 s
    with ThreadPoolExecutor(max(1, min(8, len(wheres)))) as pool:
        machines = list(pool.map(machine, wheres))
    # a notice, not a refusal: the provider's last test in Settings failed (providers.test)
    choice = str(body.get("provider") or "")
    last = providers.tests().get(choice)
    notes = [f"{providers.label(choice)} failed its last test in Settings: {providers.said(last)}"] if isinstance(last, dict) and not last.get("ok") else []
    return jsonify(machines=machines, ready=prerequisites.ready(machines), errors=[], notes=notes)


# The Harbor a recipe pins (nullius/harbor/source.py): PyPI's releases and a
# repository's refs, each read once per process when the read succeeds (refs
# again on ?fresh=1, since a branch moves); the agents a pin registers, kept
# beside its wheels. Nothing here builds on a GET.
_HARBOR_RELEASES: dict = {}
_HARBOR_REFS: dict = {}


@app.route("/new-run/harbor/releases")
def new_run_harbor_releases():
    """The official Harbor's releases on PyPI, newest first, for the
    builder's Harbor panel."""
    if not _HARBOR_RELEASES:
        found = harbor_source.releases()
        if found["error"]:
            return found
        _HARBOR_RELEASES.update(found)
    return _HARBOR_RELEASES


@app.route("/new-run/harbor/refs")
def new_run_harbor_refs():
    """A repository's branches and tags, each at its commit, for a Harbor
    pinned from a repository - a private fork included: git ls-remote with
    this machine's own credentials, for a browser on this machine only."""
    if not shared._local_client():
        abort(403, description="Repositories are listed for a browser on the application host only.")
    url = gitrefs.url_of(request.args.get("repo", ""))
    if not url:
        return {"error": "not a repository: owner/name on GitHub, or a git URL (https, ssh, user@host:path, file)"}, 400
    if url not in _HARBOR_REFS or request.args.get("fresh"):
        found = dict(gitrefs.ls_remote(url), url=url, label=gitrefs.label_of(url))
        if found["error"]:
            return dict(found, error=f"could not list refs: {found['error']}; type a commit")
        _HARBOR_REFS[url] = found
    return _HARBOR_REFS[url]


@app.route("/new-run/harbor/agents", methods=["GET", "POST"])
def new_run_harbor_agents():
    """The agents a pinned Harbor registers. GET reads what a scan kept
    (``state`` ``absent`` when none did); POST - the builder's explicit scan,
    with the form token, from this machine - builds the pin here when it is a
    repository, installs it in a throwaway environment and asks it, once."""
    if request.method == "POST":
        shared._require_csrf()
        if not shared._local_client():
            abort(403, description="A Harbor is built from a browser on the application host only.")
        raw = (request.get_json(silent=True) or {}).get("harbor")
    else:
        raw = {k: request.args.get(k) for k in ("kind", "version", "repo", "revisionKind", "revision", "commit")}
    pin, problem = harbor_source.pin_of(raw)
    if pin is None:
        return {"state": "failed", "agents": None, "error": problem or "no Harbor chosen"}, 400
    return harbor_source.scan(pin, harbor_source.root(execution.runs_root()), build=request.method == "POST")


def _licence_states() -> dict:
    """{benchmark: {name: {set, on, expires, expired}}} for each standard
    benchmark whose environments need licences: what is held of each name
    they declare, a data provider's token in that provider's store."""
    from nullius import secrets
    from nullius.standard.contract import definitions
    out: dict = {}
    for b in definitions.load("benchmark", every=True)[0]:
        for s in definitions.secrets_of(b):
            held = out.setdefault(b["key"], {})
            got = secrets.state(b["key"], s["name"], s.get("provider") or "")
            if got and s["name"] not in held:
                held[s["name"]] = {k: got[k] for k in ("set", "on", "expires", "expired")}
    return out


# Saved recipes (nullius/recipes/store.py): anyone who can open the builder reads them; a browser on this machine
# alone writes them, with the form token.
WRITE_HERE = "Saved recipes change from a browser on the application host only."


def _recipe_write():
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description=WRITE_HERE)
    if request.content_length and request.content_length > 10 * recipe_store.MAX_BYTES:
        abort(413)
    return request.get_json(silent=True)


@app.route("/new-run/recipes")
def recipes_list():
    return jsonify(recipes=recipe_store.listing())


@app.route("/new-run/recipes", methods=["POST"])
def recipes_save():
    body = _recipe_write() or {}
    try:
        return jsonify(ok=True, recipe=recipe_store.save(body.get("recipe"), body.get("id")))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400


@app.route("/new-run/recipes/<recipe_id>", methods=["DELETE"])
def recipes_delete(recipe_id):
    _recipe_write()
    try:
        return jsonify(ok=recipe_store.delete(recipe_id))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400


@app.route("/new-run/recipes/import", methods=["POST"])
def recipes_import():
    """One recipe or a list, or {"recipes": [...], "lenient": true}: each one added unless its name is saved
    already. Lenient adds the good ones and names the bad."""
    body = _recipe_write()
    wrapped = isinstance(body, dict) and "recipes" in body
    items = body["recipes"] if wrapped else body
    items = items if isinstance(items, list) else [items]
    try:
        return jsonify(ok=True, **recipe_store.import_(items, lenient=wrapped and body.get("lenient") is True))
    except ValueError as e:
        return jsonify(ok=False, error="Not imported: " + str(e)), 400


def _kept_recipe(job_id: str) -> bool:
    ex = execution.for_job(job_id)
    return bool(ex and ex.plan.recipe)


def _host_target():
    """The benchmark and its requirements a host check or install is about, and where: 'local' or a host of Settings."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Machines are checked and set up from a browser on the application host only.")
    b = definitions.get("benchmark", request.form.get("benchmark", ""))
    if b is None:
        abort(400, description="no such benchmark")
    names = [n for n in request.form.get("requirements", "").split(",") if n in (b.get("host") or {})]
    entry = request.form.get("entry", "local")
    if entry == "local":
        return b, names, None
    known = {h.entry: h for h in execute._hosts()}
    if entry not in known:
        abort(400, description=f"{entry} is not one of the hosts in Settings")
    return b, names, known[entry]


@app.route("/new-run/host/check", methods=["POST"])
def new_run_host_check():
    """What a machine lacks: {ok, missing: {requirement: [reasons]}, install}.
    Remote GPU memory is checked by the run on its host, not by the SSH probe."""
    b, names, host = _host_target()
    if host is None:
        gone = {n: [why] for n, why in hostreq.problems(b, names, gpu_ids=constraints.of(constraints.LOCAL, strict=False).get("gpu_ids")).items()}
    else:
        programs = [p for r in hostreq.requirements(b, names).values() for p in r["check"]]
        got = hosts.test(host, programs=programs)
        if not got["ok"]:
            return jsonify(ok=False, error=got["error"])
        gone = {n: [p for p in r["check"] if p in got["missing"]] for n, r in hostreq.requirements(b, names).items()}
        gone = {n: [", ".join(ps) + " not on PATH"] for n, ps in gone.items() if ps}
    return jsonify(ok=True, missing=gone, install=hostreq.install_lines(b, list(gone)))


@app.route("/new-run/host/install", methods=["POST"])
def new_run_host_install():
    """The benchmark's install commands for the requirements a machine lacks, run there on an explicit
    choice: apt with ``sudo -n``, so a machine whose sudo asks for a password is refused and told the
    commands to run by hand. The commands come from the definition, never from the form."""
    b, names, host = _host_target()
    script = hostreq.install_script(b, names)
    if script is None:
        return jsonify(ok=True, said="nothing to install")
    argv = ["bash", "-c", script] if host is None else host.argv("bash -c " + shlex.quote(script), timeout=10)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        rc, out = p.returncode, (p.stdout + p.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        rc, out = 124, "no end within 10 minutes"
    if rc != 0:
        why = "sudo needs a password there" if "a password is required" in out or "a terminal is required" in out else out.strip().splitlines()[-1:] or ["failed"]
        return jsonify(ok=False, error=why if isinstance(why, str) else why[0], manual=hostreq.install_lines(b, names))
    return jsonify(ok=True, said="installed")


@app.route("/new-run/dirs")
def new_run_dirs():
    """Directory completions for the local-checkout field. Names only.

    The typed spelling is preserved in the suggestions (a `~` or a relative
    path stays as typed) because the shell, not this app, will resolve it.
    """
    prefix = request.args.get("path", "")
    head, part = os.path.split(os.path.expanduser(prefix))
    try:
        names = sorted(
            n for n in os.listdir(head or ".")
            if n.startswith(part) and (n[:1] != "." or part[:1] == ".")
            and os.path.isdir(os.path.join(head, n))
        )[:20]
    except OSError:
        names = []
    typed_head = os.path.split(prefix)[0]
    return {"dirs": [os.path.join(typed_head, n) if typed_head else n
                     for n in names]}


# Where `harbor run -d name@version` resolves names: the registry file the
# CLI itself defaults to (harbor.constants.DEFAULT_REGISTRY_URL). The CLI's
# own `datasets list` points at the hub
# website, whose org/name namespace has no public JSON — so this file is the
# one machine-readable source of the name@version entries.
RUN_REGISTRY_URL = (
    "https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json"
)

# One fetch per process — a dead network is not retried on
# every view.
_REGISTRY_SCAN: dict = {}


def _fetch_registry_names():
    """The registry's name@version entries, or None when the fetch failed."""
    try:
        req = urllib.request.Request(
            RUN_REGISTRY_URL, headers={"User-Agent": "nullius"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        entries = data.get("datasets", []) if isinstance(data, dict) else data
        return sorted({f"{d['name']}@{d['version']}" for d in entries
                       if d.get("name") and d.get("version")})
    except (OSError, ValueError, KeyError, TypeError):
        return None


# The hub is the newer, larger catalog (org/name, resolves latest unless
# pinned). It has no JSON API — probed 2026-08-18, /api/* all 404 — so its
# names are collected from the paginated HTML listing.
RUN_HUB_URL = "https://hub.harborframework.com/datasets"


def _fetch_hub_names():
    """org/name entries from the hub's paginated listing, or None.

    Walks ?page=N collecting /datasets/org/name links until a page adds
    nothing new; a mid-walk network error keeps what was already collected.
    """
    names: set[str] = set()
    try:
        for page in range(1, 21):
            req = urllib.request.Request(f"{RUN_HUB_URL}?page={page}",
                                         headers={"User-Agent": "nullius"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", "replace")
            # The links sit inside a React Server Components payload with
            # escaped quotes, so match the path itself, not an href attribute.
            found = set(re.findall(r"/datasets/([\w.-]+/[\w.-]+)", html))
            if not found - names:
                break
            names |= found
    except OSError:
        pass
    return sorted(names) or None


@app.route("/new-run/registry")
def new_run_registry():
    """Dataset names from Harbor's two catalogs, best-effort.

    `names` is the registry file's name@version entries (pinned); `hub` is the hub website's org/name entries (resolves
    latest unless pinned).
    """
    if "names" not in _REGISTRY_SCAN:
        _REGISTRY_SCAN["names"] = _fetch_registry_names() or []
        _REGISTRY_SCAN["hub"] = _fetch_hub_names() or []
    return _REGISTRY_SCAN


@app.route("/new-run/endpoints", methods=["POST"])
def new_run_endpoints():
    """The serving providers OpenRouter lists for one model, for the Model step's Served by; asked as the
    model list is, with the key kept on this machine."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403)
    body = request.get_json(silent=True)
    provider = body.get("provider") if isinstance(body, dict) else None
    model = body.get("model") if isinstance(body, dict) else None
    if providers.split(provider)[0] != "openrouter" or not isinstance(model, str) or not model.strip():
        return jsonify(ok=False, endpoints=[], message="Only OpenRouter routes a model to several providers: choose it and a model."), 400
    try:
        return jsonify(ok=True, endpoints=providers.list_endpoints(provider, model))
    except ValueError as error:
        return jsonify(ok=False, endpoints=[], message=str(error)), 502


@app.route("/new-run/models", methods=["POST"])
def new_run_models():
    """Return the selected provider's catalogue, keeping its key on this machine."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403)
    body = request.get_json(silent=True)
    provider = body.get("provider") if isinstance(body, dict) else None
    if providers.split(provider)[0] not in providers.PROVIDERS:
        return jsonify(ok=False, models=[], message="Choose a model provider."), 400
    try:
        return jsonify(ok=True, models=providers.list_models(provider))
    except ValueError as error:
        return jsonify(ok=False, models=[], message=str(error)), 502
