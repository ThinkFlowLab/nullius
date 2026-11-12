"""Outcome classification.

The whole point of the app: separate *infrastructure* failures (rate limits,
setup crashes) from genuine *agent* outcomes (solved / wrong patch / no patch),
so the solve rate reflects the agent and not the harness.
"""

from __future__ import annotations

# Outcome labels, ordered from best to worst for display.
RESOLVED = "resolved"
# A benchmark that gives a trial a score and no pass or fail (a token F1 over a
# conversation's questions): the trial was answered and measured, neither
# resolved nor failed. Its score is the measure; the run has no solve rate.
SCORED = "scored"
PATCH_FAILED_TESTS = "patch_failed_tests"
# The benchmark-neutral failure: the verifier said no, and the benchmark
# reports nothing finer than the score. What a failed trial of a dataset
# whose verifier reports no patch detail, or that the app knows only by its
# reward, classifies as.
FAILED_VERIFICATION = "failed_verification"
PATCH_NOT_APPLIED = "patch_not_applied"
NO_PATCH = "no_patch"
RATE_LIMITED = "rate_limited"
API_ERROR = "api_error"
SETUP_ERROR = "setup_error"
# The run's own spending cap refused a model call the unit needed (the
# standard's relay, ``standard.ledger``): the unit was cut off. Nothing
# failed, and no attempt was had: the money ran out, by the user's own word.
SPENDING_CAP = "spending_cap"
CANCELLED = "cancelled"
AGENT_ERROR = "agent_error"
UNKNOWN = "unknown"

# Outcomes excluded from the adjusted rate: infrastructure failures or
# interruptions imposed by the run, not the agent's ability.
INFRA_OUTCOMES = {RATE_LIMITED, API_ERROR, SETUP_ERROR, SPENDING_CAP, CANCELLED}

ORDER = [
    RESOLVED,
    SCORED,
    PATCH_FAILED_TESTS,
    FAILED_VERIFICATION,
    PATCH_NOT_APPLIED,
    NO_PATCH,
    AGENT_ERROR,
    RATE_LIMITED,
    API_ERROR,
    SETUP_ERROR,
    CANCELLED,
    SPENDING_CAP,
    UNKNOWN,
]

# Harbor names its provider failures Api*, all descending from ApiError, so the
# prefix follows their hierarchy instead of guessing at it — and a class they
# add later is classified correctly without us noticing it exists.
#
# The two below are the exceptions to that rule: they are provider or config
# failures that don't carry the prefix. Everything Harbor ships is pinned by a
# test, so this list cannot quietly fall behind theirs.
_API_PREFIX = "api"
_INFRA_EXCEPTIONS = {
    "agentauthenticationerror",   # bad or missing credentials — config, not skill
    "modelnotfounderror",         # the model name is wrong; the agent never ran
    "networkconnectionerror",     # DNS, refused connection, TLS — transport, not skill
    "unknownapierror",            # Harbor's own "some provider error"; an ApiError without the prefix
}
# What a unit the run's spending cap cut off carries (``standard.ledger.ERROR``): recorded by the relay that
# refused the call, never inferred.
_CAP_EXCEPTION = "spendingcaperror"

# What Harbor's agents raise when the agent itself fails — its budget spent,
# its own context filled, its own output truncated, a refusal, its process
# exiting non-zero. Each is the agent's behaviour under conditions it was
# given, so each is a result and belongs in the denominator, charged to the
# agent. Deliberately *not* infra; move one only with a reason written down.
#
# The list is explicit and short on purpose. An exception on none of these
# lists is UNKNOWN: still in the denominator, charged to nobody. The
# alternative — everything unmatched is the agent's fault — made a crash in
# the harness, the verifier or this reader read as poor coding. The trial
# page shows the recorded type beside the label, so a reader who meets a new
# one can place it here.
_AGENT_EXCEPTIONS = {
    "agenttimeouterror",            # the agent spent its budget — that is a result
    "unitspendingcaperror",         # the unit spent its own spending cap (the standard's relay): the same, in money
    "agentsafetyrefusalerror",      # the agent (or its provider) chose to refuse
    "contextwindowexceedederror",   # the agent filled its own context
    "contextlengthexceedederror",   # same, from Harbor's LLM layer
    "outputlengthexceedederror",    # the agent's reply hit max_tokens
    "outputtokenexceedederror",     # same, seen from an installed agent's output
    "nonzeroagentexitcodeerror",    # the agent's process died and Harbor found no finer reason
}

LABELS = {
    RESOLVED: "Resolved",
    SCORED: "Scored",
    PATCH_FAILED_TESTS: "Patch failed tests",
    FAILED_VERIFICATION: "Failed verification",
    PATCH_NOT_APPLIED: "Patch did not apply",
    NO_PATCH: "No patch produced",
    AGENT_ERROR: "Agent error",
    RATE_LIMITED: "Rate limited",
    API_ERROR: "API error",
    SETUP_ERROR: "Setup error",
    SPENDING_CAP: "Spending cap",
    CANCELLED: "Run stopped",
    UNKNOWN: "Unknown",
}


# One-line meaning of each outcome, for the glossary the UI renders. Lives here
# rather than in a template so the wording follows the classifier that produces
# it — change the rule below, change the sentence with it.
DESCRIPTIONS = {
    RESOLVED: "The patch applied and the verifier's tests all passed. A genuine solve.",
    SCORED: (
        "The benchmark gave the trial a score and no pass or fail - a token F1 over a "
        "conversation's questions, partial credit. Answered and measured, neither "
        "solved nor failed: the score is the result, and the run has no solve rate."
    ),
    PATCH_FAILED_TESTS: (
        "The agent produced a patch that applied cleanly, but the tests still failed. "
        "A real attempt that got the answer wrong — the most informative failure."
    ),
    FAILED_VERIFICATION: (
        "The verifier ran and scored the attempt below success, on a benchmark that "
        "reports no patch detail, or that the app knows only "
        "by its reward. A real attempt that didn't pass; counted against the agent."
    ),
    PATCH_NOT_APPLIED: (
        "A patch was produced but wouldn't apply to the repository — usually malformed "
        "context lines or edits against the wrong revision."
    ),
    NO_PATCH: "The agent finished without producing a patch at all.",
    AGENT_ERROR: (
        "The agent itself failed — it timed out, filled its context, truncated its "
        "own output, refused, spent its own spending cap, or its process exited non-zero. Counted against the "
        "agent, because each of those is a result rather than an interruption."
    ),
    RATE_LIMITED: (
        "The model API returned a rate-limit error (HTTP 429) and the run died. "
        "Infrastructure, not capability — excluded from the adjusted rate."
    ),
    API_ERROR: (
        "The model API failed some other way — overloaded, unreachable, out of "
        "quota, or the key was rejected. The agent never got a fair attempt, so "
        "this is infrastructure too and is excluded from the adjusted rate."
    ),
    SETUP_ERROR: (
        "The environment or image failed to build or start, so the agent never ran. "
        "Infrastructure, not capability — excluded from the adjusted rate."
    ),
    SPENDING_CAP: (
        "The run reached its spending cap: its relay refused a model call this "
        "trial needed, so the trial was cut off. Nothing failed and no attempt "
        "was had, so it is excluded from the adjusted rate."
    ),
    CANCELLED: (
        "The run was stopped while this trial was in progress. Its attempt was interrupted, "
        "so it is excluded from the adjusted rate."
    ),
    UNKNOWN: (
        "No verifier report and no exception, or an exception this reader does not "
        "recognise — a crash in the harness, the verifier or the reader itself. The "
        "outcome could not be determined; it stays in the denominator, charged to "
        "nobody, and the recorded type is shown so the classifier can be extended."
    ),
}


def from_verdict(exception_type, verdict):
    """Return an outcome label from the exception type and a `Verdict`.

    An exception outranks the verifier: a trial that crashed did not earn
    whatever the verifier managed to say about it. Which side it is charged
    to comes from the lists above; an exception on none of them is `UNKNOWN`.

    The patch-shaped labels are reachable only for a benchmark whose reader
    fills `produced` / `usable`. A verdict that failed without that detail is
    `FAILED_VERIFICATION` — the verifier said no, and this benchmark has
    nothing further to say about why — rather than borrowing a label about
    patches that would not be true.
    """
    if exception_type:
        et = exception_type.lower().rpartition(".")[2]  # tolerate dotted paths
        if et == "cancellederror":
            return CANCELLED
        if et == _CAP_EXCEPTION:
            return SPENDING_CAP
        # Rate limiting first: it is an Api* class too, but common enough to be
        # worth seeing on its own rather than folded into the generic bucket.
        if "ratelimit" in et or "rate_limit" in et or "429" in et:
            return RATE_LIMITED
        if "setup" in et or "environment" in et or "build" in et:
            return SETUP_ERROR
        if et.startswith(_API_PREFIX) or et in _INFRA_EXCEPTIONS:
            return API_ERROR
        if et in _AGENT_EXCEPTIONS:
            return AGENT_ERROR
        # Neither side claims it. Not the agent's fault by default: the
        # trial stays counted and nobody is charged.
        return UNKNOWN

    if verdict is None or verdict.succeeded is None:
        return UNKNOWN
    if verdict.succeeded:
        return RESOLVED
    if verdict.produced is None:
        return FAILED_VERIFICATION
    if not verdict.produced:
        return NO_PATCH
    if verdict.usable is False:
        return PATCH_NOT_APPLIED
    return PATCH_FAILED_TESTS
