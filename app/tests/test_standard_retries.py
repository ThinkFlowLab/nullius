"""What an attempt is retried after: Harbor's retry rule, and this runner's own stops."""

import time
import unittest

from nullius.standard.runner import pace, retries


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
                   "UnitSpendingCapError", "UnitCallCapError", "CancelledError"):
            with self.subTest(et=et):
                self.assertFalse(retries.retryable(ended(et)))

    def test_a_failure_its_record_says_no_try_mends_is_not(self):
        # a setup this machine cannot give, a driver that did not answer in its time: named as any setup or
        # verifier error, told apart by the record
        self.assertFalse(retries.retryable({"exception_info": {"exception_type": "EnvironmentSetupError", "retryable": False}}))
        self.assertTrue(retries.retryable({"exception_info": {"exception_type": "EnvironmentSetupError"}}))

    def test_a_proof_run_tries_a_rate_limit_again_and_nothing_else(self):
        self.assertTrue(retries.rate_limited(ended("RateLimitError")))
        for et in ("ApiError", "NonZeroAgentExitCodeError", "AgentSetupTimeoutError", None):
            with self.subTest(et=et):
                self.assertFalse(retries.rate_limited(ended(et)))

    def test_a_task_the_agent_failed_is_not(self):
        self.assertFalse(retries.retryable(ended(None)))
        self.assertFalse(retries.retryable(None))

    def test_none_by_default(self):
        self.assertEqual((retries.RETRIES, retries.MAX_RETRIES), (0, 9))


class Pause(unittest.TestCase):
    def test_a_rate_limit_holds_new_units_a_minute_or_as_long_as_the_provider_said(self):
        self.assertEqual(pace.FIRST, 60.0)                       # a provider's limits are mostly per minute
        p = pace.Pace()
        t = time.time()
        self.assertEqual(p.ended("RateLimitError"), 60.0)
        self.assertEqual(p.ended("RateLimitError", said=500.0), 500.0)    # longer than the doubled 120: the provider's
        self.assertEqual(p.pause, 120.0)
        self.assertGreaterEqual(p.until, t + 500)
        self.assertIsNone(p.ended("ApiError"))
        self.assertEqual(p.pause, 0.0)

    def test_a_retry_waits_its_pause_from_its_own_end(self):
        now = time.time()
        self.assertAlmostEqual(pace.Pace().due(now - 50), 10, delta=1)            # a minute from its end, 50 s ago
        self.assertEqual(pace.Pace().due(now - 120), 0.0)                          # over already: at once
        self.assertAlmostEqual(pace.Pace().due(now - 50, said=200.0), 150, delta=1)   # the provider said longer


if __name__ == "__main__":
    unittest.main()
