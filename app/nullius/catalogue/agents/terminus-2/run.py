"""Terminus 2, Harbor's own terminal agent, as a command agent of the Nullius standard.

Harbor runs Terminus 2 against a BaseEnvironment: a container it reaches by
``exec``, ``upload_file`` and ``download_file``. Here the environment is the
machine this runs on - the unit's workspace on the local backend, or the
inside of the task's own container when the standard runs the unit in one -
so ``Here`` is that interface over the local shell, and nothing else. The
agent itself is Harbor's, untouched: its prompt, its parser, its tmux
session, its summarisation, its LiteLLM client.

    run.py "<the task>"

Terminus works in the terminal and says nothing back: the answer is what it
left in the workspace, so nothing is printed. Its own trajectory.json and
pane log land in ``agent/terminus-2/`` beside the unit's workspace, the
rest of what it says in the agent's log (stderr).

The model, its endpoint and key come from the environment the definition
sets (NULLIUS_MODEL, OPENAI_API_BASE, OPENAI_API_KEY): the relay's; the
reasoning effort from NULLIUS_REASONING_EFFORT (the option reasoning_effort).
"""
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from harbor.agents.terminus_2.terminus_2 import Terminus2
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths

SESSION = "terminus-2"


class Here(BaseEnvironment):
    """This machine, as the environment Terminus works in.

    Harbor mounts the trial's agent folder at ``/logs/agent`` in its
    containers; Terminus pipes its pane there. Here that one path is spelled
    as ``logs``, the agent's own folder. tmux gets a server of its own
    (``TMUX_TMPDIR``), so two units on one machine never share a session.
    ``user`` is ignored: Terminus asks root only to install tmux, and runs as
    whoever runs this otherwise - the same here."""

    def __init__(self, workspace: str, logs: str, tmux_dir: str):
        self.workspace = workspace
        self.logs = logs
        self.tmux_dir = tmux_dir
        super().__init__(environment_dir=Path(workspace), environment_name="here", session_id=SESSION,
                         trial_paths=TrialPaths(trial_dir=Path(logs)), task_env_config=EnvironmentConfig())

    @staticmethod
    def type() -> str:
        return "here"

    def _validate_definition(self):
        pass

    async def start(self, force_build: bool) -> None:
        pass

    async def stop(self, delete: bool) -> None:
        pass

    def _spell(self, path: str) -> str:
        agent_dir = str(EnvironmentPaths.agent_dir)
        return self.logs + path[len(agent_dir):] if path == agent_dir or path.startswith(agent_dir + "/") else path

    async def exec(self, command: str, cwd=None, env=None, timeout_sec=None, user=None) -> ExecResult:
        command = command.replace(str(EnvironmentPaths.agent_dir) + "/", self.logs + "/")
        full = dict(os.environ, TMUX_TMPDIR=self.tmux_dir, **(env or {}))
        proc = await asyncio.create_subprocess_exec("bash", "-c", command, cwd=cwd or self.workspace, env=full,
                                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            return ExecResult(stdout=out.decode("utf-8", "replace"), stderr="timed out", return_code=124)
        return ExecResult(stdout=out.decode("utf-8", "replace"), stderr=err.decode("utf-8", "replace"), return_code=proc.returncode)

    async def upload_file(self, source_path, target_path: str):
        shutil.copy(str(source_path), self._spell(target_path))

    async def upload_dir(self, source_dir, target_dir: str):
        shutil.copytree(str(source_dir), self._spell(target_dir), dirs_exist_ok=True)

    async def download_file(self, source_path: str, target_path):
        shutil.copy(self._spell(source_path), str(target_path))

    async def download_dir(self, source_dir: str, target_dir):
        shutil.copytree(self._spell(source_dir), str(target_dir), dirs_exist_ok=True)


def logs_dir(workspace: str) -> str:
    """Where Terminus writes its own files: ``agent/terminus-2/`` beside the
    workspace when the unit has an agent folder there, else a folder of its own."""
    beside = os.path.join(os.path.dirname(workspace), "agent")
    where = os.path.join(beside, SESSION) if os.path.isdir(beside) else tempfile.mkdtemp(prefix="terminus-2-")
    os.makedirs(where, exist_ok=True)
    return where


async def main(prompt: str) -> int:
    workspace = os.getcwd()
    logs = logs_dir(workspace)
    tmux_dir = tempfile.mkdtemp(prefix="tmux-")
    effort = os.environ.get("NULLIUS_REASONING_EFFORT", "default")
    agent = Terminus2(logs_dir=Path(logs), model_name="openai/" + os.environ["NULLIUS_MODEL"],
                      api_base=os.environ["OPENAI_API_BASE"], record_terminal_session=False,
                      reasoning_effort=None if effort in ("", "default") else effort)
    env = Here(workspace, logs, tmux_dir)
    rc = 0
    try:
        await agent.setup(env)
        await agent.run(prompt, env, AgentContext())
    except Exception as e:
        logging.getLogger(SESSION).error("terminus-2: %s: %s", type(e).__name__, e)
        rc = 1
    finally:
        await env.exec("tmux kill-server", timeout_sec=30)
        shutil.rmtree(tmux_dir, ignore_errors=True)
    return rc


if __name__ == "__main__":
    # stdout is the answer and Terminus has none: everything it and litellm print goes to the log
    answer = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    code = asyncio.run(main(sys.argv[1]))
    os.dup2(answer, 1)
    sys.exit(code)
