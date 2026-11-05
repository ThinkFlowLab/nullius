"""Asking a set of questions over the states they read, and keeping the
answers: the seam every surface uses, so a page never groups questions by
state or merges records on its own.

``states_for(ids, build)`` groups the ids by the state kind each reads and
builds each state once; ``ask_states`` makes one call per state and merges
the records (``client.merge``); ``ask_and_store`` does that and saves the
result under the trial's key in the workspace store, merged over what was
there. A new surface registers its group (``questions.register``) and, if
its questions read a state of their own, that state's builder
(``state.register``), then calls these with its own key.

Every call also carries the ``hygiene`` group - does the state address the
judge, does it still show a credential - because state is untrusted text
from a job directory and output tokens are free. Their answers never land
in ``answers``: they go under ``hygiene[kind]`` in the record, one reading
per state kind, so a surface shows the flag beside every answer drawn from
that state and no answer changes because of it."""
from __future__ import annotations

import json
from typing import Callable, Optional

from . import client, questions as Q, store


def with_hygiene(ids) -> list:
    """The ids a call sends: the surface's own, then the hygiene group."""
    return list(ids) + [q for q in Q.hygiene_ids() if q not in ids]


def split_hygiene(rec: dict, kind: str) -> dict:
    """Move the hygiene answers out of `rec["answers"]` into
    `rec["hygiene"][kind]` - `{id: probability}` - so the surface's answers
    read as before and the reading is kept per state kind."""
    readings = {}
    for q in Q.hygiene_ids():
        a = rec["answers"].pop(q, None)
        if a and isinstance(a.get("noul"), (int, float)):
            readings[q] = a["noul"]
    if readings:
        rec.setdefault("hygiene", {})[kind] = readings
    return rec


def states_for(ids, build: Callable) -> dict:
    """{kind: (ids, text, truncated)}: the questions grouped by the state
    they read, each state built once by `build(kind) -> (text, truncated)`."""
    groups: dict = {}
    for q in ids:
        groups.setdefault(Q.state_of(q), []).append(q)
    out = {}
    for kind, qs in groups.items():
        text, truncated = build(kind)
        out[kind] = (qs, text, truncated)
    return out


def preflight(states: dict, calibration: Optional[dict] = None, budget: int = client.BUDGET_TOKENS) -> dict:
    """Refuse locally a call the judge would refuse: each state's text plus
    its questions' JSON, estimated at the upper edge of the band, against
    the request budget. Raises JudgeError(413) naming the state kind and
    roughly how far over it is; returns {kind: tokens_high} otherwise.

    state.LIMIT is 60,000 characters - about 20,000 tokens at the measured
    3 per token, 24,000 at the band's edge of 2.5 - so a built-in state fits on the default
    of thumb. The check matters when a calibration says the judge counts
    fewer characters per token than that, and for a state kind a later
    group registers with a limit of its own."""
    out = {}
    for kind, (qs, text, _) in states.items():
        if not qs:
            continue
        chars = len(text) + len(json.dumps(Q.request_questions(with_hygiene(qs))))
        est = client.estimate(chars, calls=1, calibration=calibration)
        out[kind] = est["tokens_high"]
        if est["tokens_high"] > budget:
            over = est["tokens_high"] - budget
            raise client.JudgeError(413, f"the `{kind}` state is too big for one judge call: up to {est['tokens_high']:,} tokens "
                                         f"({chars:,} characters, {est['basis']}) against a budget of {budget:,}; "
                                         f"about {over:,} tokens - roughly {int(over * est['chars_per_token']):,} characters - over. Nothing was sent.")
    return out


def ask_states(states: dict, key: str, post: Optional[Callable] = None, calibration: Optional[dict] = None,
               budget: int = client.BUDGET_TOKENS) -> Optional[dict]:
    """One judge call per state, the records merged into one; None when
    there was nothing to ask. `preflight` runs first, so a batch the judge
    would refuse is refused here before any call - and before any charge.
    Each call carries the hygiene group; its readings are kept per state
    kind under `hygiene`, apart from `answers`."""
    preflight(states, calibration=calibration, budget=budget)
    records = []
    for kind, (qs, text, truncated) in states.items():
        if qs:
            records.append(split_hygiene(client.ask(text, with_hygiene(qs), key, truncated=truncated, post=post, kind=kind), kind))
    merged = client.merge(records)
    if merged is not None:
        hygiene = {}
        for rec in records:
            hygiene.update(rec.get("hygiene") or {})
        merged["hygiene"] = hygiene
    return merged


def ask_and_store(ws_dir: str, job_id: str, trial_name: str, states: dict, key, post: Optional[Callable] = None,
                  calibration: Optional[dict] = None) -> Optional[dict]:
    """`ask_states`, then the merged record saved under the trial's key -
    its answers over the version's earlier ones. Returns the record as
    stored. The pre-flight reads the workspace's own calibration unless one
    is given, so a judge that counts more tokens per character than the rule
    of thumb is refused on its own evidence."""
    if calibration is None:
        calibration = store.calibration(ws_dir)
    rec = ask_states(states, key, post=post, calibration=calibration)
    if rec is not None:
        # a call over some of the kinds keeps the earlier readings for the others, as the answers are kept
        lane = getattr(key, "lane", "system-one")                # the LLM judge's records apart from System One's
        earlier = store.load(ws_dir, job_id, trial_name, version=rec["questions"], lane=lane) or {}
        rec["hygiene"] = {**(earlier.get("hygiene") or {}), **rec.get("hygiene", {})}
        store.save(ws_dir, job_id, trial_name, rec, lane=lane)
    return rec
