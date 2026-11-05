import json, sys, urllib.request
HISTORY = [{"id": "s1", "text": "We talked about the weather."}, {"id": "s2", "text": "My bike is green."},
           {"id": "s3", "text": "Lunch was soup."}]
QS = {"q1": ("What colour is the bike?", "green"), "q2": ("What was lunch?", "soup")}
def handle(op, **kw):
    if op == "units":
        return {"units": list(QS)}
    if op == "setup":
        return {"memory": {"add": HISTORY, "recall": [{"id": kw["unit"], "question": QS[kw["unit"]][0]}]}}
    if op == "produce":
        mem = kw["recalled"][0]["memory"]
        req = urllib.request.Request(kw["base_url"] + "/chat/completions", headers={"Content-Type": "application/json", "Authorization": "Bearer " + kw["key"]},
                                     data=json.dumps({"model": kw["model"], "messages": [{"role": "user", "content": mem}]}).encode())
        return {"answer": json.loads(urllib.request.urlopen(req).read())["choices"][0]["message"]["content"]}
    if op == "score":
        hit = QS[kw["unit"]][1] in (kw.get("answer") or "")
        return {"reward": 1.0 if hit else 0.0}
    raise ValueError(op)
for line in sys.stdin:
    req = json.loads(line)
    try:
        out = handle(req.pop("op"), **req)
    except Exception as e:
        out = {"error": repr(e)}
    print(json.dumps(out), flush=True)
