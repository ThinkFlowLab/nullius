"""A real computer server over an inert desktop, for retry and phase-boundary checks."""
import atexit
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from nullius.standard import computer_port as port, jsonl


class Desktop:
    def python(self, code):
        return {"status": "success", "output": "made\n", "error": "", "returncode": 0}

    def capture(self):
        return b"fixture image"

    def password(self):
        return ""


def desktop(socket, log, inert=False):
    bridge = port.Bridge(Desktop(), 2, 0, (1920, 1080), "/tmp/agent")
    if inert:
        Path(socket).touch()
        server = port.Server.__new__(port.Server)
        server._state(bridge, socket, log)
        print("ready", flush=True)
        for line in sys.stdin:
            for tool in ("screenshot", "click"):
                server.record({"tool": tool, "start": time.time(), "end": time.time(), "isError": False})
            print(json.dumps(bridge.info()), flush=True)
        Path(socket).unlink()
        return
    server = port.Server(bridge, socket, log)

    def loop():
        try:
            for _ in range(2):
                _, actions = bridge.predict("", None)
                for action in actions:
                    pass
        finally:
            bridge.finish()

    worker = threading.Thread(target=loop)
    worker.start()
    print("ready", flush=True)
    try:
        sys.stdin.read()
    finally:
        bridge.end_turn()
        worker.join(5)
        server.close()


held = None


def end(unit=None, dir=None):
    global held
    if held is not None:
        child, folder, trial = held
        child.stdin.close()
        try:
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()
            shutil.rmtree(folder)
            (trial / "ended.json").write_text(json.dumps({"time": time.time(), "pid": child.pid}))
            held = None
    return {}


def setup(unit, dir, **told):
    global held
    end()
    trial = Path(dir).parent
    folder = tempfile.mkdtemp(prefix="cm-")
    socket = os.path.join(folder, "s")
    log = trial / "verifier/computer-calls.jsonl"
    log.parent.mkdir(exist_ok=True)
    child = subprocess.Popen([sys.executable, __file__, "desktop", socket, str(log),
                              *(["inert"] if "inert" in sys.argv else [])],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    held = child, folder, trial
    if child.stdout.readline().strip() != "ready":
        raise RuntimeError("desktop did not start")
    (trial / "setup.json").write_text(json.dumps(dict(told, pid=child.pid, driver=os.getpid(),
                                                     socket=socket, time=time.time())))
    return {"prompt": "Look and click.", "agent_seconds": 20,
            "mcp": [{"name": "computer", "server": "computer", "args": [socket]}]}


def act(**_):
    child = held[0]
    child.stdin.write("act\n")
    child.stdin.flush()
    return json.loads(child.stdout.readline())


if __name__ == "__main__":
    if sys.argv[1:2] == ["desktop"]:
        desktop(*sys.argv[2:])
    else:
        atexit.register(end)
        jsonl.serve({"units": lambda **_: {"units": ["one"]}, "setup": setup, "end": end, "act": act,
                     "score": lambda **_: {"reward": 1.0, "passed": True}})
