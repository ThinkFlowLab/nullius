"""ATIF trajectories from any producer: standalone documents in, one job out.

    PYTHONPATH=app python3 -m nullius.importers.cli trajectories/ -o jobs_imported
    PYTHONPATH=app python3 -m nullius.importers.cli agent/trajectory.json -o jobs_imported

An ATIF document is the file Harbor's agents write as ``agent/trajectory.json``
and the reader already opens inside a trial (``nullius.traces.trajectory``).
On its own — dropped on the import picker, or a directory of them with no
``result.json`` beside — nothing recognised it. This does, by content: a JSON
object whose ``schema_version`` reads ``ATIF-v1.x`` and which carries
``steps``. The producer's name, the file's name and the directory it sits in
are not consulted, so a trajectory from claude-code, opencode or
any other adopter of the schema is read the same way, and two copies of one
document normalise identically wherever they were found.

**Supported schema versions**: ``ATIF-v1.0`` through ``ATIF-v1.7``
(``SUPPORTED_VERSIONS``) — the versions Nullius already parses inside a job,
as written by Harbor's agents (``final_metrics``, per-step
``metrics``, the ``extra`` usage slot). A document of any other version is
refused by name rather than read as though it were one of these.

The app walks Harbor's shape, so this converts: one directory (or one file) becomes one job, each document
a trial directory holding the document *copied byte for byte* as
``agent/trajectory.json``. Nothing is parsed twice — ``trajectory.load`` is
the one reader, here and on the trial page — and nothing is re-measured:

- **No benchmark outcome is invented.** A trajectory records what an agent
  did, not whether it was right. The trial carries no ``verifier_result``,
  no reward and no ``verifier/`` marker, so it reads as ``unknown`` — never
  a score, never a solve rate — and stays out of every rate.
- **Usage and timing are the document's own, or absent.** ``agent_result``
  copies ``final_metrics`` (prompt, completion, cached, cost) and is null
  where the document is silent; the trial window is the span of the step
  timestamps, and null when no step carries one. Nothing is zeroed to fill
  a column: "unmeasured" and "zero" are different claims.
- **Provenance is the document's.** ``agent_info`` comes from the ATIF
  ``agent`` block (name, version, model_name), falling back to the first
  step's ``model_name`` for the model; the schema version, session id and
  the source file's path and digest are kept on the trial record under
  ``source_document``.
- **The recorded command is the import.** No harness ran here, so the line
  the job page offers to copy is the one that reproduces this job
  directory, not a ``harbor run`` that never happened.

A tree this claims but cannot read is refused before anything is written,
with a ``SystemExit`` whose message names the file (relative to the tree
given) and what is wrong — a document that is not JSON, one without
``steps``, one of an unsupported version — and is redacted: a credential in
a bad file's name or content must not travel into the error. A ``.json``
that is well-formed but not ATIF at all (a ``trace.json``, a
``spans.meta.json``) is simply not one of ours and is passed over.

What this does *not* claim: a Harbor job or trial (a ``result.json`` on the
walk). Those trajectories belong to the record around them, whose accounting
is the reader's and must not be shadowed by a second, thinner reading of the same
file.

Re-converting is idempotent: trial names are
deterministic (the document's path relative to the tree, a bare
``trajectory.json`` named after its directory), every file is rewritten in
place, and a trial the tree no longer holds is removed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
from datetime import timezone

from nullius.redact import redact
from nullius.traces.trajectory import parse_ts

NAME = "atif"
LABEL = "ATIF"

# The schema versions this reads — the ones `trajectory.py` is known to
# parse inside a Harbor job. Anchoring on the list is what keeps detect()
# from claiming a future shape and reading it as one of these.
SUPPORTED_VERSIONS = tuple(f"ATIF-v1.{minor}" for minor in range(8))

# What makes a JSON object an ATIF document at all, whatever its version:
# the schema tag. Shape (steps present, a list) is checked when read, so a
# partial document is refused by name rather than passed over as foreign.
_VERSION_RE = re.compile(r"^ATIF-v\d+\.\d+$")

# A file that is not JSON is only *ours* to refuse when it says it is an
# ATIF document; sniffed in the head of the file, since it cannot be parsed.
_MARKER = b'"ATIF-v'
_SNIFF_BYTES = 4096

# Files that mark a directory as part of a record another reader owns.
_OWNED_BY = ("result.json",)



def _is_atif(doc) -> bool:
    return (isinstance(doc, dict)
            and isinstance(doc.get("schema_version"), str)
            and bool(_VERSION_RE.match(doc["schema_version"])))


def _mentions_atif(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return _MARKER in fh.read(_SNIFF_BYTES)
    except OSError:
        return False


def _read(path: str, shown: str) -> dict:
    """The document at *path*, or a SystemExit naming what is wrong with it.

    The message is what the CLI prints and the picker shows, so it is
    redacted here, at construction, whichever branch built it.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as e:
        raise SystemExit(redact(f"{shown}: cannot be read ({e.strerror})")) from None
    except ValueError as e:
        raise SystemExit(redact(f"{shown}: not JSON ({e})")) from None
    if not _is_atif(doc):
        raise SystemExit(redact(f"{shown}: not an ATIF document"))
    version = doc["schema_version"]
    if version not in SUPPORTED_VERSIONS:
        raise SystemExit(redact(
            f"{shown}: unsupported ATIF schema_version {version!r} "
            f"(supported: {SUPPORTED_VERSIONS[0]} through {SUPPORTED_VERSIONS[-1]})"))
    if "steps" not in doc:
        raise SystemExit(redact(f"{shown}: no steps (a partial document)"))
    if not isinstance(doc["steps"], list):
        raise SystemExit(redact(f"{shown}: steps is not a list"))
    return doc


def _claims(path: str) -> bool:
    """Is this file ours — an ATIF document, or a broken file that says it is?"""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError:
        return False
    except ValueError:
        return _mentions_atif(path)
    return _is_atif(doc)


def find_documents(root: str) -> list[str]:
    """Every file under *root* this importer claims, sorted; *root* itself
    when it is one file. Empty when a ``result.json`` is
    seen on the walk — that tree is a record another reader owns."""
    if os.path.isfile(root):
        return [root] if _claims(root) else []
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if any(name in filenames for name in _OWNED_BY):
            return []
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if name.endswith(".json") and _claims(path):
                found.append(path)
    return found


def detect(path: str) -> bool:
    """Does *path* hold standalone ATIF documents (or is it one)?"""
    return bool(find_documents(path))


def check(path: str) -> None:
    """Read every document the way `convert` would, writing nothing; raises
    ``SystemExit`` (redacted) for the first thing `convert` would refuse."""
    _read_all(path)


def trees(root: str) -> list[str]:
    """One directory, one job, whatever its depth — the CLI's reading of
    *root*, shared by the picker. The documents found under it are
    that job's trials, named by their place in the tree (`_trial_name`), so
    a nested directory of trajectories is one job of several trials rather
    than a job per child."""
    return [root] if detect(root) else []


def _shown(path: str, root: str) -> str:
    """How a file is named in a message: relative to the tree given."""
    if os.path.isfile(root):
        return os.path.basename(path)
    return os.path.relpath(path, root)


def _read_all(root: str) -> list[tuple[str, dict]]:
    paths = find_documents(root)
    if not paths:
        raise SystemExit(redact(
            f"no ATIF trajectories (schema_version {SUPPORTED_VERSIONS[0]} through "
            f"{SUPPORTED_VERSIONS[-1]}) found in {root}"))
    return [(path, _read(path, _shown(path, root))) for path in paths]



def _trial_name(path: str, root: str) -> str:
    """The document's place in the tree, flattened for a directory name.

    A bare ``trajectory.json`` is named after the directory holding it, so
    ``claude-code/trajectory.json`` and ``claude-code.json`` are the same
    trial under either layout. A single file, or a ``trajectory.json`` at
    the root itself, takes the file's own stem.
    """
    if os.path.isfile(root):
        rel = os.path.basename(path)
    else:
        rel = os.path.relpath(path, root)
    stem = rel[:-len(".json")] if rel.endswith(".json") else rel
    parts = [p for p in stem.split(os.sep) if p and p != "."]
    if len(parts) > 1 and parts[-1] == "trajectory":
        parts.pop()
    return "__".join(parts) or "trajectory"


def _instant(stamp: str) -> float:
    """A stamp as seconds since the epoch, a naive one read as UTC: what the
    job's first start and last end are chosen by."""
    when = parse_ts(stamp)
    if when is None:
        return float("inf")
    return (when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when).timestamp()


def _window(doc: dict) -> tuple[str | None, str | None]:
    """The first and last step timestamps that parse, in step order and as
    recorded; None when no step carries a clock — the trial then has no
    duration, not a zero one. Step order rather than min/max: the document's
    ordering is preserved, and two clocks of different kinds (one naive, one
    zoned) are never compared."""
    stamps = [s.get("timestamp") for s in doc["steps"]
              if isinstance(s, dict) and parse_ts(s.get("timestamp")) is not None]
    if not stamps:
        return None, None
    return stamps[0], stamps[-1]


def _agent_info(doc: dict) -> dict:
    agent = doc.get("agent") if isinstance(doc.get("agent"), dict) else {}
    model = agent.get("model_name") or next(
        (s.get("model_name") for s in doc["steps"]
         if isinstance(s, dict) and s.get("model_name")), "")
    return {
        "name": str(agent.get("name") or ""),
        "version": str(agent.get("version") or ""),
        "model_info": {"name": str(model or "")},
    }


def _usage(doc: dict) -> dict:
    """``final_metrics`` in the fields the reader sums — null where absent."""
    metrics = doc.get("final_metrics") if isinstance(doc.get("final_metrics"), dict) else {}

    def number(key):
        value = metrics.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return {
        "n_input_tokens": number("total_prompt_tokens"),
        "n_output_tokens": number("total_completion_tokens"),
        "n_cache_tokens": number("total_cached_tokens"),
        "cost_usd": number("total_cost_usd"),
    }


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── one document = one trial ────────────────────────────────────────────────

def convert_document(path: str, doc: dict, trial: str, job_dir: str) -> dict:
    """Write one document as a Harbor-shaped trial directory. Returns a summary."""
    trial_dir = os.path.join(job_dir, trial)
    agent_dir = os.path.join(trial_dir, "agent")
    os.makedirs(agent_dir, exist_ok=True)

    started, finished = _window(doc)
    result = {
        "trial_name": trial,
        "task_name": f"{NAME}/{trial}",
        # The harness, not a dataset: the benchmark readers stay unengaged,
        # and with no report and no reward the outcome is `unknown`.
        "source": NAME,
        "agent_info": _agent_info(doc),
        "exception_info": {},
        "verifier_result": {},
        "started_at": started,
        "finished_at": finished,
        "environment_setup": None,
        "agent_setup": None,
        # The document is the agent's own window; what surrounded it (image
        # pull, setup, a verifier) was never recorded here.
        "agent_execution": (
            {"started_at": started, "finished_at": finished} if started else None),
        "verifier": None,
        "agent_result": _usage(doc),
        "source_document": {
            "path": os.path.abspath(path),
            "sha256": _sha256(path),
            "schema_version": doc["schema_version"],
            "session_id": doc.get("session_id"),
        },
    }
    with open(os.path.join(trial_dir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)

    # Byte for byte: the trial page reads what the producer wrote, and a
    # digest of the copy is a digest of the source.
    shutil.copyfile(path, os.path.join(agent_dir, "trajectory.json"))

    return {"trial_dir": trial, "started_at": started, "finished_at": finished,
            "agent_info": result["agent_info"]}



def _write_job(docs: list, root: str, out_root: str, job_name: str, command: str) -> str:
    """The read documents, as one job directory of one trial each."""
    job_dir = os.path.join(out_root, job_name)
    os.makedirs(job_dir, exist_ok=True)

    summaries = [convert_document(path, doc, _trial_name(path, root), job_dir)
                 for path, doc in docs]
    # Derived output this importer wholly owns: a trial the tree no longer
    # holds is stale and would keep being counted.
    produced = {s["trial_dir"] for s in summaries}
    for name in os.listdir(job_dir):
        sub = os.path.join(job_dir, name)
        if os.path.isdir(sub) and name not in produced:
            shutil.rmtree(sub)

    # ordered as instants, not as text: one stamp with a zone and one without sort apart as strings
    starts = sorted((s["started_at"] for s in summaries if s["started_at"]), key=_instant)
    ends = sorted((s["finished_at"] for s in summaries if s["finished_at"]), key=_instant)
    with open(os.path.join(job_dir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "started_at": starts[0] if starts else None,
            "finished_at": ends[-1] if ends else None,
            "n_total_trials": len(summaries),
            "stats": {"n_errored_trials": 0, "n_running_trials": 0,
                      "n_pending_trials": 0, "n_retries": 0},
        }, fh, indent=2)

    # The first document's producer names the job; a tree that mixes
    # producers still shows each trial's own under agent_info.
    agent = next((s["agent_info"] for s in summaries if s["agent_info"]["name"]),
                 summaries[0]["agent_info"])
    with open(os.path.join(job_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "job_name": job_name,
            "harness": NAME,   # provenance; the walker ignores unknown keys
            "command": command or (
                f"PYTHONPATH=app python3 -m nullius.importers.cli {shlex.quote(root)} "
                f"-o {shlex.quote(out_root)} --job-name {shlex.quote(job_name)}"),
            "agents": [{"name": agent["name"],
                        "model_name": agent["model_info"]["name"]}],
            "tasks": [{"name": f"{NAME}/{s['trial_dir']}"} for s in summaries],
        }, fh, indent=2)
    return job_dir


def convert(root: str, out_root: str, job_name: str = "",
            command: str = "") -> list[str]:
    """Convert the documents at *root* — a directory of them or one file —
    into one job. Every document is read before anything is written, so a
    refused tree leaves no half-written job behind."""
    docs = _read_all(root)
    if not job_name:
        base = os.path.basename(os.path.normpath(root))
        if os.path.isfile(root):
            base = _trial_name(root, root)
        job_name = f"{NAME}__{base}"
    return [_write_job(docs, root, out_root, job_name, command)]
