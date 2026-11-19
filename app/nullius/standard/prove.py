"""A benchmark's proofs: its controls behave, its units are stable, nothing leaks.

An agent or a capability proves its ports by kits (``conform``); a benchmark
proves itself, per dataset, by four checks - what ``cli prove`` runs and
writes into the benchmark's ``proven``:

- ``nop``, the null control: no agent, an empty answer, the workspace as
  setup left it. Nothing should pass - every score at the floor - up to the
  bound the control declares (``controls.nop.expect.at_most``, 0 by default).
- ``oracle``, the reference control: the driver's ``oracle`` answer, and
  what it writes into the workspace. Everything should pass - every score at
  the ceiling - down to ``controls.oracle.expect.at_least`` (1 by default; a
  judged benchmark may declare less, the judge being a model).
- ``units``: two ``units`` calls, each from a driver of its own, give the
  same ids; the ids are unique, plain enough to name a trial, as many as the
  dataset says it holds.
- ``leak``: after ``setup``, the reference is not already there to read. For
  each unit the driver sets a workspace up, the oracle is run on a copy of
  it, and what the oracle gives - its answer, and the lines it writes into
  files - is looked for in the prompt (a capability benchmark's: its recall
  questions, since the sessions it adds are meant to hold the evidence) and
  in every file setup wrote. A text shorter than ``SHORT`` characters (a
  number, a name) is not looked for - it would be found by chance - and the
  record says how many were skipped. A benchmark whose questions may name
  their answer among choices ("A or B?") says so (``controls.oracle.in_prompt``):
  those units are counted and named in the record, not failed.

The controls are real runs, jobs like any other (named
``<stamp>__<benchmark>__<dataset>__prove-nop`` and ``…-oracle``, so the runs
pages pair them as a launch's controls); the units and leak checks talk to
the driver directly and leave nothing behind. A control calls no model, so
it needs no key - except a judged benchmark's, whose judge still grades.
``--bench-option`` reaches the checks' driver and each control's run;
``--job nop=DIR`` holds a merged launch of a control to its bound.

A check that passes is recorded with its date, its counts and its job; one
that fails is taken back, as ``conform`` takes back a port - what it reached kept
under ``failed``, for a page to say why. A check run on
fewer units (``--limit``) does not replace one that covered more. A
benchmark whose units may not be redistributed (``units.redistribute``) is
recorded without what names them: no ``in_prompt.first``, no problems. Parity
with the authors' own scorer is not checked yet (``docs/standard.md``).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import time
from typing import Callable, Dict, List, Optional

from nullius.standard import definitions, install, job, jsonl, retries, run, shard as shards

STAMP = "%Y-%m-%d__%H-%M-%S"
SHORT = 12                      # characters: a reference shorter than this is not looked for
CHECKS = ("units", "leak", "nop", "oracle")
_PLAIN_ID = re.compile(r"^[^\s\x00-\x1f]{1,1000}$")   # whole in result.json; its trial folder takes the first 60 and the unit number
_MAX_FILE = 1 << 20             # a file setup wrote is read up to this size


def job_name(stamp: str, bench: str, dataset: str, control: str, shard: Optional[tuple] = None) -> str:
    return f"{stamp}__{bench}__{dataset}__prove-{control}" + (f"__shard-{shard[0]}of{shard[1]}" if shard else "")


class _Driver:
    """The benchmark's driver, started as a run starts it, for the checks
    that talk to it directly (units, leak)."""

    def __init__(self, bench: dict, ds: dict, work: str, licences=None, options: Optional[dict] = None):
        self.options = options if options is not None else options_for(bench, ds)
        self.up = install.upstream(bench)
        self.data = install.dataset(bench, ds, self.up)
        install.driver_packages(bench)                  # its driver.install, once: start() then finds it
        self.bench, self.ds, self.work = bench, ds, work
        self.licences = licences if licences is not None else run.Licences(bench)

    def start(self, n: int) -> jsonl.Proc:
        home = os.path.join(self.work, f"driver-home-{n}")
        os.makedirs(home, exist_ok=True)
        cmd, extra = install.driver_argv(self.bench)
        credentials = run.driver_secrets(self.bench, self.licences)
        env = run.base_env(home, {**extra, **credentials, "NULLIUS_UPSTREAM": self.up or "", "NULLIUS_DATASET": self.data or "",
                                  "NULLIUS_DATASET_ID": self.ds["id"], "PYTHONPATH": run.driver_path(), "PYTHONDONTWRITEBYTECODE": "1",
                                  "NULLIUS_VERSION": str(self.bench.get("_version") or ""),
                                  **run.option_env(self.options)})
        return jsonl.Proc(cmd, self.bench["_here"], env, os.path.join(self.work, f"driver-{n}.log")).start()


def options_for(bench: dict, ds: dict, given: Optional[dict] = None, which=shutil.which) -> dict:
    """The benchmark's options on ``ds`` for a control's run, at ``given`` or their defaults."""
    return run.resolve_bench_options(bench, ds["id"], given, "yours", which)[0]


def units_check(driver: _Driver, ds: dict) -> dict:
    """Two drivers, one ``units`` call each: the same ids, unique, plain."""
    got = []
    for n in (1, 2):
        p = driver.start(n)
        try:
            got.append(p.request("units", dataset=ds["id"], timeout=300).get("units"))
        except jsonl.Failed as e:
            return {"ok": False, "units": 0, "problems": [str(e)], "ids": []}
        finally:
            p.close()
    a, b = got
    problems = []
    if not isinstance(a, list) or not a:
        return {"ok": False, "units": 0, "problems": ["units: no ids"], "ids": []}
    if a != b:
        problems.append("two calls gave different ids" + ("" if len(a) == len(b) else f" ({len(a)}, then {len(b)})"))
    bad = [u for u in a if not isinstance(u, str) or not _PLAIN_ID.match(u)]
    if bad:
        problems.append(f"{len(bad)} id(s) cannot name a trial (a string, no spaces, at most 1000 characters): {bad[:3]!r}")
    names = [str(u).replace("/", "-") for u in a]          # what the pages read as a task's name (job.write_trial)
    if len(set(names)) != len(names):
        dup = sorted({x for x in names if names.count(x) > 1})
        problems.append(f"{len(names) - len(set(names))} id(s) repeat: {dup[:3]!r}")
    if isinstance(ds.get("units"), int) and ds["units"] != len(a):
        problems.append(f"the dataset says {ds['units']} units, the driver gives {len(a)}")
    return {"ok": not problems, "units": len(a), "problems": problems, "ids": [str(u) for u in a],
            "passed": f"{len(a)} ids, the same twice, unique" if not problems else ""}


def _files(root: str) -> Dict[str, bytes]:
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        for f in files:
            p = os.path.join(d, f)
            try:
                if os.path.islink(p) or os.path.getsize(p) > _MAX_FILE:
                    continue
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, root)] = fh.read()
            except OSError:
                continue
    return out


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def leak_check(driver: _Driver, units: List[str], family: str, in_prompt: str = "") -> dict:
    """For each unit: set up, run the oracle on a copy, and look for what it
    gave in what setup had already shown. ``in_prompt``: the benchmark's
    reason a prompt may name its own answer (``controls.oracle.in_prompt``,
    a choice question offering it among others); such a unit is counted and
    named, not failed. Found in a file setup wrote, it is always a leak."""
    p = driver.start(0)
    looked, short, nothing, leaks, failed, bad, named = 0, 0, 0, [], [], set(), []
    try:
        for i, unit in enumerate(units):
            a, b = os.path.join(driver.work, "leak", f"{i}", "a"), os.path.join(driver.work, "leak", f"{i}", "b")
            os.makedirs(a)
            try:
                sent, seconds = run.setup_request(driver.bench, "control")       # the oracle's unit, as a run sends it
                s = p.request("setup", unit=unit, dir=a, timeout=seconds, **sent)
                before = _files(a)
                shutil.copytree(a, b)
                o = p.request("oracle", unit=unit, dir=b, timeout=600)
            except jsonl.Failed as e:
                failed.append(f"{unit}: {e}")
                bad.add(unit)
                continue
            after = _files(b)
            if family == "capability":
                shown = " ".join(q.get("question", "") for q in (s.get("memory") or {}).get("recall") or [])
            else:
                shown = s.get("prompt") or ""
            shown = _flat(shown)
            files = _flat("\n".join(x.decode("utf-8", "replace") for x in before.values()))
            needles = []
            if str(o.get("answer") or "").strip():
                needles.append(("the answer", str(o["answer"])))
            for path, new in after.items():
                old = before.get(path)
                if old == new:
                    continue
                kept = set(old.decode("utf-8", "replace").splitlines()) if old is not None else set()
                added = "\n".join(l for l in new.decode("utf-8", "replace").splitlines() if l not in kept)
                if added.strip():
                    needles.append((path, added))
            if not needles:
                nothing += 1
            for what, text in needles:
                flat = _flat(text)
                if len(flat) < SHORT:
                    short += 1
                    continue
                looked += 1
                if flat in files or (flat in shown and not in_prompt):
                    leaks.append(f"{unit}: {what} is already there after setup, in {'a file' if flat in files else 'the prompt'}")
                    bad.add(unit)
                elif flat in shown:
                    named.append(unit)
            shutil.rmtree(os.path.dirname(a), ignore_errors=True)
    finally:
        p.close()
    problems = leaks + [f"could not check {f}" for f in failed]
    rec = {"ok": not problems, "units": len(units), "looked_for": looked, "short": short, "problems": problems,
           "passed": f"{len(units) - len(bad)}/{len(units)}"}
    notes = []
    if short:
        notes.append(f"{short} reference(s) under {SHORT} characters not looked for: too short to tell a leak from chance")
    if nothing:
        notes.append(f"{nothing} unit(s) whose oracle gave nothing to look for")
    if named:
        rec["in_prompt"] = {"units": len(named), "first": named[:5], "why": in_prompt}
        notes.append(f"{len(named)} prompt(s) name their answer, as the benchmark says they may: {in_prompt}")
    if notes:
        rec["note"] = "; ".join(notes)
    return rec


def control_check(bench: dict, ds: dict, which: str, *, limit: Optional[int], jobs_root: str, stamp: str,
                  provider: str, model: str, judge_model: Optional[str], env, concurrency: int,
                  say: Callable[[str], None], shard: Optional[tuple] = None, launch: Optional[str] = None,
                  licences=None, options: Optional[dict] = None, job_dir: Optional[str] = None,
                  ids: Optional[List[str]] = None, remove_images: bool = False) -> dict:
    """Run one control over the dataset (or its first ``limit`` units, of
    ``shard`` when given) and hold it to its bound: the pass rate, or the mean score.
    ``job_dir``: a control run already, held to the bound instead; it must cover ``ids``."""
    other = definitions.control(bench, which)
    problems = []
    if job_dir:
        job_dir = os.path.abspath(job_dir.rstrip("/"))
        problems += job_problems(bench, ds, which, job_dir, ids or [])
        cfg = _config(job_dir)
    else:
        job_dir = run.run(bench, other, model=model, provider=provider, dataset=ds["id"], limit=limit, concurrency=concurrency,
                          jobs_root=jobs_root, name=job_name(stamp, definitions.job_word(bench), ds["id"], which, shard),
                          say=lambda s: say("    " + s), env=env, judge_model=judge_model, shard=shard, launch=launch,
                          licences=licences, bench_options=options,
                          remove_images=remove_images, retries=1, retry_if=retries.rate_limited)   # as conform's
        cfg = {}
    field = FIELD[bench["score"]["kind"]]
    results = [res for _, res in trials(job_dir)]
    n = len(results)
    errored = sum(1 for res in results if res.get("exception_info"))
    values = [verdict(bench, res) for res in results if not res.get("exception_info")]
    unscored = sum(1 for v in values if v is None)
    values = [v for v in values if v is not None]
    top = sum(1 for v in values if v >= 1.0)
    if bench["score"]["kind"] == "pass":
        value = top / n if n else 0.0
    else:
        value = sum(values) / len(values) if values else 0.0
    (bound, expected), = other["expect"].items()
    within = value <= expected if bound == "at_most" else value >= expected
    if not n:
        problems.append("no unit ran")
    if errored:
        problems.append(f"{errored} unit(s) ended in an error before the score: read the job's trials")
    if unscored:
        problems.append(f"{unscored} unit(s) ended with no {field} from 0 to 1 and no error: read the job's trials")
    if n and not within:
        problems.append(f"{'pass rate' if bench['score']['kind'] == 'pass' else 'mean score'} {value:.4f}, expected {bound.replace('_', ' ')} {expected}")
    rec = {"ok": not problems, "passed": f"{top}/{n}", "value": round(value, 4), "expect": {bound: expected},
           "units": n, "job": os.path.basename(job_dir), "source": other["source"], "problems": problems}
    ran_with = _options_of(cfg) if cfg else (options or {})
    if ran_with:
        rec["options"] = ran_with
    judge = (cfg.get("standard") or {}).get("judge") if cfg else judge_model or (bench["score"].get("judge") or {}).get("model")
    if judge:
        rec["judge"] = judge
    return rec


FIELD = {"pass": "reward", "score": "score"}
UNITS_FAILED = "the units check failed"


def trials(job_dir: str) -> List[tuple]:
    """(trial, its result.json) for each trial folder of a job; an unreadable record is {}."""
    out = []
    for name in sorted(os.listdir(job_dir)):
        path = os.path.join(job_dir, name, "result.json")
        if name.startswith(".") or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                res = json.load(f)
        except (OSError, ValueError):
            res = {}
        out.append((name, res if isinstance(res, dict) else {}))
    return out


def verdict(bench: dict, res: dict) -> Optional[float]:
    """The trial's value in the field its benchmark's score kind names, when it is a finite number from 0 to 1."""
    v = (((res.get("verifier_result") or {}).get("rewards")) or {}).get(FIELD[bench["score"]["kind"]])
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0.0 <= v <= 1.0:
        return None
    return float(v)


def _options_of(cfg: dict):
    """The benchmark options a job ran with: one set, or each distinct set of its merged shards."""
    std = cfg.get("standard") or {}
    if std.get("benchmark_options"):
        return std["benchmark_options"]
    sets, todo = [], list(std.get("merged_from") or [])
    while todo:
        m = todo.pop()
        if m.get("benchmark_options") or not m.get("merged_from"):
            sets.append(m.get("benchmark_options") or {})
        else:
            todo += m.get("merged_from") or []
    distinct = {json.dumps(x, sort_keys=True): x for x in sets}
    return [distinct[k] for k in sorted(distinct)] if any(sets) else []


def _config(job_dir: str) -> dict:
    try:
        with open(os.path.join(job_dir, "config.json"), encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def job_problems(bench: dict, ds: dict, which: str, job_dir: str, ids: List[str]) -> List[str]:
    """Why ``job_dir`` is not one run of ``which`` over each unit of ``ids`` once."""
    cfg = _config(job_dir)
    std = cfg.get("standard") or {}
    if cfg.get("runner") != "standard":
        return [f"{job_dir}: not a job of the Nullius standard"]
    want = {"benchmark": bench["key"], "benchmark version": job._version(bench).get("benchmark_version"),
            "control": which, "dataset": f"{bench['key']}/{ds['id']}"}
    have = {"benchmark": std.get("benchmark"), "benchmark version": std.get("benchmark_version"),
            "control": (std.get("control") or {}).get("name"),
            "dataset": ((cfg.get("datasets") or [{}])[0] or {}).get("name")}
    bad = [f"its {k} is {have[k]!r}, not {want[k]!r}" for k in want if have[k] != want[k]]
    if std.get("merged_despite"):
        bad.append("merged despite differences: " + "; ".join(std["merged_despite"]))
    if (std.get("repeat") or 1) > 1:
        bad.append(f"each unit ran {std['repeat']} times")
    ran: Dict[str, int] = {}
    shard_jobs = {m.get("job") for m in std.get("merged_from") or []}
    alien: Dict[str, List[str]] = {}
    for trial, res in trials(job_dir):
        tstd = res.get("standard") if isinstance(res.get("standard"), dict) else {}
        own = {"benchmark": tstd.get("benchmark"), "benchmark version": tstd.get("benchmark_version"),
               "control": (tstd.get("control") or {}).get("name"),
               "who ran it": (tstd.get("plugged"), (res.get("agent_info") or {}).get("name")),
               "job": tstd.get("job")}
        expect = {"benchmark": want["benchmark"], "benchmark version": want["benchmark version"], "control": which,
                  "who ran it": (which, which), "job": cfg.get("job_name")}
        wrong = [k for k in expect if own[k] != expect[k]]
        if shard_jobs and tstd.get("shard_job") not in shard_jobs:
            wrong.append("shard")
        for k in wrong:
            alien.setdefault(k, []).append(trial)
        u = str(tstd.get("unit") or trial)
        ran[u] = ran.get(u, 0) + 1
    for k, names in alien.items():
        bad.append(f"{len(names)} trial(s) whose own record names another {k} than the job: {', '.join(names[:3])}")
    known = set(ids)
    missing = [u for u in ids if u not in ran]
    extra = sorted(u for u in ran if u not in known)
    twice = sorted(u for u, k in ran.items() if k > 1)
    if missing:
        bad.append(f"{len(missing)} of the dataset's {len(ids)} units have no trial: {', '.join(missing[:5])}" + (" ..." if len(missing) > 5 else ""))
    if extra:
        bad.append(f"{len(extra)} trial(s) of units the dataset does not have: {', '.join(extra[:5])}")
    if twice:
        bad.append(f"{len(twice)} unit(s) with more than one trial: {', '.join(twice[:5])}")
    return [f"{os.path.basename(job_dir)}: {b}" for b in bad]


def prove(bench: dict, *, dataset: Optional[str] = None, limit: Optional[int] = None, jobs_root: str = "jobs",
          provider: str = "openai", model: str = "", judge_model: Optional[str] = None, stamp: Optional[str] = None,
          env=None, concurrency: int = 4, write: bool = True, say: Callable[[str], None] = print,
          shard: Optional[tuple] = None, launch: Optional[str] = None, licences=None,
          bench_options: Optional[dict] = None, jobs: Optional[Dict[str, str]] = None,
          remove_images: bool = False) -> Dict[str, dict]:
    """Every check on every dataset (or ``dataset``); with ``write``, what
    passed is recorded in the benchmark's ``proven``. ``shard`` (I, N): the
    leak check and the controls cover that shard's units only (``shard``),
    the units check all of them, and each record says which shard.
    ``remove_images``: each unit's own image is removed after it, so the
    oracle pulls it again (by default they stay). Returns
    {dataset: {check: record}}, each record with ``ok`` and its ``problems``.
    ``jobs``: {control: job directory}, held to its bound instead of run."""
    env = env if env is not None else os.environ
    stamp = stamp or time.strftime(STAMP)
    jobs = dict(jobs or {})
    sets = [d for d in bench["datasets"] if dataset in (None, d["id"])]
    if not sets:
        raise run.Refused(f"{bench['label']} has no dataset {dataset!r}: {[d['id'] for d in bench['datasets']]}")
    declared = [c for c in definitions.CONTROLS if c in (bench.get("controls") or {})]
    if jobs:
        if dataset is None and len(sets) > 1:
            raise run.Refused("a control's job is a run on one dataset: name it (--dataset)")
        if limit or (shard and shard[1] > 1):
            raise run.Refused("a control's job is held over the whole dataset: no limit, no shard")
        for c, d in jobs.items():
            if c not in declared:
                raise run.Refused(f"{bench['label']} declares no {c} control")
            if not os.path.isdir(d):
                raise run.Refused(f"{d}: no such job")
    opts = {ds["id"]: options_for(bench, ds, bench_options) for ds in sets}
    if not declared:
        say(f"{bench['label']} declares no controls: only its units and nothing else can be checked")
    got: Dict[str, dict] = {}
    for ds in sets:
        say(f"{bench['key']}/{ds['id']}:")
        work = tempfile.mkdtemp(prefix="prove-")
        checks: Dict[str, dict] = {}
        try:
            try:
                driver = _Driver(bench, ds, work, licences=licences, options=opts[ds["id"]])
            except install.InstallFailed as e:
                raise run.Refused(f"could not install what the proof needs: {e}")
            checks["units"] = units_check(driver, ds)
            every = ids = checks["units"].pop("ids")
            if shard and shard[1] > 1:
                ids = [u for _, u in shards.select(ids, *shard)]
            ids = ids[:limit] if limit else ids
            _say(say, "units", checks["units"])
            if "oracle" in declared and ids:
                checks["leak"] = leak_check(driver, ids, bench["family"], bench["controls"]["oracle"].get("in_prompt") or "")
                _say(say, "leak", checks["leak"])
        finally:
            shutil.rmtree(work, ignore_errors=True)
        for c in declared:
            if not checks["units"]["ok"] and not jobs.get(c):
                checks[c] = {"ok": False, "units": 0, "source": definitions.control(bench, c)["source"],
                             "problems": [UNITS_FAILED + ": the control was not run"]}
            else:
                checks[c] = control_check(bench, ds, c, limit=limit, jobs_root=jobs_root, stamp=stamp, provider=provider,
                                          model=model, judge_model=judge_model, env=env, concurrency=concurrency, say=say,
                                          shard=shard if shard and shard[1] > 1 else None, launch=launch, licences=licences,
                                          options=opts[ds["id"]], job_dir=jobs.get(c), ids=every,
                                          remove_images=remove_images)
                if not checks["units"]["ok"]:
                    checks[c]["problems"].append(UNITS_FAILED + ", so this job cannot be checked against the dataset")
                    checks[c]["ok"] = False
            _say(say, c, checks[c])
        total = ds.get("units") if isinstance(ds.get("units"), int) else checks["units"]["units"]
        for name, r in checks.items():
            r["on"] = time.strftime("%Y-%m-%d")
            if limit and name != "units" and limit < total:
                r["limit"] = limit
            if shard and shard[1] > 1 and name != "units":
                r["shard"] = f"{shard[0]}/{shard[1]}"
        got[ds["id"]] = checks
    if write:
        record(bench, got)
    return got


def _say(say, name, r):
    say(f"  {name}: {'passed' if r['ok'] else 'FAILED'} {r.get('passed', '')}".rstrip()
        + "".join(f"\n    {p}" for p in r.get("problems") or []) + (f"\n    ({r['note']})" if r.get("note") else ""))


def record(bench: dict, got: Dict[str, dict]) -> None:
    """Write what passed into ``bench``'s ``proven`` and save it: a check that
    passed replaces its record unless that one covered more units; one that
    failed is taken back. The failure itself is kept under ``failed`` - what
    it reached, against what bound, its job - so a page can say "0.9955 < 1"
    rather than only "not proven"; the next pass of that check clears it.
    Units that may not be redistributed are never named (:func:`_unnamed`)."""
    if (bench.get("units") or {}).get("redistribute") is False:
        got = {ds: {name: _unnamed(r) for name, r in checks.items()} for ds, checks in got.items()}
    definitions.save(bench, lambda cur: cur.update(proven=_merged(cur.get("proven") or {}, got)))


def _unnamed(r: dict) -> dict:
    """A check's record without the units it names: the leak check's first
    prompts that name their answer, and a failed check's problems."""
    r = dict(r, problems=[])
    if "in_prompt" in r:
        r["in_prompt"] = {k: v for k, v in r["in_prompt"].items() if k != "first"}
    return r


def _merged(proven: dict, got: Dict[str, dict]) -> dict:
    proven = dict(proven)
    for ds, checks in got.items():
        cur = dict(proven.get(ds) or {})
        failed = dict(cur.pop("failed", None) or {})
        for name, r in checks.items():
            if not r["ok"]:
                cur.pop(name, None)
                failed[name] = {k: r[k] for k in ("passed", "value", "expect", "units", "job", "on", "limit", "shard") if k in r}
                failed[name]["problems"] = list(r.get("problems") or [])[:3]
                continue
            failed.pop(name, None)
            old = cur.get(name) or {}
            if old and old.get("units", 0) > r.get("units", 0):
                continue
            cur[name] = {k: v for k, v in r.items() if k not in ("ok", "problems")}
        if cur or failed:
            proven[ds] = {**{k: cur[k] for k in CHECKS if k in cur}, **({"failed": failed} if failed else {})}
        else:
            proven.pop(ds, None)
    return proven


def _pct(v: float) -> str:
    return f"{v * 100:g}%"


def must_score(expect: dict) -> str:
    """A control's bound as a page says it: "nothing" and "everything" at the
    full mark, else the bound ("at least 93%")."""
    (bound, v), = expect.items()
    if bound == "at_most":
        return "nothing" if v <= 0 else f"at most {_pct(v)}"
    return "everything" if v >= 1 else f"at least {_pct(v)}"


def short_of(rec: Optional[dict], expect: dict) -> str:
    """A proof that met a bound short of the full mark - an oracle under every
    unit, a nop over none - as a page says it: what it reached, and the bound
    ("93.55%, at least 93% needed"). "" at the full mark, or with no record."""
    if not rec or not isinstance(rec.get("value"), (int, float)):
        return ""
    (bound, v), = (rec.get("expect") or expect).items()
    if (rec["value"] <= 0) if bound == "at_most" else (rec["value"] >= 1):
        return ""
    return f"{_pct(rec['value'])}, " + (f"at most {_pct(v)} allowed" if bound == "at_most" else f"at least {_pct(v)} needed")


def status(bench: dict) -> Dict[str, Dict[str, dict]]:
    """Per dataset, each control the benchmark declares - its source, where it
    comes from, its bound and what it must score - and whether it is proven on
    that dataset, with the record and how far short of the full mark it is
    (``short_of``); then the units and leak checks the same way. ``failed``:
    the last attempt that did not pass, while none has since. What a page shows."""
    out = {}
    controls = bench.get("controls") or {}
    for ds in bench["datasets"]:
        rec = (bench.get("proven") or {}).get(ds["id"]) or {}
        failed = rec.get("failed") if isinstance(rec.get("failed"), dict) else {}
        row = {}
        for c in definitions.CONTROLS:
            if c in controls:
                d = definitions.control(bench, c)
                row[c] = {"source": d["source"], "from": d["from"], "expect": d["expect"], "must": must_score(d["expect"]),
                          "proven": rec.get(c), "short": short_of(rec.get(c), d["expect"]), "failed": failed.get(c)}
        for c in ("units", "leak"):
            row[c] = {"proven": rec.get(c), "failed": failed.get(c)}
        out[ds["id"]] = row
    return out
