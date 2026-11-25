"""Workspace Leaderboard and local, atomic Scoring settings endpoints."""
from __future__ import annotations

from flask import abort, jsonify, redirect, render_template, request, url_for
from nullius import ondisk
from nullius.leaderboard import board, configuration, service, weights
from .core import app
from . import shared


def _document(deferred=False):
    workspace = shared.STATE["workspace"]
    return service.document(shared._load_jobs(), workspace=workspace.name if workspace else "",
                            workspace_open=workspace is not None or bool(shared.STATE["roots"]),
                            all_scores=request.args.get("all_scores") == "1", deferred=deferred)


@app.get("/leaderboard")
def leaderboard():
    return render_template("leaderboard/board.html", board=_document(), percent=weights.percent)


@app.get("/api/leaderboard")
def leaderboard_data():
    return jsonify(_document())


@app.get("/api/leaderboard/comparison")
def leaderboard_comparison():
    agents, benchmarks = service.catalogue()
    settings, _ = configuration.load(configuration.location(), benchmarks)
    return jsonify(board.project(shared._load_jobs(), agents, benchmarks, settings,
                                 request.args.get("benchmark", ""), request.args.get("version", ""),
                                 all_scores=request.args.get("all_scores") == "1"))


@app.get("/api/leaderboard/metrics")
def leaderboard_metrics():
    initial = _document()
    if request.args.get("context") != initial["context"]:
        return jsonify(error="The workspace or scoring settings changed. Reload the Leaderboard."), 409
    completed = _document(deferred=True)
    if completed["context"] != initial["context"]:
        return jsonify(error="The workspace or scoring settings changed. Reload the Leaderboard."), 409
    return jsonify(context=completed["context"], rows=[{"key": r["key"], "metrics": r["metrics"]} for r in completed["rows"]])


@app.get("/settings/scoring")
def scoring_alias():
    return redirect(url_for("scoring_page"))


@app.get("/scoring")
def scoring_page():
    _, benchmarks = service.catalogue()
    settings, warning = configuration.load(configuration.location(), benchmarks)
    return render_template("leaderboard/scoring.html", settings=settings, warning=warning, options=service.options(benchmarks),
                           rows=_document()["rows"], axes=configuration.AXES, capabilities=configuration.CAPABILITIES,
                           domains=configuration.DOMAINS, writable=shared._local_client(), revision=service.revision())


@app.route("/api/scoring", methods=["GET", "POST"])
def scoring_data():
    _, benchmarks = service.catalogue()
    if request.method == "GET":
        settings, warning = configuration.load(configuration.location(), benchmarks)
        return jsonify(settings=settings, warning=warning, revision=service.revision())
    shared._require_csrf()
    if not shared._local_client():
        abort(403)
    try:
        path = configuration.location()
        path.parent.mkdir(parents=True, exist_ok=True)
        with ondisk.locked(str(path) + ".lock"):
            if request.headers.get("If-Match") and request.headers["If-Match"] != service.revision():
                return jsonify(error="Scoring settings changed in another page. Reload before editing."), 409
            data = request.get_json(silent=True)
            if data == {"reset": True}:
                data = configuration.defaults(benchmarks)
            settings = configuration.save(path, data, benchmarks)
            return jsonify(settings=settings, revision=service.revision(), message="Scoring saved")
    except (ValueError, TypeError, OverflowError):
        return jsonify(error="Invalid scoring settings. Check names, mappings, scores and weights."), 400
    except OSError:
        return jsonify(error="Scoring settings could not be saved. Retry."), 503
