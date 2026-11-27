"""A Nullius server as MCP tools, for an assistant: ``nullius mcp``, over stdio.

    NULLIUS_SERVER=http://bench.internal:5057 NULLIUS_TOKEN=... nullius mcp

Each tool is one call of ``nullius.client.Client`` and answers its JSON
document. What a tool may do is the token's scope: with a ``read`` token the
run tools answer the server's refusal. ``start_run`` starts only when
``confirm`` answers the question ``check_run`` returned, as Review asks it.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys

from nullius.client import ApiError, Client

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")     # ours first

_S, _I, _B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
_JOB = {"job_id": dict(_S, description="a job id, as list_jobs returns it")}
_TRIAL = {**_JOB, "trial": dict(_S, description="a trial name of that job, as get_job returns it")}
_RECIPE = {"recipe": {"description": "a saved recipe's id or name (list_recipes), or a whole recipe object"}}
_RUN = {"run_id": dict(_S, description="a run id, as start_run or list_runs returns it")}

# name: (description, properties, required, the call)
TOOLS = {
    "server_info": ("The server's version, this token's scope, and the machines a run can go to.", {}, [],
                    lambda nx, a: nx.info()),
    "catalogue": ("Benchmarks with their datasets, task sets and what fits each, agents and capabilities.", {}, [],
                  lambda nx, a: nx.catalogue()),
    "list_jobs": ("The jobs of the open workspace: dataset, agent, model, outcome counts, raw and adjusted solve rate.", {}, [],
                  lambda nx, a: nx.jobs()),
    "get_job": ("One job with every trial: outcome, reward, cost, tokens, exception. Read the adjusted rate and the "
                "outcome counts first: the raw rate counts infrastructure failures as misses.", _JOB, ["job_id"],
                lambda nx, a: nx.job(a["job_id"])),
    "get_trial": ("One trial: outcome, checks, usage, exception and the steps of its trajectory with their tool calls.",
                  _TRIAL, ["job_id", "trial"], lambda nx, a: nx.trial(a["job_id"], a["trial"])),
    "get_conversation": ("A page of one trial's conversation: messages, tool arguments and observations.",
                         {**_TRIAL, "page": _I, "q": dict(_S, description="text to search for")}, ["job_id", "trial"],
                         lambda nx, a: nx.conversation(a["job_id"], a["trial"], page=a.get("page"), q=a.get("q"))),
    "compare_jobs": ("Two or more jobs side by side, by task.",
                     {"job_ids": {"type": "array", "items": _S, "minItems": 2},
                      "matched": dict(_B, description="only the tasks every job ran")}, ["job_ids"],
                     lambda nx, a: nx.compare(*a["job_ids"], matched=a.get("matched") is True)),
    "get_leaderboard": ("The open workspace's agents that have a scored run, ranked by composite score: each one's overall "
                        "score, rank and score by axis, with the runs each score reads. A score is partial when `reasons` "
                        "says so.", {}, [], lambda nx, a: _ranked(nx.leaderboard())),
    "get_judgements": ("A judge model's stored answers about a job's trials (lane: decision or llm). Free to read.",
                       {**_JOB, "lane": _S}, ["job_id"], lambda nx, a: nx.judgements(a["job_id"], a.get("lane") or "decision")),
    "judge_trial": ("Ask a judge model about one trial. A paid model call: ask only when the person wants it.",
                    {**_TRIAL, "lane": _S}, ["job_id", "trial"], lambda nx, a: nx.judge(a["job_id"], a["trial"], a.get("lane") or "decision")),
    "list_recipes": ("The saved recipes: id, name and the recipe.", {}, [], lambda nx, a: nx.recipes()),
    "check_run": ("What Review shows for a recipe, without starting it: errors, the prerequisites of each machine, "
                  "and the question to confirm.", {**_RECIPE, "where": _S}, ["recipe"],
                  lambda nx, a: nx.check(a["recipe"], a.get("where"))),
    "start_run": ("Start a recipe. It spends model tokens and machine time: call check_run first, and set confirm only "
                  "when the person has agreed to the question it returned.", {**_RECIPE, "where": _S, "confirm": _B}, ["recipe"],
                  lambda nx, a: nx.start(a["recipe"], a.get("where"), confirm=a.get("confirm") is True)),
    "list_runs": ("The runs the server follows: state, stages, job ids.", {}, [], lambda nx, a: nx.runs()),
    "get_run": ("One run: state, stages, and its log lines from `since` on.", {**_RUN, "since": _I}, ["run_id"],
                lambda nx, a: nx.run(a["run_id"], a.get("since") or 0)),
    "stop_run": ("Stop a run.", _RUN, ["run_id"], lambda nx, a: nx.stop(a["run_id"])),
}


def _ranked(board: dict) -> dict:
    """The leaderboard without the catalogue's agents that have no run here: most of the document, and nothing to rank."""
    return {**board, "rows": [r for r in board.get("rows") or [] if r.get("has_runs")]}


def listed() -> list:
    return [{"name": name, "description": text,
             "inputSchema": {"type": "object", "properties": props, "required": need, "additionalProperties": False}}
            for name, (text, props, need, _) in TOOLS.items()]


_TYPES = {"string": str, "integer": int, "boolean": bool, "array": list}


def _is(value, prop) -> bool:
    if not isinstance(value, _TYPES[prop["type"]]) or (prop["type"] == "integer" and isinstance(value, bool)):
        return False
    return prop["type"] != "array" or all(_is(v, prop["items"]) for v in value)


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(m, nx):
    """One JSON-RPC message in, its reply out (None for a notification, or a response)."""
    if not isinstance(m, dict):
        return _error(None, -32600, "one JSON-RPC message at a time")
    if "id" not in m or "method" not in m:
        return None
    method, rid, params = m["method"], m["id"], m.get("params") or {}
    if not isinstance(params, dict):
        return _error(rid, -32602, "params is an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "nullius", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": listed()}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or name not in TOOLS:
            return _error(rid, -32602, f"unknown tool {name!r}")
        if not isinstance(args, dict):
            return _error(rid, -32602, "arguments is an object")
        args = {k: v for k, v in args.items() if v is not None}
        unknown = sorted(set(args) - set(TOOLS[name][1]))
        if unknown:                             # dropped, it would answer about something else than was asked
            return _error(rid, -32602, f"{name} takes no {', '.join(unknown)}")
        wrong = [f"{k}: {p['type']}" for k, p in TOOLS[name][1].items() if k in args and "type" in p and not _is(args[k], p)]
        if wrong:
            return _error(rid, -32602, f"{name} takes {', '.join(wrong)}")
        missing = [k for k in TOOLS[name][2] if k not in args]
        try:
            if missing:
                raise ApiError(400, f"{name} needs {', '.join(missing)}")
            text, err = json.dumps(TOOLS[name][3](nx, args), ensure_ascii=False), False
        except ApiError as e:
            text, err = json.dumps({"error": str(e), **e.document}, ensure_ascii=False), True
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    elif method == "ping":
        result = {}
    elif method in ("resources/list", "prompts/list"):
        result = {method.split("/")[0]: []}
    else:
        return _error(rid, -32601, f"no {method}")
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main(argv=None) -> int:
    argparse.ArgumentParser(prog="nullius mcp", description="A Nullius server as MCP tools for an assistant, over stdio. "
                            "NULLIUS_SERVER and NULLIUS_TOKEN name the server and its token.").parse_args(argv)
    try:
        nx = Client()
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    for line in sys.stdin:
        try:
            reply = handle(json.loads(line), nx)
        except (ValueError, RecursionError):
            reply = _error(None, -32700, "a line that is not JSON")
        if reply is not None:
            print(json.dumps(reply), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
