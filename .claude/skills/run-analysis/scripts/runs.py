#!/usr/bin/env python3
"""Health-check, analyse and render Harbor job output (the folders Nullius writes).

Benchmark- and agent-agnostic. Reads only the standard Harbor trial files:
  <trial>/result.json            task name, reward(s), exception, phase timings, token totals, cost
  <trial>/agent/trajectory.json  optional ATIF trajectory: steps, tool calls, per-call usage
and the optional Nullius run wrapper files next to `jobs/` (run-*.exit, run-*.log).

Subcommands
  health  PATH [PATH...]                      triage one or more runs: did they run, what failed, when
  analyze PATH [PATH...] [--label NAME ...]   metrics for one run or several -> metrics.json (+ readable summary)
  render  metrics.json --out page.html        analysis page from the bundled template, prose slots left open
  check   page.html                           refuse a page that still has open slots or undefined short forms

Standard library only.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import json
import math
import os
import re
import statistics as st
import sys
import tempfile
import zipfile



def load(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except Exception:
        return None


def ts(value):
    """Parse the ISO timestamps Harbor writes (with or without a zone); naive result in UTC."""
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t


def span(block):
    if not isinstance(block, dict):
        return None
    a, b = ts(block.get("started_at")), ts(block.get("finished_at"))
    return (b - a).total_seconds() if a and b else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def median(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    return xs[int(q * (len(xs) - 1))] if xs else None


def stderr_of_rate(p, n):
    return math.sqrt(p * (1 - p) / n) if n and p is not None else None




def materialise(path, workdir=None):
    """A zip is extracted to a temporary folder; a folder is used as is."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        target = tempfile.mkdtemp(prefix="run-", dir=workdir)
        with zipfile.ZipFile(path) as z:
            z.extractall(target)
        return target
    return path


def find_jobs(root, max_depth=6):
    """Job directories under root: a directory whose children are trial directories.

    A trial directory holds a result.json with a `task_name`. Returns {job_dir: [trial_dirs]}.
    """
    jobs = collections.defaultdict(list)
    root = root.rstrip(os.sep)
    base_depth = root.count(os.sep)
    for cur, dirs, files in os.walk(root):
        if cur.count(os.sep) - base_depth > max_depth:
            dirs[:] = []
            continue
        if "result.json" in files:
            data = load(os.path.join(cur, "result.json"))
            if isinstance(data, dict) and "task_name" in data:
                jobs[os.path.dirname(cur)].append(cur)
                dirs[:] = []  # nothing of interest below a trial
    return dict(jobs)


def trial_dirs_without_result(job_dir, known):
    known = set(known)
    out = []
    for name in sorted(os.listdir(job_dir)):
        p = os.path.join(job_dir, name)
        if os.path.isdir(p) and p not in known and any(
            os.path.exists(os.path.join(p, f)) for f in ("config.json", "trial.log", "exception.txt")
        ):
            out.append(p)
    return out


def pick_job(path, job_name=None, workdir=None):
    root = materialise(path, workdir)
    jobs = find_jobs(root)
    if not jobs:
        raise SystemExit(f"no Harbor trials found under {path} (a trial has result.json with task_name)")
    if job_name:
        match = [j for j in jobs if os.path.basename(j) == job_name]
        if not match:
            raise SystemExit(f"job {job_name!r} not found under {path}; found: {[os.path.basename(j) for j in jobs]}")
        return match[0], jobs[match[0]], root
    if len(jobs) > 1:
        listing = ", ".join(f"{os.path.basename(j)} ({len(t)} trials)" for j, t in sorted(jobs.items()))
        raise SystemExit(f"{path} holds several jobs: {listing}. Name one with PATH::JOB_NAME.")
    job = next(iter(jobs))
    return job, jobs[job], root


def split_spec(spec):
    if "::" in spec:
        p, j = spec.split("::", 1)
        return p, j
    return spec, None



ERROR_PATTERNS = [
    re.compile(r'"message"\s*:\s*"([^"]{8,220})"'),
    re.compile(r"\b([A-Za-z_.]*(?:Error|Exception)): ([^\n]{0,220})"),
    re.compile(r"(?m)^(?:Error|ERROR|error)[: ]\s*([^\n]{4,220})"),
]
VOLATILE = re.compile(r"\d+\.\d+|\b[0-9a-f]{8,}\b|\b\d{4,}\b|gen-[\w-]+|req_[\w-]+|user_[\w-]+")


SECRETS = re.compile(r"sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}|(?i:api[_-]?key|token|secret|password)[\"'=: ]+[A-Za-z0-9._-]{8,}")


def short_error(message, normalise=True):
    """One line out of a long exception message.

    With normalise=True volatile numbers and ids are masked so identical failures group together;
    with normalise=False the line is kept as written (secrets masked) to show the exact text once.
    """
    if not message:
        return ""
    text = str(message)
    tail = text[-6000:]
    best = ""
    for pattern in ERROR_PATTERNS:
        found = pattern.findall(tail)
        if found:
            last = found[-1]
            best = ": ".join(last) if isinstance(last, tuple) else last
            break
    if not best:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        best = lines[-1] if lines else text
    best = SECRETS.sub("[redacted]", best)
    if normalise:
        best = VOLATILE.sub("#", best)
    return re.sub(r"\s+", " ", best)[:220]


def primary_reward(result):
    rewards = ((result.get("verifier_result") or {}).get("rewards")) or {}
    if not isinstance(rewards, dict) or not rewards:
        return None, {}
    numeric = {k: v for k, v in rewards.items() if isinstance(v, (int, float))}
    if "reward" in numeric:
        return float(numeric["reward"]), numeric
    if numeric:
        first = next(iter(numeric))
        return float(numeric[first]), numeric
    return None, {}


CONTROL_AGENTS = {"nop", "oracle"}
LOG_BOILERPLATE = ("harbor view", "harbor upload", "Results written to", "Inspect results", "Share results", "result.json")
TOOL_ERROR_HINTS = ("success=False", '"isError": true', '"is_error": true', "Traceback (most recent call last)")


def read_trajectory(trial_dir, ignore_tool=None):
    """Generic ATIF reader. Everything is optional; missing pieces come back as None/empty."""
    tj = load(os.path.join(trial_dir, "agent", "trajectory.json"))
    if not isinstance(tj, dict) or not isinstance(tj.get("steps"), list):
        return None
    steps = tj["steps"]
    tools = collections.Counter()
    tool_errors = 0
    agent_times = []
    per_call_in, per_call_uncached = [], []
    n_agent_steps = 0
    last_error, last_error_step = None, -10
    for step_no, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        if s.get("source") == "agent":
            n_agent_steps += 1
            t = ts(s.get("timestamp"))
            if t:
                agent_times.append(t)
            m = s.get("metrics") or {}
            if isinstance(m, dict) and m.get("prompt_tokens") is not None:
                pin = m.get("prompt_tokens") or 0
                per_call_in.append(pin)
                per_call_uncached.append(pin - (m.get("cached_tokens") or 0))
        step_calls = [c for c in (s.get("tool_calls") or []) if isinstance(c, dict)]
        kept = 0
        for call in step_calls:
            tool_name = str(call.get("function_name") or call.get("name") or "?")
            if ignore_tool and ignore_tool.search(tool_name):
                continue
            kept += 1
            tools[tool_name] += 1
            status = ((call.get("extra") or {}).get("status") or "").lower()
            if status in ("failed", "error"):
                tool_errors += 1
        for r in ((s.get("observation") or {}).get("results")) or []:
            content = str((r or {}).get("content") or "")
            if any(h in content for h in TOOL_ERROR_HINTS):
                # an agent that logs a wrapper call and the real call repeats the same error on
                # neighbouring steps; count it once
                key = content[:300]  # the two records share their start and differ in trailing metadata
                if key == last_error and step_no - last_error_step <= 2:
                    last_error_step = step_no
                    continue
                last_error, last_error_step = key, step_no
                tool_errors += 1
    # Some agents record one usage entry per model call outside the steps.
    raw = ((tj.get("extra") or {}).get("raw_usage")) or []
    raw = [u for u in raw if isinstance(u, dict)] if isinstance(raw, list) else []
    if not per_call_in and raw:
        for u in raw:
            if u.get("input_tokens") is not None:
                pin = u.get("input_tokens") or 0
                cached = u.get("cache_read_tokens")
                if cached is None:
                    cached = u.get("cache_tokens") or 0
                per_call_in.append(pin)
                per_call_uncached.append(pin - cached)
    n_calls = max(len(raw), len(per_call_in)) or (n_agent_steps or None)
    gaps = [(b - a).total_seconds() for a, b in zip(agent_times, agent_times[1:]) if (b - a).total_seconds() > 0]
    agent = tj.get("agent") or {}
    return dict(
        n_steps=len(steps), n_agent_steps=n_agent_steps, n_model_calls=n_calls,
        calls_exact=bool(per_call_in or raw), tools=dict(tools), n_tool_calls=sum(tools.values()),
        tool_errors=tool_errors, step_gaps=gaps, per_call_in=per_call_in, per_call_uncached=per_call_uncached,
        agent_name=agent.get("name"), agent_version=agent.get("version"), model=agent.get("model_name"),
    )


def read_trial(trial_dir, pass_threshold, ignore_tool=None):
    r = load(os.path.join(trial_dir, "result.json")) or {}
    reward, rewards = primary_reward(r)
    exc = r.get("exception_info") or {}
    ar = r.get("agent_result") or {}
    traj = read_trajectory(trial_dir, ignore_tool)
    info = r.get("agent_info") or {}
    cfg_agent = ((r.get("config") or {}).get("agent")) or {}
    model = info.get("model_info") or {}
    return dict(
        dir=trial_dir, trial=os.path.basename(trial_dir), task=r.get("task_name") or os.path.basename(trial_dir),
        reward=reward, rewards=rewards, passed=(reward is not None and reward >= pass_threshold),
        exc=exc.get("exception_type"),
        exc_short=(short_error(exc.get("exception_message")) or str(exc.get("exception_type") or "")) if exc else "",
        exc_raw=short_error(exc.get("exception_message"), normalise=False) if exc else "",
        started=r.get("started_at"), finished=r.get("finished_at"), total_s=span(r),
        env_setup_s=span(r.get("environment_setup")), agent_setup_s=span(r.get("agent_setup")),
        exec_s=span(r.get("agent_execution")), verifier_s=span(r.get("verifier")),
        in_tok=ar.get("n_input_tokens"), out_tok=ar.get("n_output_tokens"), cache_tok=ar.get("n_cache_tokens"),
        cost=ar.get("cost_usd"),
        agent=info.get("name") or cfg_agent.get("name") or (traj or {}).get("agent_name"),
        agent_version=info.get("version") or (traj or {}).get("agent_version"),
        model=(model.get("name") if isinstance(model, dict) else None) or cfg_agent.get("model_name") or (traj or {}).get("model"),
        traj=traj,
    )




def short_task(name):
    return str(name).split("/")[-1].split("__")[-1]


def make_grouper(task_names, regex=None, max_groups=12):
    """Group tasks for the per-group breakdown.

    With --group-regex the first capture group (or whole match) is the group. Otherwise strip the
    prefix all task names share and use the deepest dash-separated prefix that still gives at most
    `max_groups` groups; one group ("all") when nothing sensible exists.
    """
    shorts = {t: short_task(t) for t in task_names}
    if regex:
        rx = re.compile(regex)

        def by_regex(t):
            m = rx.search(shorts[t])
            if not m:
                return "other"
            return m.group(1) if m.groups() else m.group(0)

        return by_regex, "regex"
    values = list(shorts.values())
    common = os.path.commonprefix(values) if len(values) > 1 else ""
    common = common[: common.rfind("-") + 1] if "-" in common else ""
    stripped = {t: s[len(common):] for t, s in shorts.items()}
    best = None
    for depth in (1, 2, 3):
        groups = {t: "-".join(re.split(r"-", s)[:depth]) for t, s in stripped.items()}
        n = len(set(groups.values()))
        if 2 <= n <= max_groups:
            best = groups
    if best is None:
        return (lambda t: "all"), "single"
    return (lambda t: best[t]), "name-prefix"




def wrapper_files(root):
    """Nullius writes run-<part>.exit / .log / .sh next to jobs/. Report what is there."""
    found = []
    for cur, dirs, files in os.walk(root):
        if cur.count(os.sep) - root.rstrip(os.sep).count(os.sep) > 3:
            dirs[:] = []
            continue
        for f in files:
            if f.endswith(".exit") or (f.startswith("run") and f.endswith(".log")):
                found.append(os.path.join(cur, f))
    out = {}
    for p in sorted(found):
        stem = os.path.basename(p).rsplit(".", 1)[0]
        entry = out.setdefault(stem, {})
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        if p.endswith(".exit"):
            entry["exit"] = text.strip()
        else:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            entry["log_lines"] = len(lines)
            useful = [ln for ln in lines if not any(b in ln for b in LOG_BOILERPLATE) and not ln.strip().startswith("/")]
            entry["log_tail"] = (useful or lines)[-8:]
    return out


def health_one(path, pass_threshold, workdir=None):
    root = materialise(path, workdir)
    report = dict(path=path, wrappers=wrapper_files(root), jobs=[], verdicts=[])
    jobs = find_jobs(root)
    for name, w in report["wrappers"].items():
        if w.get("exit") not in (None, "", "0"):
            report["verdicts"].append(f"{name}: exit code {w['exit']}")
        if "exit" not in w and w.get("log_lines", 0) == 0:
            report["verdicts"].append(f"{name}: no exit file and an empty log — stopped or still running")
    if not jobs:
        report["verdicts"].append("no trials ran: there is no job output. Read the wrapper log above for the cause.")
        return report
    for job_dir, trials in sorted(jobs.items()):
        rows = [read_trial(t, pass_threshold) for t in trials]
        orphans = trial_dirs_without_result(job_dir, trials)
        job_result = load(os.path.join(job_dir, "result.json")) or {}
        stats = job_result.get("stats") or {}
        n = len(rows)
        exc_types = collections.Counter(r["exc"] for r in rows if r["exc"])
        exc_msgs = collections.Counter(r["exc_short"] for r in rows if r["exc"])
        scored = [r for r in rows if r["reward"] is not None]
        zero_tok = sum(1 for r in rows if not (r["in_tok"] or 0) and not (r["out_tok"] or 0))
        starts = sorted(x for x in (ts(r["started"]) for r in rows) if x)
        ends = sorted(x for x in (ts(r["finished"]) for r in rows) if x)
        job = dict(
            job=os.path.basename(job_dir), dir=job_dir, trials=n, trial_dirs_without_result=len(orphans),
            n_total_expected=job_result.get("n_total_trials"), job_finished_at=job_result.get("finished_at"),
            scored=len(scored), passed=sum(1 for r in rows if r["passed"]),
            reward_mean=mean([r["reward"] if r["reward"] is not None else 0.0 for r in rows]) if rows else None,
            exceptions=dict(exc_types), top_exception_messages=exc_msgs.most_common(5),
            trials_with_no_tokens=zero_tok, first_start=str(starts[0]) if starts else None,
            last_finish=str(ends[-1]) if ends else None,
            window_minutes=((ends[-1] - starts[0]).total_seconds() / 60) if starts and ends else None,
            trial_seconds_median=median([r["total_s"] for r in rows]),
            agent_seconds_median=median([r["exec_s"] for r in rows]),
            error_examples={msg: next((r["exc_raw"] for r in rows if r["exc_short"] == msg and r["exc_raw"]), "") for msg, _ in exc_msgs.most_common(5)},
            cancelled=stats.get("n_cancelled_trials"), agents=sorted({str(r["agent"]) for r in rows if r["agent"]}),
            models=sorted({str(r["model"]) for r in rows if r["model"]}),
        )
        verdicts = []
        if n and sum(exc_types.values()) == n:
            verdicts.append("every trial raised an exception")
        if exc_msgs:
            msg, count = exc_msgs.most_common(1)[0]
            if count >= max(3, 0.5 * sum(exc_types.values())) and count >= 0.2 * n:
                verdicts.append(f"systemic failure: {count}/{n} trials share one error: {msg}")
                failing = sorted((ts(r["started"]), r) for r in rows if r["exc_short"] == msg and ts(r["started"]))
                ok = sorted(ts(r["started"]) for r in rows if not r["exc"] and ts(r["started"]))
                if failing and ok and ok[0] < failing[0][0]:
                    verdicts.append(
                        f"it started mid-run: first such failure at {failing[0][0]} UTC, "
                        f"{sum(1 for t in ok if t < failing[0][0])} trials had started cleanly before it"
                    )
                elif failing:
                    verdicts.append("it was there from the first trial: nothing ran cleanly before it")
        control = bool(rows) and all(str(r["agent"]).lower() in CONTROL_AGENTS for r in rows)
        if control:
            verdicts.append("control agent: it uses no model by design; keep it out of agent comparisons")
        elif zero_tok and zero_tok >= 0.5 * n:
            verdicts.append(f"{zero_tok}/{n} trials consumed no model tokens: the model was never reached")
        if job_result and not job_result.get("finished_at"):
            verdicts.append("the job never wrote a finish time: it was stopped or is still running")
        expected = job_result.get("n_total_trials")
        if expected and n < expected:
            verdicts.append(f"incomplete: {n} of {expected} trials have results")
        if orphans:
            verdicts.append(f"{len(orphans)} trial folders have no result.json (cancelled or in flight)")
        n_err, n_unscored = sum(exc_types.values()), n - len(scored)
        if n and n_err / n > 0.05 and n_err < n and not any(v.startswith("systemic") for v in verdicts):
            verdicts.append(f"{n_err}/{n} trials errored ({100 * n_err / n:.0f}%) with no single dominant cause: read the messages above")
        if n_unscored:
            verdicts.append(f"{n_unscored} trials have no score")
        if not verdicts:
            verdicts.append("healthy at the job level: all trials scored, no dominant failure")
        job["verdicts"] = verdicts
        report["jobs"].append(job)
    return report


def cmd_health(args):
    reports = [health_one(p, args.pass_threshold, args.workdir) for p in args.paths]
    if args.json:
        json.dump(reports, sys.stdout, indent=1, default=str)
        print()
        return
    for rep in reports:
        print(f"== {rep['path']}")
        for name, w in rep["wrappers"].items():
            print(f"   wrapper {name}: exit={w.get('exit', 'missing')} log_lines={w.get('log_lines', 0)}")
            for line in (w.get("log_tail") or [])[-5:]:
                print(f"      | {line[:160]}")
        for v in rep["verdicts"]:
            print(f"   ! {v}")
        for j in rep["jobs"]:
            rm = j["reward_mean"]
            print(f"   job {j['job']}: {j['trials']} trials, {j['scored']} scored, {j['passed']} passed, "
                  f"mean reward {rm:.3f}" if rm is not None else f"   job {j['job']}: {j['trials']} trials")
            print(f"      agent={j['agents']} model={j['models']}")
            wm, tm, am = j["window_minutes"], j["trial_seconds_median"], j["agent_seconds_median"]
            print(f"      window (UTC) {str(j['first_start'])[:19]} -> {str(j['last_finish'])[:19]}"
                  + (f" = {wm:.0f} min" if wm is not None else "")
                  + (f"; median trial {tm:.0f} s" if tm is not None else "")
                  + (f", of which agent execution {am:.0f} s" if am is not None else ""))
            if j["exceptions"]:
                print(f"      exceptions: {j['exceptions']}")
                for msg, c in j["top_exception_messages"]:
                    print(f"        {c:4d} x {msg[:150]}")
                    example = j["error_examples"].get(msg)
                    if example and example != msg:
                        print(f"               e.g. {example[:200]}")
            for v in j["verdicts"]:
                print(f"      -> {v}")




def summarise(rows, grouper, pass_threshold):
    by_task = collections.defaultdict(list)
    for r in rows:
        by_task[r["task"]].append(r)
    n_tasks = len(by_task)
    rewards = [(r["reward"] if r["reward"] is not None else 0.0) for r in rows]
    task_rate = {t: mean([1.0 if x["passed"] else 0.0 for x in v]) for t, v in by_task.items()}
    p = mean(list(task_rate.values()))
    trajs = [r["traj"] for r in rows if r["traj"]]
    calls = [t["n_model_calls"] for t in trajs if t["n_model_calls"]]
    tool_counts = [t["n_tool_calls"] for t in trajs]
    used = [r for r in rows if (r["in_tok"] or 0) > 0 or (r["out_tok"] or 0) > 0] or rows
    tin = sum(r["in_tok"] or 0 for r in rows)
    tcache = sum(r["cache_tok"] or 0 for r in rows)
    total_calls = sum(calls)
    per_in = [v for t in trajs for v in t["per_call_in"]]
    per_unc = [v for t in trajs for v in t["per_call_uncached"]]
    gaps = [g for t in trajs for g in t["step_gaps"]]
    tools = collections.Counter()
    for t in trajs:
        tools.update(t["tools"])
    excs = collections.Counter(r["exc"] for r in rows if r["exc"])
    exc_msgs = collections.Counter(r["exc_short"] for r in rows if r["exc"])
    starts = sorted(x for x in (ts(r["started"]) for r in rows) if x)
    ends = sorted(x for x in (ts(r["finished"]) for r in rows) if x)
    groups = collections.defaultdict(list)
    for r in rows:
        groups[grouper(r["task"])].append(r)
    extra_metrics = collections.defaultdict(list)
    for r in rows:
        for k, v in r["rewards"].items():
            extra_metrics[k].append(v)
    return dict(
        trials=len(rows), tasks=n_tasks, attempts_per_task=round(len(rows) / n_tasks, 2) if n_tasks else None,
        reward_mean=mean(rewards), pass_rate=p, tasks_always_passed=sum(1 for v in task_rate.values() if v == 1.0),
        trials_passed=sum(1 for r in rows if r["passed"]),
        standard_error=stderr_of_rate(p, n_tasks),
        reward_metrics={k: mean(v) for k, v in extra_metrics.items()},
        errored=sum(excs.values()), exception_types=dict(excs), exception_messages=exc_msgs.most_common(6),
        unscored=sum(1 for r in rows if r["reward"] is None),
        trials_without_usage=len(rows) - len(used) if used is not rows else 0,
        in_tok_mean=mean([r["in_tok"] for r in used]), in_tok_median=median([r["in_tok"] for r in used]),
        out_tok_mean=mean([r["out_tok"] for r in used]), out_tok_median=median([r["out_tok"] for r in used]),
        cache_share=(tcache / tin) if tin else None,
        cost_mean=mean([r["cost"] for r in used]), cost_total=sum(r["cost"] or 0 for r in rows) if any(r["cost"] for r in rows) else None,
        env_setup_mean=mean([r["env_setup_s"] for r in rows]), agent_setup_mean=mean([r["agent_setup_s"] for r in rows]),
        exec_mean=mean([r["exec_s"] for r in rows]), exec_median=median([r["exec_s"] for r in rows]),
        exec_max=max([r["exec_s"] for r in rows if r["exec_s"] is not None], default=None),
        verifier_mean=mean([r["verifier_s"] for r in rows]), total_mean=mean([r["total_s"] for r in rows]),
        trajectories=len(trajs), calls_exact=bool(per_in),
        model_calls_mean=mean(calls), model_calls_median=median(calls),
        in_tok_per_call_mean=(tin / total_calls) if total_calls else None,
        uncached_per_call_mean=((tin - tcache) / total_calls) if total_calls else None,
        in_tok_per_call_median=median(per_in), uncached_per_call_median=median(per_unc),
        tool_calls_mean=mean(tool_counts), tool_calls_median=median(tool_counts),
        top_tools=tools.most_common(15), tool_errors=sum(t["tool_errors"] for t in trajs),
        trials_with_tool_errors=sum(1 for t in trajs if t["tool_errors"]),
        step_gap_median=median(gaps), step_gap_p90=pct(gaps, 0.9), step_gap_max=max(gaps, default=None),
        first_start=str(starts[0]) if starts else None, last_finish=str(ends[-1]) if ends else None,
        wall_hours=((ends[-1] - starts[0]).total_seconds() / 3600) if starts and ends else None,
        agents=sorted({str(r["agent"]) for r in rows if r["agent"]}),
        agent_versions=sorted({str(r["agent_version"]) for r in rows if r["agent_version"]}),
        models=sorted({str(r["model"]) for r in rows if r["model"]}),
        groups={g: dict(
            tasks=len({r["task"] for r in v}), trials=len(v),
            pass_rate=mean([1.0 if r["passed"] else 0.0 for r in v]),
            standard_error=stderr_of_rate(mean([1.0 if r["passed"] else 0.0 for r in v]), len({r["task"] for r in v})),
            errored=sum(1 for r in v if r["exc"]), in_tok_mean=mean([r["in_tok"] for r in v if (r["in_tok"] or 0) > 0]),
            exec_mean=mean([r["exec_s"] for r in v]), cost_total=sum(r["cost"] or 0 for r in v),
        ) for g, v in groups.items()},
        task_rate=task_rate, task_errored={t: any(x["exc"] for x in v) for t, v in by_task.items()},
    )


def flatten(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(flatten(v, f"{prefix}{i}."))
    else:
        out[prefix[:-1]] = d
    return out


def config_diff(job_dirs):
    """Config keys whose values differ between the runs: the variables the comparison actually tests."""
    configs = [load(os.path.join(j, "config.json")) or {} for j in job_dirs]
    flats = [flatten(c) for c in configs]
    skip = ("job_name", "jobs_dir")
    diff = {}
    for k in sorted(set().union(*[set(f) for f in flats])) if flats else []:
        if any(k.startswith(s) for s in skip):
            continue
        values = [f.get(k) for f in flats]
        if any(v != values[0] for v in values[1:]):
            diff[k] = values
    return diff, configs


def cmd_analyze(args):
    labels = list(args.label or [])
    runs, job_dirs, rates, errored = [], [], [], []
    all_rows = []
    ignore = re.compile(args.ignore_tool) if args.ignore_tool else None
    for spec in args.runs:
        path, job_name = split_spec(spec)
        job, trials, _ = pick_job(path, job_name, args.workdir)
        job_dirs.append(job)
        all_rows.append([read_trial(t, args.pass_threshold, ignore) for t in trials])
    all_tasks = sorted({r["task"] for rows in all_rows for r in rows})
    grouper, group_method = make_grouper(all_tasks, args.group_regex)
    for i, rows in enumerate(all_rows):
        s = summarise(rows, grouper, args.pass_threshold)
        rates.append(s.pop("task_rate"))
        errored.append(s.pop("task_errored"))
        label = labels[i] if i < len(labels) else os.path.basename(job_dirs[i])
        runs.append(dict(label=label, job=os.path.basename(job_dirs[i]), dir=job_dirs[i], **s))
    n = len(runs)
    out = dict(
        generated=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), title=args.title or "",
        pass_threshold=args.pass_threshold, group_method=group_method, runs=runs,
        groups=sorted({g for r in runs for g in r["groups"]}),
        solved_rule="a task counts as solved when at least half of its trials pass",
    )
    if n >= 2:
        common = sorted(set.intersection(*[set(r) for r in rates]))
        solved = [{t for t in common if rate[t] >= 0.5} for rate in rates]
        union = set.union(*solved) if solved else set()
        inter = set.intersection(*solved) if solved else set()
        out["overlap"] = dict(
            common_tasks=len(common), tasks_not_in_every_run=len(all_tasks) - len(common),
            solved_by_all=len(inter), solved_by_none=len(set(common) - union), solved_by_some=len(union - inter),
            unique_solves=[len(solved[i] - set.union(*[solved[j] for j in range(n) if j != i])) for i in range(n)],
            by_group={g: dict(solved_by_all=sum(1 for t in common if grouper(t) == g and t in inter),
                              solved_by_none=sum(1 for t in common if grouper(t) == g and t not in union),
                              solved_by_some=sum(1 for t in common if grouper(t) == g and t in union and t not in inter))
                      for g in sorted({grouper(t) for t in common})},
            solved_by_none_tasks=[short_task(t) for t in sorted(set(common) - union)],
        )
        base = runs[0]
        out["gaps"] = []
        for r in runs[1:]:
            gap = (r["pass_rate"] or 0) - (base["pass_rate"] or 0)
            se = max(x for x in (r["standard_error"], base["standard_error"], 1e-9) if x is not None)
            out["gaps"].append(dict(run=r["label"], versus=base["label"], gap=gap, in_standard_errors=abs(gap) / se))
        out["pairs"] = []
        for i in range(n):
            for j in range(i + 1, n):
                i_only, j_only = sorted(solved[i] - solved[j]), sorted(solved[j] - solved[i])
                out["pairs"].append(dict(
                    first=runs[i]["label"], second=runs[j]["label"], both=len(solved[i] & solved[j]),
                    first_only=len(i_only), second_only=len(j_only), neither=len(set(common) - solved[i] - solved[j]),
                    first_only_where_second_errored=sum(1 for t in i_only if errored[j].get(t)),
                    second_only_where_first_errored=sum(1 for t in j_only if errored[i].get(t)),
                    first_only_tasks=[short_task(t) for t in i_only], second_only_tasks=[short_task(t) for t in j_only],
                    by_group={g: dict(first_only=sum(1 for t in i_only if grouper(t) == g),
                                      second_only=sum(1 for t in j_only if grouper(t) == g))
                              for g in sorted({grouper(t) for t in common})},
                ))
        out["group_gaps"] = {}
        for g in out["groups"]:
            b0 = base["groups"].get(g)
            if not b0:
                continue
            out["group_gaps"][g] = []
            for r in runs[1:]:
                rg = r["groups"].get(g)
                if not rg:
                    continue
                gse = max(x for x in (rg["standard_error"], b0["standard_error"], 1e-9) if x is not None)
                ggap = (rg["pass_rate"] or 0) - (b0["pass_rate"] or 0)
                out["group_gaps"][g].append(dict(run=r["label"], versus=base["label"], gap=ggap, in_standard_errors=abs(ggap) / gse))
        diff, configs = config_diff(job_dirs)
        out["config_diff"] = diff
    else:
        configs = [load(os.path.join(job_dirs[0], "config.json")) or {}]
    out["concurrency"] = [c.get("n_concurrent_trials") for c in configs]
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, default=str)

    fmt = lambda v: "n/a" if v is None else (f"{v:,.3f}" if isinstance(v, float) and abs(v) < 10 else f"{v:,.0f}" if isinstance(v, (int, float)) else str(v))
    keys = ["trials", "tasks", "attempts_per_task", "pass_rate", "standard_error", "trials_passed", "errored", "unscored",
            "trials_without_usage", "model_calls_mean", "model_calls_median", "in_tok_per_call_mean", "in_tok_per_call_median",
            "uncached_per_call_mean", "in_tok_mean", "in_tok_median", "out_tok_mean", "cache_share", "tool_calls_mean",
            "tool_errors", "cost_mean", "cost_total", "agent_setup_mean", "exec_mean", "exec_median", "exec_max",
            "step_gap_median", "step_gap_p90", "wall_hours", "trajectories"]
    print(f"wrote {args.out}\n")
    print(f"{'metric':28s} " + " ".join(f"{r['label'][:20]:>20s}" for r in runs))
    for k in keys:
        print(f"{k:28s} " + " ".join(f"{fmt(r.get(k)):>20s}" for r in runs))
    for r in runs:
        print(f"\n[{r['label']}] exceptions: {r['exception_types'] or 'none'}")
        for msg, c in r["exception_messages"][:4]:
            print(f"   {c:4d} x {msg[:150]}")
        if r["top_tools"]:
            print("   top tools: " + ", ".join(f"{name} x{count}" for name, count in r["top_tools"][:8]))
        if r["trials_without_usage"]:
            print(f"   {r['trials_without_usage']} trials report no token usage: token and cost means exclude them, totals are a lower bound")
    print(f"\ngroups ({group_method}):")
    for g in out["groups"]:
        cells = " ".join(f"{fmt(r['groups'].get(g, {}).get('pass_rate')):>8s}" for r in runs)
        tasks = next((r["groups"][g]["tasks"] for r in runs if g in r["groups"]), "")
        gaps = " ".join(f"[{x['in_standard_errors']:.1f} standard errors]" for x in out.get("group_gaps", {}).get(g, []))
        print(f"  {g[:34]:34s} tasks={tasks!s:>4} {cells}   errors " + "/".join(str(r["groups"].get(g, {}).get("errored", 0)) for r in runs) + "  " + gaps)
    if n >= 2:
        for g in out["gaps"]:
            print(f"\ngap {g['run']} - {g['versus']} = {g['gap']:+.3f} = {g['in_standard_errors']:.1f} standard errors of one run")
        ov = out["overlap"]
        print(f"overlap on {ov['common_tasks']} common tasks: solved by all {ov['solved_by_all']}, by some {ov['solved_by_some']}, "
              f"by none {ov['solved_by_none']}; unique solves {ov['unique_solves']}")
        for p in out["pairs"]:
            print(f"pair {p['first']} / {p['second']}: both={p['both']} first only={p['first_only']} second only={p['second_only']} neither={p['neither']}")
        if out["config_diff"]:
            print("\nconfig differences (what this comparison varies):")
            for k, values in list(out["config_diff"].items())[:40]:
                print(f"  {k}: " + " | ".join(repr(str(v)[:48]) for v in values))
        if ov["tasks_not_in_every_run"]:
            print(f"\nWARNING: {ov['tasks_not_in_every_run']} tasks are not present in every run. Rates use each run's own tasks; "
                  "overlap and pairs use the common tasks only.")



E = html.escape


def n_tok(v):
    if v is None:
        return "n/a"
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e4:
        return f"{v / 1e3:.0f}k"
    if v >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{v:.0f}"


def num(v, digits=0):
    return "n/a" if v is None else f"{v:,.{digits}f}"


def money(v, digits=2):
    return "n/a" if v is None else f"${v:,.{digits}f}"


def flags(values, higher_is_better, rel=0.10):
    """Mark the values that are clearly worse than the best one (by more than `rel`, relative)."""
    known = [v for v in values if v is not None]
    if len(known) < 2:
        return [""] * len(values)
    best = max(known) if higher_is_better else min(known)
    scale = max(abs(v) for v in known) or 1.0
    return [" worse" if v is not None and abs(v - best) / scale > rel else "" for v in values]


def chart_svg(groups, runs, references):
    x0, width, y = 150, 670, 30
    body = []
    for g in groups:
        bars = [(f"c{i}", r["label"], r["groups"].get(g, {}).get("pass_rate")) for i, r in enumerate(runs)]
        bars += [("ref", ref["label"], ref["value"]) for ref in references if ref["group"] == g]
        label_y = y + (len(bars) * 15) // 2 + 4
        body.append(f'<text x="{x0 - 12}" y="{label_y}" text-anchor="end">{E(g[:22])}</text>')
        for cls, name, val in bars:
            if val is not None:
                w = round(val * width)
                body.append(f'<rect class="bar {cls}" x="{x0}" y="{y}" width="{max(w, 1)}" height="13" rx="2">'
                            f'<title>{E(g)} {E(name)} {val:.2f}</title></rect>'
                            f'<text class="val" x="{x0 + w + 6}" y="{y + 10}">{val:.2f}</text>')
            y += 15
        y += 16
    bottom = y - 8
    grid = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        x = round(x0 + frac * width)
        grid.append(f'<line class="grid" x1="{x}" y1="22" x2="{x}" y2="{bottom}"/>'
                    f'<text x="{x}" y="{bottom + 18}" text-anchor="middle">{frac:.2f}</text>')
    grid.append(f'<line class="axis" x1="{x0}" y1="22" x2="{x0}" y2="{bottom}"/>')
    names = ", ".join(r["label"] for r in runs)
    return (f'<svg viewBox="0 0 880 {bottom + 26}" role="img" aria-label="Pass rate by task group: {E(names)}">'
            + "".join(grid) + "".join(body) + "</svg>")


def slot(name, hint):
    return f'<!--SLOT:{name}--><p class="slot">SLOT TODO ({E(name)}): {E(hint)}</p><!--/SLOT:{name}-->'


def cmd_render(args):
    m = load(args.metrics)
    if not m or "runs" not in m:
        raise SystemExit(f"cannot read run metrics from {args.metrics}")
    runs = m["runs"]
    n = len(runs)
    template_path = args.template or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "template.html")
    with open(template_path, encoding="utf-8") as fh:
        page = fh.read()
    references = []
    for spec in args.reference or []:
        parts = spec.split(":")
        if len(parts) != 3:
            raise SystemExit("--reference takes GROUP:LABEL:VALUE, e.g. checkout:published:0.505")
        references.append(dict(group=parts[0], label=parts[1], value=float(parts[2])))

    sw = lambda i: f'<span class="sw c{i}"></span>'
    head = "".join(f"<th>{sw(i)}{E(r['label'])}</th>" for i, r in enumerate(runs))
    get = lambda key: [r.get(key) for r in runs]

    def row(label, cells, marks=None):
        marks = marks or [""] * n
        return f"<tr><td>{label}</td>" + "".join(f'<td class="num{k}">{c}</td>' for c, k in zip(cells, marks)) + "</tr>"

    def duo(k1, k2, f):
        return [f"{f(r.get(k1))} · {f(r.get(k2))}" for r in runs]

    one = lambda v: num(v, 1)
    pc = lambda v: "n/a" if v is None else f"{v * 100:.0f}%"
    decisive = any((g.get("in_standard_errors") or 0) >= 2 for g in m.get("gaps", []))
    rows = [
        row("pass rate over all tasks (failed and errored trials count as 0)", [num(v, 3) for v in get("pass_rate")],
            flags(get("pass_rate"), True, rel=0.0) if decisive else None),
        row("standard error of that rate", [num(v, 3) for v in get("standard_error")]),
        row("trials passed / trials", [f'{r["trials_passed"]} / {r["trials"]}' for r in runs]),
        row("errored trials", [str(v) for v in get("errored")], flags(get("errored"), False, rel=0.0)),
        row("model calls / trial (mean · median)", duo("model_calls_mean", "model_calls_median", one), flags(get("model_calls_mean"), False)),
        row("input tokens / model call (mean · median)", duo("in_tok_per_call_mean", "in_tok_per_call_median", n_tok)),
        row("uncached input tokens / model call (mean · median)", duo("uncached_per_call_mean", "uncached_per_call_median", n_tok),
            flags(get("uncached_per_call_mean"), False)),
        row("input tokens / trial (mean · median)", duo("in_tok_mean", "in_tok_median", n_tok)),
        row("cache share of input", [pc(v) for v in get("cache_share")], flags(get("cache_share"), True)),
        row("output tokens / trial (mean · median)", duo("out_tok_mean", "out_tok_median", n_tok)),
        row("tool calls / trial (mean · median)", duo("tool_calls_mean", "tool_calls_median", one)),
        row("trials with a tool error (heuristic)", ["n/a" if not r["trajectories"] else f'{r["trials_with_tool_errors"]} ({r["tool_errors"]} errors)' for r in runs]),
        row("cost / trial (mean) · total", [f'{money(r["cost_mean"], 4)} · {money(r["cost_total"])}' for r in runs], flags(get("cost_mean"), False)),
        row("agent setup, s (mean)", [num(v) for v in get("agent_setup_mean")], flags(get("agent_setup_mean"), False, rel=0.5)),
        row("agent execution, s (mean · median · max)", [f'{num(r["exec_mean"])} · {num(r["exec_median"])} · {num(r["exec_max"])}' for r in runs]),
    ]

    groups = m.get("groups") or sorted({g for r in runs for g in r["groups"]})
    grows = []
    for g in groups:
        cells = [r["groups"].get(g, {}) for r in runs]
        rates = [c.get("pass_rate") for c in cells]
        best = max([x for x in rates if x is not None], default=None)
        marks = []
        for c in cells:
            x, se = c.get("pass_rate"), c.get("standard_error") or 0
            best_se = max([k.get("standard_error") or 0 for k in cells if k.get("pass_rate") == best], default=0)
            marks.append(" worse" if x is not None and best is not None and (best - x) >= 2 * max(se, best_se, 1e-9) and best > x else "")
        tasks = next((c["tasks"] for c in cells if c), "")
        grows.append(f'<tr><td>{E(g)}</td><td class="num">{tasks}</td>'
                     + "".join(f'<td class="num{k}">{num(c.get("pass_rate"), 2)}</td>' for c, k in zip(cells, marks))
                     + f'<td class="num">{" · ".join(str(c.get("errored", 0)) for c in cells)}</td>'
                     + f'<td class="num">{" · ".join(n_tok(c.get("in_tok_mean")) for c in cells)}</td></tr>')
    group_head = "<th>task group</th><th>tasks</th>" + head + "<th>errored trials</th><th>input tokens / trial</th>"

    # outcomes: always a failure table; plus paired boxes (2 runs) or overlap boxes (3+)
    outcomes = []
    if n == 2:
        p = m["pairs"][0]
        outcomes.append(
            '<div class="paired">'
            f'<div><div class="k">both solve</div><div class="v">{p["both"]}</div></div>'
            f'<div><div class="k">{E(p["first"])} only</div><div class="v">{p["first_only"]}</div><div class="s">{p["first_only_where_second_errored"]} where the other errored</div></div>'
            f'<div><div class="k">{E(p["second"])} only</div><div class="v">{p["second_only"]}</div><div class="s">{p["second_only_where_first_errored"]} where the other errored</div></div>'
            f'<div><div class="k">neither</div><div class="v">{p["neither"]}</div></div></div>')
    elif n > 2:
        ov = m["overlap"]
        outcomes.append(
            '<div class="paired">'
            f'<div><div class="k">solved by all</div><div class="v">{ov["solved_by_all"]}</div></div>'
            f'<div><div class="k">solved by some</div><div class="v">{ov["solved_by_some"]}</div></div>'
            f'<div><div class="k">solved by none</div><div class="v">{ov["solved_by_none"]}</div></div>'
            f'<div><div class="k">common tasks</div><div class="v">{ov["common_tasks"]}</div></div></div>')
        outcomes.append('<div class="tw"><table><thead><tr><th>run</th><th>tasks only this run solves</th></tr></thead><tbody>'
                        + "".join(f'<tr><td>{sw(i)}{E(r["label"])}</td><td class="num">{u}</td></tr>' for i, (r, u) in enumerate(zip(runs, ov["unique_solves"])))
                        + "</tbody></table></div>")
    fail_rows = []
    for i, r in enumerate(runs):
        failed = r["trials"] - r["trials_passed"] - r["errored"]
        fail_rows.append(f'<tr><td>{sw(i)}{E(r["label"])}</td><td class="num">{r["trials_passed"]}</td><td class="num">{max(failed, 0)}</td>'
                         f'<td class="num">{r["errored"]}</td><td class="num">{r["unscored"]}</td></tr>')
    outcomes.append('<div class="tw"><table><thead><tr><th>run</th><th>passed</th><th>scored, not passed</th><th>errored</th><th>no score</th></tr></thead><tbody>'
                    + "".join(fail_rows) + "</tbody></table></div>")
    err_rows = "".join(f'<tr><td>{sw(i)}{E(r["label"])}</td><td class="num">{c}</td><td style="text-align:left;white-space:normal">{E(msg)}</td></tr>'
                       for i, r in enumerate(runs) for msg, c in r["exception_messages"][:4])
    if err_rows:
        outcomes.append('<div class="tw"><table><thead><tr><th>run</th><th>trials</th><th style="text-align:left">most frequent error</th></tr></thead><tbody>'
                        + err_rows + "</tbody></table></div>")

    sg = lambda r: "n/a" if r["step_gap_median"] is None else f'{r["step_gap_median"]:.1f} · {r["step_gap_p90"]:.1f} · {r["step_gap_max"]:.0f}'
    lat = [
        row("time between consecutive agent steps, s (median · 90th percentile · max)", [sg(r) for r in runs]),
        row("environment setup, s (mean)", [num(v) for v in get("env_setup_mean")]),
        row("agent setup, s (mean)", [num(v) for v in get("agent_setup_mean")]),
        row("agent execution, s (mean)", [num(v) for v in get("exec_mean")]),
        row("verifier, s (mean)", [num(v) for v in get("verifier_mean")]),
        row("whole trial, s (mean)", [num(v) for v in get("total_mean")]),
    ]

    exc = lambda r: ", ".join(f"{v} {k}" for k, v in r["exception_types"].items()) or "none"
    conc = m.get("concurrency") or [None] * n
    details = [
        row("trials (tasks × attempts)", [f'{r["trials"]} ({r["tasks"]} × {r["attempts_per_task"]})' for r in runs]),
        row("agent", [E(", ".join(r["agents"]) or "n/a") + (" " + E(", ".join(r["agent_versions"])) if r["agent_versions"] else "") for r in runs]),
        row("model", [E(", ".join(r["models"]) or "n/a") for r in runs]),
        row("first trial start → last finish (UTC)", [f'{E(str(r["first_start"])[:16])} → {E(str(r["last_finish"])[:16])}' for r in runs]),
        row("wall time, h", [num(v, 2) for v in get("wall_hours")]),
        row("concurrent trials", [str(c or "n/a") for c in conc]),
        row("total cost", [money(v) for v in get("cost_total")]),
        row("trials that report no token usage (excluded from token and cost means)", [str(r.get("trials_without_usage", 0)) for r in runs]),
        row("exceptions", [E(exc(r)) for r in runs]),
        row("job", [f'<code>{E(r["job"])}</code>' for r in runs]),
    ]
    config_block = ""
    if n >= 2:
        diff = m.get("config_diff") or {}
        diff_rows = "".join("<tr><td><code>" + E(k) + "</code></td>" + "".join(f'<td class="num">{E(str(v)[:60])}</td>' for v in values) + "</tr>"
                            for k, values in list(diff.items())[:25]) or f'<tr><td colspan="{n + 1}">no differences found in the job configs</td></tr>'
        config_block = ('<p class="note">What differs between the job configurations. A fair comparison varies only what it means to test.</p>'
                        f'<div class="tw"><table><thead><tr><th>setting</th>{head}</tr></thead><tbody>{diff_rows}</tbody></table></div>')

    labels = [r["label"] for r in runs]
    title = m.get("title") or (" vs ".join(labels) if n <= 3 else f"Comparison of {n} runs")
    se_list = ", ".join(num(v, 3) for v in get("standard_error"))
    if n >= 2:
        gap_text = "; ".join(f'{g["run"]} − {g["versus"]} = {g["gap"]:+.3f} ({g["in_standard_errors"]:.1f} standard errors)' for g in m["gaps"])
        noise = (f"Sampling noise: the standard error of a pass rate p over n tasks is √(p(1−p)/n), here ≈ {se_list}. Gaps: {gap_text}. "
                 "Below about two standard errors, treat two runs as tied.")
        tile_sub = "; ".join(f'{g["gap"]:+.3f} = {g["in_standard_errors"]:.1f} standard errors' for g in m["gaps"][:2])
    else:
        noise = (f"Sampling noise: the standard error of a pass rate p over n tasks is √(p(1−p)/n), here ≈ {se_list}. "
                 "A later run must differ by about two standard errors before the difference means anything.")
        tile_sub = f"± {se_list} standard error"
    def tile(lab, vals, sub, crit=None):
        spans = []
        for i, v in enumerate(vals):
            cls = ' class="crit"' if crit and crit[i] else ""
            spans.append("<span" + cls + ">" + sw(i) + str(v) + "</span>")
        return ('<div class="tile"><div class="lab">' + lab + '</div><div class="pair">' + "".join(spans)
                + '</div><div class="sub">' + E(sub) + "</div></div>")
    tiles = (
        tile("Pass rate", [num(v, 3) for v in get("pass_rate")], tile_sub)
        + tile("Errored trials", [str(v) for v in get("errored")], " · ".join(exc(r) for r in runs)[:120], [bool(k) for k in flags(get("errored"), False, rel=0.0)])
        + tile("Input tokens / model call", [n_tok(v) for v in get("in_tok_per_call_mean")], "mean; n/a where the agent wrote no trajectory")
        + tile("Cost / trial", [money(v, 3) for v in get("cost_mean")], "cache share " + " vs ".join(pc(v) for v in get("cache_share")),
               [bool(k) for k in flags(get("cost_mean"), False)]))
    legend = "".join(f"<span>{sw(i)}{E(r['label'])}</span>" for i, r in enumerate(runs)) \
        + "".join(f'<span><span class="sw ref"></span>{E(ref["label"])}</span>' for ref in references[:1])
    fill = {
        "TITLE": E(title), "H1": E(title),
        "EYEBROW": E(f'{max(r["tasks"] for r in runs)} tasks · {" / ".join(str(r["attempts_per_task"]) for r in runs)} trials per task · '
                     f'{" / ".join(str(r["first_start"])[:10] for r in runs)}'),
        "LEDE": slot("lede", "three bullets: the headline result with its noise, the main cause, the cost or reliability story"),
        "META": "".join(f'<span>{E(r["label"])}: {E(", ".join(r["agents"]) or "?")} · {E(", ".join(r["models"]) or "?")}</span>' for r in runs),
        "TILES": tiles, "HEAD": head, "OVERALL_ROWS": "\n".join(rows),
        "OVERALL_NOTE": slot("overall-note", "one or two sentences on what drives the numbers above; say which are estimates"),
        "LEGEND": legend, "CHART": chart_svg(groups, runs, references),
        "CHART_CAPTION": slot("chart-caption", "what the bars show; for a reference bar, its source, date and how comparable it is"),
        "GROUP_HEAD": group_head, "GROUP_ROWS": "\n".join(grows),
        "OUTCOMES": "\n".join(outcomes),
        "OUTCOMES_NOTE": slot("outcomes-note", "which check fails when a trial is not passed, where failures concentrate, and for several runs where the one-sided wins are"),
        "LATENCY_ROWS": "\n".join(lat),
        "LATENCY_NOTE": slot("latency-note", "what the timings mean and any confound, such as runs made on different days"),
        "ISSUES": slot("issues", "numbered findings in families; each with a title, evidence with counts, a bold impact line and a Fix line (markup is in the template comment)"),
        "DETAIL_ROWS": "\n".join(details), "CONFIG_DIFF": config_block, "NOISE_NOTE": E(noise),
        "METHOD": E("Per-trial metrics from result.json; call-level metrics, tool calls and step timings from agent/trajectory.json where the agent wrote one ("
                    + ", ".join(f'{r["trajectories"]} of {r["trials"]} trials for {r["label"]}' for r in runs) + "). "
                    + m.get("solved_rule", "").capitalize() + f". Tasks grouped by {m['group_method']}. Jobs: " + ", ".join(r["job"] for r in runs) + "."),
    }
    for key, value in fill.items():
        page = page.replace("{{" + key + "}}", value)
    if args.slots:
        prose = load(args.slots)
        if not isinstance(prose, dict):
            raise SystemExit(f"--slots needs a JSON object of slot name -> HTML, got {args.slots}")
        for name, content in prose.items():
            pattern = re.compile(r"<!--SLOT:" + re.escape(name) + r"-->.*?<!--/SLOT:" + re.escape(name) + r"-->", re.S)
            if not pattern.search(page):
                raise SystemExit(f"--slots names an unknown slot: {name}")
            page = pattern.sub(lambda _m, c=content: c, page)
    left = re.findall(r"\{\{[A-Z_0-9]+\}\}", page)
    if left:
        raise SystemExit(f"template placeholders not filled: {sorted(set(left))}")
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    open_slots = re.findall(r"<!--SLOT:([\w-]+)-->", page)
    print("wrote " + args.out + "\nopen prose slots to fill: " + ", ".join(open_slots))



SHORT_FORMS = {
    "SE": "standard error", "LLM": "model (or large language model)", "p90": "90th percentile", "p95": "95th percentile",
    "p50": "median", "SD": "standard deviation", "CI": "confidence interval", "OOM": "out of memory", "KB": "knowledge base",
}


def cmd_check(args):
    with open(args.page, encoding="utf-8") as fh:
        page = fh.read()
    problems = []
    open_slots = re.findall(r"SLOT TODO \(([\w-]+)\)", page)
    if open_slots:
        problems.append(f"open slots: {', '.join(open_slots)}")
    body = page[page.find("<main"):] if "<main" in page else page
    text = re.sub(r"<(code|style|script)[^>]*>.*?</\1>", " ", body, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    for short, meaning in SHORT_FORMS.items():
        if re.search(rf"(?<![\w-]){re.escape(short)}(?![\w-])", text) and meaning.split(" (")[0].lower() not in text.lower():
            problems.append(f"'{short}' is used but never spelled out ({meaning})")
    if re.search(r"\{\{[A-Z_]+\}\}", page):
        problems.append("unfilled {{PLACEHOLDER}} tokens")
    if "<title>" not in page[:8192]:
        problems.append("no <title> in the first 8 KB")
    if problems:
        print("NOT READY:")
        for p in problems:
            print(" -", p)
        raise SystemExit(1)
    print("ok: no open slots, no unexplained short forms, title present")




def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pass-threshold", type=float, default=1.0, help="reward at or above this counts as a pass (default 1.0)")
    common.add_argument("--workdir", default=None, help="where zips are extracted (default: system temp)")
    h = sub.add_parser("health", parents=[common], help="triage runs")
    h.add_argument("paths", nargs="+")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_health)
    an = sub.add_parser("analyze", parents=[common], help="metrics for one or more runs")
    an.add_argument("runs", nargs="+", help="run folder, job folder or zip; add ::JOB_NAME when it holds several jobs")
    an.add_argument("--label", action="append", help="display name for each run, in the same order (repeatable)")
    an.add_argument("--title", help="page title; default is the labels joined by 'vs'")
    an.add_argument("--group-regex", help="regex on the short task name; first capture group is the task group")
    an.add_argument("--ignore-tool", help="regex of tool names to leave out of tool counts, e.g. a generic wrapper the agent logs next to the real call")
    an.add_argument("--out", default="metrics.json")
    an.set_defaults(func=cmd_analyze)
    rd = sub.add_parser("render", help="comparison page from metrics.json")
    rd.add_argument("metrics")
    rd.add_argument("--out", default="comparison.html")
    rd.add_argument("--template")
    rd.add_argument("--slots", help="JSON file mapping slot names to the HTML that fills them, so prose survives a re-render")
    rd.add_argument("--reference", action="append", help="GROUP:LABEL:VALUE adds an external reference bar to one group of the chart")
    rd.set_defaults(func=cmd_render)
    ck = sub.add_parser("check", help="verify a page is ready to publish")
    ck.add_argument("page")
    ck.set_defaults(func=cmd_check)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
