import json, os, sys
PROMPTS = {"good": "write answer.txt 42", "wrong": "write answer.txt 41", "slow": "hang", "model": "model hi"}
def handle(op, **kw):
    if op == "units":
        units = list(PROMPTS)
        if kw.get("dataset") == "responding":
            units.remove("slow")
        return {"units": units}
    if op == "setup":
        if kw["unit"] == "broken":
            raise ValueError("no such unit")
        return {"prompt": PROMPTS[kw["unit"]]}
    if op == "score":
        p = os.path.join(kw["dir"], "answer.txt")
        ok = os.path.isfile(p) and open(p).read().strip() == "42"
        return {"reward": 1.0 if ok else 0.0, "passed": ok, "detail": {"answer": kw.get("answer")}}
    raise ValueError(op)
for line in sys.stdin:
    req = json.loads(line)
    try:
        out = handle(req.pop("op"), **req)
    except Exception as e:
        out = {"error": repr(e)}
    print(json.dumps(out), flush=True)
