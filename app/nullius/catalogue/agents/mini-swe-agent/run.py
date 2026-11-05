"""mini-swe-agent as a command agent of the Nullius standard.

mini prints its whole conversation to the terminal; the answer is what it
submits at the end (its trajectory's ``info.submission``). This runs mini on
the task given as the one argument, in the current directory (the unit's
workspace), with its own output sent to stderr - the agent's log - and prints
only the submission on stdout, which Nullius reads as the answer.

    run.py "<the task>"

The model, its endpoint and key come from the environment the definition
sets (MSWEA_MODEL_NAME, OPENAI_API_BASE, OPENAI_API_KEY): the relay's; the
reasoning effort from NULLIUS_REASONING_EFFORT (the option reasoning_effort).
"""
import json
import os
import subprocess
import sys
import tempfile

task = sys.argv[1]
out = os.path.join(tempfile.mkdtemp(prefix="mini-"), "trajectory.json")
mini = os.path.join(os.path.dirname(sys.executable), "mini")
# the run's reasoning effort, mini's own model.model_kwargs.reasoning_effort; -c replaces mini's config list, so its
# own mini.yaml is named first (drop_params kept). default: none, as mini sends on its own.
effort = os.environ.get("NULLIUS_REASONING_EFFORT", "default")
config = ["-c", "mini.yaml", "-c", f"model.model_kwargs.reasoning_effort={effort}"] if effort not in ("", "default") else []
rc = subprocess.run([mini, "--task", task, "--yolo", "--exit-immediately", "--cost-limit", "0", "--output", out, *config],
                    stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr).returncode
try:
    with open(out) as f:
        traj = json.load(f)
except (OSError, ValueError):
    sys.exit(rc or 1)
info = traj.get("info") or {}
print((info.get("submission") or "").strip())
sys.exit(0 if info.get("exit_status") in (None, "Submitted") else (rc or 0))
