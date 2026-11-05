"""OpenTelemetry span exports: a directory of OTLP/JSON files in, one job out.

    PYTHONPATH=app python3 -m nullius.importers.cli traces-export/ -o jobs_imported

A production trace export is not a benchmark run — no task, no verifier, no
reward — but it is spans with clocks, which is what the trial page's waterfall
and ``/charts`` draw. So this lays the export out as the one shape the app
walks: a job directory holding **one trial per trace id**, each with the
``harbor-trace-v1`` document ``traces/otel.py`` builds under ``agent/``. The
format, the conventions read and the field mapping are that module's; this one
decides what a trial *is* and what its ``result.json`` may honestly say.

What it says is little, on purpose:

- **The outcome is unknown, and no reward exists.** A span in ERROR marks that
  span in the trace; it is not an exception on the trial, because a failed
  call is not a failed task and the app's outcome taxonomy is about tasks.
  ``exception_info`` and ``verifier_result`` are therefore empty, which
  ``classify`` reads as *unknown* — not resolved, not failed.
- **Tokens are the trace's own totals** — the sum over its LLM spans — written
  only when at least one span reported usage; a trace with no usage attributes
  records no ``agent_result`` at all rather than three zeros.
- **The model is the first LLM span's**, the agent the resource's
  ``service.name``. An export that names neither shows neither.
- **The clock is the trace's window**, first span start to last span end, as
  both the trial's ``started_at``/``finished_at`` and its ``agent_execution``
  phase — the only phase there is evidence for.

Recognition is by content: a ``.json`` file whose top level is an
``ExportTraceServiceRequest`` (``resourceSpans``), or a JSON-lines file of them
— under the directory at any depth. What one job is under a root the app opened is this module's answer
(``trees``): an export directory is one job, and a parent that holds no
export of its own but several export directories is one job per directory —
each is one producer's export (the trial's agent is its ``service.name``),
and pooling them would put several agents under one heading. The CLI
converts the directory it is given as one job; a parent given to it is one
pooled job, which is the one place the two still read a tree differently.
Nothing Harbor writes carries ``resourceSpans``, and an ATIF
trajectory carries ``schema_version`` instead, so nothing here claims their
directories.

A file the reader refuses — bad JSON, no ``resourceSpans``, an unsupported
schema URL — is reported with its redacted message and skipped; only when
*nothing* converts is that fatal. The source directory is never written to:
everything lands under the output root, and a re-convert rewrites the same
names in place (a trace the export no longer holds is removed), so refreshing
a workspace updates rather than duplicates.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone

from nullius.redact import redact
from nullius.traces import otel as reader
from nullius.traces import trace

NAME = "otel"
LABEL = "OpenTelemetry"

# Directory-name characters for a trial: the root span's name can be anything
# a producer chose, and a slash in it would nest the trial.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")



def find_exports(root: str) -> list[str]:
    """Every OTLP/JSON file under *root*, sorted. A cheap head check first,
    then the real read: the walk runs on every workspace open, over roots
    that may hold thousands of Harbor files that are not this."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not (name.endswith(".json") or name.endswith(".jsonl")):
                continue
            if not reader.looks_like_export(path):
                continue
            try:
                if reader.read_file(path):
                    out.append(path)
            except reader.ReadError:
                continue
    return sorted(out)


def detect(path: str) -> bool:
    """Does *path* hold at least one readable OTLP/JSON trace export?"""
    return os.path.isdir(path) and bool(find_exports(path))


def trees(root: str) -> list[str]:
    """The directories under *root* that are one job each.

    *root* itself when an export file sits directly in it — an export
    directory is one job however its files are arranged below. Otherwise
    each child directory holding an export, one job per producer's export
    rather than one pooled job of several agents. Nothing when there is no
    export under *root* at all.
    """
    exports = find_exports(root) if os.path.isdir(root) else []
    if not exports:
        return []
    if any(os.path.dirname(path) == root for path in exports):
        return [root]
    return [os.path.join(root, name) for name in sorted(os.listdir(root))
            if os.path.isdir(os.path.join(root, name)) and detect(os.path.join(root, name))]


def refused(root: str) -> list[str]:
    """(path, redacted reason) for every candidate file the reader refuses —
    what the CLI reports beside the job it wrote."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if not (name.endswith(".json") or name.endswith(".jsonl")):
                continue
            if not reader.looks_like_export(path):
                continue
            try:
                reader.read_file(path)
            except reader.ReadError as exc:
                # The reader names the file by basename; this names it by its
                # path under the root, once, in place of that.
                reason = str(exc)
                prefix = os.path.basename(path) + ": "
                if reason.startswith(prefix):
                    reason = reason[len(prefix):]
                out.append(f"{os.path.relpath(path, root)}: {reason}")
    return out



def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _dump(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


def _window(document: dict) -> tuple:
    """(start_ns, end_ns) over every span, end None when no span ended."""
    starts, ends = [], []
    for raw in trace.raw_spans_of(document.get("spans") or []):
        starts.append(raw["start_ns"])
        if raw.get("end_ns") is not None:
            ends.append(raw["end_ns"])
    return (min(starts) if starts else None, max(ends) if ends else None)


def trial_name_of(document: dict) -> str:
    """``<root span name>__<7 hex of the trace id>`` — readable, and unique
    per trace the way a Harbor trial name is unique per attempt."""
    tid = str(document.get("session_id") or "")
    stem = _UNSAFE.sub("_", str(document.get("trial_name") or "")).strip("._-") or "trace"
    return f"{stem[:80]}__{tid[:7] or 'unknown'}"


def write_trial(document: dict, job_dir: str) -> dict:
    """One trace document as a trial directory. Returns a summary."""
    trial = trial_name_of(document)
    trial_dir = os.path.join(job_dir, trial)
    agent_dir = os.path.join(trial_dir, "agent")
    os.makedirs(agent_dir, exist_ok=True)

    start_ns, end_ns = _window(document)
    started = _iso(start_ns) if start_ns else None
    finished = _iso(end_ns) if end_ns else None
    tokens = (document.get("totals") or {}).get("tokens") or {}
    model = reader.model_of(document)
    agent = document.get("agent") or {}

    result = {
        "trial_name": trial,
        "task_name": f"{NAME}/{document.get('trial_name') or trial}",
        "source": NAME,
        "agent_info": {
            "name": agent.get("name") or "",
            "version": agent.get("version") or "",
            "model_info": {"name": model},
        },
        # Deliberately empty: a span's status is not a task's outcome, and no
        # verifier ran. The walker reads this pair as `unknown`.
        "exception_info": {},
        "verifier_result": {},
        "started_at": started,
        "finished_at": finished,
        "environment_setup": None,
        "agent_setup": None,
        "agent_execution": (
            {"started_at": started, "finished_at": finished} if started and finished else None),
        "verifier": None,
        "config": {"environment": {"type": ""}},
    }
    if tokens:
        result["agent_result"] = {
            "n_input_tokens": tokens.get("input_tokens", 0),
            "n_output_tokens": tokens.get("output_tokens", 0),
            "n_cache_tokens": tokens.get("cache_read_tokens", 0),
            "cost_usd": None,
        }
    _dump(os.path.join(trial_dir, "result.json"), result)
    _dump(os.path.join(agent_dir, "trace.json"), document)
    return {"trial_dir": trial, "started_at": started, "finished_at": finished,
            "model": model, "agent": agent.get("name") or ""}


def _write_job(documents: list, out_root: str, job_name: str, command: str,
               root: str, skipped: list) -> str:
    job_dir = os.path.join(out_root, job_name)
    os.makedirs(job_dir, exist_ok=True)
    summaries = [write_trial(doc, job_dir) for doc in documents]

    # Derived output this importer wholly owns: a trial the export no longer
    # produces is stale and would keep being counted.
    produced = {s["trial_dir"] for s in summaries}
    for name in os.listdir(job_dir):
        sub = os.path.join(job_dir, name)
        if os.path.isdir(sub) and name not in produced:
            shutil.rmtree(sub)

    starts = sorted(s["started_at"] for s in summaries if s["started_at"])
    ends = sorted(s["finished_at"] for s in summaries if s["finished_at"])
    _dump(os.path.join(job_dir, "result.json"), {
        "started_at": starts[0] if starts else None,
        "finished_at": ends[-1] if ends else None,
        "n_total_trials": len(summaries),
        "stats": {"n_errored_trials": 0, "n_running_trials": 0,
                  "n_pending_trials": 0, "n_retries": 0},
    })
    agents = sorted({s["agent"] for s in summaries if s["agent"]}) or [""]
    models = sorted({s["model"] for s in summaries if s["model"]})
    _dump(os.path.join(job_dir, "config.json"), {
        "job_name": job_name,
        "harness": NAME,
        "command": command,
        "agents": [{"name": agents[0], "model_name": models[0] if len(models) == 1 else "",
                    "kwargs": {}}],
        "datasets": [{"name": NAME, "path": root, "n_tasks": len(summaries)}],
        "import": {"format": reader.FORMAT, "conventions": reader.CONVENTIONS,
                   "skipped": skipped},
    })
    return job_dir


def convert(root: str, out_root: str, job_name: str = "",
            command: str = "") -> list[str]:
    """Convert every OTLP/JSON export under *root* into one job directory.

    Traces are grouped across files, one trial each. Files the reader refuses
    are listed in the job's ``config.json`` under ``import.skipped`` with their
    redacted reasons; when nothing reads, that list is the error.
    """
    paths = find_exports(root)
    skipped = refused(root)
    if not paths:
        raise SystemExit(redact(
            f"no OTLP/JSON trace exports (resourceSpans) found in {root}"
            + ("; refused: " + "; ".join(skipped) if skipped else "")))
    try:
        documents = reader.read(paths)
    except reader.ReadError as exc:       # a file changed under us since find_exports
        raise SystemExit(str(exc)) from None
    if not documents:
        raise SystemExit(redact(f"the OTLP/JSON exports in {root} carry no spans"))
    name = job_name or f"{NAME}__{os.path.basename(os.path.abspath(root)) or 'export'}"
    return [_write_job(documents, out_root, name, command, root, skipped)]
