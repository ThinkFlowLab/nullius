"""Running a recipe from the builder - here or on a host - and following it."""
from __future__ import annotations

import json
import os
import re
from flask import abort, jsonify, request, url_for
from nullius import execution, hosts, prerequisites, prices, providers
from nullius.harbor import source as harbor_source
from nullius.standard import recipe as standard_recipe

from .core import app
from . import shared
from . import settings as settings_web


@app.route("/run", methods=["POST"])
def run_start():
    """Start a plan. The builder posts the runner script and what it needs;
    the target is this machine or one of the Settings' hosts. Same guards
    as every mutation: form token, same origin, and a loopback client -
    running is writing to a machine."""
    shared._require_csrf()
    if not settings_web._local_client():
        abort(403, description="Runs are started from a browser on the application host only.")
    body = request.get_json(silent=True) or {}
    recipe = body.get("recipe") if isinstance(body.get("recipe"), dict) else {}
    if recipe.get("harness") == standard_recipe.HARNESS:
        return _start_standard(recipe)
    where = str(body.get("where") or "local")
    host = None
    if where != "local":
        try:
            host = hosts.parse(where)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        if host.entry not in {h.entry for h in _hosts()}:
            return jsonify(ok=False, error=f"{where} is not one of the hosts in Settings"), 400
    if len(json.dumps(recipe)) > 200_000:
        return jsonify(ok=False, error="the recipe is too large to keep with the run"), 400
    if recipe.get("harness", "harbor") != "harbor":
        return jsonify(ok=False, error=providers.GONE), 400
    # A Harbor recipe runs its pin (nullius/harbor/source.py), or harbor on the target's PATH when unpinned.
    # One with a model to call needs its provider's key here (controls alone call none).
    missing = provider_problem(recipe.get("provider") or "openrouter") if body.get("model") else ""
    if missing:
        return jsonify(ok=False, error=missing), 400
    pin, problem = harbor_source.pin_of(recipe.get("harbor"))
    if problem:
        return jsonify(ok=False, error=problem), 400
    # a local task folder as the server reads it, absolute: the check runs from here, the part from its run directory
    tasks = str(body.get("tasks") or "")
    tasks = os.path.abspath(os.path.expanduser(tasks)) if tasks else ""
    plan = execution.Plan(name=str(body.get("name") or ""), script=str(body.get("script") or ""), host=host, tasks=tasks, harbor_pin=pin or {},
                          **credentials_for(recipe.get("provider") or "openrouter"),     # none named: OpenRouter, as the builder reads it
                          model=str(body.get("model") or ""), jobs=[j for j in (body.get("jobs") or []) if isinstance(j, dict)],
                          parts=[{"id": re.sub(r"[^A-Za-z0-9_.-]", "_", str(p.get("id") or "")), "label": str(p.get("label") or ""), "script": str(p.get("script") or "")}
                                 for p in (body.get("parts") or []) if isinstance(p, dict)],
                          recipe=recipe)
    return _start(plan)


def credentials_for(provider) -> dict:
    """Where a run's key and endpoint come from, as ``Plan`` fields: a recipe
    on OpenAI is sent to OpenAI's own endpoint, fixed, with OPENAI_API_KEY;
    one on OpenRouter to OpenRouter's own, fixed, with OPENROUTER_API_KEY;
    one on a local or self-hosted endpoint - or a provider this does not
    know - with the model key and MODEL_API_URL, which such a run cannot go
    without. Every run is given its endpoint: a Harbor part's script requires
    API_URL, and an OpenAI run left without one exited before Harbor began."""
    if provider == "openai":
        return {"key_var": providers.PROVIDER_KEY["openai"], "url_var": "", "base_url": providers.PROVIDER_BASE["openai"]}
    if provider == "openrouter":
        return {"key_var": providers.PROVIDER_KEY["openrouter"], "url_var": "", "base_url": providers.PROVIDER_BASE["openrouter"]}
    return {"key_var": providers.PROVIDER_KEY["custom"], "url_var": "MODEL_API_URL", "base_url": ""}


def provider_problem(provider) -> str:
    """Why a run cannot start here on this provider - its key, or for a local
    or self-hosted endpoint its URL, is not set in Settings - or "" when it
    can. The builder says the same before Run (command-builder-core.js,
    providerBlocker)."""
    p = provider if provider in providers.PROVIDERS else "custom"         # as credentials_for reads one it does not know
    have = providers.available(os.environ)
    if p in have:
        return ""
    why = f"{providers.PROVIDER_LABEL[p]} cannot run here: {providers.PROVIDER_NEEDS[p]} is not set in Settings."
    if not have:
        return why + " Set a model key there to run the recipe."
    return why + " Choose " + " or ".join(providers.PROVIDER_LABEL[x] for x in have) + " on the Model step, or set it in Settings."


def standard_plans(recipe: dict, out: dict, known) -> tuple:
    """([Plan], error): one plan per machine the standard recipe's *Where*
    names - its shard there - each a run of its own, or the error that keeps
    every one of them from starting. ``known``: the entries of Settings'
    hosts; a machine not among them is refused."""
    plans = []
    for sh in out["shards"]:
        host = None
        if sh["where"] != "local":
            host = hosts.parse(sh["where"])
            if host.entry not in known:
                return [], f"{sh['where']} is not one of the hosts in Settings"
        plans.append(execution.Plan(name=sh["name"], script=sh["script"], host=host, runner=standard_recipe.HARNESS,
                                    **credentials_for(recipe.get("provider")), model=out["model"], jobs=sh["jobs"],
                                    parts=sh["parts"], recipe=recipe, docker=bool(out.get("docker")), keyless=bool(out.get("keyless")),
                                    licences=dict(out.get("licences") or {}) if host else {}))
    return plans, ""


def _start_standard(recipe: dict):
    """The standard: compiled here from its definitions, like a runner's; on
    this machine, a host, or split over several - one run per machine, every
    one checked before any starts."""
    if len(json.dumps(recipe)) > 200_000:
        return jsonify(ok=False, error="the recipe is too large to keep with the run"), 400
    out = standard_recipe.compile(recipe, docker_unusable=docker_problem, unpriced=unpriced)
    if out["errors"]:
        return jsonify(ok=False, error=" ".join(e["message"] for e in out["errors"])), 400
    # a run of controls calls no model: it is handed no key, so none need be set
    missing = "" if out.get("keyless") else provider_problem(recipe.get("provider"))
    if missing:
        return jsonify(ok=False, error=missing), 400
    plans, problem = standard_plans(recipe, out, {h.entry for h in _hosts()})
    if problem:
        return jsonify(ok=False, error=problem), 400
    if len(plans) == 1:
        return _start(plans[0])
    for plan in plans:
        problem = plan.check()
        if problem:
            return jsonify(ok=False, error=problem), 400
        if any(e.plan.name == plan.name for e in execution.active()):
            return jsonify(ok=False, error=f"{plan.name} is running already"), 409
        if os.path.exists(os.path.join(execution.runs_root(), plan.name)):
            return jsonify(ok=False, error=execution.name_taken(plan.name)), 409
    started = [execution.start(plan) for plan in plans]
    shared.adopt_roots([ex.jobs for ex in started])
    return jsonify(ok=True, open=url_for("index", view="launch"), runs=[ex.summary() for ex in started],
                   **started[0].summary())


def unpriced(models: list) -> list:
    """Which of ``models`` have no price in the user's list (``nullius.prices``): what a recipe with a spending
    cap is told at its Limits step, since a run with a cap is refused without a price for each model it calls."""
    rows = prices.table()
    return [m for m in models if m not in rows]


def docker_problem():
    """Why docker cannot run containers on this machine, or None; kept for a
    minute (``prerequisites.docker_here``)."""
    return prerequisites.docker_here()


def _start(plan: execution.Plan):
    problem = plan.check()
    if problem:
        return jsonify(ok=False, error=problem), 400
    if any(e.plan.name == plan.name for e in execution.active()):
        return jsonify(ok=False, error=f"{plan.name} is running already"), 409
    if os.path.exists(os.path.join(execution.runs_root(), plan.name)):
        return jsonify(ok=False, error=execution.name_taken(plan.name)), 409
    ex = execution.start(plan)
    # The job lands (or is mirrored) under runs/<name>/jobs: the workspace
    # reads it from now on. The Runs page, by launch, shows it from the first
    # second: its runs queued, preparing, then running, each opening its job.
    shared.adopt_roots([ex.jobs])
    return jsonify(ok=True, open=url_for("index", view="launch"), **ex.summary())


@app.route("/run/<run_id>")
def run_status(run_id):
    """State and log lines from ``since`` on; the page polls this."""
    ex = execution.EXECUTIONS.get(run_id)
    if ex is None:
        abort(404)
    since = request.args.get("since", type=int) or 0
    return jsonify(**ex.summary(), log=ex.tail(since))


@app.route("/run/<run_id>/continue", methods=["POST"])
def run_continue(run_id):
    """The answer to a pause: go on with the next agent."""
    shared._require_csrf()
    if not settings_web._local_client():
        abort(403)
    ex = execution.EXECUTIONS.get(run_id)
    if ex is None:
        abort(404)
    ex.continue_()
    return jsonify(ok=True, **ex.summary())


@app.route("/run/<run_id>/stop", methods=["POST"])
def run_stop(run_id):
    shared._require_csrf()
    if not settings_web._local_client():
        abort(403)
    ex = execution.EXECUTIONS.get(run_id)
    if ex is None:
        abort(404)
    ex.stop()
    return jsonify(ok=True, **ex.summary())


def _hosts():
    try:
        return hosts.parse_list(os.environ.get("HOSTS", ""))
    except ValueError:
        return []
