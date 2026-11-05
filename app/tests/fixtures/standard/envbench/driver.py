"""The environment bench's driver: each unit names its environment, and its
score reads what the agent's probe saw there (probe.txt in the workspace)."""
import json
import os
import sys

ENVIRONMENTS = {"tool": "tool", "licensed": "licensed", "plain": None}


def probe(dir):
    try:
        with open(os.path.join(dir, "probe.txt")) as f:
            return dict(line.strip().split("=", 1) for line in f if "=" in line)
    except OSError:
        return {}


def handle(op, **kw):
    if op == "units":
        return {"units": list(ENVIRONMENTS)}
    if op == "setup":
        env = ENVIRONMENTS[kw["unit"]]
        return dict({"prompt": "probe " + kw["unit"]}, **({"environment": env} if env else {}))
    if op == "score":
        seen = probe(kw["dir"])
        scorer = bool(os.environ.get("FAKE_SCORER_TOKEN"))       # to: driver - here, never in the agent's
        unit = kw["unit"]
        if unit == "tool":
            ok = seen.get("jq") == "yes"
        elif unit == "licensed":
            ok = seen.get("secret") == "yes" and seen.get("scorer") == "no" and scorer \
                and seen.get("licfile") == "yes" and seen.get("server") == "yes" and seen.get("public") != "yes"
        else:
            ok = seen.get("secret") == "no" and seen.get("scorer") == "no" and seen.get("marker") == "no" \
                and seen.get("licfile") == "no" and seen.get("server") == "no"
        return {"reward": 1.0 if ok else 0.0, "passed": ok, "detail": dict(seen, driver_has_scorer_token=scorer)}
    raise ValueError(op)


for line in sys.stdin:
    req = json.loads(line)
    try:
        out = handle(req.pop("op"), **req)
    except Exception as e:
        out = {"error": repr(e)}
    print(json.dumps(out), flush=True)
