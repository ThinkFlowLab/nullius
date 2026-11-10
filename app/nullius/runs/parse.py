"""Walk job directories and build the normalized data model.

A *job* is one Harbor run (a timestamped dir holding a ``result.json`` and one
subdir per *trial*). A *trial* is one task attempt. We read only what we need
and redact on the way in.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from nullius.runs import verifier
from nullius.runs import classify
from nullius.redact import redact

# What a trace document in a trial's `agent/` may be called: the producer's own
# capture, or a filtered view saved from the trial page. A bare filename, so
# there is no path to traverse in the first place, and no leading dot to hide the
# file it writes.
#
# One definition, two readers: the trial page validates a name before writing it,
# and one arriving in a query string (?trace=) before reading it.
TRACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.json$")
DEFAULT_TRACE_NAME = "trace.json"

# The shape this module produces. A workspace caches parsed Jobs keyed on it,
# so bump it when a Trial or Job field is added, renamed or means something
# new — otherwise a cache written by the old parse is read back as current.
SCHEMA = "12"

# What a rate-limit record in `agent/gateway.log` looks like. The agent's SDK
# retries a 429 below the harness (an agent's `max_retries` reaching its
# OpenAI client), so a limit the run survived leaves nothing in result.json —
# the outcome says `resolved` and the only evidence is the log. Three shapes
# are counted: the exception class or the API's error code (`RateLimitError`,
# `rate_limit_exceeded`, "Rate limit reached"), the HTTP reason phrase ("Too
# Many Requests"), and a bare 429 *as a status* — `status_code=429`, `HTTP 429`,
# `Error code: 429`. A bare 429 alone is not enough: event ids, hashes and
# token counts in the same log can carry those digits by chance.
# What is not counted: the SDK's "Retrying request …" line, which names no
# status, so a 5xx retry is not mistaken for a rate limit.
RATE_LIMIT_RE = re.compile(
    rb"rate.?limit|too many requests"
    rb"|(?:status(?:[_ -]?code)?|error code|code|http(?:/\d(?:\.\d)?)?)\W{0,4}429(?!\w|\.\d)",
    re.IGNORECASE)
# How much of a gateway log the scan reads: the count is over the first
# GATEWAY_SCAN_BYTES only. Scanning is a bounded parse-time cost; for a log
# that outgrows the bound, the count is a floor.
GATEWAY_SCAN_BYTES = 8 * 1024 * 1024


def count_rate_limits(path: str) -> int | None:
    """Rate-limit records in a gateway log, or None when there is no log.

    None and 0 are different answers: an agent that wrote no gateway log
    (a control, an imported run, the direct transport) cannot be said to have
    hit no limits, only that nothing recorded it. Lines are counted, bounded by
    GATEWAY_SCAN_BYTES, and the file is never held whole.
    """
    try:
        handle = open(path, "rb")
    except OSError:
        return None
    count, budget = 0, GATEWAY_SCAN_BYTES
    with handle:
        for line in handle:
            if RATE_LIMIT_RE.search(line):
                count += 1
            budget -= len(line)
            if budget <= 0:
                break
    return count


def _iso(ts: str | None):
    """A stamp as an aware datetime. Harbor writes its stamps without a zone,
    in UTC; the standard, OpenTelemetry and zoned ATIF imports write one. A
    naive and an aware datetime cannot be compared or subtracted, so a page
    that ordered or spanned both returned 500: every stamp is given UTC."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when


def _dur(a: dict | None) -> float | None:
    """Seconds between started_at/finished_at of a phase block."""
    if not a:
        return None
    s, f = _iso(a.get("started_at")), _iso(a.get("finished_at"))
    if s and f:
        return (f - s).total_seconds()
    return None


@dataclass
class Trial:
    job_id: str
    dir: str
    task_name: str
    trial_name: str
    task_base: str            # e.g. django__django-10554
    source: str
    model_name: str
    outcome: str
    reward: float | None
    exception_type: str
    exception_message: str
    # verifier detail. The patch pair is `None` for a benchmark that has no
    # patches to report on — templates show absence, not "No".
    patch_exists: bool | None
    patch_applied: bool | None
    f2p_pass: int
    f2p_fail: int
    p2p_pass: int
    p2p_fail: int
    n_input_tokens: int
    n_output_tokens: int
    n_cache_tokens: int
    cost_usd: float | None
    # timings (seconds)
    dur_total: float | None
    dur_env: float | None
    dur_agent_setup: float | None
    dur_agent: float | None
    dur_verifier: float | None
    has_trajectory: bool = False
    # The resolved agent version recorded in agent_info —
    # what makes two runs of "the same" agent actually comparable.
    agent_version: str = ""
    # The agent the trial's own result.json names (agent_info.name). A job
    # config written with defaults left out names no agent when the agent is
    # Harbor's default (oracle); the trials still say who ran.
    agent_info_name: str = ""
    reasoning_effort: str = ""
    # The environment the trial's own result.json records (`config.environment
    # .type`, e.g. "docker") — the job-level config does not carry it.
    env_type: str = ""
    has_trace: bool = False
    # Rate-limit records the agent's own log carries — 429s its SDK retried and
    # the run survived, which no result.json field records (`count_rate_limits`).
    # None when the trial has no `agent/gateway.log`: not measured, not zero.
    n_rate_limited_retries: int | None = None
    # A graded verdict (an F1, partial credit), beside the pass or fail the
    # reward says: the runner records it when its benchmark scores that way.
    # A benchmark that gives a score and no pass or fail leaves the reward
    # out: the trial is then scored, not resolved or failed.
    score: float | None = None
    # How many parts that score is the mean of (the questions of a
    # conversation), when the benchmark scores an item part by part; each
    # part is in verifier/parts.json. None: the score is the item's own.
    n_parts: int | None = None
    # Those parts' scores by the field the benchmark breaks its score down by
    # (a question's category): {value: [sum of scores, parts]}, a part with no
    # score counting nothing, as in the trial's own mean; and that field's word.
    part_groups: dict = field(default_factory=dict)
    parts_by: str = ""
    # Names of the tests that failed, kept so a regression can be shown, not
    # just counted.
    f2p_failures: list = field(default_factory=list)
    p2p_failures: list = field(default_factory=list)
    # Absolute clocks, for anything that places this trial on a timeline
    # rather than measuring it — an exporter drawing spans needs where a phase
    # *sat*, not just how long it took. `phases` maps a Harbor phase name
    # (environment_setup, agent_setup, agent_execution, verifier) to its
    # (started, finished) pair; a phase the run never recorded is absent, so a
    # reader sees nothing rather than a zero-length span.
    started_at: datetime | None = None
    finished_at: datetime | None = None
    phases: dict = field(default_factory=dict)

    @property
    def dur_phases(self) -> float:
        """Sum of the four phases Harbor breaks out."""
        return sum(
            v or 0
            for v in (self.dur_env, self.dur_agent_setup, self.dur_agent, self.dur_verifier)
        )

    @property
    def dur_other(self) -> float:
        """Trial time Harbor doesn't attribute to a phase.

        Image pull, container start and teardown, artifact collection. Clamped
        at 0: if the phases ever exceed the total — clock skew, or phases that
        overlap — a negative value would corrupt the bar's geometry silently
        rather than showing an error.
        """
        return max(0.0, (self.dur_total or 0) - self.dur_phases)

    @property
    def is_regression(self) -> bool:
        """The patch broke tests that were passing before it was applied."""
        return self.p2p_fail > 0

    @property
    def outcome_label(self) -> str:
        return classify.LABELS.get(self.outcome, self.outcome)

    @property
    def vocab(self):
        """The dataset's own names for its checks — see `verifier.Vocabulary`."""
        return verifier.vocabulary_for(self.source)

    @property
    def is_infra(self) -> bool:
        return self.outcome in classify.INFRA_OUTCOMES

    @property
    def trajectory_path(self) -> str:
        return os.path.join(self.dir, "agent", "trajectory.json")

    @property
    def trace_path(self) -> str:
        """The timing sidecar, written beside the trajectory when captured."""
        return os.path.join(self.dir, "agent", DEFAULT_TRACE_NAME)

# Harbor's control agents. Neither calls a model: `oracle` applies the task's
# gold patch, `nop` does nothing. Their zero tokens and zero cost are true
# measurements, not missing ones — which is the whole reason to tell them apart.
CONTROL_AGENTS = {"oracle", "nop"}


@dataclass
class Job:
    job_id: str
    dir: str
    model_name: str
    dataset: str
    n_tasks: int
    started_at: datetime | None
    finished_at: datetime | None
    trials: list = field(default_factory=list)  # list[Trial]
    agent_name: str = ""       # e.g. "claude-code", "opencode"
    reasoning_effort: str = ""
    # Harbor's own bookkeeping from the job-level result.json stats block.
    # n_running/n_pending distinguish a finished run from one still going —
    # without them an in-progress sweep reads as a run full of misses.
    n_total_trials: int = 0
    n_errored_trials: int = 0
    n_running_trials: int = 0
    n_pending_trials: int = 0
    n_retries: int = 0
    preparing: bool = False    # launched, no result.json yet — see is_preparing_job_dir
    # The dataset path exactly as the config recorded it (may be relative to
    # wherever harbor ran, and empty for registry fetches). Kept because it's
    # the only lead for finding the checkout — and its gold patches — locally.
    dataset_path: str = ""
    # The `harbor run` invocation reconstructed from this job's recorded
    # config — what a copy-and-rerun needs. Empty when config.json is absent.
    harbor_command: str = ""
    #: the Harbor fork version the job recorded in its lock.json (`harbor.version`),
    #: and whether that Harbor was an editable checkout — what actually ran,
    #: as distinct from what a recipe intended; "" when the tree carries none
    harbor_version: str = ""
    harbor_editable: bool | None = None
    #: a job whose config names `runner`: its runner and resolved upstream
    #: commit, in place of Harbor metadata
    runner: str = ""
    runner_commit: str = ""
    runner_judge: str = ""          # the benchmark's own judge, as the run named it: "official gpt-4o · evaluate_qa.py"
    questions_from: str = ""        # a runner's recipe dataset: the items every arm answers, a control's with a file of its own included
    runner_items: list = field(default_factory=list)   # what the runner calls one item and many, from its definition: ["question", "questions"]
    # A Nullius standard job that ran one of its benchmark's declared controls
    # (config ``standard.control``): who wrote it - official, adapter, nullius.
    # "" for every other job.
    control_source: str = ""
    # A Nullius standard job (config ``standard``): its trials are written as each unit ends, so the
    # live board links one as soon as it has - unlike a runner's, imported at its end.
    standard: bool = False
    # The launch a Nullius standard job was one shard of (config
    # ``standard.launch``): the shards of one launch, run on several machines,
    # are grouped as one on the runs page (launches.group). "" for every other job.
    launch_id: str = ""
    # How the job was launched, as its config.json recorded it with Harbor's
    # defaults filled in (launch_of): what "New run like this" rebuilds a
    # recipe from when the run kept none. Empty when config.json is absent.
    launch: dict = field(default_factory=dict)
    # A run with a spending cap (a Nullius standard job's ``result.json``, ``budget``): the cap and what was
    # spent in USD, whether the cap stopped the run, the units it cut off and those never begun. {} without one.
    budget: dict = field(default_factory=dict)
    # A Harbor job run with several agents or models (`-m a -m b`, or a config
    # listing several): each one's {name, model, effort}, which split_agents
    # turns into one view per agent. [] for a job of one agent.
    agents: list = field(default_factory=list)

    @property
    def agent_label(self) -> str:
        """Readable half of the agent id — the class, not the module path."""
        return self.agent_name.split(":")[-1] if self.agent_name else ""

    @property
    def kind(self) -> str:
        """'control' for Harbor's oracle/nop baselines, else 'agent'. A
        runner's arm is never Harbor's control, whatever it is called:
        a runner's `oracle` arm is its own ceiling, not a reference solution.
        A standard job is one when its config says it ran a declared control."""
        named = self.agent_label.lower() in CONTROL_AGENTS
        return "control" if named and (not self.runner or self.control_source) else "agent"

    @property
    def is_control(self) -> bool:
        return self.kind == "control"

    @property
    def n_planned(self) -> int:
        """How many trials the run set out to do — Harbor's own count when it
        recorded one, else the task count from the config. Comparing this with
        the trials actually on disk exposes a partial or interrupted run."""
        return self.n_total_trials or self.n_tasks

    @property
    def is_running(self) -> bool:
        """Trials still executing or queued — the run isn't over yet."""
        return (self.n_running_trials + self.n_pending_trials) > 0

    @property
    def harbor_label(self) -> str:
        """The recorded Harbor: version, and `editable` when the lock says the
        checkout was — a development Harbor, not a release; empty when unrecorded."""
        if not self.harbor_version:
            return ""
        return self.harbor_version + (" · editable checkout" if self.harbor_editable else "")

    @property
    def is_preparing(self) -> bool:
        """Launched, not started: Harbor has written the job's config and
        nothing else yet — no result record, no trial. The runs list shows the
        row now rather than when the first event lands."""
        return self.preparing

    @property
    def state(self) -> str:
        """``preparing`` → ``running`` → ``finished``, for a badge."""
        return "preparing" if self.preparing else "running" if self.is_running else "finished"

    @property
    def agent_version(self) -> str:
        """The resolved agent version, taken from the first trial that has one."""
        return next((t.agent_version for t in self.trials if t.agent_version), "")

    @property
    def vocab(self):
        """The check vocabulary of the dataset this job ran, from its trials.

        Every job on disk runs one dataset, so the first trial that names its
        checks speaks for the run. A job of only unknown datasets gets the
        empty vocabulary, and the table drops the check columns rather than
        labelling them with another benchmark's words.
        """
        for t in self.trials:
            v = t.vocab
            if v.gained or v.held or v.has_patch:
                return v
        return verifier.GENERIC_VOCABULARY

    @property
    def duration_min(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds() / 60.0
        return None

    @property
    def counts(self) -> dict:
        c = {k: 0 for k in classify.ORDER}
        for t in self.trials:
            c[t.outcome] = c.get(t.outcome, 0) + 1
        return c

    @property
    def total_input_tokens(self) -> int:
        return sum(t.n_input_tokens or 0 for t in self.trials)

    @property
    def total_output_tokens(self) -> int:
        return sum(t.n_output_tokens or 0 for t in self.trials)

    @property
    def total_cache_tokens(self) -> int:
        return sum(t.n_cache_tokens or 0 for t in self.trials)

    @property
    def total_cost_usd(self):
        """None when no trial reported a cost — not every agent does."""
        costs = [t.cost_usd for t in self.trials if t.cost_usd is not None]
        return sum(costs) if costs else None

    @property
    def mean_score(self):
        """The mean of the trials' graded scores, None when none was graded.
        A trial scored part by part weighs as many as it has: the mean is
        then over every part - every question, not every conversation - which
        is the number a paper that scores questions reports."""
        scored = [(t.score, t.n_parts or 1) for t in self.trials if t.score is not None]
        return sum(s * n for s, n in scored) / sum(n for _, n in scored) if scored else None

    @property
    def score_by_group(self) -> list:
        """The mean score by the field the benchmark breaks it down by - a
        question's category - over every part of every trial: [{group, mean,
        n}], in the order the values sort (numbers as numbers); [] when no
        trial was scored that way."""
        total = {}
        for t in self.trials:
            for g, (s, n) in (t.part_groups or {}).items():
                acc = total.setdefault(g, [0.0, 0])
                acc[0] += s
                acc[1] += n
        key = lambda g: (0, float(g), "") if g.replace(".", "", 1).lstrip("-").isdigit() else (1, 0.0, g)
        return [{"group": g, "mean": s / n, "n": n} for g, (s, n) in sorted(total.items(), key=lambda kv: key(kv[0])) if n]

    @property
    def parts_by(self) -> str:
        """The word for what the score is broken down by, '' when it is not."""
        return next((t.parts_by for t in self.trials if t.parts_by), "")

    @property
    def n_scored_parts(self):
        """How many parts the mean score is over, None when no trial has parts."""
        counts = [t.n_parts for t in self.trials if t.score is not None and t.n_parts]
        return sum(counts) if counts else None

    @property
    def scored_only(self) -> bool:
        """Scores and no pass or fail anywhere: the mean score is the
        headline, and the solve rates stay absent rather than 0% - also when
        a trial failed on an exception, which is a verdict of its own."""
        return any(t.score is not None for t in self.trials) and not any(t.reward is not None for t in self.trials)

    @property
    def n_regressions(self) -> int:
        """Trials whose patch broke previously-passing tests."""
        return sum(1 for t in self.trials if t.is_regression)

    @property
    def reports_regressions(self) -> bool:
        """Can this job's benchmark say whether anything broke?

        `is_regression` reads `p2p_fail`, which a benchmark with no reader for
        held checks leaves at 0 — indistinguishable from "nothing broke"
        unless the question of whether it was asked is kept separate. A job
        where some trial reported held checks at all can be counted; one where
        none did shows absence, per the rule that a field a benchmark cannot
        answer is never rendered as a plausible zero.
        """
        return any(t.p2p_pass or t.p2p_fail for t in self.trials)

    @property
    def n_resolved(self) -> int:
        return sum(1 for t in self.trials if t.outcome == classify.RESOLVED)

    @property
    def has_verdict(self) -> bool:
        """Did anything score this job?

        A trial's outcome is `unknown` only when it carries no verdict at all
        — no verifier report, no reward and no exception (`classify
        .from_verdict`). A standalone ATIF trajectory or an OTel export is
        nothing but such trials, and "nothing was scored"
        is a different claim from "everything scored missed": the rates
        below are `None` here, and the pages show absence rather than 0%.
        One verified trial is enough — from then on the unknowns stay in the
        denominator, the rule `.claude/rules/running.md` states.
        """
        return any(t.outcome != classify.UNKNOWN for t in self.trials)

    @property
    def raw_solve_rate(self) -> float | None:
        """Resolved ÷ all trials; `None` when no trial carries a verdict."""
        if not self.trials or not self.has_verdict or self.scored_only:
            return None
        return self.n_resolved / len(self.trials)

    @property
    def adjusted_solve_rate(self) -> float | None:
        """Solve rate over trials that actually ran (excludes infra failures);
        `None` when no trial carries a verdict."""
        if not self.has_verdict or self.scored_only:
            return None
        eligible = [t for t in self.trials if not t.is_infra]
        return (self.n_resolved / len(eligible)) if eligible else None

    @property
    def n_infra(self) -> int:
        return sum(1 for t in self.trials if t.is_infra)


def is_preparing_job_dir(path: str) -> bool:
    """A job directory Harbor has created but not started: its job-level
    ``config.json`` (the one naming ``agents`` or ``datasets`` — a trial's names
    a ``task``) with no ``result.json`` yet. Everything between the launch and
    ``job.start`` looks like this on disk, and it is the state a person who
    just pressed Run is waiting to see."""
    if os.path.isfile(os.path.join(path, "result.json")):
        return False
    cfg = verifier._read_json(os.path.join(path, "config.json"))
    return isinstance(cfg, dict) and ("agents" in cfg or "datasets" in cfg) and "task" not in cfg


def looks_like_job_dir(path: str) -> bool:
    """A ``result.json`` and either a trial that has one of its own or a live
    feed — or a job that has been launched and not started yet. Harbor writes a
    trial's ``result.json`` when the trial ends, so a job whose first trial is
    still running has none, and that is exactly the job the live board exists
    for: its ``events.jsonl`` is Harbor's own sign that the job is underway, so
    it counts. Before even that, the job-level ``config.json`` alone means
    *preparing* (see :func:`is_preparing_job_dir`). A bare ``result.json``
    still does not count.
    """
    if not os.path.isfile(os.path.join(path, "result.json")):
        return is_preparing_job_dir(path)
    if os.path.isfile(os.path.join(path, "events.jsonl")):
        return True
    return any(
        os.path.isdir(os.path.join(path, d))
        and os.path.isfile(os.path.join(path, d, "result.json"))
        for d in os.listdir(path)
    )


def find_job_dirs(roots) -> list[str]:
    """Return every job directory found under the given root paths.

    A root may itself be a job dir, or a parent containing job dirs (one level
    down). Both are handled, and results are de-duplicated while preserving
    order, so overlapping roots are safe.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            found.append(path)

    for root in roots:
        if not os.path.isdir(root):
            continue
        if looks_like_job_dir(root):
            add(root)
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if os.path.isdir(p) and looks_like_job_dir(p):
                add(p)
    return found


def _token_totals(res: dict) -> dict:
    """Token/cost totals for a trial, whichever shape Harbor wrote.

    Single-step trials carry one AgentContext on ``agent_result``; multi-step
    trials leave that null and record one per entry of ``step_results``.
    Mirrors Harbor's ``TrialResult.compute_token_cost_totals``: cost stays
    None unless some step actually reported one — "unpriced" and "free" are
    different claims.
    """
    if res.get("agent_result"):
        contexts = [res["agent_result"]]
    else:
        contexts = [
            sr.get("agent_result")
            for sr in res.get("step_results") or []
            if isinstance(sr, dict) and sr.get("agent_result")
        ]
    totals = {"n_input_tokens": 0, "n_output_tokens": 0, "n_cache_tokens": 0, "cost_usd": None}
    for ctx in contexts:
        for key in ("n_input_tokens", "n_output_tokens", "n_cache_tokens"):
            totals[key] += ctx.get(key) or 0
        if ctx.get("cost_usd") is not None:
            totals["cost_usd"] = (totals["cost_usd"] or 0.0) + ctx["cost_usd"]
    return totals


def parse_trial(job_id: str, trial_dir: str) -> Trial | None:
    res = verifier._read_json(os.path.join(trial_dir, "result.json"))
    if not res:
        return None

    task_name = res.get("task_name", os.path.basename(trial_dir))
    task_base = task_name.split("/")[-1]
    exc = res.get("exception_info") or {}
    exc_type = exc.get("exception_type", "") or ""

    agent_res = _token_totals(res)
    ver_res = res.get("verifier_result") or {}
    reward = None
    if isinstance(ver_res, dict):
        reward = (ver_res.get("rewards") or {}).get("reward")

    # Older jobs record `source`; newer ones only carry it as the prefix of
    # a "<dataset>/<task>" task name. It selects the verifier loader and
    # reader, so it has to be resolved before the report is touched.
    source = res.get("source") or (task_name.rsplit("/", 1)[0] if "/" in task_name else "")

    report = verifier.load(source, trial_dir, task_base)
    verdict = verifier.read(source, report, reward)
    outcome = classify.from_verdict(exc_type, verdict)
    rewards = (ver_res.get("rewards") or {}) if isinstance(ver_res, dict) else {}
    score = rewards.get("score") if isinstance(rewards.get("score"), (int, float)) and not isinstance(rewards.get("score"), bool) else None
    # the parts' scores by the field the benchmark breaks its score down by (verifier/parts.json)
    part_groups, parts_by = {}, ""
    pj = verifier._read_json(os.path.join(trial_dir, "verifier", "parts.json"))
    if isinstance(pj, dict) and pj.get("by") and isinstance(pj.get("parts"), list):
        parts_by = str(pj["by"])
        for x in pj["parts"]:
            if isinstance(x, dict) and "group" in x:
                acc = part_groups.setdefault(str(x["group"]), [0.0, 0])
                acc[0] += x["score"] if isinstance(x.get("score"), (int, float)) and not isinstance(x.get("score"), bool) else 0.0
                acc[1] += 1
    # a score and no pass or fail, no exception: the benchmark measured it and gave no verdict to classify
    if outcome == classify.UNKNOWN and score is not None and reward is None and not exc_type:
        outcome = classify.SCORED

    traj_path = os.path.join(trial_dir, "agent", "trajectory.json")
    trace_path = os.path.join(trial_dir, "agent", "trace.json")

    phases = {}
    for name in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        block = res.get(name) or {}
        started, finished = _iso(block.get("started_at")), _iso(block.get("finished_at"))
        if started and finished:
            phases[name] = (started, finished)

    return Trial(
        job_id=job_id,
        dir=trial_dir,
        task_name=task_name,
        trial_name=res.get("trial_name", os.path.basename(trial_dir)),
        task_base=task_base,
        source=source,
        model_name=(res.get("agent_info", {}).get("model_info", {}) or {}).get("name", ""),
        agent_version=(res.get("agent_info") or {}).get("version") or "",
        reasoning_effort=str((((res.get("config") or {}).get("agent") or {}).get("kwargs") or {}).get("reasoning_effort", "")),
        agent_info_name=str((res.get("agent_info") or {}).get("name") or ""),
        outcome=outcome,
        reward=reward,
        score=score,
        part_groups=part_groups,
        parts_by=parts_by,
        n_parts=(ver_res.get("parts") or {}).get("n") if isinstance(ver_res, dict) and isinstance(ver_res.get("parts"), dict)
        and isinstance((ver_res.get("parts") or {}).get("n"), int) and not isinstance((ver_res.get("parts") or {}).get("n"), bool) else None,
        exception_type=exc_type,
        exception_message=redact(exc.get("exception_message", "")),
        patch_exists=verdict.produced,
        patch_applied=verdict.usable,
        f2p_pass=verdict.gained_pass,
        f2p_fail=verdict.gained_fail,
        p2p_pass=verdict.held_pass,
        p2p_fail=verdict.held_fail,
        f2p_failures=list(verdict.gained_failures),
        p2p_failures=list(verdict.held_failures),
        n_input_tokens=agent_res.get("n_input_tokens") or 0,
        n_output_tokens=agent_res.get("n_output_tokens") or 0,
        n_cache_tokens=agent_res.get("n_cache_tokens") or 0,
        cost_usd=agent_res.get("cost_usd"),
        dur_total=_dur(
            {
                "started_at": res.get("started_at"),
                "finished_at": res.get("finished_at"),
            }
        ),
        dur_env=_dur(res.get("environment_setup")),
        dur_agent_setup=_dur(res.get("agent_setup")),
        dur_agent=_dur(res.get("agent_execution")),
        dur_verifier=_dur(res.get("verifier")),
        has_trajectory=os.path.isfile(traj_path),
        has_trace=os.path.isfile(trace_path),
        n_rate_limited_retries=count_rate_limits(os.path.join(trial_dir, "agent", "gateway.log")),
        env_type=((res.get("config") or {}).get("environment") or {}).get("type", ""),
        started_at=_iso(res.get("started_at")),
        finished_at=_iso(res.get("finished_at")),
        phases=phases,
    )


def harbor_command(cfg: dict, env_type: str = "") -> str:
    """Reconstruct the `harbor run` invocation a job's config.json records.

    Faithful to the file: only flags the harness actually recorded appear, so
    a copied command reruns what ran rather than what a form would suggest —
    including the `--ak` kwargs, which no other surface reproduces. The one
    outside fact is `--env`, which the job-level config does not carry; the
    caller passes it from a trial's recorded environment when it has one.

    A job that another harness produced records its own invocation under
    ``command`` and it is returned verbatim: reconstructing a ``harbor run``
    for a run Harbor never performed would print a line that cannot reproduce
    it, which is the one thing this function exists not to do. No Harbor
    config carries that key, so this cannot shadow a real one.
    """
    recorded = cfg.get("command")
    if isinstance(recorded, str) and recorded.strip():
        return recorded.strip()

    agents = cfg.get("agents") or [{}]
    agent = agents[0]
    name = agent.get("name", "")
    if not name:
        return ""

    # No PYTHONPATH is reconstructed, whatever the recorded name looks like: the
    # lane in service is Harbor's built-in agent, and a module:Class name in an
    # old job is a fact about that job, not an instruction to rebuild the
    # adapter checkout it once resolved in. The --agent value is what the job
    # recorded, verbatim.
    parts = ["harbor", "run"]

    n_tasks = 0
    datasets = cfg.get("datasets") or []
    if datasets:
        d0 = datasets[0]
        if d0.get("path"):
            parts += ["-p", shlex.quote(d0["path"])]
        elif d0.get("name"):
            ref = d0["name"] + (f"@{d0['version']}" if d0.get("version") else "")
            parts += ["-d", shlex.quote(ref)]
        n_tasks = d0.get("n_tasks") or 0

    if env_type:
        parts += ["--env", shlex.quote(env_type)]
    if agent.get("model_name"):
        parts += ["--model", shlex.quote(agent["model_name"])]
    parts += ["--agent", shlex.quote(name)]
    for key, value in (agent.get("kwargs") or {}).items():
        parts += ["--ak", shlex.quote(f"{key}={value}")]
    if n_tasks:
        parts += ["--n-tasks", str(n_tasks)]
    if cfg.get("n_concurrent_trials"):
        parts += ["--n-concurrent", str(cfg["n_concurrent_trials"])]
    if (cfg.get("retry") or {}).get("max_retries"):
        parts += ["--max-retries", str(cfg["retry"]["max_retries"])]
    return " ".join(parts)


# Harbor writes config.json with `exclude_defaults=True` (harbor/job.py,
# 0.22): a key it leaves out held Harbor's default, which is not the
# builder's (concurrency 4 and no retries here, 1 and 3 there). Read from
# harbor/models/job/config.py at 0.22; a launch says which ones it filled.
HARBOR_DEFAULTS = {"n_concurrent_trials": 4, "max_retries": 0, "n_attempts": 1, "timeout_multiplier": 1.0}
_PHASE_MULTIPLIERS = ("agent_timeout_multiplier", "agent_setup_timeout_multiplier",
                      "verifier_timeout_multiplier", "environment_build_timeout_multiplier")
# A launch carries only the name of a value filed under a name like these: it
# is a credential, and the record is embedded in the builder's page.
_SECRET_KEY = re.compile(r"(?:api[-_]?key|token|password|authorization|credential|secret)", re.I)
# What a recipe can say; any other key a config recorded is named, not dropped.
_LAUNCH_TOP = {"job_name", "jobs_dir", "quiet", "debug", "agents", "datasets", "tasks", "retry",
               "n_concurrent_trials", "n_attempts", "timeout_multiplier", "environment", *_PHASE_MULTIPLIERS}
_LAUNCH_AGENT = {"name", "import_path", "model_name", "kwargs", "env"}
_LAUNCH_DATASET = {"path", "name", "version", "task_names", "exclude_task_names", "n_tasks"}


def _scrub(values, at: str = "") -> tuple:
    """(kept, secret names): string values redacted, credential-named ones left
    out - at any depth, since a kwarg may nest them (``llm_kwargs.api_key``,
    an MCP server's ``headers.Authorization``); a nested one is named by its path."""
    kept, secret = {}, []
    for key, value in (values if isinstance(values, dict) else {}).items():
        name = f"{at}{key}"
        if _SECRET_KEY.search(str(key)):
            secret.append(name)
        else:
            kept[str(key)], inner = _scrub_value(value, name)
            secret += inner
    return kept, secret


def _scrub_value(value, at: str) -> tuple:
    if isinstance(value, str):
        return redact(value), []
    if isinstance(value, dict):
        return _scrub(value, at + ".")
    if isinstance(value, list):
        out, secret = [], []
        for i, v in enumerate(value):
            v, inner = _scrub_value(v, f"{at}[{i}]")
            out.append(v); secret += inner
        return out, secret
    return value, []


def launch_of(cfg: dict) -> dict:
    """How a job was launched, from its config.json: the agents with their
    models, kwargs and environment, the first dataset and its task filters,
    the limits and the timeout multipliers - Harbor's defaults filled in for
    the keys it left out (``defaulted`` names them), credential-named values
    left out (``secret`` names them per agent), and every recorded key a
    builder recipe has no field for named in ``unexpressed``. Empty for a
    config another harness wrote (``command``) and for no config at all."""
    if not isinstance(cfg, dict) or not cfg or cfg.get("command"):
        return {}
    unexpressed = sorted(k for k in cfg if k not in _LAUNCH_TOP)
    agents = []
    for i, a in enumerate(cfg.get("agents") or []):
        if not isinstance(a, dict):
            continue
        kwargs, secret = _scrub(a.get("kwargs"))
        env, secret_env = _scrub(a.get("env"))
        agents.append({"name": a.get("name") or a.get("import_path") or "", "model": a.get("model_name") or "",
                       "kwargs": kwargs, "env": env, "secret": secret + secret_env})
        unexpressed += [f"agents[{i}].{k}" for k in sorted(a) if k not in _LAUNCH_AGENT]
    datasets = [d for d in cfg.get("datasets") or [] if isinstance(d, dict)]
    d0 = datasets[0] if datasets else {}
    dataset = ({"path": redact(str(d0["path"]))} if d0.get("path") else
               {"name": str(d0["name"]), "version": str(d0.get("version") or "")} if d0.get("name") else {})
    unexpressed += [f"datasets[0].{k}" for k in sorted(d0) if k not in _LAUNCH_DATASET]
    unexpressed += [f"datasets[{i}]" for i in range(1, len(datasets))]
    retry = cfg.get("retry") if isinstance(cfg.get("retry"), dict) else {}
    unexpressed += [f"retry.{k}" for k in sorted(retry) if k != "max_retries"]
    environment = cfg.get("environment") if isinstance(cfg.get("environment"), dict) else {}
    unexpressed += [f"environment.{k}" for k in sorted(environment) if k != "type"]
    recorded = {"n_concurrent_trials": cfg.get("n_concurrent_trials"), "max_retries": retry.get("max_retries"),
                "n_attempts": cfg.get("n_attempts"), "timeout_multiplier": cfg.get("timeout_multiplier")}
    return {
        "agents": agents,
        "dataset": dataset,
        "tasks": len(cfg.get("tasks") or []),
        "task_names": [str(t) for t in d0.get("task_names") or []],
        "exclude_task_names": [str(t) for t in d0.get("exclude_task_names") or []],
        "n_tasks": d0.get("n_tasks") or None,
        **{k: (HARBOR_DEFAULTS[k] if v is None else v) for k, v in recorded.items()},
        **{k: cfg.get(k) for k in _PHASE_MULTIPLIERS},
        "environment": str(environment.get("type") or ""),
        "defaulted": [k for k, v in recorded.items() if v is None],
        "unexpressed": unexpressed,
    }


def _budget(b) -> dict:
    """A job's recorded spending cap, read leniently: {} unless it names a cap; an amount that is no number is
    None (not reported), never zero."""
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if not isinstance(b, dict) or not num(b.get("max_usd")):
        return {}
    names = lambda v: [str(x) for x in v] if isinstance(v, list) else []
    return {"max_usd": b["max_usd"], "spent_usd": num(b.get("spent_usd")), "unsettled_usd": num(b.get("unsettled_usd")),
            "stopped": b.get("stopped") is True, "cut_off": names(b.get("cut_off")), "not_run": names(b.get("not_run"))}


def parse_job(job_dir: str, reuse=None) -> Job:
    """One job directory. ``reuse(trial_dir)``: a Trial already parsed from
    files that have not changed since (a workspace's cache, for a running
    job), or None to parse it."""
    res = verifier._read_json(os.path.join(job_dir, "result.json")) or {}
    cfg = verifier._read_json(os.path.join(job_dir, "config.json")) or {}
    stats = res.get("stats") or {}

    agents = cfg.get("agents") or [{}]
    model = agents[0].get("model_name", "")

    # Three config shapes in the wild: `datasets: [{path, n_tasks}]` for a
    # local checkout, `datasets: [{name, version, n_tasks}]` for a registry
    # fetch (no `path` key at all), and `tasks: [{name: "<dataset>/<task>"}]`
    # where the dataset is only a prefix.
    datasets = cfg.get("datasets") or []
    tasks = cfg.get("tasks") or []
    dataset_path = ""
    if datasets:
        d0 = datasets[0]
        dataset = dataset_path = d0.get("path", "")
        if not dataset:
            name, version = d0.get("name", ""), d0.get("version", "")
            dataset = f"{name}@{version}" if name and version else name
        n_tasks = d0.get("n_tasks") or 0
    elif tasks:
        prefixes = {
            t["name"].rsplit("/", 1)[0]
            for t in tasks
            if isinstance(t.get("name"), str) and "/" in t["name"]
        }
        dataset = prefixes.pop() if len(prefixes) == 1 else ("mixed" if prefixes else "")
        n_tasks = len(tasks)
    else:
        dataset, n_tasks = "", 0

    job = Job(
        job_id=os.path.basename(job_dir),
        dir=job_dir,
        model_name=model,
        dataset=os.path.basename(dataset.rstrip("/")) if dataset else "",
        n_tasks=n_tasks,
        started_at=_iso(res.get("started_at")),
        finished_at=_iso(res.get("finished_at")),
        agent_name=agents[0].get("name", ""),
        reasoning_effort=str((agents[0].get("kwargs") or {}).get("reasoning_effort", "")),
        n_total_trials=res.get("n_total_trials") or 0,
        n_errored_trials=stats.get("n_errored_trials") or 0,
        n_running_trials=stats.get("n_running_trials") or 0,
        n_pending_trials=stats.get("n_pending_trials") or 0,
        n_retries=stats.get("n_retries") or 0,
        preparing=is_preparing_job_dir(job_dir),
        # what a job recorded about itself is shown and copied verbatim, so a
        # key that leaked into a path or an importer's --command is scrubbed here
        dataset_path=redact(dataset_path),
    )

    for name in sorted(os.listdir(job_dir)):
        sub = os.path.join(job_dir, name)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "result.json")):
            t = (reuse(sub) if reuse else None) or parse_trial(job.job_id, sub)
            if t:
                job.trials.append(t)
    if not job.agent_name:
        # a config written with defaults left out names no agent for Harbor's default one; the trials do
        job.agent_name = next((t.agent_info_name for t in job.trials if t.agent_info_name), "")
    job.runner = str(cfg.get("runner_label") or cfg.get("runner") or "") if isinstance(cfg.get("runner"), str) else ""
    job.runner_commit = str(cfg.get("runner_commit") or "")[:12] if job.runner else ""
    job.runner_judge = str(cfg.get("runner_judge") or "") if job.runner else ""
    job.questions_from = str(cfg.get("questions_from") or "") if job.runner else ""
    words = cfg.get("runner_items") if job.runner else None
    job.runner_items = [str(w) for w in words[:2]] if isinstance(words, list) and len(words) >= 2 else []
    std = cfg.get("standard") if isinstance(cfg.get("standard"), dict) else {}
    ctl = std.get("control") if isinstance(std.get("control"), dict) else {}
    job.control_source = str(ctl.get("source") or "control") if ctl.get("name") == job.agent_label.lower() else ""
    job.launch_id = str(std.get("launch") or "")
    job.budget = _budget(res.get("budget"))
    job.standard = bool(std)
    # a runner's job was never a `harbor run`: there is no command to rebuild
    job.harbor_command = "" if job.runner else redact(harbor_command(
        cfg, next((t.env_type for t in job.trials if t.env_type), "")))
    job.launch = launch_of(cfg)
    if len(agents) > 1 and not job.runner:
        job.agents = [{"name": a.get("name") or a.get("import_path") or "", "model": a.get("model_name") or "",
                       "effort": str((a.get("kwargs") or {}).get("reasoning_effort", "")),
                       "command": redact(harbor_command(dict(cfg, agents=[a]),
                                                        next((t.env_type for t in job.trials if t.env_type), "")))}
                      for a in agents if isinstance(a, dict)]
    lock = verifier._read_json(os.path.join(job_dir, "lock.json")) or {}
    harbor = lock.get("harbor") if isinstance(lock.get("harbor"), dict) else {}
    job.harbor_version = str(harbor.get("version") or "")
    job.harbor_editable = harbor.get("is_editable") if isinstance(harbor.get("is_editable"), bool) else None
    return job


def _agent_of(t, agents: list) -> int:
    """Which of a job's agents ran trial ``t``: by its model, then its name."""
    def named(a):
        return t.agent_info_name and t.agent_info_name in (a["name"], a["name"].split(":")[-1])
    by_model = [i for i, a in enumerate(agents) if a["model"] == t.model_name]
    if len(by_model) == 1:
        return by_model[0]
    pool = by_model or list(range(len(agents)))
    named_pool = [i for i in pool if named(agents[i])]
    pool = named_pool or pool
    if len(pool) == 1:
        return pool[0]
    exact = [i for i in pool if agents[i]["effort"] == t.reasoning_effort]
    return exact[0] if len(exact) == 1 else -1


def split_agents(jobs: list) -> list:
    """A Harbor job of several agents or models as one view per agent, in
    their config's order: each its own trials, rate, control or not, and
    command. Pooled under its first agent, model-b's misses were shown as
    model-a's and an oracle listed first made the real agent a control. A
    view's id is the job's with the agent after ``~``; its trials link to it.
    Harbor's job-level counts are shared evenly; the rest is the view's own."""
    out = []
    for job in jobs:
        if len(job.agents) < 2:
            out.append(job)
            continue
        k = len(job.agents)
        groups = {i: [] for i in range(k)}
        for t in job.trials:
            groups.setdefault(_agent_of(t, job.agents), []).append(t)
        labels = set()
        for i, a in enumerate(job.agents):
            label = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{a['name'].split(':')[-1]}-{a['model']}".strip("-")) or str(i + 1)
            if label in labels:
                label += f"-{i + 1}"
            labels.add(label)
            vid = f"{job.job_id}~{label}"
            trials = [replace(t, job_id=vid) for t in groups[i]]
            out.append(replace(
                job, job_id=vid, model_name=a["model"], agent_name=a["name"], reasoning_effort=a["effort"],
                trials=trials, harbor_command=a["command"], agents=[],
                n_total_trials=job.n_total_trials // k if job.n_total_trials % k == 0 else len(trials),
                n_errored_trials=sum(1 for t in trials if t.exception_type),
                n_running_trials=job.n_running_trials // k + (i < job.n_running_trials % k),
                n_pending_trials=job.n_pending_trials // k + (i < job.n_pending_trials % k),
                n_retries=job.n_retries // k + (i < job.n_retries % k)))
        orphans = groups.get(-1)
        if orphans:                       # a trial of an agent the config does not list: kept, under the job's own id
            out.append(replace(job, trials=orphans, agents=[]))
    return out


def load_all(roots) -> list[Job]:
    jobs = split_agents([parse_job(d) for d in find_job_dirs(roots)])
    # Newest first. Compared as epoch numbers, not datetimes: a job whose
    # result.json never got a started_at (a crashed run) yields None, and
    # datetime.min is naive where parsed stamps are aware — mixing the two
    # in one sort raises. -inf sorts the undated to the end instead.
    jobs.sort(
        key=lambda j: j.started_at.timestamp() if j.started_at else float("-inf"),
        reverse=True,
    )
    return jobs
