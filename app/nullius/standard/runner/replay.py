"""One recorded model call sent again: K times, as it was or changed, under a spending cap, the answers counted.

A run that keeps its exchange record (``exchanges``) holds each call of its agent whole. Replay reads one:

- :func:`source` loads call N of a trial as the request that was sent, rebuilt from the store and checked
  against its recorded sha256, with the job's provider, its pin and its price;
- :func:`prepare` applies named changes (:func:`replace_text`, :func:`system_text`, :func:`tool_description`,
  :func:`tools`, :func:`drop_last`, :func:`set_field`, :func:`model`, :func:`served_by`, :func:`provider`,
  :func:`through`), each recorded with the request's sha256 before and after, and says what would be sent and
  what it is likely to cost; it sends nothing, so it is the dry run;
- :func:`send` posts the prepared request K times to a relay of its own (``relay.Relay``): the key from the
  environment, the model it forces, the pin, the spending cap (``ledger``), the cost and the record are the
  code a run uses. Each answer is read by a rule (:func:`first_tool_call` by default, or a command,
  :func:`command_rule`) and the distinct answers are counted.

Everything is kept in one folder per replay, never in the run's tree: ``replay.json`` (the source call, the
changes, the cap, the rule, the spend, the counts; written before the first send and again at the end),
``request.json`` (the bytes sent), ``answers.jsonl`` (each answer read, one line per send), ``relay/`` (the
replay relay's call record and exchange record: each answer whole, as it arrived) and ``ledger/``.

A request that names a conversation stored at the provider (``previous_response_id``, ``conversation``, an
``item_reference``) is refused: what the provider holds is not in the record. The key is never written; a
command given as a change or a rule runs without it in its environment.
"""
from __future__ import annotations

import concurrent.futures
import copy
import datetime
import hashlib
import http.client
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from nullius import prices as price_list, providers, settings
from nullius.standard.calls import exchanges, ledger as ledgers, relay
from nullius.traces import calls as by_call

SCHEMA = "nullius.replay/v1"
MAX_TIMES = 1000                                 # sends of one replay
COMMAND_SECONDS = 120                            # a change's or a rule's command
STOP_STATUS = (401, 403)                         # the key refused: every later send would be too
_TRY = re.compile(r"\.try-[1-9][0-9]*$")
_STORED = ("previous_response_id", "conversation")


class NotFound(Exception):
    """No such job, trial, record or call."""


class NotWhole(Exception):
    """The call's request is not in the record whole: cut at the store's bound, a part or a picture missing, a
    store kept as text, or bytes that no longer rebuild to their recorded sha256."""


class Refused(Exception):
    """A replay that cannot be made as asked: the message says why and what to give instead."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _dumps(req: dict) -> bytes:
    return json.dumps(req).encode()             # as the relay sends a request it parsed


def _jsonl(path: str) -> List[dict]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for row in f:
                try:
                    got = json.loads(row)
                except ValueError:
                    continue
                if isinstance(got, dict):
                    out.append(got)
    except OSError:
        pass
    return out


def _inside(root: str, *parts: str) -> str:
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, *parts))
    if any(".." in p.split(os.sep) for p in parts) or os.path.commonpath((root, path)) != root:
        raise Refused("an artifact path resolves outside its folder")
    return path


def _check_tree(root: str) -> None:
    """Check existing artifacts before a reader or writer follows their paths."""
    seen = set()
    for folder, dirs, files in os.walk(root, followlinks=True):
        real = _inside(root, os.path.relpath(folder, root))
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        for name in dirs + files:
            _inside(root, os.path.relpath(os.path.join(folder, name), root))


# -- the source: one recorded call ---------------------------------------------------------------------------
@dataclass
class Source:
    """Call ``call`` of a trial's agent route, its request rebuilt and checked against ``sent_sha256``."""
    job_dir: str
    trial: str
    trial_dir: str
    route: str
    call: int
    path: str
    request: dict
    sent_sha256: str
    headers: Dict[str, str]
    reply: dict                                  # status, streamed, body as it arrived
    provider: Optional[str]                      # the job's (config.json), None when it does not say
    served_by: Dict[str, List[str]]              # the job's pin by model
    prices: Dict[str, dict]                      # the job's price rows by model
    record: dict = field(default_factory=dict)   # the call's line in the call record: cost, tokens

    @property
    def model(self) -> str:
        return str(self.request.get("model") or "")


def trial_dir(job_dir: str, trial: str) -> str:
    """The folder of ``trial`` in ``job_dir``: ``<trial>``, or ``retried/<trial>`` for an earlier try (``.try-N``)."""
    if not trial or trial in (".", "..") or "/" in trial or os.sep in trial:
        raise NotFound(f"{trial!r} is not a trial's name: the name of a folder of the job, as `nullius runs show` prints it")
    if not os.path.isdir(job_dir):
        raise NotFound(f"no such directory: {job_dir}")
    where = os.path.join(job_dir, "retried", trial) if _TRY.search(trial) else os.path.join(job_dir, trial)
    where = _inside(job_dir, os.path.relpath(where, job_dir))
    if not os.path.isdir(where):
        raise NotFound(f"no trial named {trial!r} in {job_dir}")
    return where


def _store(tdir: str) -> tuple:
    folder = _inside(tdir, os.path.relpath(by_call.store_folder(tdir), tdir))
    _check_tree(folder)
    routes = exchanges.tags(folder)
    tag = by_call.route_tag(tdir, routes)
    if tag not in routes:
        raise NotFound(f"{os.path.basename(tdir)} kept no exchanges (a run before them, or --exchanges off)" if not routes
                       else f"{os.path.basename(tdir)} holds the exchanges of several routes: {', '.join(routes)}")
    return folder, tag


def _record_rows(tdir: str, tag: str) -> List[dict]:
    rows = _jsonl(_inside(tdir, by_call.CALLS))
    return rows or _jsonl(_inside(tdir, by_call.RELAY, tag + ".jsonl"))


def _config(job_dir: str) -> dict:
    try:
        with open(_inside(job_dir, "config.json"), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return {}
    return got.get("standard") if isinstance(got, dict) and isinstance(got.get("standard"), dict) else {}


def source(job_dir: str, trial: str, call: int) -> Source:
    """Call ``call`` (its ``n`` in the call record) of ``trial`` in ``job_dir``, as it was sent. Raises
    :class:`NotFound` for a missing job, trial, record or call, and :class:`NotWhole` for a request the record
    does not hold whole."""
    tdir = trial_dir(job_dir, trial)
    folder, tag = _store(tdir)
    try:
        got = exchanges.check(folder, tag, call)
    except exchanges.Later as e:
        raise NotWhole(str(e))
    except exchanges.Missing as e:
        raise NotFound(str(e))
    if got["request"] is None or not got["same"]:
        said = (f"sha256 {got['sha256']}, not the recorded {got['sent_sha256']}: " if got["sha256"] else "") + str(got["why"])
        raise NotWhole(f"call {call} of {trial} cannot be sent again as it was: {said}")
    line = next(x for x in exchanges.lines(folder, tag) if x.get("n") == call)
    try:
        req = json.loads(got["request"])
    except ValueError:
        req = None
    if not isinstance(req, dict) or _dumps(req) != got["request"]:
        raise NotWhole(f"call {call} of {trial} was sent as a body that is not a JSON object: it is kept, not replayed")
    cfg = _config(job_dir)
    rows = ((cfg.get("prices") or {}).get("models") or {}) if isinstance(cfg.get("prices"), dict) else {}
    record = next((r for r in _record_rows(tdir, tag) if r.get("n") == call), {})
    return Source(job_dir=os.path.abspath(job_dir), trial=trial, trial_dir=os.path.abspath(tdir), route=tag, call=call,
                  path=str(line.get("path") or ""), request=req, sent_sha256=got["sent_sha256"],
                  headers={str(k): str(v) for k, v in (line.get("headers") or {}).items()},
                  reply=line.get("reply") if isinstance(line.get("reply"), dict) else {},
                  provider=cfg.get("provider") if isinstance(cfg.get("provider"), str) else None,
                  served_by={m: list(s) for m, s in (cfg.get("served_by") or {}).items()} if isinstance(cfg.get("served_by"), dict) else {},
                  prices={m: {k: v for k, v in r.items() if k in price_list.RATES or k == "max_output"}
                          for m, r in rows.items() if isinstance(r, dict)},
                  record=record)


def recorded(job_dir: str, trial: str) -> Dict[int, dict]:
    """Each call's recorded reply, read as :func:`answer` reads one (text, reasoning, tool calls, the body as it
    arrived), by its ``n``: the requests are not rebuilt."""
    folder, tag = _store(trial_dir(job_dir, trial))
    try:
        lines = exchanges.lines(folder, tag)
    except exchanges.Later as e:
        raise NotWhole(str(e))
    return {line["n"]: answer_of(str(line.get("path") or ""), line.get("reply")) for line in lines if isinstance(line.get("n"), int)}


def answer_of(path: str, reply) -> dict:
    """A reply as a record keeps it (``status``, ``streamed``, ``body``), read as :func:`answer` reads one."""
    reply = reply if isinstance(reply, dict) else {}
    return answer(path, int(reply.get("status") or 0), str(reply.get("body") or "").encode("utf-8"), bool(reply.get("streamed")))


def calls(job_dir: str, trial: str, rule: Optional[Callable[[dict], str]] = None) -> List[dict]:
    """What a trial holds that can be sent again: each call of its exchange record, with ``n``, its path, the
    model, the reply's status, the request's size in bytes, ``whole`` (it rebuilds to its recorded sha256) and,
    when not, why; the call record's cost and tokens beside. With ``rule``, each call's ``label``: what the rule
    reads in its recorded answer."""
    if rule is not None:
        answers = recorded(job_dir, trial)
    tdir = trial_dir(job_dir, trial)
    folder, tag = _store(tdir)
    try:
        lines = exchanges.lines(folder, tag)
        parts = exchanges.stored_parts(folder)
    except exchanges.Later as e:
        raise NotWhole(str(e))
    records = {r.get("n"): r for r in _record_rows(tdir, tag) if isinstance(r.get("n"), int)}
    out = []
    for line in lines:
        n = line.get("n")
        if not isinstance(n, int):
            continue
        rec = records.get(n) or {}
        usage = ((rec.get("reply") or {}).get("usage") or {}) if isinstance(rec.get("reply"), dict) else {}
        row = {"n": n, "path": line.get("path"), "model": (line.get("sent") or {}).get("model") if isinstance(line.get("sent"), dict) else rec.get("model"),
               "status": (line.get("reply") or {}).get("status") if isinstance(line.get("reply"), dict) else None,
               "side": bool(rec.get("side")), "bytes": None, "whole": False, "why": None,
               "cost_usd": rec.get("cost_usd"), "input_tokens": usage.get("input"), "output_tokens": usage.get("output")}
        try:
            raw = exchanges.rebuild(folder, line, parts)
            row["bytes"] = len(raw)
            row["whole"] = _sha(raw) == line.get("sent_sha256")
            if not row["whole"]:
                row["why"] = ("the relay's key was cut out of the request" if line.get("key_cut")
                              else "the store's parts do not rebuild the bytes sent")
        except exchanges.Missing as e:
            row["why"] = str(e)
        if rule is not None:
            row["label"] = rule(answers.get(n) or {"status": None})
        out.append(row)
    return out


# -- changes -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Change:
    """A named change of the request: ``apply`` takes a copy and returns the changed request. ``args`` is what
    the record keeps of it; ``provider``, another provider to send to."""
    name: str
    args: dict
    apply: Callable[[dict], dict]
    provider: Optional[str] = None


def _system_slots(req: dict) -> List[tuple]:
    """(container, key) of each system text: ``system``, ``instructions``, a chat system message's content."""
    out = []
    for k in ("system", "instructions"):
        v = req.get(k)
        if isinstance(v, str):
            out.append((req, k))
        elif isinstance(v, list):
            out += [(b, "text") for b in v if isinstance(b, dict) and isinstance(b.get("text"), str)]
    for m in req.get("messages") or req.get("input") or []:
        if isinstance(m, dict) and m.get("role") in ("system", "developer"):
            out += _text_slots(m)
    return out


def _text_slots(v) -> List[tuple]:
    """(container, key) of each text of a message or an item: ``text``, a string ``content`` or ``output``."""
    out = []
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, str) and k in ("text", "content", "output") and not x.startswith("data:"):
                out.append((v, k))
            elif isinstance(x, (dict, list)) and k not in ("source", "image_url"):
                out += _text_slots(x)
    elif isinstance(v, list):
        for x in v:
            out += _text_slots(x)
    return out


def _texts(req: dict) -> List[tuple]:
    seen, out = set(), []
    inputs = [(req, "input")] if isinstance(req.get("input"), str) else []
    for c, k in inputs + _system_slots(req) + _text_slots(req.get("messages") or req.get("input") or []):
        if (id(c), k) not in seen:
            seen.add((id(c), k))
            out.append((c, k))
    return out


def replace_text(old: str, new: str) -> Change:
    """``old`` replaced by ``new`` in the system text or a message: it must appear exactly once in them."""
    if not old:
        raise Refused("a text to replace must not be empty")

    def apply(req):
        found = [(c, k) for c, k in _texts(req) if old in c[k]]
        times = sum(c[k].count(old) for c, k in found)
        if times != 1:
            raise Refused(f"the text to replace ({old[:60]!r}) appears {times} times in the system text and the "
                          "messages: it must appear exactly once")
        c, k = found[0]
        c[k] = c[k].replace(old, new)
        return req
    return Change("replace_text", {"old": old, "new": new}, apply)


def system_text(text: str) -> Change:
    """The system text replaced whole by ``text``: ``system`` or ``instructions`` as one string, or a chat
    request's system message's content."""
    def apply(req):
        for k in ("system", "instructions"):
            if k in req and isinstance(req[k], (str, list)):
                req[k] = text
                return req
        for m in req.get("messages") or req.get("input") or []:
            if isinstance(m, dict) and m.get("role") in ("system", "developer"):
                m["content"] = text
                return req
        raise Refused("the request holds no system text to replace")
    return Change("system_text", {"text": text}, apply)


def _tool_name(t) -> Optional[str]:
    if not isinstance(t, dict):
        return None
    return t["function"].get("name") if isinstance(t.get("function"), dict) else t.get("name")


def tool_description(name: str, text: str) -> Change:
    """The description of the tool named ``name`` replaced by ``text``."""
    def apply(req):
        found = [t for t in req.get("tools") or [] if _tool_name(t) == name]
        if len(found) != 1:
            names = ", ".join(str(_tool_name(t)) for t in req.get("tools") or []) or "none"
            raise Refused(f"the request holds {len(found)} tools named {name!r} (its tools: {names})")
        t = found[0]
        (t["function"] if isinstance(t.get("function"), dict) else t)["description"] = text
        return req
    return Change("tool_description", {"tool": name, "text": text}, apply)


def tools(defs: list) -> Change:
    """The tools replaced whole by ``defs``, a list of tool definitions in the request's own form."""
    if not isinstance(defs, list):
        raise Refused("the tools are a JSON list of tool definitions")

    def apply(req):
        req["tools"] = copy.deepcopy(defs)
        return req
    return Change("tools", {"tools": [_tool_name(t) for t in defs], "sha256": _sha(json.dumps(defs).encode())}, apply)


def drop_last(k: int) -> Change:
    """The last ``k`` messages (or input items) left out."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise Refused(f"messages to drop: a whole number, at least 1 (not {k!r})")

    def apply(req):
        field_ = "messages" if isinstance(req.get("messages"), list) else "input"
        items = req.get(field_)
        if not isinstance(items, list) or len(items) <= k:
            raise Refused(f"the request holds {len(items) if isinstance(items, list) else 0} messages: dropping {k} leaves none")
        req[field_] = items[:-k]
        return req
    return Change("drop_last", {"messages": k}, apply)


def set_field(name: str, value) -> Change:
    """A top-level parameter of the request set to ``value`` (``temperature``, ``max_tokens``); None removes it.
    The model, the pin and the parts have changes of their own."""
    if name in ("model", "provider") or name in exchanges.PART_FIELDS or not name:
        raise Refused(f"{name!r} is not set by a field: the model, the pin, the system text, the tools and the messages "
                      "have changes of their own")

    def apply(req):
        if value is None:
            req.pop(name, None)
        else:
            req[name] = copy.deepcopy(value)
        return req
    return Change("set_field", {"field": name, "value": value}, apply)


def model(name: str) -> Change:
    """Another model. A pin names serving providers for one model: the request's is left out."""
    if not name:
        raise Refused("a model's name must not be empty")

    def apply(req):
        req["model"] = name
        req.pop("provider", None)
        return req
    return Change("model", {"model": name}, apply)


def served_by(slugs) -> Change:
    """Another pin: only these serving providers, in this order (OpenRouter's routing); none, no pin."""
    slugs = list(slugs or [])
    bad = relay.serving_problems({"model": slugs}) if slugs else []
    if bad:
        raise Refused("served by: " + "; ".join(b.replace("model: ", "", 1) for b in bad))

    def apply(req):
        if slugs:
            req["provider"] = relay.routing(slugs)
        else:
            req.pop("provider", None)
        return req
    return Change("served_by", {"served_by": slugs}, apply)


def provider(name: str) -> Change:
    """Another provider (``providers.PROVIDERS``). One that does not route a model to several serving providers
    takes no pin: the request's is left out."""
    if name not in providers.PROVIDERS:
        raise Refused(f"provider {name!r} is not one of {', '.join(providers.PROVIDERS)}")

    def apply(req):
        if name != "openrouter":
            req.pop("provider", None)
        return req
    return Change("provider", {"provider": name}, apply, provider=name)


def _child_env(env, drop=()) -> dict:
    """The environment a user's command runs in: this one less every provider key and every value ``drop`` holds."""
    env = env if env is not None else os.environ
    keys = {providers.key_setting(p) for p in providers.PROVIDERS} | {"ANTHROPIC_AUTH_TOKEN", "API_KEY_TYPESAFE"}
    secret = {d for d in drop if d} | {v for k, v in env.items() if v and (k in keys or k.endswith("_API_KEY"))}
    return {k: v for k, v in env.items()
            if k not in keys and not k.endswith("_API_KEY") and v not in secret}


def through(command: str, env=None) -> Change:
    """The request passed through a command: it reads the request's JSON on stdin and prints the request to send
    on stdout (another version of an agent's request builder). It runs without any provider key."""
    def apply(req):
        try:
            got = subprocess.run(command, shell=True, input=_dumps(req), capture_output=True, timeout=COMMAND_SECONDS,
                                 env=_child_env(env))
        except subprocess.TimeoutExpired:
            raise Refused(f"the command {command!r} did not answer in {COMMAND_SECONDS} s")
        if got.returncode != 0:
            raise Refused(f"the command {command!r} exited {got.returncode}: "
                          + got.stderr.decode("utf-8", "replace").strip()[-300:])
        try:
            out = json.loads(got.stdout)
        except ValueError:
            out = None
        if not isinstance(out, dict):
            raise Refused(f"the command {command!r} printed no JSON object: a request is one")
        return out
    return Change("through", {"command": command}, apply)


# -- reading an answer ---------------------------------------------------------------------------------------
Rule = Callable[[dict], str]


def _read_rule(rule: Rule, a: dict, keys) -> str:
    try:
        return exchanges._cut_key(rule(exchanges._cut_key(a, keys)), keys)
    except Exception as e:
        raise Refused("reading rule failed: " + exchanges._cut_key(str(e), keys)) from None


def answer(path: str, status: int, body: bytes, streamed: bool) -> dict:
    """A reply read whole: text, reasoning, tool calls with their arguments, usage, who served it, how it ended,
    an error; ``status`` as it came."""
    out = {"status": status, "streamed": streamed, "text": "", "reasoning": "", "tool_calls": [], "usage": None}
    if status == 200:
        try:
            got = relay.read_reply(path, body, streamed, reasoning=True, whole=True)
        except Exception:
            got = {}
        out.update({"text": got.get("text") or "", "reasoning": got.get("reasoning") or "",
                    "tool_calls": [{"name": str(c.get("name") or ""), "arguments": c.get("arguments")}
                                   for c in got.get("tool_calls") or [] if isinstance(c, dict)],
                    "usage": got.get("usage")})
        out.update(relay.reply_facts(path, body, streamed))
    else:
        out["error"] = relay._said(status, body)
    out["body"] = body.decode("utf-8", "replace")
    return out


def _canonical(arguments) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return arguments
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def first_tool_call(a: dict) -> str:
    """The default rule: the first tool call's name and its arguments as canonical JSON; ``no call`` for an
    answer without one; ``error: ...`` for a failed one."""
    if a.get("status") != 200:
        return f"error: HTTP {a.get('status')}"
    if a.get("error"):
        e = a["error"]
        return "error: " + str(e.get("code") or e.get("type") or e.get("message") if isinstance(e, dict) else e)[:80]
    if not a.get("tool_calls"):
        return "no call"
    c = a["tool_calls"][0]
    return f"{c['name']} {_canonical(c.get('arguments'))}"


first_tool_call.rule = {"name": "first tool call", "reads": "the first tool call's name and arguments, as canonical JSON"}


READ_FIELDS = ("status", "text", "reasoning", "tool_calls", "body")     # what a rule's command reads of an answer


def command_rule(command: str, env=None) -> Rule:
    """A rule by a command: it reads one answer on stdin, a JSON object of :data:`READ_FIELDS` (the text, the
    reasoning and the tool calls as read, whatever the protocol or a stream; ``body``, the reply as it arrived),
    and prints its label (its first line counts). It runs without any provider key. A failed answer is its
    error, as the default rule says it."""
    def rule(a: dict) -> str:
        if a.get("status") != 200:
            return first_tool_call(a)
        given = json.dumps({k: a.get(k) for k in READ_FIELDS}, ensure_ascii=False).encode()
        try:
            got = subprocess.run(command, shell=True, input=given, capture_output=True,
                                 timeout=COMMAND_SECONDS, env=_child_env(env))
        except subprocess.TimeoutExpired:
            return "read failed: no label in time"
        if got.returncode != 0:
            return f"read failed: exit {got.returncode}"
        lines = got.stdout.decode("utf-8", "replace").strip().splitlines()
        return lines[0].strip() if lines else "no label"
    rule.rule = {"name": "command", "command": command}
    return rule


def _tool_of(label: str) -> str:
    return label.split(" ", 1)[0] if label and not label.startswith(("error:", "no call", "read failed", "no label")) else label


def count(labels: List[str], recorded: Optional[str] = None, by_tool: bool = True) -> List[dict]:
    """The distinct answers, most frequent first: ``label``, ``count`` and whether it is the recorded answer's.
    ``by_tool`` (for the default rule's labels): grouped by tool then arguments, each with its ``tool``."""
    got: Dict[str, int] = {}
    for x in labels:
        got[x] = got.get(x, 0) + 1
    if recorded is not None:
        got.setdefault(recorded, 0)
    if not by_tool:
        return [{"label": x, "count": got[x], "recorded": x == recorded} for x in sorted(got, key=lambda x: (-got[x], x))]
    tool_n: Dict[str, int] = {}
    for x, n in got.items():
        tool_n[_tool_of(x)] = tool_n.get(_tool_of(x), 0) + n
    order = sorted(got, key=lambda x: (-tool_n[_tool_of(x)], _tool_of(x), -got[x], x))
    return [{"label": x, "tool": _tool_of(x), "count": got[x], "recorded": x == recorded} for x in order]


# -- prepared: what would be sent ----------------------------------------------------------------------------
def protocol(path: str) -> str:
    """The protocol a recorded path speaks: ``messages``, ``responses`` or ``chat``."""
    p = path.split("?")[0].rstrip("/")
    return "messages" if p.endswith("/messages") else "responses" if p.endswith("/responses") else "chat"


def _stored_conversation(req: dict) -> Optional[str]:
    for k in _STORED:
        if req.get(k) not in (None, "", {}):
            return k
    items = req.get("input")
    if isinstance(items, list) and any(isinstance(i, dict) and i.get("type") == "item_reference" for i in items):
        return "item_reference"
    return None


@dataclass
class Prepared:
    """The request a replay sends, and where: what :func:`send` takes and a dry run prints."""
    source: Source
    request: dict
    raw: bytes
    sha256: str
    changes: List[dict]
    model: str
    provider: str
    endpoint: str
    key_setting: str
    key_header: str
    serving: Dict[str, List[str]]
    price: dict
    estimate_usd: float                          # one call, as the ledger books it

    def per_call_usd(self) -> float:
        """One call's estimate, no upper bound: the ledger's booking and, for a request sent unchanged, at least
        what the recorded call cost."""
        cost = self.source.record.get("cost_usd")
        if self.sha256 == self.source.sent_sha256 and isinstance(cost, (int, float)) and not isinstance(cost, bool):
            return max(self.estimate_usd, float(cost))
        return self.estimate_usd

    def summary(self, times: int = 1, max_usd: Optional[float] = None) -> dict:
        """What would be sent, its estimated cost K times (:meth:`per_call_usd`), and against the cap: nothing of
        the key but its name."""
        rec = self.source.record
        recorded_cost = rec.get("cost_usd") if isinstance(rec.get("cost_usd"), (int, float)) else None
        per = self.per_call_usd()
        return {"source": {"job_dir": self.source.job_dir, "trial": self.source.trial, "route": self.source.route,
                           "call": self.source.call, "path": self.source.path, "sent_sha256": self.source.sent_sha256,
                           "model": self.source.model, "provider": self.source.provider, "cost_usd": recorded_cost},
                "changes": self.changes,
                "request": {"sha256": self.sha256, "bytes": len(self.raw), "unchanged": self.sha256 == self.source.sent_sha256,
                            "path": self.source.path, "model": self.model, "provider": self.provider, "endpoint": self.endpoint,
                            "served_by": self.serving.get(self.model), "key_setting": self.key_setting,
                            "key_header": self.key_header, "fields": relay.request_fields(self.request)},
                "price": self.price, "times": times, "max_usd": max_usd,
                "estimate_usd": {"per_call": per, "all": per * times, "recorded_call": recorded_cost, "upper_bound": False},
                "fits": None if max_usd is None else per * times <= max_usd}


def _relay_fields(req: dict, path: str) -> None:
    if req.get("stream") and path.split("?")[0].endswith("/chat/completions"):
        options = req.setdefault("stream_options", {})
        if not isinstance(options, dict):
            raise Refused("stream_options must be a JSON object for a streamed chat request")
        options["include_usage"] = True


def prepare(src: Source, changes: List[Change] = (), *, prices: Optional[Dict[str, dict]] = None,
            endpoint: Optional[str] = None, env=None) -> Prepared:
    """``src``'s request with ``changes`` applied in order, each recorded with the request's sha256 before and
    after. ``prices``: rows by model over the job's own and, for a model the job did not price, the user's list.
    ``endpoint``: the provider's base URL instead of its record's (a local proxy). Sends nothing and reads no key:
    a dry run. Raises :class:`Refused` for a change that does not apply, a stored conversation, a provider that
    does not speak the recorded protocol, or a model with no price."""
    env = os.environ if env is None else env
    req, done = copy.deepcopy(src.request), []
    before = src.sent_sha256
    chosen = src.provider
    for c in changes:
        req = c.apply(copy.deepcopy(req))
        if not isinstance(req, dict):
            raise Refused(f"the change {c.name} gave no request")
        _relay_fields(req, src.path)
        chosen = c.provider or chosen
        after = _sha(_dumps(req))
        done.append({"name": c.name, "args": c.args, "sha256_before": before, "sha256_after": after})
        before = after
    stored = _stored_conversation(req)
    if stored:
        raise Refused(f"the request names a conversation stored at the provider ({stored}): what the provider holds is "
                      "not in the record, so it cannot be sent again as it was")
    name = req.get("model")
    if not isinstance(name, str) or not name:
        raise Refused("the request names no model")
    if not chosen:
        raise Refused("the job does not say its provider: give one (--provider)")
    if chosen not in providers.PROVIDERS:
        raise Refused(f"provider {chosen!r} is not one of {', '.join(providers.PROVIDERS)}")
    proto = protocol(src.path)
    rec = providers.record(chosen, env)
    if chosen != src.provider and providers.serves(rec, proto) is False:
        raise Refused(f"{rec['name']} does not serve {providers.PROTOCOLS[proto]['label']}, the protocol of the recorded call")
    base = endpoint or rec["base"]
    if not base:
        raise Refused(f"{rec['name']} has no endpoint set (Settings)")
    problem = settings.endpoint_url_problem(base)
    if problem:
        raise Refused(f"endpoint: {problem}")
    row = (prices or {}).get(name) or src.prices.get(name)
    if not row:
        row = price_list.table().get(name)
    if not row:
        raise Refused(f"no price for the model {name!r}: a replay is capped, and sends no call it cannot price: "
                      f"--price {name}=INPUT,OUTPUT, or nullius prices set")
    row = {k: v for k, v in row.items() if k in price_list.RATES or k == "max_output"}
    p = req.get("provider")
    serving = ({name: list(p["order"])} if isinstance(p, dict) and set(p) == {"order", "allow_fallbacks"}
               and p.get("allow_fallbacks") is False and isinstance(p.get("order"), list) and p["order"] else {})
    _relay_fields(req, src.path)
    raw = _dumps(req)
    usage = ((src.record.get("reply") or {}).get("usage") or {}) if isinstance(src.record.get("reply"), dict) else {}
    seen = usage.get("output") if isinstance(usage.get("output"), int) and usage.get("output") > 0 else None
    return Prepared(source=src, request=req, raw=raw, sha256=_sha(raw), changes=done, model=name, provider=chosen,
                    endpoint=base, key_setting=rec["key"], key_header=providers.PROTOCOLS[proto]["auth"],
                    serving=serving, price=row, estimate_usd=ledgers.bound(req, raw, row, src.path, seen))


# -- sending -------------------------------------------------------------------------------------------------
def default_out(src: Source, root: str = "replays") -> str:
    """``replays/<job>/<trial>/call-<N>__<time>`` under ``root``, in the invoking directory by default."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d__%H-%M-%S")
    return os.path.join(root, os.path.basename(os.path.normpath(src.job_dir)), src.trial, f"call-{src.call}__{stamp}")


def _check_out(out: str, src: Source) -> str:
    out = os.path.abspath(out)
    real, job = os.path.realpath(out), os.path.realpath(src.job_dir)
    if os.path.commonpath((real, job)) == job:
        raise Refused(f"{out} is inside the job's folder: a replay writes nothing in a run's tree")
    if os.path.exists(out) and (not os.path.isdir(out) or os.listdir(out)):
        raise Refused(f"{out} already holds files: a replay's folder is its own")
    return out


def _write_json(path: str, doc: dict) -> None:
    part = path + ".part"
    with open(part, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(part, path)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def post(rl: relay.Relay, prep: Prepared, tag: str) -> tuple:
    """``prep`` sent once through the started relay ``rl`` on its route ``tag``: (the answer read whole, as
    :func:`answer` gives it, with the route, the seconds, the relay's refusal and the cost; the relay's record of
    the call). The answer's ``sent_sha256`` is the relay's own record of the bytes it sent."""
    t0 = time.time()
    host, port = rl.server.server_address[:2]
    headers = {k: v for k, v in prep.source.headers.items() if k.lower() not in ("content-length", "host")}
    conn = http.client.HTTPConnection(host if host not in ("0.0.0.0", "") else "127.0.0.1", port, timeout=relay.TIMEOUT + 60)
    try:
        conn.request("POST", f"/{tag}/v1{prep.source.path}", body=prep.raw, headers=headers)
        r = conn.getresponse()
        body, status = r.read(), r.status
        refused, kind = r.getheader("X-Nullius-Refused"), r.getheader("Content-Type") or ""
    except (OSError, http.client.HTTPException) as e:
        body, status, refused, kind = json.dumps({"error": {"message": f"the replay's relay: {type(e).__name__}"}}).encode(), 502, None, ""
    finally:
        conn.close()
    a = answer(prep.source.path, status, body, "text/event-stream" in kind)
    rec = next(iter(rl.calls(tag)[-1:]), {})
    a.update({"route": tag, "seconds": round(time.time() - t0, 3), "refused": refused,
              "cost_usd": rec.get("cost_usd"), "cost_from": rec.get("cost_from")})
    line = next(iter(exchanges.lines(os.path.join(rl.dir, exchanges.FOLDER), tag)), {})
    a["sent_sha256"] = line.get("sent_sha256")
    return a, rec


def send(prep: Prepared, times: int, max_usd: float, out: str, *, rule: Optional[Rule] = None, concurrency: int = 1,
         env=None, say: Optional[Callable[[str], None]] = None) -> dict:
    """``prep`` sent ``times`` times under a spending cap of ``max_usd``, through a relay of its own, its answers
    read by ``rule`` (:func:`first_tool_call` by default) and counted; everything kept in the folder ``out``
    (outside the run's tree, new or empty). ``concurrency``: sends in flight at once. The key is read from
    ``env`` (the process environment by default) by the provider's setting name. The sends stop at the first
    one the cap refuses, or that the provider refuses for its key. Returns the document written to
    ``replay.json``."""
    from nullius.standard.runner import run as runs                # its key and endpoint checks, as a run makes them
    env = os.environ if env is None else env
    if isinstance(times, bool) or not isinstance(times, int) or not 1 <= times <= MAX_TIMES:
        raise Refused(f"times: a whole number from 1 to {MAX_TIMES} (not {times!r})")
    try:
        cap = float(max_usd)
    except (TypeError, ValueError):
        cap = 0.0
    if not 0 < cap < float("inf"):
        raise Refused(f"max_usd {max_usd!r}: the replay's spending cap in USD, above zero")
    concurrency = max(1, min(int(concurrency or 1), times))
    rule = rule or first_tool_call
    try:
        _, key = runs.provider_endpoint(prep.provider, env)
    except runs.Refused as e:
        raise Refused(str(e))
    out = _check_out(out, prep.source)
    os.makedirs(os.path.join(out, "ledger"), exist_ok=True)
    with open(os.path.join(out, "request.json"), "wb") as f:
        f.write(prep.raw)
    recorded = _read_rule(rule, answer_of(prep.source.path, prep.source.reply), (key,))
    doc = {"schema": SCHEMA, **prep.summary(times, cap), "request_file": "request.json", "concurrency": concurrency,
           "rule": dict(getattr(rule, "rule", None) or {"name": getattr(rule, "__name__", "rule")}),
           "recorded_answer": recorded, "state": "sending", "started": _now()}
    doc["request"].pop("fields", None)
    doc = exchanges._cut_key(doc, (key,))
    _write_json(os.path.join(out, "replay.json"), doc)  # the rule is kept before the first send
    led = ledgers.Ledger.create(os.path.join(out, "ledger", "ledger.json"), cap)
    rl = relay.Relay(prep.endpoint, key, os.path.join(out, "relay"), prices={prep.model: prep.price}, model=prep.model,
                     key_header=prep.key_header, ledger=led.path, serving=prep.serving or None, exchanges="whole").start()
    width = max(3, len(str(times)))
    stop, why, lock = threading.Event(), [], threading.Lock()

    def one(i: int) -> Optional[dict]:
        if stop.is_set():
            return None
        a, rec = post(rl, prep, f"r{i + 1:0{width}d}")
        a, rec = exchanges._cut_key([a, rec], (key,))
        a["send"] = i + 1
        try:
            if a["refused"]:
                stop.set()
                why.append(f"send {i + 1} was refused ({a['refused']}): " + str(rec.get("error") or ""))
            elif a["status"] in STOP_STATUS:
                stop.set()
                why.append(f"send {i + 1}: the provider refused the key (HTTP {a['status']})")
            else:
                a["label"] = _read_rule(rule, a, (key,))
        finally:
            with lock, open(os.path.join(out, "answers.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
        return a

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            got = list(pool.map(one, range(times)))
    finally:
        rl.stop()
    answers = [a for a in got if a is not None]
    labels = [a["label"] for a in answers if "label" in a]
    try:
        state = led.state()
        spend = {k: state[k] for k in ("max_usd", "spent_usd", "unsettled_usd")}
    except ledgers.Unavailable as e:
        spend = {"max_usd": cap, "error": str(e)}
    doc.update({"state": "stopped" if why else "done", "stopped": why[0] if why else None, "ended": _now(),
                "sends": {"sent": sum(1 for a in answers if not a.get("refused")),
                          "answered": sum(1 for a in answers if a.get("status") == 200),
                          "failed": sum(1 for a in answers if a.get("status") != 200 and not a.get("refused")),
                          "refused": sum(1 for a in answers if a.get("refused")), "not_sent": times - len(answers)},
                # the relay's own record of each send that went out: its bytes are the prepared request's
                "sent_as_prepared": all(a.get("sent_sha256") == prep.sha256 for a in answers if not a.get("refused")),
                "spend": spend, "counts": count(labels, recorded, by_tool=rule is first_tool_call),
                "same_as_recorded": sum(1 for x in labels if x == recorded)})
    _write_json(os.path.join(out, "replay.json"), doc)
    if say:
        say(exchanges._cut_key(f"{out}: {doc['sends']['answered']} of {times} answered, "
                              f"{ledgers.usd(spend.get('spent_usd') or 0.0)} USD spent", (key,)))
    doc["out"] = out
    return doc


def replay(job_dir: str, trial: str, call: int, times: int, max_usd: float, out: Optional[str] = None,
           changes: List[Change] = (), **kw) -> dict:
    """:func:`source`, :func:`prepare` and :func:`send` in one: call ``call`` of ``trial`` sent ``times`` times
    under ``max_usd``. ``out`` defaults to :func:`default_out`; ``kw`` goes to :func:`send` (``rule``,
    ``concurrency``, ``env``) and :func:`prepare` (``prices``, ``endpoint``)."""
    src = source(job_dir, trial, call)
    prep = prepare(src, changes, **{k: kw.pop(k) for k in ("prices", "endpoint") if k in kw}, env=kw.get("env"))
    return send(prep, times, max_usd, out or default_out(src), **kw)
