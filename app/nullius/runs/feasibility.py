"""The feasibility sweep, read back from what the controls recorded.

A sweep runs Harbor's two control agents over a task set to ask one question
per task — *is the task feasible*: can an empty solution fail, and can a
reference solution pass? ``nop`` does nothing, so the verifier must reject
it; ``oracle`` applies the task's reference solution, so the verifier must
accept it. Environment setup, data loading and the verifier are all
exercised on the way. It is feasibility evidence and nothing else: not a
configuration or hyperparameter search, and no promise that an agent will
succeed on a feasible task.

This module builds the report from recorded jobs. It never runs anything.

Where the controls are on disk: a run started from the builder with several
agents writes one job per agent, ``<name>-nop``, ``<name>-oracle`` and
``<name>-<agent>``; the portable bundle's ``--bracket`` writes
``<name>-bracket-nop`` and ``<name>-bracket-oracle``; a sweep may also be
a control job on its own. ``related`` finds the controls that belong with a
job by those names, within the one workspace, and ``report`` reads them.

Per task the report keeps **both** control outcomes and classifies from the
pair, in this order — the first that applies wins, so a task is called
feasible only when both sides ran and said the right thing:

    unexpected_reward    nop was accepted: the verifier rejects nothing, and
                         nothing else about the task can be trusted
    task_broken          nop was rejected, but the verifier says the empty
                         solution failed tests that were passing before it —
                         held (PASS_TO_PASS) checks. Nothing was changed, so
                         the task was already broken as shipped, or its
                         verifier is unstable; whatever oracle said, the
                         task is not feasible
    missing              a side is absent (never ran, or not in this
                         workspace) or recorded no outcome
    setup_failure        a side failed before the verifier ran — an
                         environment, image, API or agent error — so the
                         task was not exercised
    reference_rejected   oracle ran and the verifier rejected the reference
                         solution: the task cannot be passed as shipped
    feasible             oracle accepted, nop rejected, both by the verifier

One side alone never validates a task: oracle-only evidence cannot show that
a verifier rejects an empty solution, nop-only evidence cannot show that a
solution can pass. The header says which controls actually executed, and
over what scope, so a report over one control reads as exactly that.

``task_broken`` is the same facet the trial page shows as *⚠ Broke tests*
(`Trial.is_regression`, from `p2p_fail`), read on the control instead of the
agent: on the agent's trial a held failure is a regression the patch caused;
on ``nop``, which applies nothing, it is a failure the task brought with it.
It is benchmark-agnostic — any reader that fills ``held`` gets it — and a
benchmark whose verifier reports no held checks simply never
produces the class, rather than a plausible "not broken".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import classify
from .parse import CONTROL_AGENTS

NOP, ORACLE = "nop", "oracle"
CONTROLS = (NOP, ORACLE)

FEASIBLE = "feasible"
UNEXPECTED_REWARD = "unexpected_reward"
TASK_BROKEN = "task_broken"
SETUP_FAILURE = "setup_failure"
REFERENCE_REJECTED = "reference_rejected"
MISSING = "missing"
ORDER = [FEASIBLE, UNEXPECTED_REWARD, TASK_BROKEN, REFERENCE_REJECTED, SETUP_FAILURE, MISSING]
LABELS = {
    FEASIBLE: "Feasible",
    UNEXPECTED_REWARD: "Unexpected reward",
    TASK_BROKEN: "Task broken",
    REFERENCE_REJECTED: "Reference rejected",
    SETUP_FAILURE: "Setup/data failure",
    MISSING: "Missing result",
}
DESCRIPTIONS = {
    FEASIBLE: "The verifier rejected the empty solution and accepted the reference one. Both controls ran; the task is feasible.",
    UNEXPECTED_REWARD: "The verifier accepted the empty solution. It rejects nothing, so a pass on this task means nothing — whatever the oracle did.",
    TASK_BROKEN: "The empty solution failed tests that were passing before it. nop changes nothing, so the task was already broken as shipped, or its verifier is unstable — the failures are named. Not feasible, whatever the oracle did.",
    REFERENCE_REJECTED: "Both controls reached the verifier, and it rejected the reference solution. The task cannot be passed as shipped.",
    SETUP_FAILURE: "A control failed before the verifier ran — environment build, image, API or agent error — so the task was not exercised on that side.",
    MISSING: "A control has no recorded outcome for this task: it did not run, ran elsewhere, or recorded nothing. Not validated.",
}

# Outcomes that mean a control reached the verifier and was rejected.
_REJECTED = {classify.FAILED_VERIFICATION, classify.PATCH_FAILED_TESTS, classify.PATCH_NOT_APPLIED, classify.NO_PATCH}
# Outcomes that mean the control failed before the verifier could say anything.
_FAILED_BEFORE_VERIFIER = classify.INFRA_OUTCOMES | {classify.AGENT_ERROR}

_SUFFIX = re.compile(r"-(?:bracket-)?(?:nop|oracle)$")


@dataclass
class Side:
    """One control's outcome on one task. ``present`` is False when the
    control never recorded this task — that is the missing case, kept apart
    from a recorded outcome of ``unknown``."""

    control: str
    present: bool = False
    job_id: str = ""
    trial_name: str = ""
    outcome: str = ""
    reward: float | None = None
    exception_type: str = ""
    # Held (PASS_TO_PASS) checks the verifier saw fail on this control, as
    # `Trial.p2p_fail` / `p2p_failures` record them — the same reading the
    # trial page flags as *⚠ Broke tests*.
    p2p_fail: int = 0
    p2p_failures: list = field(default_factory=list)

    @property
    def label(self) -> str:
        if not self.present:
            return "not run"
        return classify.LABELS.get(self.outcome, self.outcome)

    @property
    def accepted(self) -> bool:
        return self.present and self.outcome == classify.RESOLVED

    @property
    def rejected(self) -> bool:
        return self.present and self.outcome in _REJECTED

    @property
    def failed(self) -> bool:
        return self.present and self.outcome in _FAILED_BEFORE_VERIFIER

    @property
    def unscored(self) -> bool:
        return not self.present or self.outcome == classify.UNKNOWN

    @property
    def broke_tests(self) -> bool:
        """The verifier ran and saw previously-passing tests fail."""
        return self.present and self.p2p_fail > 0


@dataclass
class TaskRow:
    task_base: str
    task_name: str
    nop: Side
    oracle: Side
    verdict: str = MISSING

    @property
    def label(self) -> str:
        return LABELS[self.verdict]

    @property
    def validated(self) -> bool:
        """True only for `feasible`: both sides ran and the verifier answered
        both the right way. Never true with a side missing or failed."""
        return self.verdict == FEASIBLE

    @property
    def why(self) -> str:
        """The row's reason in one phrase, naming the side that decided it."""
        if self.verdict == UNEXPECTED_REWARD:
            return "nop accepted"
        if self.verdict == TASK_BROKEN:
            n = self.nop.p2p_fail
            return f"nop broke {n} passing test{'' if n == 1 else 's'}: " + ", ".join(self.nop.p2p_failures)
        if self.verdict == MISSING:
            sides = [s.control for s in (self.nop, self.oracle) if s.unscored]
            return ", ".join(f"{c} {'not run' if not getattr(self, c).present else 'unscored'}" for c in sides)
        if self.verdict == SETUP_FAILURE:
            sides = [s for s in (self.nop, self.oracle) if s.failed]
            return ", ".join(f"{s.control}: {s.exception_type or s.label}" for s in sides)
        if self.verdict == REFERENCE_REJECTED:
            return f"oracle {self.oracle.label.lower()}"
        return "nop rejected, oracle accepted"


@dataclass
class ControlRun:
    """One control as it actually executed: the job it is, and how much of
    the scope it covered."""

    control: str
    job_id: str
    n_trials: int
    n_planned: int
    state: str          # preparing | running | finished

    @property
    def short(self) -> str:
        """The control and its coverage, without the job id: what the page shows."""
        planned = f" of {self.n_planned} planned" if self.n_planned and self.n_planned != self.n_trials else ""
        return f"{self.control} · {self.n_trials} trial{'s' if self.n_trials != 1 else ''}{planned}{' · still ' + self.state if self.state != 'finished' else ''}"

    @property
    def summary(self) -> str:
        """The same with the job it is: the hover, and the log line."""
        return self.short.replace(f"{self.control} · ", f"{self.control} · {self.job_id} · ", 1)


@dataclass
class ControlSummary:
    """One control job read as what it is: a check that behaved or did not,
    never a score.

    ``nop`` behaves when the verifier rejects it, ``oracle`` when the
    verifier accepts it; the other answer is the feasibility class for that
    side — ``unexpected reward`` for a resolved nop, ``reference rejected``
    for a failed oracle. A trial that failed before the verifier ran is a
    setup failure, and one with no verdict is unscored; neither says the
    task is feasible, so neither counts as behaved. The runs list shows
    this in place of the control's Solved/Raw/Adjusted cells and keeps its
    trials out of the agents' totals — a nop that failed every task is the
    expected result, not "Failed verification".
    """

    control: str
    n: int
    n_behaved: int
    n_unexpected: int
    n_failed: int
    n_unscored: int
    running: bool = False

    @property
    def expected(self) -> str:
        """What the verifier must say for this control to have behaved."""
        return "rejected" if self.control == NOP else "passed"

    @property
    def problem(self) -> str:
        """The feasibility class of the side that did not behave, or ''."""
        if self.n_unexpected:
            return "unexpected reward" if self.control == NOP else "reference rejected"
        if self.n_failed:
            return "setup failure"
        return ""

    @property
    def ok(self) -> bool:
        return self.n > 0 and self.n_behaved == self.n

    @property
    def state(self) -> str:
        """`ok`, `warn` or `pending` — the tone of the line and the cells."""
        if self.n_unexpected or self.n_failed:
            return "warn"
        if self.ok and not self.running:
            return "ok"
        return "pending"

    @property
    def label(self) -> str:
        """`5/5 rejected ✓`, `1 unexpected reward ⚠`, `3/5 passed · running`."""
        if not self.n:
            return "no trials yet" if self.running else "no trials"
        if self.problem:
            n = self.n_unexpected or self.n_failed
            return f"{n} {self.problem} ⚠"
        if self.n_unscored:
            return f"{self.n_behaved}/{self.n} {self.expected} · {self.n_unscored} unscored"
        if self.running:
            return f"{self.n_behaved}/{self.n} {self.expected} · running"
        return f"{self.n_behaved}/{self.n} {self.expected} ✓"


def summary_of(job) -> ControlSummary | None:
    """`job` read as a control, or None when it is an agent's run."""
    control = control_of(job)
    if not control:
        return None
    sides = [Side(control, True, job.job_id, t.trial_name, _outcome(t), t.reward, t.exception_type,
                  t.p2p_fail, list(t.p2p_failures)) for t in job.trials]
    behaved = (lambda s: s.rejected) if control == NOP else (lambda s: s.accepted)
    wrong = (lambda s: s.accepted) if control == NOP else (lambda s: s.rejected)
    return ControlSummary(
        control, len(sides),
        sum(1 for s in sides if behaved(s)),
        sum(1 for s in sides if wrong(s)),
        sum(1 for s in sides if s.failed),
        sum(1 for s in sides if s.unscored),
        running=bool(job.is_running or job.is_preparing),
    )


@dataclass
class Report:
    job_id: str
    dataset: str
    n_tasks: int                   # the task limit the job recorded; 0 is the full dataset
    executed: list = field(default_factory=list)      # [ControlRun], in CONTROLS order
    rows: list = field(default_factory=list)          # [TaskRow], by task name

    @property
    def not_run(self) -> list:
        ran = {c.control for c in self.executed}
        return [c for c in CONTROLS if c not in ran]

    @property
    def scope(self) -> str:
        """The task scope the job recorded, as the recipe asked for it."""
        ds = self.dataset or "the dataset"
        if self.n_tasks:
            return f"{self.n_tasks} task{'s' if self.n_tasks != 1 else ''} of {ds} (--n-tasks {self.n_tasks})"
        return f"every task of {ds} (no task limit recorded)"

    @property
    def counts(self) -> dict:
        c = {k: 0 for k in ORDER}
        for r in self.rows:
            c[r.verdict] += 1
        return c

    @property
    def n_feasible(self) -> int:
        return self.counts[FEASIBLE]

    @property
    def caveat(self) -> str:
        """What this evidence cannot say, when one control is absent."""
        missing = self.not_run
        if not missing:
            return ""
        if missing == [NOP]:
            return "Only oracle ran: this cannot establish that the verifier rejects an empty solution. No task is validated."
        if missing == [ORACLE]:
            return "Only nop ran: this cannot establish that a solution can pass. No task is validated."
        return "Neither control ran: nothing here is feasibility evidence."


def control_of(job) -> str:
    """`nop` or `oracle` when the job is one of Harbor's controls, or a
    standard job that ran a control its benchmark declares, else ''. A
    runner's arm is never one, whatever it is called (`Job.kind`)."""
    name = job.agent_label.lower()
    return name if name in CONTROL_AGENTS and job.is_control else ""


def _outcome(t) -> str:
    """A control's outcome on one task. A benchmark that scores and does not
    pass or fail leaves *scored*: at the ceiling (1) that is the reference
    accepted, at the floor (0) the answer rejected, in between neither."""
    if t.outcome == classify.SCORED and t.score is not None:
        if t.score >= 1.0:
            return classify.RESOLVED
        if t.score <= 0.0:
            return classify.FAILED_VERIFICATION
    return t.outcome


def base_name(job_id: str) -> str:
    """The recipe name a control job belongs to: the id without its control
    suffix, in either spelling the builder writes."""
    return _SUFFIX.sub("", job_id)


def related(job, jobs) -> dict:
    """The control jobs that belong with `job`, by control name.

    Matched by the builder's naming: a job's own base name, and — for an
    agent job named `<base>-<agent>` — the base with its last segment
    dropped. A control job is always its own side. The dataset must agree
    where both record one, so a control over another dataset that happens
    to share a name is not read as this job's.
    """
    out = {}
    own = control_of(job)
    if own:
        out[own] = job
    bases = [base_name(job.job_id)]
    if not own and "-" in job.job_id:
        bases.append(job.job_id.rsplit("-", 1)[0])
    wanted = {f"{b}{infix}{c}": c for b in bases for infix in ("-", "-bracket-") for c in CONTROLS}
    for other in jobs:
        c = wanted.get(other.job_id)
        if not c or c in out or control_of(other) != c:
            continue
        if job.dataset and other.dataset and job.dataset != other.dataset:
            continue
        out[c] = other
    return out


def _last_by_task(job) -> dict:
    """The last recorded attempt per task, as the run pages show one."""
    by_task = {}
    for t in job.trials:
        by_task[t.task_base] = t
    return by_task


def _side(control, job, task_base) -> Side:
    if job is None:
        return Side(control)
    t = _last_by_task(job).get(task_base)
    if t is None:
        return Side(control, job_id=job.job_id)
    return Side(control, True, job.job_id, t.trial_name, _outcome(t), t.reward, t.exception_type,
                t.p2p_fail, list(t.p2p_failures))


def verdict(nop: Side, oracle: Side) -> str:
    """The class of a task from its two sides — see the module docstring for
    the order and why a single side never validates."""
    if nop.accepted:
        return UNEXPECTED_REWARD
    if nop.broke_tests:
        return TASK_BROKEN
    if nop.unscored or oracle.unscored:
        return MISSING
    if nop.failed or oracle.failed:
        return SETUP_FAILURE
    if not oracle.accepted:
        return REFERENCE_REJECTED
    return FEASIBLE


def report(job, jobs) -> Report:
    """The feasibility report for `job`: its controls, the scope they ran
    over, and every task either control recorded with both outcomes."""
    controls = related(job, jobs)
    scoped = job if not control_of(job) else next((controls[c] for c in CONTROLS if c in controls), job)
    rep = Report(job_id=job.job_id, dataset=scoped.dataset, n_tasks=scoped.n_tasks)
    for c in CONTROLS:
        cj = controls.get(c)
        if cj is not None:
            rep.executed.append(ControlRun(c, cj.job_id, len(cj.trials), cj.n_planned, cj.state))
    # Every task any side recorded, and — for an agent job — every task the
    # agent ran, so a task the bracket never covered shows as missing rather
    # than disappearing from the report.
    names = {}
    for source in [job, *controls.values()]:
        for t in source.trials:
            names.setdefault(t.task_base, t.task_name)
    for task_base in sorted(names):
        nop, oracle = _side(NOP, controls.get(NOP), task_base), _side(ORACLE, controls.get(ORACLE), task_base)
        rep.rows.append(TaskRow(task_base, names[task_base], nop, oracle, verdict(nop, oracle)))
    return rep
