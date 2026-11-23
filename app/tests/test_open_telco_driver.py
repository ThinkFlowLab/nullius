"""Open Telco's driver glue, with GSMA's tasks and Inspect AI's eval loop stubbed.

No download, no Inspect install, no model call: the stubs stand for the
upstream's task functions and Inspect's eval, so these tests check what the
driver asks of them and what it makes of their reply.
"""
import copy
import hashlib
import importlib.util
import inspect as pyinspect
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

FOLDER = Path(__file__).resolve().parents[1] / "nullius" / "catalogue" / "benchmarks" / "open-telco"
REV = "a" * 40


def sample(text, target, choices=None, id=None, **metadata):
    return SimpleNamespace(id=id, input=text, target=target, choices=choices, metadata=metadata or None)


SUBJECTS = ["Lexicon", "Research overview"] * 6
DATA = {
    "teleqna": [sample(f"q{i}", "AB"[i % 2], ["x", "y"], subject=s) for i, s in enumerate(SUBJECTS, 1)],
    "teletables": [sample("t1", "C", ["a", "b", "c"], id="t_7_abc123"), sample("t2", "A", ["a", "b", "c"], id="t_9_def456")],
    "telemath": [sample("m1", "0.5"), sample("m2", "12.0")],
    "telelogs": [sample("l1", "C3")],
    "3gpp_tsg": [sample("d1", "SA5")],
}
FILES = {"teleqna": "test_teleqna.json", "teletables": "test_teletables.json", "telemath": "test_telemath.json",
         "telelogs": "test_telelogs.json", "3gpp_tsg": "test_3gpp_tsg.json"}


class Upstream:
    """GSMA's task functions and Inspect's eval loop, as far as the driver sees them."""

    def __init__(self):
        self.built, self.evals, self.models = [], [], []

    def tasks(self):
        def make(name, config, system=None, **params):
            def fn(dataset_path="GSMA/ot-lite", split="test", full=False, **args):
                self.built.append((name, dataset_path, args))
                rows = [copy.copy(s) for s in DATA[config]]                  # each load is new objects, as hf_dataset's
                for k, v in args.items():
                    if k in ("subject", "difficulty", "task_id") and v not in ("full", None):
                        rows = [s for s in rows if (s.metadata or {}).get(k) == v]
                return SimpleNamespace(dataset=rows, system=system, args=args)
            fn.__name__ = name
            fn.__module__ = f"evals.{name}.{name}"
            fn.__signature__ = pyinspect.Signature(
                [pyinspect.Parameter(p, pyinspect.Parameter.KEYWORD_ONLY, default=d)
                 for p, d in {**params, "dataset_path": "GSMA/ot-lite", "split": "test", "full": False}.items()])
            module = ModuleType(fn.__module__)
            module.DEFAULT_DATASET_NAME = config
            return fn, module

        return {"teleqna": make("teleqna", "teleqna", subject="full"), "teletables": make("teletables", "teletables"),
                "telemath": make("telemath", "telemath", system="SOLVE, THEN \\boxed{}"),
                "telelogs": make("telelogs", "telelogs", eval_type="soft"), "three_gpp": make("three_gpp", "3gpp_tsg")}

    def eval(self, task, model, sample_id, log_dir, display, score_display, **config):
        self.evals.append({"task": task, "model": model, "sample_id": sample_id, "config": config})
        s = next(x for x in task.dataset if x.id == sample_id) if isinstance(sample_id, str) else task.dataset[sample_id - 1]
        asked = ([SimpleNamespace(text=task.system)] if task.system else []) + [SimpleNamespace(text="ASK " + s.input)]
        reply = model.kw["custom_outputs"][0].content if model.name == "mockllm/model" else "ANSWER: B"
        right = bool(reply) and str(s.target) in reply
        verdict = SimpleNamespace(value="C" if right else "I", answer=reply[-3:])
        logged = SimpleNamespace(events=[SimpleNamespace(event="sample_init"), SimpleNamespace(event="model", input=asked)],
                                 output=SimpleNamespace(completion=reply), scores={"stub_scorer": verdict},
                                 target=s.target, error=None)
        return [SimpleNamespace(samples=[logged], status="success", error=None)]

    def get_model(self, name, **kw):
        self.models.append((name, kw))
        return SimpleNamespace(name=name, kw=kw)

    def modules(self, fetch=None):
        inspect_ai = ModuleType("inspect_ai")
        inspect_ai.eval = self.eval
        model = ModuleType("inspect_ai.model")
        model.get_model = self.get_model
        model.ModelOutput = SimpleNamespace(from_content=lambda model, content: SimpleNamespace(content=content))
        scorer = ModuleType("inspect_ai.scorer")
        scorer.value_to_float = lambda: (lambda v: 1.0 if v == "C" else 0.0)
        registry = ModuleType("evals._registry")
        evals = ModuleType("evals")
        evals._registry = registry
        mods = {"inspect_ai": inspect_ai, "inspect_ai.model": model, "inspect_ai.scorer": scorer,
                "evals": evals, "evals._registry": registry}
        registry.__all__ = []
        for name, (fn, module) in self.tasks().items():
            registry.__all__.append(name)
            setattr(registry, name, fn)
            mods[fn.__module__] = module
        hub = ModuleType("huggingface_hub")
        hub.hf_hub_download = fetch or (lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download expected")))
        mods["huggingface_hub"] = hub
        return mods


class OpenTelcoDriver(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nullius-open-telco-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.here = self.root / "definition"
        self.here.mkdir()
        shutil.copy(FOLDER / "driver.py", self.here / "driver.py")
        self.upstream = self.root / "upstream"
        self.snap = self.upstream / ".open-telco-data" / f"org__lite@{REV}"
        self.snap.mkdir(parents=True)
        files = {"README.md": b"configs: every test"} | {f: f.encode() for f in FILES.values()}
        for name, data in files.items():
            (self.snap / name).write_bytes(data)
        digest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        source = {"cards": {"org/lite": digest["README.md"]},
                  "datasets": {f"{c}-lite": {"repository": "org/lite", "revision": REV, "file": f, "sha256": digest[f]}
                               for c, f in FILES.items()}}
        (self.here / "data-source.json").write_text(json.dumps(source))
        self.work = self.root / "work"
        self.work.mkdir()
        cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, cwd)
        self.up = Upstream()

    def load(self, dataset, fetch=None, **options):
        env = {"NULLIUS_UPSTREAM": str(self.upstream), "NULLIUS_DATASET_ID": dataset,
               **{"NULLIUS_OPTION_" + k.upper(): v for k, v in options.items()}}
        stubs = patch.dict(sys.modules, self.up.modules(fetch))
        environment = patch.dict(os.environ, env)
        stubs.start()
        environment.start()
        self.addCleanup(stubs.stop)
        self.addCleanup(environment.stop)
        spec = importlib.util.spec_from_file_location(f"open_telco_driver_{dataset.replace('-', '_')}", self.here / "driver.py")
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        return driver

    def test_units_are_the_datas_own_ids_else_the_place_in_the_file(self):
        self.assertEqual(self.load("teletables-lite").units("teletables-lite")["units"], ["t_7_abc123", "t_9_def456"])
        units = self.load("teleqna-lite").units("teleqna-lite")["units"]
        self.assertEqual(units, [f"{i:02d}" for i in range(1, 13)])         # padded to the file's width
        self.assertEqual(self.up.built[-1][1], str(self.snap))                 # the pinned copy, as dataset_path

    def test_a_filter_runs_the_tasks_own_argument_and_keeps_each_questions_number(self):
        driver = self.load("teleqna-lite", subject="research-overview")
        self.assertEqual(driver.units("teleqna-lite")["units"], ["02", "04", "06", "08", "10", "12"])
        self.assertIn(("teleqna", str(self.snap), {"subject": "Research overview"}), self.up.built)   # as the data write it
        everything = self.load("teleqna-lite", subject="all")
        self.assertEqual(len(everything.units("teleqna-lite")["units"]), 12)

    def test_the_prompt_is_what_the_task_asks_its_model_and_nothing_is_written(self):
        driver = self.load("telemath-lite")
        prompt = driver.setup("2", str(self.work))["prompt"]
        self.assertEqual(prompt, "SOLVE, THEN \\boxed{}\n\nASK m2")          # the system message first, then the question
        (call,) = self.up.evals
        self.assertEqual((call["model"].name, call["model"].kw["custom_outputs"][0].content, call["sample_id"]),
                         ("mockllm/model", "", 2))
        self.assertEqual([p.name for p in self.work.iterdir() if p.name != "open-telco.lock"], [])

    def test_the_answer_is_scored_as_the_tasks_model_reply(self):
        driver = self.load("teleqna-lite")
        right = driver.score("01", str(self.work), [], "ANSWER: B")
        self.assertEqual((right["reward"], right["passed"], right["detail"]["target"]), (1.0, True, "B"))
        self.assertEqual(self.up.evals[-1]["model"].kw["custom_outputs"][0].content, "ANSWER: B")
        nothing = driver.score("01", str(self.work), [], None)
        self.assertEqual((nothing["reward"], nothing["passed"]), (0.0, False))
        self.assertEqual(self.up.evals[-1]["model"].kw["custom_outputs"][0].content, "")

    def test_the_scorers_own_argument_reaches_the_task(self):
        driver = self.load("telelogs-lite", eval_type="hard")
        driver.score("1", str(self.work), [], "\\boxed{C3}")
        self.assertEqual(self.up.evals[-1]["task"].args, {"eval_type": "hard"})

    def test_the_reference_is_written_as_each_prompt_asks(self):
        self.assertEqual(self.load("teleqna-lite").oracle("02", "")["answer"], "ANSWER: A")
        self.assertEqual(self.load("teletables-lite").oracle("t_7_abc123", "")["answer"], "ANSWER: C")
        self.assertEqual(self.load("telemath-lite").oracle("1", "")["answer"], "\\boxed{0.5}")
        self.assertEqual(self.load("telelogs-lite").oracle("1", "")["answer"], "\\boxed{C3}")
        self.assertEqual(self.load("3gpp_tsg-lite").oracle("1", "")["answer"], '{"WORKING GROUP": "SA5"}')

    def test_the_original_reaches_the_model_through_the_units_relay_only(self):
        driver = self.load("teleqna-lite")
        with self.assertRaisesRegex(ValueError, "no model setup"):
            driver.answer("01", "", "model", "m", "http://relay/u1/v1", "word")
        out = driver.answer("01", "", "original", "org/m-1", "http://relay/u1/v1", "word")
        self.assertEqual(out, {"answer": "ANSWER: B", "model": "org/m-1"})
        self.assertEqual(self.up.models[-1], ("openai-api/nullius/org/m-1",
                                              {"base_url": "http://relay/u1/v1", "api_key": "word", "memoize": False}))
        self.assertEqual(self.up.evals[-1]["config"], {})                    # provider default: no temperature sent
        warm = self.load("teleqna-lite", temperature="0.5")
        warm.answer("01", "", "original", "m", "http://relay/u2/v1", "word")
        self.assertEqual(self.up.evals[-1]["config"], {"temperature": 0.5})

    def test_a_file_that_is_not_its_pin_is_fetched_again_and_refused_if_still_wrong(self):
        (self.snap / "test_telemath.json").write_bytes(b"changed upstream")
        fetched = []

        def wrong(repo, name, repo_type, revision, local_dir):
            fetched.append((repo, name, repo_type, revision))
            Path(local_dir, name).write_bytes(b"still not it")

        with self.assertRaisesRegex(ValueError, "test_telemath.json is not the file its sha256 names"):
            self.load("telemath-lite", fetch=wrong)
        self.assertEqual(fetched, [("org/lite", "test_telemath.json", "dataset", REV)])
        self.assertFalse((self.snap / "test_telemath.json").exists())

        def right(repo, name, repo_type, revision, local_dir):
            Path(local_dir, name).write_bytes(name.encode())

        self.assertEqual(self.load("telemath-lite", fetch=right).ROOT, str(self.snap))


class BundledDataSource(unittest.TestCase):
    def test_every_dataset_has_a_pinned_file_and_its_card(self):
        standard = json.loads((FOLDER / "standard.json").read_text())
        source = json.loads((FOLDER / "data-source.json").read_text())
        ids = [d["id"] for d in standard["datasets"]]
        self.assertEqual(sorted(source["datasets"]), sorted(ids))
        for key, d in source["datasets"].items():
            with self.subTest(key):
                self.assertEqual(d["repository"], "GSMA/ot-lite" if key.endswith("-lite") else "GSMA/ot-full")
                self.assertRegex(d["revision"], r"^[0-9a-f]{40}$")
                self.assertRegex(d["sha256"], r"^[0-9a-f]{64}$")
                self.assertIn(key.rsplit("-", 1)[0], d["file"])
                self.assertRegex(source["cards"][d["repository"]], r"^[0-9a-f]{64}$")
        self.assertEqual(len({(d["repository"], d["revision"]) for d in source["datasets"].values()}), 2)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_]+-(lite|full)", i) for i in ids))


if __name__ == "__main__":
    unittest.main()
