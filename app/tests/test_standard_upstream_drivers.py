"""A benchmark's driver runs the upstream's own code, in an environment of
its own: ``driver.install`` is checked as an agent's ``install`` is, installed
once per digest under ``drivers/``, never handed a secret, and filled into
``{install}`` in the driver's command wherever a driver starts.

Then, for the catalogue's drivers that call their upstream (GSM8K, HumanEval,
LoCoMo, LongMemEval), the driver's verdict is the upstream's own for the same
inputs: the upstream called directly, in the same environment, beside the
driver; and a reader asked by the upstream's own client call sends what that
call sends, retries included, to a fake model on loopback. Those checks need
the upstream and the driver's environment installed under ``STANDARD_DIR``
(``cli prove KEY`` installs both) and are skipped without them; nothing here
reaches the network."""
from __future__ import annotations

import gzip
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from nullius.standard import container, definitions, install, jsonl, run
from tests import docker_tier

BENCH = {
    "schema": "nullius.benchmark/v1", "key": "b", "label": "B", "summary": "s", "family": "agent",
    "needs": ["agent.task/v1"], "upstream": {"license": "MIT"},
    "driver": {"command": ["python3", "{here}/driver.py"]},
    "datasets": [{"id": "d", "units": 1}], "units": {"noun": "task", "nouns": "tasks"},
    "score": {"kind": "pass", "says": "s"},
}


def bench(**driver) -> dict:
    return dict(json.loads(json.dumps(BENCH)), driver=dict(BENCH["driver"], **driver))


class Schema(unittest.TestCase):
    def test_a_driver_install_is_checked_as_an_install_is(self):
        b = bench(command=["{install}/.venv/bin/python", "{here}/driver.py"],
                  install={"python": "3.12", "pip": ["openai==1.35.1"],
                           "indexes": [{"url": "https://download.pytorch.org/whl/cpu", "packages": ["torch==2.5.1"]}]})
        self.assertEqual(definitions.problems(b, "benchmark"), [])
        bad = definitions.problems(bench(install={"pip": ["openai"]}), "benchmark")
        self.assertIn("driver.install.pip: 'openai' is not name==version", bad)
        bad = definitions.problems(bench(install={"project": "."}), "benchmark")
        self.assertTrue(any(p.startswith("driver.install.project: needs upstream.repository") for p in bad), bad)

    def test_install_in_the_command_needs_a_driver_install(self):
        bad = definitions.problems(bench(command=["{install}/.venv/bin/python", "{here}/driver.py"]), "benchmark")
        self.assertIn("driver.command: {install} names driver.install, which is not set", bad)
        bad = definitions.problems(bench(command=["{prompt}"]), "benchmark")
        self.assertTrue(any("{prompt} is not a slot" in p for p in bad), bad)

    def test_a_version_may_change_the_driver_install(self):
        d = json.loads(json.dumps(BENCH))
        del d["driver"]
        d["upstream"] = {"license": "MIT"}
        d["default"] = "1"
        d["versions"] = {"1": {"driver": {"command": ["python3", "{here}/driver.py"]}},
                         "2": {"driver": {"command": ["{install}/.venv/bin/python", "{here}/driver.py"],
                                          "install": {"pip": ["numpy==2.1.3"]}}}}
        self.assertEqual(definitions.problems(d, "benchmark"), [])
        one, two = definitions.resolve(d, "1"), definitions.resolve(d, "2")
        self.assertIsNone(install.driver_dir(one))
        self.assertIn(os.sep + os.path.join("drivers", "b") + os.sep, install.driver_dir(two))


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_driver_without_install_runs_on_what_the_machine_has(self):
        b = dict(bench(), _here="/defs/b")
        self.assertEqual(install.driver_argv(b, self.tmp), (["python3", "/defs/b/driver.py"], {}))

    def test_install_is_filled_with_its_folder_once_installed(self):
        b = dict(bench(command=["{install}/.venv/bin/python", "{here}/driver.py"], install={"pip": ["numpy==2.1.3"]}), _here="/defs/b")
        where = install.driver_dir(b, self.tmp)
        self.assertTrue(where.startswith(os.path.join(self.tmp, "drivers", "b") + os.sep))
        os.makedirs(where)
        open(os.path.join(where, install.MARK), "w").close()          # installed: nothing is run
        with mock.patch.object(install, "_run", side_effect=AssertionError("installed again")):
            self.assertEqual(install.driver_argv(b, self.tmp), ([where + "/.venv/bin/python", "/defs/b/driver.py"], {}))
        other = dict(b, driver=dict(b["driver"], install={"pip": ["numpy==2.1.2"]}))
        self.assertNotEqual(install.driver_dir(other, self.tmp), where)          # a re-pin installs beside it

    def test_the_installer_is_never_handed_a_secret(self):
        b = dict(bench(command=["{install}/.venv/bin/python", "{here}/driver.py"], install={"pip": ["numpy==2.1.3"]}), _here=self.tmp)
        seen = []

        def fake(cmd, **kw):
            seen.append(kw.get("env"))
            return subprocess.CompletedProcess(cmd, 0)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-secret", "HOME": os.environ.get("HOME", "/")}), \
                mock.patch.object(subprocess, "run", side_effect=fake):
            install.driver_packages(b, self.tmp)
        self.assertTrue(seen)
        self.assertTrue(all("OPENAI_API_KEY" not in e for e in seen))


# -- the catalogue's drivers against their upstream ---------------------------

def _installed(key: str):
    """The benchmark, its upstream and its driver's environment, when both are
    installed under STANDARD_DIR; None otherwise."""
    b = definitions.get("benchmark", key)
    up, drv = install.upstream_dir(b), install.driver_dir(b)
    if not (install.ready(up) and install.ready(drv)):
        return None
    return b, up, drv


def _python(drv: str) -> str:
    return os.path.join(drv, ".venv", "bin", "python")


def _upstream_says(drv: str, up: str, code: str, args) -> object:
    """``code`` run by the driver's interpreter (the machine's python3 for a
    driver without an environment of its own) with the upstream importable,
    ``ARGS`` its input: what it prints, as JSON - the upstream called directly."""
    out = subprocess.run([_python(drv) if drv else "python3", "-c", code, json.dumps(args)], capture_output=True, text=True, timeout=300,
                         env=dict(run.base_env(tempfile.gettempdir()), NULLIUS_UPSTREAM=up))
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _driver(test, b, up, data, **more) -> jsonl.Proc:
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    cmd, extra = install.driver_argv(b)
    env = run.base_env(tmp, {**extra, "NULLIUS_UPSTREAM": up, "NULLIUS_DATASET": data, "PYTHONPATH": run.APP, **more})
    p = jsonl.Proc(cmd, b["_here"], env, os.path.join(tmp, "driver.log")).start()
    test.addCleanup(p.close)
    return p


class Gsm8k(unittest.TestCase):
    ANSWERS = ["so #### 18", "#### 1,234", "#### -3.50", "no marker 18", "#### 18 and #### 19", "", "####18"]

    def test_the_verdict_is_the_upstreams_is_correct(self):
        got = _installed("gsm8k")
        if not got:
            self.skipTest("GSM8K's upstream and driver environment are not installed (cli prove gsm8k)")
        b, up, drv = got
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        data = os.path.join(tmp, "test.jsonl")
        problems = [{"question": "q", "answer": "work\n#### 18"}, {"question": "q", "answer": "#### 1,234"},
                    {"question": "q", "answer": "#### -3.50"}]
        with open(data, "w") as f:
            f.writelines(json.dumps(p) + "\n" for p in problems)
        p = _driver(self, b, up, data)
        said = [[p.request("score", unit=f"{n:04d}", dir=tmp, transcript=[], answer=a)["passed"] for a in self.ANSWERS]
                for n in range(1, len(problems) + 1)]
        theirs = _upstream_says(drv, up, (
            "import json, os, sys\nsys.path.insert(0, os.path.join(os.environ['NULLIUS_UPSTREAM'], 'grade_school_math'))\n"
            "import dataset\nps, ans = json.loads(sys.argv[1])\n"
            "print(json.dumps([[dataset.is_correct(a, p) for a in ans] for p in ps]))"), [problems, self.ANSWERS])
        self.assertEqual(said, theirs)
        self.assertTrue(said[0][0] and said[1][1] and not said[0][3])


class HumanEvalNotTested(unittest.TestCase):
    """The workspace files solution.py imports are named beside the verdict, which stays the check's on
    solution.py alone. A stand-in of human-eval's three modules stands for the upstream, so this runs
    without the clone: what is tested is the driver's reading, not the upstream's check."""

    STAND_IN = {
        "__init__.py": "",
        "data.py": "import json\ndef read_problems(path):\n    return {p['task_id']: p for p in map(json.loads, open(path))}\n",
        "execution.py": ("def check_correctness(problem, completion, timeout):\n"
                         "    return {'task_id': problem['task_id'], 'passed': False, 'result': 'failed: the stand-in', 'completion_id': None}\n"),
        "evaluation.py": "def evaluate_functional_correctness(sample_file, k=[1], n_workers=4, timeout=3.0, problem_file=''):\n    pass\n",
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        up = os.path.join(self.tmp, "upstream")
        os.makedirs(os.path.join(up, "human_eval"))
        for name, text in self.STAND_IN.items():
            with open(os.path.join(up, "human_eval", name), "w") as f:
                f.write(text)
        data = os.path.join(self.tmp, "problems.jsonl")
        with open(data, "w") as f:
            f.write(json.dumps({"task_id": "T/0", "prompt": "def f():\n", "entry_point": "f", "canonical_solution": "    return 1\n",
                                "test": ""}) + "\n")
        self.p = _driver(self, definitions.get("benchmark", "humaneval"), up, data)
        self.ws = os.path.join(self.tmp, "workspace")
        os.makedirs(os.path.join(self.ws, "pkg"))
        for name in ("helpers.py", "rel.py"):
            open(os.path.join(self.ws, name), "w").close()

    def score(self, solution):
        with open(os.path.join(self.ws, "solution.py"), "w") as f:
            f.write(solution)
        return self.p.request("score", unit="T/0", dir=self.ws, transcript=[], answer="")

    def test_the_files_solution_py_imports_are_named_and_the_verdict_is_the_checks(self):
        got = self.score("import os, helpers\nfrom pkg.sub import g\nfrom . import rel\nfrom .solution import f\n"
                         "def f():\n    return helpers.one()\n")
        self.assertEqual((got["reward"], got["detail"]["result"]), (0.0, "failed: the stand-in"))      # the check's own
        self.assertEqual(got["detail"]["not_tested"], ["helpers.py", "pkg/", "rel.py"])           # the standard library is no file here
        self.assertNotIn("not_tested", self.score("import math\ndef f():\n    return 1\n")["detail"])   # the standard library only
        self.assertNotIn("not_tested", self.score("import helpers\ndef f(:\n")["detail"])         # no syntax, no names


class HumanEval(unittest.TestCase):
    """The verdict is the upstream's check_correctness on the completion the
    driver derives from solution.py: the text after the prompt, or the whole
    file when the agent changed the prompt's part."""

    def setUp(self):
        self.b = definitions.get("benchmark", "humaneval")
        self.up = install.upstream_dir(self.b)
        if not install.ready(self.up):
            self.skipTest("HumanEval's upstream is not cloned (cli prove humaneval)")
        self.data = os.path.join(self.up, self.b["datasets"][0]["path"])
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def theirs(self, cases):
        """check_correctness itself, on (task id, completion) pairs, at 3 s."""
        return _upstream_says(None, self.up, (
            "import json, os, sys\nsys.path.insert(0, os.environ['NULLIUS_UPSTREAM'])\n"
            "from human_eval.data import read_problems\nfrom human_eval.execution import check_correctness\n"
            "data, cases = json.loads(sys.argv[1])\nps = read_problems(data)\n"
            "print(json.dumps([check_correctness(ps[t], c, 3.0)['result'] for t, c in cases]))"), [self.data, cases])

    def test_the_verdict_is_the_upstreams_check_correctness(self):
        p = _driver(self, self.b, self.up, self.data)
        ids = p.request("units", dataset="humaneval")["units"][:3]
        probs = {}
        with gzip.open(self.data, "rt") as f:
            for line in f:
                q = json.loads(line)
                probs[q["task_id"]] = q
        files, cases = [], []
        for t in ids:
            q = probs[t]
            files += [q["prompt"],                                            # as setup wrote it: no body
                      q["prompt"] + q["canonical_solution"],                    # a body after the prompt
                      "import math\n" + q["prompt"] + q["canonical_solution"],  # the prompt's part changed: the whole file
                      q["prompt"] + "    return None\n"]                       # a wrong body
            cases += [(t, ""), (t, q["canonical_solution"]), (t, "\nimport math\n" + q["prompt"] + q["canonical_solution"]),
                      (t, "    return None\n")]
        said = []
        for (t, _), text in zip(cases, files):
            p.request("setup", unit=t, dir=self.tmp)
            with open(os.path.join(self.tmp, "solution.py"), "w") as f:
                f.write(text)
            said.append(p.request("score", unit=t, dir=self.tmp, transcript=[], answer="")["detail"]["result"])
        self.assertEqual(said, self.theirs(cases))
        self.assertEqual(said[1:3], ["passed", "passed"])
        self.assertNotEqual(said[0], "passed")

    def test_on_docker_the_check_runs_in_a_throwaway_container_and_the_agents_code_never_here(self):
        bin_dir, log = os.path.join(self.tmp, "bin"), os.path.join(self.tmp, "docker-argv.json")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:          # records how it was asked, answers as the container would
            f.write(f"#!{sys.executable}\nimport json, sys\njson.dump(sys.argv[1:], open({log!r}, 'w'))\n"
                    "print(json.dumps({'task_id': 't', 'passed': True, 'result': 'passed', 'completion_id': None}))\n")
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        p = _driver(self, self.b, self.up, self.data, **{container.CHECK_IMAGE: "nullius/base:test",
                                                          "PATH": bin_dir + os.pathsep + os.environ["PATH"]})
        t = p.request("units", dataset="humaneval")["units"][0]
        ws = os.path.join(self.tmp, "ws"); os.makedirs(ws)
        p.request("setup", unit=t, dir=ws)
        marker = os.path.join(self.tmp, "ran-on-the-host")
        with open(os.path.join(ws, "solution.py"), "w") as f:
            f.write(f"open({marker!r}, 'w').write('x')\n")
        self.assertEqual(p.request("score", unit=t, dir=ws, transcript=[], answer="")["detail"]["result"], "passed")
        self.assertFalse(os.path.exists(marker))
        argv = json.load(open(log))
        self.assertEqual((argv[:2], argv[argv.index("--network") + 1]), (["run", "--rm"], "none"))
        self.assertIn("nullius/base:test", argv)
        self.assertEqual(os.listdir(ws), ["solution.py"])                 # the check's folder is gone

    @unittest.skipIf(docker_tier.why_not(), docker_tier.why_not() or "")
    def test_in_the_units_image_the_verdict_is_the_upstreams(self):
        p = _driver(self, self.b, self.up, self.data, **{container.CHECK_IMAGE: container.base_tag()})
        t = p.request("units", dataset="humaneval")["units"][0]
        said = []
        for body in ("oracle", "    return None\n"):
            ws = tempfile.mkdtemp(dir=self.tmp)
            p.request("setup", unit=t, dir=ws)
            if body == "oracle":
                p.request("oracle", unit=t, dir=ws)
            else:
                with open(os.path.join(ws, "solution.py"), "a") as f:
                    f.write(body)
            said.append(p.request("score", unit=t, dir=ws, transcript=[], answer="", timeout=300)["passed"])
        self.assertEqual(said, [True, False])

    def test_the_controls(self):
        p = _driver(self, self.b, self.up, self.data)
        t = p.request("units", dataset="humaneval")["units"][0]
        p.request("setup", unit=t, dir=self.tmp)
        nop = p.request("score", unit=t, dir=self.tmp, transcript=[], answer="")
        p.request("oracle", unit=t, dir=self.tmp)
        ref = p.request("score", unit=t, dir=self.tmp, transcript=[], answer="")
        self.assertEqual((nop["passed"], ref["passed"]), (False, True))
        self.assertEqual((ref["detail"]["completion"], ref["detail"]["timeout"]), ("after the prompt", 3.0))
        os.remove(os.path.join(self.tmp, "solution.py"))
        self.assertEqual(p.request("score", unit=t, dir=self.tmp, transcript=[], answer="")["detail"], {"why": "no solution.py"})


class Locomo(unittest.TestCase):
    QA = [{"question": "Would she enjoy a park?", "answer": "Likely yes; she likes the outdoors", "category": 3},
          {"question": "Which pets?", "answer": "a cat, two dogs", "category": 1},
          {"question": "Which city?", "answer": "Paris; the capital", "category": 4},
          {"question": "When did she run?", "answer": "7 May 2023", "category": 2},
          {"question": "What year?", "answer": 2022, "category": 2}]
    ANSWERS = ["Likely yes", "She likes running outdoors", "two dogs, a cat", "cats", "Paris", "on 7 May, 2023", "2022", "", "The runs"]

    def setUp(self):
        got = _installed("locomo")
        if not got:
            self.skipTest("LoCoMo's upstream and driver environment are not installed (cli prove locomo)")
        self.b, self.up, self.drv = got
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "locomo.json")
        with open(self.data, "w") as f:
            json.dump([{"sample_id": "s", "conversation": {}, "qa": self.QA}], f)

    def test_the_score_is_the_upstreams_eval_question_answering(self):
        p = _driver(self, self.b, self.up, self.data)
        units = [f"s:{i}" for i in range(len(self.QA))]
        said = [[p.request("score", unit=u, dir=self.tmp, transcript=[], answer=a)["reward"] for a in self.ANSWERS] for u in units]
        theirs = _upstream_says(self.drv, self.up, (
            "import json, os, sys, contextlib\nsys.path.insert(0, os.environ['NULLIUS_UPSTREAM'])\n"
            "with contextlib.redirect_stdout(sys.stderr):\n    from task_eval.evaluation import eval_question_answering\n"
            "qa, ans = json.loads(sys.argv[1])\nout = []\n"
            "with contextlib.redirect_stdout(sys.stderr):\n"
            "    for q in qa:\n        out.append([float(eval_question_answering([dict(q, prediction=a)], 'prediction')[0][0]) for a in ans])\n"
            "print(json.dumps(out))"), [self.QA, self.ANSWERS])
        self.assertEqual(said, theirs)
        self.assertEqual(said[1][2], 1.0)                 # category 1, by its comma-separated parts
        self.assertGreater(said[0][1], 0.0)               # "running" meets "likes ... outdoors": stemmed, as LoCoMo's F1 is

    def test_the_reference_is_the_gold_the_scorer_reads(self):
        """For category 3 the answer field's part before ";" (the authors'
        reasoning follows it): the reference scores 1 where the whole field would not."""
        p = _driver(self, self.b, self.up, self.data)
        said = {u: p.request("oracle", unit=u, dir=self.tmp)["answer"] for u in ("s:0", "s:1", "s:2")}
        self.assertEqual(said, {"s:0": "Likely yes", "s:1": "a cat, two dogs", "s:2": "Paris; the capital"})
        for u, a in said.items():
            self.assertEqual(p.request("score", unit=u, dir=self.tmp, transcript=[], answer=a)["reward"], 1.0, u)
        self.assertLess(p.request("score", unit="s:0", dir=self.tmp, transcript=[], answer=self.QA[0]["answer"])["reward"], 1.0)

    def test_the_reader_prompt_is_the_upstreams(self):
        seen, base = _fake_model(self, " May 2023 ")
        p = _driver(self, self.b, self.up, self.data)
        said = [p.request("produce", unit=u, dir=self.tmp, recalled=[{"id": u, "memory": "DATE: 1 May\nA: hi"}], model="gpt-4o-mini",
                          base_url=base, key="k")["answer"] for u in ("s:2", "s:3")]
        self.assertEqual(said, ["May 2023", "May 2023"])          # stripped, as get_gpt_answers keeps it
        theirs = _upstream_says(self.drv, self.up, (
            "import json, os, sys, contextlib\nsys.path.insert(0, os.environ['NULLIUS_UPSTREAM'])\n"
            "with contextlib.redirect_stdout(sys.stderr):\n    from task_eval import gpt_utils\n"
            "print(json.dumps(gpt_utils.QA_PROMPT))"), [])
        self.assertEqual(seen[0]["messages"][0]["content"], "DATE: 1 May\nA: hi\n\n" + theirs.format("Which city?"))
        self.assertEqual(seen[1]["messages"][0]["content"], "DATE: 1 May\nA: hi\n\n" + theirs.format(
            "When did she run? Use DATE of CONVERSATION to answer with an approximate date."))
        # run_chatgpt's own request, at the settings get_gpt_answers asks one question with in its RAG mode
        self.assertEqual({(s["model"], s["temperature"], s["max_tokens"], s["n"]) for s in seen}, {("gpt-4o-mini", 0, 32, 1)})
        self.assertEqual(seen[0]["messages"][0]["role"], "user")

    def test_the_reader_is_run_chatgpt_as_published(self):
        """Its role follows the model's name, a name it does not know fails
        the question, and an API error is retried (here, once)."""
        seen, base = _fake_model(self, "Paris", fail_first=1)
        p = _driver(self, self.b, self.up, self.data)
        ask = dict(unit="s:2", dir=self.tmp, recalled=[{"id": "s:2", "memory": "M"}], base_url=base, key="k")
        self.assertEqual(p.request("produce", model="gpt-3.5-turbo-0125", **ask)["answer"], "Paris")
        self.assertEqual(len(seen), 2)                           # the 500 asked again, as run_chatgpt does
        self.assertEqual([(s["model"], s["messages"][0]["role"]) for s in seen], [("gpt-3.5-turbo", "system")] * 2)
        with self.assertRaisesRegex(jsonl.Failed, "ValueError"):
            p.request("produce", model="claude-sonnet", **ask)
        self.assertEqual(len(seen), 2)                           # nothing asked


def _fake_model(test, reply: str, fail_first: int = 0, status: int = 500):
    """An OpenAI-compatible endpoint on loopback that answers ``reply`` and
    keeps every request body: (the bodies, its base_url). ``fail_first``:
    that many requests are answered ``status`` first."""
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if len(seen) <= fail_first:
                body = json.dumps({"error": {"message": "try again", "type": "server_error"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": seen[-1]["model"],
                               "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": reply}}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    return seen, f"http://127.0.0.1:{srv.server_address[1]}/v1"


class LongMemEval(unittest.TestCase):
    Q = [{"question_id": "q1", "question_type": "temporal-reasoning", "question": "How many days?", "answer": "18",
          "question_date": "2023/05/30", "haystack_session_ids": [], "haystack_sessions": [], "haystack_dates": []},
         {"question_id": "q2_abs", "question_type": "multi-session", "question": "Which car?", "answer": "Never said.",
          "question_date": "2023/05/30", "haystack_session_ids": [], "haystack_sessions": [], "haystack_dates": []}]

    def setUp(self):
        got = _installed("longmemeval")
        if not got:
            self.skipTest("LongMemEval's upstream and driver environment are not installed (cli prove longmemeval)")
        self.b, self.up, self.drv = got
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "lme.json")
        with open(self.data, "w") as f:
            json.dump(self.Q, f)

    def test_the_judge_is_the_upstreams_prompt_and_call(self):
        seen, base = _fake_model(self, "Yes.")
        p = _driver(self, self.b, self.up, self.data)
        judge = {"model": "gpt-4o-2024-08-06", "base_url": base, "key": "k"}
        said = [p.request("score", unit=q["question_id"], dir=self.tmp, transcript=[], answer="19 days", judge=judge) for q in self.Q]
        self.assertEqual([s["passed"] for s in said], [True, True])
        theirs = _upstream_says(self.drv, self.up, (
            "import json, os, sys\nsys.path.insert(0, os.path.join(os.environ['NULLIUS_UPSTREAM'], 'src'))\n"
            "from evaluation.evaluate_qa import get_anscheck_prompt\nqs = json.loads(sys.argv[1])\n"
            "print(json.dumps([get_anscheck_prompt(q['question_type'], q['question'], q['answer'], '19 days', "
            "abstention='_abs' in q['question_id']) for q in qs]))"), self.Q)
        self.assertEqual([s["messages"] for s in seen], [[{"role": "user", "content": t}] for t in theirs])
        self.assertEqual({(s["model"], s["n"], s["temperature"], s["max_tokens"]) for s in seen}, {("gpt-4o-2024-08-06", 1, 0, 10)})

    def test_the_reader_prompt_is_the_upstreams_template(self):
        seen, base = _fake_model(self, " 18 days ")
        p = _driver(self, self.b, self.up, self.data)
        said = p.request("produce", unit="q1", dir=self.tmp, recalled=[{"id": "q1", "memory": "MEM"}], model="m", base_url=base, key="k")
        self.assertEqual(said["answer"], "18 days")
        prompt = seen[0]["messages"][0]["content"]
        self.assertTrue(prompt.startswith("I will give you several history chats between you and a user."), prompt)
        self.assertTrue(prompt.endswith("History Chats:\n\nMEM\n\nCurrent Date: 2023/05/30\nQuestion: How many days?\nAnswer:"), prompt)
        # run_generation's main asks with these, gen_length 500 without chain of thought
        self.assertEqual({(s["model"], s["n"], s["temperature"], s["max_tokens"]) for s in seen}, {("m", 1, 0, 500)})

    def test_the_reader_retries_a_rate_limit_through_the_upstreams_backoff(self):
        """chat_completions_with_backoff, run_generation's own: a 429 the
        client's own retries give up on is asked again after 5 s."""
        seen, base = _fake_model(self, "18 days", fail_first=3, status=429)
        p = _driver(self, self.b, self.up, self.data)
        said = p.request("produce", unit="q1", dir=self.tmp, recalled=[{"id": "q1", "memory": "MEM"}], model="m", base_url=base, key="k",
                         timeout=120)
        self.assertEqual(said["answer"], "18 days")
        self.assertEqual(len(seen), 4)                 # the client's 2 retries, then the upstream's backoff


if __name__ == "__main__":
    unittest.main()
