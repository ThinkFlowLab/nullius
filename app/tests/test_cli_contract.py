"""The command lines and the importers, asserted the way their contract
states them — one test per statement.

Each test asserts one sentence of the shell side's contract (the command
line, importing), whose wording opens its docstring, as directly as the code
allows. The other suites pin the pieces — a message, a code, a digest — and
each test here puts the pieces together in the order the sentence states them.

Every fixture is written by the test into a temporary directory: the
smallest Harbor job, ATIF document or OTLP export that has the shape the
check names. Nothing here reads a run from outside the repository, asks a
model, or opens a socket: the in-process calls run with `socket.socket` and
`socket.create_connection` patched to raise, and the subprocesses inherit a
`sitecustomize` that does the same, so a command that tried to reach a
network would fail the test rather than pass it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nullius import importers
from nullius.importers import cli as import_cli
from nullius.runs import cli as runs_cli

APP = Path(__file__).resolve().parents[1]

# Credential-shaped and obviously fake; matched by the redactor's first pattern.
KEY = "sk-test-0123456789abcdefghij"
MARK = "[REDACTED]"
CLIS = ("importers.cli", "runs.cli")

# What a subprocess gets on its path so that any socket it opened would raise:
# `socket.socket` stays a class (ssl subclasses it at import) whose
# construction fails, and `create_connection` fails outright.
SITECUSTOMIZE = """\
import socket
class _Refuse(socket.socket):
    def __init__(self, *a, **k):
        raise AssertionError("socket opened")
def _refuse(*a, **k):
    raise AssertionError("socket opened")
socket.socket = _Refuse
socket.create_connection = _refuse
"""


class _Refuse(socket.socket):
    def __init__(self, *a, **k):
        raise AssertionError("socket opened")


def _refuse(*a, **k):
    raise AssertionError("socket opened")



def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_digest(root) -> dict:
    """Every file under *root* by relative path with its digest: what
    "byte-for-byte" and "nothing new" both mean."""
    root = Path(root)
    return {str(p.relative_to(root)): sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}


def write_json(path, doc) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")
    return str(path)


def harbor_job(root, job_id="job-a", *, exception=None, command="", trajectory=None,
               tokens=(100, 10, 50)) -> str:
    """The smallest Harbor job the reader walks:
    `config.json`, `result.json`, one trial directory with its `result.json`.
    *exception* makes the trial an errored one; *trajectory* puts an ATIF
    document beside the trial's result, where Harbor's agents write theirs."""
    job = Path(root) / job_id
    trial = job / "task-1__abc"
    n_in, n_out, n_cache = tokens
    write_json(job / "config.json", {
        "job_name": job_id, "command": command,
        "agents": [{"name": "claude-code", "model_name": "openai/m"}],
        "datasets": [{"path": "harbor-datasets/swe-bench-verified", "n_tasks": 1}]})
    write_json(job / "result.json", {
        "id": "488f6fc5-ac2c-496c-82a3-9a243818f95c",
        "started_at": "2026-08-12T15:53:07.204546", "finished_at": "2026-08-12T16:02:38.879914",
        "n_total_trials": 1,
        "stats": {"n_completed_trials": 0 if exception else 1, "n_errored_trials": 1 if exception else 0,
                  "n_running_trials": 0, "n_pending_trials": 0, "n_cancelled_trials": 0}})
    write_json(trial / "result.json", {
        "task_name": "swe-bench-verified/task-1", "trial_name": "task-1__abc",
        "started_at": "2026-08-12T15:53:07.204546", "finished_at": "2026-08-12T16:02:38.879914",
        "agent_result": {"n_input_tokens": n_in, "n_output_tokens": n_out, "n_cache_tokens": n_cache,
                         "cost_usd": 0.01},
        "verifier_result": None if exception else {"rewards": {"reward": 1.0}},
        "exception_info": {"exception_type": "ApiError", "exception_message": exception} if exception else None})
    if trajectory is not None:
        write_json(trial / "agent" / "trajectory.json", trajectory)
    return str(job)


def atif_doc(version="ATIF-v1.7", **overrides) -> dict:
    """The smallest standalone ATIF document the importer accepts."""
    doc = {
        "schema_version": version, "session_id": "s-1",
        "agent": {"name": "someagent", "version": "0.1", "model_name": "m"},
        "steps": [
            {"step_id": 1, "source": "user", "message": "do it", "timestamp": "2026-08-12T13:55:06Z"},
            {"step_id": 2, "source": "agent", "message": "done", "timestamp": "2026-08-12T13:55:16Z",
             "model_name": "m", "metrics": {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 0}}],
        "final_metrics": {"total_prompt_tokens": 10, "total_completion_tokens": 2,
                          "total_cached_tokens": 0, "total_cost_usd": None}}
    doc.update(overrides)
    return doc


def otlp_doc() -> dict:
    """The smallest OTLP/JSON trace export: one resource, one scope, one span."""
    return {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "agent"}}]},
        "scopeSpans": [{"scope": {"name": "t"}, "spans": [{
            "traceId": "4bf92f3577b34da6a3ce929d0e0e4736", "spanId": "00f067aa0ba902b7", "parentSpanId": "",
            "name": "run", "kind": 1, "startTimeUnixNano": "1789034400000000000",
            "endTimeUnixNano": "1789034400500000000", "attributes": [], "status": {"code": 0}}]}]}]}


def without_dirs(value):
    """A CLI document with its `dir` fields dropped: docs/cli.md says a path
    is echoed as given, so a key in a directory *name* is the caller's."""
    if isinstance(value, dict):
        return {k: without_dirs(v) for k, v in value.items() if k != "dir"}
    if isinstance(value, list):
        return [without_dirs(v) for v in value]
    return value


class Scratch(unittest.TestCase):
    """A temp directory, a fresh cwd per test, and no way to open a socket."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-cli-contract-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(self.cwd)
        self.site = os.path.join(self.tmp, "site")
        os.makedirs(self.site)
        Path(self.site, "sitecustomize.py").write_text(SITECUSTOMIZE)
        for name, fake in (("socket", _Refuse), ("create_connection", _refuse)):
            p = patch.object(socket, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def sh(self, module, *args, env=None) -> subprocess.CompletedProcess:
        """One CLI as a shell runs it, with the socket-refusing
        `sitecustomize` on its path."""
        clean = dict(os.environ)
        clean.update(env or {})
        clean["PYTHONPATH"] = str(APP) + os.pathsep + self.site
        return subprocess.run([sys.executable, "-m", f"nullius.{module}", *map(str, args)],
                              cwd=self.cwd, env=clean, capture_output=True, text=True)

    def assertTmpHolds(self, *names):
        self.assertEqual(sorted(os.listdir(self.tmp)), sorted(("cwd", "site") + names))
        self.assertEqual(os.listdir(self.cwd), [], "the command wrote into the cwd")


# ── cli.md ──────────────────────────────────────────────────────────────────

class CommandLine(Scratch):
    def test_help_and_json_results_across_the_named_failure_kinds(self):
        """Human help and machine-readable results are tested, including empty
        inputs, invalid arguments, unavailable resources, failed jobs and
        authorization failures."""
        for module in CLIS:
            with self.subTest(help=module):
                out = self.sh(module, "--help")
                self.assertEqual(out.returncode, 0, out.stderr)
                for line in ("usage:", f"python3 -m nullius.{module}", "examples:", "exit codes:"):
                    self.assertIn(line, out.stdout)
                self.assertEqual(out.stderr, "")

        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        missing = os.path.join(self.tmp, "nowhere")
        harbor_job(self.tmp, "failed-job", exception="ApiError: 401 unauthorized")
        cases = {
            "empty input": [
                ("runs.cli", ["list", empty, "--json"], 3, "no job directory found"),
                ("importers.cli", [empty, "-o", os.path.join(self.tmp, "out"), "--json"], 3, "no importer recognised"),
            ],
            "invalid arguments": [
                ("runs.cli", ["list", "--bogus", self.tmp, "--json"], 2, "usage:"),
                ("importers.cli", [self.tmp, "--harness", "nope", "--json"], 2, "usage:"),
            ],
            "unavailable resource": [
                ("runs.cli", ["show", missing, "failed-job", "--json"], 3, "no such directory"),
                ("runs.cli", ["show", self.tmp, "no-such-job", "--json"], 3, "no job named"),
                ("importers.cli", [missing, "-o", os.path.join(self.tmp, "out"), "--json"], 3, "no such directory"),
            ],
            "failed job": [
                ("runs.cli", ["show", self.tmp, "failed-job", "--json"], 0, ""),
                ("runs.cli", ["list", self.tmp, "--json"], 0, ""),
            ],
            # No command takes a credential any more: the authorization
            # failure a shell meets is one a run recorded - a trial that died
            # on a 401.
            "authorization failure": [
                ("runs.cli", ["show", self.tmp, "failed-job", "--json"], 0, ""),
            ],
        }
        for kind, runs in cases.items():
            for module, args, code, said in runs:
                with self.subTest(kind=kind, module=module, args=args):
                    out = self.sh(module, *args)
                    self.assertEqual(out.returncode, code, out.stderr)
                    self.assertIn(said, out.stderr)
                    if code == 0:
                        self.assertNotEqual(out.stdout, "")
                        json.loads(out.stdout)
                    else:
                        self.assertEqual(out.stdout, "", "a failure leaves stdout to the document")
        shown = json.loads(self.sh("runs.cli", "show", self.tmp, "failed-job", "--json").stdout)
        self.assertEqual((shown["trials"][0]["outcome"], shown["trials"][0]["exception_type"]), ("api_error", "ApiError"))
        self.assertEqual(shown["counts"]["api_error"], 1)
        listed = json.loads(self.sh("runs.cli", "list", self.tmp, "--json").stdout)
        self.assertEqual([r["job_id"] for r in listed], ["failed-job"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out")))

    def test_documented_exit_codes_no_secrets_and_no_unintended_writes(self):
        """Commands work non-interactively with documented exit codes and no
        secrets in output; read/preview checks produce no unintended writes,
        runs or uploads."""
        # each code, produced non-interactively (stdin closed, no prompt)
        root = os.path.join(self.tmp, KEY)                       # the key in the job's path
        harbor_job(root, "errored", exception=f"401 for api_key={KEY}")
        harbor_job(root, "fine")
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        produced = {
            0: ("runs.cli", ["list", root]),
            2: ("runs.cli", ["list"]),
            3: ("runs.cli", ["list", os.path.join(self.tmp, "nowhere")]),
            4: ("importers.cli", [empty, "--harness", "atif", "-o", os.path.join(self.tmp, "out")]),
        }
        for code, (module, args) in produced.items():
            with self.subTest(code=code):
                out = self.sh(module, *args)
                self.assertEqual(out.returncode, code, out.stderr)
        # no secret in output: not the one in the exception, and not the one
        # in the path except where docs/cli.md says a path is echoed as given
        # (the `dir` fields of a --json document)
        before = tree_digest(root)
        outputs = {}
        for name, (module, args) in {
            "list": ("runs.cli", ["list", root]), "list --json": ("runs.cli", ["list", root, "--json"]),
            "show": ("runs.cli", ["show", root, "errored"]), "show --json": ("runs.cli", ["show", root, "errored", "--json"]),
        }.items():
            out = self.sh(module, *args)
            outputs[name] = out
            with self.subTest(command=name):
                self.assertNotIn(KEY, out.stderr)
                stdout = out.stdout
                if name.endswith("--json"):
                    stdout = json.dumps(without_dirs(json.loads(out.stdout)))
                self.assertNotIn(KEY, stdout)
        shown = json.loads(outputs["show --json"].stdout)
        self.assertEqual(shown["trials"][0]["exception_message"], f"401 for api_key={MARK}")
        self.assertRegex(outputs["show"].stdout, r"(?m)^  task-1__abc\s+api_error\s+ApiError$")
        # reads left the source byte-identical and wrote nothing outside the
        # directory named on the command line; the refusal wrote nothing at all
        self.assertEqual(tree_digest(root), before)
        self.assertTmpHolds(KEY, "empty")

    def test_a_key_in_the_recorded_command_is_not_echoed_by_show_json(self):
        """Recorded commands and dataset paths must not expose credentials."""
        harbor_job(self.tmp, command=f"harbor run --api-key {KEY}")
        out = self.sh("runs.cli", "show", self.tmp, "job-a", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn(KEY, out.stdout)


# ── import.md ───────────────────────────────────────────────────────────────

class ImportContract(Scratch):
    def shapes(self) -> dict:
        """The smallest tree of each recognised shape, plus the two kinds of
        tree no importer may claim: a Harbor job (the reader's) and an
        unrelated directory."""
        atif = os.path.join(self.tmp, "atif")
        write_json(os.path.join(atif, "trajectory.json"), atif_doc())
        otel = os.path.join(self.tmp, "otel")
        write_json(os.path.join(otel, "export.json"), otlp_doc())
        harbor = harbor_job(self.tmp, "harbor")
        unrelated = os.path.join(self.tmp, "unrelated")
        os.makedirs(unrelated)
        Path(unrelated, "notes.txt").write_text("nothing a harness wrote\n")
        return {"atif": atif, "otel": otel, "harbor": harbor, "unrelated": unrelated}

    def test_a_recognised_input_goes_to_its_unique_importer_and_unclaimed_input_is_refused(self):
        """A recognized input goes to its unique importer; unclaimed input is refused."""
        shapes = self.shapes()
        registry = importers.available()
        self.assertGreaterEqual(set(registry), {"atif", "otel"})
        for name in ("atif", "otel"):
            with self.subTest(shape=name):
                claimants = [n for n, m in registry.items() if m.detect(shapes[name])]
                self.assertEqual(claimants, [name])
                self.assertEqual(importers.detect(shapes[name]).NAME, name)
        for name in ("harbor", "unrelated"):
            with self.subTest(shape=name):
                self.assertEqual([n for n, m in registry.items() if m.detect(shapes[name])], [])
                self.assertIsNone(importers.detect(shapes[name]))
                out = self.sh("importers.cli", shapes[name], "-o", os.path.join(self.tmp, "out"))
                self.assertEqual(out.returncode, 3, out.stderr)
                self.assertIn("no importer recognised", out.stderr)
                self.assertEqual(out.stdout, "")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out")))

    def test_conversion_preserves_the_source_tree_byte_for_byte(self):
        """Conversion preserves the source tree byte-for-byte."""
        shapes = self.shapes()
        for name in ("atif", "otel"):
            src = shapes[name]
            before = tree_digest(src)
            with self.subTest(shape=name, how="in-process"):
                jobs = importers.get(name).convert(src, os.path.join(self.tmp, "out-py"))
                self.assertEqual(len(jobs), 1)
                self.assertEqual(tree_digest(src), before)
            with self.subTest(shape=name, how="cli"):
                out = self.sh("importers.cli", src, "-o", os.path.join(self.tmp, "out-cli"))
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(tree_digest(src), before)
        self.assertTmpHolds("atif", "otel", "harbor", "unrelated", "out-py", "out-cli")


if __name__ == "__main__":
    unittest.main()
