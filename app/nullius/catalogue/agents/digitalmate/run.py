"""Start the product service with relay credentials and submit Nullius's task."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
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

from gateway import Conversation, submit_task


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


@contextmanager
def product(install: Path, *, port: int = 18092, project: Path | None = None, mcp_config: Path | None = None):
    """One isolated service for this trial; yields its access token.

    ``project`` binds Hive's file tools to that folder and ``mcp_config`` gives Bees those MCP servers; both need
    a product that has them (the fixed version), and nothing passes them for the release.
    """
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
        state = Path(temporary) / "state"
        env = dict(os.environ)
        token = uuid.uuid4().hex
        env.update(
            DIGITALMATE_ACCESS_TOKEN=token,
            DIGITALMATE_CLIENT_AUTH="instance",
            DIGITALMATE_WORKSWARM_COMMAND=json.dumps([str(runtime), "-m", "server_engine.workswarm_launcher"]),
            DIGITALMATE_SEARCH_PYTHON=sys.executable,
            DIGITALMATE_WORKSWARM_TOOL_PYTHON=str(runtime),
        )
        # Match the release's deploy/entrypoint.py defaults for its owned state.
        env.setdefault("DIGITALMATE_SECRET_STORE", "file")
        env.setdefault("DIGITALMATE_MEMORY_BACKEND", "sqlite-offline")
        env.setdefault("DIGITALMATE_SCHEDULE_SCAN_ENABLED", "0")
        # These values must not redirect this trial to a deployed service.
        for key in ("DIGITALMATE_WORKSWARM_WS", "DIGITALMATE_CONFIG_FILE", "DIGITALMATE_HIVE_CONFIG",
                    "DIGITALMATE_HIVE_HOME_MCP_SERVERS"):
            env.pop(key, None)
        if mcp_config is not None:
            env["DIGITALMATE_HIVE_HOME_MCP_SERVERS"] = str(mcp_config)
        if project is not None:
            # The product's own operator command binds the folder; state must not exist yet.
            subprocess.run([sys.executable, "-m", "dm_hive_tools.workspace",
                            "--state-dir", str(state), "--project-dir", str(project)],
                           env=env, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr, check=True)
        else:
            state.mkdir()
        service = subprocess.Popen(
            [sys.executable, "-m", "server_engine.engine_cli", "serve", "--workspace", str(state),
             "--host", "127.0.0.1", "--port", str(port),
             # The release serves only as Hive Home: Beebou takes the turn, Bees run what it dispatches.
             "--hive-results", "--hive-turns", "--hive-bees", "--hive-beebou-id", "beebou"],
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
                    with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=2) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("DigitalMate service did not become ready in 180 seconds")
                time.sleep(0.5)
            yield token
        finally:
            stop(service)
            # Preserve both WorkSwarm runtimes' logs before deleting trial state.
            for name in ("workswarm", "bee-workswarm"):
                runtime_log = state / name / "process.log"
                if runtime_log.is_file():
                    from server_engine.engine_config import redact_credentials
                    with runtime_log.open("rb") as handle:
                        handle.seek(max(0, runtime_log.stat().st_size - 32768))
                        tail = handle.read().decode("utf-8", errors="replace")
                    print(redact_credentials(tail, env), file=sys.stderr)
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def run(install: Path, prompt: str) -> str:
    with product(install) as token:
        return submit_task(prompt, token)


def session(install: Path, port: int, *, project: Path | None = None, mcp_config: Path | None = None) -> None:
    """One instance for several prompts, one JSON line each way; Hive keeps the conversation.

    A Bee's open question is that turn's reply, and the next prompt answers it.
    """
    with product(install, port=port, project=project, mcp_config=mcp_config) as token:
        print(json.dumps({"ready": True}), flush=True)
        conversation = Conversation()
        for line in sys.stdin:
            prompt = json.loads(line).get("prompt")
            if not isinstance(prompt, str):
                raise ValueError("Session prompt must be text")
            answer = submit_task(prompt, token, url=f"ws://127.0.0.1:{port}/digitalmate/ws",
                                 conversation=conversation)
            print(json.dumps({"answer": answer}), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", required=True, type=Path)
    parser.add_argument("--session", action="store_true", help="answer JSON-line prompts on stdin (acp.py)")
    parser.add_argument("--port", type=int, default=18092)
    parser.add_argument("--project", type=Path, help="bind Hive's file tools to this folder (fixed version)")
    parser.add_argument("--mcp-config", type=Path, help="MCP servers for Bees (fixed version)")
    parser.add_argument("prompt", nargs="?")
    args = parser.parse_args()
    try:
        if args.session:
            session(args.install.resolve(), args.port, project=args.project, mcp_config=args.mcp_config)
        else:
            if args.prompt is None:
                parser.error("a prompt is required")
            print(run(args.install.resolve(), args.prompt))
        return 0
    except (RuntimeError, subprocess.SubprocessError, ValueError, OSError) as error:
        print("digitalmate-nullius: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
