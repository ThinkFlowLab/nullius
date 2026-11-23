"""Compare a benchmark's image pins with registry tags, without pulling or
changing anything. Read images.json, or string literals in driver.py without
executing it. Registry V2 HEAD requests read manifest digests; anonymous
Bearer challenges are supported, private credentials are never loaded.
"""
from __future__ import annotations

import ast
import json
import re
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY = re.compile(r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*")
ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json"))


def references(folder: str) -> list:
    """(name, reference) in file order. images.json takes precedence."""
    root = Path(folder)
    if (root / "images.json").exists():
        data = json.loads((root / "images.json").read_text())
        if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
            raise ValueError("images.json must map names to pinned image references")
        return list(data.items())
    tree = ast.parse((root / "driver.py").read_text())
    strings = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and "@sha256:" in n.value), key=lambda n: (n.lineno, n.col_offset))
    return [(s, s) for s in dict.fromkeys(n.value for n in strings)]


def target(reference: str, tag: str | None = None) -> tuple:
    """Registry, repository, tag and pinned digest. Docker Hub is implicit;
    --tag overrides an embedded tag, otherwise an untagged pin uses latest."""
    name, _, digest = reference.rpartition("@")
    if not DIGEST.fullmatch(digest):
        raise ValueError("expected an image pinned by sha256 digest")
    embedded = None
    if ":" in name.rsplit("/", 1)[-1]:
        name, embedded = name.rsplit(":", 1)
    tag = tag if tag is not None else embedded or "latest"
    if not TAG.fullmatch(tag):
        raise ValueError("invalid image tag")
    first, slash, rest = name.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry, repository = "docker.io", name
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", registry) or not REPOSITORY.fullmatch(repository):
        raise ValueError("invalid registry or repository name")
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        registry = "registry-1.docker.io"
        if "/" not in repository:
            repository = "library/" + repository
    return registry, repository, tag, digest


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A registry's bearer token must not follow a redirect to another service.
        raise ValueError("registry redirect not followed")


def _token(challenge: str, repository: str, open_) -> str:
    kind, _, fields = challenge.partition(" ")
    params = dict(re.findall(r'(\w+)="([^"\r\n]*)"', fields))
    realm = params.get("realm", "")
    url = urlsplit(realm)
    if kind.lower() != "bearer" or url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("registry requires unsupported authentication")
    query = urlencode({"service": params.get("service", ""), "scope": f"repository:{repository}:pull"})
    try:
        with open_(Request(realm + ("&" if url.query else "?") + query), timeout=15) as response:
            data = json.loads(response.read(65537))
    except HTTPError as e:
        e.close()
        raise ValueError(f"token service returned HTTP {e.code}; anonymous access unavailable") from None
    except (ValueError, UnicodeError):
        raise ValueError("invalid token service response") from None
    token = (data.get("token") or data.get("access_token")) if isinstance(data, dict) else None
    if not isinstance(token, str) or not token or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError("token service returned no usable bearer token")
    return token


def current(registry: str, repository: str, tag: str, open_=None) -> str:
    """Ask only for manifest headers. A missing tag raises HTTPError(404);
    auth-service failures must not be mistaken for a missing manifest."""
    open_ = open_ or build_opener(_NoRedirect()).open
    url = f"https://{registry}/v2/{repository}/manifests/{tag}"
    headers = {"Accept": ACCEPT}
    try:
        response = open_(Request(url, headers=headers, method="HEAD"), timeout=15)
    except HTTPError as e:
        if e.code != 401:
            raise
        challenge = e.headers.get("WWW-Authenticate", "")
        e.close()
        headers["Authorization"] = "Bearer " + _token(challenge, repository, open_)
        response = open_(Request(url, headers=headers, method="HEAD"), timeout=15)
    with response:
        digest = response.headers.get("Docker-Content-Digest", "")
        if not DIGEST.fullmatch(digest):
            raise ValueError("registry returned no valid sha256 manifest digest")
        return digest


def check(reference: str, tag: str | None = None) -> tuple:
    """(state, detail): same, moved, gone, or not checked. Never include
    response bodies, authentication URLs or credentials in diagnostics."""
    try:
        registry, repository, tag, pinned = target(reference, tag)
        found = current(registry, repository, tag)
        return ("same", f"{tag}: {pinned}") if found == pinned else ("moved", f"{tag}: {pinned} -> {found}")
    except HTTPError as e:
        e.close()
        if e.code == 404:
            return "gone", f"{tag}: registry returned HTTP 404"
        return "not checked", f"registry returned HTTP {e.code}"
    except (URLError, OSError, HTTPException):
        return "not checked", "registry connection failed or timed out"
    except ValueError as e:
        return "not checked", str(e)
