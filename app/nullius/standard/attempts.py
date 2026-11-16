"""What a repeat is tried again after, and how many times (``cli run --attempts``).

A repeat is one run of a unit, counted whatever its result (``--repeat``). An
attempt is one try within a repeat: a repeat that ended in a failure a further
try may mend runs again, after the run's other units and one at a time, until
it ends otherwise or has had ``ATTEMPTS`` tries (2 by default, ``MAX_ATTEMPTS``
at most); each try before is kept under the job's ``retried/``.

Which failures, as Harbor's retry decides it (its ``RetryConfig``): any
exception but those that are the agent's own result or a mistake no second try
mends - and here a missing secret, the spending caps and a stopped run. A task
the agent failed carries no exception and is never tried again.
"""
from __future__ import annotations

from typing import Optional

from nullius.standard import ledger

ATTEMPTS = 2
MAX_ATTEMPTS = 10

NOT_RETRIED = frozenset({
    # Harbor's RetryConfig.exclude_exceptions: a result, or a mistake a second try does not mend
    "AgentTimeoutError", "VerifierTimeoutError", "RewardFileNotFoundError", "RewardFileEmptyError",
    "VerifierOutputParseError", "ApiUsageLimitError", "AgentSafetyRefusalError", "AgentAuthenticationError",
    "ModelNotFoundError",
    # this runner's own: a secret no second try provides, the spending caps, a stopped run
    "EnvironmentSecretMissingError", ledger.ERROR, ledger.UNIT_ERROR, "CancelledError",
})


def retryable(res: Optional[dict]) -> bool:
    """Whether a repeat's result is a failure a further attempt may mend."""
    et = ((res or {}).get("exception_info") or {}).get("exception_type")
    return bool(et) and et not in NOT_RETRIED
