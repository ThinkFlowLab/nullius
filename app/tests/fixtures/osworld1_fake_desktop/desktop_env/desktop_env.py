"""A fake of the upstream's DesktopEnv for the computer port's tests: no VM, no docker, no network.

It does what run_single_example and a Desktop over the upstream's controllers use of DesktopEnv: reset, _get_obs,
step, evaluate, close, the controller's execute_python_command, get_file and get_screenshot, the setup controller's
upload, the used mark and the guest's password. Guest code is not run: execute_python_command answers with the code's length and its first line,
and an uploaded file is kept in memory, readable with get_file. Every event goes to $FAKE_DESKTOP_LOG, one JSON line
each, with whether it ran inside env.step. evaluate gives 1 when the last action is the one the task expects (FAIL
for an infeasible task, DONE otherwise), 0 otherwise.
"""
import base64
import json
import os

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


class _Controller:
    def __init__(self, env):
        self.env = env

    def execute_python_command(self, command):
        first = command.splitlines()[0] if command else ""
        self.env.record("execute", first=first[:200])
        if command.rstrip().endswith("print('made')"):                 # the computer port making the agent's folder
            return {"status": "success", "output": "made\n", "error": "", "returncode": 0}
        return {"status": "success", "output": f"{len(command)} {first}", "error": "", "returncode": 0}

    def get_file(self, path, strict=False):
        self.env.record("get_file", path=path)
        return self.env.files.get(path)

    def get_screenshot(self):
        return PNG

    def start_recording(self):
        self.env.record("start_recording")

    def end_recording(self, dest):
        self.env.record("end_recording")
        with open(dest, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42")


class _SetupController:
    def __init__(self, env):
        self.env = env

    def _upload_file_setup(self, files):
        for f in files:
            with open(f["local_path"], "rb") as src:
                self.env.files[f["path"]] = src.read()
        self.env.record("upload", paths=[f["path"] for f in files])


class DesktopEnv:
    vm_ip = "127.0.0.1"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.client_password = kwargs.get("client_password") or "password"
        self.controller, self.setup_controller = _Controller(self), _SetupController(self)
        self.files, self.actions = {}, []
        self.task, self.instruction, self.in_step = {}, None, False
        self.is_environment_used = False
        self.record("started", path_to_vm=kwargs.get("path_to_vm"))

    def record(self, event, **fields):
        log = os.environ.get("FAKE_DESKTOP_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"event": event, "in_step": self.in_step, **fields}) + "\n")

    def reset(self, task_config=None):
        self.task = task_config or {}
        self.instruction = self.task.get("instruction")
        self.actions = []
        self.record("reset", task=self.task.get("id"))

    def _get_obs(self):
        return {"screenshot": PNG}

    def step(self, action, pause=2):
        self.actions.append(action)
        done = action in ("DONE", "FAIL")
        self.record("step", action=action, pause=pause)
        if not done and action not in ("WAIT",):
            self.in_step = True
            try:
                self.controller.execute_python_command(action)
            finally:
                self.in_step = False
        return self._get_obs(), 0, done, {}

    def evaluate(self):
        expected = "FAIL" if (self.task.get("evaluator") or {}).get("func") == "infeasible" else "DONE"
        last = self.actions[-1] if self.actions else None
        self.record("evaluate", last=last)
        return 1.0 if last == expected else 0.0

    def close(self):
        self.record("close", used=self.is_environment_used)
