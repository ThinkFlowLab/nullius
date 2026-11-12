"""Where a run's model calls go, and the builder's steps.

The builder's three providers (command-builder-core.js, ``PROVIDERS``), named
the same here: what each is called, where it is served, the setting its key is
read from, and what makes it one a run can start on here. Also the builder's
step numbers, so an error from a compiled recipe names the step that edits it.
"""
from __future__ import annotations

import os
from typing import List

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
PROVIDER_LABEL = {"openrouter": "OpenRouter", "openai": "OpenAI", "custom": "Local or self-hosted OpenAI-compatible"}
# Where a provider is served: OpenRouter and OpenAI at their own endpoint,
# fixed; a local or self-hosted endpoint has no default and must be named
# (Settings: MODEL_API_URL).
PROVIDER_BASE = {"openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1", "custom": ""}
# The setting a run's key is read from, per provider (web/execute.py,
# credentials_for): a key of its own for OpenRouter and for OpenAI, the
# model key for a local or self-hosted endpoint.
PROVIDER_KEY = {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY", "custom": "MODEL_API_KEY"}
# What makes a provider one a run can start on here: its key, for OpenRouter
# and OpenAI; its endpoint, for a local or self-hosted one, whose key is optional.
PROVIDER_NEEDS = dict(PROVIDER_KEY, custom="MODEL_API_URL")


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
    """The providers whose setting is set (``PROVIDER_NEEDS``), in the order
    the builder prefers them: OpenRouter, OpenAI, local or self-hosted."""
    return [p for p in PROVIDERS if env.get(PROVIDER_NEEDS[p])]


def embedding_problem(model: str, provider: str) -> str:
    """Bare OpenAI embedding names work on OpenAI or OpenRouter; qualified names require OpenRouter."""
    allowed = ("openrouter",) if "/" in model else ("openai", "openrouter")
    if provider not in allowed:
        names = " or ".join(PROVIDER_LABEL[p] for p in allowed)
        return f"{model} needs the {names} provider for this search."
    return ""


def list_models(provider: str, env=None, *, opener=None) -> list[dict]:
    """Read model IDs from the selected provider; credentials never leave its endpoint."""
    import json
    import urllib.parse
    import urllib.request

    env = os.environ if env is None else env
    if provider not in PROVIDERS:
        raise ValueError("Choose a model provider.")
    base = PROVIDER_BASE[provider] if provider != "custom" else env.get("MODEL_API_URL", "")
    key = env.get(PROVIDER_KEY[provider], "")
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
