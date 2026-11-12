"""SWE-bench Verified's driver for the Nullius standard (JSON lines on stdin and stdout).

Each instance is a GitHub issue of a Python project, and a container the
SWE-bench authors built for it: the repository at the issue's base commit in
``/testbed``, its dependencies installed. The dataset (the SWE-bench
organisation's own, on Hugging Face, at a pinned revision) carries, per
instance, the problem statement, the gold patch, the image's name, the eval
script the harness runs and the name of the log parser that reads its output.

units  - the instance ids, in the dataset's order
setup  - the problem statement as the prompt, verbatim; the instance's own
         image (``image``: the authors' prebuilt image, pulled by the digest
         its tag had when pinned here, ``images.json``), ``/testbed`` as the
         working directory, and the check (``verify``: the harness's eval
         script for the instance, written into the container once the agent
         has ended and run there with a way out to download what it installs
         - it resets the test files to the base commit, applies the test
         patch and runs the FAIL_TO_PASS and PASS_TO_PASS tests). The agent's
         changes are tested where it made them; the harness would re-apply
         them as a patch in a fresh container.
score  - the harness's own grading (``swebench.harness.grading.get_eval_report``)
         of the patch and eval script's output: resolved when a patch exists,
         every FAIL_TO_PASS test passes and every PASS_TO_PASS test still passes. Its report is kept
         as ``verifier/report.json``, in the harness's own shape; the diff
         from the instance's base commit, captured before testing, is kept
         as ``verifier/model.patch`` and given to the grader.
oracle - the reference control: the gold patch applied in the container with
         ``git apply``, as the harness applies a prediction.

The driver never runs docker: the run does each step in the unit's container.
"""
import json
import os
import shlex
import sys

import pyarrow.parquet as pq
from swebench.harness.grading import get_eval_report
from swebench.harness.utils import make_test_spec

HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = "/testbed"
EVAL_SECONDS = 1800                      # the harness's own --timeout default for one instance's tests
SOLVE_SECONDS = 300
RESULTS = "/tmp/nullius-swebench-verifier"

with open(os.path.join(HERE, "images.json")) as f:
    IMAGES = json.load(f)

_rows = None


def rows() -> dict:
    """The dataset's instances by id, read once from the parquet file the run checked."""
    global _rows
    if _rows is None:
        _rows = {r["instance_id"]: r for r in pq.read_table(os.environ["NULLIUS_DATASET"]).to_pylist()}
    return _rows


def row(unit: str) -> dict:
    if unit not in rows():
        raise ValueError(f"{unit} is not an instance of this dataset")
    return rows()[unit]


def units(dataset):
    return {"units": list(rows())}


def setup(unit, dir):
    r = row(unit)
    capture = (f"cd {WORKDIR} && git add -A && "
               f"git diff --cached --binary --no-ext-diff --no-textconv {shlex.quote(r['base_commit'])} -- "
               f"> {RESULTS}/model.patch && exec /bin/bash /eval.sh")
    return {"prompt": r["problem_statement"], "environment": "task", "image": {"pull": IMAGES[unit]}, "workdir": WORKDIR,
            "env": {"PATH": "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/condabin",
                    "CONDA_DEFAULT_ENV": "testbed", "CONDA_PREFIX": "/opt/miniconda3/envs/testbed",
                    "CONDA_SHLVL": "1", "CONDA_EXE": "/opt/miniconda3/bin/conda",
                    "CONDA_PYTHON_EXE": "/opt/miniconda3/bin/python", "_CE_CONDA": "", "_CE_M": "",
                    "CONDA_PROMPT_MODIFIER": "(testbed) "},
            "verify": {"write": {"/eval.sh": make_test_spec(r).eval_script}, "run": ["/bin/bash", "-c", capture],
                       "collect": RESULTS, "seconds": EVAL_SECONDS, "network": True}}


def score(unit, dir, transcript, answer, verified=None):
    if not verified:
        return {"reward": 0.0, "passed": False, "detail": {"why": "the instance's tests did not run"}}
    log = os.path.join(verified["dir"], "test-stdout.txt")
    with open(os.path.join(verified["dir"], "model.patch")) as f:
        patch = f.read()
    report = get_eval_report(make_test_spec(row(unit)), {"instance_id": unit, "model_patch": patch or None}, log, True)
    with open(os.path.join(verified["dir"], "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    r = report[unit]
    tests = {k: {kk: len(vv) for kk, vv in v.items()} for k, v in (r.get("tests_status") or {}).items()}
    detail = {"exit": verified.get("exit"), "timed_out": verified.get("timed_out"), "resolved": r["resolved"],
              "patch_successfully_applied": r["patch_successfully_applied"], "tests": tests}
    if r.get("infra_failure_reason"):
        detail["infra_failure"] = r["infra_failure_reason"]
    return {"reward": 1.0 if r["resolved"] else 0.0, "passed": bool(r["resolved"]), "detail": detail}


def oracle(unit, dir):
    return {"answer": "", "solve": {"write": {"/tmp/gold.patch": row(unit)["patch"]},
                                    "run": ["git", "-C", WORKDIR, "apply", "-v", "/tmp/gold.patch"], "seconds": SOLVE_SECONDS}}


HANDLERS = {"units": units, "setup": setup, "score": score, "oracle": oracle}
for line in sys.stdin:
    try:
        req = json.loads(line)
        out = HANDLERS[req.pop("op")](**req)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out), flush=True)
