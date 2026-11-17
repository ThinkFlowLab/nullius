"""OSWorld-V2's null control: the upstream's run_single_example on one task with an agent that does nothing.

    nop.py VM_IMAGE PASSWORD EXAMPLES DOMAIN TASK_ID RESULT_DIR
    nop.py --instruction EXAMPLES DOMAIN TASK_ID OUT

The agent answers DONE at its one step, and evaluate() on the untouched VM goes
to RESULT_DIR/result.txt. --instruction writes the task's instruction and
whether it asks for a proxy to OUT as JSON. A task with no class file is an
error. The setup gets SETUP_ATTEMPTS; a failed one goes to
RESULT_DIR/setup_failed.json by exception type only, since the message could
quote the gated task."""
import argparse
import json
import logging
import os
import signal
import sys

from task_loader import find_task_class_path, load_task_config, resolve_task_json_path

SETUP_ATTEMPTS = 2
SETUP_FAILED = "setup_failed.json"


class DoNothing:
    def reset(self, *args, **kwargs):
        pass

    def predict(self, instruction, obs):
        return "", ["DONE"]


def _stop(signum, frame):
    sys.exit(1)


def _fail(said):
    logging.error(said)
    sys.exit(1)


def _task(examples, domain, eid):
    if not find_task_class_path(eid, examples, domain, "v2"):
        _fail(f"no task class for {domain}__{eid} under {examples}")
    try:
        task = load_task_config(resolve_task_json_path(task_id=eid, base_dir=examples, domain=domain, eval_version="v2"),
                                task_id=eid, base_dir=examples, domain=domain, eval_version="v2")
        instruction = task["instruction"]
    except (Exception, SystemExit) as e:
        _fail(f"the task class for {domain}__{eid} did not load: {type(e).__name__}"
              + (", which may need a service this entry does not set up (the mock websites)"
                 if isinstance(e, ValueError) else ""))
    return task, instruction


logging.basicConfig(level=logging.INFO, format="[%(asctime)s %(levelname)s %(name)s] %(message)s")
if sys.argv[1:2] == ["--instruction"]:
    examples, domain, eid, out = sys.argv[2:6]
    task, instruction = _task(examples, domain, eid)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"instruction": instruction, "proxy": bool(task.get("proxy", False))}, f)
    sys.exit(0)
import lib_run_single
from desktop_env import desktop_env
from desktop_env.desktop_env import DesktopEnv, EnvironmentSetupError

vm, password, examples, domain, eid, out = sys.argv[1:7]
example, instruction = _task(examples, domain, eid)
signal.signal(signal.SIGTERM, _stop)
desktop_env.MAX_RETRIES = SETUP_ATTEMPTS
env = None
try:
    env = DesktopEnv(path_to_vm=vm, action_space="pyautogui", provider_name="docker", screen_size=(1920, 1080),
                     headless=True, os_type="Ubuntu", require_a11y_tree=False, enable_proxy=True,
                     client_password=password, instance_type=example.get("instance_type"),
                     volume_size=example.get("volume_size"), force_disable_vnc=True, force_disable_recording=True)
    lib_run_single.run_single_example(DoNothing(), env, example, 1, instruction,
                                      argparse.Namespace(sleep_after_execution=3.0, result_dir=out,
                                                         checkpoint_eval_mode="off"), out, [])
except EnvironmentSetupError as e:
    with open(os.path.join(out, SETUP_FAILED), "w", encoding="utf-8") as f:
        json.dump({"error": type(e).__name__, "cause": type(e.__cause__).__name__ if e.__cause__ else None,
                   "attempts": SETUP_ATTEMPTS}, f)
    logging.error(f"the null control's setup of {domain}__{eid} failed: {type(e).__name__}")
    raise
except Exception as e:
    logging.error(f"the null control's run of {domain}__{eid} failed: {type(e).__name__}")
    raise
finally:
    if env is not None:
        env.close()
