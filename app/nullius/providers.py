"""Where a run's model calls go, and the builder's steps.

The builder's three providers (command-builder-core.js, ``PROVIDERS``), named
the same here: what each is called, where it is served, the setting its key is
read from, and what makes it one a run can start on here. The third,
``custom``, is any number of OpenAI-compatible endpoints: the one at
MODEL_API_URL, and one per id in Settings (``settings.endpoint_ids``). A
recipe names one by ``provider: custom`` and its ``endpoint`` id; the builder
and the judge pickers by a choice, ``custom:<id>`` (``choice``, ``split``). Also the builder's
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
from typing import List

from nullius import paths, settings

# The builder's steps (command-builder-core.js, STEP): an error names the step that edits it.
STEP = {"benchmark": 0, "runner": 1, "dataset": 2, "setup": 3, "agent": 4, "model": 5, "limits": 6, "where": 7, "review": 8}

# Harbor's card on the builder's Runner step: compiled by the builder's own core
# (static/builder/command-builder-core.js), not by the application.
HARBOR = {"key": "harbor", "label": "Harbor", "builtin": True,
          "summary": "Task directories in containers, with the oracle and nop bracket."}

# A recipe whose harness is neither Harbor nor the standard: a runner of a benchmark's own scripts,
# no longer offered - those scripts run as the benchmark's Original setup on the Nullius runner.
GONE = ("The Native runner is no longer offered: the benchmark's own code runs as its Original setup "
        "on the Nullius runner.")

PROVIDERS = ("openrouter", "openai", "custom")
PROVIDER_LABEL = {"openrouter": "OpenRouter", "openai": "OpenAI", "custom": settings.DEFAULT_ENDPOINT_NAME}
# Where a provider is served: OpenRouter and OpenAI at their own endpoint,
# fixed; an OpenAI-compatible endpoint has no default and must be named
# (Settings: MODEL_API_URL, or an endpoint's own).
PROVIDER_BASE = {"openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1", "custom": ""}
# The setting a run's key is read from, per provider (web/execute.py,
# credentials_for): a key of its own for OpenRouter and for OpenAI, the
# endpoint's key for an OpenAI-compatible one.
PROVIDER_KEY = {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY", "custom": "MODEL_API_KEY"}
# What makes a provider one a run can start on here: its key, for OpenRouter
# and OpenAI; its endpoint, for an OpenAI-compatible one, whose key is optional.
PROVIDER_NEEDS = dict(PROVIDER_KEY, custom="MODEL_API_URL")


def endpoint_vars(endpoint: str = "") -> tuple:
    """(URL setting, key setting) of an OpenAI-compatible endpoint: MODEL_API_URL
    and MODEL_API_KEY for none named, else that endpoint's own. An id that is not
    one names settings that are never set, so a run on it never falls back to another."""
    return ("MODEL_API_URL", "MODEL_API_KEY") if not endpoint else (f"MODEL_{endpoint}_API_URL", f"MODEL_{endpoint}_API_KEY")


def choice(provider: str, endpoint: str = "") -> str:
    """A provider as the builder and the judge pickers offer it: ``custom:<id>`` for a named endpoint."""
    return f"custom:{endpoint}" if provider == "custom" and endpoint else provider


def split(value: str) -> tuple:
    """(provider, endpoint id) of a choice; a ``custom:`` with no valid id stays whole, so it is no provider."""
    p, _, e = str(value or "").partition(":")
    return (p, e) if p == "custom" and settings.ENDPOINT_ID.match(e) else (str(value or ""), "")


def _with_url(env) -> List[tuple]:
    """(id, URL) of each OpenAI-compatible endpoint whose URL is set; "" is MODEL_API_URL's."""
    return [(e, env[endpoint_vars(e)[0]]) for e in ("", *settings.endpoint_ids(env)) if env.get(endpoint_vars(e)[0])]


def endpoints(env=os.environ) -> List[dict]:
    """The OpenAI-compatible endpoints a run can be sent to, each whose URL is set and
    holds no credential (``broken``): the one at MODEL_API_URL first, then the named
    ones by name. Names only, never a value."""
    out = [{"id": e, "name": (env.get(f"MODEL_{e}_NAME") if e else "") or e or settings.DEFAULT_ENDPOINT_NAME}
           for e, url in _with_url(env) if not settings.endpoint_url_problem(url)]
    return sorted(out, key=lambda x: (x["id"] != "", x["name"].casefold()))


def broken(env=os.environ) -> List[str]:
    """The endpoint choices whose URL is set but is refused (a credential in it, or no
    http(s) URL): never offered, and a recipe on one is told to fix it in Settings."""
    return [choice("custom", e) for e, url in _with_url(env) if settings.endpoint_url_problem(url)]


def label(value: str, env=os.environ) -> str:
    """What a choice is called: the provider's name, or the endpoint's."""
    p, e = split(value)
    if not e:
        return PROVIDER_LABEL.get(p, p)
    return next((x["name"] for x in endpoints(env) if x["id"] == e), env.get(f"MODEL_{e}_NAME") or e)


def needs(value: str) -> str:
    """The setting that makes a choice one a run can start on."""
    p, e = split(value)
    return endpoint_vars(e)[0] if e else PROVIDER_NEEDS.get(p, PROVIDER_NEEDS["custom"])


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
    prefers them: OpenRouter, OpenAI, then each OpenAI-compatible endpoint."""
    return [p for p in PROVIDERS[:2] if env.get(PROVIDER_NEEDS[p])] + [choice("custom", e["id"]) for e in endpoints(env)]


def embedding_problem(model: str, provider: str) -> str:
    """Bare OpenAI embedding names work on OpenAI or OpenRouter; qualified names require OpenRouter."""
    allowed = ("openrouter",) if "/" in model else ("openai", "openrouter")
    if provider not in allowed:
        names = " or ".join(PROVIDER_LABEL[p] for p in allowed)
        return f"{model} needs the {names} provider for this search."
    return ""


def _where(provider: str, endpoint: str, env) -> tuple:
    """(base URL, key) a choice is asked at; ValueError, before any request, when it cannot be asked."""
    url_var, key_var = endpoint_vars(endpoint)
    base = PROVIDER_BASE[provider] if provider != "custom" else env.get(url_var, "")
    key = env.get(key_var if provider == "custom" else PROVIDER_KEY[provider], "")
    if not base:
        raise ValueError("Set the provider endpoint in Settings, or enter a model ID.")
    if provider == "openai" and not key:
        raise ValueError("Add the provider key in Settings, or enter a model ID.")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The provider endpoint must be an HTTP or HTTPS base URL.")
    return base.rstrip("/"), key


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_MOST = 8 * 1024 * 1024


def _get(url: str, headers: dict, opener=None) -> bytes:
    """One GET, never another method: no redirect followed, so a key stays at its endpoint; at most 8 MiB read."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "nullius", **headers})
    with (opener or urllib.request.build_opener(_NoRedirect()).open)(req, timeout=12) as response:
        content = response.read(_MOST + 1)
    if len(content) > _MOST:
        raise ValueError("response too large")
    return content


def _models(content: bytes) -> list[dict]:
    """The model list of a ``{"data": [{"id": ...}]}`` answer, by name; ValueError when there is none."""
    data = json.loads(content)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("missing model list")
    models = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("id")
        if not isinstance(model, str) or not model or len(model) > 256 or any(c.isspace() or ord(c) < 32 for c in model):
            continue
        label = row.get("name") or row.get("display_name") or model
        models[model] = {"id": model, "name": label[:256] if isinstance(label, str) else model}
    return sorted(models.values(), key=lambda m: (m["name"].casefold(), m["id"]))


def _bearer(key: str) -> dict:
    return {"Authorization": "Bearer " + key} if key else {}


def list_models(provider: str, env=None, *, opener=None) -> list[dict]:
    """Read model IDs from the selected provider, or ``custom:<id>``'s endpoint; credentials never leave its endpoint."""
    env = os.environ if env is None else env
    provider, endpoint = split(provider)
    if provider not in PROVIDERS:
        raise ValueError("Choose a model provider.")
    base, key = _where(provider, endpoint, env)
    try:
        return _models(_get(base + "/models", _bearer(key), opener))
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        # Provider errors can contain keys or private endpoint details; do not echo them.
        raise ValueError("Model list unavailable. Enter a model ID manually.") from None


# A model OpenRouter serves: its maker, a slash, its name (a variant after a colon: ``:free``).
_ROUTED = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*")
# A serving provider's slug, as ``--served-by`` takes it (``standard.relay.SLUG``).
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
    base, key = _where(provider, endpoint, env)
    author, _, name = model.partition("/")
    url = f"{base}/models/{urllib.parse.quote(author, safe='')}/{urllib.parse.quote(name, safe=':')}/endpoints"
    try:
        return _endpoints(_get(url, _bearer(key), opener))
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        raise ValueError("Provider list unavailable. Enter a provider's slug by hand.") from None


# Settings' Test, per provider: only requests that cost nothing. A model list everywhere; OpenRouter's is
# public, so its key is asked at its own key-information endpoint. TypeSafe has no free request: no Test.
TESTED = ("openrouter", "openai", "anthropic", "custom")
ANTHROPIC = ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY")
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
    if provider not in TESTED:
        raise ValueError("This provider has no test.")
    if provider == "anthropic":
        from nullius.standard.relay import ANTHROPIC_VERSION
        base, key = ANTHROPIC[0], env.get(ANTHROPIC[1], "")
        auth, models = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}, base + "/models?limit=1000"
    else:
        base, key = _where(provider, endpoint, env)
        auth, models = _bearer(key), base + "/models"
    if not key and provider != "custom":
        raise ValueError("Add the provider key in Settings.")
    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reached": False, "key": "not tested",
           "models": None, "ms": 0, "error": ""}
    start = clock()
    try:
        # OpenRouter's model list is public: its key goes to its key endpoint only
        out["models"] = len(_models(_get(models, {} if provider == "openrouter" else auth, opener)))
        out["reached"] = True
        if provider == "openrouter":
            _get(base + "/key", auth, opener)
            out["key"] = "accepted"
        elif provider != "custom":
            out["key"] = "accepted"
        elif not key:
            out["key"] = "not checked"
        else:
            try:
                _get(models, _bearer("nullius-wrong-" + secrets.token_hex(8)), opener)
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
        part = f"{out}.{os.getpid()}.part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump(kept, f, indent=1, sort_keys=True)
        os.replace(part, out)


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
