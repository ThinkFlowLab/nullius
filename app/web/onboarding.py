"""Getting started: the guide's checklist, read fresh on each ask
(`nullius.onboarding`). Booleans only: no key or path leaves in it."""
from __future__ import annotations

import os

from flask import jsonify, url_for

from nullius import onboarding, workspace

from .core import app
from . import shared


@app.route("/onboarding")
def onboarding_state():
    ws = shared.STATE["workspace"]
    jobs = workspace.job_index(ws).values() if ws else ()
    found = onboarding.facts(os.environ, jobs, onboarding.any_judged(ws.dir if ws else None))
    return jsonify(sections=onboarding.checklist(found, url_for))
