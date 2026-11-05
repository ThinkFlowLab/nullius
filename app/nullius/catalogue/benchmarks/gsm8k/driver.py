"""GSM8K's driver for the Nullius standard (JSON lines on stdin and stdout).

units  - one per problem of the test file, named by its line number ("0001")
setup  - the question goes in the prompt; the workspace stays empty
score  - the upstream's own extract_answer and is_correct, imported from
         grade_school_math/dataset.py at the tested commit ($NULLIUS_UPSTREAM),
         applied to the agent's last message
oracle - the reference control: the problem's own worked solution, the test
         file's "answer" field, which ends in '#### <number>'
answer - the original setup: the problem asked of a model in one call through
         the unit's relay, in the format the paper's solutions end with
         ('#### <number>'), the reply scored as any agent's last message is.
         The paper's own answerers were its fine-tuned models, never released,
         so the setup takes any model; the paper decoded greedily, which the
         temperature option asks for (default: the provider's own, since a
         model that reasons refuses one)

The upstream's module imports torch (for a training dataset class the score
never uses), so the driver runs in the environment of its own that
standard.json declares (driver.install), never in Nullius's.

The reference solution is never in the workspace: it is read from the
dataset only when the score is taken.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.environ["NULLIUS_UPSTREAM"], "grade_school_math"))
import dataset as upstream                      # noqa: E402  grade_school_math/dataset.py, as published

PROBLEMS = {}


def load():
    if not PROBLEMS:
        with open(os.environ["NULLIUS_DATASET"]) as f:
            for n, line in enumerate(f, 1):
                if line.strip():
                    PROBLEMS[f"{n:04d}"] = json.loads(line)
    return PROBLEMS


TEMPERATURE = os.environ.get("NULLIUS_OPTION_TEMPERATURE", "default")


def units(dataset, setup=None):
    return {"units": list(load())}


def prompt(unit):
    q = load()[unit]["question"]
    return (f"Solve this grade-school math word problem. Work it out step by step, then end your reply with "
            f"a final line of the form `#### <number>`: the answer as a plain number, with no units and no "
            f"other text on that line.\n\n{q}")


def setup(unit, dir):
    return {"prompt": prompt(unit)}


def answer(unit, dir, setup, model, base_url, key):
    """The original: one chat call, the same words an agent is given, no agent loop around the model."""
    if setup != "original":
        raise ValueError(f"GSM8K has no {setup} setup")
    body = {"model": model, "messages": [{"role": "user", "content": prompt(unit)}]}
    if TEMPERATURE != "default":
        body["temperature"] = float(TEMPERATURE)
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=600) as r:
        reply = json.load(r)
    text = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not text.strip():
        raise RuntimeError("the reply had no text")
    return {"answer": text, "model": model}


def score(unit, dir, transcript, answer, setup=None, **_):
    problem = load()[unit]
    if upstream.extract_answer(problem["answer"]) == upstream.INVALID_ANS:
        raise ValueError(f"problem {unit} has no '####' reference answer")
    ok = upstream.is_correct(answer or "", problem)
    return {"reward": 1.0 if ok else 0.0, "passed": ok,
            "detail": {"expected": upstream.extract_answer(problem["answer"]), "extracted": upstream.extract_answer(answer or ""),
                       "answer": (answer or "")[-4000:]}}


def oracle(unit, dir):
    return {"answer": load()[unit]["answer"]}


HANDLERS = {"units": units, "setup": setup, "answer": answer, "score": score, "oracle": oracle}
for line in sys.stdin:
    try:
        req = json.loads(line)
        out = HANDLERS[req.pop("op")](**req)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out), flush=True)
