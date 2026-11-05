"""Kit for agent.conversation/v1: the second turn needs the first."""
from nullius.standard.jsonl import serve

CODES = {"c1": "heron-42", "c2": "maple-17"}


def units(dataset):
    return {"units": list(CODES)}


def setup(unit, dir):
    return {"prompt": f"Remember this code word: {CODES[unit]}. Reply only with: noted. Do not use any tools."}


def turn(unit, dir, transcript):
    if sum(1 for t in transcript if t["role"] == "user") >= 2:
        return {"done": True}
    return {"message": "What was the code word I gave you? Reply with the code word only."}


def score(unit, dir, transcript, answer):
    asked = sum(1 for t in transcript if t["role"] == "user")
    ok = asked == 2 and CODES[unit] in (answer or "")
    return {"reward": 1.0 if ok else 0.0, "passed": ok, "detail": {"turns": asked, "said": (answer or "")[:200]}}


serve({"units": units, "setup": setup, "turn": turn, "score": score})
