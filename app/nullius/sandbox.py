"""The Settings page's Sandbox: the container backend's engine on this machine,
docker or podman (``container.DOCKER``), and its Docker Hub login under Data
providers.

Two things, each something a user acts on:

- **Engine**: whether it answers, and its name and version (``container.unusable``).
- **Registry login**: Docker Hub limits anonymous pulls, which stops a
  benchmark with an image per unit part-way. The username and an access token
  go to ``docker login --password-stdin`` (``podman login ... docker.io``)
  once; the engine keeps them in its own store (``~/.docker/config.json`` or
  the credential helper it names; podman's ``auth.json``), the same as a
  login typed in a terminal. Nullius stores neither, and reads back only the
  username.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from typing import List, Optional

from nullius.standard import container

REGISTRY = "https://index.docker.io/v1/"            # Docker Hub, as docker names it in its config
HUB = "docker.io"                                   # as podman names it: without it, podman logs in to its first search registry
_USER = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_TOKEN_MAX = 4096


def docker_state() -> dict:
    """{ok, version} when the engine answers - its name and version, ``podman
    6.1.4`` - else {ok: False, error}."""
    why = container.unusable()
    if why:
        return {"ok": False, "error": why}
    r = container._docker(["version", "--format", "{{.Server.Version}}"], timeout=30, check=False)
    return {"ok": True, "version": f"{container.DOCKER} {r.stdout.strip()}"}


def _config_dir() -> str:
    return os.environ.get("DOCKER_CONFIG") or os.path.join(os.path.expanduser("~"), ".docker")


def login_user(registry: str = REGISTRY) -> Optional[str]:
    """The username the engine holds a Docker Hub login for, or None. Read from
    docker's config, or asked of the credential helper it names; the secret
    that comes with it is dropped here, never kept or returned. Podman says the
    username itself."""
    if container.podman():
        try:
            r = container._docker(["login", "--get-login", HUB], timeout=30, check=False)
        except container.Unavailable:
            return None
        return (r.stdout.strip() or None) if r.returncode == 0 else None
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
    """``docker login`` (``podman login ... docker.io``) to Docker Hub, the token over stdin: {ok, user} or
    {ok: False, error}. Neither value is stored or logged here."""
    username = (username or "").strip().lower()
    if not _USER.match(username):
        return {"ok": False, "error": "Enter your Docker Hub username."}
    if not token or len(token) > _TOKEN_MAX or "\n" in token.strip():
        return {"ok": False, "error": "Enter an access token."}
    try:
        r = container._docker(["login", "--username", username, "--password-stdin", *_hub()], input=token.strip() + "\n",
                              timeout=60, check=False)
    except container.Unavailable as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        said = [ln for ln in (r.stderr or r.stdout).strip().splitlines() if ln.strip()]
        return {"ok": False, "error": (said[-1] if said else f"{container.DOCKER} login: exit {r.returncode}")[:300]}
    return {"ok": True, "user": login_user() or username}


def _hub() -> List[str]:
    return [HUB] if container.podman() else []


def logout() -> dict:
    try:
        r = container._docker(["logout", *_hub()], timeout=30, check=False)
    except container.Unavailable as e:
        return {"ok": False, "error": str(e)}
    return {"ok": r.returncode == 0} if r.returncode == 0 else {"ok": False, "error": (r.stderr or r.stdout).strip()[-300:]}

