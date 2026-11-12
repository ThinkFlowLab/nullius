"""The Settings page and its one Save."""
from __future__ import annotations

import os
from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from nullius import execution, hosts, paths, prices, sandbox, secrets, settings
from nullius.standard import container
from nullius.standard import recipe as standard_recipe
from nullius.judge import client

from .core import app
from . import shared


def _local_client() -> bool:
    """Only a browser on this machine may write settings: the request comes
    from a loopback address and nothing says it was forwarded."""
    forwarded = any(h in request.headers for h in ("X-Forwarded-For", "X-Real-Ip", "Forwarded"))
    return request.remote_addr in ("127.0.0.1", "::1") and not forwarded


@app.route("/settings")
def settings_page():
    """Every variable the application reads, its state and where it comes
    from; a secret by presence only."""
    rows = settings.states(shared.ENV_FILE)
    withheld = set(execution.withheld())             # what a run never inherits, read or not
    groups = [(g, [r for r in rows if r["group"] == g]) for g in settings.GROUPS]
    access = []
    filed = settings.in_file(shared.ENV_FILE)
    for benchmark in shared._catalog_cached().benchmarks:
        if "standard" not in benchmark.harness:
            continue
        needs = {}
        for environment in standard_recipe.licences_of(benchmark.key):
            for secret in environment["needs"]:
                if secret.get("provider"):                   # a data provider's token: Data providers alone
                    continue
                needs.setdefault(secret["name"], dict(secret, movable=secret["name"] in filed))
        if needs:
            access.append({"key": benchmark.key, "title": benchmark.title, "needs": list(needs.values())})
    # A written value that goes unseen is said rather than left silent: a key no setting reads.
    return render_template("settings/settings.html", embedded=request.args.get("embedded") == "1", groups=groups, benchmark_access=access, csrf=shared._csrf_token(), writable=_local_client(),
                           env_file=shared.ENV_FILE,
                           storage_paths=[("Settings", shared.ENV_FILE), ("User catalogue", paths.catalogue_dir()),
                                          ("Workspaces", paths.workspaces_dir()), ("Runs", paths.runs_dir()),
                                          ("Benchmark licences", paths.secrets_dir()), ("Model prices", paths.prices_file()),
                                          ("Cache", paths.cache_dir())],
                           unread=[dict(u, inherited=u["name"] not in withheld, startup=u["instead"] in settings.STARTUP)
                                   for u in settings.unread(shared.ENV_FILE)],
                           prices=prices.state(),
                           ssh_hosts=hosts.scan_ssh_config(), judges=(client.system_one(os.environ), client.llm_judge(os.environ)), llm_providers=client.LLM_PROVIDERS, s1_providers=client.SYSTEM_ONE_PROVIDERS, agent_keys=settings.agent_key_names())


@app.route("/settings/hosts/test", methods=["POST"])
def settings_host_test():
    """One host, one ssh: does it answer without a prompt, and are uv, docker
    and harbor there. Same guards as Save; nothing is stored, nothing but
    the probe is sent. The entry need not be saved yet."""
    shared._require_csrf()
    if not _local_client():
        abort(403, description="Hosts are tested from a browser on the application host only.")
    try:
        host = hosts.parse(request.form.get("entry", ""))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(hosts.test(host))


def _host_form():
    """user, host, port from the Add host form, checked as an entry would be."""
    user, host, port = request.form.get("user", "").strip(), request.form.get("host", "").strip(), request.form.get("port", "").strip()
    parsed = hosts.parse(f"{user}@{host}" + (f":{port}" if port else ""))
    return parsed.user, parsed.host, parsed.port


@app.route("/settings/hosts/key", methods=["POST"])
def settings_host_key():
    """A key file for a manual host, kept under ~/.ssh/nullius with mode
    0600; the entry that names it comes back. Same guards as Save."""
    shared._require_csrf()
    if not _local_client():
        abort(403, description="Hosts are added from a browser on the application host only.")
    upload = request.files.get("key")
    try:
        user, host, port = _host_form()
        if upload is None or not upload.filename:
            raise ValueError("no key file")
        path = hosts.save_key(user, host, upload.read(20001))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except OSError as e:
        return jsonify(ok=False, error=f"not written: {e.strerror or e}"), 500
    return jsonify(ok=True, entry=f"{user}@{host}" + (f":{port}" if port else "") + f":{path}")


@app.route("/settings/hosts/enrol", methods=["POST"])
def settings_host_enrol():
    """A password, used once: Nullius's own public key goes into the host's
    authorized_keys over ssh, and the entry that comes back names that key.
    The password is neither stored nor logged. Same guards as Save."""
    shared._require_csrf()
    if not _local_client():
        abort(403, description="Hosts are added from a browser on the application host only.")
    try:
        user, host, port = _host_form()
        password = request.form.get("password", "")
        if not password:
            raise ValueError("no password")
        entry = hosts.enrol(user, host, port, password)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except RuntimeError as e:
        return jsonify(ok=False, error=str(e)), 502
    return jsonify(ok=True, entry=entry.entry)


@app.route("/settings/sandbox")
def settings_sandbox():
    """The Sandbox section's state, asked when the section opens: docker and
    the images Nullius fetched."""
    state = sandbox.docker_state()
    if not state["ok"]:
        return jsonify(docker=state, images=[])
    try:
        held = sandbox.images()
    except container.Unavailable as e:
        held, state = [], {"ok": False, "error": str(e)}
    return jsonify(docker=state, images=held)


@app.route("/settings/data-providers")
def settings_data_providers():
    """Connection metadata only; no credential is returned."""
    return jsonify(huggingface=bool(secrets.status(secrets.HUGGINGFACE).get("HF_TOKEN", {}).get("set")),
                   dockerhub=sandbox.login_user())


@app.route("/settings/data-providers/huggingface", methods=["POST"])
def settings_huggingface():
    _sandbox_guard("Data providers are changed")
    action = request.form.get("action", "save")
    try:
        if action == "remove":
            secrets.delete(secrets.HUGGINGFACE, "HF_TOKEN")
        elif action == "save":
            secrets.set(secrets.HUGGINGFACE, "HF_TOKEN", request.form.get("token", ""))
        else:
            return jsonify(ok=False, error="Unknown action."), 400
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except OSError:
        return jsonify(ok=False, error="Could not update the credential store."), 500
    return jsonify(ok=True)


def _sandbox_guard(what):
    shared._require_csrf()
    if not _local_client():
        abort(403, description=f"{what} from a browser on the application host only.")


@app.route("/settings/sandbox/login", methods=["POST"])
def settings_sandbox_login():
    """A Docker Hub username and access token, handed to docker login once;
    neither is stored or logged by Nullius. Same guards as Save."""
    _sandbox_guard("Docker logs in")
    got = sandbox.login(request.form.get("username", ""), request.form.get("token", ""))
    return jsonify(got), (200 if got["ok"] else 400)


@app.route("/settings/sandbox/logout", methods=["POST"])
def settings_sandbox_logout():
    _sandbox_guard("Docker logs out")
    got = sandbox.logout()
    return jsonify(got), (200 if got["ok"] else 502)


@app.route("/settings/sandbox/images/remove", methods=["POST"])
def settings_sandbox_remove():
    """Every image Nullius fetched for one benchmark, removed: the one way
    they go, since runs keep them. Same guards as Save."""
    _sandbox_guard("Images are removed")
    bench = request.form.get("benchmark", "").strip()
    if not bench:
        return jsonify(ok=False, error="no benchmark"), 400
    try:
        got = sandbox.remove(bench)
    except container.Unavailable as e:
        return jsonify(ok=False, error=str(e)), 502
    return jsonify(got), (200 if got["ok"] else 409)


@app.route("/settings/prices/update", methods=["POST"])
def settings_prices_update():
    """The public price list fetched and kept (``nullius.prices``): the one
    fetch, asked for here and never by a run. Same guards as Save."""
    _sandbox_guard("Prices are updated")
    try:
        prices.update()
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 502
    return jsonify(ok=True, **prices.state())


@app.route("/settings/prices/set", methods=["POST"])
def settings_prices_set():
    """A price of the user's own for one model, over the list."""
    _sandbox_guard("Prices are set")
    f = request.form
    try:
        prices.set_own(f.get("model", ""), prices.row(f.get("input") or None, f.get("output") or None, f.get("cached") or None))
    except (ValueError, TypeError) as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, **prices.state())


@app.route("/settings/prices/remove", methods=["POST"])
def settings_prices_remove():
    _sandbox_guard("Prices are removed")
    if not prices.remove_own(request.form.get("model", "")):
        return jsonify(ok=False, error="not one of your own prices"), 404
    return jsonify(ok=True, **prices.state())


def _settings_return():
    return url_for("settings_page", embedded="1") if request.form.get("embedded") == "1" else url_for("settings_page")


def _saved_response(message, ok=True, anchor="", retryable=False):
    if request.accept_mimetypes.best == "application/json":
        # Re-render only masked state, never echo a submitted credential.
        return jsonify(ok=ok, message=message, retryable=retryable, html=settings_page() if ok else ""), (200 if ok else 500 if retryable else 400)
    flash(message, "ok" if ok else "error")
    return redirect(_settings_return() + anchor)


@app.route("/settings/save", methods=["POST"])
def settings_save():
    """One form, one Save: every variable the page shows, in user settings. Same
    guards as the environment mutations, plus: loopback clients only; every
    value validated for its kind before any is written; values never echoed
    back or logged. A field left empty keeps what is set; the row's × removes
    the variable from the file. A variable the process environment also holds
    is written to the file for the next start, and the message says so."""
    shared._require_csrf()
    if not _local_client():
        abort(403, description="Settings are written from a browser on the application host only.")
    current = {s["name"]: s for s in settings.states(shared.ENV_FILE)}
    changes = {}
    names = settings.agent_key_names()
    for var in settings.VARIABLES + [settings.agent_variable(n) for n in names]:
        posted = request.form.get("value:" + var.name)
        if posted is None:
            continue
        posted = posted.strip()
        if request.form.get("clear:" + var.name) == "1":
            if current[var.name]["source"] != "unset":
                changes[var.name] = None
        elif posted and (var.secret or posted != current[var.name]["shown"]):
            changes[var.name] = posted
    # Agent keys: one added by name and value, one removed with its name; the list of names follows them
    keep = [n for n in names if changes.get(n, "") is not None]
    new_name = (request.form.get("agent_key_name") or "").strip().upper()
    new_value = (request.form.get("agent_key_value") or "").strip()
    if new_name or new_value:
        if not (new_name and new_value):
            return _saved_response("A custom key needs its name and its value.", False, "#custom-keys")
        if new_name not in keep:
            keep.append(new_name)
        changes[new_name] = new_value
    if keep != names:
        changes["AGENT_KEYS"] = ",".join(keep) if keep else None
    if not changes:
        return _saved_response("Nothing changed.")
    try:
        settings.save_many(shared.ENV_FILE, changes)
    except ValueError as e:
        return _saved_response(str(e), False)
    except OSError as e:
        return _saved_response(f"Not written: {e.strerror or e}", False, retryable=True)
    later = any(current.get(n, {}).get("source") == "environment" for n in changes)
    return _saved_response("Saved. Environment overrides still apply." if later else "Saved")
