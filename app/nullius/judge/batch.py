"""Asking a set of questions over the states they read, and keeping the
answers: the seam every surface uses, so a page never groups questions by
state or merges records on its own.

A new surface registers its group (``questions.register``) and, if
its questions read a state of their own, that state's builder
(``state.register``), then calls ``states_for`` and ``ask_and_store`` with its own key.

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
    `rec["hygiene"][kind]` - `{id: probability}`."""
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

    The check matters when a calibration says the judge counts
    fewer characters per token than the default band's 2.5, and for a state kind a later
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


def ask_and_store(base: str, job_id: str, trial_name: str, states: dict, key, post: Optional[Callable] = None,
                  calibration: Optional[dict] = None, started: Optional[str] = None,
                  lane: Optional[str] = None, old: Optional[dict] = None, job_dir: Optional[str] = None,
                  strict: bool = False) -> Optional[dict]:
    """`ask_states`, then the merged record saved under the trial's key -
    its answers over the version's earlier ones - in the store at `base`
    (`store.home`). Returns the record as stored. The pre-flight reads the
    job's own calibration unless one is given. `started`: the trial's own
    start, which the record keeps (`store.save`). `lane`: where it is kept,
    the judge's own lane unless named (`store.TASKS`). `old`: the job's
    older records (`store.overlay`), the file's copied in by the save. `job_dir`: the
    job's folder, nothing saved once it is deleted (`store.Gone`). `strict`: an
    attempt with several tries (`store.save`). A job with no identity yet (pending), or such a try
    with no start, is refused before the judge is asked: its record could not be kept, or shown."""
    if not job_id:
        raise client.JudgeError(0, "this job has no identity until its result is written, so it is not judged yet")
    if strict and not started:
        raise client.JudgeError(0, "this try has no start, so its judgement could not be told from another try's")
    if calibration is None:
        calibration = store.calibration(base, [job_id])
    rec = ask_states(states, key, post=post, calibration=calibration)
    if rec is not None:
        # the LLM judge's records apart from Decision's; a call over some of the kinds keeps the
        # earlier readings for the others, as the answers are kept (`store.save`), and returns them
        lane = lane or getattr(key, "lane", "decision")
        rec.setdefault("hygiene", {})
        rec["hygiene"] = store.save(base, job_id, trial_name, rec, lane=lane, started=started, old=old, job_dir=job_dir,
                                     strict=strict)["hygiene"]
    return rec
