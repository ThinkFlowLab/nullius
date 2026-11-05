"""Outcome classification against Harbor's exception taxonomy.

Provider and setup failures must not count as agent failures. Unrecognized
exceptions remain UNKNOWN, with their recorded type available to the page.
"""

import json
import os
import tempfile
import unittest

from nullius.runs import classify as C
from nullius.runs import parse, verifier


def _outcome(exception_type, report):
    """Classify a raw SWE-bench ``verifier/report.json`` block, as parse does."""
    return C.from_verdict(exception_type, verifier.read("swe-bench", report, None))


# Harbor exception examples. test_prefix_rule_covers_new_api_classes also
# checks the prefix rule independently of this enumeration.
HARBOR_INFRA = [
    "ApiConnectionClosedError",
    "ApiConnectionError",
    "ApiError",
    "ApiInternalServerError",
    "ApiKeyRejectedError",
    "ApiOverloadedError",
    "ApiProviderResourceNotFoundError",
    "ApiResponseStalledError",
    "ApiUsageLimitError",
    "AgentAuthenticationError",   # NonZeroAgentExitCodeError, recognised as a missing login
    "ModelNotFoundError",         # same, the model name is wrong
    "NetworkConnectionError",     # same, DNS / connection refused / TLS
    "UnknownApiError",            # an ApiError subclass that lacks the prefix
]

HARBOR_RATE_LIMIT = ["ApiRateLimitError"]

HARBOR_SETUP = [
    "AgentSetupTimeoutError",
    "EnvironmentBuildError",          # 0.20's name, still matched by the rule
    "EnvironmentStartTimeoutError",
    "SandboxBuildFailedError",
]

# Agent behaviour under conditions it was given. Deliberately counted against
# the agent; see the note in classify.py before moving any of them.
HARBOR_AGENT = [
    "AgentTimeoutError",
    "AgentSafetyRefusalError",
    "ContextWindowExceededError",
    "ContextLengthExceededError",
    "OutputLengthExceededError",
    "OutputTokenExceededError",
    "NonZeroAgentExitCodeError",
]

# Harbor classes a trial can record that belong to neither side: the run was
# cancelled, the verifier never answered, the sandbox died. Each stays in the
# denominator and is charged to nobody — the documented rule for `unknown`.
HARBOR_NEITHER = [
    "CancelledError",                 # asyncio's, recorded when a job is stopped
    "VerifierTimeoutError",
    "VerifierOutputParseError",
    "RewardFileNotFoundError",
    "RewardFileEmptyError",
    "HealthcheckError",
    "MemoryLimitExceededError",
    "SandboxLikelyOutOfMemoryError",
]


class LiveBoardAgrees(unittest.TestCase):
    """The live boards name a trial's error before its result is read: a wrong
    model name (ModelNotFoundError) was the agent's there and an API error on
    the results, and on Harbor's board so was every Api* failure."""

    def test_both_boards_charge_the_infrastructure_as_the_classifier_does(self):
        from types import SimpleNamespace
        from nullius.harbor import feed
        from nullius.standard import job
        harbor = lambda kind: feed.outcome_of(SimpleNamespace(verifier_result=None, exception_info=SimpleNamespace(exception_type=kind)))[0]
        standard = lambda kind: job.feed_outcome("pass", None, (kind, kind))["outcome"]
        for name in HARBOR_INFRA + HARBOR_RATE_LIMIT + HARBOR_SETUP:
            for board, outcome in (("harbor", harbor), ("standard", standard)):
                with self.subTest(name, board=board):
                    self.assertIn(outcome(name), ("infra_error", "timeout") if "Timeout" in name else ("infra_error",))
        for name in HARBOR_AGENT:
            for board, outcome in (("harbor", harbor), ("standard", standard)):
                with self.subTest(name, board=board):
                    self.assertIn(outcome(name), ("agent_error", "timeout"))


class HarborTaxonomy(unittest.TestCase):
    def test_provider_failures_are_infra(self):
        for name in HARBOR_INFRA:
            with self.subTest(name):
                got = _outcome(name, None)
                self.assertEqual(got, C.API_ERROR)
                self.assertIn(got, C.INFRA_OUTCOMES)

    def test_rate_limit_keeps_its_own_bucket(self):
        """Common enough to be worth seeing separately from generic API failure."""
        for name in HARBOR_RATE_LIMIT:
            with self.subTest(name):
                self.assertEqual(_outcome(name, None), C.RATE_LIMITED)

    def test_setup_failures_are_infra(self):
        for name in HARBOR_SETUP:
            with self.subTest(name):
                self.assertEqual(_outcome(name, None), C.SETUP_ERROR)

    def test_agent_behaviour_counts_against_the_agent(self):
        for name in HARBOR_AGENT:
            with self.subTest(name):
                got = _outcome(name, None)
                self.assertEqual(got, C.AGENT_ERROR)
                self.assertNotIn(got, C.INFRA_OUTCOMES)

    def test_prefix_rule_covers_new_api_classes(self):
        """Harbor adding an Api* class must not silently become an agent failure."""
        self.assertEqual(_outcome("ApiSomethingNobodyHasWrittenYetError", None),
                         C.API_ERROR)

    def test_dotted_paths_are_tolerated(self):
        self.assertEqual(_outcome("harbor.exceptions.ApiOverloadedError", None),
                         C.API_ERROR)

    def test_harbor_classes_on_neither_side_are_unknown(self):
        for name in HARBOR_NEITHER:
            with self.subTest(name):
                self.assertEqual(_outcome(name, None), C.UNKNOWN)

    def test_an_unknown_exception_still_outranks_the_verifier(self):
        """A trial that crashed did not earn what the verifier said about it,
        whoever the crash belongs to."""
        self.assertEqual(_outcome("SomethingNobodyHasWrittenYetError",
                                    {"resolved": True}), C.UNKNOWN)

    def test_the_agent_list_is_explicit_and_lower_case(self):
        """The list is matched by exact lower-cased name; an entry with a
        capital or a dot would never match anything."""
        for name in C._AGENT_EXCEPTIONS | C._INFRA_EXCEPTIONS:
            with self.subTest(name):
                self.assertEqual(name, name.lower())
                self.assertNotIn(".", name)
        self.assertFalse(C._AGENT_EXCEPTIONS & C._INFRA_EXCEPTIONS,
                         "a name on both lists would be infra by order, silently")


class UnknownExceptionInTheRates(unittest.TestCase):
    """The rules the classification feeds: an unknown-exception trial is
    counted (raw and adjusted denominators) and is not infra."""

    def _job(self, tmp):
        job = os.path.join(tmp, "job")
        for name, result in (
            ("t-ok", {"verifier_result": {"rewards": {"reward": 1}}}),
            ("t-crash", {"exception_info": {
                "exception_type": "SomethingNobodyHasWrittenYetError",
                "exception_message": "the reader fell over",
                "exception_traceback": "Traceback (most recent call last): ...",
                "occurred_at": "2026-09-17T12:00:00"}}),
            ("t-429", {"exception_info": {
                "exception_type": "ApiRateLimitError",
                "exception_message": "429", "exception_traceback": "",
                "occurred_at": "2026-09-17T12:00:00"}}),
        ):
            d = os.path.join(job, name)
            os.makedirs(d)
            with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
                json.dump({"task_name": f"terminal-bench/{name}", "trial_name": name,
                           "source": "terminal-bench", **result}, fh)
            if "verifier_result" in result:      # Terminal-Bench's verifier writes its reward here
                os.makedirs(os.path.join(d, "verifier"))
                with open(os.path.join(d, "verifier", "reward.txt"), "w", encoding="utf-8") as fh:
                    fh.write("1")
        with open(os.path.join(job, "result.json"), "w", encoding="utf-8") as fh:
            json.dump({"stats": {"n_trials": 3}}, fh)
        return parse.parse_job(job)

    def test_counted_in_both_denominators_and_not_infra(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(tmp)
        by_name = {t.trial_name: t for t in job.trials}
        crash = by_name["t-crash"]
        self.assertEqual(crash.outcome, C.UNKNOWN)
        self.assertEqual(crash.outcome_label, "Unknown")
        self.assertEqual(crash.exception_type, "SomethingNobodyHasWrittenYetError")
        self.assertFalse(crash.is_infra)
        self.assertTrue(by_name["t-429"].is_infra)
        self.assertEqual(job.n_infra, 1)
        self.assertAlmostEqual(job.raw_solve_rate, 1 / 3)        # three recorded
        self.assertAlmostEqual(job.adjusted_solve_rate, 1 / 2)   # the 429 leaves; the crash stays


class VerifierOutcomes(unittest.TestCase):
    """No exception: the outcome comes from the verifier report."""

    def test_resolved(self):
        self.assertEqual(_outcome(None, {"resolved": True}), C.RESOLVED)

    def test_no_patch(self):
        self.assertEqual(_outcome(None, {"resolved": False, "patch_exists": False}),
                         C.NO_PATCH)

    def test_patch_not_applied(self):
        self.assertEqual(
            _outcome(None, {"resolved": False, "patch_exists": True,
                              "patch_successfully_applied": False}),
            C.PATCH_NOT_APPLIED)

    def test_patch_failed_tests(self):
        self.assertEqual(
            _outcome(None, {"resolved": False, "patch_exists": True,
                              "patch_successfully_applied": True}),
            C.PATCH_FAILED_TESTS)

    def test_unknown_when_nothing_is_recorded(self):
        self.assertEqual(_outcome(None, None), C.UNKNOWN)


class Presentation(unittest.TestCase):
    """Every outcome must be displayable, or the UI renders a blank cell."""

    def test_every_outcome_has_a_label_and_a_description(self):
        for outcome in C.ORDER:
            with self.subTest(outcome):
                self.assertIn(outcome, C.LABELS)
                self.assertIn(outcome, C.DESCRIPTIONS)

    def test_every_outcome_is_ordered(self):
        """ORDER drives sorting; an outcome missing from it raises on compare."""
        produced = {C.RESOLVED, C.SCORED, C.PATCH_FAILED_TESTS, C.FAILED_VERIFICATION,
                    C.PATCH_NOT_APPLIED, C.NO_PATCH, C.AGENT_ERROR,
                    C.RATE_LIMITED, C.API_ERROR, C.SETUP_ERROR, C.UNKNOWN}
        self.assertEqual(produced, set(C.ORDER))


if __name__ == "__main__":
    unittest.main()
