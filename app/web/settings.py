"""The Settings page and its one Save."""
from __future__ import annotations

import os
import shutil
import socket
from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from nullius import constraints, execution, hosts, paths, prerequisites, prices, providers, sandbox, secrets, settings
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
                           machines=_machines(), constraint_table=constraints.TABLE,
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


def _size(mb: int) -> str:
    """Memory as the constraint fields read it: whole gigabytes in GB, else MB."""
    return f"{mb // 1024} GB" if mb and mb % 1024 == 0 else f"{mb} MB"


def _engine(found: dict, whole: bool = False) -> str:
    """The engine a machine's last test found: its name, as the list says it,
    or ``whole`` with its version and how it runs, as its Info says it."""
    e, t = found.get("engine") or {}, found.get("test") or {}
    if e:
        name = "Podman" if e["kind"] == "podman" else "Docker"
        return name + (f" {e['version']}" + (", rootless" if e.get("rootless") else "")
                       + (", userns-remap" if e.get("userns_remap") else "") if whole else "")
    if not found:
        return "Not tested"
    if not t.get("ok"):
        return "Unknown"                     # not reached
    return f"{t['engine'].capitalize()} does not answer" if t.get("engine") else "None"


def _capacity(cap: dict) -> str:
    """A machine's capacity in a few words: CPUs, memory, its GPUs and their
    memory, rounded to whole GB (MB below one); Info has the exact values."""
    if not cap:
        return "Not tested"
    gb = lambda mb: f"{round(mb / 1024)} GB" if mb >= 1024 else f"{mb} MB"
    gpus = cap.get("gpus") or []
    return " · ".join([*([f"{cap['cpus']} CPUs"] if cap.get("cpus") else []), *([gb(cap["memory_mb"])] if cap.get("memory_mb") else []),
                       *([f"{len(gpus)} GPU{'s' if len(gpus) > 1 else ''}, {gb(sum(g['memory_mb'] for g in gpus))}"] if gpus else [])])


def _machine(entry: str, label: str, host=None, found=None) -> dict:
    """One machine as the Run hosts list shows it: its constraints, what its
    last test found (``found``, ``constraints.load``) and what Settings warns of."""
    values, cap = constraints.of(entry, strict=False), (found or {}).get("capacity") or {}   # an invalid setting: said above the list
    label_of = lambda k: constraints.BY_KEY[k].label if k in constraints.BY_KEY else k
    sd = (found or {}).get("systemd")
    return {"entry": entry, "label": label, "host": host, "found": found or {}, "engine_less": constraints.engine_less(found),
            "scope_ok": constraints.scope_ok(found),
            # a scope's: what it held, or why there is none; the test probes with samples
            "scope": sd and {"why": sd.get("why", ""), "enforced": [label_of(k) for k in sd.get("enforced") or []],
                             "refused": [(label_of(k), constraints.explain(k, constraints.samples(cap.get("local") or cap).get(k, ""), why, scope=True)
                                          + ("; applied instead: the process can widen it" if getattr(constraints.BY_KEY.get(k), "machine", "") else ""))
                                         for k, why in (sd.get("refused") or {}).items()]},
            "not_enforced": len(constraints.not_enforced(values, found)), "undelegated": constraints.undelegated(found),
            "engine_name": _engine(found or {}), "engine_text": _engine(found or {}, whole=True), "capacity_text": _capacity(cap), "memory_text": _size(cap["memory_mb"]) if cap.get("memory_mb") else "",
            "local_text": ", ".join([*([f"{cap['local']['cpus']:g} CPUs"] if "cpus" in cap.get("local", {}) else []),
                                     *([_size(cap["local"]["memory_mb"])] if "memory_mb" in cap.get("local", {}) else [])]),
            "enforced": [label_of(k) for k in (found or {}).get("enforced") or []],
            "refused": [(label_of(k), constraints.explain(k, constraints.samples(cap).get(k, ""), why))   # the test probes with samples
                        for k, why in ((found or {}).get("refused") or {}).items()],
            "warnings": constraints.warnings(values, found),
            "shown": {k: "none" if v == [] else constraints.spell(v) for k, v in values.items()},
            "capacity": {c.key: ("GPU " + ", ".join(str(g["index"]) for g in cap[c.capacity]) if cap[c.capacity] else "no GPU") if c.key == "gpu_ids"
                         else constraints.spell(cap[c.capacity]) for c in constraints.TABLE if c.capacity in cap}}


def _machines():
    """This machine, then each host."""
    try:
        listed = hosts.parse_list(os.environ.get("HOSTS", ""))
    except ValueError:
        listed = []
    found = constraints.load()
    return [_machine(constraints.LOCAL, "This machine", None, found.get(constraints.LOCAL))] + \
        [_machine(h.entry, h.label, h, found.get(h.entry)) for h in listed]


@app.route("/settings/hosts/test", methods=["POST"])
def settings_host_test():
    """One machine: for a host, one ssh - does it answer without a prompt,
    and are uv, docker and harbor there. Then, where docker answers, its
    capacity and the constraints its engine enforces (``hosts.measure``).
    What it found is kept for the machine's Info and warnings, and its Info
    comes back rendered (``info``). Same guards as Save. The entry need not be
    saved yet; ``local`` is this machine."""
    shared._require_csrf()
    if not _local_client():
        abort(403, description="Hosts are tested from a browser on the application host only.")
    entry = request.form.get("entry", "")
    if entry == constraints.LOCAL:
        # here: always reached, and asked as the Review step asks (prerequisites.check_here) - one probe, many rows
        host, rows = None, {r["name"]: r for r in prerequisites.check_here(
            ["engine", "uv", "harbor"], docker=lambda: prerequisites.docker_here(fresh=True))}
        why = None if rows["engine"]["state"] == "ok" else rows["engine"]["detail"]
        there = bool(shutil.which(container.DOCKER))
        result = {"entry": entry, "ok": True, "error": why, "hostname": socket.gethostname(),
                  "uv": rows["uv"]["state"] == "ok", "harbor": rows["harbor"]["state"] == "ok",
                  "engine": container.DOCKER if there else None, "docker": there and not container.podman(),
                  "podman": there and container.podman(), "docker_ok": why is None}
    else:
        try:
            host = hosts.parse(entry)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        result = hosts.test(host)
    found = {"test": {k: result.get(k) for k in ("ok", "latency_ms", "hostname", "uv", "docker", "docker_ok", "docker_userns",
                                                 "podman", "podman_ok", "engine", "harbor",
                                                 "harbor_version", "error") if k in result}}
    if result["ok"]:                     # the capacity on every machine; the engine's part where one answers
        found.update(hosts.measure(host, engine=result.get("engine")))
        result["constraints"] = dict(found, warnings=constraints.warnings(constraints.of(entry, strict=False), found))
    constraints.save(entry, found)
    m = _machine(entry, "This machine" if host is None else host.label, host, constraints.load().get(entry))
    result["info"] = render_template("settings/machine_info.html", m=m, writable=True)
    result["cells"] = {"engine": m["engine_name"], "capacity": m["capacity_text"]}
    result["not_enforced"] = m["not_enforced"]
    return jsonify(result)


def _host_form():
    """user, host, port from the Add host form, checked as an entry would be."""
    user, host, port = request.form.get("user", "").strip(), request.form.get("host", "").strip(), request.form.get("port", "").strip()
    parsed = hosts.parse(f"{user}@{host}" + (f":{port}" if port else ""))
    return parsed.user, parsed.host, parsed.port


@app.route("/settings/hosts/key", methods=["POST"])
def settings_host_key():
    """A key file for a manual host, kept under <config>/ssh with mode
    0600 until Save removes its host; the entry that names it comes back.
    Same guards as Save."""
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
    """The Sandbox section's state, asked when the section opens: docker's."""
    return jsonify(docker=sandbox.docker_state())


@app.route("/settings/data-providers")
def settings_data_providers():
    """Connection metadata only; no credential is returned. The Hugging Face
    endpoint is not one: it is shown, and marked when the environment holds it."""
    endpoint = settings.state(settings.BY_NAME["HF_ENDPOINT"], settings.in_file(shared.ENV_FILE))
    return jsonify(huggingface=bool(secrets.status(secrets.HUGGINGFACE).get("HF_TOKEN", {}).get("set")),
                   huggingface_endpoint=settings.endpoint_url_shown(os.environ.get("HF_ENDPOINT", "")) if endpoint["source"] != "unset" else "",
                   huggingface_endpoint_env=endpoint["source"] == "environment",
                   huggingface_endpoint_default=settings.HF_ENDPOINT_DEFAULT,
                   dockerhub=sandbox.login_user())


@app.route("/settings/data-providers/huggingface", methods=["POST"])
def settings_huggingface():
    """The token, kept in the owner-only secret store, and the endpoint, a
    setting of its own (``HF_ENDPOINT``): either may be saved alone. An empty
    token keeps the saved one; an empty endpoint, or the default, removes it."""
    _sandbox_guard("Data providers are changed")
    action = request.form.get("action", "save")
    held = settings.state(settings.BY_NAME["HF_ENDPOINT"], settings.in_file(shared.ENV_FILE))
    try:
        if action == "remove":
            secrets.delete(secrets.HUGGINGFACE, "HF_TOKEN")
            if held["source"] == "file":
                settings.save(shared.ENV_FILE, "HF_ENDPOINT", None)
        elif action == "save":
            token = request.form.get("token", "").strip()
            endpoint = request.form.get("endpoint", "").strip().rstrip("/")
            has_token = bool(secrets.status(secrets.HUGGINGFACE).get("HF_TOKEN", {}).get("set"))
            if not token and not has_token and not endpoint:
                return jsonify(ok=False, error="Enter an access token or an endpoint URL."), 400
            if endpoint and endpoint != settings.HF_ENDPOINT_DEFAULT:
                why = settings.BY_NAME["HF_ENDPOINT"].validate(endpoint)
                if why:
                    return jsonify(ok=False, error=f"The endpoint URL must be {why}."), 400
            if token:
                secrets.set(secrets.HUGGINGFACE, "HF_TOKEN", token)
            if held["source"] != "environment":
                value = endpoint if endpoint and endpoint != settings.HF_ENDPOINT_DEFAULT else None
                if value is not None or held["source"] == "file":
                    settings.save(shared.ENV_FILE, "HF_ENDPOINT", value)
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


def _host_changed(current):
    """A host edited (``host_from`` to ``host_to``) or deleted (``host_from``
    alone): HOSTS and HOST_CONSTRAINTS saved together, the constraints under
    the host's new entry or gone, and its last test with them: kept, marked
    stale when the entry now reaches another machine, or forgotten."""
    if current["HOSTS"]["source"] == "environment":
        return _saved_response("HOSTS is set in the environment: hosts are changed there.", False, "#hosts")
    old, new, before = request.form.get("host_from", ""), request.form.get("host_to"), os.environ.get("HOSTS", "")
    try:
        listed, host = hosts.replace(before, old, new.strip() if new is not None else None)
    except ValueError as e:
        return _saved_response(str(e), False, "#hosts")
    if host and host.entry == old:
        return _saved_response("Nothing changed.")
    try:
        settings.save_many(shared.ENV_FILE, {"HOSTS": listed or None,
                                             constraints.SETTING: constraints.rekey(os.environ.get(constraints.SETTING, ""), old, host and host.entry)})
    except ValueError as e:
        return _saved_response(str(e), False, "#hosts")
    except OSError as e:
        return _saved_response(f"Not written: {e.strerror or e}", False, retryable=True)
    constraints.move(old, host and host.entry, stale=bool(host) and not hosts.same_machine(hosts.parse(old), host))
    said, notes = "Host saved" if host else "Host deleted", _forget_keys(before, listed or "")
    return _saved_response(" ".join([said + ".", *notes]) if notes else said)


def _forget_keys(before: str, after: str) -> list:
    """Each key file uploaded for a host of ``before`` that ``after`` no longer names, deleted; what could not be, said."""
    return [f"The key file for {os.path.basename(p)} could not be removed: {why}." for p, why in hosts.forget_keys(before, after)]


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
    if "host_from" in request.form:
        return _host_changed(current)
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
            return _saved_response("A custom key needs its name and its value.", False, "#custom-keys")
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
    notes = ["Environment overrides still apply."] if later else []
    if "HOSTS" in changes and current["HOSTS"]["source"] == "file":   # a list from the environment is not this page's
        notes += _forget_keys(current["HOSTS"]["shown"], changes["HOSTS"] or "")
    return _saved_response(" ".join(["Saved."] + notes) if notes else "Saved", reload=cards)
