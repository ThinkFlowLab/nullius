"""LoCoMo's driver for the Nullius standard (JSON lines on stdin and stdout).

A capability benchmark: the memory under test is plugged in by Nullius through
cap.memory/v1, and this driver only says what to feed it and what to ask.
What LoCoMo itself decides - the reader's prompt and the score - is LoCoMo's
own code at the tested commit ($NULLIUS_UPSTREAM), imported, not copied: its
modules import torch, bert_score and the vendors' clients, so the driver runs
in the environment of its own that standard.json declares (driver.install).

units    - every question of categories 1 to 4, as <sample_id>:<n> (the
           adversarial category 5 has no reference answer in the file)
setup    - the question's conversation, a session at a time, for ``add``;
           the question, for ``recall``
produce  - LoCoMo's reader prompt over what the memory recalled, as its RAG
           mode builds it (task_eval/gpt_utils.py: the context, a blank line,
           QA_PROMPT; a category-2 question carries the date hint
           get_gpt_answers adds), asked by LoCoMo's own run_chatgpt
           (global_methods.py) as get_gpt_answers calls it for one question
           (num_tokens_request=32, temperature=0, wait_time=2, its model
           argument and use_16k from the model's name), with the openai 0.28
           client it is written for pointed at the relay (openai.api_base and
           openai.api_key, the base_url and key Nullius gives); the answer is
           its reply stripped, as get_gpt_answers keeps it. Its behaviour is
           the upstream's, as published: the message's role and whether a
           model is asked at all follow the model's name (a name with
           "gpt-3.5" is asked as gpt-3.5-turbo, as the system; one with
           "gpt-4", as the user; any other raises), it retries API,
           connection and unavailable errors without end (a rate limit with
           no wait), and a reply with no text fails the question. The run's
           turn_seconds bounds it: past them the question is the reader's
           error and the driver is started again for the next
score    - LoCoMo's own eval_question_answering (task_eval/evaluation.py) on
           the one question: token F1 with its normalisation and Porter
           stemming, category 1 by comma-separated parts, category 3 against
           the reference's part before the first ";"
answer   - a setup's (original, model): LoCoMo's own task_eval/evaluate_qa.py,
           run as published on the one conversation - its questions of
           categories 1 to 4, as its scripts feed it - with --model
           the model the setup asks as and --batch-size as the paper's
           scripts give it (20 for gpt-4-turbo, 10 for the gpt-3.5-turbo
           windows); its openai 0.28 client pointed at the relay through
           OPENAI_API_BASE and OPENAI_API_KEY, which it reads. Its output
           file, with each question's prediction and its own F1, stays in
           the unit's folder for score. With retrieval (the setups' options
           retrieval, retriever, top_k: --use-rag, --rag-mode, --retriever,
           --top-k), as scripts/evaluate_rag_gpts.sh runs it: one question a
           call, the database in the unit's folder (--emb-dir); for
           observation and summary the upstream's own get_facts.py and
           get_session_summaries.py write it first, with the flags
           scripts/generate_observations.sh and generate_session_summaries.sh
           pass (the latter's "---data-file" read as the --data-file it
           means). Its encoders run on a CUDA GPU, as published
oracle   - the reference control: the question's own "answer" field as that
           scorer reads it - for category 3, its first part (``gold``): there
           the field can carry the authors' reasoning after a ";", which
           LoCoMo's evaluation cuts off before scoring, so the whole field
           would lose F1 on its own words
"""
import ast
import contextlib
import inspect
import json
import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.environ["NULLIUS_UPSTREAM"])
with contextlib.redirect_stdout(sys.stderr):    # stdout is the protocol's: the upstream's prints go to the log
    from task_eval import evaluation, gpt_utils  # noqa: E402  as published
import openai                                    # noqa: E402  0.28, the client run_chatgpt is written for

DATA = {}


def load():
    if not DATA:
        with open(os.environ["NULLIUS_DATASET"]) as f:
            for conv in json.load(f):
                DATA[conv["sample_id"]] = conv
    return DATA


def question(unit):
    sid, n = unit.rsplit(":", 1)
    return load()[sid], load()[sid]["qa"][int(n)]


def units(dataset, setup=None):
    if setup:                                    # a setup's unit is a conversation, as the paper batches its questions
        return {"units": list(load())}
    out = []
    for sid, conv in load().items():
        out += [f"{sid}:{i}" for i, q in enumerate(conv["qa"]) if q.get("category") in (1, 2, 3, 4)]
    return {"units": out}


# What goes into the memory for each session: the run's ``rag_mode`` option, LoCoMo's --rag-mode - dialog
# (the conversation), or the authors' own released extractions, observation (facts per speaker) and
# summary (one per session). How a fact or a summary is put as a turn is ours: a fact is its speaker's
# turn, a summary one turn whose role is "summary".
RAG_MODE = os.environ.get("NULLIUS_OPTION_RAG_MODE", "dialog")


def sessions(conv):
    c = conv["conversation"]
    n = 1
    out = []
    while f"session_{n}" in c:
        if RAG_MODE == "observation":
            turns = [{"role": spk, "text": fact} for spk, facts in conv["observation"][f"session_{n}_observation"].items()
                     for fact, _ in facts]
        elif RAG_MODE == "summary":
            turns = [{"role": "summary", "text": conv["session_summary"][f"session_{n}_summary"]}]
        else:
            turns = [{"role": t["speaker"], "text": t["text"] + (f" [shares a photo: {t['blip_caption']}]" if t.get("blip_caption") else "")}
                     for t in c[f"session_{n}"]]
        date = c.get(f"session_{n}_date_time", "")
        out.append({"id": f"session_{n}", "date": date, "turns": turns,
                    "text": f"DATE: {date}\n" + "\n".join(f"{t['role']}: {t['text']}" for t in turns)})
        n += 1
    return out


def setup(unit, dir):
    conv, q = question(unit)
    return {"memory": {"add": sessions(conv), "recall": [{"id": unit, "question": q["question"]}]}}


def date_hint():
    """The words LoCoMo's reader adds to a category-2 question. They have no
    name of their own in the upstream: they are read from get_gpt_answers'
    source, through the imported module, at the tested commit."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(gpt_utils.get_gpt_answers)))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test).replace('"', "'") == "qa['category'] == 2":
            for sub in ast.walk(node.body[0]):
                if isinstance(sub, ast.BinOp) and isinstance(sub.right, ast.Constant) and isinstance(sub.right.value, str):
                    return sub.right.value
    raise RuntimeError("LoCoMo's get_gpt_answers no longer adds a date hint to a category-2 question")


HINT = date_hint()


def produce(unit, dir, recalled, model, base_url, key):
    _, q = question(unit)
    memory = recalled[0]["memory"] if recalled else ""
    asked = q["question"] + (HINT if q["category"] == 2 else "")
    query = (memory or "(nothing recalled)") + "\n\n" + gpt_utils.QA_PROMPT.format(asked)
    openai.api_base, openai.api_key = base_url, key      # what run_chatgpt's client reads: the relay, for this question
    with contextlib.redirect_stdout(sys.stderr):
        # get_gpt_answers' own call for one question (batch size 1, as its RAG mode asks)
        answer = gpt_utils.run_chatgpt(query, num_gen=1, num_tokens_request=32,
                                       model='chatgpt' if 'gpt-3.5' in model else model,
                                       use_16k=True if any([k in model for k in ['16k', '12k', '8k', '4k']]) else False,
                                       temperature=0, wait_time=2)
    return {"answer": answer.strip()}


def gold(q):
    """The reference as LoCoMo's evaluation scores against it: the answer
    field, for category 3 only its part before the first ";"."""
    ref = str(q["answer"])
    return ref.split(";")[0].strip() if q["category"] == 3 else ref


# The paper's batch per reader, as its scripts run evaluate_qa.py (scripts/evaluate_gpts.sh): every reader
# with a gpt-3.5-turbo window is asked 10 questions a call, gpt-4-turbo 20.
BATCH = {"gpt-4-turbo": 20}
ASKED = "asked.json"                             # in the unit's folder: the conversation as fed, and what came back
ANSWERS = "answers.json"


# The setups' retrieval (none: the whole conversation in the prompt), as evaluate_qa.py's flags name it.
RETRIEVAL = os.environ.get("NULLIUS_OPTION_RETRIEVAL", "none")
RETRIEVER = os.environ.get("NULLIUS_OPTION_RETRIEVER", "dragon")
TOP_K = os.environ.get("NULLIUS_OPTION_TOP_K", "5")
# what writes the database first, for a mode whose database is written by a script of its own: its output file
BUILD = {"observation": ("get_facts.py", "observation.json", ["--use-date", "--overwrite"]),
         "summary": ("get_session_summaries.py", "session_summary.json", ["--use-date"])}


def model_key(model):
    """The name evaluate_qa.py files a reader's predictions and F1 under."""
    return model if RETRIEVAL == "none" else f"{model}_{RETRIEVAL}_top_{TOP_K}"


def upstream(script, argv, dir, env):
    """One of the upstream's task_eval scripts, run as published; its output in the unit's folder."""
    up = os.environ["NULLIUS_UPSTREAM"]
    logname = os.path.join(dir, script.replace(".py", ".log"))
    with open(logname, "w") as log:
        rc = subprocess.run([sys.executable, os.path.join(up, "task_eval", script)] + argv, cwd=os.path.join(up, "task_eval"),
                            env=env, stdout=log, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        with open(logname) as f:
            tail = [ln for ln in f.read().splitlines() if ln.strip()][-3:]
        raise RuntimeError(f"{script} exited {rc}: " + " | ".join(tail))


def answer(unit, dir, setup, model, base_url, key):
    conv = load()[unit]
    fed = dict(conv, qa=[q for q in conv["qa"] if q.get("category") in (1, 2, 3, 4)])
    asked = os.path.join(dir, ASKED)
    with open(asked, "w") as f:
        json.dump([fed], f)
    out = os.path.join(dir, ANSWERS)
    env = dict(os.environ, OPENAI_API_BASE=base_url, OPENAI_API_KEY=key)
    up = os.environ["NULLIUS_UPSTREAM"]
    argv = ["--data-file", asked, "--out-file", out, "--model", model]
    if RETRIEVAL == "none":
        argv += ["--batch-size", str(BATCH.get(model, 10))]
    else:
        if RETRIEVAL in BUILD:                   # its database, written by the upstream's own script; named as evaluate_qa.py reads it
            script, name, flags = BUILD[RETRIEVAL]
            upstream(script, ["--data-file", asked, "--out-file", os.path.join(dir, "asked_" + name),
                              "--prompt-dir", os.path.join(up, "prompt_examples"), "--emb-dir", dir,
                              "--retriever", RETRIEVER] + flags, dir, env)
        argv += ["--batch-size", "1", "--use-rag", "--retriever", RETRIEVER, "--top-k", TOP_K,
                 "--emb-dir", dir, "--rag-mode", RETRIEVAL]
    upstream("evaluate_qa.py", argv, dir, env)
    with open(out) as f:
        qa = json.load(f)[0]["qa"]
    k = model_key(model)
    return {"answer": json.dumps({str(i): q.get(f"{k}_prediction", "") for i, q in enumerate(qa)}), "model": model}


def score(unit, dir, transcript, answer, setup=None, **_):
    if setup:                                    # the upstream's own F1 per question, as evaluate_qa.py wrote it
        with open(os.path.join(dir, ANSWERS)) as f:
            qa = json.load(f)[0]["qa"]
        model = next((t["asked"] for t in transcript if t.get("asked")), "")
        k = model_key(model)
        f1 = [q.get(f"{k}_f1") for q in qa]
        got = [x for x in f1 if isinstance(x, (int, float))]
        rag = {} if RETRIEVAL == "none" else {"retrieval": RETRIEVAL, "retriever": RETRIEVER, "top_k": int(TOP_K)}
        recall = [q[f"{k}_recall"] for q in qa if isinstance(q.get(f"{k}_recall"), (int, float))]
        if recall:                                # the upstream's own: the share of a question's evidence among what was retrieved
            rag["recall"] = sum(recall) / len(recall)
        # the paper's number is over questions, not conversations: the job weighs this one by its own
        parts = {"n": len(got), "by": "category",
                 "parts": [{"n": i + 1, "text": q["question"], "group": q["category"], "score": q.get(f"{k}_f1")}
                           for i, q in enumerate(qa) if isinstance(q.get(f"{k}_f1"), (int, float))]}
        return {"reward": sum(got) / len(got) if got else 0.0, **({"parts": parts} if got else {}),
                "detail": dict({"questions": len(qa), "scored": len(got), "model": model,
                                "per_question": [dict({"question": q["question"], "reference": q["answer"], "category": q["category"],
                                                       "answer": q.get(f"{k}_prediction"), "f1": q.get(f"{k}_f1")},
                                                      **({"retrieved": q.get(f"{k}_prediction_context"), "recall": q.get(f"{k}_recall")} if rag else {}))
                                                 for q in qa]}, **rag)}
    _, q = question(unit)
    with contextlib.redirect_stdout(sys.stderr):
        values, _, _ = evaluation.eval_question_answering([dict(q, prediction=answer or "")], "prediction")
    return {"reward": float(values[0]), "detail": {"question": q["question"], "reference": q["answer"], "category": q["category"],
                                                  "answer": answer, "evidence": q.get("evidence")}}


def oracle(unit, dir):
    _, q = question(unit)
    return {"answer": gold(q)}


HANDLERS = {"units": units, "setup": setup, "produce": produce, "score": score, "oracle": oracle, "answer": answer}
for line in sys.stdin:
    try:
        req = json.loads(line)
        out = HANDLERS[req.pop("op")](**req)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out), flush=True)
