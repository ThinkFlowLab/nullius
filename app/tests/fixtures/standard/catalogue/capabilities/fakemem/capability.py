import json, sys
kept = []
for line in sys.stdin:
    req = json.loads(line)
    if req["op"] == "add":
        kept.append(req["session"]["text"]); out = {}
    elif req["op"] == "recall":
        words = set(req["question"].lower().strip("?").split())
        out = {"memory": " ".join(t for t in kept if words & set(t.lower().strip(".").split()))}
    else:
        out = {"error": "unknown op"}
    print(json.dumps(out), flush=True)
