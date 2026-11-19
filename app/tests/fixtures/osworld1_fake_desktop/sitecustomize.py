"""The fake upstream's start-up hook, for the tests that run the upstream's loop: run_single_example's fixed waits (60 s
after the task's setup, 20 s before the evaluation) are recorded in $FAKE_DESKTOP_LOG instead of slept. Every other
sleep is slept."""
import json
import os
import time

_sleep = time.sleep


def _recorded(seconds):
    if seconds in (20, 60) and os.environ.get("FAKE_DESKTOP_LOG"):
        with open(os.environ["FAKE_DESKTOP_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "wait", "seconds": seconds}) + "\n")
        return
    _sleep(seconds)


time.sleep = _recorded
