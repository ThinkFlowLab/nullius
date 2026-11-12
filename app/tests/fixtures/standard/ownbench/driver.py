"""The own-image bench's driver: unit "one" runs in its own image, prepared
as root before anyone works in it, and is checked in it; unit "base" names no image but asks for a check, which only
an own image can give (the run refuses it)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def handle(op, **kw):
    if op == "units":
        return {"units": ["one", "base"]}
    if op == "setup":
        verify = {"copy": {"tasks/one/tests": "/tests"}, "run": ["sh", "/tests/check.sh"], "collect": "/logs/out", "seconds": 60,
                  "network": True}
        if kw["unit"] == "base":
            return {"prompt": "work", "environment": "base", "verify": verify}
        return {"prompt": "work", "environment": "task", "image": {"build": "tasks/one/environment", "memory_mb": 256},
                "prepare": {"run": ["sh", "-c", "id -u > /prepared-by"], "seconds": 60}, "verify": verify}
    if op == "score":
        v = kw.get("verified") or {}
        try:
            with open(os.path.join(v["dir"], "reward.txt")) as f:
                reward = float(f.read().strip())
        except (KeyError, OSError, ValueError):
            reward = 0.0
        return {"reward": reward, "passed": reward == 1.0, "detail": {"exit": v.get("exit"), "output": v.get("output")}}
    if op == "oracle":
        return {"answer": "", "solve": {"copy": {"tasks/one/solution": "/solution"}, "run": ["sh", "/solution/solve.sh"]}}
    raise KeyError(op)


for line in sys.stdin:
    try:
        req = json.loads(line)
        out = handle(req.pop("op"), **req)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out), flush=True)
