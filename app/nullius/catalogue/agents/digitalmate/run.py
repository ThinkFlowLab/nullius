"""Start the product service with relay credentials and submit Nullius's task."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen
import uuid

from gateway import submit_task


def stop(service: subprocess.Popen) -> None:
    # The supervisor and tool children belong to this trial too.
    try:
        os.killpg(service.pid, signal.SIGTERM)
    except ProcessLookupError:
        service.wait()
        return
    try:
        service.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(service.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    service.wait()


def require_container() -> None:
    if not any(Path(path).exists() for path in ("/.dockerenv", "/run/.containerenv")):
        raise RuntimeError("DigitalMate requires --backend container for service isolation and cleanup")


def run(install: Path, prompt: str) -> str:
    source = install / "src"
    runtime = install / "runtime/bin/python"
    launcher = source / "harness/personal-agent/services/agent_bridge/src/server_engine/workswarm_launcher.py"
    if not runtime.is_file() or not launcher.is_file():
        raise RuntimeError("DigitalMate installation is missing its runtime or product launcher")
    for name in ("API_BASE", "API_KEY", "MODEL_NAME"):
        if not os.environ.get(name):
            raise RuntimeError("Nullius relay setting is missing: " + name)
    require_container()

    # Keep product state outside the benchmark workspace and isolated per trial.
    with tempfile.TemporaryDirectory(prefix="digitalmate-", dir=Path.home()) as temporary:
        state = Path(temporary)
        env = dict(os.environ)
        token = uuid.uuid4().hex
        env.update(
            DIGITALMATE_ACCESS_TOKEN=token,
            DIGITALMATE_WORKSWARM_COMMAND=json.dumps([str(runtime), str(launcher)]),
            DIGITALMATE_SEARCH_PYTHON=sys.executable,
            DIGITALMATE_WORKSWARM_TOOL_PYTHON=str(runtime),
        )
        # Match the release's deploy/entrypoint.py defaults for its owned state.
        env.setdefault("DIGITALMATE_SECRET_STORE", "file")
        env.setdefault("DIGITALMATE_MEMORY_BACKEND", "sqlite-offline")
        env.setdefault("DIGITALMATE_SCHEDULE_SCAN_ENABLED", "0")
        # These values must not redirect this trial to a deployed service.
        for key in ("DIGITALMATE_WORKSWARM_WS", "DIGITALMATE_CONFIG_FILE", "DIGITALMATE_CLIENT_AUTH"):
            env.pop(key, None)
        service = subprocess.Popen(
            [sys.executable, "-m", "server_engine.engine_cli", "serve", "--workspace", str(state),
             "--host", "127.0.0.1", "--port", "18092"],
            env=env, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr,
            start_new_session=True,
        )
        previous = {}
        def interrupted(signum, _frame):
            raise SystemExit(128 + signum)
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, interrupted)
        try:
            deadline = time.monotonic() + 180
            while True:
                if service.poll() is not None:
                    raise RuntimeError("DigitalMate service exited during startup; inspect agent stderr")
                try:
                    with urlopen("http://127.0.0.1:18092/health/ready", timeout=2) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("DigitalMate service did not become ready in 180 seconds")
                time.sleep(0.5)
            return submit_task(prompt, Path.cwd(), token)
        finally:
            stop(service)
            # Preserve WorkSwarm's separate log before deleting trial state.
            runtime_log = state / "workswarm/process.log"
            if runtime_log.is_file():
                from server_engine.engine_config import redact_credentials
                with runtime_log.open("rb") as handle:
                    handle.seek(max(0, runtime_log.stat().st_size - 32768))
                    tail = handle.read().decode("utf-8", errors="replace")
                print(redact_credentials(tail, env), file=sys.stderr)
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", required=True, type=Path)
    parser.add_argument("prompt")
    args = parser.parse_args()
    try:
        print(run(args.install.resolve(), args.prompt))
        return 0
    except (RuntimeError, subprocess.SubprocessError, ValueError, OSError) as error:
        print("digitalmate-nullius: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
