"""What a recipe needs of the machine it runs on, asked before Run: docker
answering (not only its command), uv, the harbor on PATH, a benchmark's
machine setup - here by ``command -v``, on a host by one ssh probe."""
from __future__ import annotations

import unittest
from unittest import mock

from nullius import hosts, prerequisites
from nullius.standard import container, definitions, recipe, run

SETUP = {"sandbox": {"summary": "a sandbox", "check": ["srt", "bwrap"], "install": ["sudo apt-get install -y bubblewrap"]}}


def by_name(rows):
    return {r["name"]: r for r in rows}


class Here(unittest.TestCase):
    def test_docker_counts_only_when_its_engine_answers(self):
        stopped = "docker is installed but not usable by this user: Cannot connect to the Docker daemon"
        got = by_name(prerequisites.check_here(["docker"], which=lambda p: "/usr/bin/" + p, docker=lambda: stopped))
        self.assertEqual((got["docker"]["state"], got["docker"]["detail"]), ("missing", stopped))
        got = by_name(prerequisites.check_here(["docker"], docker=lambda: None))
        self.assertEqual(got["docker"]["state"], "ok")

    def test_the_standard_s_docker_row_says_its_containers_run_outside_a_userns_remap(self):
        for standard, remap, said in ((True, True, prerequisites.REMAPPED), (True, False, "the engine answers"),
                                      (False, True, "the engine answers")):     # Harbor's containers keep the remap
            got = by_name(prerequisites.check_here(["docker"], standard=standard, docker=lambda: None, remap=lambda: remap))
            self.assertEqual((got["docker"]["state"], got["docker"]["detail"]), ("ok", said))

    def test_uv_harbor_and_a_setup_s_programs_are_looked_for_on_path(self):
        have = {"uvx", "srt"}
        rows = by_name(prerequisites.check_here(["uv", "harbor"], SETUP, which=lambda p: p in have, docker=lambda: None))
        self.assertEqual(rows["uv"]["state"], "ok")
        self.assertEqual(rows["harbor"]["state"], "missing")
        self.assertEqual((rows["setup:sandbox"]["state"], rows["setup:sandbox"]["detail"]), ("missing", "bwrap not on PATH"))
        self.assertEqual(rows["setup:sandbox"]["install"], ["sudo apt-get install -y bubblewrap"])
        self.assertFalse(prerequisites.ready([{"rows": list(rows.values())}]))

    def test_the_docker_answer_is_kept_a_minute_unless_asked_fresh(self):
        calls = []
        with mock.patch.object(container, "unusable", lambda: calls.append(1)), \
             mock.patch.object(prerequisites.time, "monotonic", return_value=100.0) as now, \
             mock.patch.dict(prerequisites._HERE, at=0.0, why=None):
            prerequisites.docker_here(); prerequisites.docker_here()
            self.assertEqual(len(calls), 1)
            now.return_value = 159.0
            prerequisites.docker_here()
            self.assertEqual(len(calls), 1)
            now.return_value = 161.0
            prerequisites.docker_here()
            self.assertEqual(len(calls), 2)
            prerequisites.docker_here(fresh=True)
            self.assertEqual(len(calls), 3)


class OnAHost(unittest.TestCase):
    HOST = hosts.parse("me@example.invalid")

    def probe(self, **answer):
        base = {"ok": True, "uv": True, "docker": True, "docker_ok": True, "harbor": False, "harbor_version": None,
                "hostname": "box", "error": None, "missing": []}
        return lambda host, programs=None: dict(base, **answer)

    def test_a_docker_its_user_cannot_use_is_missing_and_uv_is_installed_by_the_run(self):
        rows = by_name(prerequisites.check_host(self.HOST, ["docker", "uv"], test=self.probe(docker_ok=False, uv=False)))
        self.assertEqual(rows["docker"]["state"], "missing")
        self.assertIn("docker group", rows["docker"]["detail"])
        self.assertEqual(rows["uv"]["state"], "install")
        self.assertFalse(prerequisites.ready([{"rows": list(rows.values())}]))

    def test_an_unreachable_host_is_one_row(self):
        rows = prerequisites.check_host(self.HOST, ["docker"], test=self.probe(ok=False, error="ssh: no route"))
        self.assertEqual([(r["name"], r["state"], r["detail"]) for r in rows], [("connect", "missing", "ssh: no route")])

    def test_the_standard_s_programs_and_a_setup_are_asked_in_the_one_probe(self):
        asked = {}

        def test(host, programs=None):
            asked["programs"] = programs
            return self.probe(missing=["git", "bwrap"])(host)
        rows = by_name(prerequisites.check_host(self.HOST, ["python3", "git"], SETUP, test=test))
        self.assertEqual(sorted(asked["programs"]), ["bwrap", "git", "python3", "srt"])
        self.assertEqual((rows["python3"]["state"], rows["git"]["state"], rows["setup:sandbox"]["state"]), ("ok", "missing", "missing"))
        self.assertEqual(prerequisites.check_host(self.HOST, ["docker"], test=self.probe())[1]["state"], "ok")

    def test_a_host_s_docker_with_userns_remap_is_said_for_the_standard_alone(self):
        remapped = self.probe(docker_userns=True)
        rows = by_name(prerequisites.check_host(self.HOST, ["docker"], standard=True, test=remapped))
        self.assertEqual((rows["docker"]["state"], rows["docker"]["detail"]), ("ok", prerequisites.REMAPPED))
        self.assertEqual(by_name(prerequisites.check_host(self.HOST, ["docker"], test=remapped))["docker"]["detail"], "the engine answers")
        self.assertEqual(by_name(prerequisites.check_host(self.HOST, ["docker"], standard=True, test=self.probe()))["docker"]["detail"],
                         "the engine answers")


class OwnImage(unittest.TestCase):
    """A benchmark whose units each run in their own image needs docker for
    its controls whatever the backend: the recipe says so, and the run
    refuses up front rather than failing each unit."""

    TB = definitions.get("benchmark", "terminal-bench")

    def a_recipe(self, others, backend="local"):
        return {"version": 1, "harness": "standard", "name": "tb-x", "title": "t", "benchmark": "terminal-bench", "datasets": ["small"],
                "others": others, "provider": "openai", "model": "", "limit": 1, "concurrency": 1, "where": "local", "backend": backend}

    def test_the_recipe_needs_docker_for_a_control_in_an_own_image(self):
        self.assertTrue(recipe.compile(self.a_recipe(["nop"]))["docker"])
        self.assertTrue(recipe.compile(self.a_recipe(["nop"], "docker"))["docker"])

    def test_the_run_refuses_a_control_when_docker_does_not_answer(self):
        nop = definitions.control(self.TB, "nop")
        with mock.patch.object(container, "unusable", lambda: "docker is not installed on this machine"):
            with self.assertRaises(run.Refused) as e:
                run.Run(self.TB, nop, model="", provider="openai", dataset="small", env={}, jobs_root="/nonexistent-never-written")
        self.assertIn("runs in its own image, which needs docker: docker is not installed", str(e.exception))


if __name__ == "__main__":
    unittest.main()
