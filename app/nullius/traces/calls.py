"""A trial's transcript by model call: what each call of its agent sent that
the call before had not, the answer whole, and the session's steps that began
under it.

Read from the trial's own records: the relay's call record
(``agent/calls.jsonl``, else ``.work/relay/<unit>.jsonl``) and, when the run
kept one, the store of its exchanges (``.work/relay/exchanges``,
``nullius.standard.calls.exchanges``). Calls are in the order they started. A call
is numbered as the cap on a unit's calls counts them: each the provider
answered; a refused or failed call keeps its place, unnumbered.

- **Sent**: the parts a call holds that the call before it (on the same
  route, a side call apart) did not, in the request's order: tool results,
  pictures by their sha256, a new user message. The first call also lists the
  system text and the tools, folded. A part of the call before that this one
  no longer holds, or holds changed, is counted in one folded line; the
  answer of the call before, sent back to the model, is left out (it is that
  call's Answer).
- **Answer**: the reply read whole (``relay.read_reply``): text, reasoning,
  tool calls with their arguments, how it ended, who served it.
- **Steps**: the session's steps (``agent/trajectory.json``) that began after
  the call started and before the next one did. Steps before the first call
  are the view's ``before``.

Without the store a call has its time, tokens, cost and the answer as the
call record abridged it; its Sent is not recorded. A trial with no call record
has no calls. A call the store cannot give back (a damaged part, a picture
name that is not a digest) is said unreadable and the others are read. A store
in a later format than this code reads is not read: each call says why, as the
store refused it, and is read from its call record.

Every text a view holds went through ``redact``; a picture is its hash, type
and size, never its bytes (:func:`picture` reads one, for a page). No Flask.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from nullius.redact import redact
from nullius.standard.calls import exchanges, relay
from nullius.traces import conversation, trajectory

CALLS = os.path.join("agent", "calls.jsonl")
RELAY = os.path.join(".work", "relay")
PAGE_SIZE = conversation.PAGE_SIZE
STEPS_SHOWN = 100                                # a call's steps listed on its page; the rest counted
# the types a picture is served as: the store's four, ``image/jpg`` as JPEG
IMAGES = {"image/png": "image/png", "image/jpeg": "image/jpeg", "image/jpg": "image/jpeg",
          "image/webp": "image/webp", "image/gif": "image/gif"}
_SHA = re.compile(r"[0-9a-f]{64}")
_TRY = re.compile(r"\.try-[0-9]+$")
_IDS = ("tool_call_id", "tool_use_id", "call_id")
# what a part says besides its text: not shown as text
_NOT_TEXT = {"type", "role", "id", "name", "signature", "detail", "media_type", "cache_control", "status", *_IDS}
_LABEL = {"system": "System text", "tools": "Tools", "user": "User message", "tool result": "Tool result",
          "answer": "Assistant message", "other": "Part"}
# Responses items that are the model's own output sent back
_ANSWER_ITEMS = {"function_call", "reasoning", "custom_tool_call", "local_shell_call", "computer_call",
                 "web_search_call", "file_search_call", "code_interpreter_call", "image_generation_call", "mcp_call"}


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


def _iso(t) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(t), timezone.utc).isoformat() if t else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _number(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def store_folder(trial_dir: str) -> str:
    return os.path.join(trial_dir, RELAY, exchanges.FOLDER)


def pictures_kept(trial_dir: str) -> Optional[dict]:
    """The pictures the trial's store keeps, each sent to the model and kept once (``blobs/``): ``{count, bytes}``,
    or None when it keeps none. A folder or a file reached through a link is not read."""
    folder = os.path.join(store_folder(os.path.realpath(trial_dir)), exchanges.BLOBS)
    if os.path.realpath(folder) != folder or not os.path.isdir(folder):
        return None
    kinds, count, size = {"." + e for e in exchanges.EXT.values()}, 0, 0
    try:
        with os.scandir(folder) as found:
            for e in found:
                if os.path.splitext(e.name)[1] in kinds and e.is_file(follow_symlinks=False):
                    count, size = count + 1, size + e.stat(follow_symlinks=False).st_size
    except OSError:
        return None
    return {"count": count, "bytes": size} if count else None


def route_tag(trial_dir: str, routes: List[str]) -> str:
    """The unit's route: the end of its trial's name (an earlier try's ``.try-N`` left off), else the store's one route."""
    tag = _TRY.sub("", os.path.basename(os.path.normpath(trial_dir))).rsplit("__", 1)[-1]
    return tag if tag in routes or not routes else routes[0] if len(routes) == 1 else tag


def _record(trial_dir: str, tag: str) -> List[dict]:
    rows = _jsonl(os.path.join(trial_dir, CALLS))
    if rows or not relay._TAG.match(tag):
        return rows
    return _jsonl(os.path.join(trial_dir, RELAY, tag + ".jsonl"))


def recorded(trial_dir: str) -> int:
    """How many calls the trial's call record holds: 0 when it has none (a Harbor or imported trial, a control)."""
    folder = store_folder(trial_dir)
    return len(_record(trial_dir, route_tag(trial_dir, exchanges.tags(folder))))


def signature(trial_dir: str, trajectory_path: str = "") -> tuple:
    """What a view is read from, by size and time: a view kept for it holds while this is the same."""
    folder = store_folder(trial_dir)
    tag = route_tag(trial_dir, exchanges.tags(folder))
    out = []
    for path in (os.path.join(trial_dir, CALLS), os.path.join(trial_dir, RELAY, tag + ".jsonl"),
                 os.path.join(folder, tag + ".jsonl"), os.path.join(folder, exchanges.PARTS), trajectory_path):
        try:
            st = os.stat(path) if path else None
            out.append((path, st.st_size, st.st_mtime_ns) if st else None)
        except OSError:
            out.append(None)
    return tuple(out)


# -- the store's parts, each line on its own --------------------------------------
def _parts(folder: str) -> Dict[str, object]:
    """Each part of the store by its hash. A line that is not JSON or whose hash is not a digest is left
    out: only the calls that name it are then unreadable."""
    out = {}
    try:
        path = exchanges._inside(os.path.realpath(folder), exchanges.PARTS)
    except exchanges.Missing:
        return out
    for p in exchanges.unmarked(_jsonl(path), exchanges.PARTS):
        h = p.get("sha256")
        if isinstance(h, str) and re.fullmatch(r"[0-9a-fA-F]{64}", h) and "part" in p:
            out[h] = p["part"]
    return out


def _marks(v, found: Dict[str, str]) -> None:
    """Every picture's mark in ``v``: its hash and type, into ``found``."""
    if isinstance(v, dict):
        if set(v) == {exchanges.LITERAL}:
            for x in (v[exchanges.LITERAL] or {}).values() if isinstance(v[exchanges.LITERAL], dict) else ():
                _marks(x, found)
            return
        if set(v) == exchanges.MARK:
            if isinstance(v.get("blob"), str) and isinstance(v.get("type"), str):
                found.setdefault(v["blob"], v["type"])
            return
        for x in v.values():
            _marks(x, found)
    elif isinstance(v, list):
        for x in v:
            _marks(x, found)


def _walk(v, texts: List[str], pictures: List[dict], check, media=None) -> None:
    """A part's text and its pictures, in order. ``check(mark)``: the picture as a view holds it; it raises
    ``exchanges.Missing`` for a mark that names no digest."""
    if isinstance(v, str):
        inline = exchanges._picture(v, media, text_only=True)
        if inline:
            kind, raw, _ = inline
            h = hashlib.sha256(raw).hexdigest()
            pictures.append({"sha256": h, "type": kind, "bytes": len(raw), "stored": False})
            texts.append(f"[picture {h[:12]}]")
        else:
            texts.append(v)
    elif isinstance(v, list):
        for x in v:
            _walk(x, texts, pictures, check)
    elif isinstance(v, dict):
        if set(v) == {exchanges.LITERAL} and isinstance(v[exchanges.LITERAL], dict):
            v = v[exchanges.LITERAL]
        elif set(v) == exchanges.MARK:
            pictures.append(check(v))
            texts.append(f"[picture {str(v.get('blob'))[:12]}]")
            return
        media = v.get("media_type") if v.get("type") == "base64" and isinstance(v.get("media_type"), str) else None
        for k, x in v.items():
            if k not in _NOT_TEXT:
                _walk(x, texts, pictures, check, media if k == "data" else None)


def _kind(field: str, part) -> str:
    if field == "tools":
        return "tools"
    if field in ("system", "instructions"):
        return "system"
    if isinstance(part, str):
        return "user"
    if not isinstance(part, dict):
        return "other"
    role, typ = part.get("role"), part.get("type")
    if role in ("system", "developer"):
        return "system"
    if role == "tool" or (isinstance(typ, str) and typ.endswith("_output")):
        return "tool result"
    if role == "assistant" or typ in _ANSWER_ITEMS:
        return "answer"
    if role == "user":
        c = part.get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return "tool result"
        return "user"
    return "other"


def _tool_name(part) -> str:
    if not isinstance(part, dict):
        return ""
    f = part.get("function") if isinstance(part.get("function"), dict) else {}
    return str(part.get("name") or f.get("name") or part.get("type") or "")


def _held(line: dict) -> List[tuple]:
    """(field, hash) of each part a call's line names, in the request's order."""
    out = []
    for k, v in (line.get("sent") or {}).items():
        if k in exchanges.PART_FIELDS and isinstance(v, dict):
            if isinstance(v.get("parts"), list):
                out += [(k, h) for h in v["parts"]]
            elif isinstance(v.get("part"), str):
                out.append((k, v["part"]))
    return out


class _Reader:
    """One trial's store, read once for every call of a view."""

    def __init__(self, folder: str):
        self.folder = folder
        self.parts = _parts(folder)
        self.stored: Dict[str, bool] = {}
        self.checked: dict = {}

    def picture(self, mark: dict) -> dict:
        path = exchanges._picture_path(self.folder, mark)         # Missing: a name that is not a digest, a path outside
        h = mark["blob"]
        if h not in self.stored:
            self.stored[h] = os.path.isfile(path)
        return {"sha256": h, "type": str(mark.get("type")), "bytes": _number(mark.get("bytes")), "stored": self.stored[h]}

    def item(self, field: str, h: str) -> dict:
        exchanges._digest(h)
        if h not in self.parts:
            raise exchanges.Missing(f"the part {h} is not in the store")
        part = self.parts[h]
        kind = _kind(field, part)
        texts, pictures = [], []
        if kind == "tools":
            name = _tool_name(part)
            _walk(part, [], pictures, self.picture)
            text = exchanges._text_body(json.dumps(part, indent=1, ensure_ascii=False))
            return {"kind": kind, "sha256": h, "names": [name] if name else [], "text": text, "pictures": pictures}
        _walk(part, texts, pictures, self.picture)
        out = {"kind": kind, "sha256": h, "text": "\n".join(t for t in texts if t), "pictures": pictures}
        ids = [part.get(k) for k in _IDS if isinstance(part, dict) and isinstance(part.get(k), str)]
        if not ids and isinstance(part, dict) and isinstance(part.get("content"), list):
            ids = [b.get("tool_use_id") for b in part["content"] if isinstance(b, dict) and isinstance(b.get("tool_use_id"), str)]
        if kind == "tool result" and ids:
            out["tool_call_id"] = ids[0]
        return out

    def sent(self, line: dict, before: Optional[dict]) -> dict:
        """A call's Sent: its items, and the folded line of what the call before held that it does not."""
        held = _held(line)
        for k, h in held:
            if (k, h) not in self.checked:
                try:
                    self.checked[k, h] = self.item(k, h)
                except exchanges.Missing as e:
                    self.checked[k, h] = e
            if isinstance(self.checked[k, h], exchanges.Missing):
                raise self.checked[k, h]
        new = set(exchanges.new_parts(line, before))
        items: List[dict] = []
        if before is None:                       # the first: the system text and the tools, folded, then all of it
            tools = [self.checked[k, h] for k, h in held if k == "tools"]
            said = [self.checked[k, h] for k, h in held if k in ("messages", "input")]
            items += [self.checked[k, h] for k, h in held if k in ("system", "instructions")] + [x for x in said if x["kind"] == "system"]
            if tools:
                items.append({"kind": "tools", "names": [n for t in tools for n in t["names"]],
                              "text": "\n\n".join(t["text"] for t in tools), "pictures": [p for t in tools for p in t["pictures"]]})
            items += [x for x in said if x["kind"] != "system"]
            return {"recorded": True, "items": items, "rewritten": [], "dropped": 0}
        had = {h for _, h in _held(before)}
        msgs = [(i, h) for i, (k, h) in enumerate(held) if k in ("messages", "input")]
        last_kept = max((i for i, h in msgs if h in had), default=-1)
        rewritten, tools = [], []
        for i, (k, h) in enumerate(held):
            if h not in new:
                continue
            got = self.checked[k, h]
            if k == "tools":
                tools.append(got)
            elif k in ("messages", "input") and i < last_kept:
                rewritten.append(got)            # a part among those the call before held, changed
            elif got["kind"] != "answer":        # the call before's answer, sent back: its own Answer
                items.append(got)
        if tools:
            items.insert(0, {"kind": "tools", "names": [n for t in tools for n in t["names"]],
                             "text": "\n\n".join(t["text"] for t in tools), "pictures": [p for t in tools for p in t["pictures"]]})
        now = {h for _, h in held}
        return {"recorded": True, "items": items, "rewritten": rewritten, "dropped": len(had - now)}


def _answer_of_line(line: dict) -> Optional[dict]:
    reply = line.get("reply") if isinstance(line.get("reply"), dict) else {}
    body = reply.get("body")
    if reply.get("status") != 200 or not isinstance(body, str):
        return None
    raw, streamed, path = body.encode("utf-8", "replace"), bool(reply.get("streamed")), str(line.get("path") or "")
    try:
        got = relay.read_reply(path, raw, streamed, reasoning=True, whole=True)
    except Exception:                            # a body the reader does not know: shown as it came
        got = {"text": body}
    facts = relay.reply_facts(path, raw, streamed)
    return {"text": got.get("text") or "", "reasoning": got.get("reasoning") or "",
            "tool_calls": [{"name": str(c.get("name") or ""), "arguments": c.get("arguments") if isinstance(c.get("arguments"), str)
                            else json.dumps(c.get("arguments"))} for c in got.get("tool_calls") or [] if isinstance(c, dict)],
            "end": facts.get("end"), "served_by": facts.get("served_by"), "error": facts.get("error"), "abridged": False}


def _answer_of_record(rec: dict) -> dict:
    reply = rec.get("reply") if isinstance(rec.get("reply"), dict) else {}
    return {"text": reply.get("text") or "", "reasoning": "",
            "tool_calls": [{"name": str(c.get("name") or ""), "arguments": c.get("arguments") if isinstance(c.get("arguments"), str)
                            else json.dumps(c.get("arguments"))} for c in reply.get("tool_calls") or [] if isinstance(c, dict)],
            "end": reply.get("end"), "served_by": reply.get("served_by"), "error": reply.get("error"), "abridged": True}


def _redacted(v):
    if isinstance(v, str):
        return redact(exchanges._text_body(v))
    if isinstance(v, list):
        return [_redacted(x) for x in v]
    if isinstance(v, dict):
        return {k: _redacted(x) for k, x in v.items()}
    return v


def view(trial_dir: str, traj=None) -> dict:
    """The trial's calls: ``record`` says what they were read from (``exchanges``, ``calls`` or None),
    ``calls`` each call, ``before`` the steps before the first one, ``pictures`` the store's pictures by
    hash (their types). ``traj``: the trial's trajectory as ``trajectory.load`` reads it, for the steps."""
    folder = store_folder(trial_dir)
    tag = route_tag(trial_dir, exchanges.tags(folder))
    records = _record(trial_dir, tag)
    lines, reader, refused = {}, None, ""
    try:
        lines = {x["n"]: x for x in exchanges.lines(folder, tag) if isinstance(x.get("n"), int)} if relay._TAG.match(tag) else {}
        reader = _Reader(folder) if lines else None
    except exchanges.Later as e:                 # a later format: its calls from their record alone, each saying why
        lines, reader, refused = {}, None, str(e)
    except exchanges.Missing:
        pass
    seen = {r.get("n") for r in records}
    records += [{"n": n, "t0": x.get("t0"), "path": x.get("path"), "status": (x.get("reply") or {}).get("status")}
                for n, x in sorted(lines.items()) if n not in seen]          # a call its record lost
    order = sorted(range(len(records)), key=lambda i: (_number(records[i].get("t0")) or 0.0, i))
    pictures: Dict[str, str] = {}
    if reader is not None:
        for p in reader.parts.values():
            _marks(p, pictures)
        for x in lines.values():
            if isinstance(x.get("sent"), dict):
                _marks({k: v for k, v in x["sent"].items() if k not in exchanges.PART_FIELDS}, pictures)
    out, counted, before = [], 0, {}
    for i in order:
        rec = records[i]
        path = str(rec.get("path") or "")
        answered = rec.get("status") == 200 and bool(rec.get("model")) and not relay._free(path)
        counted += answered
        usage = ((rec.get("reply") or {}).get("usage") or {}) if isinstance(rec.get("reply"), dict) else {}
        t0, t1 = _number(rec.get("t0")), _number(rec.get("t1"))
        call = {"position": len(out), "number": counted if answered else None, "n": rec.get("n"),
                "label": f"Call {counted}" if answered else "Refused" if rec.get("refused") else
                         "Failed" if rec.get("status") != 200 else "Not counted",
                "t0": t0, "time": _iso(t0), "seconds": round(t1 - t0, 3) if t0 is not None and t1 is not None else None,
                "model": rec.get("model"), "side": bool(rec.get("side")), "path": path, "status": rec.get("status"),
                "refused": rec.get("refused"), "error": rec.get("error"),
                "tokens": {k: usage[k] for k in ("input", "output", "cached", "cache_write", "reasoning") if isinstance(usage.get(k), int)},
                "cost_usd": _number(rec.get("cost_usd")), "cost_from": rec.get("cost_from"), "steps": []}
        line = lines.get(rec.get("n")) if isinstance(rec.get("n"), int) else None
        if line is None:
            call["sent"] = ({"recorded": False, "unreadable": True, "note": "Unreadable: " + refused} if refused and not rec.get("refused")
                            else {"recorded": False, "note": "Not sent: refused before it went out" if rec.get("refused") else "Not recorded"})
            call["answer"] = _answer_of_record(rec) if rec.get("status") == 200 else None
        else:
            call["answer"] = _answer_of_line(line) or (_answer_of_record(rec) if rec.get("status") == 200 else None)
            if line.get("cut"):
                call["sent"] = {"recorded": False, "note": "Not kept: " + str(line["cut"])}
            elif "sent_body" in line:
                call["sent"] = {"recorded": True, "items": [{"kind": "other", "text": str(line["sent_body"]), "pictures": []}],
                                "rewritten": [], "dropped": 0}
            elif not isinstance(line.get("sent"), dict):
                call["sent"] = {"recorded": False, "unreadable": True,
                                "note": "Unreadable: " + str(line.get("error") or "the store holds no request for this call")}
            else:
                side = bool(rec.get("side"))
                try:
                    call["sent"] = reader.sent(line, before.get(side))
                except (exchanges.Missing, KeyError, TypeError, AttributeError) as e:
                    call["sent"] = {"recorded": False, "unreadable": True, "note": f"Unreadable: {e}"}
                before[side] = line                  # what the next one is new against: its hashes, read or not
        out.append(call)
    early = []
    starts = [c["t0"] if c["t0"] is not None else float("-inf") for c in out]
    for pos, st in enumerate(getattr(traj, "steps", None) or [] if traj is not None and not getattr(traj, "error", "") else []):
        at = trajectory.parse_ts(st.timestamp)
        if at is None:                           # a step with no clock is under no call: the flat list has it
            continue
        ref = {"position": pos, "step_id": st.step_id, "source": st.source,
               "preview": conversation._preview(st.message if st.message and st.message != "(tool use)" else
                                                ", ".join(c.get("name", "Tool") for c in st.tool_calls)) or "No message recorded"}
        k = bisect.bisect_right(starts, at.timestamp()) - 1
        (out[k]["steps"] if k >= 0 else early).append(ref)
    out = _redacted(out)
    for c in out:
        c["search"] = "\n".join(_texts_of(c)).casefold()
    return {"record": "exchanges" if lines else "calls" if records else None, "calls": out,
            "before": _redacted(early), "folder": folder, "pictures": pictures, "notice": redact(refused)}


def _texts_of(call: dict) -> List[str]:
    out = [call.get("label") or "", call.get("error") or ""]
    sent = call.get("sent") or {}
    for item in (sent.get("items") or []) + (sent.get("rewritten") or []):
        out += [item.get("text") or "", " ".join(item.get("names") or [])]
    a = call.get("answer") or {}
    out += [a.get("text") or "", a.get("reasoning") or ""]
    for c in a.get("tool_calls") or []:
        out += [c["name"], c["arguments"] or ""]
    return [x for x in out if x]


# -- a page of the view, as the conversation reader pages steps -------------------
def _item_label(item: dict) -> str:
    label = _LABEL.get(item.get("kind"), "Part")
    return label + (f" · {item['tool_call_id']}" if item.get("tool_call_id") else "")


def _preview_of(item: dict) -> str:
    if item.get("kind") == "tools":
        return ", ".join(item.get("names") or []) or "Tools"
    said = conversation._preview(re.sub(r"\[picture [0-9a-fA-F]{12}\]", "", item.get("text") or ""))
    n = len(item.get("pictures") or [])
    return said or (f"{n} picture{'s' * (n != 1)}" if n else "Nothing recorded")


def _sections(call: dict) -> List[tuple]:
    """(key, label, text, facts) of each text a call holds: its Sent, its folded line, its Answer."""
    out = []
    sent = call.get("sent") or {}
    for i, item in enumerate(sent.get("items") or []):
        out.append((f"sent-{i}", _item_label(item), item.get("text") or "", {"part": "sent"}))
    if sent.get("rewritten") or sent.get("dropped"):
        said = [f"{_item_label(x)} as sent now:\n{x.get('text') or ''}" for x in sent.get("rewritten") or []]
        out.append(("folded", "Rewritten or dropped", "\n\n".join(said) or
                    f"{sent['dropped']} part{'s' * (sent['dropped'] != 1)} of the call before not sent again.", {"part": "sent"}))
    a = call.get("answer") or {}
    if a.get("reasoning"):
        out.append(("reasoning", "Reasoning", a["reasoning"], {"part": "answer"}))
    if a.get("text"):
        out.append(("text", "Text", a["text"], {"part": "answer"}))
    for i, c in enumerate(a.get("tool_calls") or []):
        out.append((f"tool-{i}", c["name"] or "Tool call", c["arguments"] or "", {"part": "answer", "tool": c["name"]}))
    if call.get("error") and call.get("status") != 200:
        out.append(("error", "Error", call["error"], {"part": "answer"}))
    return out


def _row(call: dict) -> dict:
    a = call.get("answer") or {}
    said = ", ".join(c["name"] for c in a.get("tool_calls") or [] if c.get("name"))
    preview = conversation._preview(a.get("text")) or said or conversation._preview(call.get("error")) or "No answer recorded"
    if said and a.get("text"):
        preview = conversation._preview(f"{said} · {a['text']}")
    return {"position": call["position"], "number": call["number"], "label": call["label"], "time": call["time"],
            "status": call["status"], "tokens": call["tokens"], "cost_usd": call["cost_usd"], "preview": preview,
            "steps": len(call["steps"]), "pictures": sum(len(i.get("pictures") or []) for i in (call.get("sent") or {}).get("items") or []),
            "unreadable": bool((call.get("sent") or {}).get("unreadable"))}


def page(v: dict, *, query="", page=1, position=None, step=None, section=None, chunk=0) -> dict:
    """Twelve calls of a view, and one selected whole: its Sent, its Answer, its steps and one window of
    the text chosen (``section``, ``chunk``), as ``conversation.page`` gives a step's."""
    query = conversation._text(query).strip()[:500]
    calls = v.get("calls") or []
    result = {"view": "calls", "query": query, "kind": "calls", "page_size": PAGE_SIZE, "page": 1, "pages": 1,
              "total": len(calls), "matches": 0, "rows": [], "selected": None, "notice": "" if calls else v.get("notice") or "", "record": v.get("record")}
    needle = query.casefold()
    matching = [i for i, c in enumerate(calls) if not needle or needle in c["search"]]
    result["matches"] = len(matching)
    result["pages"] = max(1, math.ceil(len(matching) / PAGE_SIZE))
    number = min(max(1, conversation._integer(page, 1)), result["pages"])
    chosen = None
    if step is not None:
        chosen = next((i for i in matching if any(str(s["step_id"]) == str(step) for s in calls[i]["steps"])), None)
        if chosen is None:
            result["notice"] = result["notice"] or "No call in this search holds that source step."
    elif position is not None:
        wanted = conversation._integer(position, -1)
        chosen = wanted if wanted in matching else None
    if chosen is not None:
        number = matching.index(chosen) // PAGE_SIZE + 1
    elif matching:
        chosen = matching[(number - 1) * PAGE_SIZE]
    result["page"] = number
    result["rows"] = [_row(calls[i]) for i in matching[(number - 1) * PAGE_SIZE:number * PAGE_SIZE]]
    if chosen is None:
        result["notice"] = result["notice"] or "No calls match this search."
        return result
    call, at = calls[chosen], matching.index(chosen)
    sections = _sections(call)
    keys = [s[0] for s in sections]
    wanted = section if section in keys else next((k for k in keys if not k.startswith(("sent-", "folded"))), keys[0] if keys else None)
    sent = call.get("sent") or {}
    a = call.get("answer")
    result["selected"] = {
        **{k: call[k] for k in ("position", "number", "label", "time", "seconds", "model", "side", "status", "refused",
                                "tokens", "cost_usd", "cost_from")},
        "match_number": at + 1, "previous": matching[at - 1] if at else None,
        "next": matching[at + 1] if at + 1 < len(matching) else None,
        "sent": {"recorded": bool(sent.get("recorded")), "note": sent.get("note") or "", "unreadable": bool(sent.get("unreadable")),
                 "items": [{"key": f"sent-{i}", "kind": item.get("kind"), "label": _item_label(item), "preview": _preview_of(item),
                            "characters": len(item.get("text") or ""), "pictures": [dict(x) for x in item.get("pictures") or []]}
                           for i, item in enumerate(sent.get("items") or [])],
                 "folded": ({"key": "folded", "rewritten": len(sent.get("rewritten") or []), "dropped": sent.get("dropped") or 0}
                            if sent.get("rewritten") or sent.get("dropped") else None)},
        "answer": {"recorded": a is not None, "end": (a or {}).get("end"), "served_by": (a or {}).get("served_by"),
                   "error": (a or {}).get("error"), "abridged": bool((a or {}).get("abridged")),
                   "items": [{"key": k, "label": label, "preview": conversation._preview(text, 100), "characters": len(text)}
                             for k, label, text, facts in sections if facts.get("part") == "answer"]},
        "steps": call["steps"][:STEPS_SHOWN], "more_steps": max(0, len(call["steps"]) - STEPS_SHOWN),
        "before": (v.get("before") or [])[:STEPS_SHOWN] if chosen == 0 else [],
        "sections": conversation._section_index(sections),
        "content": conversation._content(sections, wanted, chunk) if sections else None}
    return result


def document(v: dict) -> dict:
    """The view as a command prints it: every call whole, the steps by their ids."""
    keep = lambda c: {k: x for k, x in c.items() if k not in ("search", "t0", "position")}
    return {"record": v.get("record"),
            "calls": [dict(keep(c), steps=[s["step_id"] for s in c["steps"]]) for c in v.get("calls") or []],
            "before": [s["step_id"] for s in v.get("before") or []]}


def picture(v: dict, sha256: str) -> tuple:
    """(bytes, media type) of a picture the trial's store lists, of one of the four image types, its bytes
    the hash they are named by. ``LookupError`` for any other: not a digest, not listed, not an image,
    not kept (a store kept as text), damaged."""
    if not isinstance(sha256, str) or not _SHA.fullmatch(sha256):
        raise LookupError("not a sha256")
    kind = (v.get("pictures") or {}).get(sha256)
    if kind not in IMAGES:
        raise LookupError("not a picture of this trial's store")
    try:
        path = exchanges._picture_path(v["folder"], {"blob": sha256, "type": kind})
        with open(path, "rb") as f:
            data = f.read()
    except (exchanges.Missing, OSError, KeyError):
        raise LookupError("not kept")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise LookupError("damaged")
    return data, IMAGES[kind]
