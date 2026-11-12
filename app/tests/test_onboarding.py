"""Getting started: what the checklist says is done comes from what this
machine can check, and a sub-task nothing on disk records is left to the
browser's visits."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from unittest import mock

from nullius import onboarding
from nullius.standard import container

NO_DOCKER = lambda: False


def url_for(endpoint):
    return "/" + endpoint


class Facts(unittest.TestCase):
    def test_nothing_set_is_nothing_done(self):
        f = onboarding.facts({}, [], False, docker=NO_DOCKER)
        self.assertEqual(f, dict.fromkeys(["provider", "where", "judge", "control_run", "agent_run", "judged"], False))

    def test_each_fact_is_read_from_its_own_source(self):
        env = {"OPENROUTER_API_KEY": "sk-or-1", "HOSTS": "box", "TYPESAFE_API_KEY": "ts-1"}
        f = onboarding.facts(env, [{"agent": "oracle"}, {"agent": "Claude-Code"}], True, docker=NO_DOCKER)
        self.assertTrue(all(f.values()), f)
        self.assertTrue(onboarding.facts({}, [], docker=lambda: True)["where"])                 # docker here is a place to run
        self.assertTrue(onboarding.facts({"MODEL_API_URL": "http://box:8000/v1"}, [], docker=NO_DOCKER)["provider"])
        only_controls = onboarding.facts({}, [{"agent": "nop"}, {"agent": "oracle"}], docker=NO_DOCKER)
        self.assertEqual((only_controls["control_run"], only_controls["agent_run"]), (True, False))

    def test_docker_counts_only_when_it_answers(self):
        """Docker Desktop's WSL integration leaves /usr/bin/docker in place
        while its engine is quit: the command alone is not a place to run."""
        with tempfile.TemporaryDirectory() as d:
            docker = os.path.join(d, "docker")
            with mock.patch.dict(os.environ, {"PATH": d}), mock.patch.object(container, "DOCKER", "docker"):
                self.assertFalse(onboarding.docker_answers())                                   # no command at all
                for code, answers in ((1, False), (0, True)):
                    with open(docker, "w") as f:
                        f.write(f"#!/bin/sh\nexit {code}\n")
                    os.chmod(docker, 0o755)
                    self.assertEqual(onboarding.docker_answers(), answers, code)

    def test_a_judgement_in_either_lane_counts(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(onboarding.any_judged(d))
            os.makedirs(os.path.join(d, "judgements-llm", "job"))
            self.assertFalse(onboarding.any_judged(d))                                         # a folder is not a judgement
            open(os.path.join(d, "judgements-llm", "job", "t.json"), "w").close()
            self.assertTrue(onboarding.any_judged(d))
        self.assertFalse(onboarding.any_judged(None))


class Checklist(unittest.TestCase):
    def test_a_fact_settles_a_sub_task_and_a_visit_is_left_open(self):
        sections = onboarding.checklist({"provider": True}, url_for)
        subs = {x["id"]: x for s in sections for x in s["subs"]}
        self.assertIs(subs["provider"]["done"], True)
        self.assertIs(subs["where"]["done"], False)
        self.assertIsNone(subs["job"]["done"])                       # nothing on disk records it: the browser's visit does
        self.assertEqual(subs["agent"]["url"], "/new_run")           # a sub-task can be done on another page than its section's
        for x in subs.values():
            self.assertTrue(x["target"] and all(len(t) == 2 for t in x["target"]), x["id"])
            self.assertTrue(("done" in x) and (x["done"] is not None or x["visit"]), x["id"])

    def test_the_visit_patterns_match_their_pages_only(self):
        visit = {x["id"]: re.compile(x["visit"]) for s in onboarding.SECTIONS for x in s["subs"] if "visit" in x}
        self.assertTrue(visit["open"].search("/benchmarks/swe-bench-verified"))
        self.assertFalse(visit["open"].search("/benchmarks/new"))
        self.assertTrue(visit["browse"].search("/benchmarks"))
        self.assertTrue(visit["trial"].search("/trial/job-1/task__abc"))
        self.assertFalse(visit["trial"].search("/trial/job-1/task__abc/conversation"))
        self.assertTrue(visit["compared"].search("/compare?pick=ws:a&pick=ws:b"))
        self.assertFalse(visit["compared"].search("/compare"))


if __name__ == "__main__":
    unittest.main()
