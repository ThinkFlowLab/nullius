"""Each model exchange of a unit, kept whole: the request the relay sent and the reply that came back.

A unit's relay (``relay.Relay``) writes, beside its call record, under ``<record_dir>/exchanges/``:

- ``<tag>.jsonl``: one line per call it sent on an agent's route (``relay.agent_route``), its side calls
  included: ``n``, the call's number on its route (the call record's ``n`` too), ``t0``, ``path``,
  ``sent_sha256`` (the sha256 of the bytes sent), ``sent`` (the request after the relay's changes, its
  parts by hash), ``headers`` (the values of the headers sent, credentials left out) and ``reply`` (its
  status, whether it was streamed, its body as it arrived, as text);
- ``parts.jsonl``: each distinct part once, ``{"sha256": ..., "part": ...}``. A part is one item of
  ``messages`` or ``input``, one tool, or the system text (``system``, ``instructions``); its hash is the
  sha256 of its JSON. Inside a part, an inline base64 picture is ``{"blob": sha256, "type": ..., "bytes":
  ..., "as": "data_url"|"base64"}`` and its bytes are in
- ``blobs/<sha256>.<ext>``: each distinct picture once, as its bytes (png, jpeg, webp, gif).

Each ``.jsonl`` file begins with the mark of its format, ``{"schema": "nullius.exchanges/v1"}`` (``HEADER``): a
line no reader takes for a call or a part. A file without it is in version 1, the format before the mark. A
picture is the bytes its name hashes and carries no mark: the lines that name it do. A reader refuses a file
marked with a later version or another format (:class:`Later`), naming both; it never guesses.

What is stored is what was sent, the relay's key cut out of every string: a request that held the key
then no longer rebuilds to its hash, and its line says ``key_cut``. Nothing else is redacted, so a secret
an agent typed in a prompt or showed in a screenshot is in the store as it was sent. The modes
(``MODES``): ``whole``; ``text``, the same without the pictures' bytes (their hash, type and size stay);
``off``, no store. A unit's store is bounded (``max_mb``): once a call's new content would take it past
the bound, that call's line and every later one say ``cut``, and the call record goes on. Nothing here
stops or changes a reply: a failure is written in the call's line (``error``) when it can be, and dropped
when it cannot.

:func:`rebuild` gives a recorded request's bytes back from its line; :func:`new_parts`, the parts a call
holds that the call before did not.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import threading
from typing import Dict, List, Optional

from nullius import settings

MODES = ("whole", "text", "off")
DEFAULT = "whole"
MAX_MB = 256                                     # a unit's store, in millions of bytes
FOLDER = "exchanges"
PARTS = "parts.jsonl"
BLOBS = "blobs"
# the fields of a request kept by part: a list's items each a part, a text one part
PART_FIELDS = ("system", "instructions", "tools", "messages", "input")
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp", "image/gif": "gif"}
_DATA_URL = re.compile(r"data:(image/(?:png|jpeg|jpg|webp|gif));base64,")
_TEXT_DATA_URL = re.compile(r"data:(image/[^;,\s]+);base64,", re.IGNORECASE)
MARK = frozenset(("blob", "type", "bytes", "as"))   # a picture's place in a stored part
LITERAL = "literal"
VERSION = 1                                      # the store's format, as this code writes and reads it
HEADER = json.dumps({"schema": f"nullius.exchanges/v{VERSION}"}) + "\n"   # the first line of each .jsonl file
_SCHEMA = re.compile(r"nullius\.exchanges/v([1-9][0-9]*)")


class Missing(Exception):
    """A request that cannot be rebuilt from the store: the message says what is missing."""


class Later(Missing):
    """A file of the store in a later version of its format than this code reads, or in another format."""


def _picture(text: str, media: Optional[str] = None, text_only: bool = False) -> Optional[tuple]:
    """(type, bytes, form) of an inline picture: a data URL, or a Messages source's base64 ``data`` of
    type ``media``. Whole mode requires the bytes to encode back to the original text."""
    if media is None:
        m = (_TEXT_DATA_URL if text_only else _DATA_URL).match(text)
        if not m:
            return None
        kind, b64, form = m.group(1), text[m.end():], "data_url"
    elif media in EXT or (text_only and media.lower().startswith("image/")):
        kind, b64, form = media, text, "base64"
    else:
        return None
    if text_only:
        b64 = "".join(b64.split())
        b64 += "=" * (-len(b64) % 4)
    try:
        raw = base64.b64decode(b64, altchars=b"-_" if text_only else None, validate=True)
    except (binascii.Error, ValueError):
        return None
    # the alphabet alone, padding at the end only (validate): every whole group encodes back to itself, so the
    # text is the bytes' own encoding when its length is and its last group is
    if not text_only and (len(b64) != 4 * ((len(raw) + 2) // 3) or
                          (raw and base64.b64encode(raw[-(len(raw) % 3 or 3):]).decode() != b64[-4:])):
        return None
    return kind, raw, form


def _mark(found: dict, kind: str, raw: bytes, form: str) -> dict:
    h = hashlib.sha256(raw).hexdigest()
    found[h] = (kind, raw)
    return {"blob": h, "type": kind, "bytes": len(raw), "as": form}


def _lift(v, found: dict, text_only: bool = False):
    """``v`` with each inline picture replaced by its mark, the picture in ``found`` by its hash."""
    if isinstance(v, str):
        p = _picture(v, text_only=text_only) if v.lower().startswith("data:image/") else None
        return _mark(found, *p) if p else v
    if isinstance(v, list):
        return [_lift(x, found, text_only) for x in v]
    if isinstance(v, dict):
        media = v.get("media_type") if v.get("type") == "base64" and isinstance(v.get("media_type"), str) else None
        out = {}
        for k, x in v.items():
            p = _picture(x, media, text_only) if media and k == "data" and isinstance(x, str) else None
            out[k] = _mark(found, *p) if p else _lift(x, found, text_only)
        return {LITERAL: out} if set(v) in (MARK, {LITERAL}) else out
    return v


def secret_keys(keys):
    """The keys to cut, longest first. A keyless provider's placeholder is not a secret."""
    return tuple(sorted({k for k in keys if k and k != settings.MODEL_KEY_PLACEHOLDER}, key=len, reverse=True))


def _cut_key(v, keys):
    """``v`` with each of ``keys`` cut out of every string, a mapping's keys too."""
    if isinstance(v, str):
        for k in secret_keys(keys):
            v = v.replace(k, "[REDACTED]")
        return v
    if isinstance(v, list):
        return [_cut_key(x, keys) for x in v]
    if isinstance(v, dict):
        return {_cut_key(k, keys): _cut_key(x, keys) for k, x in v.items()}
    return v


def _text_body(text: str) -> str:
    """Remove inline pictures from a JSON body, an event stream or a data URL."""
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    found = {}
    lifted = _lift(value, found, True)
    if found:
        return json.dumps(lifted)
    if isinstance(value, str):
        return "".join("data:" + _text_body(row[5:].rstrip("\r\n")) + row[len(row.rstrip("\r\n")):]
                       if row.startswith("data:") else row for row in text.splitlines(keepends=True))
    return text


def split(req: dict, found: dict, parts: dict, text_only: bool = False) -> dict:
    """A request's ``sent``: each part by its hash, the rest as it was. ``parts`` gains each part's
    stored line by hash, ``found`` each picture's (type, bytes)."""
    sent = {}
    for k, v in req.items():
        if k not in PART_FIELDS:
            sent[k] = _lift(v, found, text_only)
        elif isinstance(v, list):
            sent[k] = {"parts": [_part(x, found, parts, text_only) for x in v]}
        elif isinstance(v, str):
            sent[k] = {"part": _part(v, found, parts, text_only)}
        else:
            sent[k] = {"as_sent": _lift(v, found, text_only)}
    return sent


def _part(x, found: dict, parts: dict, text_only: bool = False) -> str:
    mine = {}
    text = json.dumps(_lift(x, mine, text_only), separators=(",", ":"))
    h = hashlib.sha256(text.encode()).hexdigest()
    parts[h] = '{"sha256":"%s","part":%s}' % (h, text)
    found.update(mine)
    return h


class Store:
    """One unit's store, written by its relay: the parts and pictures already written are known by
    their hashes, so a call that resends its context adds only what is new."""

    def __init__(self, record_dir: str, mode: str = DEFAULT, max_mb: int = MAX_MB, keys=()):
        if mode not in MODES or mode == "off":
            raise ValueError(f"exchanges {mode!r}: whole or text")
        if isinstance(max_mb, bool) or not isinstance(max_mb, int) or max_mb < 1:
            raise ValueError(f"exchanges_max_mb {max_mb!r}: a whole number of MB, at least 1")
        self.dir, self.mode, self.max_mb, self.bound = os.path.join(record_dir, FOLDER), mode, max_mb, max_mb * 10 ** 6
        self.keys = secret_keys(keys)
        self.parts: set = set()
        self.blobs: set = set()
        self.size = 0
        self.cut = False                         # the bound was met: every later call is cut
        self.lock = threading.Lock()

    def write(self, tag: str, n: int, t0: float, path: str, req: Optional[dict], raw: bytes, headers: Dict[str, str],
              status: int, streamed: bool, body: bytes) -> None:
        """One call's line, its new parts and pictures. ``req``: the request whose ``json.dumps`` is
        ``raw``, the bytes sent; None when the relay sent the body as it came (it is then kept whole as
        ``sent_body``). ``headers``: those sent, the credentials already left out. Never raises."""
        head = {"n": n, "t0": t0, "path": path, "sent_sha256": None}
        try:
            head["path"] = _cut_key(path, self.keys)
            head["sent_sha256"] = hashlib.sha256(raw).hexdigest()
            held = any(k.encode() in raw for k in self.keys)
            line = dict(head)
            found, parts = {}, {}
            if req is None:
                line["sent_body"] = _cut_key(raw.decode("utf-8", "replace"), self.keys)
                line["error"] = "the request's body is not a JSON object: it is kept whole, not by part"
            else:
                line["sent"] = split(_cut_key(req, self.keys) if held else req, found, parts, self.mode == "text")
            if held:
                line["key_cut"] = True
            line["headers"] = _cut_key(headers, self.keys)
            line["reply"] = {"status": status, "streamed": streamed, "body": _cut_key(body.decode("utf-8", "replace"), self.keys)}
            if self.mode == "text":
                line["headers"] = {k: _text_body(v) for k, v in line["headers"].items()}
                line["reply"]["body"] = _text_body(line["reply"]["body"])
                if "sent_body" in line:
                    line["sent_body"] = _text_body(line["sent_body"])
            text = json.dumps(line)
            self._keep(tag, head, text, parts, found)
        except Exception as e:                   # nothing of the store may reach the caller's reply
            self._failed(tag, head, e)

    def _keep(self, tag: str, head: dict, text: str, parts: dict, found: dict) -> None:
        with self.lock:
            new_parts = "".join(p + "\n" for h, p in parts.items() if h not in self.parts)
            new_blobs = [(h, kind, raw) for h, (kind, raw) in found.items() if h not in self.blobs] if self.mode == "whole" else []
            paths = [os.path.join(self.dir, name) for name in [tag + ".jsonl"] + ([PARTS] if new_parts else [])]
            marks = sum(len(HEADER) for path in paths if not os.path.exists(path) or not os.path.getsize(path))
            if self.cut or self.size + marks + len(new_parts) + sum(len(raw) for _, _, raw in new_blobs) + len(text) + 1 > self.bound:
                self.cut = True
                self._append(tag + ".jsonl", json.dumps(dict(head, cut=f"the store reached its bound of {self.max_mb} MB: "
                                                                        "this call and every later one are not kept")) + "\n")
                return
            for h, kind, raw in new_blobs:       # what a line names is written before it
                self._put(os.path.join(BLOBS, f"{h}.{EXT[kind]}"), raw)
                self.blobs.add(h)
            if new_parts:
                self._append(PARTS, new_parts)
                self.parts.update(parts)
            self._append(tag + ".jsonl", text + "\n")

    def _failed(self, tag: str, head: dict, e: Exception) -> None:
        try:
            said = _cut_key(f"{type(e).__name__}: {e}", self.keys)[:500]
            with self.lock:
                self._append(tag + ".jsonl", json.dumps(dict(head, error=said)) + "\n")
        except Exception:
            pass                                 # a full disk: the call record still says the call

    def _append(self, name: str, text: str) -> None:
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, name)
        before = os.path.getsize(path) if os.path.exists(path) else 0
        if not before:
            text = HEADER + text                 # a new file begins with the mark of its format
        try:
            with open(path, "a") as f:
                if f.write(text) != len(text):
                    raise OSError("the store append was incomplete")
        except Exception:
            if os.path.exists(path):
                os.truncate(path, before)
            raise
        self.size += len(text)                   # ASCII: json.dumps escapes the rest

    def _put(self, name: str, data: bytes) -> None:
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as f:
            if f.write(data) != len(data):
                raise OSError("the stored picture was incomplete")
        os.replace(path + ".part", path)
        self.size += len(data)


# -- reading a store ---------------------------------------------------------------
def _inside(folder: str, name: str) -> str:
    path = os.path.realpath(os.path.join(folder, name))
    if os.path.commonpath((folder, path)) != folder:
        raise Missing("the stored path resolves outside its folder")
    return path


def _digest(value) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise Missing("the stored digest must be 64 hexadecimal digits")
    return value


def _picture_path(folder: str, mark: dict) -> str:
    name = f"{_digest(mark['blob'])}.{EXT.get(mark['type'], 'bin')}"
    return _inside(os.path.join(os.path.realpath(folder), BLOBS), name)


def lines(folder: str, tag: str) -> List[dict]:
    """The lines of route ``tag`` in the store at ``folder`` (``<record_dir>/exchanges``); a line that
    cannot be read is left out."""
    return _jsonl(_inside(os.path.realpath(folder), tag + ".jsonl"))


def tags(folder: str) -> List[str]:
    """The routes a store holds lines of."""
    try:
        return sorted(n[:-6] for n in os.listdir(folder) if n.endswith(".jsonl") and n != PARTS)
    except OSError:
        return []


def _jsonl(path: str) -> List[dict]:
    out = []
    try:
        with open(path) as f:
            for row in f:
                try:
                    got = json.loads(row)
                except ValueError:
                    continue
                if isinstance(got, dict):
                    out.append(got)
    except OSError:
        pass
    return unmarked(out, os.path.basename(path))


def unmarked(rows: List[dict], name: str) -> List[dict]:
    """The rows of the store's file ``name`` without the mark of its format; a file without one is in
    version 1. Raises :class:`Later` for a mark of a later version or of another format."""
    out = []
    for row in rows:
        if set(row) != {"schema"}:
            out.append(row)
            continue
        said = row["schema"]
        m = _SCHEMA.fullmatch(said) if isinstance(said, str) else None
        if m is None:
            raise Later(f"{name} is marked {said!r:.80}, not a version of the exchange store's format; "
                        f"this Nullius reads its version {VERSION}")
        if int(m.group(1)) > VERSION:
            raise Later(f"{name} is in version {int(m.group(1))} of the exchange store's format; "
                        f"this Nullius reads version {VERSION}: read it with a later Nullius")
    return out


def stored_parts(folder: str) -> Dict[str, dict]:
    """Each readable part by its hash: {"part": ..., "as_sent": bool}. Damaged rows are left out."""
    parts = {}
    for p in _jsonl(_inside(os.path.realpath(folder), PARTS)):
        if "part" not in p:
            continue
        try:
            h = _digest(p.get("sha256"))
            if not p.get("as_sent"):
                _drop(p["part"], lambda mark: _picture_path(folder, mark))
        except Missing:
            continue
        parts[h] = {"part": p.get("part"), "as_sent": bool(p.get("as_sent"))}
    return parts


def _drop(v, picture):
    """A stored part with each picture's mark replaced by ``picture(mark)``."""
    if isinstance(v, dict):
        if set(v) == {LITERAL}:
            return {k: _drop(x, picture) for k, x in v[LITERAL].items()}
        if set(v) == MARK:
            return picture(v)
        return {k: _drop(x, picture) for k, x in v.items()}
    if isinstance(v, list):
        return [_drop(x, picture) for x in v]
    return v


def rebuild(folder: str, line: dict, parts: Optional[Dict[str, dict]] = None) -> bytes:
    """The bytes a recorded call sent, put back together from its line, its parts and its pictures:
    what ``sent_sha256`` is the hash of when nothing was cut from them. Raises :class:`Missing` when
    the store does not hold the request."""
    parts = stored_parts(folder) if parts is None else parts
    if line.get("cut"):
        raise Missing(f"call {line.get('n')} was not kept: {line['cut']}")
    if "sent_body" in line:
        return line["sent_body"].encode()
    if not isinstance(line.get("sent"), dict):
        raise Missing(f"call {line.get('n')} has no request in the store" + (f": {line['error']}" if line.get("error") else ""))
    def part(h):
        _digest(h)
        if h not in parts:
            raise Missing(f"the part {h} is not in the store")
        got = parts[h]
        return got["part"] if got["as_sent"] else _drop(got["part"], picture)

    def picture(mark):
        name = _picture_path(folder, mark)
        try:
            with open(name, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            raise Missing(f"the picture {mark['blob']} is not in the store (a store kept as text holds no pictures)")
        return f"data:{mark['type']};base64,{b64}" if mark["as"] == "data_url" else b64

    req = {}
    for k, v in line["sent"].items():
        if k in PART_FIELDS and isinstance(v, dict) and set(v) == {"parts"}:
            req[k] = [part(h) for h in v["parts"]]
        elif k in PART_FIELDS and isinstance(v, dict) and set(v) == {"part"}:
            req[k] = part(v["part"])
        elif k in PART_FIELDS and isinstance(v, dict) and set(v) == {"as_sent"}:
            req[k] = _drop(v["as_sent"], picture)
        else:
            req[k] = _drop(v, picture)
    return json.dumps(req).encode()


def check(folder: str, tag: str, n: int) -> dict:
    """Call ``n`` of route ``tag`` rebuilt: {"request": bytes or None, "sha256", "sent_sha256", "same",
    "why"}; ``why`` says what kept it from rebuilding or from matching. A file in a later format raises
    :class:`Later`."""
    line = next((x for x in lines(folder, tag) if x.get("n") == n), None)
    if line is None:
        raise Missing(f"no call {n} in the store of {tag}")
    try:
        got = rebuild(folder, line)
    except Later:
        raise
    except Missing as e:
        return {"request": None, "sha256": None, "sent_sha256": line.get("sent_sha256"), "same": False, "why": str(e)}
    sha = hashlib.sha256(got).hexdigest()
    same = sha == line.get("sent_sha256")
    why = None if same else ("the relay's key was cut out of the request" if line.get("key_cut")
                             else "the store's parts do not rebuild the bytes sent")
    return {"request": got, "sha256": sha, "sent_sha256": line.get("sent_sha256"), "same": same, "why": why}


def new_parts(line: dict, before: Optional[dict] = None) -> List[str]:
    """The parts call ``line`` holds that ``before``, the call before it, did not: their hashes, in the
    request's order."""
    def held(x):
        out = []
        for k, v in ((x or {}).get("sent") or {}).items():
            if k in PART_FIELDS and isinstance(v, dict):
                out += list(v.get("parts") or []) + ([v["part"]] if isinstance(v.get("part"), str) else [])
        return out
    seen = set(held(before))
    return [h for h in held(line) if h not in seen]
