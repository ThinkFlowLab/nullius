"""Where a run's model calls go, and the builder's steps.

The builder's three providers (command-builder-core.js, ``PROVIDERS``), named
the same here: what each is called, where it is served, the setting its key is
read from, and what makes it one a run can start on here. The third,
``custom``, is any number of OpenAI-compatible endpoints: the one at
MODEL_API_URL, and one per id in Settings (``settings.endpoint_ids``). A
recipe names one by ``provider: custom`` and its ``endpoint`` id; the builder
and the judge pickers by a choice, ``custom:<id>`` (``choice``, ``split``). Also the builder's
step numbers, so an error from a compiled recipe names the step that edits it.
"""
from __future__ import annotations

import os
from typing import List

from nullius import settings

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


def list_models(provider: str, env=None, *, opener=None) -> list[dict]:
    """Read model IDs from the selected provider, or ``custom:<id>``'s endpoint; credentials never leave its endpoint."""
    import json
    import urllib.parse
    import urllib.request

    env = os.environ if env is None else env
    provider, endpoint = split(provider)
    if provider not in PROVIDERS:
        raise ValueError("Choose a model provider.")
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
    headers = {"Accept": "application/json", "User-Agent": "nullius"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base.rstrip("/") + "/models", headers=headers)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    fetch = opener or urllib.request.build_opener(NoRedirect()).open
    try:
        with fetch(req, timeout=12) as response:
            content = response.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("model response too large")
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
    except (OSError, ValueError, TypeError):
        # Provider errors can contain keys or private endpoint details; do not echo them.
        raise ValueError("Model list unavailable. Enter a model ID manually.") from None
