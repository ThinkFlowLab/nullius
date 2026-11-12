"""What the pages share: the open workspace, its jobs, the catalogue cache, path resolution, the query helpers, the form-token guard."""
from __future__ import annotations

import glob
import hashlib
import ipaddress
from itertools import islice
import secrets
import os
from urllib.parse import urlsplit
from flask import abort, flash, g, has_request_context, request, session
from markupsafe import Markup, escape
from werkzeug.exceptions import Forbidden
from nullius import importers as harnesses
from nullius import execution
from nullius.catalogue import catalog
from nullius import workspace
from nullius import settings, paths
from nullius.redact import redact

from .core import app


_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # app/, one up from this package
# User settings, loaded at start and read again before any request once it has
# changed on disk (settings.sync), so a key written by hand while the app
# runs counts from the next request, without a restart; existing environment
# variables win. The Settings page edits the same file (nullius.settings
# knows every variable read here).
ENV_FILE = paths.settings_file()
settings.load(ENV_FILE)


def _host_name(host: str) -> str:
    """A Host header's name without its port: "[::1]:5057" is "::1"."""
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _served_name(name: str) -> bool:
    """A name this server answers to. A page on a name that resolves here
    (DNS rebinding) would otherwise be same-origin with the app and pass the
    loopback, Origin and form-token guards alike; an address literal or
    localhost cannot be such a page's origin. The --host name it was started
    with is accepted as well."""
    name = name.lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost") or name == str(app.config.get("SERVED_HOST", "")).lower():
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


@app.before_request
def _refuse_foreign_host():
    if not _served_name(_host_name(request.host)):
        abort(421, description="This server answers on its own address only (localhost, an IP address or its --host name).")


@app.before_request
def _sync_env_file():
    """The file as it is now: a hand edit applies to this request and every
    one after it. A file that cannot be read leaves the settings as loaded."""
    try:
        settings.sync(ENV_FILE)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        app.logger.warning("%s not read: %s", os.path.basename(ENV_FILE), getattr(e, "strerror", None) or e)


# What each benchmark actually measures. Keyed by the dataset name Harbor
# records in the job config; unknown datasets fall back to a stub so a new
# benchmark shows up in the UI rather than vanishing.
DATASETS = {
    "swe-bench-verified": {
        "title": "SWE-bench Verified",
        "what": (
            "500 real GitHub issues drawn from 12 widely used Python projects — Django, "
            "SymPy, scikit-learn, matplotlib, requests and others. Each task hands the agent "
            "a repository at the commit before the fix, plus the issue text."
        ),
        "scored": (
            "The agent must produce a patch. It is applied to a clean checkout and the test "
            "suite is run: FAIL_TO_PASS tests must flip from failing to passing, and "
            "PASS_TO_PASS tests must stay green — so a patch cannot pass by breaking "
            "unrelated behaviour."
        ),
        "note": (
            "The “Verified” subset was human-annotated to remove tasks that were unsolvable "
            "or had broken tests, so a failure here reflects the agent, not the benchmark."
        ),
    },
    "swe-bench": {
        "title": "SWE-bench",
        "what": (
            "Real GitHub issues from open-source Python projects, each paired with the "
            "repository at the commit before the fix. The agent reads the issue and must "
            "produce a working patch."
        ),
        "scored": (
            "The patch is applied to a clean checkout and the project's tests are run: "
            "FAIL_TO_PASS tests must flip from failing to passing, and PASS_TO_PASS tests "
            "must stay green."
        ),
        "note": (
            "Recorded simply as “swe-bench” by the harness, so the exact split "
            "(full, Lite or Verified) isn't distinguished here."
        ),
    },
    "terminal-bench": {
        "title": "Terminal-Bench",
        "what": (
            "Tasks that exercise a real terminal inside an isolated container — building and "
            "configuring software, debugging a broken environment, driving command-line tools "
            "end to end. From the same team as Harbor."
        ),
        "scored": (
            "Each task ships a test script that runs after the agent finishes. The task counts "
            "as solved only if that script exits clean, so the agent is judged on the final "
            "state of the machine rather than on what it said it did."
        ),
        "note": "",
    },
}


def dataset_title(name):
    """Human name for a dataset slug, falling back to the slug itself."""
    entry = DATASETS.get(name)
    return entry["title"] if entry else name


def dataset_info(name):
    return DATASETS.get(name) or {
        "title": name,
        "what": "No description recorded for this dataset yet.",
        "scored": "",
        "note": "",
    }


# Runs-table columns, in render order. Drives the show/hide menu; the template
# tags every th/td with the matching data-col.
COLUMNS = [
    ("run", "Run"), ("agent", "Agent"), ("version", "Version"), ("start", "Start"), ("end", "End"), ("time", "Time"),
    ("model", "Model"), ("dataset", "Dataset"), ("mix", "Mix"),
    ("trials", "Trials"), ("solved", "Solved"), ("raw", "Raw"),
    ("adjusted", "Adjusted"), ("infra", "Infra errors"), ("broke", "Broke tests"),
    ("tokens", "Tokens"), ("cost", "Cost"),
]


# Active roots, mutable at runtime via the workspace picker. Seeded at
# startup from CLI args / env; roots may be empty, in which case the UI asks.
# `label` is what the header shows — roots can be an internal detail (an upload
# scans a temp dir *and* its child, which is meaningless to read).
# The open workspace, and what it resolved to. `roots` and `label` are what
# the user pointed at; `job_roots` is after the foreign trees among them were
# converted — the directories parse actually walks. Conversion happens when
# the workspace is opened or refreshed, not on every request.
STATE: dict = {"roots": [], "label": "", "workspace": None, "job_roots": []}
WS_HOME = workspace.home()


# A path containing any of these is treated as a glob, so one entry can name
# several run directories: jobs_*, current/jobs_*/2026-08-*, **/jobs.
GLOB_CHARS = "*?["


def is_pattern(value) -> bool:
    return any(ch in (value or "") for ch in GLOB_CHARS)


def resolve_submitted_paths(values):
    """Split submitted paths into (existing dirs, unusable), preserving order.

    Entries containing glob metacharacters expand to every directory they
    match, so `.../jobs_*` picks up all of them in one field. Non-matching
    patterns are reported like a missing directory rather than silently
    resolving to nothing.
    """
    good, bad = [], []
    for raw in values:
        raw = (raw or "").strip()
        if not raw:
            continue
        resolved = os.path.abspath(os.path.expanduser(raw))
        if is_pattern(raw):
            matches = sorted(p for p in glob.glob(resolved, recursive=True) if os.path.isdir(p))
            if not matches:
                bad.append(raw)
            for p in matches:
                if p not in good:
                    good.append(p)
        elif os.path.isdir(resolved):
            if resolved not in good:
                good.append(resolved)
        else:
            bad.append(raw)
    return good, bad


def suggest_directories(value):
    """Complete one path component, including linked and mounted directories."""
    value = (value or "").lstrip()
    if not value or len(value) > 4096 or "\0" in value or is_pattern(value):
        return []
    expanded = os.path.expanduser(value)
    if value == "~":
        expanded += os.sep
    parent, prefix = os.path.split(expanded)
    parent = os.path.abspath(parent or ".")
    matches = []
    try:
        with os.scandir(parent) as entries:
            # Suggestions never recurse or enumerate an unbounded directory.
            for entry in islice(entries, 5000):
                if not os.path.normcase(entry.name).startswith(os.path.normcase(prefix)):
                    continue
                if entry.name.startswith(".") and not prefix.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        matches.append(os.path.join(parent, entry.name) + os.sep)
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(matches, key=os.path.normcase)[:20]


def _bad_paths_message(bad):
    """Patterns that matched nothing read differently from a missing path."""
    patterns = [p for p in bad if is_pattern(p)]
    plain = [p for p in bad if not is_pattern(p)]
    parts = []
    if plain:
        listed = ", ".join(f'<span class="mono">{escape(p)}</span>' for p in plain)
        parts.append(f"No {'directory' if len(plain) == 1 else 'directories'} at {listed}")
    if patterns:
        listed = ", ".join(f'<span class="mono">{escape(p)}</span>' for p in patterns)
        parts.append(f"Nothing matches {listed}")
    return Markup(" · ".join(parts))


def set_source(roots, label=None) -> None:
    """Point the app at *roots*, with *label* as the human-facing name."""
    STATE["roots"] = roots
    if label:
        STATE["label"] = label
    elif len(roots) == 1:
        STATE["label"] = roots[0]
    else:
        STATE["label"] = f"{roots[0]} +{len(roots) - 1}" if roots else ""


def _import_roots(roots, import_root):
    """Convert any root a foreign harness recognises, returning job roots.

    Which harness wrote a run is a property of the directory, not a question
    for whoever opens it: the shapes are distinguishable, and someone handed a
    results folder often cannot answer. Detection identifies it, and
    a harness added to `nullius/importers/` is readable here without a UI
    change — the point of the registry.

    What one job is under a claimed root is the importer's answer
    (`harnesses.trees`), matching the CLI's job boundaries. A root nothing
    claims goes through as it is: a Harbor job directory or a parent of
    them, which `find_job_dirs` walks.

    A tree the importer claims but refuses is left out, so one bad tree
    never costs the others or the process — but not silently: the reason,
    redacted where it was raised, is flashed to the page being served, where
    the picker shows it in place of "Nothing job-shaped", and logged when
    there is no page (a workspace reopened at start-up).
    """
    resolved, seen = [], set()

    def take(path):
        if path not in seen:
            seen.add(path)
            resolved.append(path)

    for root in roots:
        module = harnesses.detect(root) if os.path.isdir(root) else None
        if module is None:
            take(root)
            continue
        for tree in harnesses.trees(module, root):
            try:
                for job_dir in module.convert(tree, import_root):
                    take(job_dir)
            except (OSError, SystemExit) as e:
                why = redact(str(e) or f"{module.NAME} could not read it")
                app.logger.warning("Not imported: %s — %s", redact(tree), why)
                if has_request_context():
                    flash(Markup(f'Not imported: <span class="mono">{escape(redact(tree))}</span> '
                                 f'— {escape(why)}'), "error")
    return resolved


def open_workspace(ws, refresh=False):
    """Make *ws* the one the app reads: convert its foreign roots once, then
    every request walks the result and reads parsed jobs from its cache."""
    STATE["workspace"] = ws
    set_source(ws.roots, ws.label)
    if refresh:
        workspace.clear_cache(ws)
    STATE["job_roots"] = _import_roots(ws.roots, ws.imports_dir)
    ws.job_roots = list(STATE["job_roots"])
    workspace.touch(ws)


def close_workspace():
    STATE.update({"workspace": None, "roots": [], "label": "", "job_roots": []})


def adopt_roots(roots, label="", new=False):
    """Roots the user just pointed at: into the open workspace, or a new one.

    Without a label, roots that already have a workspace reopen it — so a
    restart with the same argv lands in the same place, cache and all.
    """
    ws = STATE["workspace"]
    if ws and not new:
        ws.roots = list(ws.roots) + [r for r in roots if r not in ws.roots]
        workspace.save(ws)
        open_workspace(ws)
        return ws
    ws = None if label else workspace.find_by_roots(WS_HOME, roots, besides=execution.runs_root())
    ws = ws or workspace.create(WS_HOME, roots, label)
    open_workspace(ws)
    return ws


def _load_jobs():
    ws = STATE["workspace"]
    if ws is None:
        return []
    return workspace.load_jobs(ws, STATE["job_roots"])


def _jobs_for_workspace(name=None):
    """Read an indexed workspace without making it active or converting sources.
    Once per request and workspace: a page that asks for the jobs several
    times (the job page's controls, the rail, a summary) walks them once."""
    memo = g.setdefault("_jobs", {}) if has_request_context() else {}
    key = name if name and not (STATE["workspace"] and name == STATE["workspace"].name) else ""
    if key in memo:
        return memo[key]
    if not key:
        jobs = _load_jobs()
    else:
        ws = workspace.load(WS_HOME, name)
        if ws is None:
            abort(404)
        jobs = workspace.load_jobs(ws, ws.job_roots or ws.roots)
    memo[key] = jobs
    return jobs


def _record_workspace():
    name = request.args.get("workspace") if has_request_context() else None
    if not name:
        return STATE["workspace"]
    ws = workspace.load(WS_HOME, name)
    if ws is None:
        abort(404)
    return ws


def _jobs_for_request():
    return _jobs_for_workspace(request.args.get("workspace"))


@app.url_defaults
def _preserve_record_workspace(endpoint, values):
    """Carry evidence provenance through links, forms, and lazy detail requests."""
    record_endpoints = {"job", "trial", "save_trace", "artifact_page", "conversation_data",
                        "trace_span_data"}
    if has_request_context() and endpoint in record_endpoints and "workspace" not in values:
        name = request.args.get("workspace")
        if name:
            values["workspace"] = name


def _find_job(jobs, job_id):
    return next((j for j in jobs if j.job_id == job_id), None)


def _safe_dest(base: str, rel: str) -> str | None:
    """Join *rel* onto *base*, refusing anything that escapes *base*."""
    dest = os.path.normpath(os.path.join(base, rel))
    if os.path.commonpath([base, dest]) != base:
        return None
    return dest


@app.template_global()
def view_query(filt, chart_filt):
    """Every argument a trial view is defined by, as one set of `url_for` kwargs.

    The two filters answer different questions — `trace.Filter` prunes spans out
    of the whole view, `charts.ChartFilter` only says which kinds one chart
    draws — but a link has to carry both, or clicking either control silently
    discards the other. Each still owns the naming of its own query keys.
    """
    return {**filt.query, **chart_filt.query}


@app.template_global()
def task_labels_for(job):
    """Compact labels for this run only; IDs and ambiguous names stay intact."""
    dataset = os.path.basename((job.dataset or "").rstrip("/")).split("@", 1)[0]
    prefix = f"{dataset}__" if dataset and dataset != "mixed" else ""
    labels, originals = {}, {}
    for trial in job.trials:
        full = trial.task_base
        short = full[len(prefix):] if prefix and full.startswith(prefix) else full
        short = short or full
        labels[trial.trial_name] = short
        originals.setdefault(short, set()).add(full)
    return {trial.trial_name: (trial.task_base if len(originals[labels[trial.trial_name]]) > 1
                               else labels[trial.trial_name])
            for trial in job.trials}


_CATALOG_CACHE: dict = {"signature": None, "cat": None}


def _catalog_cached():
    # read again when any of its files changes, or a benchmark folder comes or goes
    signature = catalog.signature()
    if all(mtime is None for _, mtime in signature):
        return None
    if _CATALOG_CACHE["signature"] != signature:
        _CATALOG_CACHE.update(signature=signature, cat=catalog.load())
    return _CATALOG_CACHE["cat"]


def _csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def _origin_of(url: str) -> str:
    """scheme://host[:port] of an Origin or Referer, compared whole: a prefix
    test would let http://127.0.0.1:50570 pass for :5057."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


# Every page's forms send it: the workspace menu in base.html posts from anywhere.
app.jinja_env.globals["csrf_token"] = lambda: _csrf_token()


def _interface_id() -> str:
    """A digest of the templates, static files and web routes a page is made of
    and reads. A page rendered under another digest runs code this process no
    longer serves."""
    digest = hashlib.sha256()
    for root in (os.path.join(app.root_path, app.template_folder), app.static_folder, os.path.join(app.root_path, "web")):
        for folder, subfolders, names in os.walk(root):
            subfolders[:] = sorted(d for d in subfolders if d != "__pycache__")   # written at import, not code
            for name in sorted(names):
                path = os.path.join(folder, name)
                digest.update(os.path.relpath(path, root).encode() + b"\0")
                with open(path, "rb") as f:
                    digest.update(f.read())
    return digest.hexdigest()[:16]


INTERFACE = _interface_id()
app.jinja_env.globals["interface_id"] = lambda: INTERFACE


@app.after_request
def _send_interface(response):
    response.headers["X-Nullius-Interface"] = INTERFACE
    return response


class StaleFormToken(Forbidden):
    """A token the session does not hold, as from a page loaded before a restart.
    Its header tells the page to ask for a reload instead of showing the error."""
    description = "Stale form token; reload the page and try again."

    def get_headers(self, environ=None, scope=None):
        return [*super().get_headers(environ, scope), ("X-Nullius-Reason", "stale-form-token")]


def _require_csrf():
    """The one guard every mutation of the environments takes: the session's
    token, sent back by the form, and a same-origin request."""
    sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
    if not sent:
        abort(403, description="Missing form token; reload the page and try again.")
    if not secrets.compare_digest(sent, session.get("csrf", "")):
        raise StaleFormToken()
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin and _origin_of(origin) != request.host_url.rstrip("/").lower():
        abort(403, description="Cross-origin requests cannot change environments.")
