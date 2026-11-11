"""A run's spending cap: one small file every relay of the job books its calls in.

A capped run (``run --max-usd``) gives each of its relays - the units' own,
in this process or in their containers, and the benchmark's - the same
ledger. Before a call goes out, its relay books what the call is likely to
cost (:func:`bound`). The call is sent only while what was spent, what the
calls in flight have booked and this booking stay within the cap. The first
call that does not fit is refused and closes the ledger: every later call is
refused too, so a run stops at one point instead of letting its small calls
through.

A reply turns the booking into what the call cost (``settle``). A call the
provider answered with an error gives its booking back (``release``). One
whose cost was never known - a reply with no token count, a reply cut off -
keeps its whole booking, as ``unsettled``: it may have been billed.

What was spent is each reply's real cost. Only a call in flight is an
estimate, since its cost is known when its reply comes back: the prompt's
characters over three as input tokens, and as output the average reply
the run has had from that model so far (the ledger keeps their count and
sum, ``seen``) - ``OUTPUT`` before any - and never more than the limit the
call names or the model's own (its price row's ``max_output``). The
model's limit alone would book a short answer at a hundred times its
cost, and a few calls at a time would fill a cap nothing had been spent
of; the largest reply so far would let one long answer do the same to
every call after it. A call that costs more than was booked for it takes
the spend past the cap by the difference, and the next call is refused.

A cap per unit (``run --max-usd-per-unit``) is kept in the same file: what
each unit spent and has in flight, beside the run's. A call that does not
fit its unit's cap is refused, and so is every later call of that unit; the
run goes on. A unit's calls are its own and its simulated user's - part of
the task - and never its judge's, which scores it (:func:`unit_of`).

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

OUTPUT = 4096                       # output tokens booked for a call before the run has had a reply from its model
ERROR = "SpendingCapError"          # what a unit the cap ended carries: its own outcome outside the rate, never a miss (runs.classify)
# what a unit that reached its own cap carries: it had its attempt and spent its allowance, as one that spends its
# time does - an agent error, counted against the agent (runs.classify)
UNIT_ERROR = "UnitSpendingCapError"
_LIMITS = ("max_completion_tokens", "max_output_tokens", "max_tokens")


class Unavailable(Exception):
    """The ledger could not be read or written: a capped call is then not sent."""


def unit_of(tag: str) -> Optional[str]:
    """The unit a relay's route books for, by its tag (``run``): the unit's own calls and its simulated user's
    (``<tag>-simulator``) are the task; its judge's (``<tag>-judge``) score it and count toward no unit."""
    if tag.endswith("-judge"):
        return None
    return tag[:-len("-simulator")] if tag.endswith("-simulator") else tag


def bound(req: dict, raw: bytes, row: dict, path: str = "", seen: Optional[int] = None) -> float:
    """What a call is likely to cost, in USD, at ``row`` (a price row, USD
    per million tokens). Input: the request's characters over three - its
    JSON whole, less what it carries as base64 - at the dearer of the input
    and the cache-write price. Output: ``seen``, the average reply the run
    has had from this model, or ``OUTPUT`` before any; never more than the
    limit the call names or the row's own. An embeddings call has none."""
    tokens = math.ceil(max(0, len(raw) - _blobs(req)) / 3)
    rate = max(row["input"], row.get("cache_write") or 0)
    out = 0
    if not path.split("?")[0].endswith("/embeddings"):
        named = [req.get(k) for k in _LIMITS if isinstance(req.get(k), int) and not isinstance(req.get(k), bool) and req.get(k) > 0]
        out = min(x for x in (named[0] if named else None, row.get("max_output"), seen or OUTPUT) if x)
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
    def create(cls, path: str, max_usd: Optional[float] = None, unit_max_usd: Optional[float] = None) -> "Ledger":
        """``max_usd``: the run's cap, or None for a run with a cap per unit alone. ``unit_max_usd``: each unit's."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        led = cls(path)
        led._write({"max_usd": None if max_usd is None else float(max_usd),
                    "unit_max_usd": None if unit_max_usd is None else float(unit_max_usd),
                    "spent_usd": 0.0, "unsettled_usd": 0.0, "held": {}, "closed": False, "seen": {}, "units": {}, "of": {}})
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
        float(doc["max_usd"] or 0), float(doc["spent_usd"]), float(doc["unsettled_usd"]), sum(doc["held"].values())
        doc["unit_max_usd"], dict(doc["seen"]), dict(doc["units"]), dict(doc["of"])     # all there, or it is no ledger
        return doc

    def _write(self, doc: dict) -> None:
        """Written whole beside the file, then renamed: a reader that takes no lock never finds half of it."""
        part = f"{self.path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(part, self.path)

    def reserve(self, amount, unit: Optional[str] = None, model: Optional[str] = None) -> Tuple[Optional[str], dict]:
        """(the booking's id, the state before it) when ``amount`` fits;
        (None, the state) when it does not, or the ledger is closed already -
        and it is closed from then on. ``unit``: the unit the call is part
        of (:func:`unit_of`): with a cap per unit, a call that does not fit
        its unit's is refused (the state says ``by: unit``) and closes that
        unit alone - the run's ledger stays open for the others. ``amount``
        may be a function of what ``model``'s replies have been on average
        (:func:`bound`): it is then read here, under the same lock, and
        the state says the ``amount`` booked."""
        with self._locked() as doc:
            if callable(amount):
                amount = amount(_usual(doc, model))
            seen = dict(_state(doc), by="cap", amount=amount)
            each = doc["unit_max_usd"] if unit is not None else None
            if each is not None:
                u = doc["units"].setdefault(unit, {"spent_usd": 0.0, "unsettled_usd": 0.0, "closed": False})
                flight = sum(doc["held"].get(b, 0.0) for b, w in doc["of"].items() if w == unit) + u["unsettled_usd"]
                seen["unit"] = {"max_usd": each, "spent_usd": u["spent_usd"], "held_usd": flight, "closed": u["closed"]}
                if u["closed"] or amount > each - u["spent_usd"] - flight:
                    u["closed"] = True
                    self._write(doc)
                    return None, dict(seen, by="unit")
            if doc["closed"] or amount > seen["left_usd"]:
                if not doc["closed"]:
                    doc["closed"] = True
                    self._write(doc)
                return None, seen
            booking = uuid.uuid4().hex[:12]
            doc["held"][booking] = amount
            if each is not None:
                doc["of"][booking] = unit
            self._write(doc)
            return booking, seen

    def settle(self, booking: str, cost: Optional[float], model: Optional[str] = None, output: Optional[int] = None) -> None:
        """The call's cost in place of its booking; None, its cost was never
        known: the whole booking is kept, as unsettled. ``output``: the
        reply's output tokens, added to what ``model`` has answered in this
        run - its next calls are booked at their average."""
        with self._locked() as doc:
            amount = doc["held"].pop(booking, None)
            if amount is None:
                return
            if model and isinstance(output, int) and not isinstance(output, bool) and output > 0:
                replies, tokens = doc["seen"].get(model) or (0, 0)
                doc["seen"][model] = [replies + 1, tokens + output]
            unit = doc["units"].get(doc["of"].pop(booking, None))
            if cost is None:
                doc["unsettled_usd"] += amount
            else:
                doc["spent_usd"] += max(0.0, float(cost))
            if unit is not None:                 # the same, on its unit's own count
                unit["unsettled_usd" if cost is None else "spent_usd"] += amount if cost is None else max(0.0, float(cost))
            self._write(doc)

    def release(self, booking: str) -> None:
        """The booking given back: the provider answered with an error, or was never reached."""
        with self._locked() as doc:
            doc["of"].pop(booking, None)
            if doc["held"].pop(booking, None) is not None:
                self._write(doc)

    def forget(self, unit: str) -> None:
        """A unit that ended: its own count goes, so the file stays the size of the units in flight."""
        with self._locked() as doc:
            if doc["units"].pop(unit, None) is not None:
                self._write(doc)

    def usual(self, model) -> Optional[int]:
        """The output tokens ``model`` has answered a call of this run with, on average; None before any. Read without the lock."""
        try:
            return _usual(self._read(), model)
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise Unavailable(f"the spending cap's ledger could not be read: {type(e).__name__}: {e}")

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


def _usual(doc: dict, model) -> Optional[int]:
    replies, tokens = doc["seen"].get(model) or (0, 0)
    return math.ceil(tokens / replies) if replies else None


def _state(doc: dict) -> dict:
    held = sum(doc["held"].values())
    left = float("inf") if doc["max_usd"] is None else doc["max_usd"] - doc["spent_usd"] - doc["unsettled_usd"] - held   # no cap on the run: a cap per unit alone
    return {"max_usd": doc["max_usd"], "spent_usd": doc["spent_usd"], "unsettled_usd": doc["unsettled_usd"], "held_usd": held,
            "left_usd": left, "closed": bool(doc["closed"])}


def usd(v: float) -> str:
    """An amount as a person reads it: cents, and up to two digits more when it has them."""
    whole, _, cents = f"{v:.4f}".rstrip("0").partition(".")
    return f"{whole}.{cents.ljust(2, '0')}"


def refusal(state: dict, amount: float) -> str:
    """Why a call was not sent, as its caller and the record read it."""
    if state.get("by") == "unit":
        u = state["unit"]
        if u["closed"]:
            return (f"an earlier call of this unit did not fit its spending cap of {usd(u['max_usd'])} USD "
                    f"({usd(u['spent_usd'])} spent by the unit): no further call of this unit is sent")
        return (f"this unit's spending cap of {usd(u['max_usd'])} USD: {usd(u['spent_usd'])} spent by the unit"
                + (f", {usd(u['held_usd'])} booked by its calls in flight or never settled" if u["held_usd"] else "")
                + f", and this call may cost up to {usd(amount)}; it was not sent, and no further call of this unit is")
    if state["closed"]:
        return (f"an earlier call did not fit the run's spending cap of {usd(state['max_usd'])} USD "
                f"({usd(state['spent_usd'])} spent): no further call of this run is sent")
    flight = state["held_usd"] + state["unsettled_usd"]
    return (f"the run's spending cap of {usd(state['max_usd'])} USD: {usd(state['spent_usd'])} spent"
            + (f", {usd(flight)} booked by calls in flight or never settled" if flight else "")
            + f", and this call may cost up to {usd(amount)}; it was not sent, and no further call of this run is")
