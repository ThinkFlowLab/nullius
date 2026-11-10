"""A run's spending cap: one small file every relay of the job books its calls in.

A capped run (``run --max-usd``) gives each of its relays - the units' own,
in this process or in their containers, and the benchmark's - the same
ledger. Before a call goes out, its relay books the most the call is likely
to cost (:func:`bound`). The call is sent only while what was spent, what the
calls in flight have booked and this booking stay within the cap. The first
call that does not fit is refused and closes the ledger: every later call is
refused too, so a run stops at one point instead of letting its small calls
through.

A reply turns the booking into what the call cost (``settle``). A call the
provider answered with an error gives its booking back (``release``). One
whose cost was never known - a reply with no token count, a reply cut off -
keeps its whole booking, as ``unsettled``: it may have been billed.

A booking is an estimate, not a bill: the prompt's characters over three as
input tokens, and as output the limit the call names, else the model's own
(its price row's ``max_output``), else ``OUTPUT``. A call that costs more
than was booked for it takes the spend past the cap by the difference, and
the next call is refused.

The file is rewritten whole under a lock held for one read and one write
(``flock`` on a file beside it, as an install is locked): a relay that dies
holding it lets it go.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import uuid
from typing import Optional, Tuple

OUTPUT = 4096                       # output tokens booked when neither the call nor the model's price row names a limit
ERROR = "SpendingCapError"          # what a unit the cap ended carries: its own outcome outside the rate, never a miss (runs.classify)
_LIMITS = ("max_completion_tokens", "max_output_tokens", "max_tokens")


class Unavailable(Exception):
    """The ledger could not be read or written: a capped call is then not sent."""


def bound(req: dict, raw: bytes, row: dict, path: str = "") -> float:
    """The most a call is likely to cost, in USD, at ``row`` (a price row,
    USD per million tokens). Input: the request's characters over three -
    its JSON whole, less what it carries as base64 - at the dearer of the
    input and the cache-write price. Output: the limit the call names, else
    the row's, else ``OUTPUT``; an embeddings call has none."""
    tokens = math.ceil(max(0, len(raw) - _blobs(req)) / 3)
    rate = max(row["input"], row.get("cache_write") or 0)
    out = 0
    if not path.split("?")[0].endswith("/embeddings"):
        named = [req.get(k) for k in _LIMITS if isinstance(req.get(k), int) and not isinstance(req.get(k), bool) and req.get(k) > 0]
        out = named[0] if named else row.get("max_output") or OUTPUT
    return (tokens * rate + out * row["output"]) / 1e6


def _blobs(v) -> int:
    """Characters of a request that are an encoded file, not a prompt: a
    ``data:`` address, a block's base64 ``data``."""
    if isinstance(v, str):
        return len(v) if v.startswith("data:") else 0
    if isinstance(v, list):
        return sum(_blobs(x) for x in v)
    if isinstance(v, dict):
        if v.get("type") == "base64" and isinstance(v.get("data"), str):
            return len(v["data"])
        return sum(_blobs(x) for x in v.values())
    return 0


class Ledger:
    def __init__(self, path: str):
        self.path = path

    @classmethod
    def create(cls, path: str, max_usd: float) -> "Ledger":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        led = cls(path)
        led._write({"max_usd": float(max_usd), "spent_usd": 0.0, "unsettled_usd": 0.0, "held": {}, "closed": False})
        return led

    @contextlib.contextmanager
    def _locked(self):
        try:
            with open(self.path + ".lock", "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    yield self._read()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise Unavailable(f"the spending cap's ledger could not be used: {type(e).__name__}: {e}")

    def _read(self) -> dict:
        with open(self.path, encoding="utf-8") as f:
            doc = json.load(f)
        float(doc["max_usd"]), float(doc["spent_usd"]), float(doc["unsettled_usd"]), sum(doc["held"].values())
        return doc

    def _write(self, doc: dict) -> None:
        """Written whole beside the file, then renamed: a reader that takes no lock never finds half of it."""
        part = f"{self.path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(part, self.path)

    def reserve(self, amount: float) -> Tuple[Optional[str], dict]:
        """(the booking's id, the state before it) when ``amount`` fits;
        (None, the state) when it does not, or the ledger is closed already -
        and it is closed from then on."""
        with self._locked() as doc:
            seen = _state(doc)
            if doc["closed"] or amount > seen["left_usd"]:
                if not doc["closed"]:
                    doc["closed"] = True
                    self._write(doc)
                return None, seen
            booking = uuid.uuid4().hex[:12]
            doc["held"][booking] = amount
            self._write(doc)
            return booking, seen

    def settle(self, booking: str, cost: Optional[float]) -> None:
        """The call's cost in place of its booking; None, its cost was never
        known: the whole booking is kept, as unsettled."""
        with self._locked() as doc:
            amount = doc["held"].pop(booking, None)
            if amount is None:
                return
            if cost is None:
                doc["unsettled_usd"] += amount
            else:
                doc["spent_usd"] += max(0.0, float(cost))
            self._write(doc)

    def release(self, booking: str) -> None:
        """The booking given back: the provider answered with an error, or was never reached."""
        with self._locked() as doc:
            if doc["held"].pop(booking, None) is not None:
                self._write(doc)

    def state(self) -> dict:
        """``max_usd``, ``spent_usd``, ``unsettled_usd``, ``held_usd`` (the
        calls in flight), ``left_usd`` and ``closed``, read without the lock."""
        try:
            return _state(self._read())
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise Unavailable(f"the spending cap's ledger could not be read: {type(e).__name__}: {e}")

    def closed(self) -> bool:
        """Whether a call was refused: nothing more of the run is begun. A
        ledger that cannot be read is closed - no call it guards is sent."""
        try:
            return bool(self.state()["closed"])
        except Unavailable:
            return True


def _state(doc: dict) -> dict:
    held = sum(doc["held"].values())
    return {"max_usd": doc["max_usd"], "spent_usd": doc["spent_usd"], "unsettled_usd": doc["unsettled_usd"], "held_usd": held,
            "left_usd": doc["max_usd"] - doc["spent_usd"] - doc["unsettled_usd"] - held, "closed": bool(doc["closed"])}


def usd(v: float) -> str:
    """An amount as a person reads it: cents, and up to two digits more when it has them."""
    whole, _, cents = f"{v:.4f}".rstrip("0").partition(".")
    return f"{whole}.{cents.ljust(2, '0')}"


def refusal(state: dict, amount: float) -> str:
    """Why a call was not sent, as its caller and the record read it."""
    if state["closed"]:
        return (f"an earlier call did not fit the run's spending cap of {usd(state['max_usd'])} USD "
                f"({usd(state['spent_usd'])} spent): no further call of this run is sent")
    flight = state["held_usd"] + state["unsettled_usd"]
    return (f"the run's spending cap of {usd(state['max_usd'])} USD: {usd(state['spent_usd'])} spent"
            + (f", {usd(flight)} booked by calls in flight or never settled" if flight else "")
            + f", and this call may cost up to {usd(amount)}; it was not sent, and no further call of this run is")
