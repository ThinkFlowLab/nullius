"""Conformance writes down what passed, takes back what failed, and is the
only way a port gets into ``offers``."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from nullius.standard import conform, definitions, install, match

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

    def test_a_kit_that_passes_is_recorded_with_its_job(self):
        got, d = self.check("capability", "fakemem", ["cap.memory/v1"])
        self.assertTrue(got["cap.memory/v1"]["ok"])
        rec = d["proven"]["cap.memory/v1"]
        self.assertEqual(rec["passed"], "2/2")
        self.assertEqual(rec["kit_version"], definitions.kit_version(definitions.kit("cap.memory/v1")))   # the kit it passed
        self.assertTrue(definitions.on_current_kit("cap.memory/v1", rec))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "jobs", rec["job"])))
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
        self.assertEqual(conform.candidates(a), ["agent.task/v1", "agent.workspace/v1", "agent.tools/v1", "agent.conversation/v1", "agent.search/v1"])
        cmd = dict(a, start={"command": ["x", "{prompt}"]})
        self.assertEqual(conform.candidates(cmd), ["agent.task/v1", "agent.workspace/v1", "agent.search/v1"])
        self.assertEqual(conform.candidates(definitions.get("capability", "fakemem", self.root)), ["cap.memory/v1"])


if __name__ == "__main__":
    unittest.main()
