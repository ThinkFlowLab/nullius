"""LoCoMo's retrieval in its setups: the driver runs the upstream's own
scripts with the flags the paper's scripts pass - evaluate_qa.py's --use-rag,
--rag-mode, --retriever, --top-k and --emb-dir, get_facts.py or
get_session_summaries.py first for a database they write - and scores the
predictions under the names evaluate_qa.py files them by. The upstream is a
stand-in here: scripts that record how they were called and write what the
real ones write, so nothing is installed and no GPU or model is needed. The
real retrievers are proven on a machine with a GPU, by a run."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from nullius.standard import definitions

BENCH = definitions.get("benchmark", "locomo")
DRIVER = os.path.join(BENCH["_here"], "driver.py")

CONV = {"sample_id": "conv-1", "conversation": {"speaker_a": "A", "speaker_b": "B", "session_1_date_time": "1 May",
                                                 "session_1": [{"speaker": "A", "text": "hi", "dia_id": "D1:1"}]},
        "observation": {"session_1_observation": {"A": [["A said hi", "D1:1"]]}},
        "session_summary": {"session_1_summary": "A said hi."},
        "qa": [{"question": "What did A say?", "answer": "hi", "category": 4, "evidence": ["D1:1"]},
               {"question": "Adversarial", "adversarial_answer": "x", "category": 5}]}

# The stand-in upstream: evaluate_qa.py writes one prediction a question under the key the real one uses, with
# its F1 and, with retrieval, its recall; the database scripts write the pickle name evaluate_qa.py reads.
EVALUATE_QA = '''
import argparse, json, os, sys
p = argparse.ArgumentParser()
for f in ("--out-file", "--model", "--data-file", "--rag-mode", "--emb-dir", "--retriever"):
    p.add_argument(f, default="")
p.add_argument("--batch-size", type=int, default=1); p.add_argument("--top-k", type=int, default=5)
p.add_argument("--use-rag", action="store_true")
a = p.parse_args()
with open(os.path.join(os.path.dirname(a.out_file), "calls.jsonl"), "a") as f:
    f.write(json.dumps({"script": "evaluate_qa.py", "argv": sys.argv[1:], "base": os.environ.get("OPENAI_API_BASE")}) + "\\n")
if a.use_rag and a.rag_mode != "dialog":
    want = os.path.join(a.emb_dir, "asked_%s_conv-1.pkl" % ("session_summary" if a.rag_mode == "summary" else a.rag_mode))
    assert os.path.exists(want), want
data = json.load(open(a.data_file))
key = a.model if not a.use_rag else "%s_%s_top_%s" % (a.model, a.rag_mode, a.top_k)
for d in data:
    for q in d["qa"]:
        q[key + "_prediction"] = "hi"
        q[key + "_f1"] = 1.0
        if a.use_rag:
            q[key + "_prediction_context"] = ["D1:1"]
            q[key + "_recall"] = 0.5
json.dump(data, open(a.out_file, "w"))
'''
BUILD = '''
import argparse, json, os, sys
p = argparse.ArgumentParser()
for f in ("--out-file", "--data-file", "--emb-dir", "--prompt-dir", "--retriever"):
    p.add_argument(f, default="")
p.add_argument("--use-date", action="store_true"); p.add_argument("--overwrite", action="store_true")
a = p.parse_args()
with open(os.path.join(a.emb_dir, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"script": os.path.basename(sys.argv[0]), "argv": sys.argv[1:]}) + "\\n")
open(a.out_file.replace(".json", "_conv-1.pkl"), "wb").write(b"db")
'''
GPT_UTILS = '''
QA_PROMPT = "{}"
def get_gpt_answers(in_data, out_data, prediction_key, args):
    for qa in in_data["qa"]:
        if qa['category'] == 2:
            q = qa['question'] + ' Use DATE of CONVERSATION to answer with an approximate date.'
def run_chatgpt(*a, **k):
    return ""
'''


class Driver:
    """The driver over the stand-in, one request at a time."""

    def __init__(self, root, options):
        self.root = root
        up = os.path.join(root, "up")
        os.makedirs(os.path.join(up, "task_eval"))
        os.makedirs(os.path.join(up, "prompt_examples"))
        files = {"task_eval/__init__.py": "", "task_eval/evaluation.py": "def eval_question_answering(*a): return [0.0], 0, []\n",
                 "task_eval/gpt_utils.py": GPT_UTILS, "task_eval/evaluate_qa.py": EVALUATE_QA,
                 "task_eval/get_facts.py": BUILD, "task_eval/get_session_summaries.py": BUILD, "openai.py": ""}
        for name, text in files.items():
            with open(os.path.join(up, name), "w") as f:
                f.write(textwrap.dedent(text))
        data = os.path.join(root, "locomo10.json")
        with open(data, "w") as f:
            json.dump([CONV], f)
        env = dict(os.environ, NULLIUS_UPSTREAM=up, NULLIUS_DATASET=data, PYTHONPATH=up,
                   **{"NULLIUS_OPTION_" + k.upper(): v for k, v in options.items()})
        self.p = subprocess.Popen([sys.executable, DRIVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)

    def ask(self, **req):
        self.p.stdin.write(json.dumps(req) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=30)
        self.p.stdout.close()


class Retrieval(unittest.TestCase):
    def run_unit(self, **options):
        with tempfile.TemporaryDirectory() as root:
            d = Driver(root, options)
            try:
                unit = os.path.join(root, "unit")
                os.makedirs(unit)
                got = d.ask(op="answer", unit="conv-1", dir=unit, setup="original", model="gpt-3.5-turbo",
                            base_url="http://relay/v1", key="placeholder")
                self.assertNotIn("error", got, got)
                score = d.ask(op="score", unit="conv-1", dir=unit, transcript=[{"asked": "gpt-3.5-turbo"}],
                              answer=got["answer"], setup="original")
            finally:
                d.close()
            with open(os.path.join(unit, "calls.jsonl")) as f:
                calls = [json.loads(x) for x in f]
            return got, score, calls

    def test_without_retrieval_the_whole_conversation_is_asked_in_batches(self):
        got, score, calls = self.run_unit()
        self.assertEqual([c["script"] for c in calls], ["evaluate_qa.py"])
        argv = calls[0]["argv"]
        self.assertNotIn("--use-rag", argv)
        self.assertEqual(argv[argv.index("--batch-size") + 1], "10")      # a gpt-3.5-turbo window's, as evaluate_gpts.sh
        self.assertEqual(calls[0]["base"], "http://relay/v1")
        self.assertEqual(json.loads(got["answer"]), {"0": "hi"})          # category 5 is not fed
        self.assertEqual(score["reward"], 1.0)
        self.assertNotIn("retrieval", score["detail"])

    def test_dialog_retrieval_runs_evaluate_qa_with_the_rag_flags_one_question_a_call(self):
        got, score, calls = self.run_unit(retrieval="dialog", retriever="dpr", top_k="25")
        self.assertEqual([c["script"] for c in calls], ["evaluate_qa.py"])  # dialog's database is evaluate_qa.py's own
        argv = calls[0]["argv"]
        for flag, value in (("--rag-mode", "dialog"), ("--retriever", "dpr"), ("--top-k", "25"), ("--batch-size", "1")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--use-rag", argv)
        self.assertEqual(json.loads(got["answer"]), {"0": "hi"})          # read under gpt-3.5-turbo_dialog_top_25
        self.assertEqual((score["reward"], score["detail"]["recall"], score["detail"]["top_k"]), (1.0, 0.5, 25))
        self.assertEqual(score["detail"]["per_question"][0]["retrieved"], ["D1:1"])

    def test_observation_and_summary_write_their_database_first_with_the_scripts_flags(self):
        for mode, script, flags in (("observation", "get_facts.py", ["--use-date", "--overwrite"]),
                                    ("summary", "get_session_summaries.py", ["--use-date"])):
            with self.subTest(mode=mode):
                _, score, calls = self.run_unit(retrieval=mode, top_k="2")
                self.assertEqual([c["script"] for c in calls], [script, "evaluate_qa.py"])
                build = calls[0]["argv"]
                self.assertEqual(build[build.index("--retriever") + 1], "dragon")    # the default, the paper's
                self.assertEqual([f for f in build if f in ("--use-date", "--overwrite")], flags)
                self.assertEqual(calls[1]["argv"][calls[1]["argv"].index("--rag-mode") + 1], mode)
                self.assertEqual(score["reward"], 1.0)

    def test_a_retrieval_needs_a_gpu_and_none_does_not(self):
        o = BENCH["options"]
        self.assertEqual(definitions.option_needs(BENCH, {"retrieval": "none"})["host"], [])
        for mode in ("dialog", "observation", "summary"):
            self.assertEqual(definitions.option_needs(BENCH, {"retrieval": mode})["host"], ["cuda_gpu"])
        self.assertEqual(BENCH["host"]["cuda_gpu"]["check"], ["nvidia-smi"])
        self.assertEqual({n for n in o if "original" in (o[n].get("setups") or [])}, {"retrieval", "retriever", "top_k"})
        for n in ("retriever", "top_k"):                                  # shown only with a retrieval
            self.assertEqual(o[n]["when"], {"retrieval": ["dialog", "observation", "summary"]})
        self.assertEqual(o["rag_mode"]["label"], "RAG mode")
        vals, bad = definitions.bench_options_of(BENCH, "locomo10", {"retrieval": "dialog"}, "yours")
        self.assertEqual((vals, bad), ({"rag_mode": "dialog"}, []))       # a memory's run never gets the setups' retrieval


class When(unittest.TestCase):
    def test_when_names_another_option_and_some_of_its_values(self):
        opts = {"mode": {"summary": "m", "values": ["a", "b"], "default": "a"},
                "k": {"summary": "k", "values": ["1", "2"], "default": "1", "when": {"mode": ["b"]}}}
        self.assertEqual(definitions._bench_options({"options": opts}), [])
        for bad in ({"mode": ["c"]}, {"nope": ["a"]}, {"k": ["1"]}, {"mode": []}, ["mode"]):
            with self.subTest(when=bad):
                o = {**opts, "k": {**opts["k"], "when": bad}}
                self.assertTrue(any(".when" in x for x in definitions._bench_options({"options": o})))


if __name__ == "__main__":
    unittest.main()
