"""A setup's generated code stays in a container unless local execution was accepted."""
import tempfile
import unittest
from unittest import mock

from nullius.standard import container, definitions, run
from tests.test_standard_setups import bench


class SetupChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = bench(self.tmp.name)
        self.b["score"]["runs_code"] = True
        self.other, why = definitions.setup(self.b, "original", "paper-1", "custom")
        self.assertFalse(why)

    def runner(self, **kw):
        return run.Run(self.b, self.other, model="paper-1", provider="custom",
                       jobs_root=self.tmp.name, env={"MODEL_API_URL": "http://127.0.0.1:9/v1"},
                       say=lambda _: None, **kw)

    def test_schema_accepts_only_boolean(self):
        for value in (True, False):
            self.b["score"]["runs_code"] = value
            self.assertFalse(definitions.problems(self.b, "benchmark"))
        for value in (None, 0, 1, "true", [], {}):
            self.b["score"]["runs_code"] = value
            self.assertIn("score.runs_code: true or false", definitions.problems(self.b, "benchmark"))

    @mock.patch.object(container, "unusable", return_value=None)
    def test_docker_check_image_reaches_the_local_driver_even_with_consent(self, usable):
        for consent in (False, True):
            with self.subTest(consent=consent):
                r = self.runner(unsandboxed=consent)
                with mock.patch.object(container, "present", return_value=True), \
                     mock.patch.object(container, "base_image", return_value="nullius/base:test") as build:
                    r.ready()
                build.assert_called_once()
                self.assertEqual(r.backend, "local")
                with mock.patch.object(run.jsonl, "Proc") as proc:
                    r.driver()
                    env = proc.call_args.args[2]
                    self.assertEqual(env[container.CHECK_IMAGE], "nullius/base:test")

    @mock.patch.object(container, "unusable", return_value="daemon unavailable")
    def test_no_docker_requires_consent_before_any_work(self, usable):
        with self.assertRaisesRegex(run.Refused, "--unsandboxed"):
            self.runner()
        r = self.runner(unsandboxed=True)
        self.assertFalse(r.container_check)
        r.ready()
        with mock.patch.object(run.jsonl, "Proc") as proc:
            r.driver()
            self.assertNotIn(container.CHECK_IMAGE, proc.call_args.args[2])

    @mock.patch.object(container, "unusable", return_value=None)
    def test_failed_build_never_falls_back_even_with_consent(self, usable):
        r = self.runner(unsandboxed=True)
        with mock.patch.object(container, "present", return_value=False), \
             mock.patch.object(container, "base_image", side_effect=container.Unavailable("build failed")):
            with self.assertRaisesRegex(run.Refused, "build failed"):
                r.ready()

    def test_a_check_that_runs_no_code_does_not_probe_docker(self):
        for value in (False, None):
            if value is None:
                self.b["score"].pop("runs_code")
            else:
                self.b["score"]["runs_code"] = value
            with mock.patch.object(container, "unusable") as usable:
                r = self.runner()
                r.ready()
            usable.assert_not_called()
            self.assertFalse(r.container_check)
