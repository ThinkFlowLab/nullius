"""Running a recipe from the builder - here or on a host - and following it."""
from __future__ import annotations

import json
import os
import re
import time
from flask import abort, jsonify, request, url_for
from nullius import execution, hosts, prerequisites, prices, providers, settings
from nullius.harbor import source as harbor_source
from nullius.standard.backends import boat
from nullius.standard.calls import ledger
from nullius.standard.pages import recipe as standard_recipe
from nullius.standard.runner import rerun

from .core import app
from . import shared


@app.route("/run", methods=["POST"])
def run_start():
    """Start a plan. The builder posts the recipe;
    the target is this machine or one of the Settings' hosts. Same guards
    as every mutation: form token, same origin, and a loopback client -
    running is writing to a machine."""
    shared._require_csrf()
    if not shared._local_client():
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
    # Its agents speak chat: a provider that serves none is refused, model or not (command-builder-core.js, providerBlocker).
    offered, provider = providers.offered(["chat"]), str(recipe.get("provider") or "openrouter")
    if provider not in offered:
        names = [providers.label(p) for p in offered]
        return jsonify(ok=False, error=f"{providers.label(provider)} does not serve what this recipe's agent speaks. Choose "
                                       + ", ".join(names[:-1]) + (" or " if len(names) > 1 else "") + names[-1] + " on the Model step."), 400
    # One with a model to call needs its provider's key here (controls alone call none).
    missing = provider_problem(recipe.get("provider") or "openrouter", recipe.get("endpoint")) if body.get("model") else ""
    if missing:
        return jsonify(ok=False, error=missing), 400
    pin, problem = harbor_source.pin_of(recipe.get("harbor"))
    if problem:
        return jsonify(ok=False, error=problem), 400
    # a local task folder as the server reads it, absolute: the check runs from here, the part from its run directory
    tasks = str(body.get("tasks") or "")
    tasks = os.path.abspath(os.path.expanduser(tasks)) if tasks else ""
    plan = execution.Plan(name=str(body.get("name") or ""), script=str(body.get("script") or ""), host=host, tasks=tasks, harbor_pin=pin or {},
                          **credentials_for(recipe.get("provider") or "openrouter", recipe.get("endpoint")),     # none named: OpenRouter, as the builder reads it
                          model=str(body.get("model") or ""), jobs=[j for j in (body.get("jobs") or []) if isinstance(j, dict)],
                          parts=[{"id": re.sub(r"[^A-Za-z0-9_.-]", "_", str(p.get("id") or "")), "label": str(p.get("label") or ""), "script": str(p.get("script") or "")}
                                 for p in (body.get("parts") or []) if isinstance(p, dict)],
                          recipe=recipe)
    return _start(plan)


def credentials_for(provider, endpoint=None) -> dict:
    """Where a run's key and endpoint come from, as ``Plan`` fields, from the
    provider's record: a recipe on a preset run provider (OpenAI, OpenRouter, Anthropic) is
    sent to its own base, fixed, with its own key; one on a custom provider -
    or a provider this does not know - with that provider's base and key
    (``endpoint``, its id: none, MODEL_API_URL and MODEL_API_KEY), which such a
    run cannot go without. Every run is given its endpoint: a Harbor part's
    script requires API_URL."""
    if provider in providers.PROVIDERS and provider in providers.PRESETS:      # a run provider: never TypeSafe's key
        rec = providers.record(provider)
        return {"key_var": rec["key"], "url_var": "", "base_url": rec["base"]}
    url_var, key_var = providers.endpoint_vars(str(endpoint or "") if provider == "custom" else "")
    return {"key_var": key_var, "url_var": url_var, "base_url": ""}


def provider_problem(provider, endpoint=None) -> str:
    """Why a run cannot start here on this provider - its key, or for an
    OpenAI-compatible endpoint its URL, is not set in Settings, or the
    endpoint is gone - or "" when it can. The builder says the same before
    Run (command-builder-core.js, providerBlocker)."""
    if provider == "custom" and endpoint and not settings.ENDPOINT_ID.match(str(endpoint)):
        return "The recipe's endpoint is not an endpoint id: choose an endpoint on the Model step."
    p = providers.choice(provider, str(endpoint or "")) if provider in providers.PROVIDERS else "custom"   # as credentials_for reads one it does not know
    have = providers.available(os.environ)
    if p in have:
        return ""
    why = (f"{providers.label(p)} cannot run here: its URL, {providers.needs(p)}, holds a credential or is not an http(s) URL; "
           "put the key in the Key field in Settings." if p in providers.broken(os.environ)
           else f"{providers.label(p)} cannot run here: {providers.needs(p)} is not set in Settings.")
    if not have:
        return why + " Set a model key there to run the recipe."
    return why + " Choose " + " or ".join(providers.label(x) for x in have) + " on the Model step, or set it in Settings."


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
                                    **credentials_for(recipe.get("provider"), recipe.get("endpoint")), model=out["model"], jobs=sh["jobs"],
                                    parts=sh["parts"], recipe=recipe, docker=bool(out.get("docker")), keyless=bool(out.get("keyless")),
                                    licences=dict(out.get("licences") or {}) if host else {}))
    return plans, ""


def _start_standard(recipe: dict):
    """The standard: compiled here from its definitions; on
    this machine, a host, or split over several - one run per machine, every
    one checked before any starts."""
    if len(json.dumps(recipe)) > 200_000:
        return jsonify(ok=False, error="the recipe is too large to keep with the run"), 400
    out = standard_recipe.compile(recipe, docker_unusable=docker_problem, boat_unusable=boat.unusable,
                                  boat_size_problem=boat_size_problem, unpriced=unpriced)
    if out["errors"]:
        return jsonify(ok=False, error=" ".join(e["message"] for e in out["errors"])), 400
    # a run of controls calls no model: it is handed no key, so none need be set
    missing = "" if out.get("keyless") else provider_problem(recipe.get("provider"), recipe.get("endpoint"))
    if missing:
        return jsonify(ok=False, error=missing), 400
    plans, problem = standard_plans(recipe, out, {h.entry for h in _hosts()})
    if problem:
        return jsonify(ok=False, error=problem), 400
    if len(plans) == 1:
        return _start(plans[0])
    started, problem, status = begin(plans)
    if problem:
        return jsonify(ok=False, error=problem), status
    return jsonify(ok=True, open=url_for("index", view="launch"), runs=[ex.summary() for ex in started],
                   **started[0].summary())


_PRICED = {"at": float("-inf"), "rows": {}}


def unpriced(models: list) -> list:
    """Which of ``models`` have no price in the user's list (``nullius.prices``): what a recipe with a spending
    cap is told at its Limits step, since a run with a cap is refused without a price for each model it calls."""
    if time.monotonic() - _PRICED["at"] > 5:     # the builder asks at every keystroke: the list is read at most every five seconds
        _PRICED.update(at=time.monotonic(), rows=prices.table())
    return [m for m in models if m not in _PRICED["rows"]]


_BOAT = {"at": 0.0, "key": None, "account": {}}


def boat_account() -> dict:
    """What Boat's limits say of this account, kept a minute for the same key and asked briefly:
    {types: {size: why it cannot start one, or None}, slots: VMs at once or None}. Empty without a key;
    a Boat that does not answer says nothing, so every size is offered and Run says the rest."""
    k = boat.key()
    if not k:
        return {}
    if time.time() - _BOAT["at"] > 60 or _BOAT["key"] != hash(k):
        client = boat.Client(k, timeout=5)
        limits = client.limits()
        _BOAT.update(at=time.time(), key=hash(k), account={"types": boat.machine_types(client, limits),
                                                          "slots": boat.slots(client, limits)})
    return _BOAT["account"]


def boat_sizes() -> dict:
    """{size: why this account cannot start it}, the sizes it can start left out."""
    return {t: why for t, why in (boat_account().get("types") or {}).items() if why}


def boat_size_problem(size: str):
    return boat_sizes().get(size)


def docker_problem():
    """Why docker cannot run containers on this machine, or None; kept for a
    minute (``prerequisites.docker_here``)."""
    return prerequisites.docker_here()


def begin(plans: list) -> tuple:
    """Start ``plans``, each checked before any starts: (the runs, "", 200), or
    ([], why not, its status) and nothing started."""
    for plan in plans:
        problem = plan.check()
        if problem:
            return [], problem, 400
        if any(e.plan.name == plan.name for e in execution.active()):
            return [], f"{plan.name} is running already", 409
        if os.path.exists(os.path.join(execution.runs_root(), plan.name)):
            return [], execution.name_taken(plan.name), 409
    started = [execution.start(plan) for plan in plans]
    # The job lands (or is mirrored) under runs/<name>/jobs: the workspace
    # reads it from now on.
    shared.adopt_runs(started)
    return started, "", 200


def _start(plan: execution.Plan):
    started, problem, status = begin([plan])
    if problem:
        return jsonify(ok=False, error=problem), status
    return jsonify(ok=True, open=url_for("index", view="launch"), **started[0].summary())


REASON_MAX = 200


def again_offer(job, ex) -> dict | None:
    """What the job's page offers to run its tasks again: {"where": the machine it ran on} when the server can,
    {"why": one line} when it cannot - no record of where it ran, still running, merged from its shards' jobs, its
    host gone from Settings, a copy that is not its run's own - and None for a job that is not the standard's."""
    if not job.standard:
        return None
    if job.merged_from:
        return {"why": "Merged from its shards' jobs: run a task again in its shard's job."}
    if ex is None or ex.plan.runner != standard_recipe.HARNESS:
        return {"why": "Not started from here: no record of where it ran."}
    if ex.state in execution.ALIVE or job.is_running or job.is_preparing:
        return {"why": "Still running: more tasks can be started once it has ended."}
    if os.path.realpath(job.dir) != os.path.realpath(os.path.join(ex.jobs, job.job_id)):
        return {"why": "This copy is not its run's own: open the job its run wrote."}
    if ex.plan.host is not None and ex.plan.host.entry not in {h.entry for h in _hosts()}:
        return {"why": f"Its host, {ex.plan.host.label}, is no longer in Settings."}
    return {"where": ex.plan.host.label if ex.plan.host else "this machine"}


@app.route("/job/<job_id>/rerun", methods=["POST"])
def job_rerun(job_id):
    """Run the trials the job page ticked again, in the job, where its run ran: this machine, or the host the
    run was sent to (``Execution.again``); a task it planned with no result runs there too. Same guards as Run.
    Refused, and nothing started, for a job the page offers no button for (``again_offer``), a trial the job
    does not hold, a host that no longer holds the job, or a provider whose key is not set here."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Runs are started from a browser on the application host only.")
    j = shared._find_job(shared._jobs_for_request(), job_id)
    if j is None:
        abort(404)
    ex = execution.for_job(job_id)
    offer = again_offer(j, ex)
    if offer is None or offer.get("why"):
        return jsonify(ok=False, error=(offer or {}).get("why") or "Only a job of the Nullius standard is run again."), 409
    body = request.get_json(silent=True) or {}
    from . import runs as runs_web
    held = {t.trial_name for t in j.trials} | {t for w in runs_web.waiting_of(j) for t in w["trials"]}   # and the tasks with no result
    picked = list(dict.fromkeys(t for t in body.get("trials") or [] if isinstance(t, str))) if isinstance(body.get("trials"), list) else []
    if not picked:
        return jsonify(ok=False, error="Tick the tasks to run again."), 400
    unknown = [t for t in picked if t not in held]
    if unknown:
        return jsonify(ok=False, error=f"{job_id} holds no trial {', '.join(unknown[:3])}"), 400
    reason = " ".join(str(body.get("reason") or "").split())
    if len(reason) > REASON_MAX:
        return jsonify(ok=False, error=f"The reason is one line of at most {REASON_MAX} characters."), 400
    budget = job_cap(j, ex, settle=True)        # one cap for the job: the part has what the parts before it left
    if budget and budget["closed"]:
        said = rerun.spent_out(budget, "change it to run more.")
        return jsonify(ok=False, error=said[0].upper() + said[1:]), 409
    recipe = ex.plan.recipe or {}
    missing = "" if ex.plan.keyless else provider_problem(recipe.get("provider") or "openai", recipe.get("endpoint"))
    if missing:
        return jsonify(ok=False, error=missing), 400
    gone = ex.holds(job_id)
    if gone:
        return jsonify(ok=False, error=gone), 409
    n = 1 + sum(1 for p in ex.plan.parts if p.get("again"))
    try:
        ex.again(standard_recipe.again(job_id, picked, reason=reason, max_usd=budget["max_usd"] if budget and budget["changed"] else None, n=n))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 409
    return jsonify(ok=True, **ex.summary())


def job_cap(job, ex, settle: bool = False) -> dict | None:
    """The job's one spending cap as its page shows it and its next part runs under it (``rerun.budget``): the cap
    set on the page (``Plan.caps``) where it differs from the job's own, else the job's. ``settle``: a cap set on the
    page that the job's record now holds is dropped from the run's record. None for a job with no cap."""
    if not job.standard:
        return None
    set_here = (ex.plan.caps or {}).get(job.job_id) if ex is not None else None
    try:
        got = rerun.budget(job.dir, cap=set_here)
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if settle and set_here is not None and got is not None and not got["changed"]:
        with ex._turn:
            ex.plan.caps = {k: v for k, v in ex.plan.caps.items() if k != job.job_id}
            ex._save()
    return got


@app.route("/job/<job_id>/cap", methods=["POST"])
def job_cap_change(job_id):
    """A new spending cap for the whole job, from its next part on: kept in its run's record (``Plan.caps``) and
    handed to that part as ``rerun --max-usd``. Refused where the page offers no next part (``again_offer``: a part
    running among them), and for a cap at or under what the job has spent."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="Runs are started from a browser on the application host only.")
    j = shared._find_job(shared._jobs_for_request(), job_id)
    if j is None:
        abort(404)
    ex = execution.for_job(job_id)
    offer = again_offer(j, ex)
    if offer is None or offer.get("why"):
        return jsonify(ok=False, error=(offer or {}).get("why") or "Only a job of the Nullius standard has a cap to change."), 409
    try:
        cap = float((request.get_json(silent=True) or {}).get("max_usd"))
    except (TypeError, ValueError):
        cap = -1.0
    if not 0 < cap < float("inf"):
        return jsonify(ok=False, error="The spending cap is an amount in USD above zero."), 400
    now = rerun.budget(j.dir, cap=cap)
    # nothing left (float noise included), or a cap that reads as the spend the page shows
    if now["left_usd"] <= 0 or ledger.usd(cap) == ledger.usd(now["spent_usd"]):
        return jsonify(ok=False, error=f"The cap must be above what this run has spent, {ledger.usd(now['spent_usd'])} USD."), 400
    with ex._turn:
        caps = {k: v for k, v in (ex.plan.caps or {}).items() if k != job_id}
        ex.plan.caps = caps if not now["changed"] else dict(caps, **{job_id: cap})
        ex._save()
    return jsonify(ok=True, max_usd=cap, left_usd=now["left_usd"])


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
    if not shared._local_client():
        abort(403)
    ex = execution.EXECUTIONS.get(run_id)
    if ex is None:
        abort(404)
    ex.continue_()
    return jsonify(ok=True, **ex.summary())


@app.route("/run/<run_id>/stop", methods=["POST"])
def run_stop(run_id):
    shared._require_csrf()
    if not shared._local_client():
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
