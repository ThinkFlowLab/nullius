"""A study: recorded model calls of several trials, each sent K times as recorded and under named changes, under
one spending cap, read by one rule written down before the first send, and compared call by call.

- :func:`targets` names the calls of the trials given: every one, every Nth, or those whose recorded answer a
  rule labels as asked; :func:`read_targets` reads them from a file of lines.
- :func:`study` prepares each target under each arm (``replay.prepare``): the first arm is the call as
  recorded, each other a named list of changes (``replay.replace_text`` and the rest; :func:`changes_of` reads
  them from JSON). It writes the plan (the targets, the arms, the requests' sha256 and models, K, the rule, the
  yes or no reading, the two arms compared) to ``study.json`` before the first send, then sends what is not
  answered yet through relays of its own, booked in one ledger for the whole study. Started again on the same
  folder it goes on: only what was not answered is sent, under the same cap counted from the ledger; a plan
  that differs from the kept one is refused. ``dry_run`` sends nothing and writes nothing.
- The result: for each target and arm the count of each answer; the table of targets by arms (the answers
  read yes of those answered); between two arms the targets whose share of yes went up, went down or tied,
  and an exact two-sided sign test (:func:`sign_test`).
- :func:`recount` reads a kept study or replay again with another rule, with no send; its counts go to
  ``recounts/`` beside the earlier ones.

The folder, outside every run's tree: ``study.json``, ``targets.jsonl`` (each target's source and its recorded
reply as it arrived), ``answers.jsonl`` (one line per send, written as it is answered), ``relay/`` (one relay per
start and destination: each answer whole, as it arrived), ``ledger/`` and ``recounts/``. The key is never
written; a command given as a change or a rule runs without it.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import inspect
import json
import marshal
import math
import os
import re
import shlex
import threading
import types
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from nullius.standard.calls import exchanges, ledger as ledgers, relay
from nullius.standard.runner import replay as rp
from nullius.standard.runner.replay import NotFound, NotWhole, Refused  # noqa: F401  (a study's errors are replay's)

SCHEMA = "nullius.study/v1"
RECOUNT = "nullius.recount/v1"
RECORDED = "recorded"                            # the first arm: the call as it was sent
MAX_SENDS = 10000                                # sends of one study
_ARM = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_READS_MB = 1                                    # a rule's own file larger than this is not hashed into the plan
CHANGES = {"replace_text": rp.replace_text, "system_text": rp.system_text, "tool_description": rp.tool_description,
           "tools": rp.tools, "drop_last": rp.drop_last, "set_field": rp.set_field, "model": rp.model,
           "served_by": rp.served_by, "provider": rp.provider, "through": rp.through}


@dataclass(frozen=True)
class Target:
    """Call ``call`` (its ``n`` in the call record) of ``trial`` in ``job_dir``."""
    job_dir: str
    trial: str
    call: int

    def record(self) -> dict:
        return {"job_dir": os.path.abspath(self.job_dir), "trial": self.trial, "call": self.call}


@dataclass
class Arm:
    """A named list of changes (``replay.Change``), applied in order to each target's request."""
    name: str
    changes: List[rp.Change] = field(default_factory=list)


def changes_of(spec) -> List[rp.Change]:
    """Changes from JSON: a list of ``[NAME, ARG...]``, NAME a change of :data:`CHANGES` and the ARGs its
    arguments, as a program passes them: ``[["replace_text", "old", "new"], ["set_field", "temperature", 0]]``."""
    if not isinstance(spec, list):
        raise Refused("an arm's changes are a JSON list of [NAME, ARG...]")
    out = []
    for c in spec:
        if not isinstance(c, list) or not c or c[0] not in CHANGES:
            raise Refused(f"a change is [NAME, ARG...], NAME one of {', '.join(CHANGES)}: not {json.dumps(c)}")
        try:
            out.append(CHANGES[c[0]](*c[1:]))
        except TypeError:
            raise Refused(f"{c[0]} does not take the arguments {json.dumps(c[1:])}")
    return out


# -- targets -------------------------------------------------------------------------------------------------
def read_targets(path: str) -> List[Target]:
    """A file of lines ``JOB_DIR TRIAL CALL``: ``#`` starts a comment, a blank line is skipped, a relative
    JOB_DIR is read from the file's folder."""
    try:
        with open(path, encoding="utf-8") as f:
            rows = f.read().splitlines()
    except OSError as e:
        raise NotFound(f"cannot read the targets file {path}: {e.strerror or e}")
    out = []
    for i, row in enumerate(rows, 1):
        row = row.split("#", 1)[0].strip()
        if not row:
            continue
        bits = row.rsplit(None, 2)
        if len(bits) != 3 or not bits[2].isdigit():
            raise Refused(f"{path}, line {i}: JOB_DIR TRIAL CALL, the call a number (not {row!r})")
        job = bits[0] if os.path.isabs(bits[0]) else os.path.join(os.path.dirname(os.path.abspath(path)), bits[0])
        out.append(Target(os.path.normpath(job), bits[1], int(bits[2])))
    if not out:
        raise Refused(f"{path} names no target: one line JOB_DIR TRIAL CALL each")
    return out


def targets(job_dir: str, trials: Sequence[str], *, every: int = 1, where: Optional[str] = None,
            rule: Optional[rp.Rule] = None) -> List[Target]:
    """The calls of ``trials`` in ``job_dir`` that can be sent again (whole, the agent's own): every one; every
    ``every``-th (its n a multiple of it); with ``where``, a regular expression, only those whose recorded answer
    ``rule`` (``replay.first_tool_call`` by default) labels with a match."""
    if isinstance(every, bool) or not isinstance(every, int) or every < 1:
        raise Refused(f"every: a whole number, at least 1 (not {every!r})")
    found = _pattern(where, "where") if where is not None else None
    out = []
    for trial in trials:
        for row in rp.calls(job_dir, trial, rule=(rule or rp.first_tool_call) if found else None):
            if row["whole"] and not row["side"] and not row["n"] % every and (not found or found.search(row["label"])):
                out.append(Target(job_dir, trial, row["n"]))
    return out


# -- the plan ------------------------------------------------------------------------------------------------
def _pattern(text: str, what: str):
    try:
        return re.compile(text)
    except (re.error, TypeError) as e:
        raise Refused(f"{what} {text!r} is not a regular expression: {e}")


def _bare(code: types.CodeType) -> types.CodeType:
    """``code`` and the code nested in it without their file and lines: a rule run from another path, or moved in
    its file, is the same rule."""
    return code.replace(co_filename="", co_firstlineno=1, co_linetable=b"",
                        co_consts=tuple(_bare(c) if isinstance(c, types.CodeType) else c for c in code.co_consts))


def _function_fingerprint(rule: rp.Rule) -> Optional[str]:
    def describe(value):
        if isinstance(value, types.FunctionType):
            return {"code": marshal.dumps(_bare(value.__code__)).hex(), "defaults": describe(value.__defaults__),
                    "keywords": describe(value.__kwdefaults__),
                    "closure": [describe(c.cell_contents) for c in value.__closure__ or ()],
                    "globals": describe(inspect.getclosurevars(value).globals)}
        if isinstance(value, types.ModuleType):
            return {"module": value.__name__}
        if isinstance(value, dict):
            return {k: describe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [describe(v) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError("the rule captures a value without a stable fingerprint")

    try:
        if not isinstance(rule, types.FunctionType):
            return None
        raw = json.dumps(describe(rule), sort_keys=True, allow_nan=False).encode()
        return hashlib.sha256(raw).hexdigest()
    except (TypeError, ValueError, RecursionError):
        return None


def rule_record(rule: rp.Rule) -> dict:
    """What the plan keeps of a rule: its description and, for a command, the sha256 of each file the command
    names (a reading script changed since the first start is another rule)."""
    out = dict(getattr(rule, "rule", None) or {"name": getattr(rule, "__name__", "rule")})
    if out.get("command"):
        try:
            words = shlex.split(str(out["command"]))
        except ValueError:
            words = []
        files = {}
        for w in words:
            try:
                if os.path.isfile(w) and os.path.getsize(w) <= _READS_MB * 10 ** 6:
                    with open(w, "rb") as f:
                        raw = f.read()
                    if not raw.startswith(b"\x7fELF"):     # a program, not its script: an interpreter's update is not a new rule
                        files[os.path.abspath(w)] = hashlib.sha256(raw).hexdigest()
            except OSError:
                continue
        if files:
            out["files"] = files
    else:
        out["fingerprint"] = _function_fingerprint(rule)
    return out


def yes_record(yes: Optional[str]) -> dict:
    return {"name": "the recorded answer"} if yes is None else {"name": "matches", "pattern": yes}


def _is_yes(yes: Optional[str], label: str, recorded: Optional[str]) -> bool:
    return label == recorded if yes is None else bool(re.search(yes, label))


def _plan(targets, arms, preps, times, rule, yes, compare) -> dict:
    return {"targets": [dict(t.record(), sent_sha256=p[0].source.sent_sha256) for t, p in zip(targets, preps)],
            "arms": [{"name": a.name, "changes": [{"name": c["name"], "args": c["args"]} for c in p.changes]}
                     for a, p in zip(arms, preps[0])],
            "requests": [[{"arm": a.name, "model": p.model, "provider": p.provider, "served_by": p.serving.get(p.model),
                           "sha256": p.sha256} for a, p in zip(arms, row)] for row in preps],
            "times": times, "rule": rule_record(rule), "yes": yes_record(yes), "compare": compare}


def _differs(kept: dict, plan: dict) -> Optional[str]:
    """What of the plan changed since the first start, in words; None when nothing did."""
    models = lambda p: [[(r["model"], r["provider"], r["served_by"]) for r in row] for row in p.get("requests") or []]
    for what, same in (("targets", kept.get("targets") == plan["targets"]),
                       ("arms", [a["name"] for a in kept.get("arms") or []] == [a["name"] for a in plan["arms"]]),
                       ("number of sends for each target and arm", kept.get("times") == plan["times"]),
                       ("reading rule", kept.get("rule") == plan["rule"]),
                       ("yes or no reading", kept.get("yes") == plan["yes"]),
                       ("arms compared", kept.get("compare") == plan["compare"]),
                       ("model, provider or pin", models(kept) == models(plan)),
                       ("changes", kept.get("arms") == plan["arms"]),
                       ("requests", kept.get("requests") == plan["requests"])):
            if not same:
                return what
    return None


def _check_out(out: str, targets: List[Target]) -> str:
    out = os.path.abspath(out)
    real = os.path.realpath(out)
    for job in {os.path.realpath(t.job_dir) for t in targets}:
        if os.path.commonpath((real, job)) == job:
            raise Refused(f"{out} is inside the job {job}: a study writes nothing in a run's tree")
    rp._check_tree(out)
    return out


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Refused(f"{path} cannot be read: {e}")
    return got if isinstance(got, dict) else None


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


# -- the tally -----------------------------------------------------------------------------------------------
def sign_test(higher: int, lower: int) -> float:
    """The exact two-sided sign test: the chance, were each target that moved as likely to move either way, of a
    split at least as uneven as ``higher`` against ``lower`` (ties left out before); 1.0 when none moved."""
    n = higher + lower
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(min(higher, lower) + 1)) / 2 ** n)


def _name(t: dict) -> str:
    return f"{t['trial']} call {t['call']}"


def tally(plan: dict, recorded: List[Optional[str]], rows: List[dict], yes: Optional[str] = None,
          compare: Optional[Sequence[str]] = None, by_tool: bool = False) -> dict:
    """The counts of a study's answers: ``rows`` its answered sends (``target``, ``arm``, ``label``), one each;
    ``recorded`` each target's recorded label. ``table``: a row per target, a cell per arm (yes of answered);
    ``counts``: each distinct label by target and arm; ``test``: the sign test between the two arms of
    ``compare`` (the plan's by default). ``by_tool``: the counts grouped by tool, for the default rule's labels."""
    arms = [a["name"] for a in plan["arms"]]
    cells = {(i, a): [] for i in range(1, len(plan["targets"]) + 1) for a in arms}
    for r in rows:
        if (r.get("target"), r.get("arm")) in cells and "label" in r:
            cells[(r["target"], r["arm"])].append(r["label"])
    table, counts = [], []
    for i, t in enumerate(plan["targets"], 1):
        rec = recorded[i - 1]
        table.append({"target": f"t{i}", "name": _name(t), "recorded": rec,
                      "arms": {a: {"yes": sum(1 for x in cells[(i, a)] if _is_yes(yes, x, rec)), "answered": len(cells[(i, a)])}
                               for a in arms}})
        counts.append({"target": f"t{i}", "arms": {a: rp.count(cells[(i, a)], rec, by_tool) for a in arms}})
    compare = list(compare) if compare else plan.get("compare")
    test = None
    if compare:
        a, b = compare
        if a not in arms or b not in arms or a == b:
            raise Refused(f"compare: two arms of {', '.join(arms)} (not {a!r} and {b!r})")
        moved = {"higher": [], "lower": [], "ties": [], "not_counted": []}
        for row in table:
            x, y = row["arms"][a], row["arms"][b]
            if not x["answered"] or not y["answered"]:
                moved["not_counted"].append(row["target"])
                continue
            ra, rb = x["yes"] * y["answered"], y["yes"] * x["answered"]     # the shares, compared without division
            moved["higher" if rb > ra else "lower" if rb < ra else "ties"].append(row["target"])
        test = {"a": a, "b": b, **moved, "tie_names": [table[int(t[1:]) - 1]["name"] for t in moved["ties"]],
                "p_two_sided": sign_test(len(moved["higher"]), len(moved["lower"]))}
    return {"yes": yes_record(yes), "table": table, "counts": counts, "test": test}


def _answered(out: str) -> Dict[tuple, dict]:
    """The study's answered sends by (target, arm, k): the first answer of each, a refusal or a failure is none."""
    got = {}
    for a in rp._jsonl(os.path.join(out, "completed.jsonl")) + rp._jsonl(os.path.join(out, "answers.jsonl")):
        rp._inside(out, a.get("relay") or "", exchanges.FOLDER, (a.get("route") or "") + ".jsonl")
        slot = (a.get("target"), a.get("arm"), a.get("k"))
        if a.get("status") == 200 and not a.get("refused") and slot not in got:
            got[slot] = a
        elif slot in got and (a.get("relay"), a.get("route")) == (got[slot].get("relay"), got[slot].get("route")):
            got[slot].update(a)
    return got


# -- the study -----------------------------------------------------------------------------------------------
def study(out: str, targets: Sequence[Target], arms: Sequence[Arm] = (), times: int = 1, max_usd: Optional[float] = None, *,
          rule: Optional[rp.Rule] = None, yes: Optional[str] = None, compare: Optional[Sequence[str]] = None,
          prices: Optional[Dict[str, dict]] = None, endpoint: Optional[str] = None, concurrency: int = 1,
          stop_after: Optional[int] = None, dry_run: bool = False, env=None,
          say: Optional[Callable[[str], None]] = None, same_rule: bool = False) -> dict:
    """``targets``, each sent ``times`` times as recorded and under each of ``arms``, under one spending cap of
    ``max_usd`` for the whole study, everything kept in the folder ``out``. ``rule`` reads each answer
    (``replay.first_tool_call`` by default); ``yes``, a regular expression a label must match to read yes (by
    default: the label is the recorded answer's); ``compare``, the two arms of the sign test (the first two by
    default). ``stop_after``: at most this many sends in this start. ``dry_run``: what would be sent and its
    estimate, nothing sent or written. On a folder that holds the study already, it goes on: only what was not
    answered is sent; another plan or cap is refused. Returns the document written to ``study.json``, with
    ``out``; the dry run, its own. ``same_rule`` confirms an uncheckable function is unchanged on a restart."""
    from nullius.standard.runner import run as runs                # its key and endpoint checks, as a run makes them
    env = os.environ if env is None else env
    targets, arms = list(targets), [Arm(RECORDED, [])] + list(arms)
    if not targets:
        raise Refused("a study needs at least one target")
    if len({t.record()["job_dir"] + "\0" + t.trial + "\0" + str(t.call) for t in targets}) != len(targets):
        raise Refused("a target is named twice: each call is one target")
    names = [a.name for a in arms]
    for a in arms[1:]:
        if not _ARM.match(a.name or "") or a.name == RECORDED:
            raise Refused(f"an arm's name: letters, digits, '.', '_' or '-', at most 40, and not {RECORDED!r} (not {a.name!r})")
    if len(set(names)) != len(names):
        raise Refused("two arms have one name")
    if isinstance(times, bool) or not isinstance(times, int) or not 1 <= times <= rp.MAX_TIMES:
        raise Refused(f"times: a whole number from 1 to {rp.MAX_TIMES} (not {times!r})")
    if len(targets) * len(arms) * times > MAX_SENDS:
        raise Refused(f"{len(targets)} targets, {len(arms)} arms and {times} sends each make more than {MAX_SENDS} sends")
    try:
        cap = float(max_usd)
    except (TypeError, ValueError):
        cap = 0.0
    if not 0 < cap < float("inf"):
        raise Refused(f"max_usd {max_usd!r}: the study's spending cap in USD, above zero")
    if stop_after is not None and (isinstance(stop_after, bool) or not isinstance(stop_after, int) or stop_after < 1):
        raise Refused(f"stop_after: a whole number, at least 1 (not {stop_after!r})")
    if yes is not None:
        _pattern(yes, "yes")
    compare = list(compare) if compare else (names[:2] if len(names) > 1 else None)
    if compare and (len(compare) != 2 or compare[0] == compare[1] or not set(compare) <= set(names)):
        raise Refused(f"compare: two arms of {', '.join(names)} (not {compare!r})")
    rule = rule or rp.first_tool_call
    out = _check_out(out, targets)
    preps = [[rp.prepare(src, a.changes, prices=prices, endpoint=endpoint, env=env) for a in arms]
             for src in (rp.source(t.job_dir, t.trial, t.call) for t in targets)]
    plan = _plan(targets, arms, preps, times, rule, yes, compare)
    kept = _read_json(os.path.join(out, "study.json"))
    if kept is None and os.path.exists(out) and (not os.path.isdir(out) or os.listdir(out)):
        raise Refused(f"{out} already holds files and no study: a study's folder is its own")
    if kept is not None:
        if kept.get("schema") != SCHEMA or not isinstance(kept.get("plan"), dict):
            raise Refused(f"{out}/study.json is not a study's")
        if not plan["rule"].get("command") and (not plan["rule"].get("fingerprint")
                                              or not kept["plan"].get("rule", {}).get("fingerprint")):
            if not same_rule:
                raise Refused("the reading rule cannot be fingerprinted: pass same_rule=True only if it is unchanged")
            plan["rule"] = kept["plan"]["rule"]
        what = _differs(kept["plan"], plan)
        if what:
            raise Refused(f"the study in {out} began with other {what}: it goes on only as it began (a new folder starts another)")
        if kept.get("max_usd") != cap:
            raise Refused(f"the study in {out} has a cap of {ledgers.usd(kept.get('max_usd') or 0.0)} USD: it goes on under it")
    led = ledgers.Ledger(os.path.join(out, "ledger", "ledger.json"))
    spent = None
    if kept is not None:
        try:
            spent = led.state()
        except ledgers.Unavailable as e:
            raise Refused(f"the study's ledger cannot be read, so its spend is unknown: {e}")
    answered = _answered(out) if kept is not None else {}
    todo = [(i, j, k) for k in range(1, times + 1) for i in range(1, len(targets) + 1) for j in range(len(arms))
            if (i, names[j], k) not in answered]
    estimate = sum(preps[i - 1][j].per_call_usd() for i, j, _ in todo)     # an estimate, no upper bound
    if dry_run:
        return {"dry_run": True, "out": out, "goes_on": kept is not None,
                "targets": [dict(plan["targets"][i], target=f"t{i + 1}", path=row[0].source.path, bytes=len(row[0].raw),
                                 recorded_cost_usd=row[0].source.record.get("cost_usd"),
                                 arms=[{"arm": a.name, "sha256": p.sha256, "bytes": len(p.raw), "unchanged": p.sha256 == p.source.sent_sha256,
                                        "model": p.model, "provider": p.provider, "served_by": p.serving.get(p.model),
                                        "estimate_usd": p.per_call_usd()} for a, p in zip(arms, row)])
                            for i, row in enumerate(preps)],
                "arms": plan["arms"], "times": times, "rule": plan["rule"], "yes": plan["yes"], "compare": compare,
                "sends": {"all": len(targets) * len(arms) * times, "answered": len(answered), "to_send": len(todo)},
                "estimate_usd": estimate, "upper_bound": False, "max_usd": cap, "spent_usd": (spent or {}).get("spent_usd", 0.0),
                "fits": estimate <= cap - (spent or {}).get("spent_usd", 0.0) - (spent or {}).get("unsettled_usd", 0.0)}
    keys = {}
    for row in preps:
        for p in row:
            if p.provider not in keys:
                try:
                    keys[p.provider] = runs.provider_endpoint(p.provider, env)[1]
                except runs.Refused as e:
                    raise Refused(str(e))
    if kept is None:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "targets.jsonl"), "w", encoding="utf-8") as f:
            for i, row in enumerate(preps, 1):
                src = row[0].source
                f.write(json.dumps(dict(plan["targets"][i - 1], target=f"t{i}", route=src.route, path=src.path,
                                        reply=exchanges._cut_key(src.reply, keys.values()), cost_usd=src.record.get("cost_usd")), ensure_ascii=False) + "\n")
        kept = {"schema": SCHEMA, "plan": plan, "max_usd": cap, "state": "sending", "created": _now(), "starts": []}
        _write(out, kept)                        # the plan and its rule, kept before the first send
        led = ledgers.Ledger.create(led.path, cap)
    doc = kept
    recorded = [rp._read_rule(rule, rp.answer_of(row[0].source.path, row[0].source.reply), keys.values()) for row in preps]
    for a in answered.values():
        if "label" not in a:
            a["label"] = rp._read_rule(rule, a, keys.values())
            with open(os.path.join(out, "answers.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({x: v for x, v in a.items() if x != "body"}, ensure_ascii=False) + "\n")
    if todo:
        _send(out, doc, preps, names, todo, keys, led, times, rule, concurrency, stop_after, say)
    answered = _answered(out)
    try:
        state = led.state()
        spend = {k: state[k] for k in ("max_usd", "spent_usd", "unsettled_usd")}
    except ledgers.Unavailable as e:
        spend = {"max_usd": cap, "error": str(e)}
    left = len(targets) * len(arms) * times - len(answered)
    doc.update({"state": "done" if not left else "stopped", "left": left, "spend": spend,
                **tally(plan, recorded, list(answered.values()), yes, compare, rule is rp.first_tool_call)})
    doc = exchanges._cut_key(doc, keys.values())
    _write(out, doc)
    if say:
        say(exchanges._cut_key(f"{out}: {len(answered)} of {len(targets) * len(arms) * times} answered, "
                              f"{ledgers.usd(spend.get('spent_usd') or 0.0)} USD spent of {ledgers.usd(cap)}", keys.values()))
    return dict(doc, out=out)


def _send(out, doc, preps, names, todo, keys, led, times, rule, concurrency, stop_after, say) -> None:
    """One start of a study: the slots ``todo`` sent through relays of its own, each answer appended to
    ``answers.jsonl`` as it comes; the start's line in ``doc``."""
    start = len(doc["starts"]) + 1
    doc["starts"].append({"start": start, "started": _now(), "to_send": len(todo), "stop_after": stop_after})
    doc["state"] = "sending"
    _write(out, doc)
    groups: Dict[tuple, relay.Relay] = {}
    group_of = {}
    for i, j, _ in todo:
        p = preps[i - 1][j]
        g = (p.provider, p.endpoint, p.key_header, p.model, tuple(p.serving.get(p.model) or ()))
        if g not in groups:
            name = f"s{start}-g{len(groups) + 1}"
            groups[g] = relay.Relay(p.endpoint, keys[p.provider], os.path.join(out, "relay", name), prices={p.model: p.price},
                                    model=p.model, key_header=p.key_header, ledger=led.path, serving=p.serving or None,
                                    exchanges="whole")
        group_of[(i, j)] = groups[g]
    wi, wk = len(str(len(preps))), len(str(times))
    stop, why, lock, begun = threading.Event(), [], threading.Lock(), [0]

    def one(slot) -> Optional[dict]:
        i, j, k = slot
        with lock:
            if stop.is_set():
                return None
            if stop_after is not None and begun[0] >= stop_after:
                stop.set()
                why.append(f"stopped after {stop_after} sends, as asked")
                return None
            begun[0] += 1
        rl = group_of[(i, j)]
        a, rec = rp.post(rl, preps[i - 1][j], f"t{i:0{wi}d}a{j + 1}k{k:0{wk}d}")
        a, rec = exchanges._cut_key([a, rec], keys.values())
        a.update({"target": i, "arm": names[j], "k": k, "start": start, "relay": os.path.relpath(rl.dir, out)})
        with lock, open(os.path.join(out, "completed.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
        if a["refused"]:
            stop.set()
            why.append(f"t{i} {names[j]} send {k} was refused ({a['refused']}): " + str(rec.get("error") or ""))
        elif a["status"] in rp.STOP_STATUS:
            stop.set()
            why.append(f"t{i} {names[j]} send {k}: the provider refused the key (HTTP {a['status']})")
        elif a["status"] == 200:
            try:
                a["label"] = rp._read_rule(rule, a, keys.values())
            except Exception:
                stop.set()
                raise
        with lock:
            with open(os.path.join(out, "answers.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({x: v for x, v in a.items() if x != "body"}, ensure_ascii=False) + "\n")
        if say:
            say(f"t{i} {names[j]} {k}/{times}: " + (a.get("label") if "label" in a else f"HTTP {a['status']}"
                                                     + (f", refused ({a['refused']})" if a["refused"] else "")))
        return a

    got = []
    try:
        for rl in groups.values():
            rl.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(int(concurrency or 1), len(todo) or 1))) as pool:
            futures = [pool.submit(one, s) for s in todo]
            try:
                for f in futures:
                    got.append(f.result())
            except KeyboardInterrupt:             # what is in flight ends and is written; nothing more is sent
                with lock:
                    stop.set()
                    why.append("interrupted")
                got += [f.result() for f in futures[len(got):]]
    finally:
        for rl in groups.values():
            if rl.server is not None:
                rl.stop()
    sent = [a for a in got if a is not None]
    doc["starts"][-1].update({"ended": _now(), "sent": sum(1 for a in sent if not a.get("refused")),
                              "answered": sum(1 for a in sent if a.get("status") == 200 and not a.get("refused")),
                              "failed": sum(1 for a in sent if a.get("status") != 200 and not a.get("refused")),
                              "refused": sum(1 for a in sent if a.get("refused")), "stopped": why[0] if why else None,
                              "sent_as_prepared": all(a.get("sent_sha256") == preps[a["target"] - 1][names.index(a["arm"])].sha256
                                                      for a in sent if not a.get("refused"))})


def _write(out: str, doc: dict) -> None:
    rp._write_json(os.path.join(out, "study.json"), {k: v for k, v in doc.items() if k != "out"})


# -- counted again -------------------------------------------------------------------------------------------
def _reply(out: str, relay_dir: str, route: str) -> Optional[dict]:
    """A send's reply as the study's or replay's relay kept it whole: status, streamed, body."""
    folder = rp._inside(out, relay_dir, exchanges.FOLDER)
    rp._inside(folder, route + ".jsonl")
    lines = exchanges.lines(folder, route)
    return lines[0].get("reply") if lines and isinstance(lines[0].get("reply"), dict) else None


def recount(folder: str, rule: Optional[rp.Rule] = None, yes: Optional[str] = None,
            compare: Optional[Sequence[str]] = None) -> dict:
    """A kept study or replay read again by ``rule`` (``replay.first_tool_call`` by default), with no send and no
    key: each answer as its relay kept it. A study's ``yes`` and ``compare`` default to its plan's. The counts
    are written to ``recounts/<time>.json`` in the folder, beside the earlier ones (``earlier``), which stay as
    they were. Returns that document, with ``file``."""
    folder = os.path.abspath(folder)
    rp._check_tree(folder)
    rule = rule or rp.first_tool_call
    if yes is not None:
        _pattern(yes, "yes")
    kept = _read_json(os.path.join(folder, "study.json"))
    if kept is not None and kept.get("schema") == SCHEMA:
        plan = kept["plan"]
        tg = rp._jsonl(os.path.join(folder, "targets.jsonl"))
        recorded = [rule(rp.answer_of(t.get("path") or "", t.get("reply"))) for t in tg]
        rows, missing = [], 0
        for a in _answered(folder).values():
            rp._inside(folder, a.get("relay") or "", exchanges.FOLDER, (a.get("route") or "") + ".jsonl")
            reply = a if "body" in a else _reply(folder, a.get("relay") or "", a.get("route") or "")
            if reply is None:
                missing += 1
                continue
            t = tg[a["target"] - 1] if 0 < a.get("target", 0) <= len(tg) else {}
            rows.append({"target": a["target"], "arm": a["arm"], "label": rule(rp.answer_of(t.get("path") or "", reply))})
        yes = yes if yes is not None else (plan.get("yes") or {}).get("pattern")
        doc = {"schema": RECOUNT, "of": "study", "rule": rule_record(rule), "read": len(rows), "not_kept": missing,
               **tally(plan, recorded, rows, yes, compare, rule is rp.first_tool_call),
               "earlier": {"rule": plan["rule"], "yes": plan["yes"], "table": kept.get("table"), "test": kept.get("test")}}
    else:
        kept = _read_json(os.path.join(folder, "replay.json"))
        if kept is None or kept.get("schema") != rp.SCHEMA:
            raise NotFound(f"{folder} holds no study (study.json) and no replay (replay.json)")
        if compare:
            raise Refused("compare: a replay has one arm; a study compares two")
        s = kept.get("source") or {}
        try:
            line = rp.recorded(s.get("job_dir") or "", s.get("trial") or "").get(s.get("call"))
        except (NotFound, NotWhole):
            line = None
        recorded = rule(line) if line else None
        labels, missing = [], 0
        for a in rp._jsonl(os.path.join(folder, "answers.jsonl")):
            if a.get("status") != 200 or a.get("refused"):
                continue
            reply = a if "body" in a else _reply(folder, "relay", a.get("route") or "")
            if reply is None:
                missing += 1
                continue
            labels.append(rule(rp.answer_of(s.get("path") or "", reply)))
        doc = {"schema": RECOUNT, "of": "replay", "rule": rule_record(rule), "read": len(labels), "not_kept": missing,
               "recorded_answer": recorded, "counts": rp.count(labels, recorded, rule is rp.first_tool_call),
               "same_as_recorded": sum(1 for x in labels if x == recorded),
               "yes": yes_record(yes) if yes is not None else None,
               "yes_count": sum(1 for x in labels if re.search(yes, x)) if yes is not None else None,
               "earlier": {"rule": kept.get("rule"), "recorded_answer": kept.get("recorded_answer"), "counts": kept.get("counts")}}
    where = os.path.join(folder, "recounts")
    os.makedirs(where, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d__%H-%M-%S")
    name, n = os.path.join(where, stamp + ".json"), 1
    while os.path.exists(name):
        n += 1
        name = os.path.join(where, f"{stamp}-{n}.json")
    doc["counted"] = _now()
    doc["earlier"]["recounts"] = sorted(x for x in os.listdir(where) if x.endswith(".json"))
    rp._write_json(name, doc)
    return dict(doc, file=name)
