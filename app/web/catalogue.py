"""The benchmarks pages: the list, a card per benchmark, and one page per
benchmark folder - its harnesses, its datasets, its limitations, its citation."""
from __future__ import annotations

import base64
import html
import json
import os
import re
import threading
import time

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from nullius import execution, paths, providers, secrets, settings
from nullius.catalogue import catalog, citation, harbor_entry, sources, storage
from nullius.standard.contract import definitions as standard_definitions
from nullius.standard.pages import howto
from nullius.standard.proofs import prove
from nullius.standard.pages import recipe as standard_recipe
from nullius.standard.runner import sets, units

from .core import app
from . import execute
from . import settings as settings_web
from . import shared


@app.route("/benchmarks")
def benchmarks_page():
    cat = shared._catalog_cached()
    return render_template("catalogue/benchmarks.html", cat=cat, items=benchmark_cards(cat), choices=benchmark_choices(cat),
                           prompts=howto.prompts(), catalogue_dir=paths.catalogue_dir())


RUNS_WITH = {"standard": ("Nullius", "the Nullius standard"), "harbor": ("Harbor", "Harbor")}
# what a benchmark evaluates (``recipe.benchmarks``' family): its agents or its capabilities, as a card and its filter say it
FAMILIES = {"agent": "Agents", "capability": "Capabilities"}


def benchmark_cards(cat) -> list:
    """The benchmarks as cards (templates/_cards.html), one per row of ``recipe.benchmarks``: what the Benchmarks
    page and the builder's Benchmark step both draw."""
    with open(MARKS, encoding="utf-8") as fh:
        sprite = fh.read()
    sheet = url_for("static", filename="catalogue/marks.svg")
    return [_card(row, sheet if f'<symbol id="bm-{row["b"].key}"' in sprite or _user_mark(cat, row["b"]) else None)
            for row in standard_recipe.benchmarks(cat)]


def benchmark_choices(cat) -> dict:
    """The values the benchmarks' cards are filtered by: their category, what runs them, their origin, what they run."""
    return {"category": [(c, c) for c in cat.categories], "runs": [(way, label) for way, (label, _) in RUNS_WITH.items()],
            "origin": [(o, o.capitalize()) for o in catalog.ORIGINS], "evaluates": list(FAMILIES.items())}


def _card(row, sheet) -> dict:
    """A benchmark's card on the list (templates/_cards.html), from a row of ``recipe.benchmarks``: ``sheet`` the
    sprite holding its own mark, none when it has none."""
    b, ds, tasks, unit, needs = row["b"], row["datasets"], row["tasks"], row["unit"], row["host"]
    return {"key": b.key, "href": url_for("benchmark_page", key=b.key), "shelf": b.origin, "keys": " ".join([b.key, *b.datasets]),
            "search": " ".join([b.title, b.category, b.key, *(d.title for d in ds), *b.datasets]),
            "category": b.category, "origin": {"text": b.label, "class": b.origin, "value": b.origin},
            "icon": sheet and f"{sheet}#bm-{b.key}", "title": b.title, "domain": b.domain,
            "runs": [{"text": label, "class": "rt-" + way, "title": "Runs with " + name, "value": way}
                     for way, (label, name) in RUNS_WITH.items() if way in b.harness],
            "needs": needs and {"text": "needs " + ", ".join(needs),
                                "title": f"Some of its options need {', '.join(needs)} installed on the machine it runs on: see its page"},
            "tasks": {"text": tasks, "label": unit.capitalize(), "value": tasks.replace(",", "").split("–")[-1]},
            "versions": {v: {"text": n, "label": u.capitalize()} for v, (n, u) in row["versions"].items()},
            "datasets": {"text": len(ds), "label": "Datasets" if len(ds) != 1 else "Dataset"},
            "evaluates": {"text": FAMILIES[row["family"]], "value": row["family"], "title": f"{row['kind'].capitalize()} capabilities" if row["kind"] else ""},
            "scoring": b.scoring, "blocker": b.blocker[:1].upper() + b.blocker[1:]}


def _proven(std: dict) -> dict:
    """How many of a version's checks (each dataset's units, leak and controls) hold a proof."""
    oracle = any(c["key"] == "oracle" for c in std["controls"])
    checks = [p for d in std["datasets"] for p in [d["proofs"].get("units")] + ([d["proofs"].get("leak")] if oracle else [])
              + [c.get("proven") for c in d["controls"]]]
    return {"proven": sum(1 for p in checks if p), "checks": len(checks)}


@app.route("/benchmarks/<key>")
def benchmark_page(key):
    """One listed benchmark; an entry the list does not show has no page either."""
    cat = shared._catalog_cached()
    b = next((x for x in cat.listed_benchmarks if x.key == key), None)
    if b is None:
        abort(404)
    entry = harbor_entry.entry_of(key)
    # the standard's definition of it, when it has one: its proofs per dataset, and the form that runs cli prove
    std = standard_recipe.page_of(key) if "standard" in b.harness else None
    # A benchmark with versions is the whole page once per version, one shown (``?v=``, the default without it) and the
    # others swapped in place: each its own pin, proofs, setup, options, task counts, data and limitations.
    v = request.args.get("v")
    listed = (std or {}).get("versions") or [{"ref": key, "version": "", "label": "", "default": True}]
    if v and v not in {x["version"] for x in listed if x["version"]}:
        abort(404)
    every = standard_definitions.load("benchmark", every=True)[0] if std else []
    views = []
    for x in listed:
        page = std if x["default"] else standard_recipe.page_of(x["ref"])
        vb, vds = cat.in_version(b, x["version"]) if x["version"] and not x["default"] else (b, cat.datasets_of(b))
        # where each dataset's data comes from, one row per way it runs - each fetched by its harness, never by hand
        std_def = standard_definitions.find(every, x["ref"]) if std else None
        views.append(dict(x, std=page, b=vb, ds=vds, tasks=cat.tasks_of(vb, vds),
                          data={d.key: sources.of(d, b.harness, std_def, only=len(vds) == 1) for d in vds},
                          current=(x["version"] == v) if v else x["default"], **(_proven(page) if page else {})))
    shown = next(x for x in views if x["current"])
    std, ds, data = shown["std"], cat.datasets_of(b), shown["data"]
    versions = views if len(views) > 1 else []
    jobs = {j.job_id for j in shared._jobs_for_request()} if std else set()
    # the user's sets, each with the version it was made at as this benchmark's definition names it
    named = standard_definitions.get("benchmark", b.key) if std else None
    task_sets = [dict(s, version_label=sets.version_label(named, s["version"]) if s["version"] else "") for s in sets.rows(b.key)] if std else []
    return render_template("catalogue/benchmark.html", cat=cat, b=b, csrf=shared._csrf_token(), papers=citation.parse(b.citation), data=data,
                           task_sets=task_sets,
                           editable=entry is not None and not harbor_entry.not_editable(entry), std=std, std_jobs=jobs, versions=versions, views=views,
                           deletable=os.path.isfile(os.path.join(paths.catalogue_dir(), catalog.BENCHMARKS, key, catalog.ENTRY)),
                           restore_bundle=storage.can_restore("benchmarks", key, catalog.ROOT),
                           local=shared._local_client(),
                           providers=[(p, providers.label(p)) for p in providers.available(os.environ)])


# --- a dataset's tasks: browsed, chosen and kept as a task set ---------------------------------

# The listings being prepared, keyed by benchmark, resolved version, dataset and options.
_PREPARING: dict = {}
_PREPARING_LOCK = threading.Lock()


def _std_dataset(key: str, dataset: str, version: str = ""):
    """(the standard's definition of ``key``, at ``version`` or its default, and its dataset) or a 404. The definition
    goes to the listing as the command line gives it, so both read the same cached listing."""
    if version and not standard_definitions._VERSION.match(version):
        abort(404)
    bench = standard_definitions.get("benchmark", f"{key}@{version}" if version else key)
    ds = next((d for d in (bench or {}).get("datasets") or [] if d["id"] == dataset), None)
    if bench is None or ds is None:
        abort(404)
    return bench, ds


# a plan's options are a few names with short values; a longer text is refused before it is parsed
_OPTIONS_MAX = 4096


def _listing_options() -> dict:
    """The benchmark options of the plan the picker lists: a JSON object of strings, refused when it is not one,
    when it is longer than ``_OPTIONS_MAX`` characters or nested past what the parser follows."""
    given = request.args.get("options", "{}")
    if len(given) > _OPTIONS_MAX:
        abort(400)
    try:
        options = json.loads(given)
    except (ValueError, RecursionError):
        abort(400)
    if not isinstance(options, dict) or not all(isinstance(v, str) for v in options.values()):
        abort(400)
    return options


def _listing_key(bench: dict, dataset: str, options: dict):
    return bench["key"], standard_definitions.version_of(bench), dataset, tuple(sorted(options.items()))


def _listing_state(bench: dict, dataset: str, options=None) -> dict:
    """The state function's answer; whether a listing is being made here now, and why the last one failed; and
    ``say``, the line the page shows while the listing is not there."""
    got = units.state(bench, dataset, options)
    with _PREPARING_LOCK:
        job = _PREPARING.get(_listing_key(bench, dataset, options or {})) or {}
    alive = bool(job.get("thread") and job["thread"].is_alive())
    got = dict(got, preparing=alive and got["state"] != "cached", error="" if alive else job.get("error", ""))
    nouns = (bench.get("units") or {}).get("nouns", "tasks")
    fetched = ", then ".join(f["text"] for f in got["fetch"])
    if got["state"] == "cached":
        say = ""
    elif got["error"]:
        say = f"The list of {nouns} could not be made: {got['error']}. Ids can still be pasted."
    elif got["state"] == "refused":
        say = f"The list of {nouns} cannot be made here: {got['reason']}. Ids can still be pasted."
    elif got["state"] == "full":
        say = (f"The list of {nouns} needs the benchmark's own environment on this machine"
               + (f" ({', '.join(f['text'] for f in got['fetch'])})" if got["fetch"] else "")
               + ": run the dataset once, or list it with the units command, then reload. Ids can still be pasted.")
    else:
        say = f"Preparing the list of {nouns}" + (f": fetching {fetched}." if fetched else ".") + " Ids can be pasted meanwhile."
    return dict(got, say=say)


@app.route("/benchmarks/<key>/tasks/<dataset>")
def benchmark_tasks(key, dataset):
    """A dataset's tasks, at the benchmark's default version or ``?version=``: searched, counted by category, ticked
    and kept as a task set; opened from a set (``?set=NAME``), its tasks ticked, at the version it was made at, and
    saved as another set when they change. The page asks for the listing once loaded; this request reads none."""
    version = request.args.get("version", "")
    entry, problem, broken = None, "", ""
    if request.args.get("set"):
        name = request.args["set"]
        try:
            entry = sets.load(key, name)
        except sets.Refused as e:
            # said without the file's path; a file there that cannot be read can still be deleted from the page
            problem, broken = sets.plain(e, key, name), "" if isinstance(e, sets.NotFound) else name
        if entry is not None and entry["dataset"] != dataset:
            return redirect(url_for("benchmark_tasks", key=key, dataset=entry["dataset"], set=entry["name"]))
        if entry is not None and entry.get("version") and not version and standard_definitions.get("benchmark", f"{key}@{entry['version']}"):
            version = entry["version"]            # its tasks as listed at the version it was made at
    bench, ds = _std_dataset(key, dataset, version)
    return render_template("catalogue/tasks.html", bench=bench, title=standard_definitions.title(bench),
                           ds=ds, entry=entry, rev=sets.latest(entry) if entry else None, problem=problem,
                           label=sets.label_of(entry) if entry else "", version=standard_definitions.version_of(bench) or "",
                           made_at=sets.version_label(bench, entry["version"]) if entry and entry.get("version") else "",
                           broken=broken, nouns=(bench.get("units") or {}).get("nouns", "units"),
                           noun=(bench.get("units") or {}).get("noun", "unit"), csrf=shared._csrf_token(),
                           writable=shared._local_client())


@app.route("/benchmarks/<key>/tasks/<dataset>/listing")
def benchmark_tasks_listing(key, dataset):
    """One page of the dataset's tasks for a search and category values, once the listing is in the cache; before
    that, the state function's answer alone: this request never makes a listing."""
    bench, _ = _std_dataset(key, dataset, request.args.get("version", ""))
    options = _listing_options()
    st = _listing_state(bench, dataset, options)
    if st["state"] != "cached":
        return jsonify(ok=True, ready=False, **st)
    cats: dict = {}
    for word in request.args.getlist("cat"):
        field_, eq, value = word.partition("=")
        if eq and field_ and value:
            cats.setdefault(field_, []).append(value)
    page = request.args.get("page", "1")
    limit = request.args.get("limit", "")
    if limit and (not limit.isdigit() or int(limit) < 1):
        return jsonify(ok=False, error="The task limit must be a whole number above zero."), 400
    limit = int(limit) if limit else None
    # ``within=NAME``: the tasks of that set alone, as the run builder's start on specific tasks lists them
    within = None
    if request.args.get("within"):
        try:
            entry = sets.load(key, request.args["within"])
        except sets.Refused as e:
            return jsonify(ok=False, error=sets.plain(e, key, request.args["within"])), 400
        within = sets.latest(entry)["units"]
    try:
        if request.args.get("ids"):           # every match's id, for "select the N that match"
            got = units.find(bench, dataset, options, search=request.args.get("q", ""), categories=cats, size=None, within=within, limit=limit)
            return jsonify(ok=True, ready=True, total=got["total"], ids=[t["id"] for t in got["tasks"]])
        got = units.find(bench, dataset, options, search=request.args.get("q", ""), categories=cats,
                         page=int(page) if page.isdigit() and int(page) >= 1 else 1, within=within, limit=limit)
    except units.Refused as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, ready=True, **got)


@app.route("/benchmarks/<key>/tasks/<dataset>/prepare", methods=["POST"])
def benchmark_tasks_prepare(key, dataset):
    """Make a listing the state function calls light, out of this request: it fetches what the state named and
    starts the light listing command. A listing that needs the driver's full environment is never started here."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Listings are prepared from a browser on the application host only.")
    bench, _ = _std_dataset(key, dataset, request.args.get("version", ""))
    options = _listing_options()
    identity = _listing_key(bench, dataset, options)
    st = units.state(bench, dataset, options)
    if st["state"] != "light":
        return jsonify(ok=st["state"] == "cached", **_listing_state(bench, dataset, options))

    def make():
        try:
            units.listing(bench, dataset, options)
        except (units.Refused, OSError) as e:
            _PREPARING[identity]["error"] = str(e)
    with _PREPARING_LOCK:
        job = _PREPARING.get(identity)
        if not (job and job["thread"].is_alive()):
            job = _PREPARING[identity] = {"thread": threading.Thread(target=make, daemon=True), "error": ""}
            job["thread"].start()
    return jsonify(ok=True, **_listing_state(bench, dataset, options))


@app.route("/benchmarks/<key>/tasks/<dataset>/rows", methods=["POST"])
def benchmark_tasks_rows(key, dataset):
    """The chosen ids as rows (show selected, ids pasted): each with its title and categories when the listing is in
    the cache, and ``unknown``, the ids it does not list; without the listing, the ids alone."""
    bench, _ = _std_dataset(key, dataset, request.args.get("version", ""))
    options = _listing_options()
    ids = [str(u) for u in (request.get_json(silent=True) or {}).get("units") or [] if isinstance(u, str)]
    if units.state(bench, dataset, options)["state"] != "cached":
        return jsonify(ok=True, ready=False, tasks=[{"id": u, "title": "", "text": "", "categories": {}} for u in ids], unknown=[])
    have = {t["id"]: t for t in units.listing(bench, dataset, options)["tasks"]}
    return jsonify(ok=True, ready=True, tasks=[have[u] for u in ids if u in have], unknown=[u for u in ids if u not in have])


def _set_problem(key: str, name: str) -> str:
    """Why ``name`` cannot name a task set, in a line for the page, or "". Any words do: the name commands take is
    made from them."""
    if not name.strip():
        return "Give the task set a name."
    try:
        sets.check_label(name)
    except sets.Refused as e:
        return str(e)[0].upper() + str(e)[1:] + "."
    return ""


@app.route("/benchmarks/<key>/tasks/<dataset>/save", methods=["POST"])
def benchmark_tasks_save(key, dataset):
    """Keep the ticked ids as a new task set ({name, description, units, version}), at the version the page listed:
    the name typed is its label, and the name commands take is made from it. A set is never changed: the page of
    one saves another."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Task sets are saved from a browser on the application host only.")
    body = request.get_json(silent=True) or {}
    bench, _ = _std_dataset(key, dataset, str(body.get("version") or ""))
    ids = [u for u in body.get("units") or [] if isinstance(u, str)]
    if not ids:
        return jsonify(ok=False, error="Tick at least one task."), 400
    label = str(body.get("name") or "").strip()
    problem = _set_problem(key, label)
    if problem:
        return jsonify(ok=False, error=problem), 400
    try:
        entry = sets.create(bench, label, dataset=dataset, units=ids, description=str(body.get("description") or "").strip(),
                            note=str(body.get("note") or ""))
    except sets.Refused as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, name=entry["name"], open=url_for("benchmark_tasks", key=key, dataset=dataset, set=entry["name"]))


def _set_guard(key: str, dataset: str) -> str:
    """Every change to a set from its page: the form token and a browser on this machine; the set's name."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Task sets are changed from a browser on the application host only.")
    _std_dataset(key, dataset)
    return (request.form.get("set") or "").strip()


@app.route("/benchmarks/<key>/tasks/<dataset>/rename", methods=["POST"])
def benchmark_tasks_rename(key, dataset):
    """Give the set another name, its id and tasks kept (a job that ran it finds it by its id); the name commands
    take is made from it again. Then its page under the new name. An empty name, one not allowed or its own is
    refused."""
    name = _set_guard(key, dataset)
    new = (request.form.get("name") or "").strip()
    try:
        was = sets.label_of(sets.load(key, name))
        problem = f"The task set is named {was} already." if new and new == was else _set_problem(key, new)
        if problem:
            flash(problem, "error")
            return redirect(url_for("benchmark_tasks", key=key, dataset=dataset, set=name))
        entry = sets.rename(key, name, new)
    except sets.Refused as e:
        flash(sets.plain(e, key, name), "error")
        return redirect(url_for("benchmark_tasks", key=key, dataset=dataset, set=name))
    flash(f"Renamed {was} to {entry['label']}. Runs made from it follow the new name.", "ok")
    return redirect(url_for("benchmark_tasks", key=key, dataset=entry["dataset"], set=entry["name"]))


@app.route("/benchmarks/<key>/tasks/<dataset>/delete", methods=["POST"])
def benchmark_tasks_delete(key, dataset):
    """Remove the set from the catalogue; then the benchmark's task sets. A job that ran it keeps its record and names
    it without a link."""
    name = _set_guard(key, dataset)
    try:
        gone = sets.delete(key, name)
    except sets.Refused as e:
        flash(sets.plain(e, key, name), "error")
        return redirect(url_for("benchmark_tasks", key=key, dataset=dataset, set=name))
    flash(f"Deleted the task set {sets.label_of(gone) if gone else name}. Runs made from it stay.", "ok")
    return redirect(url_for("benchmark_page", key=key, _anchor="task-sets"))


@app.route("/catalogue/<group>/<key>/use-bundled", methods=["POST"])
def catalogue_use_bundled(group, key):
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Catalogue entries are changed from a browser on the application host only.")
    endpoints = {"benchmarks": "benchmark_page", "agents": "agent_page", "capabilities": "feature_page"}
    if group not in endpoints:
        abort(404)
    bundled = catalog.ROOT if group == "benchmarks" else standard_definitions.CATALOGUE
    try:
        storage.use_bundled(group, key, bundled)
    except (ValueError, OSError) as error:
        abort(409, description=str(error))
    flash("Using the bundled entry. Saved runs and results are unchanged.", "ok")
    return redirect(url_for(endpoints[group], key=key))


@app.route("/benchmarks/<key>/prove", methods=["POST"])
def benchmark_prove(key):
    """Prove a benchmark of the standard, here, as a run - its units, the leak
    check and its controls (``cli prove``); then the first control's job page, which follows the run. The
    controls call no model, so an unjudged benchmark's run is handed no key
    at all (``execution.Plan.keyless``)."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Proofs are started from a browser on the application host only.")
    version = request.form.get("version", "")
    back = url_for("benchmark_page", key=key, **({"v": version} if version else {})) + "#standard"
    limit = request.form.get("limit", "").strip()
    provider, endpoint = providers.split(request.form.get("provider", "openai"))
    stamp = time.strftime(prove.STAMP)
    got = standard_recipe.proving(f"{key}@{version}" if version else key, dataset=request.form.get("dataset", ""), limit=int(limit) if limit.isdigit() else (limit or None),
                                  provider=provider, judge_model=request.form.get("judge_model", "").strip(), stamp=stamp)
    problem = " ".join(got["errors"]) or ("" if got["keyless"] else execute.provider_problem(provider, endpoint))
    if problem:
        flash(problem, "error")
        return redirect(back)
    plan = execution.Plan(name=f"prove-{key}-{time.strftime('%m%d-%H%M%S')}", script=got["script"], runner=standard_recipe.HARNESS,
                          **execute.credentials_for(provider, endpoint), keyless=got["keyless"], jobs=got["jobs"],
                          parts=[{"id": key, "label": got["label"] + "'s proofs", "script": got["script"]}])
    _, problem, _ = execute.begin([plan])
    if problem:
        flash(problem, "error")
        return redirect(back)
    flash(f"Proving {got['label']}: its units and the leak check, then " + (f"{len(got['jobs'])} control runs." if got["jobs"] else "no control: it declares none."), "ok")
    return redirect(url_for("job", job_id=got["jobs"][0]["name"]) if got["jobs"] else back)


@app.route("/benchmarks/<key>/licences", methods=["POST"])
def benchmark_licence(key):
    """Set, replace, remove or move one of a benchmark's credentials, kept in its
    own store (``nullius.secrets``), managed in Settings: a key or a server's
    address from the text field, a licence file from the upload. Only a name
    the benchmark's environments declare, as the kind they declare it, never
    a data provider's token (Settings → Data providers). A
    browser on this machine only, with the form token, as Settings. *Move
    from saved settings* takes a value saved settings still hold into the
    store and out of the file - on this click, never by itself. Nothing is
    ever sent back of a value."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Credentials are set from a browser on the application host only.")
    back = settings_web._settings_return() + "#access-" + key
    name, action = request.form.get("name", ""), request.form.get("action", "set")
    declared = {s["name"]: s for e in standard_recipe.licences_of(key) for s in e["needs"]}
    s = declared.get(name)
    if s is None or s.get("provider"):
        flash(f"{name} is set in Settings → Data providers." if s else f"{name or 'That'} is not a credential this benchmark needs.", "error")
        return redirect(back)
    try:
        if action == "delete":
            gone = secrets.delete(key, name)
            flash(f"{name} removed." if gone else f"{name} was not set.", "ok" if gone else "error")
        elif action == "move":
            value = settings.in_file(shared.ENV_FILE).get(name)
            if not value:
                flash(f"Saved settings hold no {name}.", "error")
                return redirect(back)
            secrets.set(key, name, value, kind=s["kind"])
            settings.take(shared.ENV_FILE, name)
            flash(f"{name} moved out of saved settings.", "ok")
        else:
            expires = request.form.get("expires", "").strip()
            if s["kind"] == "file":
                f = request.files.get("file")
                data = f.read(secrets.MAX_BYTES + 1) if f else b""
                if not data:
                    flash(f"{name}: choose the licence file.", "error")
                    return redirect(back)
                secrets.set(key, name, data, kind="file", filename=f.filename or "", expires=expires)
            else:
                secrets.set(key, name, request.form.get("value", ""), kind=s["kind"], expires=expires)
            flash(f"{name} set.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(back)


# --- adding a Harbor benchmark by hand ----------------------------------------------

MARKS = os.path.join(shared._APP_DIR, "static", "catalogue", "marks.svg")
LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}
LOGO_MAX = 200_000
# the mark of a benchmark added without a logo: a box of tasks, in the current colour
PLAIN_MARK = ('<symbol id="bm-{key}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
              'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/>'
              '<path d="M7 9h10M7 13h10M7 17h6"/></symbol>')


def _guard():
    """Every write here: form token, and a browser on this machine - adding a
    benchmark writes into the user's catalogue."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Benchmarks are added from a browser on the application host only.")


def _set_mark(key: str, logo) -> None:
    """Keep an uploaded logo beside the user entry; no logo keeps its mark."""
    if logo is not None:
        harbor_entry._write(os.path.join(catalog.writable_dir(key), "logo.json"), logo)


@app.route("/static/catalogue/marks.svg")
def benchmark_marks():
    """The bundled sprite with user logos and a fallback for custom entries.

    The exact route precedes Flask's generic static-file route, so the
    builder and every catalogue page keep their existing sprite URL.
    """
    with open(MARKS, encoding="utf-8") as fh:
        text = fh.read()
    cat = shared._catalog_cached()
    for b in cat.benchmarks:
        symbol = _user_mark(cat, b)
        have = re.search(rf'<symbol id="bm-{re.escape(b.key)}"[\s\S]*?</symbol>', text)
        if have:
            if symbol:
                text = text[:have.start()] + symbol + text[have.end():]
        else:
            at = text.rindex("</svg>")
            text = text[:at] + "  " + (symbol or PLAIN_MARK.format(key=html.escape(b.key, quote=True))) + "\n" + text[at:]
    return app.response_class(text, mimetype="image/svg+xml", headers={"Cache-Control": "no-cache"})


def _user_mark(cat, b):
    """The sprite's symbol of the logo kept beside ``b``'s user entry, or None."""
    try:
        with open(os.path.join(cat.entry_dir(b.key), "logo.json"), encoding="utf-8") as fh:
            logo = json.load(fh)
        if (isinstance(logo, dict) and logo.get("kind") in LOGO_TYPES.values()
                and isinstance(logo.get("data"), str) and len(logo["data"]) * 3 // 4 <= LOGO_MAX
                and re.fullmatch(r"[A-Za-z0-9+/=]+", logo["data"])):
            return (f'<symbol id="bm-{html.escape(b.key, quote=True)}" viewBox="0 0 24 24">'
                    f'<image width="24" height="24" href="data:{logo["kind"]};base64,{logo["data"]}"/></symbol>')
    except (OSError, ValueError):
        pass
    return None


def _logo(values: dict):
    """``({kind, data, name} or None, problem or None)``: the file chosen now,
    else the one a save that came back kept in the form (``logo_kept``)."""
    up = request.files.get("logo")
    if up is not None and up.filename:
        kind = LOGO_TYPES.get(os.path.splitext(up.filename)[1].lower())
        if kind is None:
            return None, "logo: a PNG, JPEG or SVG file"
        raw = up.read()
        if len(raw) > LOGO_MAX:
            return None, f"logo: {len(raw) // 1000} KB; keep it under {LOGO_MAX // 1000} KB (a 64 px image is plenty)"
        return {"kind": kind, "data": base64.b64encode(raw).decode(), "name": os.path.basename(up.filename)}, None
    kept = re.match(r"^data:(image/png|image/jpeg|image/svg\+xml);base64,([A-Za-z0-9+/=]+)$", values.get("logo_kept") or "")
    if kept and len(kept.group(2)) * 3 // 4 <= LOGO_MAX:
        return {"kind": kept.group(1), "data": kept.group(2), "name": values.get("logo_kept_name") or "the logo chosen"}, None
    return None, None


def _form(values, errors=(), status=200, editing=None):
    """The add form, or the same form editing ``editing`` (an entry's key)."""
    cat = shared._catalog_cached()
    return render_template("catalogue/new.html", cat=cat, values=values, errors=list(errors), editing=editing, routable=_routable(),
                           origins=catalog.ORIGINS, catalogue_dir=paths.catalogue_dir(),
                           writable=shared._local_client(), csrf=shared._csrf_token()), status


def _back(values, errors, logo, editing=None):
    """The form again, with what to fix and everything typed - the logo too,
    which a browser never puts back in a file field."""
    if logo is not None:
        values = dict(values, logo_kept=f"data:{logo['kind']};base64,{logo['data']}", logo_kept_name=logo["name"])
    return _form(values, errors, 400, editing)


def _opened(key: str):
    listed = any(b.key == key for b in shared._catalog_cached().listed_benchmarks)
    return redirect(url_for("benchmark_page", key=key) if listed else url_for("benchmarks_page"))


@app.route("/benchmarks/new", methods=["GET"])
def benchmark_new():
    """The form that adds a Harbor benchmark to the catalogue."""
    return _form({"source": "hub", "origin": "official"})


@app.route("/benchmarks/new/read", methods=["POST"])
def benchmark_new_read():
    """What Harbor says of a dataset, to fill the form: its task count and
    the limits its tasks declare. Asked on the button, never on a page load."""
    _guard()
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(ok=True, **harbor_entry.read(str(body.get("source") or ""), str(body.get("ref") or "")))
    except harbor_entry.ReadError as e:
        return jsonify(ok=False, error=str(e)), 400


@app.route("/benchmarks/new", methods=["POST"])
def benchmark_new_save():
    """Write the entry, and its mark, then open its page. Nothing is written
    while anything is wrong: the form comes back with what to fix."""
    _guard()
    values = request.form.to_dict()
    route_for = (values.get("route_for") or "").strip()
    if route_for:
        return _add_route(route_for, values)
    logo, bad_logo = _logo(values)
    entry = harbor_entry.build(values)
    errors = harbor_entry.problems(entry)
    if entry["key"] == "new":
        errors.append("key: new is the address of this form")
    if bad_logo:
        errors.append(bad_logo)
    if errors:
        return _back(values, errors, logo)
    harbor_entry.save(entry)
    _set_mark(entry["key"], logo)
    return _opened(entry["key"])


def _routable() -> list:
    """The benchmarks a Harbor route can be added to: listed, and run so far only on the Nullius standard."""
    out = []
    for b in shared._catalog_cached().benchmarks:
        if "harbor" not in b.harness:
            out.append((b.key, b.title))
    return out


def _add_route(key: str, values: dict):
    """The form's dataset as a Harbor route of an existing benchmark: one
    entry, one card, run with Harbor or on the Nullius standard."""
    old = harbor_entry.entry_of(key)
    if old is None:
        abort(404)
    if "harbor" in (old.get("harness") or []):
        return _form(values, [f"{old.get('title', key)} already runs with Harbor: edit it from its page"], 400)
    entry = harbor_entry.harbor_route(old, values)
    errors = harbor_entry.problems(entry, replacing=key, dataset=-1)
    if errors:
        return _form(values, errors, 400)
    harbor_entry.update(entry, was=key, dataset=-1)
    flash(f"{old.get('title', key)} now runs with Harbor too.", "ok")
    return _opened(key)


def _editable(key: str) -> dict:
    entry = harbor_entry.entry_of(key)
    if entry is None:
        abort(404)
    why = harbor_entry.not_editable(entry)
    if why:
        abort(409, description=f"This entry is not edited here: {why}.")
    return entry


@app.route("/benchmarks/<key>/edit", methods=["GET"])
def benchmark_edit(key):
    """The add form, filled with a Harbor entry, to change it."""
    return _form(harbor_entry.values_of(_editable(key)), editing=key)


@app.route("/benchmarks/<key>/edit", methods=["POST"])
def benchmark_edit_save(key):
    """Write the changes over the entry, keeping all it holds that the form
    does not show; a logo chosen replaces its mark. Nothing is written while
    anything is wrong."""
    _guard()
    old = _editable(key)
    values = request.form.to_dict()
    logo, bad_logo = _logo(values)
    entry = dict(harbor_entry.merged(old, harbor_entry.build(values)), key=values.get("key", "").strip() or key)
    errors = harbor_entry.problems(entry, replacing=key)
    if entry["key"] == "new":
        errors.append("key: new is the address of the add form")
    if bad_logo:
        errors.append(bad_logo)
    if errors:
        return _back(values, errors, logo, editing=key)
    harbor_entry.update(entry, was=key)
    _set_mark(entry["key"], logo)
    return _opened(entry["key"])


@app.route("/benchmarks/<key>/delete", methods=["POST"])
def benchmark_delete(key):
    """Remove a Harbor entry: its folder and its mark. The runs already made from it are not touched."""
    _guard()
    entry = _editable(key)
    try:
        harbor_entry.delete(key)
    except ValueError as e:
        abort(409, description=str(e))
    restored = harbor_entry.entry_of(key) is not None
    flash(f"Deleted {entry.get('title') or key} from your catalogue; "
          + ("the bundled entry is shown again. " if restored else "") + "Runs made from it are untouched.", "ok")
    return redirect(url_for("benchmarks_page"))
