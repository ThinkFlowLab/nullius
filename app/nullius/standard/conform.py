"""Conformance: run a port's kit against an agent or a capability, and write down
what passed.

A port is offered only once its kit has passed (``definitions.problems``
refuses an ``offers`` entry without its ``proven`` record), so this is the one
way into ``offers``. A kit passes when every one of its **basic** checks
passes. Its **skills** (``definitions.skills_of``: the harder uses of the same
contract a kit may name) are recorded one by one, shown or not, and never take
the port away - a benchmark that relies on one says so in its
``needs_skills``. A kit that names no skill is every check basic: it passes
when every check has reward 1, as before skills existed.

``repeat``: each check run that many times, for confidence; a check, basic or
a skill, passes when it passes in a majority of its runs, and the record
counts them (``2/3``). One run each by default.

The record keeps the date, the score, the model it passed on, the version of
the agent, the version of the kit (``kit_version``: a kit grows stronger under
the same port, and a proof says which one it passed), the skills it showed or
missed, and the job that shows it - a claim anyone can open and read.

A port whose kit fails is taken out of ``offers`` when the definition is
written back: a claim that stopped being true does not stay.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from nullius.runs import parse
from nullius.standard import definitions, ports, run


STAMP = "%Y-%m-%d__%H-%M-%S"


def job_name(stamp: str, kit: str, key: str) -> str:
    return f"{stamp}__kit__{kit}__{key}"


def candidates(other: dict) -> List[str]:
    """The ports of the other side's family that have a kit; a command agent
    can only ever offer the ports its form allows."""
    fam = other["_kind"]
    out = [p for p, s in ports.PORTS.items() if ports.family(p) == fam and s["kit"]]
    if fam == "capability":
        out = [p for p in out if p.split(".", 1)[1].split("/")[0] == other.get("kind")]
    if fam == "agent" and "command" in (other.get("start") or {}):
        out = [p for p in out if p in ("agent.task/v1", "agent.workspace/v1", "agent.search/v1")]
    return out


def majority(passes: int, runs: int) -> bool:
    """Whether a check run ``runs`` times passed: in more than half of them."""
    return runs > 0 and 2 * passes > runs


def outcome(kit: dict, runs: Dict[str, List[bool]], repeat: int = 1) -> dict:
    """What a kit's runs prove, from each check's passes ({check: [passed,
    ...]}, one per run): ``passed``, the basic checks held of all of them;
    ``ok`` when every one held; ``skills``, each skill's count and whether it
    held - only for a kit that names skills. With ``repeat`` over one,
    ``checks`` counts each basic check's runs too. A basic check the kit
    names and the job never ran is a check not held."""
    skills = definitions.skills_of(kit)
    named = [u for u in kit.get("basic") or [] if u not in skills]
    basic = named + [u for u in runs if u not in skills and u not in named]

    def count(u):
        got = runs.get(u) or []
        return sum(1 for x in got if x), len(got)

    held = [u for u in basic if majority(*count(u))]
    out = {"passed": f"{len(held)}/{len(basic)}", "ok": bool(basic) and len(held) == len(basic)}
    if repeat > 1:
        out["repeat"] = repeat
        out["checks"] = {u: "{}/{}".format(*count(u)) for u in basic}
    if skills:
        out["skills"] = {s: {"passed": "{}/{}".format(*count(s)), "ok": majority(*count(s))} for s in skills}
    return out


def _runs(job) -> Dict[str, List[bool]]:
    """Each check's passes in a kit's job, by the unit its trial ran (its task
    name after the kit's key); a trial that ended in an error did not pass."""
    out: Dict[str, List[bool]] = {}
    for t in job.trials:
        out.setdefault(t.task_name.split("/", 1)[-1], []).append(t.reward == 1.0)
    return out


def said(port: str, rec: dict) -> List[str]:
    """A record as the lines a shell shows: the port, then each skill."""
    lines = [f"{port}: {'passed' if rec.get('ok', True) else 'FAILED'} {rec.get('passed', '')}"
             + (f" basic checks, each held in most of {rec['repeat']} runs" if rec.get("repeat") else "")]
    lines += [f"  {u}: {n}" for u, n in (rec.get("checks") or {}).items()]
    skills = definitions.port_skills(port)
    lines += [f"  skill {s}: {'shown' if r.get('ok') else 'not shown'} {r.get('passed', '')}"
              + (f" - {skills[s]}" if s in skills else "") for s, r in (rec.get("skills") or {}).items()]
    return lines


def check(other: dict, *, model: str, provider: str, which: Optional[List[str]] = None, jobs_root: str = "jobs",
          write: bool = True, say: Callable[[str], None] = print, env=None, unsandboxed: bool = False,
          stamp: Optional[str] = None, backend: str = "local", options: Optional[dict] = None,
          repeat: int = 1) -> Dict[str, dict]:
    """Run the kits of ``which`` (every candidate port by default) and, with
    ``write``, record the result in the definition. Returns {port: record}.
    Each kit's job is named ``<stamp>__kit__<kit>__<key>`` (:func:`job_name`),
    ``stamp`` being now unless given: a page that starts the check names the
    jobs before they exist. ``backend``: where the kits run (``run.Run``).
    ``options``: the agent's options for the kits'
    runs; a record proven with any of them off its default says which.
    ``repeat``: how many times each check runs (:func:`outcome`)."""
    import os
    env = env if env is not None else os.environ
    repeat = max(1, int(repeat or 1))
    got = {}
    for p in which or candidates(other):
        kit = definitions.kit(p)
        if kit is None:
            say(f"{p}: no kit yet")
            continue
        say(f"{p}: {kit['summary']}")
        job_dir = run.run(kit, other, model=model, provider=provider, jobs_root=jobs_root, concurrency=2,
                          name=job_name(stamp or time.strftime(STAMP), kit["key"], definitions.job_word(other)),
                          say=lambda s: say("  " + s), env=env, proving=True, unsandboxed=unsandboxed,
                          backend=backend, options=options, repeat=repeat)
        o = outcome(kit, _runs(parse.parse_job(job_dir)), repeat)
        got[p] = {"passed": o["passed"], "ok": o["ok"], "on": time.strftime("%Y-%m-%d"), "model": model, "provider": provider,
                  "version": str((other.get("upstream") or {}).get("version") or ""),
                  "kit_version": definitions.kit_version(kit), "backend": backend, "job": os.path.basename(job_dir),
                  **{k: o[k] for k in ("repeat", "checks", "skills") if k in o}}
        changed = {k: v for k, v in (options or {}).items() if v != ((other.get("options") or {}).get(k) or {}).get("default")}
        if changed:
            got[p]["options"] = changed
        for line in said(p, got[p]):
            say("  " + line)
    if write:
        def change(cur):                 # on the definition as it is now: another proof or an edit since the start stays
            offers = [p for p in cur.get("offers") or [] if not (p in got and not got[p]["ok"])]
            offers += [p for p, r in got.items() if r["ok"] and p not in offers]
            # a port's record goes with its offer; another proof (fidelity) is not this run's to take back
            proven = {p: r for p, r in (cur.get("proven") or {}).items() if p in offers or not ports.known(p)}
            proven.update({p: {k: v for k, v in r.items() if k != "ok"} for p, r in got.items() if r["ok"]})
            cur["offers"] = [p for p in ports.PORTS if p in offers]
            cur["proven"] = proven
        definitions.save(other, change)
    return got
