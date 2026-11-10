"""The model endpoint every agent, driver and capability of a run is given.

It listens on 127.0.0.1, holds the real key and adds it on the way out, so
what it relays for never holds one: their requests carry a placeholder. Each
unit has a route of its own, ``http://127.0.0.1:<port>/<tag>/v1``, so a call is
known by the unit that made it without reading its content.

A reply is passed through as it arrives - a streamed one (server-sent events)
byte for byte - and recorded once it is complete, one line per call in
``<record_dir>/<tag>.jsonl``: when it started and ended, the model, the status,
the tokens, and an abridged request and reply. Three API shapes are read,
Chat Completions, Responses and Anthropic's Messages, streamed or not. The
cost is the provider's when it says it (OpenRouter's ``usage.cost``), else an
estimate from ``prices`` when one is given for exactly that model (the
user's price list, ``nullius.prices``), marked as an estimate.

What the relay changes on the way is kept small, so a call through it costs
what the same call made directly would: the connection to the provider is
kept open between calls (a new TLS handshake per call was most of what the
relay added); the caller's headers go on as they came - an agent's session
and capability headers are the provider's to read - except the credentials,
which the relay replaces with its own key, and the headers of the hop itself;
the provider's headers come back the same way (its request id, its rate
limits). It never answers for the provider: a 429 reaches the caller and the
record as it was sent, which is what makes rate limits measurable.

One answer is the relay's own: a run with a spending cap (``ledger``) books
each call before it goes out, and a call that does not fit is not sent. Its
caller gets a 402 that names the relay and the cap, and the record says
``refused``, so it never reads as the provider's error.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

from nullius.redact import redact
from nullius.standard import ledger as ledgers

PLACEHOLDER_KEY = "nullius-relay"
_TAG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TIMEOUT = 900                       # one call, streamed or not: a long reasoning turn is still one call
# The headers of one hop, which a proxy never passes on (RFC 9110, 7.6.1).
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te", "trailer",
        "trailers", "transfer-encoding", "upgrade"}
# Not passed on from the caller: the hop's, the ones the relay sets itself
# (the body it may have rewritten has a length of its own; it asks for an
# uncompressed reply so it can read the reply), and cookies.
_NOT_SENT = _HOP | {"host", "content-length", "accept-encoding", "cookie", "cookie2"}
# A credential, whatever the vendor calls it: the relay's key replaces it.
# The account headers go with them - the relay's key says whose account it is.
_CREDENTIAL = re.compile(r"authorization|api[-_]?key|auth|token|secret|password|credential|"
                         r"^openai-organization$|^openai-project$", re.I)
# Not passed back: the hop's, cookies, and what this server writes itself.
_NOT_RETURNED = _HOP | {"set-cookie", "set-cookie2", "content-length", "date", "server"}
# A kept connection the provider closed while it sat idle fails like this on
# its next use, before any of the reply is read: worth one new connection.
_STALE = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, ConnectionAbortedError)
ANTHROPIC_VERSION = "2023-06-01"


def _cut(text, n=4000):
    if not isinstance(text, str) or len(text) <= n:
        return text
    return text[: n // 4] + f"\n[... {len(text) - n} characters left out ...]\n" + text[-(3 * n // 4):]


def _said(status: int, body: bytes) -> str:
    try:
        e = json.loads(body).get("error") or {}
        m = e.get("message") if isinstance(e, dict) else str(e)
    except Exception:
        m = body[:200].decode("utf-8", "replace")
    return f"HTTP {status}: {m or ''}".strip()[:300]


def _usage(u: Optional[dict]) -> Optional[dict]:
    """Chat Completions', Responses' and Messages' usage, in one shape:
    ``input`` counts every prompt token, ``cached`` those of them a cache
    served. Anthropic counts the tokens read from its cache and those written
    to it apart from ``input_tokens``; both are put back in, as cached."""
    if not isinstance(u, dict):
        return None
    inp = u.get("prompt_tokens", u.get("input_tokens")) or 0
    out = u.get("completion_tokens", u.get("output_tokens")) or 0
    det = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    rdet = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
    cached = (det or {}).get("cached_tokens") or 0
    cache = (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    if cache:
        inp, cached = inp + cache, cache
    got = {"input": inp, "output": out, "cached": cached, "reasoning": (rdet or {}).get("reasoning_tokens") or 0}
    if isinstance(u.get("cost"), (int, float)):
        got["cost"] = u["cost"]
    return got


def _text_of_responses(resp: dict) -> (str, List[dict]):
    text, calls = [], []
    for item in resp.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") in ("output_text", "text"):
                    text.append(c.get("text") or "")
        elif item.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
            calls.append({"name": item.get("name") or item.get("type"),
                          "arguments": _cut(item.get("arguments") or item.get("input") or json.dumps(item.get("action")), 2000)})
    return "".join(text), calls


def _text_of_messages(msg: dict) -> (str, List[dict]):
    text, calls = [], []
    for c in msg.get("content") or []:
        if c.get("type") == "text":
            text.append(c.get("text") or "")
        elif c.get("type") in ("tool_use", "server_tool_use"):
            calls.append({"name": c.get("name"), "arguments": _cut(json.dumps(c.get("input")), 2000)})
    return "".join(text), calls


def read_reply(path: str, body: bytes, streamed: bool) -> dict:
    """What a reply said: its text, its tool calls, its usage."""
    if not streamed:
        try:
            got = json.loads(body)
        except ValueError:
            return {}
        if "choices" in got:
            m = ((got.get("choices") or [{}])[0].get("message")) or {}
            calls = [{"name": (c.get("function") or {}).get("name"), "arguments": _cut((c.get("function") or {}).get("arguments"), 2000)}
                     for c in m.get("tool_calls") or []]
            return {"text": m.get("content"), "tool_calls": calls, "usage": _usage(got.get("usage"))}
        if got.get("type") == "message" and isinstance(got.get("content"), list):
            text, calls = _text_of_messages(got)
            return {"text": text, "tool_calls": calls, "usage": _usage(got.get("usage"))}
        if got.get("object") == "response" or "output" in got:
            text, calls = _text_of_responses(got)
            return {"text": text, "tool_calls": calls, "usage": _usage(got.get("usage"))}
        if got.get("object") == "list" and path.split("?")[0].endswith("/embeddings"):     # how many inputs, and the usage: never the vectors
            return {"embeddings": len(got.get("data") or []), "usage": _usage(got.get("usage"))}
        return {}
    text, calls, usage, args = [], {}, None, {}
    told = None                                  # Messages: the usage of message_start, then message_delta's on it
    for line in body.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "message_start":
            told = dict(((ev.get("message") or {}).get("usage")) or {})
            continue
        if kind == "message_delta":
            told = {**(told or {}), **{k: v for k, v in (ev.get("usage") or {}).items() if v is not None}}
            continue
        if kind == "content_block_start":
            b, i = ev.get("content_block") or {}, ev.get("index", 0)
            if b.get("type") in ("tool_use", "server_tool_use"):
                calls[i], args[i] = b.get("name"), ""
            elif b.get("text"):
                text.append(b["text"])
            continue
        if kind == "content_block_delta":
            d, i = ev.get("delta") or {}, ev.get("index", 0)
            if d.get("type") == "text_delta":
                text.append(d.get("text") or "")
            elif d.get("type") == "input_json_delta":
                args[i] = args.get(i, "") + (d.get("partial_json") or "")
            continue
        if kind in ("response.completed", "response.done", "response.incomplete"):
            t, c = _text_of_responses(ev.get("response") or {})
            return {"text": t, "tool_calls": c, "usage": _usage((ev.get("response") or {}).get("usage"))}
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                text.append(d["content"])
            for tc in d.get("tool_calls") or []:
                i = tc.get("index", 0)
                f = tc.get("function") or {}
                if f.get("name"):
                    calls[i] = f["name"]
                args[i] = args.get(i, "") + (f.get("arguments") or "")
        if ev.get("usage"):
            usage = _usage(ev["usage"])
    return {"text": "".join(text), "tool_calls": [{"name": calls[i], "arguments": _cut(args.get(i, ""), 2000)} for i in sorted(calls)],
            "usage": _usage(told) if told is not None else usage}


def _request_tail(req: dict) -> dict:
    """The part of a request a reader needs: the model, how many messages,
    the last two, abridged, and the reasoning effort it asked for (chat's
    ``reasoning_effort``, the Responses API's ``reasoning.effort``; None when
    it sent none), so a run shows the effort its agent really sent. A coding
    agent resends its whole context on every call; the trajectory comes from
    the session, not from here."""
    items = req.get("messages") if isinstance(req.get("messages"), list) else req.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    items = items if isinstance(items, list) else []
    tail = []
    for m in items[-2:]:
        if isinstance(m, dict):
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
            tail.append({"role": m.get("role") or m.get("type"), "content": _cut(c if isinstance(c, str) else json.dumps(c) if c else "", 2000)})
    effort = req.get("reasoning_effort")
    if effort is None and isinstance(req.get("reasoning"), dict):
        effort = req["reasoning"].get("effort")
    return {"n_messages": len(items), "last": tail, "stream": bool(req.get("stream")),
            "reasoning_effort": effort if isinstance(effort, str) else None}


def _digest(obj) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def request_fields(req: dict) -> dict:
    """Every top-level field of a request, for ``fidelity`` to compare two
    ways of driving one agent - and no prompt: ``instructions``, each tool
    and each ``input``/``messages`` item are kept as a digest and a length (an
    item with its type and role, digested without its own ``id``), the rest as
    sent, a large value digested too."""
    out = {}
    for k, v in req.items():
        if k in ("input", "messages"):
            items = [{"role": "user", "content": v}] if isinstance(v, str) else v if isinstance(v, list) else []
            items = [{n: x for n, x in i.items() if n != "id"} if isinstance(i, dict) else i for i in items]   # an item's own id
            out[k] = [{"type": i.get("type"), "role": i.get("role"), "sha256": _digest(i), "chars": len(json.dumps(i))}
                      if isinstance(i, dict) else {"sha256": _digest(i)} for i in items]
        elif k == "tools" and isinstance(v, list):
            out[k] = [{"name": t.get("name") or (t.get("function") or {}).get("name") or t.get("type"), "sha256": _digest(t)}
                      if isinstance(t, dict) else {"sha256": _digest(t)} for t in v]
        elif k == "instructions" or len(json.dumps(v)) > 600:
            out[k] = {"sha256": _digest(v), "chars": len(v) if isinstance(v, str) else len(json.dumps(v))}
        else:
            out[k] = v
    return out


def forwarded(headers) -> Dict[str, str]:
    """The caller's request headers the relay passes on: all of them but the
    hop's, the ones it sets itself, cookies and every credential."""
    named = {n.strip().lower() for n in (headers.get("Connection") or "").split(",")}     # hop headers by name
    out = {}
    for name, value in headers.items():
        low = name.lower()
        if low in _NOT_SENT or low in named or low.startswith("proxy-") or _CREDENTIAL.search(low):
            continue
        out[name] = value
    return out


def returned(headers) -> List[tuple]:
    """The provider's reply headers passed back to the caller."""
    named = {n.strip().lower() for n in (headers.get("Connection") or "").split(",")}
    return [(n, v) for n, v in headers.items() if n.lower() not in _NOT_RETURNED and n.lower() not in named]


class _Pool:
    """Kept connections to the provider. A connection is taken for one call
    and given back once its reply was read to the end, so no two calls ever
    share one at a time; calls made one after another, from whichever
    thread, reuse it. (The relay's server starts a thread for each of its
    callers' connections, so a pool per thread would rarely be reused.)"""

    KEEP = 16                                    # idle connections kept per provider; more are closed

    def __init__(self, base: str):
        u = urllib.parse.urlsplit(base)
        self.https = u.scheme == "https"
        self.host, self.port = u.hostname, u.port or (443 if self.https else 80)
        self.prefix = u.path.rstrip("/")
        self.idle: List[http.client.HTTPConnection] = []
        self.lock = threading.Lock()
        self.opened = 0                          # connections made, for a test to count

    def take(self, fresh: bool = False):
        if not fresh:
            with self.lock:
                if self.idle:
                    return self.idle.pop(), True
        kind = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        with self.lock:
            self.opened += 1
        return kind(self.host, self.port, timeout=TIMEOUT), False

    def give(self, conn, r):
        """Back to the pool when the reply was read whole and the provider
        keeps the connection open; else closed."""
        if r.isclosed() and not r.will_close:
            with self.lock:
                if len(self.idle) < self.KEEP:
                    self.idle.append(conn)
                    return
        conn.close()

    def send(self, method: str, path: str, body: Optional[bytes], headers: Dict[str, str]):
        """The connection and the provider's response, its headers read. A
        kept connection found closed is replaced once - only before any of
        the reply was read, so a call is never sent twice for an answer."""
        conn, reused = self.take()
        while True:
            try:
                conn.request(method, self.prefix + path, body=body, headers=headers)
                return conn, conn.getresponse()
            except _STALE:
                conn.close()
                if not reused:
                    raise
                conn, reused = self.take(fresh=True)
            except BaseException:
                conn.close()
                raise

    def close(self):
        with self.lock:
            idle, self.idle = self.idle, []
        for c in idle:
            c.close()


class Relay:
    def __init__(self, base: str, key: str, record_dir: str, prices: Optional[Dict[str, dict]] = None,
                 model: Optional[str] = None, side_models=(), key_header: str = "authorization", fields: bool = False,
                 ledger: Optional[str] = None):
        """``base``: the provider's endpoint (``https://api.openai.com/v1``).
        ``model``: when given, every call is sent as this model, whatever the
        caller named - the run's model is the recipe's, not the agent's. A
        route of its own may name another (``url(tag, model=...)``): a
        benchmark's judge is asked as the judge, not as the run's model.
        ``side_models``: the models an agent's own side jobs ask for (a
        review, a summary) that it may call besides the run's model - its
        definition's ``model.side_models``, or a setup's: the upstream's
        code asking a model of its own besides the answerer. Each is a name,
        sent as asked, or ``asked=sent``, sent as the provider names it; a
        side call is recorded ``side``. Any other model is replaced by the
        run's, recorded with ``model_asked``. ``key_header``: how the provider takes its key - a
        bearer ``authorization``, or Anthropic's ``x-api-key``.
        ``fields``: each record also holds the request's fields as sent
        (:func:`request_fields`) - ``fidelity``'s, never a normal run's.
        ``ledger``: the file of the run's spending cap (``ledger``), shared
        by every relay of the job: a call is booked there before it is sent,
        and not sent when it does not fit."""
        self.fields = fields
        self.ledger = ledgers.Ledger(ledger) if ledger else None
        self.base, self.key, self.dir = base.rstrip("/"), key, record_dir
        self.prices, self.model = prices or {}, model
        self.side_models = {a: (b or a) for a, _, b in (str(m).partition("=") for m in side_models or ())}
        self.key_header = key_header.lower()
        self.models: Dict[str, str] = {}
        self.lock = threading.Lock()
        self.pool = _Pool(self.base)
        self.server: Optional[ThreadingHTTPServer] = None
        os.makedirs(record_dir, exist_ok=True)

    def url(self, tag: str, model: Optional[str] = None) -> str:
        if not _TAG.match(tag):
            raise ValueError(f"tag {tag!r}")
        if model:
            self.models[tag] = model
        return f"{self.public or self._own()}/{tag}/v1"

    def _own(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"

    def calls(self, tag: str) -> List[dict]:
        try:
            with open(os.path.join(self.dir, tag + ".jsonl")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def _cost(self, model: str, usage: Optional[dict]):
        if not usage:
            return None, None
        if "cost" in usage:
            return usage["cost"], "provider"
        p = self.prices.get(model)                # exactly this model: one whose name only starts the same is another
        if not p:
            return None, None
        fresh = max(0, usage["input"] - usage["cached"])
        return (fresh * p["input"] + usage["cached"] * p.get("cached", p["input"]) + usage["output"] * p["output"]) / 1e6, "estimate"

    def _said(self, e: Exception) -> str:
        """An exception as the agent and the record may see it: http.client
        quotes a header value it refuses (a key with a stray CR, a BOM), so
        the key itself is cut out of the text whatever else it says."""
        text = f"{type(e).__name__}: {e}"
        for k in {self.key, self.key.strip()} - {""}:
            text = text.replace(k, "[REDACTED]")
        return redact(text)[:200]

    def _headers(self, caller) -> Dict[str, str]:
        out = forwarded(caller)
        has = {k.lower() for k in out}           # a name in any case is the same header: never send it twice
        for name, value in (("Content-Type", "application/json"), ("Accept", "*/*")) + \
                (("anthropic-version", ANTHROPIC_VERSION),) * (self.key_header == "x-api-key"):
            if name.lower() not in has:
                out[name] = value
        if self.key_header == "x-api-key":
            out["x-api-key"] = self.key
        else:
            out["Authorization"] = "Bearer " + self.key
        return out

    def _forward(self, h: BaseHTTPRequestHandler):
        """One call of a caller, ``h``: sent on, passed back, recorded."""
        bits = h.path.split("/", 2)
        if len(bits) < 3 or not _TAG.match(bits[1]):
            h.send_error(404, "no route: /<tag>/v1/...")
            return
        tag, rest = bits[1], "/" + bits[2]
        rest = rest[3:] if rest.startswith("/v1/") else rest
        raw = h.rfile.read(int(h.headers.get("Content-Length") or 0))
        try:
            req = json.loads(raw) if raw else {}
        except ValueError:
            req = {}
        asked = req.get("model") if isinstance(req, dict) else None
        side = False
        if isinstance(req, dict) and req:
            if self.models.get(tag):
                req["model"] = self.models[tag]
            elif self.model and asked != self.model and asked in self.side_models:
                side = True
                req["model"] = self.side_models[asked]
            elif self.model:
                req["model"] = self.model
            if req.get("stream") and rest.split("?")[0].endswith("/chat/completions"):
                req.setdefault("stream_options", {})["include_usage"] = True
            raw = json.dumps(req).encode()
        booking = booked = None
        if self.ledger is not None and h.command == "POST":
            booking, booked, why = self._book(req if isinstance(req, dict) else {}, raw, rest)
            if why:
                self._refuse(h, tag, rest, req, asked, why)
                return
        t0 = time.time()
        conn, r, back, body = None, None, [], b""
        cut = False                              # the reply was cut off: the call may have been billed
        try:
            conn, r = self.pool.send(h.command, rest, raw if h.command == "POST" else None, self._headers(h.headers))
            status, kind, back = r.status, r.getheader("Content-Type") or "application/json", returned(r.headers)
        except Exception as e:
            status, kind = 502, "application/json"
            body = json.dumps({"error": {"message": "the relay could not reach the endpoint: " + self._said(e)}}).encode()
        streamed = r is not None and "text/event-stream" in kind
        whole = False                            # the reply read to its end: its connection can be kept
        if r is not None and not streamed:
            try:
                body, whole = r.read(), True
            except Exception as e:               # cut off mid-reply
                status, kind, back, cut = 502, "application/json", [], True
                body = json.dumps({"error": {"message": "the endpoint's reply was cut off: " + self._said(e)}}).encode()

        def record(body):
            rec = {"t0": t0, "t1": time.time(), "path": rest, "model": req.get("model") if isinstance(req, dict) else None,
                   "model_asked": asked, "status": status, "request": _request_tail(req) if isinstance(req, dict) else {},
                   "headers": sorted(k.lower() for k in forwarded(h.headers))}        # the names sent on, never a value
            if side:
                rec["side"] = True
            if self.fields and isinstance(req, dict) and req:
                rec["fields"] = request_fields(req)
            cost = None
            if status == 200:
                rec["reply"] = read_reply(rest, body, streamed)
                cost, how = self._cost(rec["model"], rec["reply"].get("usage"))
                if cost is not None:
                    rec["cost_usd"], rec["cost_from"] = cost, how
            else:
                rec["error"] = _said(status, body)
            if booking is not None:
                rec["booked_usd"] = booked
                try:
                    if status == 200 or cut:     # what it cost; unknown (no token count, cut off), the booking is kept whole
                        self.ledger.settle(booking, cost, rec["model"], ((rec.get("reply") or {}).get("usage") or {}).get("output"))
                    else:                        # an error of the provider's, or never reached: not billed
                        self.ledger.release(booking)
                except ledgers.Unavailable:
                    pass                         # the booking stays held, against the cap
            with self.lock:
                with open(os.path.join(self.dir, tag + ".jsonl"), "a") as f:
                    f.write(json.dumps(rec) + "\n")

        h.send_response(status)
        if not any(n.lower() == "content-type" for n, _ in back):
            back = back + [("Content-Type", kind)]
        for n, v in back:
            h.send_header(n, v)
        if streamed:
            if not r.getheader("Cache-Control"):
                h.send_header("Cache-Control", "no-cache")
            h.end_headers()
            parts = []
            try:
                while True:
                    chunk = r.read1(65536)
                    if not chunk:
                        whole = True
                        break
                    parts.append(chunk)
                    try:
                        h.wfile.write(chunk)
                        h.wfile.flush()
                    except OSError:
                        pass                     # the caller went away; the record still completes
            except Exception:
                pass
            record(b"".join(parts))              # before the connection closes: the caller's end of stream
        else:
            record(body)                         # before the caller has the reply, so a reader after it finds the call
            if conn is not None:                 # read whole: back to the pool before the caller can make its next call
                if whole:
                    self.pool.give(conn, r)
                else:
                    conn.close()
                conn = None
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            try:
                h.wfile.write(body)
            except OSError:
                pass
        if conn is not None:
            if whole:
                self.pool.give(conn, r)
            else:
                conn.close()

    def _book(self, req: dict, raw: bytes, rest: str) -> tuple:
        """(booking, its amount, None) for a call the cap lets out; (None,
        its amount, why) for one it does not. A model with no price is not
        sent either: a capped run sends no call it cannot price."""
        row = self.prices.get(req.get("model"))
        if not row:
            return None, None, f"no price for the model {req.get('model')!r}: a run with a spending cap sends no call it cannot price"
        try:
            amount = ledgers.bound(req, raw, row, rest, self.ledger.largest(req.get("model")))
            booking, seen = self.ledger.reserve(amount)
        except ledgers.Unavailable as e:
            return None, None, str(e)
        return booking, amount, None if booking else ledgers.refusal(seen, amount)

    def _refuse(self, h: BaseHTTPRequestHandler, tag: str, rest: str, req, asked, why: str) -> None:
        """A call the run's spending cap did not let out: answered here and
        recorded as the relay's own (``refused``), nothing sent."""
        t = time.time()
        rec = {"t0": t, "t1": t, "path": rest, "model": req.get("model") if isinstance(req, dict) else None, "model_asked": asked,
               "status": 402, "refused": "cap", "error": why, "request": _request_tail(req) if isinstance(req, dict) else {},
               "headers": sorted(k.lower() for k in forwarded(h.headers))}
        with self.lock:
            with open(os.path.join(self.dir, tag + ".jsonl"), "a") as f:
                f.write(json.dumps(rec) + "\n")
        body = json.dumps({"type": "error", "error": {"type": "spending_cap", "code": "spending_cap",
                                                      "message": "nullius relay: " + why}}).encode()
        h.send_response(402)
        for n, v in (("Content-Type", "application/json"), ("X-Nullius-Refused", "cap"), ("Content-Length", str(len(body)))):
            h.send_header(n, v)
        h.end_headers()
        try:
            h.wfile.write(body)
        except OSError:
            pass

    # The address ``url`` hands out: the listening one unless the caller sets
    # another - a sidecar listens on 0.0.0.0 and is reached as ``relay``.
    public: Optional[str] = None

    def start(self, host: str = "127.0.0.1", port: int = 0) -> "Relay":
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                relay._forward(self)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        # stop() waits for the serving loop's next look: at the default half-second that idled a worker
        # slot up to 0.5 s at the end of every unit (minutes over a 1319-unit GSM8K run)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.pool.close()


def failure(calls: List[dict]) -> Optional[tuple]:
    """When a unit's model calls ended failing, the exception a trial carries
    for it - named so ``nullius.runs.classify`` sorts it as the
    infrastructure it was - and the endpoint's words. None when the last call
    succeeded (a failure the agent recovered from is not the unit's). A call
    the run's spending cap refused is the unit's wherever it falls: once one
    is refused, every later one is."""
    cut = next((c for c in calls if c.get("refused") == "cap"), None)
    if cut:
        return ledgers.ERROR, cut.get("error") or ""
    if not calls or calls[-1].get("status") == 200:
        return None
    last = calls[-1]
    s, said = last.get("status"), last.get("error") or ""
    if s == 429:
        return "RateLimitError", said
    if s in (401, 403):
        return "AgentAuthenticationError", said
    if s == 404 and "model" in said.lower():
        return "ModelNotFoundError", said
    if s == 502:
        return "NetworkConnectionError", said
    return "ApiError", said


# -- the relay as a process of its own: a container backend's sidecar ----------
def serve(argv=None) -> int:
    """``python3 -m nullius.standard.relay serve --listen HOST:PORT --base URL
    --record DIR --key-file PATH [--model M] [--route TAG=MODEL ...] [--side-model M ...] [--key-header NAME]
    [--public URL] [--prices JSON] [--ledger FILE]``.

    ``--base`` is the provider's endpoint, ``--route`` a route sent as another
    model (a benchmark's judge), ``--public`` the address ``url`` hands out.
    The real key is read from ``--key-file`` and nowhere else: an argument or
    an environment variable is visible to anyone who lists processes or
    inspects a container. One JSON line, ``{"listening": "http://host:port"}``, says it
    is ready; SIGTERM (or SIGINT) stops it."""
    import argparse
    import signal
    ap = argparse.ArgumentParser(prog="nullius.standard.relay serve")
    ap.add_argument("--listen", default="127.0.0.1:0", help="HOST:PORT")
    ap.add_argument("--base", required=True, help="the provider's endpoint")
    ap.add_argument("--record", required=True, help="the folder each route's calls are recorded in")
    ap.add_argument("--key-file", required=True, help="the file that holds the provider's key")
    ap.add_argument("--model", help="every call sent as this model")
    ap.add_argument("--route", action="append", default=[], help="TAG=MODEL")
    ap.add_argument("--side-model", action="append", default=[], help="a model the agent may call as asked")
    ap.add_argument("--key-header", default="authorization", choices=("authorization", "x-api-key"))
    ap.add_argument("--public", help="the address url() hands out, when not the listening one")
    ap.add_argument("--prices", help="a JSON object: model -> USD per million tokens, for a cost estimate")
    ap.add_argument("--ledger", help="the file of the run's spending cap: a call is booked there before it is sent")
    a = ap.parse_args(argv)
    try:
        with open(a.key_file) as f:
            key = f.read().strip()
    except OSError as e:
        print(json.dumps({"error": f"cannot read --key-file: {e}"}), flush=True)
        return 2
    if not key:
        print(json.dumps({"error": f"--key-file {a.key_file} contains no key"}), flush=True)
        return 2
    host, _, port = a.listen.rpartition(":")
    r = Relay(a.base, key, a.record, prices=json.loads(a.prices) if a.prices else None, model=a.model,
              side_models=a.side_model, key_header=a.key_header, ledger=a.ledger)
    for route in a.route:
        tag, _, model = route.partition("=")
        if not _TAG.match(tag) or not model:
            print(json.dumps({"error": f"--route {route!r}: TAG=MODEL"}), flush=True)
            return 2
        r.models[tag] = model
    r.public = a.public
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    r.start(host or "127.0.0.1", int(port or 0))
    print(json.dumps({"listening": r._own()}), flush=True)
    while not stop.wait(1):
        pass
    r.stop()
    return 0


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] != ["serve"]:
        sys.exit("python3 -m nullius.standard.relay serve --listen HOST:PORT --base URL --record DIR --key-file PATH [--model M]")
    sys.exit(serve(sys.argv[2:]))
