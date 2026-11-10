"""The live board's feed and fold: ``events.jsonl`` → the snapshot the job page renders.

A running job should look like something running. Every Harbor run from
here appends one ``events.jsonl`` per job from Harbor's own trial hooks
(``nullius.harbor.feed``, whatever the Harbor); this application reads that file
— never a socket, never an agent runtime — and folds it into a board: per-trial
phase, time in phase, counters, outcome; job counts; a rate; an ETA. The board is
the job page's Live view, beside Results; when the feed records ``job.end`` it
stops polling and Results becomes the view the page opens on.

The feed schema, the fold and the snapshot document are pinned by the
fixtures under ``app/tests/fixtures/live/``: each feed there folds to its
``expected.json``, byte for byte as JSON (``app/tests/test_live.py``).

Every line carries ``ts`` (ISO-8601 UTC), ``seq`` (int, strictly increasing
within the file), ``job``, ``trial`` (``None`` for job events) and ``event``,
one of :data:`EVENTS` — ``job.prepare`` is the one written at launch, before
the launcher has fetched a dataset or built an image, so a board shows the
preparation window instead of nothing (its ``stage`` names what is being
prepared; ``job.start`` keeps meaning "trials are about to be scheduled").
Payloads are read leniently — a missing counter is *absent*, never zero.

Four rules are load-bearing, and each has a test:

- **The last phase event wins**, per trial, and time in phase is ``now`` minus
  that event's ``ts`` — the board never keeps its own clock.
- **Counters are the latest progress values, and an unreported one is
  absent.** On the feed a writer *omits* a counter it does not have (a present
  ``null`` is read the same way, as not reported); in the snapshot every
  counter key is present and ``null`` until something reported it — never
  ``0``. ``cost_usd`` is the case that matters: a zero there is a lie about
  money, and the page renders ``null`` as "not reported".
- **The file is read as it grows.** A torn last line (the writer mid-append)
  is dropped and noted, never raised; everything before it folds.
- **Free text is redacted on read**, with the same boundary-anchored patterns
  every other reader here uses (``redact.redact``), so the page can render
  ``activity`` and ``line`` without a second scrubber.

A fifth rule covers a job that runs more than one agent (lanes and a winner
per task), and it too is folded from the same lines:

- **Lanes and winners are the fold's, not the page's.** The snapshot carries
  one ``lane`` per agent — the agents ``job.start`` declared, in that order,
  then any agent first seen on a trial the declaration did not name, then one
  unnamed lane for trials whose agent never appears — each with its own
  counts, mean reward, totals and win count over its trials. It carries one
  ``winners`` entry per task that more than one agent ran: the best reward
  each agent reached on it so far, ``decided`` once every trial of that task
  has an outcome, and ``winner`` only when decided and one agent's best is
  strictly above every other's — a tie names nobody, and so does a task no
  agent scored. A single-agent job has one lane and nothing to win.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from nullius.redact import redact

EVENTS = ("job.prepare", "job.start", "trial.queued", "trial.phase", "trial.progress",
          "trial.log", "trial.end", "job.end")
PHASES = ("env_setup", "agent_setup", "agent_run", "verify")
#: ``scored`` is a runner's: one whose
#: benchmark gives a trial a score and no pass or fail ends it so - neither
#: resolved nor unresolved - with ``score`` on the event and, when that is a
#: mean over the trial's parts, how many (``parts``). The snapshot says so only
#: where there is one: a ``scored`` count, and ``score`` / ``parts`` on that
#: trial. A feed without one folds to a snapshot with none of those keys.
#: ``budget`` is a run's spending cap: a unit its relay cut off there (the
#: standard's ``--max-usd``), outside the rates as ``infra_error`` is.
OUTCOMES = ("resolved", "unresolved", "scored", "agent_error", "infra_error",
            "cancelled", "timeout", "budget")
ERROR_OUTCOMES = {"agent_error", "infra_error", "timeout", "budget"}
COUNTERS = ("step", "n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")
TOKEN_FIELDS = ("n_input_tokens", "n_cache_tokens", "n_output_tokens")

#: The rate window and the tail the snapshot carries.
RATE_WINDOW_SEC = 30.0
TAIL_EVENTS = 50
ACTIVITY_MAX = 120


class FeedError(ValueError):
    """A line that is not a feed line. Raised for a complete, malformed line;
    a torn last line is a notice instead (see :func:`read_events`)."""


@dataclass(frozen=True)
class Event:
    ts: datetime
    seq: int
    job: str
    trial: str | None
    event: str
    data: dict

    def to_dict(self) -> dict:
        return {"ts": _iso(self.ts), "seq": self.seq, "job": self.job,
                "trial": self.trial, "event": self.event, **self.data}


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(value) -> datetime:
    if not isinstance(value, str):
        raise FeedError(f"ts must be an ISO-8601 string, got {value!r}")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        ts = datetime.fromisoformat(text)
    except ValueError as e:
        raise FeedError(f"ts is not ISO-8601: {value!r}") from e
    if ts.tzinfo is None:
        raise FeedError(f"ts must carry a UTC offset: {value!r}")
    return ts.astimezone(timezone.utc)


def parse_line(line: str) -> Event:
    """One feed line to one :class:`Event`; :class:`FeedError` when it is not one."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        raise FeedError(f"not JSON: {e.msg}") from e
    if not isinstance(raw, dict):
        raise FeedError("a feed line is a JSON object")
    for key in ("ts", "seq", "job", "event"):
        if key not in raw:
            raise FeedError(f"missing {key!r}")
    if raw["event"] not in EVENTS:
        raise FeedError(f"unknown event {raw['event']!r}")
    if not isinstance(raw["seq"], int) or isinstance(raw["seq"], bool):
        raise FeedError(f"seq must be an int, got {raw['seq']!r}")
    trial = raw.get("trial")
    if raw["event"].startswith("trial.") and not isinstance(trial, str):
        raise FeedError(f"{raw['event']} needs a trial")
    if raw["event"].startswith("job.") and trial is not None:
        raise FeedError(f"{raw['event']} carries no trial")
    data = {k: v for k, v in raw.items() if k not in ("ts", "seq", "job", "trial", "event")}
    if raw["event"] == "trial.phase" and data.get("phase") not in PHASES:
        raise FeedError(f"unknown phase {data.get('phase')!r}")
    if raw["event"] == "trial.end" and data.get("outcome") not in OUTCOMES:
        raise FeedError(f"unknown outcome {data.get('outcome')!r}")
    for key in ("activity", "line"):
        if isinstance(data.get(key), str):
            data[key] = redact(data[key])[:ACTIVITY_MAX] if key == "activity" else redact(data[key])
    return Event(parse_ts(raw["ts"]), raw["seq"], str(raw["job"]), trial, raw["event"], data)


@dataclass
class Feed:
    events: list[Event]
    notices: list[str] = field(default_factory=list)


def read_events(path: Path | str) -> Feed:
    """Every complete line of the file, in ``seq`` order.

    A last line with no trailing newline that fails to parse is the writer
    mid-append: dropped, with a notice. Any other malformed line raises — a
    feed with a bad line in the middle is a broken writer, not a race. A
    ``seq`` that does not strictly increase is a notice too, and the events
    are folded in ``seq`` order regardless.
    """
    text = Path(path).read_text(encoding="utf-8")
    lines = text.split("\n")
    torn = lines[-1] if lines and lines[-1] != "" else None
    complete = lines[:-1] if lines else []
    events: list[Event] = []
    notices: list[str] = []
    for n, line in enumerate(complete, 1):
        if not line.strip():
            continue
        try:
            events.append(parse_line(line))
        except FeedError as e:
            raise FeedError(f"line {n}: {e}") from e
    if torn is not None:
        try:
            events.append(parse_line(torn))
        except FeedError:
            notices.append("last line torn (writer mid-append); dropped")
    seqs = [e.seq for e in events]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        notices.append("seq not strictly increasing; folded in seq order")
        events.sort(key=lambda e: e.seq)
    return Feed(events, notices)



@dataclass
class TrialState:
    trial: str
    task: str | None = None
    agent: str | None = None
    model: str | None = None
    position: int | None = None
    phase: str | None = None          #: current phase, None while queued
    since: datetime | None = None     #: ts of the winning phase event
    timeout_sec: float | None = None
    phases_done: list[str] = field(default_factory=list)
    step: int | None = None
    n_input_tokens: int | None = None
    n_cache_tokens: int | None = None
    n_output_tokens: int | None = None
    cost_usd: float | None = None
    activity: str | None = None
    last_line: str | None = None
    outcome: str | None = None
    reward: float | None = None
    score: float | None = None
    parts: int | None = None
    durations: dict = field(default_factory=dict)
    started_at: datetime | None = None
    attempt: int = 1                  #: its attempt; above 1 when Harbor retried it under the same name
    retried: list = field(default_factory=list)   #: the outcomes its earlier attempts ended with
    finished_at: datetime | None = None
    #: (ts, {token field: value}) per report — only the keys that report
    #: carried — for the rate window; see Board.tokens_per_sec.
    _series: list = field(default_factory=list, repr=False)

    @property
    def state(self) -> str:
        """queued · running · done — the card's border colour."""
        if self.outcome is not None:
            return "done"
        return "running" if self.phase else "queued"

    def elapsed_in_phase(self, now: datetime) -> float | None:
        if self.since is None or self.outcome is not None:
            return None
        return max(0.0, (now - self.since).total_seconds())

    def total_duration(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at
        return (end - self.started_at).total_seconds() if end else None

    def snapshot(self, now: datetime) -> dict:
        elapsed = self.elapsed_in_phase(now)
        return {
            "trial": self.trial, "task": self.task, "agent": self.agent, "model": self.model,
            "position": self.position, "state": self.state,
            "phase": self.phase, "since": _iso(self.since) if self.since else None,
            "elapsed_sec": elapsed, "timeout_sec": self.timeout_sec,
            "phases_done": list(self.phases_done),
            "step": self.step, "n_input_tokens": self.n_input_tokens,
            "n_cache_tokens": self.n_cache_tokens, "n_output_tokens": self.n_output_tokens,
            "cost_usd": self.cost_usd, "activity": self.activity, "last_line": self.last_line,
            "outcome": self.outcome, "reward": self.reward, "durations": dict(self.durations),
            "total_sec": self.total_duration(),
            **({"score": self.score, "parts": self.parts} if self.outcome == "scored" else {}),
            **({"attempt": self.attempt, "retried": list(self.retried)} if self.attempt > 1 else {}),
        }


@dataclass
class Board:
    job: str | None = None
    launched_at: datetime | None = None   #: first job.prepare — when the user pressed Run
    prepare_stage: str | None = None      #: the last job.prepare's `stage`
    prepare_since: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    ended_by: str | None = None           #: set when the runner, not the feed, ended the board
    n_total: int | None = None
    n_concurrent: int | None = None
    wall_sec: float | None = None
    budget: dict | None = None
    agents: list = field(default_factory=list)
    trials: list = field(default_factory=list)   #: TrialState, in first-seen order
    notices: list = field(default_factory=list)
    tail: list = field(default_factory=list)     #: the last TAIL_EVENTS events
    now: datetime | None = None

    def counts(self) -> dict:
        return _counts(self.trials, self.n_total)

    def mean_reward(self) -> float | None:
        return _mean_reward(self.trials)

    def totals(self) -> dict:
        return _totals(self.trials)

    def lane_order(self) -> list:
        """Agents in lane order: as ``job.start`` declared them, then any agent
        first seen on a trial the declaration did not name, then ``None`` for
        trials whose agent never appears — present only when there are such
        trials. Every trial falls in exactly one lane."""
        order = list(self.agents)
        for t in self.trials:
            if t.agent is not None and t.agent not in order:
                order.append(t.agent)
        if any(t.agent is None for t in self.trials):
            order.append(None)
        return order

    def lanes(self) -> list[dict]:
        """One lane per agent: its trials, and the header strip in miniature
        over them — counts (``total`` is the trials seen so far, since the feed
        does not say how many each agent will run), mean reward, totals, and
        how many tasks it has won (see :meth:`winners`)."""
        wins: dict = {}
        for w in self.winners():
            if w["winner"] is not None:
                wins[w["winner"]] = wins.get(w["winner"], 0) + 1
        out = []
        for agent in self.lane_order():
            members = [t for t in self.trials if t.agent == agent]
            out.append({"agent": agent, "trials": [t.trial for t in members],
                        "counts": _counts(members, len(members)),
                        "mean_reward": _mean_reward(members), "totals": _totals(members),
                        "wins": wins.get(agent, 0)})
        return out

    def winners(self) -> list[dict]:
        """One entry per task that more than one agent ran, in first-seen task
        order: the agents that ran it (lane order), the best reward each has
        reached on it so far (over its finished trials; ``None`` until it has
        one), ``decided`` once every trial of the task has an outcome, and the
        ``winner`` — the one agent whose best is strictly above every other's,
        named only when decided. A tie names nobody; so does a task nobody
        scored. Trials with no task or no agent take part in no contest."""
        by_task: dict = {}
        for t in self.trials:
            if t.task is not None and t.agent is not None:
                by_task.setdefault(t.task, []).append(t)
        order = [a for a in self.lane_order() if a is not None]
        out = []
        for task, members in by_task.items():
            agents = [a for a in order if any(m.agent == a for m in members)]
            if len(agents) < 2:
                continue
            rewards = {}
            for a in agents:
                scored = [m.reward for m in members
                          if m.agent == a and m.outcome is not None and m.reward is not None]
                rewards[a] = max(scored) if scored else None
            decided = all(m.outcome is not None for m in members)
            winner = None
            if decided:
                best = [r for r in rewards.values() if r is not None]
                if best:
                    top = [a for a, r in rewards.items() if r == max(best)]
                    winner = top[0] if len(top) == 1 else None
            out.append({"task": task, "agents": agents, "rewards": rewards,
                        "decided": decided, "winner": winner})
        return out

    def tokens_per_sec(self, window: float = RATE_WINDOW_SEC) -> float | None:
        """Tokens reported over the last ``window`` seconds, per second — the
        latest total per trial minus what it was at the window's start.

        ``None`` when no rate can be computed: a rate needs a trial with a
        report inside the window *and* a baseline to measure from (a report at
        or before the window's start, or a second report inside it). Totals
        that arrive only on ``trial.end`` give one point per trial and no
        rate; a real rate needs ``trial.progress``. A zero where nothing was
        measured is a lie — the same rule as ``cost_usd`` — so a board renders
        ``None`` as "not reported".

        The delta is taken **per counter key, over reports that carry that
        key**. A writer may report ``n_input_tokens``/``n_output_tokens`` on
        progress and add ``n_cache_tokens`` only on ``trial.end`` (Harbor's
        does); summing whole totals would then read the cache figure's first
        appearance as tokens produced in the window. A key needs its own
        baseline to contribute to a rate."""
        if self.now is None:
            return None
        start = self.now.timestamp() - window
        delta = 0
        measured = False
        for t in self.trials:
            for key in TOKEN_FIELDS:
                series = [(ts.timestamp(), vals[key]) for ts, vals in t._series if key in vals]
                before = [v for at, v in series if at <= start]
                inside = [v for at, v in series if at > start]
                if not inside or (not before and len(inside) < 2):
                    continue                  # no baseline for this key
                # A trial's first report inside the window is the baseline when
                # nothing precedes it: what it did before it started reporting
                # is unknown, not "everything in the last 30 s".
                base = before[-1] if before else inside[0]
                delta += max(0, series[-1][1] - base)
                measured = True
        return delta / window if measured else None

    def eta_sec(self) -> float | None:
        """The work left ÷ concurrency, the work counted in median finished
        durations: one per pending trial, and for a running one what is left of
        a median once its time so far is taken off - so the estimate counts down
        between two ends instead of standing still, and is set again at each.
        Hidden until two trials have finished, because one duration is not an
        estimate."""
        durations = [t.total_duration() for t in self.trials
                     if t.outcome is not None and t.total_duration() is not None]
        if len(durations) < 2 or self.finished_at is not None:
            return None
        c = self.counts()
        if c["running"] + c["pending"] <= 0:
            return 0.0
        median = statistics.median(durations)
        now = self.now or datetime.now(timezone.utc)
        running = [t for t in self.trials if t.state == "running"]
        spent = [max(0.0, (now - t.started_at).total_seconds()) if t.started_at else 0.0 for t in running]
        work = c["pending"] * median + sum(max(0.0, median - s) for s in spent)
        return work / max(1, self.n_concurrent or 1)

    @property
    def state(self) -> str:
        """The header's one word, in order: `unknown` (no job event yet),
        `preparing` (a job.prepare and no job.start - the launcher is fetching
        the dataset, building images, whatever its `stage` says), `starting`
        (job.start seen, no trial in a phase), `running`, `finished`."""
        if self.finished_at is not None:
            return "finished"
        if any(t.state != "queued" for t in self.trials):
            return "running"
        if self.started_at is not None:
            return "starting"
        if self.trials:
            return "running"       # trial events with no job.start on record: something is running
        if self.launched_at is not None:
            return "preparing"
        return "unknown"

    def end_by_runner(self, at: datetime, how: str) -> None:
        """The feed has no ``job.end`` but the runner that started Harbor
        knows the run ended (``how``: stopped, failed, done) at ``at``: the
        board settles there. A trial in a phase is cancelled - nothing will
        finish it - and the ones still queued never start; the notice says
        so, since the feed alone would read as a job still going."""
        if self.finished_at is not None:
            return
        self.finished_at = at
        self.ended_by = how
        cut = [t for t in self.trials if t.state == "running"]
        for t in cut:
            t.outcome, t.finished_at = "cancelled", at
        pending = self.counts()["pending"]
        self.notices.append(
            f"The run was {how} at {at.strftime('%H:%M:%S')}Z before Harbor wrote job.end: "
            f"{len(cut)} trial{'s' if len(cut) != 1 else ''} cancelled"
            + (f", {pending} never started" if pending else "") + ".")

    def elapsed_sec(self) -> float | None:
        """Since the launch when the feed recorded one, else since job.start:
        the user's clock starts at Run, and a board that showed nothing for
        the preparation window is what job.prepare exists to fix."""
        origin = self.launched_at or self.started_at
        if origin is None or self.now is None:
            return None
        end = self.finished_at or self.now
        return (end - origin).total_seconds()

    def preparing(self) -> dict | None:
        """The preparation window while it is open: its stage and how long."""
        if self.state != "preparing" or self.now is None:
            return None
        since = self.prepare_since or self.launched_at
        return {"stage": self.prepare_stage, "since": _iso(since),
                "elapsed_sec": max(0.0, (self.now - since).total_seconds())}

    def snapshot(self) -> dict:
        """The `/live` document: what a board renders, and nothing it computes
        on its own — every number here is a function of the file and ``now``."""
        now = self.now or datetime.now(timezone.utc)
        return {
            "job": self.job, "now": _iso(now), "state": self.state,
            "launched_at": _iso(self.launched_at) if self.launched_at else None,
            "preparing": self.preparing(),
            "started_at": _iso(self.started_at) if self.started_at else None,
            "finished_at": _iso(self.finished_at) if self.finished_at else None,
            # Only when the runner ended the board: the document otherwise
            # stays the fixtures' expected.json, byte for byte (test_live.Fixture).
            **({"ended_by": self.ended_by} if self.ended_by else {}),
            "running": self.finished_at is None and self.started_at is not None,
            "n_concurrent": self.n_concurrent, "wall_sec": self.wall_sec, "budget": self.budget,
            "agents": list(self.agents),
            "counts": self.counts(), "mean_reward": self.mean_reward(),
            "totals": self.totals(), "tokens_per_sec": self.tokens_per_sec(),
            "elapsed_sec": self.elapsed_sec(), "eta_sec": self.eta_sec(),
            "trials": [t.snapshot(now) for t in self.trials],
            "lanes": self.lanes(), "winners": self.winners(),
            "tail": [e.to_dict() for e in self.tail],
            "notices": list(self.notices),
        }


def _counts(trials: list, n_total: int | None) -> dict:
    """The count block over a list of trials. ``n_total`` is what the feed
    declared for the job; a lane passes the trials it has seen."""
    done = [t for t in trials if t.outcome is not None]
    return {
        # Unknown until job.prepare/job.start says, or a trial appears: a
        # "0 tasks" header during preparation would be a lie. A lane passes
        # the trials it has seen, so its total is always a number.
        "total": n_total if n_total is not None else (len(trials) or None),
        "running": sum(t.state == "running" for t in trials),
        "pending": (n_total - len([t for t in trials if t.state != "queued"])
                    if n_total is not None else sum(t.state == "queued" for t in trials)),
        "completed": len(done),
        "resolved": sum(t.outcome == "resolved" for t in done),
        "unresolved": sum(t.outcome == "unresolved" for t in done),
        **({"scored": sum(t.outcome == "scored" for t in done)} if any(t.outcome == "scored" for t in done) else {}),
        "errored": sum(t.outcome in ERROR_OUTCOMES for t in done),
        "cancelled": sum(t.outcome == "cancelled" for t in done),
    }


def _mean_reward(trials: list) -> float | None:
    rewards = [t.reward for t in trials if t.outcome is not None and t.reward is not None]
    return sum(rewards) / len(rewards) if rewards else None


def _totals(trials: list) -> dict:
    out: dict = {f: None for f in TOKEN_FIELDS}
    for f in TOKEN_FIELDS:
        vals = [getattr(t, f) for t in trials if getattr(t, f) is not None]
        out[f] = sum(vals) if vals else None
    costs = [t.cost_usd for t in trials if t.cost_usd is not None]
    out["cost_usd"] = sum(costs) if costs else None   # absent, never zero
    return out


def _num(value, kind=float):
    """A counter as reported, or None — never coerced from absence."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return kind(value)
    except (TypeError, ValueError):
        return None


def fold(events: list[Event], now: datetime | None = None, notices: list[str] | None = None) -> Board:
    """Fold a feed into a :class:`Board` as of ``now`` (default: the last event's
    ``ts`` — replay of a finished file is exact; a live board passes the clock)."""
    board = Board(notices=list(notices or []))
    by_id: dict[str, TrialState] = {}

    def trial_of(e: Event) -> TrialState:
        t = by_id.get(e.trial)
        if t is None:
            t = by_id[e.trial] = TrialState(trial=e.trial)
            board.trials.append(t)
        return t

    for e in events:
        d = e.data
        if board.job is None:
            board.job = e.job
        if e.trial is not None:
            # The spec puts task/agent/model on trial.queued; Harbor's writer
            # also stamps task and agent on every trial event. Either is read:
            # the first value seen wins, and a later event never blanks it.
            t = trial_of(e)
            for key in ("task", "agent", "model"):
                if getattr(t, key) is None and isinstance(d.get(key), str):
                    setattr(t, key, d[key])
        if e.event == "job.prepare":
            # Written at launch, before any preparation, and again as the
            # stage changes (last one wins, like trial.phase). Carries what
            # job.start will carry once the launcher knows it, so the header
            # can already say "N tasks, M concurrent".
            if board.launched_at is None:
                board.launched_at = e.ts
            board.prepare_stage = d.get("stage") if isinstance(d.get("stage"), str) else None
            board.prepare_since = e.ts
            if board.n_total is None:
                board.n_total = _num(d.get("n_total"), int)
            if board.n_concurrent is None:
                board.n_concurrent = _num(d.get("n_concurrent"), int)
            if not board.agents:
                board.agents = list(d.get("agents") or [])
        elif e.event == "job.start":
            board.started_at = e.ts
            board.n_total = _num(d.get("n_total"), int)
            board.n_concurrent = _num(d.get("n_concurrent"), int)
            board.wall_sec = _num(d.get("wall_sec"))
            board.budget = d.get("budget") if isinstance(d.get("budget"), dict) else None
            board.agents = list(d.get("agents") or [])
        elif e.event == "job.end":
            board.finished_at = e.ts
        elif e.event == "trial.queued":
            t = trial_of(e)
            t.position = _num(d.get("position"), int)
        elif e.event == "trial.phase":
            t = trial_of(e)
            if t.outcome is not None:
                # A phase after the trial ended: Harbor retries a trial under its own name (max_retries), so this is
                # its next attempt, running again; what the attempt before it ended with is kept beside it.
                t.attempt += 1
                t.retried.append(t.outcome)
                t.outcome, t.reward, t.score, t.parts, t.finished_at = None, None, None, None, None
                t.phases_done, t.durations, t.started_at = [], {}, e.ts
            if t.phase and t.phase not in t.phases_done and t.phase != d["phase"]:
                t.phases_done.append(t.phase)
            t.phase, t.since = d["phase"], e.ts       # the last phase event wins
            t.timeout_sec = _num(d.get("timeout_sec"))
            if t.started_at is None:
                t.started_at = e.ts
        elif e.event == "trial.progress":
            t = trial_of(e)
            for key in COUNTERS:
                if key in d and d[key] is not None:
                    setattr(t, key, _num(d[key], int if key != "cost_usd" else float))
            if d.get("activity") is not None:
                t.activity = d["activity"]
            reported = {k: _num(d[k], int) for k in TOKEN_FIELDS if d.get(k) is not None}
            if reported:
                t._series.append((e.ts, reported))
        elif e.event == "trial.log":
            t = trial_of(e)
            if d.get("line") is not None:
                t.last_line = d["line"]
        elif e.event == "trial.end":
            t = trial_of(e)
            t.outcome = d["outcome"]
            t.reward = _num(d.get("reward"))
            t.score, t.parts = _num(d.get("score")), _num(d.get("parts"), int)
            t.finished_at = e.ts
            if t.phase and t.phase not in t.phases_done:
                t.phases_done.append(t.phase)
            t.phase, t.since = None, None
            durations = d.get("durations")
            t.durations = {k: _num(v) for k, v in durations.items()} if isinstance(durations, dict) else {}
            for key in (*TOKEN_FIELDS, "cost_usd"):
                if d.get(key) is not None:
                    setattr(t, key, _num(d[key], int if key != "cost_usd" else float))
            reported = {k: _num(d[k], int) for k in TOKEN_FIELDS if d.get(k) is not None}
            if reported:
                t._series.append((e.ts, reported))
            if t.started_at is None:
                t.started_at = e.ts

    board.now = now or (events[-1].ts if events else datetime.now(timezone.utc))
    board.tail = events[-TAIL_EVENTS:]
    return board


def load(path: Path | str, now: datetime | None = None, *, feed=None) -> Board:
    """Read and fold one job's feed - or the feed already read (``feed``):
    the board every `/live` answer is made from (:func:`snapshot`)."""
    feed = feed if feed is not None else read_events(path)
    return fold(feed.events, now=now, notices=feed.notices)



FEED = "events.jsonl"


def feed_path(job_dir: str) -> Path:
    return Path(job_dir) / FEED


def has_feed(job_dir: str) -> bool:
    return feed_path(job_dir).is_file()


def timeline(events: list[Event]) -> dict:
    """When each trial entered each phase, and when it ended: ``{trial: {"starts":
    {phase: ts}, "ended": ts | None}}``, the first ``trial.phase`` per phase and the
    ``trial.end``, as the feed recorded them.

    Not part of the snapshot, which the fixtures pin and which stays byte for
    byte what it folds (``test_live``); the page's route carries it beside the
    snapshot, so a trial's phases can be drawn on its own clock. Only times the
    feed wrote: nothing here is measured by this application."""
    out: dict[str, dict] = {}
    for e in events:
        if e.trial is None:
            continue
        row = out.setdefault(e.trial, {"starts": {}, "ended": None})
        if e.event == "trial.phase" and e.data.get("phase"):
            row["starts"].setdefault(e.data["phase"], _iso(e.ts))
        elif e.event == "trial.end" and row["ended"] is None:
            row["ended"] = _iso(e.ts)
    return out


def snapshot(job_dir: str, now: datetime | None = None, ended: tuple | None = None,
             with_timeline: bool = False) -> dict:
    """Read and fold a job directory's feed. The one call the route makes.

    Always ``now``-driven for a live board: `fold`'s default (the last
    event's ``ts``) is exact for replaying a finished file, but a page that is
    polling wants the timers to move between polls, so the route passes the
    clock. ``ended`` is ``(how, at)`` from the runner's own record
    (`execution.ended_for`) when it knows the run is over: a feed Harbor
    never closed then settles instead of reading as a job still going.
    ``with_timeline`` adds :func:`timeline` under ``"timeline"`` for the page.
    """
    path = feed_path(job_dir)
    feed = read_events(path)
    board = load(path, now=now or datetime.now(timezone.utc), feed=feed)
    if ended is not None:
        board.end_by_runner(ended[1], ended[0])
    snap = board.snapshot()
    if with_timeline:
        snap["timeline"] = timeline(feed.events)
    return snap
