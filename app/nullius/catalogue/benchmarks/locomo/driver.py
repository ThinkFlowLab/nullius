"""LoCoMo's driver for the Nullius standard (JSON lines on stdin and stdout).

A capability benchmark: the memory under test is plugged in by Nullius through
cap.memory/v1, and this driver only says what to feed it and what to ask.
What LoCoMo itself decides - the reader's prompt and the score - is LoCoMo's
own code at the tested commit ($NULLIUS_UPSTREAM), imported, not copied: its
modules import torch, bert_score and the vendors' clients, so the driver runs
in the environment of its own that standard.json declares (driver.install).

units    - every conversation, by its sample_id, as the paper batches its
           questions: the memory is given a conversation once and asked all of
           its questions. Those of categories 1 to 4, each <sample_id>:<n>: the
           published code cannot ask or score the adversarial category 5, whose
           records have no "answer". The version "full", whose
           upstream reads their "adversarial_answer", has them too
setup    - the conversation, a session at a time, for ``add``; each of its
           questions, for ``recall``
produce  - for each question, LoCoMo's reader prompt over what the memory recalled, as its RAG
           mode builds it (task_eval/gpt_utils.py: the context, a blank line,
           QA_PROMPT; a category-2 question carries the date hint
           get_gpt_answers adds), asked by LoCoMo's own run_chatgpt
           (global_methods.py) as get_gpt_answers calls it for one question,
           with the openai 0.28
           client it is written for pointed at the relay (openai.api_base and
           openai.api_key, the base_url and key Nullius gives); the answer is
           its reply stripped, as get_gpt_answers keeps it. Its behaviour is
           the upstream's, as published: the message's role and whether a
           model is asked at all follow the model's name (a name with
           "gpt-3.5" is asked as gpt-3.5-turbo, as the system; one with
           "gpt-4", as the user; any other raises), it retries API,
           connection and unavailable errors without end (a rate limit with
           no wait), and a reply with no text fails the question: that
           question is left unscored, with its error, and the others go on.
           The run's turn_seconds bounds the whole conversation. A category-5
           question is a choice between its false answer and "Not mentioned in
           the conversation", in get_gpt_answers' words and QA_PROMPT_CAT_5, its
           reply read by get_cat_5_answer; which comes first is drawn from the
           question's id, so every memory is asked the same
score    - LoCoMo's own eval_question_answering (task_eval/evaluation.py) on
           each question: token F1 with its normalisation and Porter
           stemming, category 1 by comma-separated parts, category 3 against
           the reference's part before the first ";", category 5 whether it
           says the conversation does not mention it; the mean over the
           questions scored, each question a part
answer   - a setup's (original, model): LoCoMo's own task_eval/evaluate_qa.py,
           run as published on the one conversation - the version's
           questions, as its scripts feed it - with --model
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
judge    - the experiment judge, only when the run names its model: Mem0's
           LLM judge as Mem0 scored LoCoMo (evaluation/metrics/llm_judge.py
           at mem0ai/mem0 aae5989, Apache-2.0; its prompt and call copied
           below, as that repository no longer holds them), asked of each
           answer of categories 1 to 4 against the whole reference, as Mem0's
           script reads it; the share it marks CORRECT is the score's
           complement, beside the F1 and never in its place. A question whose
           judge call failed is left unjudged, with its error
oracle   - the reference control: each question's own "answer" field as that
           scorer reads it - for category 3, its first part (``gold``): there
           the field can carry the authors' reasoning after a ";", which
           LoCoMo's evaluation cuts off before scoring, so the whole field
           would lose F1 on its own words; for category 5, "Not mentioned in
           the conversation", the choice the scorer looks for
"""
import ast
import contextlib
import inspect
import json
import os
import random
import re
import subprocess
import sys
import textwrap

sys.path.insert(0, os.environ["NULLIUS_UPSTREAM"])
with contextlib.redirect_stdout(sys.stderr):    # stdout is the protocol's: the upstream's prints go to the log
    from task_eval import evaluation, gpt_utils  # noqa: E402  as published
import openai                                    # noqa: E402  0.28, the client run_chatgpt is written for

DATA = {}
CATEGORIES = (1, 2, 3, 4, 5) if os.environ.get("NULLIUS_VERSION") == "full" else (1, 2, 3, 4)


def load():
    if not DATA:
        with open(os.environ["NULLIUS_DATASET"]) as f:
            for conv in json.load(f):
                DATA[conv["sample_id"]] = conv
    return DATA


def questions(unit):
    """The conversation's questions the version asks, by id: <sample_id>:<n>."""
    return {f"{unit}:{i}": q for i, q in enumerate(load()[unit]["qa"]) if q.get("category") in CATEGORIES}


def units(dataset, setup=None):
    return {"units": list(load())}


def describe(dataset, **_):
    out = {}
    for unit, conv in load().items():
        c = conv["conversation"]
        n = sum(1 for k in c if k.startswith("session_") and not k.endswith("date_time"))
        out[unit] = {"title": f"{c.get('speaker_a')} and {c.get('speaker_b')}",
                     "text": f"{n} sessions, {len(questions(unit))} questions"}
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
    return {"memory": {"add": sessions(load()[unit]), "recall": [{"id": i, "question": q["question"]} for i, q in questions(unit).items()]}}


def reader_words(category):
    """The words LoCoMo's reader adds to a question of ``category``, and, for 5,
    the choice it offers beside the false answer. They have no name of their own
    in the upstream: they are read from get_gpt_answers' source, through the
    imported module, at the tested commit."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(gpt_utils.get_gpt_answers)))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test).replace('"', "'") == f"qa['category'] == {category}":
            found = list(ast.walk(ast.Module(node.body, [])))
            added = [n.right.value for n in found if isinstance(n, ast.BinOp) and isinstance(n.right, ast.Constant)
                     and isinstance(n.right.value, str)]
            offered = [x.value for n in found if isinstance(n, ast.Call) and ast.unparse(n.func) == "question.format"
                       for x in n.args if isinstance(x, ast.Constant)]
            if added and (category != 5 or offered):
                return added[0], next(iter(offered), "")
    raise RuntimeError(f"LoCoMo's get_gpt_answers no longer words a category-{category} question as it did")


HINT, _ = reader_words(2)
CHOICE, UNMENTIONED = reader_words(5) if 5 in CATEGORIES else ("", "")


def produce(unit, dir, recalled, model, base_url, key):
    """Each question's answer by its id; a question the reader failed, its error."""
    asked = questions(unit)
    out = {}
    for r in recalled:
        try:
            out[r["id"]] = read(r["id"], asked[r["id"]], r.get("memory") or "", model, base_url, key)
        except Exception as e:
            out[r["id"]] = {"error": f"{type(e).__name__}: {e}"}
    if out and all(isinstance(a, dict) for a in out.values()):
        raise RuntimeError(f"the reader answered none of the {len(out)} questions; the first: {next(iter(out.values()))['error']}")
    return {"answer": json.dumps(out)}


def read(qid, q, memory, model, base_url, key):
    """LoCoMo's reader on one question."""
    asked = q["question"] + (HINT if q["category"] == 2 else "")
    prompt, choices = gpt_utils.QA_PROMPT, {}
    if q["category"] == 5:
        pair = [UNMENTIONED, q["adversarial_answer"]]
        if random.Random(qid).random() >= 0.5:
            pair.reverse()
        prompt, choices, asked = gpt_utils.QA_PROMPT_CAT_5, dict(zip("ab", pair)), q["question"] + CHOICE.format(*pair)
    query = (memory or "(nothing recalled)") + "\n\n" + prompt.format(asked)
    openai.api_base, openai.api_key = base_url, key
    with contextlib.redirect_stdout(sys.stderr):
        # get_gpt_answers' own call for one question (batch size 1, as its RAG mode asks)
        answer = gpt_utils.run_chatgpt(query, num_gen=1, num_tokens_request=32,
                                       model='chatgpt' if 'gpt-3.5' in model else model,
                                       use_16k=True if any([k in model for k in ['16k', '12k', '8k', '4k']]) else False,
                                       temperature=0, wait_time=2)
    if choices:
        answer = gpt_utils.get_cat_5_answer(answer, choices)
    return answer.strip()


def reference(q):
    """The question's reference, as its record shows it."""
    return UNMENTIONED if q["category"] == 5 else q["answer"]


def gold(q):
    """The reference as LoCoMo's evaluation scores against it: the answer
    field, for category 3 only its part before the first ";"."""
    ref = str(reference(q))
    return ref.split(";")[0].strip() if q["category"] == 3 else ref


# The paper's batch per reader, as its scripts run evaluate_qa.py (scripts/evaluate_gpts.sh):
# 10 questions a call, gpt-4-turbo 20.
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


def answer(unit, dir, setup, model, base_url, key, **_):
    conv = load()[unit]
    fed = dict(conv, qa=[q for q in conv["qa"] if q.get("category") in CATEGORIES])
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


# Mem0's judge, mem0ai/mem0 evaluation/metrics/llm_judge.py at aae5989e78a6188b3b047c104d960c9ad0927e75, word for word.
JUDGE_PROMPT = """
Your task is to label an answer to a question as ’CORRECT’ or ’WRONG’. You will be given the following data:
    (1) a question (posed by one user to another user), 
    (2) a ’gold’ (ground truth) answer, 
    (3) a generated answer
which you will score as CORRECT/WRONG.

The point of the question is to ask about something one user should know about the other user based on their prior conversations.
The gold answer will usually be a concise and short answer that includes the referenced topic, for example:
Question: Do you remember what I got the last time I went to Hawaii?
Gold answer: A shell necklace
The generated answer might be much longer, but you should be generous with your grading - as long as it touches on the same topic as the gold answer, it should be counted as CORRECT. 

For time related questions, the gold answer will be a specific date, month, year, etc. The generated answer might be much longer or use relative time references (like "last Tuesday" or "next month"), but you should be generous with your grading - as long as it refers to the same date or time period as the gold answer, it should be counted as CORRECT. Even if the format differs (e.g., "May 7th" vs "7 May"), consider it CORRECT if it's the same date.

Now it's time for the real question:
Question: {question}
Gold answer: {gold_answer}
Generated answer: {generated_answer}

First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG. 
Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.

Just return the label CORRECT or WRONG in a json format with the key as "label".
"""
JUDGE_LABEL = "Mem0 judge"


def judge_says(q, answer, judge):
    """Mem0's evaluate_llm_judge on one answer: its request, its reading of the reply (extract_json, then "label")."""
    reply = openai.ChatCompletion.create(
        model=judge["model"], api_base=judge["base_url"], api_key=judge["key"],
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(question=q["question"], gold_answer=q["answer"], generated_answer=answer)}],
        response_format={"type": "json_object"}, temperature=0.0)
    text = reply["choices"][0]["message"]["content"].strip()
    found = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return "CORRECT" if json.loads(found.group(1) if found else text)["label"] == "CORRECT" else "WRONG"


def judged(rows, judge):
    """Each row (its question, answer and record) given the judge's label, or its error; the complement over them."""
    said = []
    for q, answer, row in rows:
        if q["category"] == 5:                   # Mem0's script skips them
            continue
        try:
            row["judge"] = judge_says(q, answer, judge)
            said.append(row["judge"] == "CORRECT")
        except Exception as e:
            row["judge_error"] = f"{type(e).__name__}: {e}"
    return {"label": JUDGE_LABEL, "score": sum(said) / len(said), "n": len(said)} if said else None


def score(unit, dir, transcript, answer, setup=None, judge=None, **_):
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
        per = [dict({"question": q["question"], "reference": reference(q), "category": q["category"],
                     "answer": q.get(f"{k}_prediction"), "f1": q.get(f"{k}_f1")},
                    **({"retrieved": q.get(f"{k}_prediction_context"), "recall": q.get(f"{k}_recall")} if rag else {}))
               for q in qa]
        also = judged([(q, row["answer"] or "", row) for q, row in zip(qa, per) if isinstance(row["f1"], (int, float))], judge) if judge else None
        return {"reward": sum(got) / len(got) if got else 0.0, **({"parts": parts} if got else {}), **({"complement": also} if also else {}),
                "detail": dict({"questions": len(qa), "scored": len(got), "model": model, "per_question": per}, **rag)}
    asked = questions(unit)
    given = json.loads(answer) if answer else {}                 # nop: nothing, so every answer empty
    got = {i: given.get(i, "") for i in asked if not isinstance(given.get(i), dict)}
    values = []
    if got:
        with contextlib.redirect_stdout(sys.stderr):
            values, _, _ = evaluation.eval_question_answering([dict(asked[i], prediction=a) for i, a in got.items()], "prediction")
    f1 = dict(zip(got, (float(v) for v in values)))
    parts = [{"n": n + 1, "text": q["question"], "group": q["category"], "score": f1[i]}
             for n, (i, q) in enumerate(asked.items()) if i in f1]
    per = [dict({"id": i, "question": q["question"], "reference": reference(q), "category": q["category"], "evidence": q.get("evidence")},
                **({"answer": got[i], "f1": f1[i]} if i in f1 else {"error": given[i]["error"]}))
           for i, q in asked.items()]
    also = judged([(q, got[i], row) for (i, q), row in zip(asked.items(), per) if i in f1], judge) if judge else None
    return {"reward": sum(f1.values()) / len(f1) if f1 else 0.0, **({"parts": {"n": len(f1), "by": "category", "parts": parts}} if f1 else {}),
            **({"complement": also} if also else {}),
            "detail": {"questions": len(asked), "scored": len(f1), "per_question": per}}


def oracle(unit, dir):
    return {"answer": json.dumps({i: gold(q) for i, q in questions(unit).items()})}


HANDLERS = {"describe": describe, "units": units, "setup": setup, "produce": produce, "score": score, "oracle": oracle, "answer": answer}
for line in sys.stdin:
    try:
        req = json.loads(line)
        out = HANDLERS[req.pop("op")](**req)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out), flush=True)
