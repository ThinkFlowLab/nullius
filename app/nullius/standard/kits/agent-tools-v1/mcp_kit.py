"""The tools kit's MCP server: JSON-RPC 2.0 over stdio, one message per line.

Twenty tools of a small shipping office - stock, parcels, orders, accounts,
site permits - most of them decoys for any one check: names alike, answers
of the same shape. Every value is derived from ``$KIT_SEED`` (one seed per
unit, drawn by the driver and never in a prompt), so an answer cannot be
guessed, only fetched with the right tool and the right arguments.

Every ``tools/call`` is appended to ``$KIT_LOG`` as one JSON line - the tool,
its arguments, whether it was an error, what it said - and the driver scores
from that log and the final answer. ``$KIT_TOOLS``, a comma-separated list,
narrows what ``tools/list`` offers (the one-tool check); every tool by default.

The driver imports this module for the same derivations (:func:`code`,
:func:`site_id`, :func:`chain`), so the server and the scoring cannot drift.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time

PROTOCOL = "2025-06-18"


def code(seed, *parts):
    """A code such as ``Q7KD-29XF``: eight characters of an HMAC of ``parts``
    under the unit's seed. Every tool answers in this shape."""
    d = hmac.new(str(seed).encode(), "|".join(str(p) for p in parts).encode(), hashlib.sha256).digest()
    s = base64.b32encode(d).decode()[:8]
    return f"{s[:4]}-{s[4:]}"


def norm(s):
    """An identifier as the tools compare it: case and outer spaces ignored."""
    return " ".join(str(s).split()).upper()


def site_id(seed, name):
    return "SITE-" + code(seed, "site", norm(name)).replace("-", "")[:5]


def chain(seed, order):
    """Order -> customer -> account -> settlement code: the three hops."""
    customer = "CUS-" + code(seed, "customer", int(order))
    account = "ACC-" + code(seed, "account", customer)
    return customer, account, code(seed, "settlement", account)


def _s(desc):
    return {"type": "string", "description": desc}


def _i(desc):
    return {"type": "integer", "description": desc}


PARCEL = {"parcel": _s("The parcel's id, such as P-1234.")}
NUMBERS = {                       # one family of near-twins: the decoy check asks for one of them
    "get_tracking_number": "The carrier's tracking number of a parcel, as the carrier knows it.",
    "get_tracing_number": "The internal trace number of a parcel in the warehouse scanner log.",
    "get_order_number": "The number of the customer order a parcel belongs to.",
    "get_invoice_number": "The number of the invoice billed for a parcel.",
    "get_return_number": "The return authorisation number of a parcel sent back.",
    "get_customs_number": "The customs declaration number of a parcel.",
    "get_batch_number": "The packing batch number a parcel was packed in.",
    "get_route_number": "The delivery route number a parcel is on.",
    "get_pallet_number": "The number of the pallet a parcel travels on.",
    "get_seal_number": "The number of the tamper seal on a parcel.",
}
# "The site", not "the site's id": a schema asking for an id the agent has no
# way to learn sends a careful agent looking for one instead of calling - the
# error is where it learns the id, as from many a real API.
SITE_YEAR = {"site": _s("The site."),"year": _i("The year, such as 2025.")}
STOCK = {"warehouse": _s("The warehouse's name."), "item": _i("The item number.")}

TOOLS = [
    ("lookup_code", "Returns the code word of this check.", {}),
    ("get_stock_code", "The stock code of an item in a warehouse.", STOCK),
    ("get_stock_count", "How many of an item a warehouse holds.", STOCK),
    *[(n, d, PARCEL) for n, d in NUMBERS.items()],
    ("get_order_status", "Where an order stands: received, packed, shipped or delivered.", {"order": _i("The order number.")}),
    ("get_order_customer", "The id of the customer who placed an order.", {"order": _i("The order number.")}),
    ("get_customer_account", "The id of the account a customer pays from.", {"customer": _s("The customer's id.")}),
    ("get_account_settlement", "The settlement code of an account.", {"account": _s("The account's id.")}),
    ("get_account_balance", "The balance of an account, in euros.", {"account": _s("The account's id.")}),
    ("get_permit_code", "The permit code of a site for a year.", SITE_YEAR),
    ("get_permit_fee", "The permit fee of a site for a year, in euros.", SITE_YEAR),
]


class ToolError(Exception):
    """What was wrong with a call, said back to the agent as ``isError``."""


def _int(args, name):
    v = args.get(name)
    if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, float) and v.is_integer())):
        raise ToolError(f"{name} must be an integer, not {json.dumps(v)}")
    return int(v)


def _str(args, name):
    v = args.get(name)
    if not isinstance(v, str) or not v.strip():
        raise ToolError(f"{name} must be a non-empty string, not {json.dumps(v)}")
    return v


def _site(seed, args):
    """A site is named by its id; a name is refused with the id it has, the
    one way to learn it - so the first call errs and the second can succeed."""
    s = norm(_str(args, "site"))
    if not s.startswith("SITE-"):
        raise ToolError(f"{_str(args, 'site')!r} is not a site id: sites are named by id here, "
                        f"and the site called {_str(args, 'site').strip()!r} has the id {site_id(seed, s)}")
    return s


def call(seed, name, args):
    """The text a tool returns; :class:`ToolError` for a call it refuses."""
    args = args if isinstance(args, dict) else {}
    if name == "lookup_code":
        return "kit-" + code(seed, "lookup").replace("-", "").lower()
    if name in ("get_stock_code", "get_stock_count"):
        w, i = norm(_str(args, "warehouse")), _int(args, "item")
        if name == "get_stock_count":
            return f"{int(code(seed, 'count', w, i).encode().hex(), 16) % 500} in stock"
        return f"stock code of item {i} in {w.title()}: {code(seed, 'stock', w, i)}"
    if name in NUMBERS:
        p = norm(_str(args, "parcel"))
        return f"{name[4:].replace('_', ' ')} of parcel {p}: {code(seed, name, p)}"
    if name == "get_order_status":
        o = _int(args, "order")
        return f"order {o}: " + ("received", "packed", "shipped", "delivered")[ord(code(seed, "status", o)[0]) % 4]
    if name == "get_order_customer":
        return f"order {_int(args, 'order')} was placed by customer {chain(seed, _int(args, 'order'))[0]}"
    if name == "get_customer_account":
        c = norm(_str(args, "customer"))
        return f"customer {c} pays from account ACC-{code(seed, 'account', c)}"
    if name == "get_account_settlement":
        a = norm(_str(args, "account"))
        return f"settlement code of account {a}: {code(seed, 'settlement', a)}"
    if name == "get_account_balance":
        a = norm(_str(args, "account"))
        return f"balance of account {a}: {int(code(seed, 'balance', a).encode().hex(), 16) % 90000 / 100:.2f} EUR"
    if name in ("get_permit_code", "get_permit_fee"):
        s, y = _site(seed, args), _int(args, "year")
        if name == "get_permit_fee":
            return f"permit fee of {s} for {y}: {100 + int(code(seed, 'fee', s, y).encode().hex(), 16) % 900} EUR"
        return f"permit code of {s} for {y}: {code(seed, 'permit', s, y)}"
    raise KeyError(name)


def listed(only=None):
    keep = [t for t in TOOLS if not only or t[0] in only]
    return [{"name": n, "description": d,
             "inputSchema": dict({"type": "object", "properties": props}, **({"required": list(props)} if props else {}))}
            for n, d, props in keep]


def handle(m, seed, only=None, log=None):
    """One JSON-RPC message in, its reply out (None for a notification)."""
    if not isinstance(m, dict) or "id" not in m:
        return None
    method, rid, params = m.get("method"), m["id"], m.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "kit", "version": "2"}}
    elif method == "tools/list":
        result = {"tools": listed(only)}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in [t["name"] for t in listed(only)]:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name!r}"}}
        try:
            text, err = call(seed, name, args), False
        except ToolError as e:
            text, err = str(e), True
        if log:
            with open(log, "a") as f:
                f.write(json.dumps({"t": time.time(), "tool": name, "arguments": args, "isError": err, "text": text}) + "\n")
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    elif method == "ping":
        result = {}
    elif method in ("resources/list", "prompts/list"):
        result = {method.split("/")[0]: []}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no " + str(method)}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main():
    seed, log = os.environ.get("KIT_SEED", ""), os.environ.get("KIT_LOG") or None
    only = [t for t in os.environ.get("KIT_TOOLS", "").split(",") if t] or None
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        reply = handle(m, seed, only, log)
        if reply is not None:
            print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
