"""OSWorld1's agent side: the driver's setup starts the runner, which builds the fake DesktopEnv and calls the upstream's
own run_single_example, as it is, with the computer port's bridge as the loop's agent; a scripted MCP client in the
agent's place joins the runner's server through the port's pipe, as the agent's session would; score ends the turn and
reads the upstream's result.txt.

No VM, no docker, no network, no model. It needs the upstream's loop (lib_run_single.py, the two modules it imports
and the agents' package one of its lines imports, from $OSWORLD_DIR, a checkout of the upstream) and a Python that
holds its packages ($OSWORLD_PYTHON); without them each test is skipped, with the reason. The rest of the upstream is a
folder of the test's own: the fake DesktopEnv (fixtures/osworld1_fake_desktop), one task, its dataset and an empty file
where the docker provider keeps the VM image, so that no image is fetched. The loop's fixed waits are recorded, not
slept (the fixture's sitecustomize.py).
"""
from __future__ import annotations

import base64
import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from nullius.standard import jsonl

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(APP, "nullius", "catalogue", "benchmarks", "osworld1")
FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "osworld1_fake_desktop")
OSWORLD = os.environ.get("OSWORLD_DIR", "")
PYTHON = os.environ.get("OSWORLD_PYTHON", "")
LOOP = ("lib_run_single.py", "lib_results_logger.py", "lib_run_errors.py", "mm_agents")
WHY = ("OSWORLD_DIR does not name a checkout of the upstream" if not all(os.path.exists(os.path.join(OSWORLD, n)) for n in LOOP)
       else "OSWORLD_PYTHON does not name the upstream's Python" if not os.path.isfile(PYTHON)
       else "")
TASK = {"id": "task-1", "instruction": "Open the terminal.", "config": [], "evaluator": {"func": "check"}}
UNIT = "os__task-1"
CLICK = "pyautogui.click(10, 20, clicks=1, button='left')"


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


def gone(pid: int, seconds: float = 15.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


def processes_under(folder: str, seconds: float = 15.0, but=()) -> list:
    """The processes whose command line names ``folder`` or whose working folder is in it, but ``but``, once
    ``seconds`` have passed with some left."""
    deadline = time.time() + seconds
    while True:
        found = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            if int(pid) in but or int(pid) == os.getpid():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    named = folder.encode() in f.read()
                named = named or os.path.realpath(os.readlink(f"/proc/{pid}/cwd")).startswith(os.path.realpath(folder))
            except OSError:
                continue
            if named and alive(int(pid)):
                found.append(int(pid))
        if not found or time.time() > deadline:
            return found
        time.sleep(0.2)


class Session:
    """A scripted MCP client in the agent's place: the setup's server computer started as the agent's session starts a
    stdio server (the port's pipe), JSON-RPC lines on its stdin and stdout, each answer waited for a bounded time."""

    def __init__(self, setup: dict, cwd: str):
        server = setup["mcp"][0]
        self.pipe = subprocess.Popen([PYTHON, *server["args"]], cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.got, self.last = b"", 0

    def ask(self, method: str, params: dict | None = None, seconds: float = 30.0) -> dict:
        self.last += 1
        message = {"jsonrpc": "2.0", "id": self.last, "method": method, **({"params": params} if params else {})}
        self.pipe.stdin.write((json.dumps(message) + "\n").encode())
        self.pipe.stdin.flush()
        deadline = time.monotonic() + seconds
        while True:
            while b"\n" in self.got:
                line, _, self.got = self.got.partition(b"\n")
                reply = json.loads(line)
                if reply.get("id") == self.last:
                    return reply
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.pipe.stdout], [], [], left)[0]:
                raise AssertionError(f"no answer to {method} within {seconds} s")
            chunk = os.read(self.pipe.stdout.fileno(), 1 << 20)
            if not chunk:
                raise AssertionError("the session's pipe ended")
            self.got += chunk

    def call(self, tool: str, **arguments) -> dict:
        return self.ask("tools/call", {"name": tool, "arguments": arguments})["result"]

    def close(self) -> None:
        self.pipe.stdin.close()
        try:
            self.pipe.wait(timeout=30)
        finally:
            if self.pipe.poll() is None:
                self.pipe.kill()
                self.pipe.wait()
            self.pipe.stdout.close()


def value(result: dict) -> dict:
    return json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))


@unittest.skipIf(WHY, WHY)
class AgentSide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osworld1-agent-")
        up = os.path.join(self.tmp, "upstream")
        os.makedirs(os.path.join(up, "evaluation_examples", "examples", "os"))
        os.makedirs(os.path.join(up, "docker_vm_data"))
        open(os.path.join(up, "docker_vm_data", "Ubuntu.qcow2"), "w").close()
        for name in LOOP:
            os.symlink(os.path.join(OSWORLD, name), os.path.join(up, name))
        shutil.copytree(os.path.join(FAKE, "desktop_env"), os.path.join(up, "desktop_env"))
        shutil.copyfile(os.path.join(FAKE, "sitecustomize.py"), os.path.join(up, "sitecustomize.py"))
        self.task = dict(TASK)
        self.dataset = os.path.join(up, "evaluation_examples", "test_one.json")
        with open(self.dataset, "w") as f:
            json.dump({"os": ["task-1"]}, f)
        self.up, self.log = up, os.path.join(self.tmp, "desktop.jsonl")
        self.trial = os.path.join(self.tmp, "job", "os__task-1__u0000")
        self.ws = os.path.join(self.trial, "workspace")
        os.makedirs(self.ws)
        self.no_programs = os.path.join(self.tmp, "no-programs")        # a PATH with no docker: none is ever run
        os.mkdir(self.no_programs)
        self.drivers, self.sessions = [], []

    def tearDown(self):
        for s in self.sessions:
            if s.pipe.poll() is None:
                s.pipe.kill()
                s.pipe.wait()
        for d in self.drivers:
            d.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def driver(self, **options) -> jsonl.Proc:
        with open(os.path.join(self.up, "evaluation_examples", "examples", "os", "task-1.json"), "w") as f:
            json.dump(self.task, f)
        env = {"PATH": self.no_programs, "HOME": self.tmp, "TMPDIR": tempfile.gettempdir(), "LANG": "C.UTF-8",
               "NULLIUS_UPSTREAM": self.up, "NULLIUS_DATASET": self.dataset, "PYTHONPATH": APP,
               "PYTHONDONTWRITEBYTECODE": "1", "FAKE_DESKTOP_LOG": self.log,
               **{"NULLIUS_OPTION_" + k.upper(): v for k, v in options.items()}}
        d = jsonl.Proc([PYTHON, os.path.join(ENTRY, "driver.py"), os.path.join(ENTRY, "agents.json"),
                        os.path.join(ENTRY, "nop.py"), os.path.join(ENTRY, "runner.py")],
                       self.tmp, env, os.path.join(self.tmp, "driver.log")).start()
        self.drivers.append(d)
        return d

    def setup(self, d: jsonl.Proc, ws=None) -> dict:
        return d.request("setup", unit=UNIT, dir=ws or self.ws, participant="agent")

    def session(self, setup: dict) -> Session:
        s = Session(setup, self.ws)
        self.sessions.append(s)
        s.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
        self.assertEqual(self.drivers[-1].request("task_given", unit=UNIT, dir=self.ws), {})   # as the run does
        return s

    def events(self, kind: str) -> list:
        with open(self.log) as f:
            return [e for e in map(json.loads, f) if e["event"] == kind]

    def task_dir(self) -> str:
        return os.path.join(self.trial, ".work", "osworld", "results", "os", "task-1")

    def traj(self) -> list:
        with open(os.path.join(self.task_dir(), "traj.jsonl")) as f:
            return [json.loads(line) for line in f]

    def runner(self) -> int:
        with open(os.path.join(self.trial, ".work", "osworld", "runner.json")) as f:
            return json.load(f)["pid"]

    def test_an_agents_calls_drive_the_upstreams_loop_to_done(self):
        d = self.driver(max_steps="5", pause="0.1")
        setup = self.setup(d)
        self.assertEqual(setup["prompt"], TASK["instruction"] + "\n\nIf you need a password for sudo, the password of "
                                                                "the computer is 'password'.")
        self.assertEqual([(m["name"], m["server"]) for m in setup["mcp"]], [("computer", "computer")])
        self.assertTrue(setup["mcp"][0]["args"][0].endswith(os.path.join("nullius", "standard", "computer_pipe.py")))
        s = self.session(setup)
        self.assertEqual(base64.b64decode(s.call("screenshot")["content"][0]["data"])[:4], b"\x89PNG")
        self.assertEqual(value(s.call("click", x=10, y=20))["steps_left"], 4)
        self.assertEqual(value(s.call("step", actions=["pyautogui.press('enter')"], response="enter"))["steps_left"], 3)
        self.assertEqual(value(s.call("done")), {"steps_left": 2, "done": True})
        s.close()
        score = d.request("score", unit=UNIT, dir=self.ws, transcript=[], answer="done")
        self.assertEqual(score, {"reward": 1.0, "detail": {"result": 1.0, "tool_calls": 4,
                                                           "by": "runner.py, run_single_example, DesktopEnv.evaluate"}})
        self.assertEqual([(t["step_num"], t["action"], t["response"]) for t in self.traj()],
                         [(1, CLICK, ""), (2, "pyautogui.press('enter')", "enter"), (3, "DONE", "")])
        self.assertEqual([(e["action"], e["pause"]) for e in self.events("step")],
                         [(CLICK, 0.1), ("pyautogui.press('enter')", 0.1), ("DONE", 0.1)])
        self.assertEqual([e["seconds"] for e in self.events("wait")], [60, 20])
        with open(os.path.join(self.trial, "verifier", "osworld", "computer-calls.jsonl")) as f:
            self.assertEqual([json.loads(line)["tool"] for line in f], ["screenshot", "click", "step", "done"])
        self.assertTrue(gone(self.runner()))
        self.assertTrue(self.events("close"))
        self.assertEqual(processes_under(self.tmp, but=(d.p.pid,)), [])

    def test_a_turn_without_done_is_evaluated_as_the_desktop_is(self):
        d = self.driver(max_steps="4", pause="0.1")
        s = self.session(self.setup(d))
        s.call("click", x=10, y=20)
        s.close()
        score = d.request("score", unit=UNIT, dir=self.ws, transcript=[], answer="")
        self.assertEqual(score["reward"], 0.0)                 # the fake gives 1 only to a task ended by DONE
        self.assertEqual([t["action"] for t in self.traj()], [CLICK])
        self.assertEqual([e["last"] for e in self.events("evaluate")], [CLICK])

    def test_fail_is_the_last_action_the_evaluator_reads(self):
        self.task["evaluator"] = {"func": "infeasible"}
        d = self.driver(max_steps="3", pause="0.1")
        s = self.session(self.setup(d))
        self.assertEqual(value(s.call("fail", reason="no such menu"))["done"], True)
        s.close()
        self.assertEqual(d.request("score", unit=UNIT, dir=self.ws, transcript=[], answer="")["reward"], 1.0)
        self.assertEqual([t["action"] for t in self.traj()], ["FAIL"])

    def test_a_step_with_an_end_ends_the_task_in_that_step(self):
        self.task["evaluator"] = {"func": "infeasible"}
        d = self.driver(max_steps="3", pause="0.1")
        s = self.session(self.setup(d))
        self.assertEqual(value(s.call("step", actions=[CLICK], response="no such menu", end="fail")),
                         {"steps_left": 2, "done": True})
        self.assertEqual(s.call("click", x=1, y=1)["content"][0]["text"], "the task has ended (fail)")
        s.close()
        score = d.request("score", unit=UNIT, dir=self.ws, transcript=[], answer="")
        self.assertEqual(score["reward"], 1.0)                 # the evaluator reads FAIL as the last action
        self.assertEqual([(t["step_num"], t["action"], t["response"]) for t in self.traj()],
                         [(1, CLICK, "no such menu"), (1, "FAIL", "no such menu")])

    def test_the_steps_option_is_the_loops_budget(self):
        d = self.driver(max_steps="2", pause="0.1")
        s = self.session(self.setup(d))
        s.call("click", x=1, y=1)
        self.assertEqual(value(s.call("click", x=2, y=2)), {"steps_left": 0, "done": True})
        self.assertEqual(s.call("click", x=3, y=3)["content"][0]["text"], "the steps are used up")
        s.close()
        d.request("score", unit=UNIT, dir=self.ws, transcript=[], answer="")
        self.assertEqual(len(self.traj()), 2)

    def test_a_result_txt_planted_in_the_workspace_is_not_read(self):
        self.task["evaluator"] = {"func": "infeasible"}                             # DONE scores 0
        d = self.driver(pause="0.1")
        s = self.session(self.setup(d))
        planted = os.path.join(self.ws, "results", "os", "task-1")
        os.makedirs(planted)
        with open(os.path.join(planted, "result.txt"), "w") as f:
            f.write("1.0\n")
        s.call("done")
        s.close()
        self.assertEqual(d.request("score", unit=UNIT, dir=self.ws, transcript=[], answer="done")["reward"], 0.0)

    def test_the_end_of_a_unit_that_never_reached_score_ends_its_runner(self):
        d = self.driver()
        setup = self.setup(d)
        runner = self.runner()
        folder = os.path.dirname(setup["mcp"][0]["args"][1])
        self.assertEqual(d.request("end", unit=UNIT, dir=self.ws), {})
        self.assertTrue(gone(runner, 1))                                            # gone when end answers
        self.assertFalse(os.path.exists(folder))
        self.assertEqual(d.request("end", unit=UNIT, dir=self.ws), {})              # a second end does nothing
        with self.assertRaisesRegex(jsonl.Failed, "no runner of this driver's for this unit"):
            d.request("score", unit=UNIT, dir=self.ws, transcript=[], answer="")

    def test_the_next_units_setup_ends_a_runner_left(self):
        d = self.driver()
        self.setup(d)
        first = self.runner()
        other = os.path.join(self.tmp, "job", "os__task-1__u0001", "workspace")
        os.makedirs(other)
        self.setup(d, ws=other)
        self.assertTrue(gone(first))
        with open(os.path.join(os.path.dirname(other), ".work", "osworld", "runner.json")) as f:
            second = json.load(f)["pid"]
        d.close()                                                                   # the run ends: stdin closed
        self.assertTrue(gone(second, 60))
        self.assertEqual(processes_under(self.tmp), [])

    def test_a_driver_killed_with_sigkill_leaves_no_runner(self):
        d = self.driver()
        setup = self.setup(d)
        runner = self.runner()
        os.killpg(d.p.pid, signal.SIGKILL)                                          # no clean-up of the driver's own
        d.p.wait(timeout=10)
        self.assertTrue(gone(runner, 60), "the runner outlived its driver")
        self.assertEqual(processes_under(self.tmp), [])
        self.assertFalse(os.path.exists(os.path.dirname(setup["mcp"][0]["args"][1])))   # its socket's folder

    def test_the_password_file_is_private_and_read_once(self):
        d = self.driver(pause="0.1")
        setup = self.setup(d)
        folder = os.path.dirname(setup["mcp"][0]["args"][1])
        self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)
        self.assertEqual(sorted(os.listdir(folder)), ["s"])                       # ready.json read and removed
        s = self.session(setup)
        s.call("done")
        s.close()
        d.request("score", unit=UNIT, dir=self.ws, transcript=[], answer="done")
        self.assertFalse(os.path.exists(folder))

    def test_a_unit_with_no_agent_starts_no_runner(self):
        d = self.driver()
        self.assertEqual(d.request("setup", unit=UNIT, dir=self.ws), {"prompt": TASK["instruction"]})
        self.assertEqual(d.request("setup", unit=UNIT, dir=self.ws, participant="control"), {"prompt": TASK["instruction"]})
        self.assertFalse(os.path.exists(os.path.join(self.trial, ".work")))


if __name__ == "__main__":
    unittest.main()
