"""The live feed for any Harbor: ``events.jsonl`` written from Harbor's own trial hooks.

A run's ``harbor`` script (``source.wrapper``) runs this file, sent beside it
as ``harbor-feed.py``, with the pinned
Harbor's Python instead of the ``harbor`` command: it registers the hooks
every Harbor ``Job`` offers (``on_trial_started`` … ``on_trial_ended``), then
hands its arguments to Harbor's own command line, unchanged. Each job then
writes the feed ``nullius.runs.live`` folds into the job page's Live view -
the same lines, in the same schema - whatever Harbor it is: a PyPI release or
a repository at a commit.

What a Harbor without its own feed cannot give is left out, never made up:
no agent-setup phase (its hooks go from the environment to the agent), no
step or token counts while the agent runs; the trial's own result fills
the totals at its end. A phase's limit is the trial's own (its
``*_timeout_sec`` properties, multipliers applied), read from the trial
object the feed keeps by name since the hooks do not carry it. A Harbor that writes the feed itself
(its ``Job`` has a live feed of its own) is left alone, so a job never has two
writers.

Nothing here imports Harbor until :func:`main` runs inside Harbor's
environment, so this module loads in this application like any other; a
failure of the feed is said once on stderr and never stops the run.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone

FEED = "events.jsonl"
TRIALS: dict = {}          # trial name -> the Harbor trial object, kept as each is made, for its limits
LIMITS = {"env_setup": "environment_build_timeout_sec", "agent_run": "agent_timeout_sec", "verify": "verifier_timeout_sec"}
# an exception type naming the infrastructure, not the agent: the board's infra_error - the provider's
# (Api*, a refused key, a model name it does not serve) as the classifier counts them (nullius.runs.classify)
INFRA = ("Api", "Authentication", "ModelNotFound", "RateLimit", "Setup", "Environment", "Docker", "Build", "Connection", "Network")


class Writer:
    """One job's feed: a line per event, ``seq`` strictly increasing, each
    line flushed as written so a reader folds it as it grows."""

    def __init__(self, path: str, job: str):
        self.path, self.job, self.seq = path, job, 0
        self.lock = threading.Lock()

    def emit(self, event: str, trial=None, **data) -> None:
        with self.lock:
            self.seq += 1
            line = {"seq": self.seq, "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "job": self.job, "event": event, "trial": trial}
            line.update({k: v for k, v in data.items() if v is not None})
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, default=str) + "\n")


def outcome_of(result) -> tuple:
    """``(outcome, reward, evidence)`` of a finished trial, in the feed's words:
    the reward when the verifier gave one, else what the exception says."""
    rewards = getattr(getattr(result, "verifier_result", None), "rewards", None)
    reward = rewards.get("reward") if isinstance(rewards, dict) else None
    info = getattr(result, "exception_info", None)
    kind = (getattr(info, "exception_type", "") or "") if info is not None else ""
    if "Cancel" in kind:
        return "cancelled", reward, kind
    if reward is not None:
        return ("resolved" if reward >= 1 else "unresolved"), reward, f"reward {reward}"
    if "Timeout" in kind:
        return "timeout", None, kind
    if kind:
        return ("infra_error" if any(w in kind for w in INFRA) else "agent_error"), None, kind
    return "unresolved", None, "no reward"


def _seconds(timing):
    start, end = getattr(timing, "started_at", None), getattr(timing, "finished_at", None)
    return (end - start).total_seconds() if start is not None and end is not None else None


def _agent_name(agent) -> str:
    return getattr(agent, "name", None) or getattr(agent, "import_path", None) or "agent"


def _task_name(config) -> str:
    task = getattr(config, "task", None)
    try:
        return task.get_task_id().get_name()
    except Exception:
        return str(getattr(task, "path", "") or "")


def _said_once(e) -> None:
    if not _SAID:
        _SAID.append(e)
        print(f"nullius: the live feed stopped: {e!r}; the run goes on", file=sys.stderr)


_SAID: list = []


def _safe(hook):
    """A hook whose failure is said once and never reaches Harbor's trial."""
    async def run(event):
        try:
            await hook(event)
        except Exception as e:
            _said_once(e)
    return run


def attach(job, writer: Writer) -> None:
    """Register the feed's hooks on a Harbor ``Job`` and say the job started:
    its trials, each queued in the order Harbor will run them."""
    def who(event) -> dict:
        agent = getattr(event.config, "agent", None)
        return {"task": event.task_name, "agent": _agent_name(agent), "model": getattr(agent, "model_name", None)}

    def limit(trial_name, name):
        try:
            v = getattr(TRIALS.get(trial_name), LIMITS[name], None)
            return float(v) if isinstance(v, (int, float)) and v > 0 else None
        except Exception:
            return None

    def phase(name):
        async def hook(event):
            writer.emit("trial.phase", event.trial_name, phase=name, timeout_sec=limit(event.trial_name, name), **who(event))
        return hook

    async def ended(event):
        result = event.result
        outcome, reward, evidence = outcome_of(result)
        usage = getattr(result, "agent_result", None)
        writer.emit("trial.end", event.trial_name, outcome=outcome, reward=reward, evidence=evidence,
                    exception_type=getattr(getattr(result, "exception_info", None), "exception_type", None),
                    durations={"env_setup": _seconds(result.environment_setup), "agent_setup": _seconds(result.agent_setup),
                               "agent_run": _seconds(result.agent_execution), "verify": _seconds(result.verifier)},
                    n_input_tokens=getattr(usage, "n_input_tokens", None), n_cache_tokens=getattr(usage, "n_cache_tokens", None),
                    n_output_tokens=getattr(usage, "n_output_tokens", None), cost_usd=getattr(usage, "cost_usd", None),
                    **who(event))

    async def cancelled(event):
        writer.emit("trial.progress", event.trial_name, activity="cancellation requested", **who(event))

    job.on_environment_started(_safe(phase("env_setup")))
    job.on_agent_started(_safe(phase("agent_run")))
    job.on_verification_started(_safe(phase("verify")))
    job.on_trial_ended(_safe(ended))
    job.on_trial_cancelled(_safe(cancelled))
    configs = list(getattr(job, "_remaining_trial_configs", None) or getattr(job, "_trial_configs", None) or [])
    writer.emit("job.start", n_total=len(getattr(job, "_trial_configs", None) or configs),
                n_concurrent=getattr(job.config, "n_concurrent_trials", None),
                agents=sorted({_agent_name(c.agent) for c in configs}), tasks=sorted({_task_name(c) for c in configs}))
    for position, c in enumerate(configs):
        writer.emit("trial.queued", c.trial_name, task=_task_name(c), agent=_agent_name(c.agent),
                    model=getattr(c.agent, "model_name", None), position=position)


def keep_trials(Trial) -> None:
    """Wrap ``Trial.__init__`` so every trial is kept by its name: the hooks
    name a trial, and its limits are on the object."""
    made = Trial.__init__

    def init(self, *args, **kwargs):
        made(self, *args, **kwargs)
        try:
            TRIALS[self.config.trial_name] = self
        except Exception as e:
            _said_once(e)

    Trial.__init__ = init


def install(Job, Trial=None) -> bool:
    """Wrap ``Job.run`` so each job writes the feed (and ``Trial`` so its
    phases carry their limits). False, and nothing changed, when this Harbor
    writes its own."""
    if hasattr(Job, "_init_live_feed"):
        return False
    if Trial is not None:
        keep_trials(Trial)
    run = Job.run
    warn = _said_once

    async def run_with_feed(self, *args, **kwargs):
        writer = None
        try:
            os.makedirs(str(self.job_dir), exist_ok=True)
            writer = Writer(os.path.join(str(self.job_dir), FEED), self.config.job_name)
            attach(self, writer)
        except Exception as e:                                  # the feed is a view: never the run's undoing
            warn(e)
        result = await run(self, *args, **kwargs)
        if writer is not None:
            try:
                stats = getattr(result, "stats", None)
                writer.emit("job.end", n_completed=getattr(stats, "n_completed_trials", None),
                            n_errored=getattr(stats, "n_errored_trials", None), n_cancelled=getattr(stats, "n_cancelled_trials", None))
            except Exception as e:
                warn(e)
        return result

    Job.run = run_with_feed
    return True


def main() -> None:
    """``python feed.py <harbor arguments>``: Harbor's command line, with the feed."""
    try:
        from harbor.job import Job
        try:
            from harbor.trial.trial import Trial
        except Exception:
            Trial = None
        install(Job, Trial)
    except Exception as e:
        print(f"nullius: no live feed with this Harbor: {e!r}; the run goes on", file=sys.stderr)
    from importlib.metadata import entry_points
    # the `harbor` command as this Harbor declares it, whatever module it lives in
    command = next(iter(entry_points(group="console_scripts", name="harbor"))).load()
    sys.argv = ["harbor"] + sys.argv[1:]
    sys.exit(command())


if __name__ == "__main__":
    main()
