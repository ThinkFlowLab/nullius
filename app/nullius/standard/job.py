"""A run written as a job in Harbor's shape, so every page reads it unchanged.

``<jobs>/<job>/``: ``config.json`` (what was run: the benchmark, the agent or
capability, the model), ``result.json`` (the counts), and one folder per unit -
a trial - holding:

- ``result.json``: the unit, the agent, the timings, the verdict
  (``verifier_result.rewards``) or the exception that ended it, the tokens and
  the cost the relay recorded;
- ``agent/trajectory.json``: ATIF, built from what Nullius saw - the session's
  events over ACP (the prompt, the agent's messages and thoughts, each tool
  call with its result) or, for a capability, each request it was sent and its
  reply - with the model calls' tokens and cost from the relay;
- ``agent/trace.json``: the same run as a timeline, ``harbor-trace-v1``: each
  model call as the relay timed it, each tool call as Nullius saw it start and
  end, under the agent's turns. What the agent does inside is not seen;
- ``agent/calls.jsonl``: the relay's record of each model call, and
  ``agent/gateway.log``: one line per call with its status, where the runs
  pages count rate limits;
- ``verifier/reward.txt`` and ``verifier/score.json``: the driver's verdict
  and its detail.
"""
from __future__ import annotations

import json
import shutil
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from nullius.standard import ledger
from nullius.traces import otel, trace


def iso(t: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(t, timezone.utc).isoformat() if t else None


def _text(content) -> str:
    """ACP content: a block, a list of blocks, or a tool call's content list."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return content.get("text") or ""
        if content.get("type") == "content":
            return _text(content.get("content"))
        if content.get("type") == "diff":
            return f"diff {content.get('path')}\n{content.get('newText') or ''}"[:4000]
        return json.dumps(content)[:4000]
    if isinstance(content, list):
        return "\n".join(_text(c) for c in content)
    return str(content)


def _usage_total(calls: List[dict]) -> dict:
    ok = [c for c in calls if c.get("status") == 200]
    u = lambda k: sum(((c.get("reply") or {}).get("usage") or {}).get(k) or 0 for c in ok)
    costs = [c["cost_usd"] for c in ok if isinstance(c.get("cost_usd"), (int, float))]
    return {"input": u("input"), "output": u("output"), "cached": u("cached"), "reasoning": u("reasoning"),
            "cost": sum(costs) if costs else None,
            "cost_from": "estimate" if any(c.get("cost_from") == "estimate" for c in ok) else ("provider" if costs else None),
            "n_calls": sum(1 for c in calls if not c.get("refused"))}       # a call its cap refused was never sent


def _tool_clock(extra: dict, status: Optional[str], t: Optional[float]):
    """When a tool call began to run (announced earlier, it may still wait for the
    model's reply to end) and when it ended, for the trace."""
    if status == "in_progress":
        extra.setdefault("running_at", iso(t))
    elif status in ("completed", "failed"):
        extra["ended_at"] = iso(t)


def trajectory_from_events(events: List[dict], calls: List[dict], agent: dict, model: str, session: str) -> dict:
    """ATIF from an ACP session's events: a user step per prompt, an agent
    step per stretch of message, and one per tool call with its result as the
    observation. The relay's calls give the totals."""
    steps: List[dict] = []
    tools = {}                                       # toolCallId -> its step

    def add(source, t, **kw):
        steps.append(dict({"step_id": len(steps) + 1, "timestamp": iso(t), "source": source}, **kw))
        return steps[-1]

    open_msg = None
    for e in events:
        u, t = e.get("update") or {}, e.get("t")
        kind = u.get("sessionUpdate")
        if kind == "user_message_chunk":
            open_msg = None
            add("user", t, message=_text(u.get("content")))
        elif kind in ("agent_message_chunk", "agent_thought_chunk"):
            if open_msg is None:
                open_msg = add("agent", t, message="", model_name=model)
            if kind == "agent_message_chunk":
                open_msg["message"] += _text(u.get("content"))
            else:
                open_msg["reasoning_content"] = open_msg.get("reasoning_content", "") + _text(u.get("content"))
        elif kind == "tool_call":
            open_msg = None
            cid = u.get("toolCallId") or f"call-{len(tools) + 1}"
            st = add("agent", t, message="", model_name=model,
                     tool_calls=[{"tool_call_id": cid, "function_name": u.get("title") or u.get("kind") or "tool",
                                  "arguments": u.get("rawInput") if isinstance(u.get("rawInput"), dict) else {"input": u.get("rawInput")}}],
                     extra={"kind": u.get("kind"), "status": u.get("status")})
            tools[cid] = st
            _tool_clock(st["extra"], u.get("status"), t)
            if u.get("content") or u.get("rawOutput") is not None:
                st["observation"] = {"results": [{"source_call_id": cid, "content": _text(u.get("content")) or json.dumps(u.get("rawOutput"))[:8000]}]}
        elif kind == "tool_call_update":
            st = tools.get(u.get("toolCallId"))
            if st is None:
                continue
            if u.get("status"):
                st["extra"]["status"] = u["status"]
                _tool_clock(st["extra"], u["status"], t)
            out = _text(u.get("content")) if u.get("content") else (json.dumps(u.get("rawOutput"))[:8000] if u.get("rawOutput") is not None else "")
            if out:
                if u.get("status") == "failed" and not out.lower().startswith(("error", "failed")):
                    out = "Error: " + out
                st["observation"] = {"results": [{"source_call_id": u.get("toolCallId"), "content": out}]}
        elif kind == "permission":
            open_msg = None
            add("system", t, message=("granted" if u.get("granted") else "refused") + " permission: " +
                _text((u.get("toolCall") or {}).get("title") or ""))
        elif kind == "notice":
            # an adapter's warning, sent apart from the agent's message: the client takes notices (acp)
            open_msg = None
            add("system", t, message=f"{u.get('severity') or 'notice'}: {u.get('title') or ''}"
                + (f" - {u['description']}" if u.get("description") else ""))
        elif kind == "plan":
            add("agent", t, message="plan:\n" + "\n".join(f"- [{x.get('status')}] {x.get('content')}" for x in u.get("entries") or []),
                model_name=model)
    tot = _usage_total(calls)
    return {"schema_version": "ATIF-v1.6", "session_id": session,
            "agent": {"name": agent.get("key"), "version": str((agent.get("upstream") or {}).get("version") or ""), "model_name": model},
            "steps": steps,
            "final_metrics": {"total_prompt_tokens": tot["input"], "total_completion_tokens": tot["output"],
                              "total_cached_tokens": tot["cached"], "total_cost_usd": tot["cost"], "total_steps": len(steps)}}


REPLY_CHARS = 100_000      # a capability's reply in its trajectory: its memory and its trace, one request's


def trajectory_from_exchanges(exchanges: List[dict], calls: List[dict], capability: dict, model: str, session: str) -> dict:
    """ATIF for a capability: each request Nullius sent it (``add``, ``recall``)
    as a tool call and its reply as the observation, whole up to ``REPLY_CHARS``
    (a reply's ``trace`` is what the library did); then the benchmark's own
    model calls, from the relay, as the reader's steps."""
    steps = []
    for i, x in enumerate(exchanges):
        cid = f"{x['op']}-{i + 1}"
        steps.append({"step_id": len(steps) + 1, "timestamp": iso(x.get("t0")), "source": "agent",
                      "message": "", "tool_calls": [{"tool_call_id": cid, "function_name": f"{capability['key']}.{x['op']}",
                                                     "arguments": x.get("request") or {}}],
                      "extra": {"ended_at": iso(x.get("t1"))},
                      "observation": {"results": [{"source_call_id": cid, "content": json.dumps(x.get("reply"))[:REPLY_CHARS]}]}})
    for c in calls:
        last = ((c.get("request") or {}).get("last") or [{}])[-1]
        steps.append({"step_id": len(steps) + 1, "timestamp": iso(c.get("t0")), "source": "user", "message": last.get("content") or ""})
        reply = c.get("reply") or {}
        st = {"step_id": len(steps) + 1, "timestamp": iso(c.get("t1")), "source": "agent", "model_name": c.get("model"),
              "message": reply.get("text") if c.get("status") == 200 else "(" + (c.get("error") or "failed") + ")"}
        if reply.get("usage"):
            st["metrics"] = {"prompt_tokens": reply["usage"]["input"], "completion_tokens": reply["usage"]["output"],
                             "cached_tokens": reply["usage"]["cached"]}
            if isinstance(c.get("cost_usd"), (int, float)):
                st["metrics"]["cost_usd"] = c["cost_usd"]
        steps.append(st)
    tot = _usage_total(calls)
    return {"schema_version": "ATIF-v1.6", "session_id": session,
            "agent": {"name": capability.get("key"), "version": "", "model_name": model},
            "steps": steps,
            "final_metrics": {"total_prompt_tokens": tot["input"], "total_completion_tokens": tot["output"],
                              "total_cached_tokens": tot["cached"], "total_cost_usd": tot["cost"], "total_steps": len(steps)}}


def _control(other: dict) -> dict:
    """A control's run says so, and who wrote the control: the runs pages read
    a standard job named ``nop`` or ``oracle`` as a control only by this."""
    if other.get("_kind") != "control":
        return {}
    return {"control": {"name": other["key"], "source": other.get("source", ""), "from": other.get("from", "")}}


def _setup(other: dict) -> dict:
    """A setup's run says which one it is, the model the upstream's code was
    asked as and the one sent in its place, and whether its scores compare with
    the paper's: only the original's do."""
    if other.get("_kind") != "upstream":
        return {}
    return {"setup": {"name": other["key"], "asked": other.get("asked", ""), "sent": other.get("sent", ""),
                      "comparable_with_paper": bool(other.get("comparable"))}}


def _version(bench: dict) -> dict:
    """A benchmark with versions says which one ran (``benchmark_version``), and the
    ``key@version`` that names it; one without says nothing more than its key."""
    v = bench.get("_version") if "@" in str(bench.get("_ref") or "") else None
    return {"benchmark_version": v, "benchmark_ref": bench["_ref"]} if v else {}


def _write(path: str, text: str) -> None:
    """A trial file written as a new regular file: whatever is at the path -
    a link the agent planted in a folder it could write, above all - is
    replaced, never written through (verifier/score.json -> ~/.bashrc)."""
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isfile(path)):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def mean_score(results: List[dict]):
    """The trials' mean score as the pages read it (parse.Job.mean_score): a
    trial scored over several parts weighs as many. None when none was scored."""
    scored = []
    for r in results:
        v = r.get("verifier_result") or {}
        s = (v.get("rewards") or {}).get("score")
        if isinstance(s, (int, float)) and not isinstance(s, bool):
            scored.append((s, (v.get("parts") or {}).get("n") or 1))
    return sum(s * n for s, n in scored) / sum(n for _, n in scored) if scored else None


def _ns(stamp) -> Optional[int]:
    """A time as the trace's nanoseconds: a clock's seconds, or a step's ISO time."""
    try:
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp).timestamp()
        return int(stamp * 1e9) if stamp else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _span(span_id: str, name: str, kind: str, source: str, start: int, end: Optional[int], **fields) -> dict:
    """One span. No end: a moment when it says so (``measured``), else a call seen to start and never to end."""
    moment = fields.pop("moment", False)
    span = {"span_id": span_id, "name": name, "kind": kind, "start_ns": start, "source": source,
            "timing_quality": "measured" if end is not None or moment else "unknown", "attributes": {}, "children": [], **fields}
    if end is not None:
        span["end_ns"], span["duration_ns"] = end, end - start
    return span


def _short_title(title: str) -> str:
    """A tool's title for its row and its filter chip: each path as its last part
    (``Fetching file:///var/tmp/run/calc.py``: ``Fetching calc.py``). The whole title stays in the span's attributes."""
    return re.sub(r"[^\s'\"]+", lambda word: word.group().rsplit("/", 1)[-1] or word.group(), title)


def _call_span(n: int, c: dict) -> Optional[dict]:
    """A model call, as the relay timed it."""
    start, end = _ns(c.get("t0")), _ns(c.get("t1"))
    if start is None:
        return None
    reply = c.get("reply") or {}
    usage = reply.get("usage") or {}
    tokens = {name: usage[k] for k, name in (("input", "input_tokens"), ("output", "output_tokens"), ("cached", "cache_read_tokens"),
                                             ("reasoning", "reasoning_tokens")) if usage.get(k)}
    attributes = {k: c[k] for k in ("model", "model_asked", "path", "status", "error", "cost_usd", "cost_from") if c.get(k) not in (None, "")}
    named = [str(x.get("name")) for x in reply.get("tool_calls") or [] if isinstance(x, dict) and x.get("name")]
    if named:
        attributes["tool_calls"] = ", ".join(named)
    last = ((c.get("request") or {}).get("last") or [{}])[-1]
    content = {"input": _text(last.get("content")) if isinstance(last, dict) else "", "output": _text(reply.get("text"))}
    # named for its model: under the name ``llm_request`` the reader would call it by what it produced, as an agent's message reads
    return _span(f"llm-{n}", str(c.get("model") or "model call"), "llm_request", "relay", start, end, tokens=tokens, content=content, attributes=attributes)


def _step_spans(n: int, st: dict, source: str) -> List[dict]:
    """What a trajectory step adds to the relay's calls: its tool call, or what the agent said at that moment."""
    at = _ns(st.get("timestamp"))
    if at is None:
        return []
    extra = st.get("extra") or {}
    if st.get("tool_calls"):
        call = st["tool_calls"][0]
        said = ((st.get("observation") or {}).get("results") or [{}])[0].get("content")
        name = str(call.get("function_name") or "tool")
        return [_span(f"tool-{n}", name, "tool", source, _ns(extra.get("running_at")) or at, _ns(extra.get("ended_at")),
                      content={"arguments": call.get("arguments"), "result": said},
                      attributes={"tool_name": _short_title(name), "title": name, **{k: extra[k] for k in ("kind", "status") if extra.get(k)}})]
    spans = []
    if st.get("reasoning_content"):
        spans.append(_span(f"reasoning-{n}", "reasoning", "reasoning", source, at, None, moment=True, content={"reasoning": st["reasoning_content"]}))
    if st.get("message"):
        kind = "system" if st.get("source") == "system" else "output"
        spans.append(_span(f"{kind}-{n}", kind, kind, source, at, None, moment=True, content={"output": st["message"]}))
    return spans


def trace_from_run(trajectory: dict, calls: List[dict], other: dict, produce: tuple) -> Optional[dict]:
    """``agent/trace.json``: the agent's window as one session, its turns in it
    (an agent's only: a capability's and a setup's user steps are the benchmark's
    own model calls), each holding the model calls and tool calls that began in
    it. Nothing recorded, no trace: a control has none to show."""
    agent = other.get("_kind") == "agent"
    source = "acp" if agent else "driver"
    steps = trajectory.get("steps") or []
    spans = [s for s in (_call_span(i + 1, c) for i, c in enumerate(calls)) if s is not None]
    turns = []
    for i, st in enumerate(steps):
        if agent and st.get("source") == "user":
            at = _ns(st.get("timestamp"))
            if at is not None:
                turns.append(_span(f"turn-{len(turns) + 1}", f"turn {len(turns) + 1}", "user_message", source, at, None, moment=True,
                                   content={"input": st.get("message")}))
        elif agent or st.get("tool_calls"):
            spans.extend(_step_spans(i + 1, st, source))
    if not spans and not turns:
        return None
    ends = lambda rows: [s["end_ns"] if "end_ns" in s else s["start_ns"] for s in rows]
    first = min(s["start_ns"] for s in spans + turns)
    begin, finish = produce if produce else (None, None)
    begin = min(_ns(begin) or first, first)
    finish = max([_ns(finish) or 0] + ends(spans + turns))
    session = _span("session", str(other.get("key") or "agent"), "session", "nullius", begin, finish)
    for span in sorted(spans, key=lambda s: s["start_ns"]):
        holder = next((t for t in reversed(turns) if t["start_ns"] <= span["start_ns"]), session)
        span["parent_span_id"] = holder["span_id"]
        holder["children"].append(span)
    for turn in turns:
        turn["parent_span_id"] = "session"
        if turn["children"]:
            end = max(ends(turn["children"]))
            turn.update(end_ns=end, duration_ns=end - turn["start_ns"])
    session["children"] = sorted(session["children"] + turns, key=lambda s: s["start_ns"])
    return {"schema_version": otel.SCHEMA, "producer": "app/nullius/standard/job.py", "trial_name": trajectory.get("session_id") or "",
            "session_id": trajectory.get("session_id") or "", "agent": trajectory.get("agent") or {},
            "clock": {"source": "this machine's clock, as the relay and the ACP client saw each call",
                      "note": "wall_ns is the agent's window; a tool call's time is what the agent reported"},
            "spans": [session], "totals": trace.breakdown([session], finish - begin).as_totals()}


def _jsonl(path: str) -> List[dict]:
    with open(path, errors="replace") as f:
        return [json.loads(line) for line in f if line.strip()]


def trial_dirs(root: str) -> List[str]:
    """The trials under a job, or under a folder of jobs. Two levels, no deeper: a trial holds its agent's own files."""
    jobs = [root] if os.path.isfile(os.path.join(root, "config.json")) else [e.path for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    return sorted(e.path for j in jobs for e in os.scandir(j) if e.is_dir(follow_symlinks=False) and os.path.isfile(os.path.join(e.path, "result.json")))


def backfill_trace(trial_dir: str, write: bool = False) -> str:
    """Give a standard trial written before traces its ``agent/trace.json``, from
    the files it kept. Says which: ``full`` (it kept its ACP events: tool calls
    with their times), ``partial`` (none kept: the relay's model calls, and any
    tool call at its start, with no duration), ``empty`` (nothing recorded),
    ``control``, ``has_trace`` (never replaced: a capture cannot be taken again),
    or ``not_standard``."""
    with open(os.path.join(trial_dir, "result.json")) as f:
        res = json.load(f)
    if "standard" not in res:
        return "not_standard"
    agent_dir = os.path.join(trial_dir, "agent")
    target = os.path.join(agent_dir, "trace.json")
    if os.path.lexists(target):
        return "has_trace"
    if "control" in res["standard"]:
        return "control"
    with open(os.path.join(os.path.dirname(trial_dir), "config.json")) as f:
        kind = (json.load(f).get("standard") or {}).get("kind")
    info = res.get("agent_info") or {}
    other = {"key": info.get("name"), "_kind": kind, "upstream": {"version": info.get("version")}}
    calls_at = os.path.join(agent_dir, "calls.jsonl")
    calls = _jsonl(calls_at) if os.path.isfile(calls_at) else []
    session = f"{res['standard'].get('job') or ''}/{res['standard'].get('unit') or ''}"
    events_at, kept_at = os.path.join(agent_dir, "acp.jsonl"), os.path.join(agent_dir, "trajectory.json")
    full = kind == "agent" and os.path.isfile(events_at)
    if full:
        trajectory = trajectory_from_events(_jsonl(events_at), calls, other, (info.get("model_info") or {}).get("name") or "", session)
    elif os.path.isfile(kept_at):
        with open(kept_at) as f:
            trajectory = json.load(f)
    else:
        trajectory = {"session_id": session}
    window = res.get("agent_execution") or {}
    clock = lambda name: datetime.fromisoformat(window[name]).timestamp() if window.get(name) else None
    timeline = trace_from_run(trajectory, calls, other, (clock("started_at"), clock("finished_at")))
    if timeline is None:
        return "empty"
    if write:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(timeline, f, indent=1)
    return "full" if full else "partial"


def write_trial(trial_dir: str, *, job: str, bench: dict, other: dict, model: str, unit: str, t0: float, t1: float,
                produce: tuple, score: Optional[dict], exception: Optional[tuple], calls: List[dict],
                trajectory: Optional[dict], extra_logs: Optional[dict] = None, environment: str = "",
                image: str = "", setup_end: Optional[float] = None, checking: tuple = (None, None)) -> dict:
    """Write one unit's trial and return its result. ``environment``: the
    benchmark's environment the unit asked for (none: the base image), and
    ``image``, the one it ran in on the docker backend - names only.
    ``setup_end``: when the unit's environment was ready and the agent began
    (its setup, relay and container from ``t0``); ``checking``: the check's
    start and end (its own step in the container, then the driver's score)."""
    os.makedirs(os.path.join(trial_dir, "agent"), exist_ok=True)
    os.makedirs(os.path.join(trial_dir, "verifier"), exist_ok=True)
    tot = _usage_total(calls)
    # the pages read a task's name after its last slash: a unit's own slashes
    # (a unit id like "set/12") would leave only its last part
    res = {"task_name": f"{bench['key']}/{unit.replace('/', '-')}","trial_name": os.path.basename(trial_dir), "source": bench["key"],
           "started_at": iso(t0), "finished_at": iso(t1),
           "agent_info": {"name": other["key"], "version": str((other.get("upstream") or {}).get("version") or ""),
                          "model_info": {"name": model}},
           "environment_setup": {"started_at": iso(t0), "finished_at": iso(setup_end)} if setup_end else None,
           "agent_execution": {"started_at": iso(produce[0]), "finished_at": iso(produce[1])} if produce and produce[0] else None,
           "verifier": {"started_at": iso(checking[0]), "finished_at": iso(checking[1])} if checking and checking[0] and checking[1] else None,
           "agent_result": {"n_input_tokens": tot["input"], "n_output_tokens": tot["output"], "n_cache_tokens": tot["cached"],
                            "cost_usd": tot["cost"], "cost_from": tot["cost_from"], "n_model_calls": tot["n_calls"]},
           "standard": {"benchmark": bench["key"], "family": bench["family"], "needs": bench["needs"], "unit": unit,
                        "plugged": other.get("_ref") or other["key"], "job": job, **_version(bench), **_control(other), **_setup(other),
                        **({"environment": environment} if environment else {}), **({"image": image} if image else {})}}
    if score is not None:
        value = float(score.get("reward") or 0.0)
        # a pass benchmark leaves a reward, as a verifier does; a score
        # benchmark a score and no pass or fail - the trial reads as Scored
        rewards = {"reward": value} if bench["score"]["kind"] == "pass" else {"score": value}
        res["verifier_result"] = {"rewards": rewards}
        parts = score.get("parts")
        if isinstance(parts, dict) and isinstance(parts.get("n"), int) and not isinstance(parts["n"], bool) and parts["n"] > 0:
            # a score over several parts (a conversation's questions): the job's mean weighs it by them
            res["verifier_result"]["parts"] = {"n": parts["n"]}
            if parts.get("by") and isinstance(parts.get("parts"), list):
                _write(os.path.join(trial_dir, "verifier", "parts.json"),
                       json.dumps({"by": parts["by"], "parts": parts["parts"]}, indent=1))
        if "reward" in rewards:
            _write(os.path.join(trial_dir, "verifier", "reward.txt"), str(value) + "\n")
        _write(os.path.join(trial_dir, "verifier", "score.json"),
               json.dumps(dict({k: v for k, v in score.items() if k != "parts"}, says=bench["score"]["says"]), indent=1))
    if exception:
        res["exception_info"] = {"exception_type": exception[0], "exception_message": exception[1][:2000]}
    _write(os.path.join(trial_dir, "agent", "calls.jsonl"), "".join(json.dumps(c) + "\n" for c in calls))
    _write(os.path.join(trial_dir, "agent", "gateway.log"),
           "".join(f"{iso(c.get('t0'))} {c.get('model')} status {c.get('status')} {c.get('error') or ''}\n" for c in calls))
    if trajectory is not None:
        _write(os.path.join(trial_dir, "agent", "trajectory.json"), json.dumps(trajectory, indent=1))
        timeline = trace_from_run(trajectory, calls, other, produce)
        if timeline is not None:
            _write(os.path.join(trial_dir, "agent", "trace.json"), json.dumps(timeline, indent=1))
    for name, text in (extra_logs or {}).items():
        _write(os.path.join(trial_dir, "agent", name), text)
    _write(os.path.join(trial_dir, "result.json"), json.dumps(res, indent=1))
    return res


def write_config(job_dir: str, *, job: str, bench: dict, other: dict, model: str, provider: str, dataset: str, units: List[str],
                 concurrency: int, granted: bool, judge: str = "", backend: str = "local", image: str = "",
                 options: Optional[dict] = None, launch: str = "", shard: Optional[dict] = None, repeat: int = 1,
                 bench_options: Optional[dict] = None,
                 simulator: str = "", prices: Optional[dict] = None):
    """``unsandboxed`` is true for an agent on the local backend only: in a
    container (``backend``, ``image``) it is not. ``standard.options`` holds
    every option's value, the defaults included, so two runs read side by side
    say what differed. ``launch`` and ``shard``: a shard of a launch run over
    several machines (``shard``), which ``merge`` reads back. ``repeat``: how
    many times each unit was run, said only when more than once. ``judge`` and
    ``simulator``: the models that graded and that played the user, when the
    benchmark has them. ``prices``: the rows its models' costs are
    estimated from and where the user's list came from, when it has any."""
    with open(os.path.join(job_dir, "config.json"), "w") as f:
        json.dump({"job_name": job, "runner": "standard", "runner_label": "Nullius",
                   "standard": {"benchmark": bench["key"], **_version(bench), "plugged": other.get("_ref") or other["key"], "kind": other["_kind"], "provider": provider,
                                "needs": bench["needs"], "permissions_granted": granted,
                                "unsandboxed": granted and backend == "local", "backend": backend,
                                **({"image": image} if image else {}), **({"judge": judge} if judge else {}),
                                **({"simulator": simulator} if simulator else {}), **({"options": options} if options else {}), **({"benchmark_options": bench_options} if bench_options else {}), **_control(other), **_setup(other),
                                **({"launch": launch} if launch else {}), **({"shard": shard} if shard else {}),
                                **({"repeat": repeat} if repeat > 1 else {}), **({"prices": prices} if prices else {})},
                   "runner_items": [bench["units"]["noun"], bench["units"]["nouns"]],
                   "n_concurrent_trials": concurrency,
                   "agents": [{"name": other["key"], "model_name": model}],
                   "datasets": [{"name": f"{bench['key']}/{dataset}", "n_tasks": len(units)}]}, f, indent=2)


#: an error kind naming the machine or the benchmark's side rather than the agent: the live board's infra_error
_INFRA = ("Setup", "Verifier", "Api", "RateLimit", "Environment", "Docker", "Build", "Connection", "Network", "Authentication",
          "ModelNotFound")


def feed_outcome(kind: str, score: Optional[dict], exception: Optional[tuple]) -> dict:
    """A unit's end in the live feed's words (``nullius.runs.live``): its
    outcome, with the reward (a pass benchmark) or the score, and the error's
    kind as evidence. A unit the run's spending cap cut off (``ledger.ERROR``)
    is the feed's ``budget``."""
    score = score or {}
    if exception:
        k = exception[0]
        return {"outcome": "cancelled" if k == "CancelledError" else "budget" if k == ledger.ERROR else "timeout" if "Timeout" in k else "infra_error" if any(w in k for w in _INFRA) else "agent_error",
                "exc": k, "evidence": (exception[1] or k)[:200]}
    if kind == "pass":
        reward = score.get("reward")
        return {"outcome": "resolved" if reward == 1.0 else "unresolved", "reward": reward, "evidence": f"reward {reward}"}
    value = score.get("score", score.get("reward"))
    return {"outcome": "scored", "score": value, "evidence": f"score {value}"}


def pass_hat(results: List[dict]) -> Optional[dict]:
    """pass^k of a run that tried each unit several times (``repeat``), as τ-bench defines it: over
    the units, the chance that k attempts drawn from its n all passed, C(c, k) / C(n, k), averaged -
    pass^1 is the pass rate; and pass@k (``at``), the chance that one of them did, as HumanEval reads it. A unit with an attempt that ended in an error, with no verdict, is left
    out, as the upstreams leave an infrastructure error out. None for a run of one attempt each."""
    from math import comb
    per: Dict[str, List[bool]] = {}
    broken = set()
    for r in results:
        u = (r.get("standard") or {}).get("unit")
        rewards = (r.get("verifier_result") or {}).get("rewards") or {}
        if u is None:
            continue
        if r.get("exception_info") or "reward" not in rewards:
            broken.add(u)
            continue
        per.setdefault(u, []).append(rewards["reward"] == 1.0)
    whole = {u: v for u, v in per.items() if u not in broken}
    n = min((len(v) for v in whole.values()), default=0)
    if n < 2:
        return None
    return {"units": len(whole), "attempts": n,
            **{str(k): round(sum(comb(sum(v[:n]), k) / comb(n, k) for v in whole.values()) / len(whole), 4) for k in range(1, n + 1)},
            # pass@k, as HumanEval's estimate_pass_at_k reads n attempts with c passed: 1 - C(n-c, k) / C(n, k), averaged
            "at": {str(k): round(sum(1 - comb(n - sum(v[:n]), k) / comb(n, k) for v in whole.values()) / len(whole), 4) for k in range(1, n + 1)}}


def write_result(job_dir: str, job: str, t0: float, t1: float, results: List[dict], planned: int, budget: Optional[dict] = None):
    """``budget``: a run with a spending cap (``run.Run.budget``): the cap,
    what was spent, whether it stopped the run, the units it cut off and
    those never begun."""
    errored = sum(1 for r in results if r.get("exception_info"))
    hat = pass_hat(results)
    with open(os.path.join(job_dir, "result.json"), "w") as f:
        json.dump({"id": job, "started_at": iso(t0), "finished_at": iso(t1), "n_total_trials": planned,
                   "stats": {"n_completed_trials": len(results) - errored, "n_errored_trials": errored,
                             "n_cancelled_trials": planned - len(results), "n_running_trials": 0, "n_pending_trials": 0},
                   **({"pass_hat_k": hat} if hat else {}), **({"budget": budget} if budget else {})}, f, indent=1)
