"""Kit for agent.tools/v1, version 2: five checks, each a different use of the
tools of one MCP server (``mcp_kit.py``) given to the session.

- ``one-tool``: the server offers a single tool without arguments;
- ``arguments``: a tool taking a string and an integer, its answer theirs;
- ``decoys``: the right tool among twenty of alike names and answers;
- ``chain``: three tools, each one's output the next one's input;
- ``retry``: the first call is refused as a tool error saying what was wrong,
  and the agent calls again, corrected.

Each unit draws a seed the prompt never carries; every answer is derived from
it, so none can be guessed. A unit passes when the final message carries the
answer *and* the server's own log shows the calls that produce it - written
beside the trial's verdict (``verifier/mcp-calls.jsonl``), outside the
workspace the agent works in. The prompts say what to find, never which tool.
"""
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sys

from nullius.standard.container import CHECK_IMAGE
from nullius.standard.jsonl import serve

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("nullius_kit_mcp", os.path.join(HERE, "mcp_kit.py"))
mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp)

UNITS = ("one-tool", "arguments", "decoys", "chain", "retry")
WAREHOUSES = ("Riverside", "Northgate", "Old Quay", "Millbrook")
SITES = ("Harbour Road", "Cedar Yard", "Mill Lane", "Station Hill")
FINISH = " Reply with it and nothing else."
SEEDS = {}
# The agent starts the server: in its container on docker, by the image's own python3 (this machine's
# interpreter, mounted over it, would not run there); here, by this one.
PYTHON = "python3" if os.environ.get(CHECK_IMAGE) else sys.executable


def asked(seed):
    """What the prompts name, from the seed: never an answer, only the question."""
    n = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return {"warehouse": WAREHOUSES[n % 4], "item": 100 + n // 4 % 900, "parcel": f"P-{1000 + n // 3600 % 9000}",
            "order": 1000 + n // 7 ** 9 % 9000, "site": SITES[n // 11 ** 9 % 4], "year": 2019 + n // 13 ** 9 % 8}


def prompt(unit, seed):
    a = asked(seed)
    use = "Using the tools of the MCP server named kit, given to this session, find "
    return {
        "one-tool": "The MCP server named kit, given to this session, holds the code word of this check. Find it." + FINISH,
        "arguments": use + f"the stock code of item {a['item']} in the {a['warehouse']} warehouse." + FINISH,
        "decoys": use + f"the carrier's tracking number of parcel {a['parcel']}." + FINISH,
        "chain": use + f"the settlement code of the account that pays for order {a['order']}." + FINISH,
        "retry": use + f"the permit code of the site called {a['site']} for the year {a['year']}." + FINISH,
    }[unit]


def expected(unit, seed):
    a = asked(seed)
    if unit == "one-tool":
        return mcp.call(seed, "lookup_code", {})
    if unit == "arguments":
        return mcp.code(seed, "stock", mcp.norm(a["warehouse"]), a["item"])
    if unit == "decoys":
        return mcp.code(seed, "get_tracking_number", mcp.norm(a["parcel"]))
    if unit == "chain":
        return mcp.chain(seed, a["order"])[2]
    return mcp.code(seed, "permit", mcp.site_id(seed, a["site"]), a["year"])


def log_path(dir):
    """The server's call log: beside the trial's verdict, not in the workspace."""
    return os.path.join(os.path.dirname(os.path.abspath(dir)), "verifier", "mcp-calls.jsonl")


def read_log(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _flat(s):
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def _ok(c, tool, **want):
    """A call of ``tool`` that succeeded with these arguments (compared as the server compares them)."""
    if c.get("tool") != tool or c.get("isError"):
        return False
    args = c.get("arguments") or {}
    for k, v in want.items():
        got = args.get(k)
        if isinstance(v, int):
            if isinstance(got, bool) or not isinstance(got, (int, float)) or got != v:
                return False
        elif mcp.norm(got) != mcp.norm(v):
            return False
    return True


def missing(unit, seed, calls):
    """What the log lacks for ``unit``: None when the calls are there."""
    a = asked(seed)
    if unit == "one-tool":
        return None if any(_ok(c, "lookup_code") for c in calls) else "lookup_code was never called"
    if unit == "arguments":
        return None if any(_ok(c, "get_stock_code", warehouse=a["warehouse"], item=a["item"]) for c in calls) \
            else f"no successful get_stock_code call for item {a['item']} in {a['warehouse']}"
    if unit == "decoys":
        return None if any(_ok(c, "get_tracking_number", parcel=a["parcel"]) for c in calls) \
            else f"get_tracking_number was never called for {a['parcel']}"
    if unit == "chain":
        customer, account, _ = mcp.chain(seed, a["order"])
        hops = [("get_order_customer", {"order": a["order"]}), ("get_customer_account", {"customer": customer}),
                ("get_account_settlement", {"account": account})]
        i = 0
        for c in calls:                                  # the three hops, in order
            if i < len(hops) and _ok(c, hops[i][0], **hops[i][1]):
                i += 1
        return None if i == len(hops) else f"the chain stopped before {hops[i][0]}"
    sid = mcp.site_id(seed, a["site"])
    refused = next((n for n, c in enumerate(calls) if c.get("tool") == "get_permit_code" and c.get("isError")), None)
    if refused is None:
        return "get_permit_code never answered with an error"
    if not any(_ok(c, "get_permit_code", site=sid, year=a["year"]) for c in calls[refused + 1:]):
        return "no corrected get_permit_code call after the error"
    return None


def verdict(unit, seed, calls, answer):
    """(passed, detail) for one unit, from the server's log and the final message."""
    want = expected(unit, seed)
    said = answer or ""
    why = None if _flat(want) in _flat(said) else "the final message does not carry the answer"
    why = why or missing(unit, seed, calls)
    codes = set(re.findall(r"[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}", said))
    came = [c["tool"] for c in calls if not c.get("isError") and any(_flat(x) in _flat(c.get("text")) for x in codes)]
    detail = {"said": said[:200], "expected": want, "calls": [c.get("tool") for c in calls]}
    if why:
        detail["why"] = why
    if came:
        detail["answer_from"] = sorted(set(came))
    return why is None, detail


def units(dataset):
    return {"units": list(UNITS)}


def setup(unit, dir):
    if unit not in UNITS:
        raise ValueError(f"no unit {unit!r}")
    SEEDS[unit] = seed = secrets.token_hex(16)
    log = log_path(dir)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    if os.path.exists(log):
        os.remove(log)
    env = {"KIT_SEED": seed, "KIT_LOG": log}
    if unit == "one-tool":
        env["KIT_TOOLS"] = "lookup_code"
    return {"prompt": prompt(unit, seed),
            "mcp": [{"name": "kit", "command": PYTHON, "args": [os.path.join(HERE, "mcp_kit.py")], "env": env}]}


def score(unit, dir, transcript, answer):
    if unit not in SEEDS:
        raise ValueError(f"{unit} was not set up")
    ok, detail = verdict(unit, SEEDS[unit], read_log(log_path(dir)), answer)
    return {"reward": 1.0 if ok else 0.0, "passed": ok, "detail": detail}


def oracle(unit, dir):
    """The reference: the calls a correct agent makes, sent to the kit's own
    server code (``mcp.handle``, logged as the server logs them), and the
    answer they lead to - so the reference passes through the same scorer."""
    seed, a = SEEDS[unit], asked(SEEDS[unit])
    if unit == "chain":
        customer, account, _ = mcp.chain(seed, a["order"])
        calls = [("get_order_customer", {"order": a["order"]}), ("get_customer_account", {"customer": customer}),
                 ("get_account_settlement", {"account": account})]
    else:
        calls = {"one-tool": [("lookup_code", {})],
                 "arguments": [("get_stock_code", {"warehouse": a["warehouse"], "item": a["item"]})],
                 "decoys": [("get_tracking_number", {"parcel": a["parcel"]})],
                 "retry": [("get_permit_code", {"site": a["site"], "year": a["year"]}),           # refused: a name, not an id
                           ("get_permit_code", {"site": mcp.site_id(seed, a["site"]), "year": a["year"]})]}[unit]
    for n, (tool, args) in enumerate(calls):
        mcp.handle({"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": tool, "arguments": args}},
                   seed, log=log_path(dir))
    return {"answer": expected(unit, seed)}


if __name__ == "__main__":
    serve({"units": units, "setup": setup, "score": score, "oracle": oracle})
