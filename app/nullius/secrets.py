"""A benchmark's licences: the secrets its environments need, kept per
benchmark outside the repository.

A licence belongs to the software of one benchmark's dataset, so it is kept
under that benchmark: ``secrets/<benchmark>/<NAME>`` in the user configuration directory
(``SECRETS_DIR`` moves the root). Two benchmarks may each ask for a
``LICENSE_KEY``; they are two files in two folders and never mix. Each
folder is 0700 and each entry a 0600 file holding the bytes it was given -
a licence file keeps its bytes, a key or a server address its text - so
nothing of it is ever in the settings file or in this process's environment.
A data provider's token (Settings → Data providers) has a folder of its own
(``PROVIDERS``): a credential declared with that ``provider`` is read from
there alone, never from the benchmark's (:func:`store_of`).

``index.json`` beside them says, per name, when it was set, when it expires
(when said), its kind and the file it came from - never the value. A page
reads :func:`status` or :func:`state`; only a run reads :func:`value` or
:func:`path`, when a unit starts (``standard/run.py``), or to send it to a
host over stdin (``execution``). Standard library only.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import time
from typing import Dict, Optional, Tuple, Union
from nullius import paths

KINDS = ("value", "file", "server")
INDEX = "index.json"
# Shared data-provider credentials remain in the owner-only secret store.
HUGGINGFACE = "data-provider-huggingface"
# The data providers a benchmark's credential may name (``provider`` in its ``needs_secrets``):
# the folder each one's token is kept in, and its name there.
PROVIDERS = {"huggingface": (HUGGINGFACE, "HF_TOKEN")}
_BENCH = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A licence server: a host name or an IPv4 address, and its port.
_SERVER = re.compile(r"^(?=.{1,253}:)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?:([0-9]{1,5})$")
MAX_BYTES = 1_000_000                # a licence file is small; anything larger is not one


def root(env=os.environ) -> str:
    return paths.secrets_dir(env)


def _folder(bench: str, make: bool = False) -> str:
    if not isinstance(bench, str) or not _BENCH.match(bench):
        raise ValueError(f"{bench!r} is not a benchmark's key")
    d = os.path.join(root(), bench)
    if make:
        os.makedirs(root(), mode=0o700, exist_ok=True)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(root(), 0o700)
        os.chmod(d, 0o700)
    return d


def _name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"{name!r} is not a licence's name (capitals, digits, underscores)")
    return name


def _write(path: str, data: bytes) -> None:
    """Written whole under a temporary name beside it, 0600 from its first
    byte, then renamed over the old one."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _index(bench: str) -> Dict[str, dict]:
    try:
        with open(os.path.join(_folder(bench), INDEX)) as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def problem(kind: str, value: Union[str, bytes]) -> Optional[str]:
    """Why ``value`` cannot be a licence of ``kind``, or None. A key is one
    line; a server is ``host:port``; a file is at most a megabyte."""
    if kind not in KINDS:
        return f"a licence is one of {KINDS}"
    data = value.encode() if isinstance(value, str) else bytes(value)
    if not data:
        return "it is empty"
    if len(data) > MAX_BYTES:
        return f"it is larger than {MAX_BYTES // 1000} kB"
    if kind == "file":
        return None
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return "it is not text"
    if "\n" in text or "\r" in text or "\0" in text:
        return "it is one line"
    if kind == "server":
        m = _SERVER.match(text)
        if not m or not 0 < int(m.group(1)) < 65536:
            return "a licence server is host:port (a name or an IPv4 address, and its port)"
    return None


def set(bench: str, name: str, value: Union[str, bytes], *, kind: str = "value", filename: str = "",
        expires: str = "") -> None:
    """Keep ``value`` as ``bench``'s licence ``name``, replacing any before.
    ``expires``: YYYY-MM-DD, or empty. Refused (ValueError) when it is not a
    licence of its kind."""
    _name(name)
    if isinstance(value, str):
        value = value.strip()
    why = problem(kind, value)
    if why:
        raise ValueError(f"{name}: {why}")
    if expires and not _DATE.match(expires):
        raise ValueError(f"{name}: an expiry date is YYYY-MM-DD")
    d = _folder(bench, make=True)
    _write(os.path.join(d, name), value.encode() if isinstance(value, str) else bytes(value))
    idx = _index(bench)
    idx[name] = {"on": time.strftime("%Y-%m-%d"), "kind": kind,
                 **({"expires": expires} if expires else {}),
                 **({"filename": os.path.basename(filename)[:120]} if filename else {})}
    _write(os.path.join(d, INDEX), json.dumps(idx, indent=1, sort_keys=True).encode())


def delete(bench: str, name: str) -> bool:
    """Remove ``bench``'s licence ``name``; whether there was one."""
    _name(name)
    d = _folder(bench)
    try:
        os.unlink(os.path.join(d, name))
        there = True
    except FileNotFoundError:
        there = False
    idx = _index(bench)
    if idx.pop(name, None) is not None:
        _write(os.path.join(d, INDEX), json.dumps(idx, indent=1, sort_keys=True).encode())
    return there


def status(bench: str) -> Dict[str, dict]:
    """Every licence ``bench`` holds: {name: {set, on, expires, kind,
    filename, expired}} - dates and names, never a value."""
    out = {}
    d = _folder(bench)
    today = time.strftime("%Y-%m-%d")
    for name, rec in _index(bench).items():
        if not _NAME.match(name) or not os.path.isfile(os.path.join(d, name)):
            continue
        exp = rec.get("expires") or ""
        out[name] = {"set": True, "on": rec.get("on") or "", "expires": exp, "kind": rec.get("kind") or "value",
                     "filename": rec.get("filename") or "", "expired": bool(exp) and exp < today}
    return out


def path(bench: str, name: str) -> Optional[str]:
    """Where ``bench``'s licence ``name`` is on this machine, or None: what a
    run mounts, or names to a process, for a licence file."""
    p = os.path.join(_folder(bench), _name(name))
    return p if os.path.isfile(p) else None


def value(bench: str, name: str) -> Optional[bytes]:
    """The bytes of ``bench``'s licence ``name``, or None. A run's alone."""
    p = path(bench, name)
    if p is None:
        return None
    with open(p, "rb") as f:
        return f.read()


def store_of(bench: str, name: str, provider: str = "") -> Tuple[str, str]:
    """Where ``bench``'s credential ``name`` is kept, (folder, name): its
    data provider's alone when ``provider`` is said, else the benchmark's.
    ValueError for a provider that is not one."""
    if not provider:
        return bench, name
    if provider not in PROVIDERS:
        raise ValueError(f"{provider!r} is not a data provider ({', '.join(PROVIDERS)})")
    return PROVIDERS[provider]


def state(bench: str, name: str, provider: str = "") -> Optional[dict]:
    """:func:`status` of one credential, read where it is kept
    (:func:`store_of`); None when it is not set."""
    folder, kept = store_of(bench, name, provider)
    return status(folder).get(kept)


# What a run on a host is handed of the licences, one line of its launcher's stdin (``execution``).
HANDED = "NULLIUS_LICENCES"


def unpack(line: str) -> Dict[str, bytes]:
    """Decode a host's licence line: base64 of {name: base64 bytes}.
    Return {name: bytes}, or nothing for a line that is not one."""
    try:
        got = json.loads(base64.b64decode(line.encode(), validate=True))
        return {n: base64.b64decode(v.encode(), validate=True) for n, v in got.items()
                if isinstance(n, str) and _NAME.match(n) and isinstance(v, str)}
    except (ValueError, AttributeError, TypeError):
        return {}
