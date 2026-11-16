"""What an attempt is retried after: Harbor's retry rule, and this runner's own stops."""

import unittest

from nullius.standard import retries


def ended(exception_type):
    return {"exception_info": {"exception_type": exception_type}} if exception_type else {"verifier_result": {"rewards": {"reward": 0.0}}}


class Retryable(unittest.TestCase):
    def test_infrastructure_and_agent_crashes_are_retried(self):
        for et in ("RateLimitError", "ApiError", "NetworkConnectionError", "EnvironmentSetupError", "AgentSetupTimeoutError",
                   "NonZeroAgentExitCodeError", "OutputLengthExceededError", "VerifierError", "SomethingNewError"):
            with self.subTest(et=et):
                self.assertTrue(retries.retryable(ended(et)))

    def test_a_result_a_wrong_setting_a_cap_or_a_stop_is_not(self):
        for et in ("AgentTimeoutError", "VerifierTimeoutError", "AgentSafetyRefusalError", "AgentAuthenticationError",
                   "ModelNotFoundError", "ApiUsageLimitError", "EnvironmentSecretMissingError", "SpendingCapError",
                   "UnitSpendingCapError", "CancelledError"):
            with self.subTest(et=et):
                self.assertFalse(retries.retryable(ended(et)))

    def test_a_task_the_agent_failed_is_not(self):
        self.assertFalse(retries.retryable(ended(None)))
        self.assertFalse(retries.retryable(None))

    def test_none_by_default(self):
        self.assertEqual((retries.RETRIES, retries.MAX_RETRIES), (0, 9))


if __name__ == "__main__":
    unittest.main()
