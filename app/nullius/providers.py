"""Where model calls go: the one definition of each model provider, and the builder's steps.

Every feature reads the registry below (``record``, ``records``, ``serves``) and asks for the
protocol it speaks: a run what its agent speaks, the LLM judge ``chat`` or ``messages``, Decision
a decision protocol. A provider is a preset (``PRESETS``) or a custom provider, ``custom``: the
OpenAI-compatible endpoint at MODEL_API_URL, and one per id in Settings (``settings.endpoint_ids``).
A recipe names one by ``provider`` (``PROVIDERS``, the ones a run is sent to) and, for a custom one,
its ``endpoint`` id; the builder and the judge pickers by a choice, ``custom:<id>`` (``choice``,
``split``). Also the builder's
step numbers, so an error from a compiled recipe names the step that edits it.
Settings' Test (``test``) asks a provider with free GETs only, and keeps the last
result per choice (``tests``, ``remember``) for its card and Review.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from nullius import ondisk, paths, settings

# --- the registry: protocols, presets and each provider's record ------------------------------------
#
# A protocol is a wire format, defined once here: its default path below a provider's base, how the key is
# sent (``auth``: a bearer ``authorization`` header, or ``x-api-key`` with ``anthropic-version``) and what
# it is called. The consumer picks the protocol it speaks; a provider never enforces one.
PROTOCOLS = {
    "chat": {"label": "OpenAI Chat Completions", "path": "/chat/completions", "auth": "authorization"},
    "responses": {"label": "OpenAI Responses", "path": "/responses", "auth": "authorization"},
    "messages": {"label": "Anthropic Messages", "path": "/messages", "auth": "x-api-key"},
    "systemone": {"label": "TypeSafe Decision", "path": "/systemone", "auth": "authorization"},
    "decisions": {"label": "OpenRouter Decisions", "path": "/decisions", "auth": "authorization"},
    "openai-decisions": {"label": "OpenAI Decisions", "path": "/decisions", "auth": "authorization"},
}
# A preset is a provider record whose name, base, key and known endpoints are fixed here. The user adds
# only endpoints of other protocols, in the setting ``setting`` (``settings.is_record``); its key is
# the secret setting ``key``. Its ``endpoints`` are also the protocols it serves.
PRESETS = {
    "openrouter": {"name": "OpenRouter", "base": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY",
                   "endpoints": {"chat": "/chat/completions", "responses": "/responses", "systemone": "/systemone",
                                 "decisions": "https://openrouter.ai/api/alpha/decisions"}},
    "openai": {"name": "OpenAI", "base": "https://api.openai.com/v1", "key": "OPENAI_API_KEY",
               "endpoints": {"chat": "/chat/completions", "responses": "/responses", "openai-decisions": "/decisions"}},
    "anthropic": {"name": "Anthropic", "base": "https://api.anthropic.com/v1", "key": "ANTHROPIC_API_KEY",
                  "endpoints": {"messages": "/messages"}},
    "typesafe": {"name": "TypeSafe", "base": "https://api.typesafe.ai/v1", "key": "TYPESAFE_API_KEY",
                 "endpoints": {"systemone": "/systemone"}},
}
FIELDS = ("name", "base", "key", "endpoints")


def setting_of(choice: str) -> str:
    """The setting that holds a provider's record: a preset's name in capitals, ``MODEL_<ID>`` for a custom
    provider, "" for the OpenAI-compatible endpoint at MODEL_API_URL, which has its old settings only."""
    p, e = split(choice)
    return p.upper() if p in PRESETS else f"MODEL_{e}" if p == "custom" and e else ""


def key_setting(choice: str) -> str:
    """The secret setting a provider's key is read from. Never the key."""
    p, e = split(choice)
    return PRESETS[p]["key"] if p in PRESETS else endpoint_vars(e)[1]


def _entry_problem(entry, host: str = "") -> Optional[str]:
    """Why ``entry`` cannot be an endpoint: a path starting with one slash, without a ``..`` segment, or a
    full URL that passes the checks a base gets; on ``host`` only, when one is given (a preset's own)."""
    if not isinstance(entry, str):
        return "a path or a URL"
    if entry.startswith(("http://", "https://")):
        if host and urllib.parse.urlsplit(entry).hostname != host:
            return f"a path, or a URL on {host}: the preset's key is sent there"
        return settings.endpoint_url_problem(entry)
    if not entry.startswith("/") or entry.startswith("//") or any(c.isspace() or ord(c) < 32 for c in entry):
        return "a path that starts with / or an http(s) URL"
    if ".." in entry.split("/"):
        return "a path without a .. segment"
    return settings.endpoint_url_problem("http://x" + entry)


def record_problem(name: str, value: str) -> Optional[str]:
    """Why ``value`` cannot be the record setting ``name``, or None. A JSON object of ``FIELDS``: a name, a
    base the checks of an endpoint's URL pass, the key's setting name (only its own: never a key), and
    endpoints by protocol, each a path, a full URL or null. A preset's fields and known endpoints are fixed;
    a full URL it adds is on its own host, the one its key is for."""
    rec = settings.json_object(value)
    if rec is None:
        return 'a JSON object, such as {"base": "https://…/v1"}'
    preset = next((p for p in PRESETS if p.upper() == name), "")
    if set(rec) - set(FIELDS):
        return "an object of " + ", ".join(FIELDS)
    fixed = [k for k in ("name", "base", "key") if preset and k in rec]
    if fixed:
        return f"a record without {', '.join(fixed)}: they are fixed for {PRESETS[preset]['name']}"
    if rec.get("name") is not None and (not isinstance(rec["name"], str) or not 0 < len(rec["name"]) <= 400
                                        or any(ord(c) < 32 for c in rec["name"])):
        return "a record whose name is one line of 400 characters at most"
    if rec.get("base") is not None and (not isinstance(rec["base"], str) or settings.endpoint_url_problem(rec["base"])):
        return f"a record whose base is {settings.endpoint_url_problem(str(rec['base']))}"
    if rec.get("key") is not None and rec["key"] != f"{name}_API_KEY":
        return f"a record whose key is the name of its key setting, {name}_API_KEY, or none"
    ends = rec.get("endpoints")
    if ends is None:
        return None
    if not isinstance(ends, dict):
        return "a record whose endpoints are an object by protocol"
    for proto, entry in ends.items():
        if proto not in PROTOCOLS:
            return f"a record whose endpoints are some of {', '.join(PROTOCOLS)}"
        if preset and proto in PRESETS[preset]["endpoints"]:
            return f"a record without the {proto} endpoint: it is fixed for {PRESETS[preset]['name']}"
        why = entry is not None and _entry_problem(entry, urllib.parse.urlsplit(PRESETS[preset]["base"]).hostname if preset else "")
        if why:
            return f"a record whose {proto} endpoint is {why}"
    return None


def join(base: str, entry: str) -> str:
    """An endpoint's URL: a full URL as it is, a path appended to ``base`` with exactly one slash between
    them (never resolved as a relative URL, which would drop the base's own path)."""
    if entry.startswith(("http://", "https://")):
        return entry
    return base.rstrip("/") + "/" + entry.lstrip("/") if base else ""


def record(choice: str, env=None) -> dict:
    """A provider's record, the three layers merged field by field (the later wins): the preset in code,
    the file's and then the environment's record setting (``settings.load`` merged those two). A custom
    provider reads its old settings where its record says nothing (a null says: none): MODEL_<ID>_NAME as ``name``,
    MODEL_<ID>_API_URL (MODEL_API_URL, unnamed) as ``base``; ``ignored`` names an old setting the record
    overrides. ``{id, name, base, key, endpoints, preset, ignored}``, where ``key`` is a setting name only."""
    env = os.environ if env is None else env
    p, e = split(choice)
    preset = PRESETS.get(p)
    own = settings.json_object(env.get(setting_of(choice))) if setting_of(choice) else None
    if own is not None and record_problem(setting_of(choice), json.dumps(own)):
        own = None                            # a record that does not validate is no record
    rec = settings.merge_patch({k: v for k, v in (preset or {}).items()}, own or {})
    if preset:
        rec.update({k: preset[k] for k in ("name", "base", "key")},
                   endpoints={**(rec.get("endpoints") or {}), **preset["endpoints"]})
    ignored = []
    if not preset:
        old_name, old_url = (f"MODEL_{e}_NAME" if e else ""), endpoint_vars(e)[0]
        for field, old in (("name", old_name), ("base", old_url)):
            if old and env.get(old):
                if field in (own or {}):          # the record sets the field, or removes it with null
                    ignored.append(old)
                else:
                    rec[field] = env[old]
        rec.setdefault("name", e or settings.DEFAULT_ENDPOINT_NAME)
        rec["key"] = key_setting(choice)
    return {"id": choice if preset or e else "custom", "name": rec.get("name") or "", "base": rec.get("base") or "",
            "key": rec["key"], "endpoints": {k: v for k, v in (rec.get("endpoints") or {}).items() if v},
            "preset": bool(preset), "ignored": ignored}


# What a base on a preset's host also serves, its endpoints, suggested on Settings' Endpoints rows: a hint,
# never saved unasked.
KNOWN_HOSTS = {urllib.parse.urlsplit(d["base"]).hostname: {p: urllib.parse.urlsplit(join(d["base"], e)).path for p, e in d["endpoints"].items()}
               for d in PRESETS.values()}


def suggestions(base: str) -> dict:
    """{protocol: full URL} a base on a known host suggests (``KNOWN_HOSTS``); {} for another host."""
    parts = urllib.parse.urlsplit(base or "")
    host = parts.netloc.rpartition("@")[2]           # never a user or password the base may hold
    return {p: f"{parts.scheme}://{host}{path}" for p, path in KNOWN_HOSTS.get(parts.hostname or "", {}).items()}


def records(env=None) -> List[dict]:
    """Every provider: the presets, then the OpenAI-compatible endpoint at MODEL_API_URL when it is set, then
    each custom provider by name."""
    env = os.environ if env is None else env
    custom = sorted((record(choice("custom", i), env) for i in settings.endpoint_ids(env)), key=lambda r: r["name"].casefold())
    return [record(p, env) for p in PRESETS] + ([record("custom", env)] if env.get("MODEL_API_URL") else []) + custom


def url(rec: dict, protocol: str) -> str:
    """Where ``rec`` is asked in ``protocol``: its endpoint for it, else its base and the protocol's default path."""
    return join(rec["base"], rec["endpoints"].get(protocol) or PROTOCOLS[protocol]["path"])


def serves(rec: dict, protocol: str) -> Optional[bool]:
    """True when ``rec`` lists the protocol, False when a preset does not, None (not known) for a custom
    provider that does not say: a hint for pickers to sort and warn by."""
    return True if protocol in rec["endpoints"] else False if rec["preset"] else None


def ready(rec: dict, env=None) -> bool:
    """Whether a provider can be asked here: a preset whose key is set, a custom provider whose base is a
    URL that holds no credential (its key is optional)."""
    env = os.environ if env is None else env
    return bool(env.get(rec["key"])) if rec["preset"] else bool(rec["base"]) and not settings.endpoint_url_problem(rec["base"])


def protocol_for(choice: str, apis, env=None) -> str:
    """The protocol a consumer speaking ``apis`` (in its order of preference) uses on a provider: the first
    the provider lists, else the first it may serve, else the first."""
    rec = record(choice, env)
    apis = list(apis) or ["chat"]
    return next((a for a in apis if serves(rec, a)), next((a for a in apis if serves(rec, a) is not False), apis[0]))


def auth(protocol: str, key: str) -> dict:
    """The headers that carry ``key`` in ``protocol``: a bearer header, or Anthropic's ``x-api-key``."""
    if PROTOCOLS[protocol]["auth"] == "x-api-key":
        from nullius.standard.calls.relay import ANTHROPIC_VERSION
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    return {"Authorization": "Bearer " + key} if key else {}


# The builder's steps (command-builder-core.js, STEP).
STEP = {"benchmark": 0, "runner": 1, "dataset": 2, "setup": 3, "agent": 4, "model": 5, "limits": 6, "where": 7, "review": 8}

# Harbor's card on the builder's Runner step.
HARBOR = {"key": "harbor", "label": "Harbor", "builtin": True,
          "summary": "Task directories in containers, with the oracle and nop bracket."}

# Said to a recipe whose harness is neither Harbor nor the standard.
GONE = ("The Native runner is no longer offered: the benchmark's own code runs as its Original setup "
        "on the Nullius runner.")

# The providers a run is sent to, in the order the builder prefers them.
PROVIDERS = ("openrouter", "openai", "anthropic", "custom")


def endpoint_vars(endpoint: str = "") -> tuple:
    """(URL setting, key setting) of an OpenAI-compatible endpoint: MODEL_API_URL
    and MODEL_API_KEY for none named, else that endpoint's own. An id that is not
    one names settings that are never set, so a run on it never falls back to another."""
    return ("MODEL_API_URL", "MODEL_API_KEY") if not endpoint else (f"MODEL_{endpoint}_API_URL", f"MODEL_{endpoint}_API_KEY")


def base_of(url_var: str, env=None) -> str:
    """The base a run's ``url_var`` names (``execution.Plan``): a custom provider's, from its record when it
    has one (MODEL_<ID>_API_URL is its old setting), else the variable's own value."""
    env = os.environ if env is None else env
    i = url_var[6:-8] if url_var.startswith("MODEL_") and url_var.endswith("_API_URL") else ""
    return record(choice("custom", i), env)["base"] if i and settings.ENDPOINT_ID.match(i) else env.get(url_var, "")


def choice(provider: str, endpoint: str = "") -> str:
    """A provider as the builder and the judge pickers offer it: ``custom:<id>`` for a named endpoint."""
    return f"custom:{endpoint}" if provider == "custom" and endpoint else provider


def split(value: str) -> tuple:
    """(provider, endpoint id) of a choice; a ``custom:`` with no valid id stays whole, so it is no provider."""
    p, _, e = str(value or "").partition(":")
    return (p, e) if p == "custom" and settings.ENDPOINT_ID.match(e) else (str(value or ""), "")


def _custom(env) -> List[dict]:
    """The custom providers whose base is set: the one at MODEL_API_URL first, then the named ones by name."""
    return [r for r in records(env) if not r["preset"] and r["base"]]


def endpoints(env=os.environ) -> List[dict]:
    """The custom providers a run can be sent to, each whose base is set and holds no credential
    (``broken``): the one at MODEL_API_URL first, then the named ones by name. Names only, never a value."""
    return [{"id": split(r["id"])[1], "name": r["name"]} for r in _custom(env) if ready(r, env)]


def broken(env=os.environ) -> List[str]:
    """The custom providers whose base is set but is refused (a credential in it, or no
    http(s) URL): never offered, and a recipe on one is told to fix it in Settings."""
    return [r["id"] for r in _custom(env) if not ready(r, env)]


def label(value: str, env=os.environ) -> str:
    """What a choice is called: the provider's name."""
    p, e = split(value)
    return record(value, env)["name"] if p in PRESETS or p == "custom" else p


def needs(value: str) -> str:
    """The setting that makes a choice one a run can start on: a preset's key, a custom provider's base."""
    p, e = split(value)
    return PRESETS[p]["key"] if p in PRESETS else endpoint_vars(e)[0]


def offered(apis, env=None) -> List[str]:
    """The run providers that may serve a consumer speaking ``apis``: every custom one, a preset only
    when it lists one of them."""
    return [p for p in PROVIDERS if any(serves(record(p, env), a) is not False for a in apis)]


def run_choices(env=None) -> List[dict]:
    """The run providers as the builder reads them (command-builder-core.js): id, name, the setting that
    makes one ready, and the protocols it lists (None: a custom provider, which may serve any)."""
    return [{"id": p, "label": label(p, os.environ if env is None else env), "needs": needs(p),
             "protocols": list(record(p, env)["endpoints"]) if p in PRESETS else None} for p in PROVIDERS]


# OpenRouter names a model with its maker in front (openai/gpt-4o); a
# benchmark's official judge or simulator is written as its maker spells it.
_MAKERS = (("openai/", ("gpt-", "o1", "o3", "o4", "chatgpt-", "text-embedding-")),
           ("anthropic/", ("claude-",)), ("google/", ("gemini-",)))


def served_as(model: str, provider: str) -> str:
    """An official model's name as ``provider`` serves it: on OpenRouter a
    bare name gets its maker's prefix (gpt-4o-2024-08-06 is
    openai/gpt-4o-2024-08-06 there); a name already prefixed, an unknown
    maker or another provider leave it as written."""
    if provider != "openrouter" or "/" in model:
        return model
    for prefix, starts in _MAKERS:
        if model.startswith(starts):
            return prefix + model
    return model


def available(env=os.environ) -> List[str]:
    """The choices whose setting is set (``needs``), in the order the builder
    prefers them: OpenRouter, OpenAI, Anthropic, then each custom provider."""
    return [p for p in PROVIDERS if p in PRESETS and env.get(PRESETS[p]["key"])] + [choice("custom", e["id"]) for e in endpoints(env)]


def embedding_problem(model: str, provider: str) -> str:
    """Bare OpenAI embedding names work on OpenAI or OpenRouter; qualified names require OpenRouter."""
    allowed = ("openrouter",) if "/" in model else ("openai", "openrouter")
    if provider not in allowed:
        names = " or ".join(PRESETS[p]["name"] for p in allowed)
        return f"{model} needs the {names} provider for this search."
    return ""


def _where(provider: str, endpoint: str, env) -> tuple:
    """(base URL, key, headers) a choice is asked at, the headers its own protocol's (Anthropic's
    ``x-api-key``); ValueError, before any request, when it cannot be asked."""
    rec = record(choice(provider, endpoint), env)
    base, key = rec["base"], env.get(rec["key"], "")
    if not base:
        raise ValueError("Set the provider endpoint in Settings, or enter a model ID.")
    if provider in ("openai", "anthropic") and not key:
        raise ValueError("Add the provider key in Settings, or enter a model ID.")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The provider endpoint must be an HTTP or HTTPS base URL.")
    native = "messages" if serves(rec, "messages") and not serves(rec, "chat") else "chat"
    return base.rstrip("/"), key, auth(native, key)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_MOST = 8 * 1024 * 1024


def _get(url: str, headers: dict, opener=None, timeout: float = 12) -> bytes:
    """One GET, never another method: no redirect followed, so a key stays at its endpoint; at most 8 MiB read."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "nullius", **headers})
    try:
        with (opener or urllib.request.build_opener(_NoRedirect()).open)(req, timeout=timeout) as response:
            content = response.read(_MOST + 1)
    except urllib.error.HTTPError as e:
        e.close()                                # its reply read by no one: closed, not left to the collector
        raise
    if len(content) > _MOST:
        raise ValueError("response too large")
    return content


def _models(content: bytes, keep=lambda row: True) -> list[dict]:
    """The model list of a ``{"data": [{"id": ...}]}`` answer, by name, of the rows ``keep`` accepts; ValueError when there is none."""
    data = json.loads(content)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("missing model list")
    models = {}
    for row in rows:
        if not isinstance(row, dict) or not keep(row):
            continue
        model = row.get("id")
        if not isinstance(model, str) or not model or len(model) > 256 or any(c.isspace() or ord(c) < 32 for c in model):
            continue
        label = row.get("name") or row.get("display_name") or model
        models[model] = {"id": model, "name": label[:256] if isinstance(label, str) else model}
    return sorted(models.values(), key=lambda m: (m["name"].casefold(), m["id"]))


def list_models(provider: str, env=None, *, opener=None) -> list[dict]:
    """Read model IDs from the selected provider, or ``custom:<id>``'s endpoint; credentials never leave its endpoint."""
    env = os.environ if env is None else env
    provider, endpoint = split(provider)
    if provider not in PROVIDERS:
        raise ValueError("Choose a model provider.")
    base, _, headers = _where(provider, endpoint, env)
    try:
        return _models(_get(base + "/models" + ("?limit=1000" if provider == "anthropic" else ""), headers, opener))
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        # Provider errors can contain keys or private endpoint details; do not echo them.
        raise ValueError("Model list unavailable. Enter a model ID manually.") from None


DECISION_TTL = 6 * 3600     # seconds OpenRouter's list of decision models is kept before it is asked again


def _decides(row: dict) -> bool:
    arch = row.get("architecture")
    out = arch.get("output_modalities") if isinstance(arch, dict) else None
    return isinstance(out, list) and "decisions" in out


def decision_models(env=None, *, fetch: bool = True, opener=None, clock=time.time) -> list[str]:
    """OpenRouter's decision models by ID, from its public list (no key sent, no redirect followed), kept
    ``DECISION_TTL`` in the cache. ``fetch`` false, or the list unreadable: the cached IDs, however old;
    [] when there are none. Only the filtered list has them: the unfiltered one leaves them out."""
    file = os.path.join(paths.cache_dir(env), "openrouter-decision-models.json")
    try:
        with open(file, encoding="utf-8") as f:
            cached = json.load(f)
        at, models = cached["at"], [m for m in cached["models"] if isinstance(m, str)]
    except (OSError, ValueError, TypeError, KeyError):
        at, models = 0, []
    if not fetch or (models and isinstance(at, (int, float)) and clock() - at < DECISION_TTL):
        return models
    try:
        fresh = [m["id"] for m in _models(_get(PRESETS["openrouter"]["base"] + "/models?output_modalities=decisions",
                                               {}, opener, timeout=5), keep=_decides)]
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return models
    if not fresh:
        return models
    try:
        os.makedirs(os.path.dirname(file), exist_ok=True)
        with open(file + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"at": clock(), "models": fresh}, f)
        os.replace(file + ".tmp", file)
    except OSError:
        pass                    # an unwritable cache asks again next time
    return fresh


# A model OpenRouter serves: its maker, a slash, its name (a variant after a colon: ``:free``).
_ROUTED = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*")
# A serving provider's slug, as ``--served-by`` takes it (``standard.calls.relay.SLUG``).
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9._-]+)*")


def _endpoints(content: bytes) -> list[dict]:
    """The serving providers of OpenRouter's endpoint list (``{"data": {"endpoints": [...]}}``), each
    ``{slug, name, quantization}``: the slug its ``tag`` (a provider and its variant, ``deepinfra/fp8``),
    else the provider's name lowercased with its spaces as dashes. ValueError when there is no list."""
    data = json.loads(content)
    rows = ((data.get("data") or {}).get("endpoints") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None)
    if not isinstance(rows, list):
        raise ValueError("missing endpoint list")
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("provider_name") if isinstance(row.get("provider_name"), str) else ""
        tag = row.get("tag") if isinstance(row.get("tag"), str) else ""
        slug = tag or re.sub(r"\s+", "-", name.strip().lower())
        if not slug or len(slug) > 128 or not _SLUG.fullmatch(slug) or slug in out:
            continue
        quant = row.get("quantization") if isinstance(row.get("quantization"), str) else ""
        out[slug] = {"slug": slug, "name": (name or slug)[:128], "quantization": quant[:32]}
    return sorted(out.values(), key=lambda e: (e["name"].casefold(), e["slug"]))


def list_endpoints(provider: str, model: str, env=None, *, opener=None) -> list[dict]:
    """The serving providers OpenRouter lists for ``model`` (``GET /models/{author}/{slug}/endpoints``), for the
    builder's Served by: asked as the model list is (its key when set, no redirect, nothing of the error echoed)."""
    env = os.environ if env is None else env
    provider, endpoint = split(provider)
    if provider != "openrouter":
        raise ValueError("Only OpenRouter routes a model to several providers.")
    model = served_as(str(model or "").strip(), provider)
    if not _ROUTED.fullmatch(model):
        raise ValueError("Choose a model as OpenRouter names it, its maker first: maker/model.")
    base, _, headers = _where(provider, endpoint, env)
    author, _, name = model.partition("/")
    url = f"{base}/models/{urllib.parse.quote(author, safe='')}/{urllib.parse.quote(name, safe=':')}/endpoints"
    try:
        return _endpoints(_get(url, headers, opener))
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        raise ValueError("Provider list unavailable. Enter a provider's slug by hand.") from None


# Settings' Test, for each run provider (TypeSafe has no free request): only requests that cost nothing. A
# model list everywhere; OpenRouter's is public, so its key is asked at its own key-information endpoint.
_TESTS_LOCK = threading.Lock()


def _unreached(reason) -> str:
    """Why a request reached nothing, in a few fixed words: never the error's own text, which may hold the URL."""
    for kind, said in ((socket.gaierror, "name not resolved"), (ConnectionRefusedError, "connection refused"),
                       (TimeoutError, "timed out"), (ssl.SSLError, "TLS failed")):
        if isinstance(reason, kind):
            return said
    return "not reached"


def test(choice: str, env=None, *, opener=None, clock=time.monotonic) -> dict:
    """A provider asked with GETs that cost nothing, never a completion: its model list (and, for OpenRouter,
    its key's information). An OpenAI-compatible endpoint that answers with a key is asked once more with a
    wrong one: an answer then means it does not check keys. ``{at, ok, reached, key, models, ms, error}``,
    ``key`` one of accepted, refused, not checked (the server checks none), not tested; never a key.
    ValueError, before any request, when the provider has no test or its settings are incomplete."""
    env = os.environ if env is None else env
    provider, endpoint = split(choice)
    if provider not in PROVIDERS:
        raise ValueError("This provider has no test.")
    rec = record(choice, env)
    if rec["preset"] and not env.get(rec["key"]):
        raise ValueError("Add the provider key in Settings.")
    base, key, headers = _where(provider, endpoint, env)
    models = base + "/models" + ("?limit=1000" if provider == "anthropic" else "")
    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reached": False, "key": "not tested",
           "models": None, "ms": 0, "error": ""}
    start = clock()
    try:
        # OpenRouter's model list is public: its key goes to its key endpoint only
        out["models"] = len(_models(_get(models, {} if provider == "openrouter" else headers, opener)))
        out["reached"] = True
        if provider == "openrouter":
            _get(base + "/key", headers, opener)
            out["key"] = "accepted"
        elif provider != "custom":
            out["key"] = "accepted"
        elif not key:
            out["key"] = "not checked"
        else:
            try:
                _get(models, auth("chat", "nullius-wrong-" + secrets.token_hex(8)), opener)
                out["key"] = "not checked"
            except urllib.error.HTTPError as e:
                e.close()
                out["key"] = "accepted" if e.code in (401, 403) else "not tested"
            except (OSError, http.client.HTTPException):
                pass
    except urllib.error.HTTPError as e:
        e.close()
        out["reached"] = True
        if e.code in (401, 403):
            out["key"] = "refused"
        else:
            out["error"] = f"HTTP {e.code}"
    except urllib.error.URLError as e:
        out["error"] = _unreached(e.reason)
    except (OSError, http.client.HTTPException) as e:
        out["error"] = _unreached(e)
    except ValueError:
        out.update(reached=True, error="no model list")
    out["ms"] = round((clock() - start) * 1000)
    out["ok"] = out["reached"] and not out["error"] and out["key"] != "refused"
    return out


def _tests_file(env=None) -> str:
    return os.path.join(paths.data_dir(env), "provider-tests.json")


def tests(env=None) -> dict:
    """Each provider's last test by choice; none when the file is missing or unreadable."""
    try:
        with open(_tests_file(env), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def remember(results: dict, env=None) -> None:
    """Set (a result) or forget (None) the last test of each choice, the file written whole and renamed."""
    with _TESTS_LOCK:
        kept = {k: v for k, v in {**tests(env), **results}.items() if v is not None}
        out = _tests_file(env)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        ondisk.write_whole(out, json.dumps(kept, indent=1, sort_keys=True))


def mark(result: dict) -> str:
    """ok, warn (it answers but its key is not known good) or bad: the colour of a test's line."""
    return "bad" if not result.get("ok") else "ok" if result.get("key") == "accepted" else "warn"


_KEY_SAID = {"refused": "key refused", "not checked": "does not check keys", "not tested": "key not tested"}


def said(result: dict) -> str:
    """A test in one line: what failed, or the key, the model count and the time it took; then when."""
    when = str(result.get("at", "")).replace("T", " ")[:16] + " UTC"
    if result.get("error"):
        head = ("" if result.get("reached") else "Not reached: ") + result["error"]
    elif result.get("key") == "refused":
        head = "Key refused"
    else:
        n = result.get("models")
        head = " · ".join([("Key accepted" if result.get("key") == "accepted" else "Reached · " + _KEY_SAID.get(result.get("key"), "")),
                           f"{n} model{'s' * (n != 1)}", f"{result.get('ms', 0)} ms"])
    return f"{head} · {when}"
