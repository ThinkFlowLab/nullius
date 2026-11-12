"""The benchmarks pages: the list, a card per benchmark, and one page per
benchmark folder - its harnesses, its datasets, its limitations, its citation."""
from __future__ import annotations

import base64
import html
import json
import os
import re
import time

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from nullius import execution, paths, providers, secrets, settings
from nullius.catalogue import catalog, citation, harbor_entry, sources, storage
from nullius.standard import definitions as standard_definitions
from nullius.standard import howto, prove
from nullius.standard import recipe as standard_recipe

from .core import app
from . import execute
from . import settings as settings_web
from . import shared


@app.route("/benchmarks")
def benchmarks_page():
    # the benchmarks of the standard some of whose options need a machine's setup (``host``): a bubble on their card
    setup = {b["key"]: sorted(b["host"]) for b in standard_definitions.load("benchmark")[0] if b.get("host")}
    return render_template("catalogue/benchmarks.html", cat=shared._catalog_cached(), origins=catalog.ORIGINS,
                           prompts=howto.prompts(), catalogue_dir=paths.catalogue_dir(), setup=setup)


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
    # where each dataset's data comes from, one row per way it runs - each fetched by its harness, never by hand
    std_def = standard_definitions.find(standard_definitions.load("benchmark")[0], key) if std else None
    ds = cat.datasets_of(b)
    data = {d.key: sources.of(d, b.harness, std_def, only=len(ds) == 1) for d in ds}
    jobs = {j.job_id for j in shared._jobs_for_request()} if std else set()
    return render_template("catalogue/benchmark.html", cat=cat, b=b, csrf=shared._csrf_token(), papers=citation.parse(b.citation), data=data,
                           editable=entry is not None and not harbor_entry.not_editable(entry), std=std, std_jobs=jobs,
                           deletable=os.path.isfile(os.path.join(paths.catalogue_dir(), catalog.BENCHMARKS, key, catalog.ENTRY)),
                           restore_bundle=storage.can_restore("benchmarks", key, catalog.ROOT),
                           local=settings_web._local_client(),
                           providers=[(p, providers.PROVIDER_LABEL[p]) for p in providers.available(os.environ)])


@app.route("/catalogue/<group>/<key>/use-bundled", methods=["POST"])
def catalogue_use_bundled(group, key):
    shared._require_csrf()
    if not settings_web._local_client():
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
    check and its controls (``cli prove``), as an agent's page proves its
    ports; then the first control's job page, which follows the run. The
    controls call no model, so an unjudged benchmark's run is handed no key
    at all (``execution.Plan.keyless``)."""
    shared._require_csrf()
    if not settings_web._local_client():
        abort(403, description="Proofs are started from a browser on the application host only.")
    back = url_for("benchmark_page", key=key) + "#standard"
    limit = request.form.get("limit", "").strip()
    provider = request.form.get("provider", "openai")
    stamp = time.strftime(prove.STAMP)
    got = standard_recipe.proving(key, dataset=request.form.get("dataset", ""), limit=int(limit) if limit.isdigit() else (limit or None),
                                  provider=provider, judge_model=request.form.get("judge_model", "").strip(), stamp=stamp)
    problem = " ".join(got["errors"]) or ("" if got["keyless"] else execute.provider_problem(provider))
    if problem:
        flash(problem, "error")
        return redirect(back)
    plan = execution.Plan(name=f"prove-{key}-{time.strftime('%m%d-%H%M%S')}", script=got["script"], runner=standard_recipe.HARNESS,
                          **execute.credentials_for(provider), keyless=got["keyless"], jobs=got["jobs"],
                          parts=[{"id": key, "label": got["label"] + "'s proofs", "script": got["script"]}])
    problem = plan.check() or (execution.name_taken(plan.name) if os.path.exists(os.path.join(execution.runs_root(), plan.name)) else None)
    if problem:
        flash(problem, "error")
        return redirect(back)
    ex = execution.start(plan)
    shared.adopt_roots([ex.jobs])
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
    if not settings_web._local_client():
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
    if not settings_web._local_client():
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
        symbol = None
        try:
            with open(os.path.join(cat.entry_dir(b.key), "logo.json"), encoding="utf-8") as fh:
                logo = json.load(fh)
            if (isinstance(logo, dict) and logo.get("kind") in LOGO_TYPES.values()
                    and isinstance(logo.get("data"), str) and len(logo["data"]) * 3 // 4 <= LOGO_MAX
                    and re.fullmatch(r"[A-Za-z0-9+/=]+", logo["data"])):
                symbol = (f'<symbol id="bm-{html.escape(b.key, quote=True)}" viewBox="0 0 24 24">'
                          f'<image width="24" height="24" href="data:{logo["kind"]};base64,{logo["data"]}"/></symbol>')
        except (OSError, ValueError):
            pass
        have = re.search(rf'<symbol id="bm-{re.escape(b.key)}"[\s\S]*?</symbol>', text)
        if have:
            if symbol:
                text = text[:have.start()] + symbol + text[have.end():]
        else:
            at = text.rindex("</svg>")
            text = text[:at] + "  " + (symbol or PLAIN_MARK.format(key=html.escape(b.key, quote=True))) + "\n" + text[at:]
    return app.response_class(text, mimetype="image/svg+xml", headers={"Cache-Control": "no-cache"})


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
                           writable=settings_web._local_client(), csrf=shared._csrf_token()), status


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
    return _form({"source": "hub", "origin": "experiment"})


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
    """Remove a Harbor entry: its folder and its mark. The page asks first,
    in place; runs already made from it are not touched."""
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
