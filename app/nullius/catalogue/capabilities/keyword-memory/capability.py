"""The reference memory: BM25 over whole sessions, the top few recalled.

cap.memory/v1 in JSON lines: ``add {session}`` keeps a session
(``{id, date, turns: [{role, text}], text}``), ``recall {question}`` answers
``{memory}`` - the best-matching sessions, each with its date, as text.
"""
import json
import math
import re
import sys

TOP = 3
WORD = re.compile(r"[a-z0-9]+")
kept = []


def words(text):
    return WORD.findall((text or "").lower())


def recall(question):
    q = set(words(question))
    if not kept or not q:
        return ""
    n = len(kept)
    avg = sum(len(d["words"]) for d in kept) / n
    df = {w: sum(1 for d in kept if w in d["set"]) for w in q}
    def bm25(d):
        s = 0.0
        for w in q:
            if w in d["set"]:
                tf = d["words"].count(w)
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d["words"]) / avg))
        return s
    best = sorted(kept, key=bm25, reverse=True)[:TOP]
    return "\n\n".join(f"[{d['session'].get('date') or d['session'].get('id')}]\n{d['text']}" for d in best if bm25(d) > 0)


for line in sys.stdin:
    try:
        req = json.loads(line)
        if req["op"] == "add":
            s = req["session"]
            text = s.get("text") or "\n".join(f"{t.get('role')}: {t.get('text')}" for t in s.get("turns") or [])
            ws = words(text)
            kept.append({"session": s, "text": text, "words": ws, "set": set(ws)})
            out = {}
        elif req["op"] == "recall":
            out = {"memory": recall(req["question"])}
        else:
            out = {"error": f"unknown op {req['op']!r}"}
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out), flush=True)
