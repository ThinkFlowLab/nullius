"""The Settings page and its one Save."""
from __future__ import annotations

import os
from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from nullius import execution, hosts, paths, prices, providers, sandbox, settings
from nullius.standard import container
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
    # A written value that goes unseen is said rather than left silent: a key no setting reads.
    return render_template("settings/settings.html", embedded=request.args.get("embedded") == "1", groups=groups, csrf=shared._csrf_token(), writable=_local_client(),
                           env_file=shared.ENV_FILE,
                           storage_paths=[("Settings", shared.ENV_FILE), ("User catalogue", paths.catalogue_dir()),
                                          ("Workspaces", paths.workspaces_dir()), ("Runs", paths.runs_dir()),
                                          ("Benchmark licences", paths.secrets_dir()), ("Model prices", paths.prices_file()),
                                          ("Cache", paths.cache_dir())],
                           unread=[dict(u, inherited=u["name"] not in withheld, startup=u["instead"] in settings.STARTUP)
                                   for u in settings.unread(shared.ENV_FILE)],
                           prices=prices.state(),
                           ssh_hosts=hosts.scan_ssh_config(), judges=(client.system_one(os.environ), client.llm_judge(os.environ)), llm_providers=client.llm_providers(os.environ), s1_providers=client.SYSTEM_ONE_PROVIDERS, agent_keys=settings.agent_key_names(),
                           endpoints=[(i, os.environ.get(f"MODEL_{i}_NAME") or i) for i in settings.endpoint_ids()],
                           tested={k: _shown(v) for k, v in providers.tests().items() if isinstance(v, dict)})


def _shown(result: dict) -> dict:
    """A provider's test as its card shows it: the result, its line and its mark."""
    return dict(result, said=providers.said(result), mark=providers.mark(result))


@app.route("/settings/providers/test", methods=["POST"])
def settings_provider_test():
    """One provider asked with the saved settings, at no cost (``providers.test``), and its result kept
    for the card and Review; only when Test is pressed. Same guards as Save."""
    _sandbox_guard("Providers are tested")
    choice = request.form.get("provider", "")
    try:
        got = providers.test(choice)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    providers.remember({choice: got})
    return jsonify(_shown(got))


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
    """The Sandbox section's state, asked when the section opens: docker, the
    Docker Hub login (its username only) and the images Nullius fetched."""
    state = sandbox.docker_state()
    if not state["ok"]:
        return jsonify(docker=state, user=None, images=[])
    try:
        held = sandbox.images()
    except container.Unavailable as e:
        held, state = [], {"ok": False, "error": str(e)}
    return jsonify(docker=state, user=sandbox.login_user(), images=held)


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
        prices.set_own(f.get("model", ""), prices.row(f.get("input") or None, f.get("output") or None, f.get("cached") or None,
                                                   f.get("cache_write") or None, cache_write_1h=f.get("cache_write_1h") or None))
    except (ValueError, TypeError) as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, **prices.state())


@app.route("/settings/prices/remove", methods=["POST"])
def settings_prices_remove():
    _sandbox_guard("Prices are removed")
    if not prices.remove_own(request.form.get("model", "")):
        return jsonify(ok=False, error="not one of your own prices"), 404
    return jsonify(ok=True, **prices.state())


# The settings each built-in provider card holds; an OpenAI-compatible endpoint's are its own.
_PROVIDER_SETTINGS = {"typesafe": ["TYPESAFE_API_KEY"], "openrouter": ["OPENROUTER_API_KEY"], "openai": ["OPENAI_API_KEY"],
                      "anthropic": ["ANTHROPIC_API_KEY"], "local": ["MODEL_API_URL", "MODEL_API_KEY"]}


def _provider_settings(card: str, ids) -> list:
    """The settings a provider card names (``local:<id>``, an endpoint), or [] for none."""
    kind, _, i = card.partition(":")
    if kind == "local" and i:
        return settings.endpoint_settings(i) if i in ids else []
    return _PROVIDER_SETTINGS.get(card, [])


def _settings_return():
    return url_for("settings_page", embedded="1") if request.form.get("embedded") == "1" else url_for("settings_page")


def _saved_response(message, ok=True, anchor="", retryable=False, reload=False):
    if request.accept_mimetypes.best == "application/json":
        # Re-render only masked state, never echo a submitted credential. reload: the page's cards changed.
        return jsonify(ok=ok, message=message, retryable=retryable, reload=reload, html=settings_page() if ok else ""), (200 if ok else 500 if retryable else 400)
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
    ids = settings.endpoint_ids()
    for var in (settings.VARIABLES + [settings.agent_variable(n) for n in names]
                + [settings.endpoint_variable(n) for i in ids for n in settings.endpoint_settings(i)]):
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
            return _saved_response("An agent key needs its name and its value.", False, "#agent-keys")
        if new_name not in keep:
            keep.append(new_name)
        changes[new_name] = new_value
    if keep != names:
        changes["AGENT_KEYS"] = ",".join(keep) if keep else None
    # A provider removed from its card: every setting of it, refused while the process environment holds one
    gone = (request.form.get("remove_provider") or "").strip()
    if gone:
        held = _provider_settings(gone, ids)
        if not held:
            return _saved_response("Not a provider on this page.", False, "#providers")
        env = [n for n in held if current[n]["source"] == "environment"]
        if env:
            return _saved_response(f"{', '.join(env)} is set in the process environment; remove it there.", False, "#providers")
        changes.update({n: None for n in held if current[n]["source"] != "unset"})
    # An endpoint added by name and URL, its key optional
    new = {part: (request.form.get("endpoint_" + part) or "").strip() for part in ("name", "url", "key")}
    if any(new.values()):
        if not (new["name"] and new["url"]):
            return _saved_response("An endpoint needs its name and its URL.", False, "#providers")
        i = settings.new_endpoint_id(new["name"], set(ids) | set(settings.endpoint_ids(settings.in_file(shared.ENV_FILE))))
        changes.update({f"MODEL_{i}_NAME": new["name"], f"MODEL_{i}_API_URL": new["url"], **({f"MODEL_{i}_API_KEY": new["key"]} if new["key"] else {})})
    cards = bool(gone) or any(settings.endpoint_variable(n) and (n.endswith("_NAME") or n not in current or changes[n] is None) for n in changes)
    if not changes:
        return _saved_response("Nothing changed.")
    try:
        settings.save_many(shared.ENV_FILE, changes)
    except ValueError as e:
        return _saved_response(str(e), False)
    except OSError as e:
        return _saved_response(f"Not written: {e.strerror or e}", False, retryable=True)
    # a provider whose settings changed is no longer what its last test asked
    stale = {card.replace("local", "custom", 1): None for card in [*_PROVIDER_SETTINGS, *(f"local:{i}" for i in ids)]
             if set(_provider_settings(card, ids)) & set(changes)}
    if stale.keys() & providers.tests().keys():
        providers.remember(stale)
    later = any(current.get(n, {}).get("source") == "environment" for n in changes)
    return _saved_response("Saved. Environment overrides still apply." if later else "Saved", reload=cards)
