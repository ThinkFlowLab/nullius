"""LongMemEval's setups and settings inventory: the paper's pipeline run as
run_generation.sh runs it (original: the paper's readers; model: any, asked
as gpt-4o), its arguments as options, every setting of every evaluation entry
point offered or recorded. The definition checks need nothing installed; the
driver is run against a fake upstream - run_generation.sh's tables, a
run_generation.py and evaluate_qa.py that record how they were called - so
what it passes is checked without a model; the checks against the real
scripts read the upstream under ``STANDARD_DIR`` and are skipped without it."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

from nullius.standard import definitions, install

BENCH = definitions.get("benchmark", "longmemeval")
DRIVER = os.path.join(BENCH["_here"], "driver.py")


def rows():
    with open(os.path.join(BENCH["_here"], definitions.SETTINGS_FILE)) as f:
        return json.load(f)


class Inventory(unittest.TestCase):
    def test_the_benchmark_and_its_inventory_are_valid(self):
        with open(os.path.join(BENCH["_here"], "standard.json")) as f:
            self.assertEqual(definitions.problems(json.load(f), "benchmark"), [])
        self.assertEqual(definitions.settings_problems([BENCH]), [])

    def test_every_entry_point_is_inventoried(self):
        entries = {r["entry"] for r in rows()}
        for e in ("src/generation/run_generation.sh", "src/generation/run_generation.py", "src/retrieval/run_retrieval.sh",
                  "src/retrieval/run_retrieval.py", "src/evaluation/evaluate_qa.py", "src/evaluation/print_qa_metrics.py",
                  "src/evaluation/print_retrieval_metrics.py", "src/index_expansion/temp_query_search_pruning.py",
                  "data/custom_history/sample_haystack_and_timestamp.py"):
            self.assertIn(e, entries)
        self.assertEqual(len([e for e in entries if e.startswith("src/index_expansion/batch_expansion_")]), 6)

    def test_what_is_offered_is_named(self):
        named = {r["nullius"] for r in rows()}
        for n, o in BENCH["options"].items():
            if "original" in (o.get("setups") or []):
                self.assertIn(f"option:{n}", named)
        for d in BENCH["datasets"]:
            self.assertIn(f"dataset:{d['id']}", named)
        for s in BENCH["setups"]:
            self.assertIn(f"setup:{s}", named)

    def test_the_setups_are_the_papers(self):
        o = BENCH["setups"]["original"]
        self.assertEqual(o["models"], ["gpt-4o", "gpt-4o-mini"])
        self.assertEqual(o["served_as"], {"gpt-4o": "gpt-4o-2024-08-06", "gpt-4o-mini": "gpt-4o-mini-2024-07-18"})
        self.assertEqual(BENCH["setups"]["model"]["runs_as"], "gpt-4o")
        defaults = {n: BENCH["options"][n]["default"] for n in
                    ("retriever", "topk", "reading", "separate_notes", "history_format", "useronly", "merge_key_expansion")}
        self.assertEqual(defaults, {"retriever": "full-history-session", "topk": "1000", "reading": "con", "separate_notes": "false",
                                    "history_format": "json", "useronly": "false", "merge_key_expansion": "none"})

    def test_the_memory_path_keeps_its_meaning(self):
        """Reading is shared; retrieval and separate notes belong to Original."""
        for setup in ("original", "model", "yours"):
            vals, bad = definitions.bench_options_of(BENCH, "longmemeval_s", {"reading": "direct"}, setup)
            self.assertEqual(bad, [])
            self.assertEqual(vals["reading"], "direct")
        self.assertNotIn("reading_method", BENCH["options"])
        vals, bad = definitions.bench_options_of(BENCH, "longmemeval_s", {}, "yours")
        self.assertEqual(bad, [])
        self.assertNotIn("retriever", vals)

    def test_the_dense_retrievers_need_a_gpu_and_gte_and_the_expansions_are_recorded_not_offered(self):
        by = {(r["entry"], r["setting"]): r["nullius"] for r in rows()}
        self.assertEqual(by[("src/retrieval/run_retrieval.sh", "$2 retriever flat-contriever, flat-stella")], "option:retriever")
        self.assertTrue(by[("src/retrieval/run_retrieval.sh", "$2 retriever flat-gte")].startswith("not offered - "))
        self.assertTrue(by[("src/retrieval/run_retrieval.sh", "$4 index_expansion")].startswith("not offered - "))
        r = BENCH["options"]["retriever"]
        for v in ("flat-contriever-session", "flat-contriever-turn", "flat-stella-session", "flat-stella-turn"):
            self.assertIn(v, r["values"])
            self.assertEqual(definitions.option_needs(BENCH, {"retriever": v})["host"], ["cuda_gpu"])
        self.assertEqual(definitions.option_needs(BENCH, {"retriever": "flat-bm25-session"})["host"], [])
        self.assertFalse([v for v in r["values"] if "gte" in v])


def _fake_upstream(root):
    """run_generation.sh's two tables as published, and scripts that record their arguments."""
    def w(p, s):
        os.makedirs(os.path.dirname(os.path.join(root, p)), exist_ok=True)
        with open(os.path.join(root, p), "w") as f:
            f.write(textwrap.dedent(s))
    w("src/generation/run_generation.sh", '''\
        model_zoo["gpt-4o"]="gpt-4o-2024-08-06"
        model_zoo["gpt-4o-mini"]="gpt-4o-mini-2024-07-18"
        if [[ $retriever_alias == "flat-bm25-turn" || $retriever_alias == "flat-contriever-turn" ]]; then
            retriever_type="flat-turn"
        elif [[ $retriever_alias == "flat-bm25-session" || $retriever_alias == "flat-contriever-session" ]]; then
            retriever_type="flat-session"
        elif [[ $retriever_alias == "full-history-turn" ]]; then
            retriever_type="orig-turn"
        elif [[ $retriever_alias == "full-history-session" ]]; then
            retriever_type="orig-session"
        elif [[ $retriever_alias == "no-retrieval" ]]; then
            retriever_type="no-retrieval"
        fi
        if [[ $reading_method == "direct" ]]; then
            reading_flags="--cot false"
        elif [[ $reading_method == "con" ]]; then
            reading_flags="--cot true"
        elif [[ $reading_method == "con-separate" ]]; then
            reading_flags="--cot true --con true"
        fi
        ''')
    w("src/generation/__init__.py", "")
    w("src/evaluation/__init__.py", "")
    w("src/openai.py", "class OpenAI:\n    def __init__(self, **kw): pass\n")
    w("src/generation/run_generation.py", '''\
        import json, sys
        def prepare_prompt(entry, retriever_type, merge_key_expansion_into_value, cot):
            if merge_key_expansion_into_value is None or merge_key_expansion_into_value == 'none':
                if cot:
                    answer_prompt_template = 'cot {} {} {}'
                else:
                    answer_prompt_template = 'direct {} {} {}'
        def main(args):
            gen_length = 500 if not args.cot else 800
        def chat_completions_with_backoff(client, **kw): pass
        if __name__ == '__main__':
            a = sys.argv[1:]
            out = a[a.index('--out_dir') + 1]
            q = json.load(open(a[a.index('--in_file') + 1]))[0]
            with open(out + '/x.json_testlog_top', 'w') as f:
                print(json.dumps({'question_id': q['question_id'], 'hypothesis': json.dumps(a)}), file=f)
        ''')
    w("src/retrieval/run_retrieval.py", '''\
        import json, os, sys
        a = sys.argv[1:]
        q = json.load(open(a[a.index('--in_file') + 1]))[0]
        q['retrieval'] = {'argv': a, 'hf_home': os.environ.get('HF_HOME')}
        open(os.path.join(a[a.index('--out_dir') + 1], 'x_retrievallog_none'), 'w').write(json.dumps(q) + '\\n')
        ''')
    w("src/evaluation/evaluate_qa.py", '''\
        import json, os, sys
        def get_anscheck_prompt(*a, **k): return ''
        def chat_completions_with_backoff(client, **kw): pass
        if __name__ == '__main__':
            hyp = [json.loads(l) for l in open(sys.argv[2])][0]
            hyp['autoeval_label'] = {'model': 'gpt-4o-2024-08-06', 'label': True,
                                     'argv': sys.argv[1:], 'base': os.environ.get('OPENAI_BASE_URL')}
            open(sys.argv[2] + '.eval-results-' + sys.argv[1], 'w').write(json.dumps(hyp) + '\\n')
        ''')


class Driver(unittest.TestCase):
    def run_driver(self, reqs, options, keep=None, questions=None):
        with tempfile.TemporaryDirectory() as tmp:
            t = keep or tmp
            up, ws = os.path.join(t, "up"), os.path.join(t, "ws")
            os.makedirs(ws)
            _fake_upstream(up)
            data = os.path.join(t, "data.json")
            with open(data, "w") as f:
                json.dump([{"question_id": q, "question_type": t, "question": "Q?", "answer": "A", "question_date": "",
                            "haystack_session_ids": [], "haystack_dates": [], "haystack_sessions": []}
                           for q, t in (questions or [("q1", "multi-session")])], f)
            env = dict(os.environ, NULLIUS_UPSTREAM=up, NULLIUS_DATASET=data,
                       **{"NULLIUS_OPTION_" + k.upper(): v for k, v in options.items()})
            lines = "".join(json.dumps(r if r["op"] == "units" else dict(r, dir=ws)) + "\n" for r in reqs)
            out = subprocess.run([sys.executable, DRIVER], input=lines, capture_output=True, text=True, env=env, timeout=60)
            return [json.loads(ln) for ln in out.stdout.splitlines()]

    def test_the_original_runs_run_generation_as_the_script_does_and_scores_with_evaluate_qa(self):
        judge = {"model": "gpt-4o-2024-08-06", "base_url": "http://judge/v1", "key": "k"}
        got = self.run_driver([
            {"op": "answer", "unit": "q1", "setup": "original", "model": "gpt-4o-mini", "base_url": "http://relay/v1",
             "key": "placeholder", "judge": judge},
            {"op": "score", "unit": "q1", "transcript": [], "answer": "", "judge": judge, "setup": "original"}], {})
        self.assertNotIn("error", got[0], got[0])
        argv = json.loads(got[0]["answer"])
        pairs = dict(zip(argv[::2], argv[1::2]))
        self.assertEqual(pairs["--model_name"], "gpt-4o-mini-2024-07-18")
        self.assertEqual(pairs["--model_alias"], "gpt-4o-mini")
        self.assertEqual(pairs["--retriever_type"], "orig-session")
        self.assertEqual(pairs["--topk_context"], "1000")
        self.assertEqual(pairs["--openai_base_url"], "http://relay/v1")
        self.assertEqual(pairs["--openai_key"], "placeholder")
        self.assertEqual(argv[-2:], ["--cot", "true"])
        self.assertTrue(got[1]["passed"], got[1])
        self.assertEqual(got[1]["detail"]["asked_as"], "gpt-4o-2024-08-06")

    def test_direct_reading_reaches_original_even_with_separate_notes_selected(self):
        got = self.run_driver([{"op": "answer", "unit": "q1", "setup": "original", "model": "gpt-4o",
                                "base_url": "u", "key": "k"}],
                              {"reading": "direct", "separate_notes": "true"})
        argv = json.loads(got[0]["answer"])
        self.assertEqual(argv[-2:], ["--cot", "false"])
        self.assertNotIn("--con", argv)

    def test_the_options_reach_the_scripts_arguments(self):
        got = self.run_driver([{"op": "answer", "unit": "q1", "setup": "model", "model": "gpt-4o", "base_url": "u",
                                "key": "k"}],
                              {"retriever": "full-history-turn", "reading": "con", "separate_notes": "true", "topk": "50",
                               "history_format": "nl", "useronly": "true", "merge_key_expansion": "replace"})
        argv = json.loads(got[0]["answer"])
        pairs = dict(zip(argv[::2], argv[1::2]))
        self.assertEqual((pairs["--retriever_type"], pairs["--topk_context"], pairs["--history_format"], pairs["--useronly"],
                          pairs["--merge_key_expansion_into_value"], pairs["--model_name"]),
                         ("orig-turn", "50", "nl", "true", "replace", "gpt-4o-2024-08-06"))
        self.assertEqual(argv[-4:], ["--cot", "true", "--con", "true"])

    QUESTIONS = [("a1", "temporal-reasoning"), ("k1", "knowledge-update"), ("m1", "multi-session"),
                 ("k2_abs", "knowledge-update"), ("k3", "knowledge-update")]

    def test_a_question_type_keeps_that_types_questions_abstention_included_in_dataset_order(self):
        got = self.run_driver([{"op": "units", "dataset": "longmemeval_oracle"}], {"question_type": "knowledge-update"},
                              questions=self.QUESTIONS)
        self.assertEqual(got[0]["units"], ["k1", "k2_abs", "k3"])

    def test_without_a_question_type_every_question_runs(self):
        for options in ({}, {"question_type": "all"}):
            got = self.run_driver([{"op": "units", "dataset": "longmemeval_oracle"}], options, questions=self.QUESTIONS)
            self.assertEqual(got[0]["units"], [q for q, _ in self.QUESTIONS])

    def test_question_type_offers_every_type_and_applies_to_every_setup(self):
        o = BENCH["options"]["question_type"]
        self.assertEqual(o["default"], "all")
        self.assertEqual(set(o["values"]), {"all", "knowledge-update", "temporal-reasoning", "multi-session",
                                            "single-session-user", "single-session-assistant", "single-session-preference"})
        for setup in ("original", "model", "yours"):
            vals, bad = definitions.bench_options_of(BENCH, "longmemeval_oracle", {"question_type": "knowledge-update"}, setup)
            self.assertEqual((bad, vals["question_type"]), ([], "knowledge-update"))

    def test_a_dense_retriever_runs_run_retrieval_with_the_repositorys_shared_model_cache(self):
        with tempfile.TemporaryDirectory() as t:
            got = self.run_driver([{"op": "answer", "unit": "q1", "setup": "original", "model": "gpt-4o", "base_url": "u",
                                    "key": "k"}], {"retriever": "flat-contriever-session"}, keep=t)
            self.assertNotIn("error", got[0], got[0])
            with open(os.path.join(t, "ws", "q1.retrieved.json")) as f:
                ran = json.load(f)[0]["retrieval"]
            pairs = dict(zip(ran["argv"][::2], ran["argv"][1::2]))
            cache = os.path.join(t, "up", "model_cache")                    # run_retrieval.sh's, not the question's folder
            self.assertEqual((pairs["--retriever"], pairs["--granularity"], pairs["--cache_dir"], ran["hf_home"]),
                             ("flat-contriever", "session", cache, cache))
            argv = json.loads(got[0]["answer"])
            self.assertEqual(dict(zip(argv[::2], argv[1::2]))["--retriever_type"], "flat-session")


def _upstream():
    return "" if install.ready(install.upstream_dir(BENCH)) else "LongMemEval's upstream is not installed"


@unittest.skipIf(_upstream(), _upstream())
class Scripts(unittest.TestCase):
    def script(self, *p):
        with open(os.path.join(install.upstream_dir(BENCH), *p)) as f:
            return f.read()

    def test_every_flag_of_run_generation_and_run_retrieval_has_a_row(self):
        for path, n in ((("src", "generation", "run_generation.py"), 16), (("src", "retrieval", "run_retrieval.py"), 10)):
            flags = set(re.findall(r"add_argument\('(--[a-z_]+)'", self.script(*path)))
            self.assertEqual(len(flags), n, path)
            recorded = " ".join(r["setting"] for r in rows() if r["entry"] in ("/".join(path), "/".join(path)[:-3] + ".sh"))
            if path[1] == "generation":
                self.assertEqual({f for f in flags if f not in recorded}, set())

    def test_every_offered_value_is_a_script_alias(self):
        sh = self.script("src", "generation", "run_generation.sh")
        for v in BENCH["options"]["retriever"]["values"]:
            self.assertIn(f'$retriever_alias == "{v}"', sh)
        for v in BENCH["options"]["reading"]["values"] + ["con-separate"]:
            self.assertIn(f'$reading_method == "{v}"', sh)
        for m in BENCH["setups"]["original"]["models"]:
            self.assertIn(f'model_zoo["{m}"]="{BENCH["setups"]["original"]["served_as"][m]}"', sh)


if __name__ == "__main__":
    unittest.main()
