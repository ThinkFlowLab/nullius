"""Kit for agent.task/v1: the answer is the agent's final message."""
from nullius.standard.jsonl import serve

WORDS = {"t1": "lantern", "t2": "quartz", "t3": "meadow"}


def units(dataset):
    return {"units": list(WORDS)}


def setup(unit, dir):
    return {"prompt": f"Reply with exactly one word, the word {WORDS[unit]}, and nothing else. Do not use any tools."}


def score(unit, dir, transcript, answer):
    said = (answer or "").strip().strip(".!*`\"'").lower()
    ok = said == WORDS[unit]
    return {"reward": 1.0 if ok else 0.0, "passed": ok, "detail": {"expected": WORDS[unit], "said": (answer or "")[:200]}}


def oracle(unit, dir):
    return {"answer": WORDS[unit]}


serve({"units": units, "setup": setup, "score": score, "oracle": oracle})
