"""Conformance writes down what passed, takes back what failed, and is the
only way a port gets into ``offers``."""
from __future__ import annotations

import os
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from nullius.standard import cli, conform, container, definitions, install, match, retries, run

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard")


class ConformTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "catalogue")
        shutil.copytree(os.path.join(FIX, "catalogue"), self.root)
        shutil.copy(os.path.join(FIX, "fake_acp_agent.py"), self.tmp)       # where the fake agent's start path points
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, kind, key, which):
        d = definitions.get(kind, key, self.root)
        got = conform.check(d, model="m", provider="custom", which=which, jobs_root=os.path.join(self.tmp, "jobs"),
                            say=lambda s: None, env={"MODEL_API_URL": "http://127.0.0.1:9/v1"}, unsandboxed=True)
        return got, definitions.get(kind, key, self.root)

    def test_a_kit_that_passes_names_its_job_and_its_record_does_not(self):
        with mock.patch.object(conform.run, "run", wraps=conform.run.run) as ran:
            got, d = self.check("capability", "fakemem", ["cap.memory/v1"])
        # a rate-limited unit runs once more, after its pause, and nothing else: a proof is never the endpoint's busy minute
        self.assertEqual((ran.call_args.kwargs["retries"], ran.call_args.kwargs["retry_if"]), (1, retries.rate_limited))
        self.assertTrue(got["cap.memory/v1"]["ok"])
        rec = d["proven"]["cap.memory/v1"]
        self.assertEqual(rec["passed"], "2/2")
        self.assertEqual(rec["kit_version"], definitions.kit_version(definitions.kit("cap.memory/v1")))   # the kit it passed
        self.assertTrue(definitions.on_current_kit("cap.memory/v1", rec))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "jobs", got["cap.memory/v1"]["job"])))
        self.assertNotIn("job", rec)               # a folder on the machine that ran it
        self.assertEqual(d["offers"], ["cap.memory/v1"])

    def test_a_memory_that_refuses_credentials_still_proves_the_memory_port(self):
        here = os.path.join(self.root, "capabilities", "fakemem")
        with open(os.path.join(here, "capability.py")) as f:
            src = f.read()
        refuse = 'kept.append(req["session"]["text"]); out = {}'
        self.assertIn(refuse, src)
        with open(os.path.join(here, "capability.py"), "w") as f:      # it drops every session that looks like a secret
            f.write(src.replace(refuse, 'out = {}\n        if not any(w in req["session"]["text"].lower() for w in '
                                        '("password", "passcode", "token", "secret", "credential", "api key")):\n'
                                        '            kept.append(req["session"]["text"])'))
        got, d = self.check("capability", "fakemem", ["cap.memory/v1"])
        self.assertTrue(got["cap.memory/v1"]["ok"])
        self.assertEqual(d["proven"]["cap.memory/v1"]["passed"], "2/2")

    def test_a_kit_that_fails_takes_the_claim_back(self):
        got, d = self.check("agent", "fake", ["agent.task/v1"])          # it shouts the prompt back: not the word
        self.assertFalse(got["agent.task/v1"]["ok"])
        self.assertNotIn("agent.task/v1", d["offers"])
        self.assertNotIn("agent.task/v1", d["proven"])
        self.assertEqual(definitions.problems(d, "agent"), [])          # still a valid definition, offering less
        kit = definitions.kit("agent.task/v1")
        self.assertTrue(match.fit(kit, d))                              # and no longer a fit for what needs it

    def test_candidates_follow_the_family_and_the_form(self):
        a = definitions.get("agent", "fake", self.root)
        self.assertEqual(conform.candidates(a), ["agent.task/v1", "agent.workspace/v1", "agent.tools/v1", "agent.conversation/v1", "agent.search/v1",
                                                 "agent.computer/v1"])
        cmd = dict(a, start={"command": ["x", "{prompt}"]})
        self.assertEqual(conform.candidates(cmd), ["agent.task/v1", "agent.workspace/v1", "agent.search/v1"])
        self.assertEqual(conform.candidates(definitions.get("capability", "fakemem", self.root)), ["cap.memory/v1"])

    def invoke(self, other, *args):
        out = io.StringIO()
        with mock.patch.object(cli, "_other", return_value=other), mock.patch.object(cli.settings, "load"), \
                mock.patch.object(cli, "_exit_on_term"), mock.patch.object(cli.progress, "ECHO", None), \
                mock.patch.dict(os.environ, {"MODEL_API_URL": "http://127.0.0.1:9/v1"}), redirect_stdout(out):
            status = cli.main(["conform", "fake", "--model", "m", "--provider", "custom", "--unsandboxed",
                               "--jobs", os.path.join(self.tmp, "jobs"), *args])
        return status, out.getvalue()

    def test_all_ports_continue_after_a_kit_refusal_and_save_the_proofs(self):
        other = definitions.get("agent", "fake", self.root)
        old = other["proven"]["agent.workspace/v1"].copy()
        ports = ["agent.task/v1", "agent.workspace/v1", "agent.conversation/v1"]
        started = []
        real_run = run.run

        def attempt(kit, participant, **kwargs):
            started.append(kit["needs"][0])
            if kit["needs"] == ["agent.workspace/v1"]:
                kit = dict(kit, environments={"task": {"image": "own", "summary": "A task image."}})
                return real_run(kit, participant, **kwargs)
            return os.path.join(self.tmp, kit["key"])

        passed = SimpleNamespace(trials=[SimpleNamespace(task_name="kit/check", reward=1.0)])
        with mock.patch.object(conform, "candidates", return_value=ports), \
                mock.patch.object(run, "run", side_effect=attempt), \
                mock.patch.object(container, "unenforced", side_effect=container.Unavailable("no container engine")), \
                mock.patch.object(conform.parse, "parse_job", return_value=passed):
            status, said = self.invoke(other)
        self.assertEqual(status, 1, said)                           # a kit was refused: not every port is proven
        self.assertEqual(started, ports)
        self.assertIn("agent.workspace/v1: refused: no container engine", said)
        saved = definitions.get("agent", "fake", self.root)
        for port in (ports[0], ports[2]):
            self.assertEqual(saved["proven"][port]["passed"], "1/1")
            self.assertNotIn("job", saved["proven"][port])
            self.assertIn(port, saved["offers"])
        self.assertEqual(saved["proven"][ports[1]], old)
        self.assertIn(ports[1], saved["offers"])

    def test_a_refused_kit_is_returned_as_refused_beside_the_proofs(self):
        other = definitions.get("agent", "fake", self.root)
        passed = SimpleNamespace(trials=[SimpleNamespace(task_name="kit/check", reward=1.0)])
        with mock.patch.object(conform, "candidates", return_value=["agent.task/v1", "agent.workspace/v1"]), \
                mock.patch.object(run, "run", side_effect=["passed-job", run.BenchmarkRefused("no engine")]), \
                mock.patch.object(conform.parse, "parse_job", return_value=passed):
            got = conform.check(other, model="m", provider="custom", jobs_root=os.path.join(self.tmp, "jobs"),
                                say=lambda s: None, env={"MODEL_API_URL": "http://127.0.0.1:9/v1"}, unsandboxed=True)
        self.assertTrue(got["agent.task/v1"]["ok"])
        self.assertEqual(got["agent.workspace/v1"], {"ok": False, "refused": "no engine"})

    def test_a_refused_ports_record_stays_also_without_its_offer(self):
        path = os.path.join(self.root, "agents", "fake", "agent.json")
        with open(path) as f:
            raw = json.load(f)
        raw["offers"].remove("agent.workspace/v1")              # a record kept without its offer
        kept = dict(raw["proven"]["agent.workspace/v1"])
        with open(path, "w") as f:
            json.dump(raw, f)
        other = definitions.get("agent", "fake", self.root)
        self.assertEqual(definitions.problems(other, "agent"), [])
        passed = SimpleNamespace(trials=[SimpleNamespace(task_name="kit/check", reward=1.0)])
        with mock.patch.object(conform, "candidates", return_value=["agent.task/v1", "agent.workspace/v1"]), \
                mock.patch.object(run, "run", side_effect=["passed-job", run.BenchmarkRefused("no engine")]), \
                mock.patch.object(conform.parse, "parse_job", return_value=passed):
            conform.check(other, model="m", provider="custom", jobs_root=os.path.join(self.tmp, "jobs"),
                          say=lambda s: None, env={"MODEL_API_URL": "http://127.0.0.1:9/v1"}, unsandboxed=True)
        saved = definitions.get("agent", "fake", self.root)
        self.assertEqual(saved["proven"].get("agent.workspace/v1"), kept)
        self.assertEqual(saved["proven"]["agent.task/v1"]["passed"], "1/1")

    def test_every_kit_refused_saves_nothing_and_exits_two(self):
        other = definitions.get("agent", "fake", self.root)
        ports = ["agent.task/v1", "agent.workspace/v1"]
        with mock.patch.object(conform, "candidates", return_value=ports), \
                mock.patch.object(run, "run", side_effect=run.BenchmarkRefused("no engine")) as attempt, \
                mock.patch.object(definitions, "save") as save:
            status, said = self.invoke(other)
        self.assertEqual(status, 2, said)
        self.assertEqual(attempt.call_count, 2)                     # each kit was tried, each refusal said
        for port in ports:
            self.assertIn(f"{port}: refused: no engine", said)
        save.assert_not_called()

    def test_ports_without_a_kit_need_no_key(self):
        other = definitions.get("agent", "fake", self.root)
        lines = []
        with mock.patch.object(definitions, "kit", return_value=None), mock.patch.object(run, "run") as attempt:
            got = conform.check(other, model="m", provider="openai", which=["agent.workspace/v1"], env={},
                                jobs_root=os.path.join(self.tmp, "jobs"), say=lines.append, unsandboxed=True)
        self.assertEqual(got, {})
        self.assertEqual(lines, ["agent.workspace/v1: no kit yet"])
        attempt.assert_not_called()

    def test_an_explicit_port_refusal_keeps_exit_status_two(self):
        other = definitions.get("agent", "fake", self.root)
        with mock.patch.object(run, "run", side_effect=run.BenchmarkRefused("kit unavailable")) as attempt, \
                mock.patch.object(definitions, "save") as save:
            status, said = self.invoke(other, "--port", "agent.workspace/v1")
        self.assertEqual(status, 2)
        self.assertIn("refused: kit unavailable", said)
        self.assertEqual(attempt.call_count, 1)
        save.assert_not_called()

    def test_a_command_refusal_still_ends_the_check_without_saving(self):
        other = definitions.get("agent", "fake", self.root)
        passed = SimpleNamespace(trials=[SimpleNamespace(task_name="kit/check", reward=1.0)])
        for reason in ("agent unavailable", "model unavailable", "key rejected"):
            with self.subTest(reason=reason), \
                    mock.patch.object(run, "run", side_effect=["passed-job", run.Refused(reason)]) as attempt, \
                    mock.patch.object(conform.parse, "parse_job", return_value=passed), \
                    mock.patch.object(definitions, "save") as save:
                status, said = self.invoke(other)
                self.assertEqual(status, 2)
                self.assertIn("refused: " + reason, said)
                self.assertEqual(attempt.call_count, 2)
                save.assert_not_called()

    def test_missing_credentials_are_refused_before_any_kit(self):
        other = definitions.get("agent", "fake", self.root)
        with mock.patch.object(run, "run") as attempt, mock.patch.object(definitions, "save") as save:
            with self.assertRaisesRegex(run.Refused, "OPENAI_API_KEY"):
                conform.check(other, model="m", provider="openai", env={}, unsandboxed=True)
        attempt.assert_not_called()
        save.assert_not_called()

    def test_an_agent_install_refusal_is_not_a_kit_refusal(self):
        other = definitions.get("agent", "fake", self.root)
        with mock.patch.object(install, "packages", side_effect=install.InstallFailed("agent unavailable")), \
                mock.patch.object(definitions, "save") as save:
            status, said = self.invoke(other)
        self.assertEqual(status, 2)
        self.assertIn("refused: could not install what the run needs: agent unavailable", said)
        save.assert_not_called()

    def test_invalid_agent_options_still_end_the_command(self):
        other = definitions.get("agent", "fake", self.root)
        with mock.patch.object(definitions, "save") as save:
            status, said = self.invoke(other, "--option", "missing=value")
        self.assertEqual(status, 2)
        self.assertIn("refused:", said)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
