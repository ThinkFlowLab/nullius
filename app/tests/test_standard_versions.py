"""An agent or a capability may hold several versions in its one file: each is
named ``key@version``, resolved flat, matched on its own proofs, and proven
into its own block."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nullius.standard import cli, conform, definitions, match

BENCH = {
    "schema": "nullius.benchmark/v1", "key": "toy", "label": "Toy", "summary": "A toy.",
    "family": "agent", "needs": ["agent.workspace/v1"],
    "driver": {"command": ["python3", "{here}/driver.py"]},
    "datasets": [{"id": "all", "units": 3}],
    "units": {"noun": "task", "nouns": "tasks"},
    "score": {"kind": "pass", "says": "the file is right"},
}
PROOF = {"passed": "2/2", "on": "2026-10-10", "model": "m", "provider": "openai", "job": "j"}
VERSIONED = {
    "schema": "nullius.agent/v1", "key": "bot", "label": "Bot", "summary": "A bot.",
    "upstream": {"license": "MIT"},
    "start": {"acp": ["{install}/node_modules/.bin/bot-acp"]},
    "model": {"apis": ["chat"], "env": {"OPENAI_BASE_URL": "{base_url}", "OPENAI_API_KEY": "{key}"}},
    "default": "1.0.0",
    "versions": {
        "2.0.0": {"upstream": {"package": "@new/bot-acp"}, "install": {"npm": ["@new/bot-acp@2.0.0"]}, "proven": {}},
        "1.0.0": {"upstream": {"package": "@old/bot-acp"}, "install": {"npm": ["@old/bot-acp@1.0.0"]},
                  "proven": {"agent.workspace/v1": dict(PROOF, version="1.0.0")}},
    },
}
FLAT = {
    "schema": "nullius.agent/v1", "key": "flat", "label": "Flat", "summary": "One version.",
    "upstream": {"package": "flat-acp", "version": "0.3.0", "license": "MIT"},
    "install": {"npm": ["flat-acp@0.3.0"]},
    "start": {"acp": ["{install}/node_modules/.bin/flat-acp"]},
    "model": {"apis": ["chat"], "env": {"OPENAI_BASE_URL": "{base_url}", "OPENAI_API_KEY": "{key}"}},
    "offers": ["agent.workspace/v1"], "proven": {"agent.workspace/v1": dict(PROOF, version="0.3.0")},
}


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory(prefix="nullius-versions-"))
        self.user = self.enterContext(tempfile.TemporaryDirectory(prefix="nullius-versions-user-"))
        self.enterContext(patch.dict(os.environ, NULLIUS_HOME=os.path.join(self.user, "profile"),
                                     NULLIUS_CATALOGUE=self.user))
        for d in (VERSIONED, FLAT):
            os.makedirs(os.path.join(self.root, "agents", d["key"]))
            self.write(d)

    def write(self, d):
        with open(os.path.join(self.root, "agents", d["key"], "agent.json"), "w") as f:
            json.dump(d, f)

    def read(self, key, user=False):
        with open(os.path.join(self.user if user else self.root, "agents", key, "agent.json")) as f:
            return json.load(f)


class Resolve(Catalogue):
    def test_a_version_is_the_shared_fields_and_its_own(self):
        d = definitions.get("agent", "bot@2.0.0", self.root)
        self.assertEqual(d["upstream"], {"license": "MIT", "package": "@new/bot-acp", "version": "2.0.0"})
        self.assertEqual(d["install"], {"npm": ["@new/bot-acp@2.0.0"]})
        self.assertEqual(d["start"], VERSIONED["start"])
        self.assertEqual((d["_ref"], d["_version"], d["_default"]), ("bot@2.0.0", "2.0.0", "1.0.0"))
        self.assertEqual(d["offers"], [])
        self.assertNotIn("versions", d)

    def test_a_bare_key_is_the_default_version(self):
        d = definitions.get("agent", "bot", self.root)
        self.assertEqual(d["_version"], "1.0.0")
        self.assertEqual(d["offers"], ["agent.workspace/v1"])

    def test_an_unknown_version_is_nothing(self):
        self.assertIsNone(definitions.get("agent", "bot@9.9.9", self.root))
        self.assertIsNone(definitions.get("agent", "flat@1.0.0", self.root))

    def test_a_file_without_versions_is_its_one_version(self):
        d = definitions.get("agent", "flat@0.3.0", self.root)
        self.assertEqual((d["_ref"], d["_versions"]), ("flat", ["0.3.0"]))
        self.assertEqual(definitions.job_word(d), "flat")
        self.assertEqual(definitions.title(d), "Flat")

    def test_load_gives_the_default_or_every_version(self):
        one, bad = definitions.load("agent", self.root)
        self.assertEqual(bad, [])
        self.assertEqual([d["_ref"] for d in one], ["bot@1.0.0", "flat"])
        every, _ = definitions.load("agent", self.root, every=True)
        self.assertEqual([d["_ref"] for d in every], ["bot@2.0.0", "bot@1.0.0", "flat"])

    def test_names_for_pages_and_jobs(self):
        d = definitions.get("agent", "bot@2.0.0", self.root)
        self.assertEqual(definitions.title(d), "Bot 2.0.0")
        self.assertEqual(definitions.job_word(d), "bot-2.0.0")
        every, _ = definitions.load("agent", self.root, every=True)
        self.assertEqual(definitions.find(every, "bot")["_ref"], "bot@1.0.0")
        self.assertEqual(definitions.find(every, "bot@2.0.0")["_ref"], "bot@2.0.0")
        self.assertIsNone(definitions.find(every, "bot@3"))


class Match(Catalogue):
    def test_each_version_fits_on_its_own_proofs(self):
        every, _ = definitions.load("agent", self.root, every=True)
        row = match.table([BENCH], every, [])["toy"]
        self.assertEqual(row["bot@1.0.0"], [])
        self.assertIn("which Bot 2.0.0 does not offer", row["bot@2.0.0"][0])
        self.assertEqual(row["flat"], [])

    def test_a_proof_of_another_version_does_not_count(self):
        bumped = copy.deepcopy(FLAT)
        bumped["upstream"]["version"] = "0.4.0"
        bumped["install"] = {"npm": ["flat-acp@0.4.0"]}
        self.write(bumped)
        why = match.fit(BENCH, definitions.get("agent", "flat", self.root))
        self.assertEqual(len(why), 1)
        self.assertIn("passed on version 0.3.0, not 0.4.0: prove it again", why[0])


class Problems(unittest.TestCase):
    def bad(self, change):
        d = copy.deepcopy(VERSIONED)
        change(d)
        return definitions.problems(d, "agent")

    def test_the_shipped_shape_passes(self):
        self.assertEqual(definitions.problems(copy.deepcopy(VERSIONED), "agent"), [])

    def test_the_default_is_one_of_the_versions(self):
        self.assertIn("default: one of the versions (2.0.0, 1.0.0)", self.bad(lambda d: d.update(default="3.0.0")))

    def test_pins_and_proofs_go_in_each_version(self):
        got = self.bad(lambda d: d.update(install={"npm": ["x@1"]}, proven={}))
        self.assertIn("install: goes in each version, under versions", got)
        self.assertIn("proven: goes in each version, under versions", got)

    def test_only_some_fields_may_differ(self):
        got = self.bad(lambda d: d["versions"]["2.0.0"].update(label="Other"))
        self.assertTrue(any(p.startswith("versions.2.0.0: label cannot differ") for p in got), got)

    def test_a_version_needs_its_install(self):
        self.assertIn("versions.2.0.0.install: what this version installs",
                      self.bad(lambda d: d["versions"]["2.0.0"].pop("install")))

    def test_a_proof_names_its_own_version(self):
        got = self.bad(lambda d: d["versions"]["2.0.0"].update(proven={"agent.task/v1": dict(PROOF, version="1.0.0")}))
        self.assertIn("versions.2.0.0.proven.agent.task/v1: proven on 1.0.0, not 2.0.0", got)

    def test_a_problem_of_one_version_names_it_and_a_shared_one_is_said_once(self):
        got = self.bad(lambda d: d["versions"]["2.0.0"].update(install={"npm": ["not a pin"]}))
        self.assertIn("versions.2.0.0: install.npm: 'not a pin' is not name@version", got)
        got = self.bad(lambda d: d.update(label=""))
        self.assertEqual(got.count("label: say it"), 1)

    def test_a_version_name_is_plain(self):
        self.assertTrue(any("is not a version" in p for p in
                            self.bad(lambda d: d["versions"].update({"2.0 beta": d["versions"]["2.0.0"]}))))


class Write(Catalogue):
    def test_proving_a_version_writes_only_its_block(self):
        d = definitions.get("agent", "bot@2.0.0", self.root)
        d["proven"] = {"agent.task/v1": dict(PROOF, version="2.0.0")}
        definitions.save(d)
        raw = self.read("bot")
        self.assertEqual(raw["versions"]["2.0.0"]["proven"], {"agent.task/v1": dict(PROOF, version="2.0.0")})
        self.assertEqual(raw["versions"]["1.0.0"], VERSIONED["versions"]["1.0.0"])
        self.assertNotIn("offers", raw)
        self.assertEqual(definitions.problems(raw, "agent"), [])

    def test_a_kit_job_is_named_with_the_version(self):
        d = definitions.get("agent", "bot@2.0.0", self.root)
        self.assertEqual(conform.job_name("S", "agent-task-v1", definitions.job_word(d)), "S__kit__agent-task-v1__bot-2.0.0")

    def new_version(self, key, version):
        with patch.object(definitions, "CATALOGUE", self.root), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["new", "version", key, version])

    def test_new_version_turns_a_flat_file_into_versions(self):
        self.new_version("flat", "0.4.0")
        raw = self.read("flat", user=True)
        self.assertEqual(self.read("flat"), FLAT)  # the bundle stays intact
        self.assertEqual(raw["default"], "0.3.0")
        self.assertEqual(list(raw["versions"]), ["0.4.0", "0.3.0"])
        self.assertEqual(raw["versions"]["0.4.0"], {"install": {"npm": ["flat-acp@0.4.0"]}, "proven": {}})
        self.assertEqual(raw["versions"]["0.3.0"]["proven"], FLAT["proven"])
        self.assertEqual(raw["upstream"], {"package": "flat-acp", "license": "MIT"})
        self.assertNotIn("offers", raw)
        self.assertEqual(definitions.problems(dict(raw, _here=os.path.dirname(self.root)), "agent"), [])
        self.assertEqual(match.fit(BENCH, definitions.get("agent", "flat", self.root)), [])

    def test_new_version_copies_the_default_and_refuses_a_duplicate(self):
        self.new_version("bot", "1.1.0")
        self.assertEqual(self.read("bot", user=True)["versions"]["1.1.0"],
                         {"upstream": {"package": "@old/bot-acp"}, "install": {"npm": ["@old/bot-acp@1.1.0"]}, "proven": {}})
        self.assertEqual(self.read("bot"), VERSIONED)
        with self.assertRaises(SystemExit):
            self.new_version("bot", "1.1.0")


# A benchmark with two versions: the second adds a dataset and a driver of its own.
DRIVER = '''import json, sys
UNITS = {UNITS}
for line in sys.stdin:
    req = json.loads(line)
    op = req.pop("op")
    out = {{"units": UNITS}} if op == "units" else {{"prompt": "p"}} if op == "setup" else {{"reward": 0.0, "detail": {{}}}}
    print(json.dumps(out), flush=True)
'''
TOY = dict(BENCH, upstream={"license": "MIT"}, default="1",
           controls={"nop": {"source": "nullius", "from": "nothing runs"}},
           versions={"2": {"datasets": [{"id": "all", "units": 3}, {"id": "hard", "units": 2}],
                           "driver": {"command": ["python3", "{here}/driver2.py"]}, "proven": {}},
                     "1": {"proven": {"all": {"units": {"units": 2, "passed": "2 ids", "on": "2026-10-12"}}}}})


class BenchmarkVersions(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nullius-bench-versions-")
        self.user = self.enterContext(tempfile.TemporaryDirectory(prefix="nullius-bench-versions-user-"))
        self.enterContext(patch.dict(os.environ, NULLIUS_HOME=os.path.join(self.user, "profile"),
                                     NULLIUS_CATALOGUE=self.user))
        here = os.path.join(self.root, "benchmarks", "toy")
        os.makedirs(here)
        with open(os.path.join(here, "standard.json"), "w") as f:
            json.dump(TOY, f)
        for name, units in (("driver.py", ["a", "b"]), ("driver2.py", ["a", "b", "c"])):
            with open(os.path.join(here, name), "w") as f:
                f.write(DRIVER.format(UNITS=json.dumps(units)))
        os.makedirs(os.path.join(self.root, "agents", "flat"))
        with open(os.path.join(self.root, "agents", "flat", "agent.json"), "w") as f:
            json.dump(FLAT, f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_version_is_the_shared_fields_and_its_own(self):
        b1, b2 = definitions.get("benchmark", "toy", self.root), definitions.get("benchmark", "toy@2", self.root)
        self.assertEqual((b1["_ref"], b2["_ref"]), ("toy@1", "toy@2"))
        self.assertEqual([d["id"] for d in b1["datasets"]], ["all"])
        self.assertEqual([d["id"] for d in b2["datasets"]], ["all", "hard"])
        self.assertEqual(b2["driver"]["command"][1], "{here}/driver2.py")
        self.assertEqual((b1["proven"]["all"]["units"]["units"], b2["proven"]), (2, {}))
        self.assertEqual(b2["controls"], TOY["controls"])                             # shared
        self.assertNotIn("offers", b2)
        self.assertIsNone(definitions.get("benchmark", "toy@3", self.root))
        self.assertEqual([b["_ref"] for b in definitions.load("benchmark", self.root, every=True)[0]], ["toy@2", "toy@1"])
        self.assertEqual(definitions.load("benchmark", self.root)[1], [])

    def test_a_file_without_versions_is_one(self):
        b = definitions.resolve(dict(BENCH, upstream={"license": "MIT"}))
        self.assertEqual((b["_ref"], definitions.job_word(b), definitions.version_of(b)), ("toy", "toy", None))

    def test_problems_of_a_version_name_it(self):
        bad = definitions.problems(dict(TOY, versions=dict(TOY["versions"], **{"3": {"needs": []}})), "benchmark")
        self.assertTrue(any("versions.3: needs cannot differ between versions" in p for p in bad), bad)
        bad = definitions.problems(dict(TOY, proven={}), "benchmark")
        self.assertIn("proven: goes in each version, under versions", bad)
        bad = definitions.problems(dict(TOY, versions={"1": {}, "2": {"datasets": []}}), "benchmark")
        self.assertIn("versions.2: datasets: at least one", bad)

    def test_match_and_the_recipe_name_a_version(self):
        from nullius.standard import recipe
        defs = {"benchmark": definitions.load("benchmark", self.root, every=True)[0],
                "agent": definitions.load("agent", self.root, every=True)[0], "capability": []}
        self.assertEqual(set(match.table(defs["benchmark"], defs["agent"], [])), {"toy", "toy@2"})
        r = {"name": "x", "benchmark": "toy@2", "dataset": "hard", "other": "flat", "provider": "openai", "model": "m",
             "limit": 1, "concurrency": 1, "unsandboxed": True}
        out = recipe.compile(r, defs)
        self.assertEqual(out["errors"], [])
        self.assertIn("run toy@2 flat", out["script"])
        self.assertEqual(recipe.compile(dict(r, benchmark="toy"), defs)["errors"][0]["step"], recipe.STEP["dataset"])   # 1 has no hard
        with patch.object(recipe, "_all", return_value=defs):
            page = recipe.choices()["benchmarks"]
        self.assertEqual(len(page), 1)
        self.assertEqual([(v["ref"], v["default"]) for v in page[0]["versions"]], [("toy@2", False), ("toy", True)])

    def test_a_run_and_its_proof_carry_the_version(self):
        from nullius.standard import prove
        b = definitions.get("benchmark", "toy@2", self.root)
        got = prove.prove(b, dataset="hard", jobs_root=os.path.join(self.root, "jobs"), stamp="2026-10-12__00-00-00", say=lambda s: None)
        self.assertTrue(got["hard"]["nop"]["ok"], got)
        job = os.path.join(self.root, "jobs", "2026-10-12__00-00-00__toy-2__hard__prove-nop")
        with open(os.path.join(job, "config.json")) as f:
            std = json.load(f)["standard"]
        self.assertEqual((std["benchmark"], std["benchmark_version"], std["benchmark_ref"]), ("toy", "2", "toy@2"))
        raw = json.load(open(os.path.join(self.root, "benchmarks", "toy", "standard.json")))
        self.assertIn("hard", raw["versions"]["2"]["proven"])                           # its own block
        self.assertEqual(raw["versions"]["1"]["proven"], TOY["versions"]["1"]["proven"])
        self.assertNotIn("proven", raw)

    def test_new_version_of_a_benchmark(self):
        with patch.object(definitions, "CATALOGUE", self.root), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["new", "version", "toy", "3"])
        with open(os.path.join(self.user, "benchmarks", "toy", "standard.json")) as f:
            raw = json.load(f)
        with open(os.path.join(self.root, "benchmarks", "toy", "standard.json")) as f:
            self.assertEqual(json.load(f), TOY)
        with open(os.path.join(self.root, "benchmarks", "toy", "driver2.py")) as original, \
                open(os.path.join(self.user, "benchmarks", "toy", "driver2.py")) as copied:
            self.assertEqual(original.read(), copied.read())
        self.assertEqual(raw["versions"]["3"], {"proven": {}})
        self.assertEqual(raw["default"], "1")


class ForkVersion(unittest.TestCase):
    """A user's own source - a fork, at a tag, a branch or a commit - is a
    version of the benchmark already there: its ``upstream`` names the fork's
    repository and commit, cloned and checked like the published one's."""

    def git(self, *a, cwd):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a],
                              cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-fork-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        work = os.path.join(self.tmp, "work")
        os.makedirs(os.path.join(work, "data"))
        with open(os.path.join(work, "data", "tasks.json"), "w") as f:
            f.write("[1, 2]\n")
        self.git("init", "-q", cwd=work)
        self.git("add", ".", cwd=work)
        self.git("commit", "-qm", "release", cwd=work)
        self.git("tag", "v1.0.0", cwd=work)
        self.published = self.git("rev-parse", "HEAD", cwd=work)
        self.upstream = os.path.join(self.tmp, "upstream.git")
        self.git("clone", "-q", "--bare", work, self.upstream, cwd=self.tmp)
        with open(os.path.join(work, "data", "tasks.json"), "w") as f:              # the fork's fix
            f.write("[1, 2, 3]\n")
        self.git("commit", "-qam", "fix", cwd=work)
        self.fixed = self.git("rev-parse", "HEAD", cwd=work)
        self.fork = os.path.join(self.tmp, "fork.git")
        self.git("clone", "-q", "--bare", work, self.fork, cwd=self.tmp)
        self.home = os.path.join(self.tmp, "home")

    def bench(self, top, fork):
        return dict(BENCH, upstream=dict(top, license="MIT"), default="1",
                    datasets=[{"id": "all", "path": "data/tasks.json", "sha256": "0" * 64, "units": 2}],
                    versions={"1": {}, "fork": {"upstream": fork,
                                                "datasets": [{"id": "all", "path": "data/tasks.json", "sha256": "1" * 64, "units": 3}]}})

    def test_a_fork_is_a_version_with_its_own_repository_and_commit_and_no_borrowed_tag(self):
        d = self.bench({"repository": self.upstream, "tag": "v1.0.0", "tested": self.published},
                       {"repository": self.fork, "tested": self.fixed})
        one, fork = definitions.resolve(d, "1"), definitions.resolve(d, "fork")
        self.assertEqual((one["upstream"]["tag"], one["upstream"]["tested"]), ("v1.0.0", self.published))
        self.assertEqual((fork["upstream"]["repository"], fork["upstream"]["tested"]), (self.fork, self.fixed))
        self.assertNotIn("tag", fork["upstream"])                                  # the published tag is not the fork's
        self.assertEqual(fork["_ref"], "toy@fork")
        tagged = definitions.resolve(self.bench({"repository": self.upstream, "tag": "v1.0.0", "tested": self.published},
                                                {"repository": self.fork, "tag": "fix-1", "tested": self.fixed}), "fork")
        self.assertEqual(tagged["upstream"]["tag"], "fix-1")

    def test_each_version_is_cloned_at_its_own_commit_and_its_data_checked_by_its_own_digest(self):
        from nullius.standard import install
        d = self.bench({"repository": self.upstream, "tag": "v1.0.0", "tested": self.published},
                       {"repository": self.fork, "tested": self.fixed})
        one, fork = definitions.resolve(dict(d, _here=self.tmp), "1"), definitions.resolve(dict(d, _here=self.tmp), "fork")
        a, b = install.upstream(one, self.home), install.upstream(fork, self.home)
        self.assertNotEqual(a, b)
        with open(os.path.join(a, "data", "tasks.json")) as f1, open(os.path.join(b, "data", "tasks.json")) as f2:
            self.assertEqual((f1.read(), f2.read()), ("[1, 2]\n", "[1, 2, 3]\n"))
        right = install.sha256_of(os.path.join(b, "data", "tasks.json"))
        self.assertTrue(install.dataset(fork, dict(fork["datasets"][0], sha256=right), b))
        with self.assertRaises(install.InstallFailed):                              # the published digest does not hold for the fork
            install.dataset(fork, dict(fork["datasets"][0], sha256=install.sha256_of(os.path.join(a, "data", "tasks.json"))), b)
        drv = {"command": ["{install}/.venv/bin/python", "{here}/driver.py"], "install": {"python": "3.12", "project": "."}}
        self.assertNotEqual(install.driver_dir(dict(one, driver=drv), self.home), install.driver_dir(dict(fork, driver=drv), self.home))

    def test_a_fetch_never_waits_on_a_password(self):
        from nullius.standard import install
        self.assertEqual(install.git_env()["GIT_TERMINAL_PROMPT"], "0")

    def test_the_tag_is_a_plain_name_of_the_repository(self):
        top = {"repository": "https://github.com/o/r", "tag": "v1.0.0", "tested": "a" * 40}
        ok = self.bench(top, {"repository": "https://github.com/me/r", "tag": "fix-1", "tested": "b" * 40})
        ok["datasets"][0]["sha256"] = ok["versions"]["fork"]["datasets"][0]["sha256"] = "c" * 64
        self.assertEqual(definitions.problems(ok, "benchmark"), [])
        for bad in ("v 1", "../x", "v1.lock", "-v1", ""):
            got = definitions.problems(dict(ok, upstream=dict(top, tag=bad, license="MIT")), "benchmark")
            self.assertTrue(any("upstream.tag" in p for p in got), (bad, got))


if __name__ == "__main__":
    unittest.main()
