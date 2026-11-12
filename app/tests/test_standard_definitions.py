"""The standard's definitions are refused for what they get wrong, and pairs
fit only on ports offered and proven."""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from nullius.standard import cli, definitions, jsonl, match, ports
from tests import APP

BENCH = {
    "schema": "nullius.benchmark/v1", "key": "toy", "label": "Toy", "summary": "A toy.",
    "family": "agent", "needs": ["agent.task/v1", "agent.workspace/v1"],
    "driver": {"command": ["python3", "{here}/driver.py"]},
    "datasets": [{"id": "all", "units": 3}],
    "units": {"noun": "task", "nouns": "tasks"},
    "score": {"kind": "pass", "says": "the file is right"},
}
AGENT = {
    "schema": "nullius.agent/v1", "key": "bot", "label": "Bot", "summary": "A bot.",
    "start": {"acp": ["python3", "{here}/agent.py"]},
    "model": {"apis": ["chat"], "env": {"OPENAI_BASE_URL": "{base_url}", "OPENAI_API_KEY": "{key}"}},
    "offers": ["agent.task/v1", "agent.workspace/v1"],
    "proven": {"agent.task/v1": {"on": "2026-10-10", "passed": "1/1"},
               "agent.workspace/v1": {"on": "2026-10-10", "passed": "1/1"}},
}
CAPABILITY = {
    "schema": "nullius.capability/v1", "key": "mem", "label": "Mem", "summary": "A memory.",
    "kind": "memory", "start": {"command": ["python3", "{here}/capability.py"]},
    "offers": ["cap.memory/v1"], "proven": {"cap.memory/v1": {"on": "2026-10-10", "passed": "1/1"}},
}


def loaded(d, kind):
    return dict(d, _kind=kind, _here="/nowhere")


class Problems(unittest.TestCase):
    def bad(self, d, kind, edit, says):
        d = copy.deepcopy(d)
        edit(d)
        got = definitions.problems(d, kind)
        self.assertTrue(any(says in p for p in got), got)

    def test_caches_stay_inside_the_home(self):
        self.assertEqual(definitions.problems(dict(AGENT, caches=["{home}/.codex/.tmp", "{home}/.npm"]), "agent"), [])
        self.assertEqual(definitions.problems(dict(CAPABILITY, caches=["{home}/.cache/x"]), "capability"), [])
        for c in (["{home}"], ["{home}/"], ["{home}/../out"], ["{home}/a/../../b"], ["/tmp/x"], ["{install}/x"], "{home}/.npm"):
            self.bad(dict(AGENT, caches=c), "agent", lambda d: None, "caches: a list of paths under {home}/")

    def test_a_project_install_needs_its_upstream_and_a_pinned_shape(self):
        up = {"repository": "https://example.org/x/y", "tested": "a" * 40, "license": "MIT"}
        ok = dict(CAPABILITY, upstream=up, env={"HF_HUB_OFFLINE": "1", "WHERE": "{install}/x"},
                  install={"python": "3.12", "project": "services/a", "extras": ["search"],
                           "indexes": [{"url": "https://download.pytorch.org/whl/cpu", "packages": ["torch"]}], "prefetch": ["BAAI/bge-small-en-v1.5"]})
        self.assertEqual(definitions.problems(ok, "capability"), [])
        self.bad(ok, "capability", lambda d: d.pop("upstream"), "install.project: needs upstream")
        self.bad(ok, "capability", lambda d: d["install"].update(project="../out"), "install.project: a folder")
        self.bad(ok, "capability", lambda d: d["install"].update(prefetch=["not a model"]), "install.prefetch")
        self.bad(ok, "capability", lambda d: d["install"].update(indexes=[{"url": "http://x"}]), "install.indexes[0]")
        self.bad(ok, "capability", lambda d: d["install"].pop("project"), "install.extras: extras of install.project")
        self.bad(ok, "capability", lambda d: d["env"].update(X="{key}"), "env.X: {key} is not a slot")
        self.bad(ok, "capability", lambda d: d["env"].update(X="$(rm -rf ~)"), "env.X: a variable name and a plain template")

    def test_a_wheel_by_url_is_pinned_by_its_digest(self):
        wheel = {"url": "https://example.org/r/model-1.0-py3-none-any.whl", "sha256": "a" * 64}
        ok = dict(CAPABILITY, install={"pip": ["lib==1.0"], "wheels": [wheel]})
        self.assertEqual(definitions.problems(ok, "capability"), [])
        for bad in ({"url": wheel["url"]}, dict(wheel, sha256="abc"), dict(wheel, url="http://example.org/r/m.whl"),
                    dict(wheel, url="https://example.org/r/model.tar.gz"), "model==1.0"):
            self.bad(ok, "capability", lambda d, bad=bad: d["install"].update(wheels=[bad]), "install.wheels[0]")

    def test_a_feature_may_call_a_model_through_the_relay(self):
        self.assertEqual(definitions.problems(dict(CAPABILITY, model={"apis": ["chat"], "env": {"API_BASE": "{base_url}"}}), "capability"), [])
        self.bad(CAPABILITY, "capability", lambda d: d.update(model={"apis": ["chat"], "env": {"API_BASE": "{secret}"}}), "is not a slot")

    def test_a_dataset_is_in_the_upstream_or_downloaded_with_its_digest(self):
        url = "https://huggingface.co/datasets/x/y/resolve/" + "a" * 40 + "/d.json"
        self.assertEqual(definitions.problems(dict(BENCH, datasets=[{"id": "d", "url": url, "sha256": "b" * 64, "units": 2}]), "benchmark"), [])
        self.bad(BENCH, "benchmark", lambda d: d.update(datasets=[{"id": "d", "url": url, "units": 2}]), "sha256")
        self.bad(BENCH, "benchmark", lambda d: d.update(datasets=[{"id": "d", "url": url, "path": "d.json", "sha256": "b" * 64, "units": 2}]), "not both")

    def test_a_judge_is_named_and_said(self):
        self.assertEqual(definitions.problems(dict(BENCH, score={"kind": "pass", "says": "s", "judge": {"model": "gpt-4o", "says": "the paper's"}}), "benchmark"), [])
        self.bad(BENCH, "benchmark", lambda d: d.update(score={"kind": "pass", "says": "s", "judge": {"model": "gpt 4o"}}), "score.judge")

    def test_an_example_is_marked_true_or_false_on_an_agent_or_a_feature(self):
        self.assertEqual(definitions.problems(dict(AGENT, example=True), "agent"), [])
        self.bad(AGENT, "agent", lambda d: d.update(example="yes"), "example: true or false")
        self.bad(BENCH, "benchmark", lambda d: d.update(example=True), "example: true or false")
        self.assertIs(definitions.get("agent", "reference", definitions.CATALOGUE).get("example"), True)

    def test_unknown_port_is_named(self):
        self.bad(BENCH, "benchmark", lambda d: d["needs"].append("agent.telepathy/v1"), "'agent.telepathy/v1' is not a port")

    def test_port_of_the_other_family_is_refused(self):
        self.bad(BENCH, "benchmark", lambda d: d["needs"].append("cap.memory/v1"), "not the agent")

    def test_shell_in_a_command_is_refused(self):
        self.bad(BENCH, "benchmark", lambda d: d["driver"].update(command=["sh", "-c", "rm -rf ~"]), "is not a plain word")

    def test_dataset_file_needs_its_digest(self):
        self.bad(BENCH, "benchmark", lambda d: d["datasets"][0].update(path="data/x.json", sha256="abc"), "sha256")

    def test_upstream_at_a_full_commit(self):
        self.bad(BENCH, "benchmark", lambda d: d.update(upstream={"repository": "https://github.com/a/b", "tested": "main", "license": "MIT"}),
                 "upstream.tested")

    def test_an_offer_without_its_proof_is_refused(self):
        self.bad(AGENT, "agent", lambda d: d["offers"].append("agent.tools/v1"), "offers agent.tools/v1, which has no conformance record")

    def test_command_agent_offers_only_task_and_workspace(self):
        def edit(d):
            d["start"] = {"command": ["bot", "{prompt}"]}
            d["offers"].append("agent.tools/v1")
            d["proven"]["agent.tools/v1"] = {}
        self.bad(AGENT, "agent", edit, "a command agent can offer only")

    def test_model_templates_use_known_slots_and_stay_in_home(self):
        self.bad(AGENT, "agent", lambda d: d["model"]["env"].update(X="{password}"), "is not a slot")
        self.bad(AGENT, "agent", lambda d: d["model"].update(files={"/etc/passwd": "t.toml"}), "is a path under {home}/")
        self.bad(AGENT, "agent", lambda d: d.update(model={"apis": ["chat"]}), "env or files")

    def test_side_models_are_model_names_each_once(self):
        ok = json.loads(json.dumps(AGENT))
        ok["model"].update(apis=["chat", "messages"], side_models=["a-reviewer", "vendor/summary-1"])
        self.assertEqual(definitions.problems(ok, "agent"), [])
        for side in ("a-reviewer", ["x", "x"], ["bad name"], [3]):
            self.bad(AGENT, "agent", lambda d, s=side: d["model"].update(side_models=s), "model.side_models")

    def test_feature_offers_ports_of_its_kind(self):
        self.bad(CAPABILITY, "capability", lambda d: d.update(kind="retriever"), "not a port of its kind")

    def test_every_port_name_is_well_formed_and_every_kit_exists(self):
        names = {k["key"] for k in definitions.kits()}
        for p, spec in ports.PORTS.items():
            self.assertRegex(p, ports.NAME)
            if spec["kit"]:
                self.assertIn(spec["kit"], names, p)
        for k in definitions.kits():
            self.assertEqual(len(k["needs"]), 1, k["key"])        # a kit proves one port

    def test_a_save_applies_its_change_to_the_file_as_it_is_now(self):
        """Two proofs of one benchmark overlap, or the user edits it while a
        conformance check runs: each save used to write back the copy loaded
        at its start and drop what the other wrote."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "standard.json")
            with open(path, "w") as f:
                json.dump(dict(BENCH, proven={}), f)
            first, second = (dict(BENCH, proven={}, _here=tmp, _kind="benchmark") for _ in range(2))
            definitions.save(first, lambda cur: cur.update(proven=dict(cur.get("proven") or {}, a={"nop": {"on": "1"}})))
            with open(path) as f:
                edited = json.load(f)
            edited["summary"] = "Edited meanwhile."
            with open(path, "w") as f:
                json.dump(edited, f)
            definitions.save(second, lambda cur: cur.update(proven=dict(cur.get("proven") or {}, b={"nop": {"on": "2"}})))
            with open(path) as f:
                saved = json.load(f)
        self.assertEqual(sorted(saved["proven"]), ["a", "b"])
        self.assertEqual(saved["summary"], "Edited meanwhile.")
        self.assertEqual(sorted(second["proven"]), ["a", "b"])           # the caller sees what was written

    def test_a_field_of_the_wrong_type_leaves_the_definition_out_and_says_so(self):
        """agent.json with "model": "gpt-4o", standard.json with "score": "pass":
        the checks crashed and /new-run, /agents and /benchmarks returned 500."""
        with tempfile.TemporaryDirectory() as tmp:
            for kind, wrong in (("agent", {"model": "gpt-4o"}), ("benchmark", {"score": "pass"})):
                path = os.path.join(tmp, definitions.KINDS[kind]["file"])
                with open(path, "w") as f:
                    json.dump({"schema": f"nullius.{kind}/v1", "key": "x", "label": "X", "summary": "x", **wrong}, f)
                d, bad = definitions._read(path, kind, "x")
                self.assertIsNone(d)
                self.assertTrue(any("wrong type" in b for b in bad), bad)

    def test_catalogue_check_details_cover_the_driver_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kit in definitions.kits():
                with self.subTest(kit=kit["key"]):
                    driver = jsonl.Proc([sys.executable, os.path.join(definitions.KITS, kit["key"], "driver.py")],
                                       cwd=tmp, env=dict(os.environ, PYTHONPATH=str(APP)),
                                       log=os.path.join(tmp, "driver.log"))
                    try:
                        units = driver.request("units", dataset="kit", timeout=10)["units"]
                    finally:
                        driver.close()
                    details = kit["check_details"]
                    self.assertEqual(list(details), units)
                    self.assertEqual(len(details), sum(d["units"] for d in kit["datasets"]))
                    for detail in details.values():
                        for field in ("label", "task", "passes"):
                            self.assertTrue(detail[field].strip())


class DecimalBenchmarkOptions(unittest.TestCase):
    def test_temperature_accepts_decimals_and_rejects_invalid_values(self):
        for key in ("gsm8k", "humaneval"):
            bench = definitions.get("benchmark", key)
            for value in ("default", "0", "0.5", "0.2", "0.8", "1.25"):
                with self.subTest(key=key, value=value):
                    values, errors = definitions.bench_options_of(bench, bench["datasets"][0]["id"],
                                                                 {"temperature": value}, setup="original")
                    self.assertEqual(errors, [])
                    self.assertEqual(values["temperature"], value)
            for value in ("-1", "nan", "inf", "abc", "0.5;echo"):
                with self.subTest(key=key, invalid=value):
                    self.assertTrue(definitions.option_problem(bench["options"]["temperature"], value))

class Fit(unittest.TestCase):
    def test_fits_when_every_need_is_offered_and_proven(self):
        self.assertEqual(match.fit(BENCH, loaded(AGENT, "agent")), [])

    def test_missing_port_is_named(self):
        b = dict(BENCH, needs=BENCH["needs"] + ["agent.conversation/v1"])
        why = match.fit(b, loaded(AGENT, "agent"))
        self.assertEqual(len(why), 1)
        self.assertIn("agent.conversation/v1", why[0])
        self.assertIn("does not offer", why[0])

    def test_claimed_but_unproven_is_not_a_fit(self):
        a = loaded(dict(AGENT, proven={"agent.task/v1": {}}), "agent")
        self.assertIn("has not passed its kit", match.fit(BENCH, a)[0])

    def test_a_reference_agent_fits_its_own_benchmarks_alone(self):
        a = loaded(dict(AGENT, only_for=["toy"]), "agent")
        self.assertEqual(match.fit(BENCH, a), [])
        why = match.fit(dict(BENCH, key="other"), a)
        self.assertEqual(why, ["Bot is the reference agent of toy, and runs no other benchmark"])
        for bad in ([], "toy", ["toy", "toy"], ["Not A Key"]):
            with self.subTest(only_for=bad):
                self.assertIn("only_for: the keys of the benchmarks it is the reference agent of, each once",
                              definitions.problems(dict(AGENT, only_for=bad), "agent"))
        self.assertEqual(definitions.problems(dict(AGENT, only_for=["toy"]), "agent"), [])

    def test_feature_never_runs_an_agent_benchmark(self):
        self.assertIn("takes an agent", match.fit(BENCH, loaded(CAPABILITY, "capability"))[0])

    def test_table_offers_each_family_its_own(self):
        fb = dict(BENCH, key="fb", family="capability", needs=["cap.memory/v1"])
        t = match.table([BENCH, fb], [loaded(AGENT, "agent")], [loaded(CAPABILITY, "capability")])
        self.assertEqual(t, {"toy": {"bot": []}, "fb": {"mem": []}})


class SetupLabels(unittest.TestCase):
    def test_unrestricted_substitute_is_named_without_changing_recipe_key(self):
        for key in ("gsm8k", "humaneval"):
            bench = definitions.find(definitions.load("benchmark", definitions.CATALOGUE)[0], key)
            self.assertEqual(definitions.setup_label(bench, "original"), "Original")
        for spec in ({"models": ["published"]}, {"providers": {"openai": ""}}):
            self.assertEqual(definitions.setup_label({"setups": {"original": spec}}, "original"), "Original")
        self.assertEqual(definitions.setup_label({}, "model"), "Original")


class Load(unittest.TestCase):
    def test_folder_name_is_the_key_and_bad_files_are_reported(self):
        with tempfile.TemporaryDirectory() as root:
            for key, body in (("toy", BENCH), ("other", dict(BENCH, key="toy")), ("broken", None)):
                os.makedirs(os.path.join(root, "benchmarks", key))
                with open(os.path.join(root, "benchmarks", key, "standard.json"), "w") as f:
                    f.write(json.dumps(body) if body else "{")
            got, bad = definitions.load("benchmark", root)
            self.assertEqual([d["key"] for d in got], ["toy"])
            self.assertTrue(any("not its folder's name" in b for b in bad), bad)
            self.assertTrue(any("broken" in b for b in bad), bad)

    def test_shipped_definitions_all_load(self):
        for kind in definitions.KINDS:
            _, bad = definitions.load(kind, definitions.CATALOGUE)
            self.assertEqual(bad, [], kind)

    def test_the_bundle_passes_nullius_standard_check(self):
        """Every definition, kit, settings inventory and catalogue entry, under an empty profile."""
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"NULLIUS_HOME": home}), \
                contextlib.redirect_stdout(out):
            os.environ.pop("NULLIUS_CATALOGUE", None)
            code = cli.cmd_check(argparse.Namespace(path=None))
        self.assertEqual(out.getvalue(), "ok\n")
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
