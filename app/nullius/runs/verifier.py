"""The verifier's own report, read as the benchmark's entry describes it.

Everything else this app reads is Harbor's and the same for every benchmark:
``result.json``, ``config.json``, ``exception_info``, ``trace.json``, the ATIF
trajectory and ``verifier_result.rewards.reward``. The verifier's report is the
exception - one benchmark writes a ``report.json``, another a CTRF
``ctrf.json`` - and its keys are the benchmark's. So no code here names them:
a benchmark's ``benchmark.json`` carries a ``report`` block, and this module
reads any report by it::

    "report": {
      "sources": [...],         the dataset names a Harbor job records for it
      "file": "verifier/report.json",
      "required": true,         no file: the verifier never ran (else the
                                reward file stands in for it)
      "under_task": true,       the document is keyed by the task's name
      "succeeded": "resolved",  a field, or "reward": Harbor's 1.0
      "produced": "patch_exists", "produced_unless": "patch_is_None",
      "usable": "patch_successfully_applied",
      "gained": {"pass": [paths], "fail": [paths]},     lists of check names
      "held":   {...},                                  the same, or
      "gained": {"tests": path, "name": "name", "status": "status",
                 "pass": "passed", "fail": "failed",
                 "count_pass": path, "count_fail": path},  a list of tests
      "words": {"gained": "Fail → Pass", "gained_abbr", "gained_term",
                "held", ..., "has_patch": true}
    }

A path is dotted (``tests_status.FAIL_TO_PASS.success``). *gained* are the
checks that had to start passing, *held* the ones that had to keep passing: a
benchmark with one pass or fail leaves ``held`` out. A benchmark with no block
still parses: the reward is all that is read, and its pages show fewer
columns. A field the report cannot answer is ``None``, never a plausible zero.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    """What a verifier said, in terms no single benchmark owns.

    `None` means *this benchmark does not report that*, and is distinct from
    `False` / `0`, which mean it reported the absence.
    """

    succeeded: bool | None = None
    produced: bool | None = None      # did the agent emit a product at all?
    usable: bool | None = None        # did that product apply / run?
    gained_pass: int = 0
    gained_fail: int = 0
    held_pass: int = 0
    held_fail: int = 0
    gained_failures: tuple[str, ...] = ()
    held_failures: tuple[str, ...] = ()


UNREAD = Verdict()


@dataclass(frozen=True)
class Vocabulary:
    """The benchmark's own names for its checks on screen (``report.words``).

    `None` means the benchmark has no such category, and the pages leave the
    row or column out rather than dash it under a foreign label. `has_patch`
    gates the "Patch applied" card the same way.
    """

    gained: str | None = None    # card label, e.g. "Fail → Pass" / "Tests"
    gained_abbr: str = ""        # table-column form; empty falls back to `gained`
    gained_term: str = ""        # the dataset's jargon name, when it has one
    held: str | None = None
    held_abbr: str = ""
    held_term: str = ""
    has_patch: bool = False


# What a benchmark without a report block gets: no named categories, so the
# check columns and patch cards are simply not drawn.
GENERIC_VOCABULARY = Vocabulary()


def reward_is_success(reward: float | None) -> bool | None:
    """Harbor's convention: 1.0 is a solve. `None` in, `None` out."""
    return None if reward is None else reward >= 1.0


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def reward_marker(trial_dir: str):
    """`{}` when the verifier left its reward file, `None` when it left nothing.

    `verifier/reward.txt`, or a `verifier/reward.json` object with a numeric
    `reward` (harbor-rewardkit): either is evidence the verifier ran. The empty
    dict is deliberately not `None`: the verifier spoke and said no more than
    the reward.
    """
    verifier = os.path.join(trial_dir, "verifier")
    if os.path.isfile(os.path.join(verifier, "reward.txt")):
        return {}
    doc = _read_json(os.path.join(verifier, "reward.json"))
    if isinstance(doc, dict) and isinstance(doc.get("reward"), (int, float)) \
            and not isinstance(doc.get("reward"), bool):
        return {}
    return None



_CACHE: dict = {}


def _blocks() -> dict:
    """Every ``report`` block in the catalogue, by each source name it lists."""
    from nullius.catalogue import catalog
    sig = catalog.signature()
    if _CACHE.get("sig") != sig:
        table = {}
        for b in catalog.load().benchmarks:
            if b.report:
                for name in b.report.get("sources") or [b.key]:
                    table[name.strip().lower()] = (b.key, b.report)
        _CACHE.update(sig=sig, table=table)
    return _CACHE["table"]


def block_for(source: str | None):
    """``(benchmark key, report block)`` for *source*, or ``None``."""
    return _blocks().get((source or "").strip().lower())


def _dig(doc, path: str):
    for part in path.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc


def _names(doc, paths) -> list:
    out = []
    for p in paths or []:
        got = _dig(doc, p)
        if isinstance(got, list):
            out += [str(x) for x in got]
    return out


def _checks(doc, spec) -> tuple[int, int, tuple]:
    """``(passed, failed, failing names)`` of one category."""
    if not spec:
        return 0, 0, ()
    if "tests" in spec:                       # a list of test objects with a status
        tests = [t for t in (_dig(doc, spec["tests"]) or []) if isinstance(t, dict)]
        name, status = spec.get("name", "name"), spec.get("status", "status")
        failing = tuple(str(t.get(name, "")) for t in tests if t.get(status) == spec.get("fail", "failed"))
        # the producer's own counts when it gives them, as trace totals are taken
        passed = _dig(doc, spec["count_pass"]) if spec.get("count_pass") else None
        failed = _dig(doc, spec["count_fail"]) if spec.get("count_fail") else None
        if passed is None and failed is None and tests:
            passed = sum(1 for t in tests if t.get(status) == spec.get("pass", "passed"))
            failed = len(failing)
        return int(passed or 0), int(failed or 0), failing
    ok, bad = _names(doc, spec.get("pass")), _names(doc, spec.get("fail"))
    return len(ok), len(bad), tuple(bad)


def _read_block(spec: dict, report, reward) -> Verdict:
    if not isinstance(report, dict):
        return Verdict()
    how = spec.get("succeeded", "reward")
    succeeded = reward_is_success(reward) if how == "reward" else bool(_dig(report, how))
    produced = usable = None
    if spec.get("produced"):
        produced = bool(_dig(report, spec["produced"])) and not (
            spec.get("produced_unless") and _dig(report, spec["produced_unless"]))
        # only meaningful once something was produced
        usable = bool(_dig(report, spec["usable"])) if spec.get("usable") and produced else None
    gp, gf, gfail = _checks(report, spec.get("gained"))
    hp, hf, hfail = _checks(report, spec.get("held"))
    return Verdict(succeeded=succeeded, produced=produced, usable=usable,
                   gained_pass=gp, gained_fail=gf, held_pass=hp, held_fail=hf,
                   gained_failures=gfail, held_failures=hfail)


# --- what the rest of the app calls ------------------------------------------

def vocabulary_for(source: str | None) -> Vocabulary:
    """How *source* names its checks, or the empty vocabulary. Never raises."""
    found = block_for(source)
    if not found:
        return GENERIC_VOCABULARY
    words = found[1].get("words") or {}
    return Vocabulary(**{k: v for k, v in words.items() if k in Vocabulary.__dataclass_fields__})


def load(source: str | None, trial_dir: str, task_base: str):
    """Find *source*'s verifier report inside *trial_dir*.

    Without a block: a ``verifier/report.json`` if there is one, else the
    reward marker. Returns `None` when the verifier left nothing at all, which
    `read()` refuses to score.
    """
    found = block_for(source)
    if not found:
        report = _read_json(os.path.join(trial_dir, "verifier", "report.json"))
        return report if isinstance(report, dict) else reward_marker(trial_dir)
    spec = found[1]
    report = _read_json(os.path.join(trial_dir, spec.get("file", "verifier/report.json")))
    if not isinstance(report, dict):
        return None if spec.get("required") else reward_marker(trial_dir)
    if spec.get("under_task"):
        inner = report.get(task_base)
        if inner is None and len(report) == 1:
            inner = next(iter(report.values()))
        return inner
    return report


def read(source: str | None, report, reward: float | None) -> Verdict:
    """Turn a verifier report into a `Verdict`, however little is understood.

    No report means the verifier never spoke, which is not the same as it
    saying no — so `succeeded` stays `None` and the reward is *not* consulted.
    """
    if report is None:
        return UNREAD
    found = block_for(source)
    if not found:
        return Verdict(succeeded=reward_is_success(reward))
    return _read_block(found[1], report, reward)
