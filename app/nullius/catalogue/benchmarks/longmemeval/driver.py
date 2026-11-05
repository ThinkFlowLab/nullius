"""Run LongMemEval over JSON lines using its pinned upstream code and client.

For cap.memory/v1, setup feeds history sessions to add and the question to
recall. produce uses the upstream history-chats prompt and reader call on
recalled text. The reading option selects chain-of-note (800 tokens) or
direct answers (500). Recalled text is not truncated: upstream truncation
requires structured sessions. Reader calls retain upstream's unbounded
five-second rate-limit retries; an empty reply fails the question.

For Original, answer runs retrieval when selected, then run_generation.py
on one question using the shell script's model and option mappings. The
retrieval log is converted to a JSON list for the reader. Hypotheses stay
in the unit directory; a skipped question becomes an error. score runs
evaluate_qa.py on that hypothesis; capability runs call its judge directly.
Both paths use the run's relay routes for reader and judge model calls.
The oracle control supplies the dataset's answer, including its explanation
for abstention questions.

Questions retain dataset order and question_id. Large datasets are streamed
once into a line-per-question index so workers need not load the whole file.
"""
import ast
import contextlib
import glob
import inspect
import json
import os
import re
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.join(os.environ["NULLIUS_UPSTREAM"], "src"))
with contextlib.redirect_stdout(sys.stderr):    # stdout is the protocol's: the upstream's prints go to the log
    from evaluation import evaluate_qa          # noqa: E402  as published
    from generation import run_generation       # noqa: E402
from openai import OpenAI                       # noqa: E402  the client evaluate_qa is written for, pinned beside it


# Match the configured reading default to the upstream chain-of-note mode.
COT = os.environ.get("NULLIUS_OPTION_READING", "con") == "con"


def reader_template():
    """prepare_prompt's template for retrieved history chats (``merge_key_expansion_into_value``
    none), with chain of thought or without (``cot``, by the run's reading): a local name in the
    upstream, so read from its source through the imported module."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(run_generation.prepare_prompt)))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "merge_key_expansion_into_value is None" in ast.unparse(node.test):
            for inner in node.body:
                if isinstance(inner, ast.If) and ast.unparse(inner.test) == "cot":
                    for st in (inner.body if COT else inner.orelse):
                        if isinstance(st, ast.Assign) and isinstance(st.value, ast.Constant) \
                                and [ast.unparse(t) for t in st.targets] == ["answer_prompt_template"]:
                            return st.value.value
    raise RuntimeError("LongMemEval's prepare_prompt no longer holds its history-chats template where the driver reads it")


def gen_length():
    """main's answer length (``gen_length = 500 if not args.cot else 800``), by the run's reading:
    a local name in the upstream, so read from its source through the imported module."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(run_generation.main)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and [ast.unparse(t) for t in node.targets] == ["gen_length"] \
                and isinstance(node.value, ast.IfExp) and ast.unparse(node.value.test) == "not args.cot" \
                and isinstance(node.value.body, ast.Constant) and isinstance(node.value.orelse, ast.Constant):
            return (node.value.orelse if COT else node.value.body).value
    raise RuntimeError("LongMemEval's main no longer sets gen_length where the driver reads it")


READER = reader_template()
GEN_LENGTH = gen_length()
INDEX = {}


def entries(path, size=1 << 20):
    """The objects of the JSON list at ``path``, one at a time."""
    decoder = json.JSONDecoder()
    with open(path, encoding="utf-8") as f:
        buf = f.read(size)
        pos = buf.index("[") + 1
        while True:
            while True:
                while pos < len(buf) and buf[pos] in " \t\r\n,":
                    pos += 1
                if pos < len(buf):
                    break
                more = f.read(size)
                if not more:
                    return
                buf, pos = buf[pos:] + more, 0
            if buf[pos] == "]":
                return
            while True:
                try:
                    obj, end = decoder.raw_decode(buf, pos)
                    break
                except json.JSONDecodeError:
                    more = f.read(size)
                    if not more:
                        raise
                    buf, pos = buf[pos:] + more, 0
            yield obj
            buf, pos = buf[end:], 0


def lines_path():
    src = os.environ["NULLIUS_DATASET"]
    out = src + ".lines"
    if not os.path.isfile(out):                          # once per file: a copy, a line per question, written whole then renamed
        part = f"{out}.{os.getpid()}.part"
        with open(part, "w", encoding="utf-8") as f:
            for e in entries(src):
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(part, out)
    return out


def index():
    if not INDEX:
        with open(lines_path(), "rb") as f:
            at = 0
            for line in f:
                INDEX[json.loads(line)["question_id"]] = at
                at += len(line)
    return INDEX


def entry(qid):
    with open(lines_path(), "rb") as f:
        f.seek(index()[qid])
        return json.loads(f.readline())


def units(dataset, setup=None):
    return {"units": list(index())}                 # a setup's unit is a question too, as run_generation reads them


def setup(unit, dir):
    q = entry(unit)
    dates = q.get("haystack_dates") or [""] * len(q["haystack_session_ids"])
    sessions = []
    for sid, turns, date in zip(q["haystack_session_ids"], q["haystack_sessions"], dates):
        ts = [{"role": t.get("role") or "user", "text": str(t.get("content") or "")} for t in turns]
        sessions.append({"id": str(sid), "date": date or "", "turns": ts,
                         "text": f"Session Date: {date}\n" + "\n".join(f"{t['role']}: {t['text']}" for t in ts)})
    return {"memory": {"add": sessions, "recall": [{"id": unit, "question": q["question"]}]}}


def produce(unit, dir, recalled, model, base_url, key):
    q = entry(unit)
    memory = recalled[0]["memory"] if recalled else ""
    prompt = READER.format(memory or "(nothing recalled)", q.get("question_date") or "", q["question"])
    # run_generation.main's own request for one question: its client, its kwargs, its call, its reading of the reply
    client = OpenAI(api_key=key, base_url=base_url)
    kwargs = {"model": model, "messages": [{"role": "user", "content": prompt}], "n": 1, "temperature": 0, "max_tokens": GEN_LENGTH}
    with contextlib.redirect_stdout(sys.stderr):
        completion = run_generation.chat_completions_with_backoff(client, **kwargs)
    return {"answer": completion.choices[0].message.content.strip()}


def score(unit, dir, transcript, answer, judge=None, setup=None, **_):
    if not judge:
        raise ValueError("LongMemEval is graded by a judge model, and none was given")
    if setup:
        return score_setup(unit, dir, judge)
    q = entry(unit)
    # evaluate_qa's own loop, for one question: its prompt, its client call (backoff included), its reading of the reply
    prompt = evaluate_qa.get_anscheck_prompt(q["question_type"], q["question"], q["answer"], answer or "", abstention="_abs" in unit)
    client = OpenAI(api_key=judge["key"], base_url=judge["base_url"])
    completion = evaluate_qa.chat_completions_with_backoff(client, model=judge["model"], messages=[{"role": "user", "content": prompt}],
                                                           n=1, temperature=0, max_tokens=10)
    said = completion.choices[0].message.content.strip()
    ok = "yes" in said.lower()
    return {"reward": 1.0 if ok else 0.0, "passed": ok,
            "detail": {"question_type": q["question_type"], "judge": judge["model"], "judge_said": said, "reference": q["answer"]}}


def oracle(unit, dir):
    return {"answer": str(entry(unit)["answer"])}


# --- a setup: the paper's pipeline, run as its scripts run it ----------------------------------------

def _opt(name, default):
    return os.environ.get("NULLIUS_OPTION_" + name.upper(), default)


def _script(*path):
    with open(os.path.join(os.environ["NULLIUS_UPSTREAM"], *path), encoding="utf-8") as f:
        return f.read()


def model_zoo():
    """run_generation.sh's model_zoo: the alias a run names -> the name run_generation.py is given."""
    return dict(re.findall(r'^model_zoo\["([^"]+)"\]="([^"]+)"', _script("src", "generation", "run_generation.sh"), re.M))


def alias_table(var, target):
    """One of run_generation.sh's if-chains, ``$var == "a" || ...`` -> ``target="value"``: the alias -> its value."""
    out = {}
    for cond, value in re.findall(r'if \[\[ (.*?) \]\]; then\s+' + target + r'="([^"]*)"', _script("src", "generation", "run_generation.sh")):
        for a in re.findall(r'\$' + var + r' == "([^"]+)"', cond):
            out[a] = value
    return out


RETRIEVED = "retrieval"      # in the unit's folder: run_retrieval.py's log, for a retriever of the paper's

# Stella V5, which run_retrieval.py reads from the cache folder rather than the hub - the README has it downloaded by
# hand: the model, its code and its 1024-dimension head (2_Dense_1024), the one run_retrieval.py loads. The weights
# once, as model.safetensors, which AutoModel reads first: the repository's pytorch_model.bin is the same 6 GB again.
STELLA = ("dunzhang/stella_en_1.5B_v5", "dunzhang_stella_en_1.5B_v5",
          ["*.json", "*.py", "*.txt", "*.model", "model*.safetensors", "2_Dense_1024/*"])


def model_cache(up, retriever):
    """run_retrieval.sh's cache - the repository's own model_cache, HF_HOME and --cache_dir both - shared by every
    question, so a retriever's model is downloaded once; Stella fetched into it first, as the README has it."""
    cache = os.path.join(up, "model_cache")
    os.makedirs(cache, exist_ok=True)
    need = ("config.json", "model.safetensors", "2_Dense_1024/pytorch_model.bin")   # each renamed in only once whole
    if retriever == "flat-stella" and not all(os.path.exists(os.path.join(cache, STELLA[1], f)) for f in need):
        from huggingface_hub import snapshot_download          # the driver's environment's, pinned
        with contextlib.redirect_stdout(sys.stderr):
            snapshot_download(STELLA[0], local_dir=os.path.join(cache, STELLA[1]), allow_patterns=STELLA[2])
    return cache
GENERATED = "generation"     # run_generation.py's test log


def _run(argv, cwd, env, log, what):
    with open(log, "w") as f:
        rc = subprocess.run(argv, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        with open(log, encoding="utf-8", errors="replace") as f:
            tail = [ln for ln in f.read().splitlines() if ln.strip()][-3:]
        raise RuntimeError(f"{what} exited {rc}: " + " | ".join(tail))


def answer(unit, dir, setup, model, base_url, key, judge=None, simulator=None, attempt=None):
    up = os.environ["NULLIUS_UPSTREAM"]
    retriever = _opt("retriever", "full-history-session")
    reading = _opt("reading", "con")
    if reading == "con" and _opt("separate_notes", "false") == "true":
        reading = "con-separate"
    retriever_type = alias_table("retriever_alias", "retriever_type").get(retriever)
    flags = alias_table("reading_method", "reading_flags").get(reading)
    served = model_zoo().get(model)
    if retriever_type is None or flags is None or served is None:
        raise ValueError(f"run_generation.sh does not know {retriever!r}, {reading!r} or {model!r}")
    in_file = os.path.join(dir, f"{unit}.json")                 # the one question, as the released files hold it
    with open(in_file, "w", encoding="utf-8") as f:
        json.dump([entry(unit)], f)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    m = re.match(r"(flat-[a-z0-9]+)-(session|turn)$", retriever)
    if m:                                                       # the paper's retrieval first, as run_retrieval.sh runs it
        out = os.path.join(dir, RETRIEVED)
        os.makedirs(out, exist_ok=True)
        cache = model_cache(up, m.group(1))
        _run([sys.executable, "run_retrieval.py", "--in_file", in_file, "--retriever", m.group(1), "--granularity", m.group(2),
              "--index_expansion_method", "none", "--index_expansion_result_join_mode", "none",
              "--index_expansion_llm", "none", "--index_expansion_result_cache", "none",
              "--out_dir", out, "--outfile_prefix", "none", "--cache_dir", cache],
             os.path.join(up, "src", "retrieval"), dict(env, PYTHONPATH=up, HF_HOME=cache),
             os.path.join(dir, "run_retrieval.log"), "run_retrieval.py")
        # its log is JSON lines, which run_generation.py reads only when json.load fails on it - and a log of one
        # question is one line, which json.load reads as that question's object: the same entries are handed
        # over as a list, the form the released files have and run_generation.py reads first
        with open(glob.glob(os.path.join(out, "*_retrievallog_*"))[0], encoding="utf-8") as f:
            ranked = [json.loads(ln) for ln in f if ln.strip()]
        in_file = os.path.join(dir, f"{unit}.retrieved.json")
        with open(in_file, "w", encoding="utf-8") as f:
            json.dump(ranked, f)
    out = os.path.join(dir, GENERATED)
    os.makedirs(out, exist_ok=True)
    argv = [sys.executable, "run_generation.py", "--in_file", in_file, "--out_dir", out,
            "--model_name", served, "--model_alias", model, "--openai_key", key, "--openai_base_url", base_url,
            "--retriever_type", retriever_type, "--topk_context", _opt("topk", "1000"),
            "--merge_key_expansion_into_value", _opt("merge_key_expansion", "none"),
            "--history_format", _opt("history_format", "json"), "--useronly", _opt("useronly", "false")] + flags.split()
    log = os.path.join(dir, "run_generation.log")
    _run(argv, os.path.join(up, "src", "generation"), env, log, "run_generation.py")
    hyp = [json.loads(ln) for p in glob.glob(os.path.join(out, "*_testlog_*")) for ln in open(p, encoding="utf-8") if ln.strip()]
    if not hyp:                                                 # main skipped the question: its own words say why
        with open(log, encoding="utf-8", errors="replace") as f:
            said = [ln for ln in f.read().splitlines() if ln.startswith("One exception captured")]
        raise RuntimeError("run_generation.py skipped the question: " + (said[-1] if said else "no hypothesis written"))
    return {"answer": hyp[0]["hypothesis"], "model": model}


def score_setup(unit, dir, judge):
    hyp = glob.glob(os.path.join(dir, GENERATED, "*_testlog_*"))
    hyp = [p for p in hyp if ".eval-results-" not in p]
    if not hyp:
        raise RuntimeError("no hypothesis to score: run_generation.py wrote none")
    up = os.environ["NULLIUS_UPSTREAM"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", OPENAI_API_KEY=judge["key"], OPENAI_BASE_URL=judge["base_url"])
    env.pop("OPENAI_ORGANIZATION", None)
    _run([sys.executable, "evaluate_qa.py", "gpt-4o", hyp[0], os.path.join(dir, f"{unit}.json")],
         os.path.join(up, "src", "evaluation"), env, os.path.join(dir, "evaluate_qa.log"), "evaluate_qa.py")
    with open(hyp[0] + ".eval-results-gpt-4o", encoding="utf-8") as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    if not rows:
        raise RuntimeError("evaluate_qa.py wrote no verdict")
    ok = bool(rows[0]["autoeval_label"]["label"])
    q = entry(unit)
    return {"reward": 1.0 if ok else 0.0, "passed": ok,
            "detail": {"question_type": q["question_type"], "judge": judge["model"], "asked_as": rows[0]["autoeval_label"]["model"],
                       "reference": q["answer"], "answer": rows[0]["hypothesis"],
                       "options": {n: _opt(n, d) for n, d in (("retriever", "full-history-session"), ("topk", "1000"),
                                   ("reading", "con"), ("separate_notes", "false"), ("history_format", "json"), ("useronly", "false"),
                                   ("merge_key_expansion", "none"))}}}


HANDLERS = {"units": units, "setup": setup, "produce": produce, "score": score, "oracle": oracle, "answer": answer}
for line in sys.stdin:
    try:
        req = json.loads(line)
        out = HANDLERS[req.pop("op")](**req)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out), flush=True)
