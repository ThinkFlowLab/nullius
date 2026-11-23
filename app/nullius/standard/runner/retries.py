"""What an attempt is tried again after, and how many times (``cli run --retries``).

An attempt is one run of a unit, counted whatever its result (``--attempts``).
A retry runs an attempt again after a failure a further try may mend: after
the run's other units, as many at once as the run's own, up to ``--retries`` times (0 by
default, ``MAX_RETRIES`` at most). Each try before is kept under the job's
``retried/``; only the last is the attempt's trial and counts.

Which failures, as Harbor's retry decides it (its ``RetryConfig``): any
exception but those a second try meets again - a wrong key or model, a usage
limit, a time the agent or the benchmark ran out of - and here a missing
secret, a setup this machine cannot give (the trial's ``exception_info`` says
``retryable: false``), the spending caps and a stopped run. A time run out
while the provider refused or failed the calls is the provider's failure
(``relay.failure``), retried; a rate limit's retry waits the provider's
pause (``pace``). A task the agent failed carries no exception and is never
retried. An attempt that spent its own spending cap, all its tries together,
is not tried again.
"""
from __future__ import annotations

from typing import Optional

from nullius.standard.calls import ledger, relay
from nullius.standard.runner import pace

RETRIES = 0
MAX_RETRIES = 9

NOT_RETRIED = frozenset({
    # Harbor's RetryConfig.exclude_exceptions: a result, or a mistake a second try does not mend
    "AgentTimeoutError", "VerifierTimeoutError", "RewardFileNotFoundError", "RewardFileEmptyError",
    "VerifierOutputParseError", "ApiUsageLimitError", "AgentSafetyRefusalError", "AgentAuthenticationError",
    "ModelNotFoundError",
    # this runner's own
    "EnvironmentSecretMissingError", ledger.ERROR, ledger.UNIT_ERROR, relay.CALLS_ERROR, "CancelledError",
    relay.CONTEXT_ERROR,
})


def retryable(res: Optional[dict]) -> bool:
    """Whether an attempt's result is a failure a retry may mend."""
    info = (res or {}).get("exception_info") or {}
    et = info.get("exception_type")
    return bool(et) and et not in NOT_RETRIED and info.get("retryable") is not False


def rate_limited(res: Optional[dict]) -> bool:
    """Whether an attempt ended refused by the provider's rate limit."""
    return ((res or {}).get("exception_info") or {}).get("exception_type") == pace.RATE_LIMITED
