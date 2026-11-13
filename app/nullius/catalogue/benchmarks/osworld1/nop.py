"""OSWorld's null control: the upstream's own run_single_example on one task with an agent that does nothing.

    nop.py VM_IMAGE TASK_JSON RESULT_DIR

The upstream's DesktopEnv on its docker provider, as run_multienv_m3.py builds
it for the docker provider (headless, pyautogui, screenshots, 1920x1080, the
docker password, no proxy); its run_single_example, max_steps 1, with an agent
whose one action is DONE; then the upstream's evaluator on the untouched VM,
written to RESULT_DIR/result.txt by run_single_example. On SIGTERM the
environment is closed, so its VM container is removed.
"""
import argparse
import json
import logging
import signal
import sys

import lib_run_single
from desktop_env.desktop_env import DesktopEnv


class DoNothing:
    def reset(self, *args, **kwargs):
        pass

    def predict(self, instruction, obs):
        return "", ["DONE"]


def _stop(signum, frame):
    sys.exit(1)


logging.basicConfig(level=logging.INFO)        # the provider's own lines, its container started among them, in the log
vm, task, out = sys.argv[1:4]
with open(task, encoding="utf-8") as f:
    example = json.load(f)
signal.signal(signal.SIGTERM, _stop)
env = DesktopEnv(path_to_vm=vm, action_space="pyautogui", provider_name="docker", screen_size=(1920, 1080),
                 headless=True, os_type="Ubuntu", require_a11y_tree=False, client_password="password",
                 enable_proxy=False)
try:
    lib_run_single.run_single_example(DoNothing(), env, example, 1, example["instruction"],
                                      argparse.Namespace(sleep_after_execution=3.0, result_dir=out), out, [])
finally:
    env.close()
