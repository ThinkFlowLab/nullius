"""One unit of an agent on OSWorld1: the upstream's DesktopEnv and its own run_single_example, imported and called as
they are, with the computer port's bridge as the loop's agent (nullius.standard.computer_port).

    runner.py --example TASK_JSON --result-dir DIR --socket PATH --ready FILE --lifeline FD --steps N --pause SECONDS
              --vm VM_IMAGE --calls FILE [--held FOLDER]

The driver starts it in an agent's unit's setup, in the unit's runner folder (the trial's .work/osworld/), with the
upstream's folder and the nullius package on its import path. It builds the upstream's DesktopEnv as the null control
does (nop.py), serves the MCP server computer at PATH (a Unix socket in a folder of the user's alone, which the driver
made) over a Desktop of five methods on the upstream's controllers, and calls run_single_example with the bridge as the
loop's agent, N as max_steps and SECONDS as sleep_after_execution. No file of the upstream is edited, patched or
replaced.

- READY, a file of mode 0600: {"ok": true, "password": the desktop user's} once the loop asks for the agent's first
  step (after the task's setup and the upstream's own wait); {"ok": false, "error": ...} when the unit fails before.
- FD, the read end of a pipe from the driver: a line turn-end ends the agent's turn (the loop then gets no action for
  the steps left and evaluates the desktop as it is); its close stops the runner as SIGTERM does.
- FILE, the call log: one line per tool call, {tool, start, end, isError}.
- FOLDER, the run's folder of files sent once, when the driver gives one (computer_port.Held).

A stop is taken once. The clean-up (the bridge's end and the server's close, then the desktop's close within
CLOSE_SECONDS) takes at most STOP_SECONDS; past it the runner exits 1 all the same, and the driver, which lets a
runner go for longer than that, removes what is left. At every exit READY and the socket's folder are removed. The
exit status is 0 only for a loop that ran to its end and a desktop closed in its time.
"""
import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import threading

SCREEN = (1920, 1080)
FINISH_SECONDS = 5                                   # the bridge's end and the server's close
CLOSE_SECONDS = 25                                   # the desktop's close: the provider stops and removes its container
STOP_SECONDS = FINISH_SECONDS + CLOSE_SECONDS
FOLDER = "/tmp/agent"                                # the agent's own folder in the VM


class Desktop:
    """The port's five operations over the upstream's own controllers."""

    def __init__(self, env, scratch):
        self.env, self.scratch = env, scratch

    def python(self, code):
        return self.env.controller.execute_python_command(code)

    def upload(self, path, data):
        fd, local = tempfile.mkstemp(dir=self.scratch)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            self.env.setup_controller._upload_file_setup([{"local_path": local, "path": path}])
        finally:
            os.unlink(local)

    def read(self, path):
        return self.env.controller.get_file(path)

    def capture(self):
        png = self.env.controller.get_screenshot()
        if not png:
            raise RuntimeError("the desktop gave no screenshot")
        return png

    def password(self):
        return self.env.client_password


class Turn:
    """What the lifeline's turn-end reaches: the bridge, once it exists."""
    bridge = None

    def end_turn(self):
        if self.bridge is not None:
            self.bridge.end_turn()


def close_within(env, seconds):
    """The desktop closed in a thread of its own, waited for ``seconds``: True when it closed in that time."""
    done = []

    def close():
        env.close()
        done.append(True)

    worker = threading.Thread(target=close, name="desktop-close", daemon=True)
    worker.start()
    worker.join(seconds)
    return bool(done)


def main(argv=None):
    p = argparse.ArgumentParser()
    for name in ("--example", "--result-dir", "--socket", "--ready", "--vm", "--calls"):
        p.add_argument(name, required=True)
    p.add_argument("--held")
    p.add_argument("--lifeline", type=int, required=True)
    p.add_argument("--steps", type=int, required=True)
    p.add_argument("--pause", type=float, required=True)
    a = p.parse_args(argv)
    from nullius.standard import computer_port as port
    stop, turn = port.Stop(STOP_SECONDS), Turn()
    try:                                             # the clean-up holds before the lifeline can stop the runner
        stop.install()
        threading.Thread(target=port.lifeline, args=(a.lifeline, turn), name="lifeline", daemon=True).start()
        return run(a, port, stop, turn)
    finally:
        try:
            os.unlink(a.ready)
        except OSError:
            pass
        shutil.rmtree(os.path.dirname(a.socket), ignore_errors=True)


def run(a, port, stop, turn):
    logging.basicConfig(level=logging.INFO)          # the provider's own lines, its container started among them
    try:
        import lib_run_single
        from desktop_env.desktop_env import DesktopEnv
        with open(a.example, encoding="utf-8") as f:
            example = json.load(f)
    except Exception as e:
        port.ready(a.ready, ok=False, error=f"{type(e).__name__}: {e}")
        return 3
    where = os.path.join(a.result_dir, *_task_path(a.example))
    env = bridge = server = None
    said, failed, code = [], "", 0

    def first():                                     # the loop asks for the agent's first step: the unit is ready
        port.ready(a.ready, ok=True, password=env.client_password or "")
        said.append(True)

    try:
        env = DesktopEnv(path_to_vm=a.vm, action_space="pyautogui", provider_name="docker", screen_size=SCREEN,
                         headless=True, os_type="Ubuntu", require_a11y_tree=False, client_password="password",
                         enable_proxy=False)
        os.makedirs(where, exist_ok=True)
        bridge = port.Bridge(Desktop(env, os.path.dirname(os.path.abspath(a.calls))), a.steps, a.pause, SCREEN,
                             FOLDER, on_first=first, held=port.Held(a.held) if a.held else None)
        server = port.Server(bridge, a.socket, a.calls)
        turn.bridge = bridge
        lib_run_single.run_single_example(bridge, env, example, a.steps, example["instruction"],
                                          argparse.Namespace(sleep_after_execution=a.pause, result_dir=a.result_dir),
                                          where, [])
    except SystemExit:                               # SIGTERM or the lifeline's end: the unit was stopped
        failed, code = "the runner was stopped", 1
    except BaseException as e:                       # the loop or the desktop failed: the harness's
        failed, code = f"{type(e).__name__}: {e}", 1
        sys.stderr.write(f"runner: {failed}\n")
    finally:
        with stop.deferred(then_stop=False):
            if not said:
                port.ready(a.ready, ok=False, error="the task's setup: " + (failed or "the loop ended before the "
                                                                                       "agent's first step"))
            if bridge is not None:
                bridge.finish()
            if server is not None:
                server.close()
            if env is not None and not close_within(env, CLOSE_SECONDS):
                sys.stderr.write(f"runner: the desktop did not close within {CLOSE_SECONDS} s\n")
                code = code or 1
    return code


def _task_path(example_file):
    """domain/id of the task file (the driver's copy, named domain__id.json)."""
    name = os.path.splitext(os.path.basename(example_file))[0]
    domain, _, eid = name.partition("__")
    return (domain, eid) if eid else (name,)


if __name__ == "__main__":
    sys.exit(main())
