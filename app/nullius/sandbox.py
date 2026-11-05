"""The Settings page's Sandbox: docker on this machine.

Three things, each something a user acts on:

- **Docker**: whether it answers, and its version (``container.unusable``).
- **Registry login**: Docker Hub limits anonymous pulls, which stops a
  benchmark with an image per unit part-way. The username and an access token
  go to ``docker login --password-stdin`` once; docker keeps them in its own
  store (``~/.docker/config.json`` or the credential helper it names), the
  same as a login typed in a terminal. Nullius stores neither, and reads back
  only the username.
- **Images**: what Nullius pulled or built for each benchmark's units
  (``container.fetched_images``), kept for the next run, and removed only
  from here. An image that was on the machine before Nullius fetched it is
  not listed, so it is never offered for removal.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from typing import Dict, List, Optional

from nullius.standard import container, definitions

REGISTRY = "https://index.docker.io/v1/"            # Docker Hub, as docker names it in its config
_USER = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_TOKEN_MAX = 4096


def docker_state() -> dict:
    """{ok, version} when docker answers, else {ok: False, error}."""
    why = container.unusable()
    if why:
        return {"ok": False, "error": why}
    r = container._docker(["version", "--format", "{{.Server.Version}}"], timeout=30, check=False)
    return {"ok": True, "version": r.stdout.strip()}


def _config_dir() -> str:
    return os.environ.get("DOCKER_CONFIG") or os.path.join(os.path.expanduser("~"), ".docker")


def login_user(registry: str = REGISTRY) -> Optional[str]:
    """The username docker holds a login for, or None. Read from docker's
    config, or asked of the credential helper it names; the secret that comes
    with it is dropped here, never kept or returned."""
    try:
        with open(os.path.join(_config_dir(), "config.json")) as f:
            config = json.load(f)
    except (OSError, ValueError):
        return None
    auth = (config.get("auths") or {}).get(registry) or {}
    if auth.get("auth"):
        try:
            return base64.b64decode(auth["auth"]).decode().split(":", 1)[0] or None
        except (ValueError, UnicodeDecodeError):
            return None
    helper = (config.get("credHelpers") or {}).get(registry) or config.get("credsStore")
    if not helper or not re.match(r"^[A-Za-z0-9_.-]+$", helper):
        return None
    try:
        r = subprocess.run(["docker-credential-" + helper, "get"], input=registry, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        return (json.loads(r.stdout).get("Username") or None) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def login(username: str, token: str) -> dict:
    """``docker login`` to Docker Hub, the token over stdin: {ok, user} or
    {ok: False, error}. Neither value is stored or logged here."""
    username = (username or "").strip().lower()
    if not _USER.match(username):
        return {"ok": False, "error": "Enter your Docker Hub username."}
    if not token or len(token) > _TOKEN_MAX or "\n" in token.strip():
        return {"ok": False, "error": "Enter an access token."}
    try:
        r = container._docker(["login", "--username", username, "--password-stdin"], input=token.strip() + "\n",
                              timeout=60, check=False)
    except container.Unavailable as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        said = [ln for ln in (r.stderr or r.stdout).strip().splitlines() if ln.strip()]
        return {"ok": False, "error": (said[-1] if said else f"docker login: exit {r.returncode}")[:300]}
    return {"ok": True, "user": login_user() or username}


def logout() -> dict:
    try:
        r = container._docker(["logout"], timeout=30, check=False)
    except container.Unavailable as e:
        return {"ok": False, "error": str(e)}
    return {"ok": r.returncode == 0} if r.returncode == 0 else {"ok": False, "error": (r.stderr or r.stdout).strip()[-300:]}


def _label(key: str) -> str:
    try:
        return definitions.get("benchmark", key).get("label") or key
    except Exception:                       # a benchmark since removed from the catalogue keeps its key
        return key


def images() -> List[dict]:
    """Per benchmark, the images Nullius fetched that are still here: [{key,
    label, images, bytes}], the largest first. An image removed outside
    Nullius is dropped from its record. Sizes are each image's own, so layers
    two images share are counted twice."""
    held = container.fetched_images()
    if not held:
        return []
    r = container._docker(["image", "inspect", "--format", "{{.Size}}"] + sorted(held), timeout=120, check=False)
    sizes = [int(x) for x in r.stdout.split() if x.isdigit()]
    if r.returncode != 0 or len(sizes) != len(held):        # one or more gone: ask one by one
        sizes, gone = [], []
        for ref in sorted(held):
            one = container._docker(["image", "inspect", "--format", "{{.Size}}", ref], timeout=30, check=False)
            if one.returncode == 0 and one.stdout.strip().isdigit():
                sizes.append(int(one.stdout.strip()))
            else:
                sizes.append(None)
                gone.append(ref)
        container.forget(gone)
    by: Dict[str, dict] = {}
    for ref, size in zip(sorted(held), sizes):
        if size is None:
            continue
        g = by.setdefault(held[ref], {"key": held[ref], "label": _label(held[ref]), "images": 0, "bytes": 0})
        g["images"] += 1
        g["bytes"] += size
    return sorted(by.values(), key=lambda g: -g["bytes"])


def remove(bench: str) -> dict:
    """Remove every image Nullius fetched for ``bench``: {ok, removed, failed}.
    An image a container still uses stays, and is named."""
    refs = sorted(ref for ref, key in container.fetched_images().items() if key == bench)
    removed, failed = [], []
    for ref in refs:
        r = container._docker(["rmi", ref], timeout=600, check=False)
        gone = r.returncode == 0 or container._docker(["image", "inspect", ref], timeout=30, check=False).returncode != 0
        (removed if gone else failed).append(ref)
    container.forget(removed)
    return {"ok": not failed, "removed": len(removed), "failed": failed}
