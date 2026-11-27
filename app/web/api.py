"""The HTTP API, ``/api/v1``: what the pages read and the builder runs, for a
program on another machine (``nullius.client``, ``docs/api.md``).

Every route takes a token (``nullius.access``) in ``Authorization: Bearer``,
never a cookie or a query argument: a page in a browser cannot send one, so
these routes take neither the form token nor the loopback rule, and the
served-name rule (``shared._served_name``) leaves them to their token.
``read`` reads; ``run`` also checks, starts, continues and stops runs, saves
recipes and asks a judge. Nothing here writes Settings or reads a key or a licence;
the leaderboard says the Scoring weights it used.

A read answers the document its command prints with ``--json``
(``nullius.runs.cli``, ``nullius.compare.cli``, ``nullius.standard.cli``, ``nullius.workspace_cli``);
the leaderboard, the one its page reads first.
A run is a recipe of the Nullius standard, started as Review starts it:
compiled on the server, its prerequisites asked first, the question Review
asks answered by ``confirm``. A Harbor recipe is started from the builder.
An error is ``{"error": "..."}`` with its status.
"""
from __future__ import annotations

import functools
import importlib.metadata
import json
import os
import time

from flask import Response, g, request
from werkzeug.exceptions import HTTPException, NotFound

from nullius import access, execution, providers
from nullius.catalogue import catalog
from nullius.compare import cli as compare_cli, comparison
from nullius.judge import client as judge_client, store as judge_store
from nullius.leaderboard import service as leaderboard_service
from nullius.recipes import store as recipe_store
from nullius.redact import redact
from nullius.standard import cli as standard_cli
from nullius.runs import artifacts, cli as runs_cli
from nullius.standard.contract import definitions, match
from nullius.standard.pages import recipe as standard_recipe
from nullius.standard.runner import sets as task_sets
from nullius import workspace, workspace_cli

from .core import app
from . import builder, execute, judge, runs, shared, traces

PREFIX = "/api/v1"
STANDARD_ONLY = "Only a recipe of the Nullius standard runs over the API: start a Harbor recipe from the builder."


def _json(doc, status: int = 200) -> Response:
    """One JSON document, timestamps in ISO 8601 as the commands print them."""
    response = Response(json.dumps(doc, ensure_ascii=False, default=runs_cli._stamp) + "\n", status=status, mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(status: int, message: str, **more) -> Response:
    return _json({"error": message, **more}, status)


def api(scope: str):
    """A route of the API: its token checked for ``scope``, its refusals in JSON."""
    def wrap(view):
        @functools.wraps(view)
        def guarded(*args, **kwargs):
            kind, _, sent = request.headers.get("Authorization", "").partition(" ")
            record = access.verify(sent.strip()) if kind.lower() == "bearer" else None
            if record is None:
                response = _error(401, "This API takes a token: Authorization: Bearer <token> (nullius access create NAME, on the server).")
                response.headers["WWW-Authenticate"] = "Bearer"
                return response
            if not access.allows(record, scope):
                return _error(403, f"This token's scope is {record['scope']}: this needs {scope}.")
            g.api_token = record
            name = request.args.get("workspace")
            if name and workspace.load(shared.WS_HOME, name) is None:      # else a page's 404, which blames the address
                return _error(404, f"no workspace {name!r}")
            try:
                return view(*args, **kwargs)
            except HTTPException as e:
                return _error(e.code or 500, e.description or e.name)
        return guarded
    return wrap


class _Refused(HTTPException):
    """A refusal with its status, said in JSON by ``api``."""

    def __init__(self, code: int, description: str):
        super().__init__(description)
        self.code = code


@app.errorhandler(405)
def _wrong_method(e):
    """Refused in JSON under the API; a page's stays the page's. A 404 and a 500 are the error page's (web/filters.py)."""
    return _error(405, e.description) if request.path.startswith(PREFIX) else e


def _body() -> dict:
    """The request's JSON object, {} when nothing was sent. One that cannot be read is refused: a default read in its
    place could ask another judge, or start another run, than the one it names."""
    if not request.get_data():
        return {}
    try:
        got = request.get_json(force=True, silent=True)
    except RecursionError:
        got = None
    if not isinstance(got, dict):
        raise _Refused(400, "The request's body is one JSON object.")
    return got


@app.route(PREFIX)
@api("read")
def api_info():
    """Who is asking, and where a run can go."""
    try:
        version = importlib.metadata.version("nullius-eval")
    except importlib.metadata.PackageNotFoundError:
        version = "source checkout"
    ws = shared.STATE["workspace"]
    return _json({"api": 1, "version": version, "token": access.shown(g.api_token), "workspace": ws.name if ws else None,
                  "where": ["local", *(h.entry for h in execute._hosts())]})


def _task_sets(key: str) -> list:
    """A benchmark's task sets as ``nullius standard set list --json`` prints them, each with the id a job records;
    none when the user's sets cannot be read, which a check that names one says."""
    try:
        return [standard_cli._set_rows(e) for e in task_sets.entries(key)[0]]
    except (task_sets.Refused, OSError):
        return []


@app.route(PREFIX + "/catalogue")
@api("read")
def api_catalogue():
    """The benchmarks with the runners that can run each, the agents and the capabilities; a standard benchmark with
    its datasets (``subset_of``: the list one is cut from, and a recipe takes one of the lists cut from a list), its
    task sets (a recipe's ``task_sets`` names one for a dataset) and what fits it, its own procedure first, by the
    reference a recipe names it with. A benchmark and an agent say their origin."""
    defs = {kind: definitions.load(kind)[0] for kind in definitions.KINDS}
    fits = match.table(defs["benchmark"], defs["agent"], defs["capability"])
    standard = {b["key"]: b for b in defs["benchmark"]}
    benchmarks = []
    for b in catalog.load().listed_benchmarks:
        row = {"key": b.key, "title": b.title, "origin": b.origin, "runners": [r.harness for r in b.usable_routes]}
        s = standard.get(b.key)
        if s:
            row.update(family=s["family"], needs=s.get("needs") or [],
                       datasets=[{"id": d["id"], "label": d.get("label"), "units": d.get("units"), "subset_of": d.get("subset_of")}
                                 for d in s["datasets"]],
                       task_sets=_task_sets(b.key),
                       fits=[*(k for k in s.get("setups") or {} if k == "original"),
                             *(ref for ref, why in fits.get(b.key, {}).items() if not why)])
        benchmarks.append(row)
    others = {kind: [{"key": d["key"], "ref": d.get("_ref") or d["key"], "label": d.get("label"), "summary": d.get("summary"),
                      "offers": d.get("offers") or [], **({"origin": definitions.origin_of(d)} if kind == "agent" else {})}
                     for d in defs[kind]] for kind in ("agent", "capability")}
    return _json({"benchmarks": benchmarks, "agents": others["agent"], "capabilities": others["capability"]})


@app.route(PREFIX + "/workspaces")
@api("read")
def api_workspaces():
    return _json([workspace_cli.row(ws) for ws in workspace.list_all(shared.WS_HOME)])


# Jobs and trials are the open workspace's, or ?workspace=<name>'s, as on the pages.

@app.route(PREFIX + "/jobs")
@api("read")
def api_jobs():
    return _json([runs_cli.job_row(j) for j in shared._jobs_for_request()])


def _job(job_id):
    job = shared._job_for_request(job_id)
    if job is None:
        raise _Refused(404, f"no job {job_id!r} in this workspace")
    return job


@app.route(PREFIX + "/jobs/<job_id>")
@api("read")
def api_job(job_id):
    return _json(runs_cli.job_detail(_job(job_id)))


@app.route(PREFIX + "/jobs/<job_id>/live")
@api("read")
def api_job_live(job_id):
    """The live board's snapshot, as the job page polls it."""
    _job(job_id)
    try:
        return runs.job_live(job_id)
    except NotFound:
        raise _Refused(404, f"job {job_id!r} kept no feed for a live board") from None


def _trial(job_id, trial_name, tries=True):
    """``tries``: an attempt's earlier try too, evidence to read and never a trial to judge, as on the pages."""
    job = _job(job_id)
    trial = traces._find_trial(job, trial_name) or (traces._find_try(job, trial_name) if tries else None)
    if trial is None:
        raise _Refused(404, f"no trial {trial_name!r} in job {job_id!r}")
    return job, trial


@app.route(PREFIX + "/jobs/<job_id>/trials/<trial_name>")
@api("read")
def api_trial(job_id, trial_name):
    return _json(runs_cli.trial_detail(*_trial(job_id, trial_name)))


@app.route(PREFIX + "/jobs/<job_id>/trials/<trial_name>/conversation")
@api("read")
def api_conversation(job_id, trial_name):
    """The trial page's conversation browser: paged, with its ``q``, ``kind``, ``page`` and ``step``."""
    _trial(job_id, trial_name)
    return traces.conversation_data(job_id, trial_name)


@app.route(PREFIX + "/jobs/<job_id>/trials/<trial_name>/trace")
@api("read")
def api_trace(job_id, trial_name):
    """The trial's trace document, each of its strings redacted. Read as a declared record is, no link followed: a
    trial's files are the agent's, and a link could lead anywhere on the server."""
    _, trial = _trial(job_id, trial_name)
    try:
        fd = artifacts._open_under(trial.dir, os.path.relpath(trial.trace_path, trial.dir), os.O_RDONLY | os.O_NONBLOCK)
        with open(fd, encoding="utf-8", errors="replace") as fh:
            raw = fh.read(artifacts.MAX_READ + 1)
        if len(raw) > artifacts.MAX_READ:
            return _error(413, "The trace is larger than the reader's limit: read it on the server.")
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError("not a trace")
        return _json(_redacted(doc))
    except (OSError, ValueError, RecursionError):
        raise _Refused(404, f"trial {trial_name!r} has no trace that can be read") from None


def _redacted(value, name: str = ""):
    """A JSON value with its strings redacted, each as the value of the name it is under: the JSON stays JSON."""
    if isinstance(value, dict):
        return {redact(k): _redacted(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v, name) for v in value]
    if not isinstance(value, str):
        return value
    under = redact(f"{name}={value}")
    return under[len(name) + 1:] if name and under.startswith(f"{name}=") else redact(value)


@app.route(PREFIX + "/jobs/<job_id>/trials/<trial_name>/artifacts")
@api("read")
def api_artifacts(job_id, trial_name):
    _, trial = _trial(job_id, trial_name)
    return _json([{k: r[k] for k in ("key", "label", "filename", "size")} 
                  for r in artifacts.listing(trial.dir, artifacts.declared(trial.source)) if r["exists"]])


@app.route(PREFIX + "/jobs/<job_id>/trials/<trial_name>/artifacts/<key>")
@api("read")
def api_artifact(job_id, trial_name, key):
    """One record whole, redacted, up to the reader's limit."""
    _, trial = _trial(job_id, trial_name)
    try:
        declared = artifacts.declared(trial.source)
        text, truncated = artifacts.whole(trial.dir, key, declared)
    except (LookupError, OSError):
        raise _Refused(404, f"trial {trial_name!r} has no record {key!r}")
    return _json({"key": key, "filename": artifacts.describe(key, declared)[1], "truncated": truncated, "text": text})


@app.route(PREFIX + "/compare")
@api("read")
def api_compare():
    """``?job=a&job=b[&matched=1]``: the comparison ``nullius compare --json`` prints."""
    ids = list(dict.fromkeys(request.args.getlist("job")))
    if len(ids) < 2:
        return _error(400, "Two different job ids are needed to compare: ?job=A&job=B.")
    try:
        selected = comparison.resolve([comparison.RunRef("", i).value for i in ids], {"": shared._jobs_for_request()})
    except ValueError as e:
        return _error(404, str(e))
    return _json(compare_cli.document(comparison.build(selected, "matched" if request.args.get("matched") in ("1", "true") else "whole")))


@app.route(PREFIX + "/leaderboard")
@api("read")
def api_leaderboard():
    """The workspace's composite agent scores, by the server's Scoring settings: the document the Leaderboard page
    reads first. Its trajectory counts (``steps``, ``calls``) stay uncounted: they are read from each trial's own
    files, which are the agent's, and whole."""
    ws = shared.STATE["workspace"]
    name = request.args.get("workspace") or (ws.name if ws else "")
    jobs = leaderboard_service.qualified(shared._jobs_for_request())
    return _json(leaderboard_service.document(jobs, workspace=name, workspace_open=bool(name or shared.STATE["roots"])))


# Judgements: a judge model's answers about a trial, kept by the job they judge (web/judge.py). Reading one is
# free; asking is a paid model call for each trial, so it takes the run scope, and several trials at once take a yes.

def _lane(value) -> str:
    lane = "decision" if value is None else value
    if lane not in judge_client.LANES:
        raise _Refused(400, f"A judge is one of: {', '.join(judge_client.LANES)}.")
    return lane


def _judged(job, lane: str) -> dict:
    """{trial: record} for the trials of a job one judge has judged, as its page counts them."""
    return judge_store.for_job(judge_store.home(), judge.job_key(job), judge.started_by_name(job), lane=lane,
                               old=judge.old_of(job), strict=judge.retried(job))


def _judge_error(e: judge_client.JudgeError) -> Response:
    return _error(400 if e.status in (401, 422) else 502, str(e))


@app.route(PREFIX + "/jobs/<job_id>/judgements")
@api("read")
def api_judgements(job_id):
    """``?lane=decision|llm``: {judged, trials, records: {trial: record}} for one job and one judge."""
    job, lane = _job(job_id), _lane(request.args.get("lane"))
    judged = _judged(job, lane)
    return _json({"lane": lane, "judged": len(judged), "trials": len(job.trials), "records": judged})


@app.route(PREFIX + "/jobs/<job_id>/trials/<trial_name>/judgement")
@api("read")
def api_judgement(job_id, trial_name):
    job, trial = _trial(job_id, trial_name)
    lane = _lane(request.args.get("lane"))
    record = judge.judgement_of(job, trial, lane)
    if record is None:
        raise _Refused(404, f"trial {trial_name!r} has no {lane} judgement")
    return _json(record)


@app.route(PREFIX + "/jobs/<job_id>/trials/<trial_name>/judge", methods=["POST"])
@api("run")
def api_judge_trial(job_id, trial_name):
    """Ask a judge about one trial, ``{"lane"?}``: the record as stored."""
    job, trial = _trial(job_id, trial_name, tries=False)
    try:
        return _json(judge.judge_trial(job, trial, judge.key(_lane(_body().get("lane", request.args.get("lane"))))))
    except judge_client.JudgeError as e:
        return _judge_error(e)


@app.route(PREFIX + "/jobs/<job_id>/judge", methods=["POST"])
@api("run")
def api_judge_job(job_id):
    """Ask a judge about every trial of a job it has not judged, ``{"lane"?, "confirm"?}``: {todo, judged, failed}.
    Each trial is a paid call: without ``confirm`` it answers how many it would ask, and asks none."""
    job, body = _job(job_id), _body()
    lane = _lane(body.get("lane", request.args.get("lane")))
    judged = _judged(job, lane)
    todo = [t for t in job.trials if t.has_trajectory and t.trial_name not in judged]
    if todo and body.get("confirm") is not True:
        return _error(409, f"This asks the judge about {len(todo)} trial(s), a paid call each. Send \"confirm\": true to ask.", todo=len(todo))
    k, cal, col = judge.key(lane), judge.batch_calibration(), judge.batch_workspace()
    done, failed = judge.run_batch(todo, lambda t: judge.judge_trial(job, t, k, calibration=cal, column=col), lambda t: t.trial_name)
    return _json({"lane": lane, "todo": len(todo), "judged": done, "failed": failed})


# Saved recipes, as the builder keeps them.

@app.route(PREFIX + "/recipes")
@api("read")
def api_recipes():
    return _json(recipe_store.listing())


def _saved(recipe_id: str) -> dict:
    """A saved recipe by its id, or by its name."""
    item = next((x for x in recipe_store.listing() if recipe_id in (x["id"], x["name"])), None)
    if item is None:
        raise _Refused(404, f"no saved recipe {recipe_id!r}")
    return item


@app.route(PREFIX + "/recipes/<path:recipe_id>")
@api("read")
def api_recipe(recipe_id):
    return _json(_saved(recipe_id))


@app.route(PREFIX + "/recipes", methods=["POST"])
@api("run")
def api_recipe_save():
    body = _body()
    try:
        return _json(recipe_store.save(body.get("recipe"), body.get("id") or None))
    except ValueError as e:
        return _error(400, str(e))


# Runs: checked and started as Review does it.

def _recipe_of(body: dict) -> dict:
    """The recipe a request names: inline, or a saved one by id or name; its *Where* replaced when the request says.
    Named as its run is, so a check refuses what the start would."""
    recipe = body.get("recipe")
    if isinstance(recipe, str):
        recipe = _saved(recipe)["recipe"]
    if not isinstance(recipe, dict):
        raise _Refused(400, "A run takes a recipe: {\"recipe\": {...}}, or a saved recipe's id or name.")
    if body.get("where"):                   # blank is not a machine: the recipe's own stay
        recipe = {**recipe, "where": body["where"]}
    name = str(body.get("name") or "").strip() or f"{recipe.get('name') or ''}-{time.strftime('%m%d-%H%M%S')}"
    return {**recipe, "name": name}


def _compiled(recipe: dict) -> tuple:
    """(what Run posts, the check's document without its machines) for one recipe of the standard. A Harbor
    recipe is compiled by the builder's page, not here: it is started from the builder."""
    if recipe.get("harness") != standard_recipe.HARNESS:
        raise _Refused(400, STANDARD_ONLY)
    out = builder._standard_compile(recipe)
    post = {"name": recipe.get("name"), "where": recipe.get("where"), "recipe": recipe}
    return post, {"errors": [e["message"] for e in out["errors"]], "notes": out["notes"], "review": out["review"],
                  "jobs": out["jobs"], "confirm": out["confirm"] or ""}


def _checked(recipe: dict) -> tuple:
    """(what Run posts, the whole check): the compile, then what each machine has when the recipe compiles."""
    post, doc = _compiled(recipe)
    machines = {"machines": [], "ready": False, "errors": [], "notes": []}
    provider, endpoint = recipe.get("provider"), recipe.get("endpoint")
    if not doc["errors"]:
        missing = execute.provider_problem(provider, endpoint)
        if missing and not standard_recipe.compile(recipe).get("keyless"):      # controls alone call no model
            doc["errors"].append(missing)
    if not doc["errors"]:
        choice = providers.choice(str(provider or ""), str(endpoint or ""))
        machines = builder.prerequisites_of({"recipe": recipe, "provider": choice}).get_json()
    doc["errors"] += machines.get("errors") or []
    doc["notes"] += machines.get("notes") or []
    doc.update(machines=machines["machines"], ready=bool(machines.get("ready")) and not doc["errors"])
    return post, doc


@app.route(PREFIX + "/runs/check", methods=["POST"])
@api("run")
def api_run_check():
    """What Review shows before Run: {ready, errors, notes, review, jobs, confirm, machines}. Starts nothing."""
    return _json(_checked(_recipe_of(_body()))[1])


@app.route(PREFIX + "/runs", methods=["POST"])
@api("run")
def api_run_start():
    """Start a recipe: ``{"recipe": {...} | "<saved id or name>", "where"?, "name"?, "confirm"?}``. A new run each
    time, named by the recipe's name and the second unless ``name`` says; refused with what Review would show
    when the recipe does not compile, a machine lacks a prerequisite, or its question is not answered."""
    body = _body()
    post, doc = _checked(_recipe_of(body))
    if doc["errors"]:
        return _error(400, " ".join(doc["errors"]), **doc)
    if not doc["ready"]:
        return _error(409, "A machine lacks what this recipe needs: see machines.", **doc)
    if doc["confirm"] and body.get("confirm") is not True:
        return _error(409, doc["confirm"] + " Send \"confirm\": true to start it.", **doc)
    started = execute.start(post)
    out = started[0].get_json() if isinstance(started, tuple) else started.get_json()
    status = started[1] if isinstance(started, tuple) else 200
    if not out.get("ok"):
        return _error(status, out.get("error") or "The run did not start.")
    out.pop("open", None)                   # a page's address
    out.setdefault("runs", [{k: v for k, v in out.items() if k != "ok"}])
    return _json(out)


@app.route(PREFIX + "/runs")
@api("read")
def api_runs():
    """The runs this server follows or kept a record of, the latest started first."""
    return _json(sorted((ex.summary() for ex in list(execution.EXECUTIONS.values())), key=lambda s: s.get("started") or 0, reverse=True))


def _run(run_id):
    ex = execution.EXECUTIONS.get(run_id)
    if ex is None:
        raise _Refused(404, f"no run {run_id!r}")
    return ex


@app.route(PREFIX + "/runs/<run_id>")
@api("read")
def api_run(run_id):
    """State, stages and the log lines from ``since`` on."""
    ex = _run(run_id)
    return _json({**ex.summary(), "log": ex.tail(request.args.get("since", type=int) or 0)})


@app.route(PREFIX + "/runs/<run_id>/stop", methods=["POST"])
@api("run")
def api_run_stop(run_id):
    ex = _run(run_id)
    ex.stop()
    return _json(ex.summary())


@app.route(PREFIX + "/runs/<run_id>/continue", methods=["POST"])
@api("run")
def api_run_continue(run_id):
    ex = _run(run_id)
    ex.continue_()
    return _json(ex.summary())
