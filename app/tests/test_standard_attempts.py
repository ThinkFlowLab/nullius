"""What a repeat is tried again after: Harbor's retry rule, and this runner's own stops."""

import unittest

from nullius.standard import attempts


def ended(exception_type):
    return {"exception_info": {"exception_type": exception_type}} if exception_type else {"verifier_result": {"rewards": {"reward": 0.0}}}


class Retryable(unittest.TestCase):
    def test_infrastructure_and_agent_crashes_are_tried_again(self):
        for et in ("RateLimitError", "ApiError", "NetworkConnectionError", "EnvironmentSetupError", "AgentSetupTimeoutError",
                   "NonZeroAgentExitCodeError", "OutputLengthExceededError", "VerifierError", "SomethingNewError"):
            with self.subTest(et=et):
                self.assertTrue(attempts.retryable(ended(et)))

    def test_a_result_a_wrong_setting_a_cap_or_a_stop_is_not(self):
        for et in ("AgentTimeoutError", "VerifierTimeoutError", "AgentSafetyRefusalError", "AgentAuthenticationError",
                   "ModelNotFoundError", "ApiUsageLimitError", "EnvironmentSecretMissingError", "SpendingCapError",
                   "UnitSpendingCapError", "CancelledError"):
            with self.subTest(et=et):
                self.assertFalse(attempts.retryable(ended(et)))

    def test_a_task_the_agent_failed_is_not(self):
        self.assertFalse(attempts.retryable(ended(None)))
        self.assertFalse(attempts.retryable(None))

    def test_the_default_is_one_more_try(self):
        self.assertEqual((attempts.ATTEMPTS, attempts.MAX_ATTEMPTS), (2, 10))



class RepeatToTheDriver(unittest.TestCase):
    """A driver is sent the repeat number as its definition names it:
    ``limits.repeats`` gets ``repeat``, the older ``limits.attempts`` still
    gets ``attempt``, and a definition that declares neither gets nothing."""

    def test_the_repeat_is_sent_as_its_definition_names_it(self):
        import threading
        import types
        from nullius.standard import run
        for limits, sent in (({"repeats": "a seed per trial"}, {"repeat": 2}), ({"attempts": "a seed per trial"}, {"attempt": 2}), ({}, {})):
            with self.subTest(limits=limits):
                me = types.SimpleNamespace(local=threading.local(), bench={"limits": limits})
                me.local.repeat = 2
                self.assertEqual(run.Run.repeat_kw(me), sent)

    def test_a_definition_names_it_one_way(self):
        from nullius.standard import definitions
        bench = definitions.get("benchmark", "tau3-bench")
        limits = {k: v for k, v in bench["limits"].items() if k != "repeats"}
        said = bench["limits"]["repeats"]
        both = "limits: repeats or its older name attempts, not both"
        self.assertNotIn(both, definitions.problems(bench, "benchmark"))
        self.assertNotIn(both, definitions.problems(dict(bench, limits=dict(limits, attempts=said)), "benchmark"))
        self.assertIn(both, definitions.problems(dict(bench, limits=dict(limits, repeats=said, attempts=said)), "benchmark"))
        self.assertTrue(any(p.startswith("limits.repeats:") for p in definitions.problems(dict(bench, limits=dict(limits, repeats="")), "benchmark")))

if __name__ == "__main__":
    unittest.main()
