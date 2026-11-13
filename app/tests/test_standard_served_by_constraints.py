"""Provider pins and machine constraints on the same run and recipe.

These intersections need both features: neither side's tests exercise them
together. The fixture driver scores locally; no endpoint or engine is used.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from nullius import constraints, progress
from nullius.standard import cli, container, definitions, install, job, merge, recipe, run
from tests import test_standard_recipe_served_by as recipes


class Together(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(install, "HOME", str(self.tmp / "install")))
        self.addCleanup(container.cap, {})
        self.b = recipes.judged()
        self.a = definitions.get("agent", "fake", recipes.ROOT)
        self.pin = {"m-judge": ["prov-a"]}

    def make(self, **kw):
        values = dict(model="m", provider="openrouter", env={"OPENROUTER_API_KEY": "or-test"},
                      judge_model="m-judge", served_by=self.pin, unsandboxed=True,
                      jobs_root=str(self.tmp), say=lambda _: None)
        values.update(kw)
        return run.Run(self.b, self.a, **values)

    def test_the_cli_records_units_constraints_and_pins_together(self):
        units = self.tmp / "units.txt"
        units.write_text("good\n")
        self.enterContext(mock.patch.object(cli, "_bench", return_value=self.b))
        self.enterContext(mock.patch.object(cli, "_exit_on_term"))
        self.enterContext(mock.patch.object(cli.settings, "load"))
        self.enterContext(mock.patch.object(progress, "ECHO", None))
        self.enterContext(mock.patch.object(run.relay.Relay, "start", lambda r: r))
        self.enterContext(mock.patch.object(run.relay.Relay, "public", "http://127.0.0.1:9"))
        self.enterContext(mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-test", "OPENAI_API_KEY": "or-test"}))
        seen = []
        proc = run.jsonl.Proc

        def driver(cmd, cwd, env, log, **kw):
            seen.append(env)
            return proc(cmd, cwd, env, log, **kw)

        self.enterContext(mock.patch.object(run.jsonl, "Proc", driver))
        for i, extra in enumerate(([], ["--from-run"])):
            out = io.StringIO()
            with self.subTest(extra=extra), redirect_stdout(out):
                code = cli.main(["run", "fakebench", "nop", "--jobs", str(self.tmp), "--name", f"job-{i}",
                                 "--units", str(units), "-j", "4", "--constraint", "concurrency=1",
                                 "--constraint", "gpu_ids=", "--provider", "openrouter", "--judge-model", "m-judge",
                                 "--served-by", "m-judge=prov-a", *extra])
                self.assertEqual(code, 0, out.getvalue())
            path = Path(out.getvalue().strip().splitlines()[-1])
            cfg = json.loads((path / "config.json").read_text())
            self.assertEqual(cfg["standard"]["units_file"], run.units_file(str(units)))
            self.assertEqual(cfg["standard"]["constraints"], {"concurrency": 1, "gpu_ids": []})
            self.assertEqual(cfg["standard"]["served_by"], self.pin)
            self.assertEqual(cfg["n_concurrent_trials"], 1)
            self.assertEqual(sorted(p.name for p in path.iterdir() if "__u" in p.name), ["good__u0000"])
            self.assertEqual(json.loads((path / "result.json").read_text())["served"]["held"], {"m-judge": None})
        self.assertTrue(seen)
        self.assertTrue(all(env["CUDA_VISIBLE_DEVICES"] == "" for env in seen))

    def test_constraint_and_host_refusals_precede_pin_and_endpoint_checks(self):
        with mock.patch.object(run, "provider_endpoint") as endpoint:
            with self.assertRaisesRegex(run.Refused, "constraints:.*at least 1"):
                self.make(constraints={"concurrency": 0}, served_by={"stray": ["prov-a"]})
            self.b = dict(self.b, host={"gpu": {"check": ["nvidia-smi"]}},
                          options={"compute": {"default": "gpu", "values": ["gpu"], "needs": {"gpu": {"host": ["gpu"]}}}})
            with self.assertRaisesRegex(run.Refused, "allows Nullius no GPU"):
                self.make(constraints={"gpu_ids": []}, served_by={"stray": ["prov-a"]}, which=lambda _: "/bin/true")
            with self.assertRaisesRegex(run.Refused, "--served-by.*does not call"):
                self.make(constraints={"gpu_ids": [0]}, served_by={"stray": ["prov-a"]}, which=lambda _: "/bin/true")
            endpoint.assert_not_called()
        out = io.StringIO()
        with mock.patch.object(cli, "_bench", return_value=self.b), mock.patch.object(cli, "_exit_on_term"), \
                mock.patch.object(cli.settings, "load"), mock.patch.object(run, "run_and_report") as start, redirect_stdout(out):
            code = cli.main(["run", "fakebench", "nop", "--constraint", "concurrency=0", "--served-by", "bad slug"])
        self.assertEqual(code, 2)
        self.assertIn("refused: constraints:", out.getvalue())
        start.assert_not_called()

    def test_a_valid_pin_still_requires_enforced_machine_caps(self):
        r = self.make(constraints={"cpus": 1})
        with mock.patch.object(constraints, "scope_check", return_value={"why": "unavailable", "enforced": [], "refused": {}}), \
                mock.patch.object(install, "packages") as setup, self.assertRaisesRegex(run.Refused, "systemd does not enforce"):
            r.ready()
        setup.assert_not_called()

    def test_the_container_relay_takes_both_caps_and_pins(self):
        self.enterContext(mock.patch.object(container, "_security", ""))
        with mock.patch.object(container, "unusable", return_value=None):
            r = self.make(backend="container", constraints={"cpus": 1, "memory_mb": 512})
        argv = container.relay_argv("img", name="relay", job="job", app="/app", record="/rec", key_file="/key",
                                    base="https://openrouter.ai/api/v1", model="m", routes={"u0-judge": "m-judge"}, serving=r.serving)
        self.assertEqual(argv[argv.index("--cpus") + 1], "1")
        self.assertEqual(argv[argv.index("--memory") + 1], "512m")
        self.assertEqual(argv[argv.index("--served-by") + 1], "m-judge=prov-a")
        with mock.patch.object(r, "scope_ready", return_value={}), \
                mock.patch.object(container, "base_tag", return_value="img"), \
                mock.patch.object(container, "present", return_value=True), \
                mock.patch.object(container, "base_image", return_value="img"), \
                mock.patch.object(container, "unenforced", return_value={"cpus": "max 100000"}), \
                self.assertRaisesRegex(run.Refused, "engine does not apply"):
            r.ready()

    def test_each_compiled_shard_has_its_constraints_and_each_called_models_pin(self):
        given = {"local": {"concurrency": 1}, "127.0.0.1": {"cpus": 2, "gpu_ids": []}}
        with mock.patch.dict(os.environ, {constraints.SETTING: json.dumps(given)}):
            out = recipe.compile(recipes.a_recipe(where=list(given), others=["fake", "nop"],
                                 model="openai/gpt-4o-mini", served_by={"model": ["prov-a"]}), recipes.defs(self.b))
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["served_models"]["model"], "openai/gpt-4o-mini")
        for part in out["shards"]:
            for line in part["command"].splitlines():
                words = shlex.split(line)
                self.assertEqual([words[i + 1] for i, w in enumerate(words) if w == "--constraint"],
                                 constraints.cli_words(given[part["where"]])[1::2])
                self.assertEqual([words[i + 1] for i, w in enumerate(words) if w == "--served-by"],
                                 ["openai/gpt-4o-mini=prov-a"])
                self.assertLess(words.index("--constraint"), words.index("--served-by"))

    def test_builder_keeps_both_validation_errors_in_main_first_order(self):
        with mock.patch.dict(os.environ, {constraints.SETTING: '{"local": {"cpus": 0}}'}):
            out = recipe.compile(recipes.a_recipe(model="openai/gpt-4o-mini",
                                 served_by={"model": ["prov-a"], "judge": ["prov-b"]}), recipes.defs(self.b))
        self.assertEqual([e["step"] for e in out["errors"]], [recipe.STEP["where"], recipe.STEP["model"]])
        self.assertIn("HOST_CONSTRAINTS", out["errors"][0]["message"])
        self.assertIn("pins openai/gpt-4o-mini twice", out["errors"][1]["message"])
        self.assertEqual((out["script"], out["parts"]), ("", []))

    def test_merge_allows_machine_limits_to_differ_but_still_checks_pins(self):
        paths = []
        for i in (1, 2):
            path = self.tmp / f"shard-{i}"
            path.mkdir()
            job.write_config(str(path), job=path.name, bench=self.b, other=self.a, model="m", provider="openrouter",
                             dataset="all", units=[], concurrency=i, granted=True, launch="launch",
                             shard={"index": i, "of": 2, "units_total": 0, "planned": []},
                             constraints={"concurrency": i}, served_by=self.pin)
            (path / "result.json").write_text(json.dumps({"served": {"calls": {"Prov A": 1}, "unnamed": 0,
                                                                    "held": {"m-judge": True}}}))
            paths.append(str(path))
        self.assertEqual(merge.check(paths)["differ"], [])
        merge.merge(paths, str(self.tmp / "joined"))
        cfg = json.loads((self.tmp / "joined/config.json").read_text())["standard"]
        self.assertEqual((cfg["constraints"], cfg["served_by"]), ({"concurrency": 1}, self.pin))
        self.assertEqual(json.loads((self.tmp / "joined/result.json").read_text())["served"]["held"], {"m-judge": True})
        path = Path(paths[1]) / "config.json"
        cfg = json.loads(path.read_text())
        cfg["standard"]["served_by"] = {"m-judge": ["prov-b"]}
        path.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(merge.Refused, "serving providers"):
            merge.check(paths)
        merge.merge(paths, str(self.tmp / "forced"), force=True)
        self.assertEqual(json.loads((self.tmp / "forced/result.json").read_text())["served"]["held"], {"m-judge": "unknown"})


if __name__ == "__main__":
    unittest.main()
