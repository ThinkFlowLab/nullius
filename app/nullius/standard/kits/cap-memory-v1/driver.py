"""Kit for cap.memory/v1: add sessions, recall for a question, the fact must come back."""
from nullius.standard.jsonl import serve

FILLER = ["We discussed the train timetable for the weekend.", "The weather was cold and grey all morning.",
          "I watched a documentary about volcanoes.", "Dinner plans changed to a quiet night in.",
          "The neighbours painted their fence blue."]
CHECKS = {
    "m1": ("My sister Ines moved to Lisbon in March.", "Where did Ines move to?", "Lisbon"),
    "m2": ("The wifi password at the cabin is tern-river-9.", "What is the wifi password at the cabin?", "tern-river-9"),
}


def units(dataset):
    return {"units": list(CHECKS)}


def setup(unit, dir):
    fact, q, _ = CHECKS[unit]
    sessions = [{"id": f"s{i + 1}", "date": f"2026-0{i + 1}-01", "turns": [{"role": "user", "text": t}]}
                for i, t in enumerate(FILLER[:1] + [fact] + FILLER[1:])]
    for s in sessions:
        s["text"] = " ".join(t["text"] for t in s["turns"])
    return {"memory": {"add": sessions, "recall": [{"id": unit, "question": q}]}}


def produce(unit, dir, recalled, **kw):
    return {"answer": recalled[0]["memory"] if recalled else ""}


def score(unit, dir, transcript, answer):
    ok = CHECKS[unit][2].lower() in (answer or "").lower()
    return {"reward": 1.0 if ok else 0.0, "passed": ok, "detail": {"recalled": (answer or "")[:500]}}


def oracle(unit, dir):
    return {"answer": CHECKS[unit][0]}                   # the fact itself, as it was added


serve({"units": units, "setup": setup, "produce": produce, "score": score, "oracle": oracle})
