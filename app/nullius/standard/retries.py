"""What an attempt is tried again after, and how many times (``cli run --retries``).

An attempt is one run of a unit, counted whatever its result (``--attempts``).
A retry runs an attempt again after a failure a further try may mend: after
the run's other units and one at a time, up to ``--retries`` times (0 by
default, ``MAX_RETRIES`` at most). Each try before is kept under the job's
``retried/``; only the last is the attempt's trial and counts.

Which failures, as Harbor's retry decides it (its ``RetryConfig``): any
exception but those that are the agent's own result or a mistake no second try
mends - and here a missing secret, the spending caps and a stopped run. A task
the agent failed carries no exception and is never retried.
"""
from __future__ import annotations

from typing import Optional

from nullius.standard import ledger

RETRIES = 0
MAX_RETRIES = 9

NOT_RETRIED = frozenset({
    # Harbor's RetryConfig.exclude_exceptions: a result, or a mistake a second try does not mend
    "AgentTimeoutError", "VerifierTimeoutError", "RewardFileNotFoundError", "RewardFileEmptyError",
    "VerifierOutputParseError", "ApiUsageLimitError", "AgentSafetyRefusalError", "AgentAuthenticationError",
    "ModelNotFoundError",
    # this runner's own: a secret no second try provides, the spending caps, a stopped run
    "EnvironmentSecretMissingError", ledger.ERROR, ledger.UNIT_ERROR, "CancelledError",
})


def retryable(res: Optional[dict]) -> bool:
    """Whether an attempt's result is a failure a retry may mend."""
    et = ((res or {}).get("exception_info") or {}).get("exception_type")
    return bool(et) and et not in NOT_RETRIED
