"""What a run is doing, one line per stage: read by a person in a terminal,
and by the run's page.

A run spends minutes before its first unit - a host connected to, ``uv``
installed, this repository's ``app/`` sent, the docker base image built, an
agent installed, a dataset downloaded - and a log that only says "running"
looks stuck all that time. Each of those says where it is in one line::

    [nullius] stage=image state=start detail="building nullius/base:229ac6f71272 (first time on this machine)" at=2026-10-12T14:03:41Z
    [nullius] stage=install agent=codex@2.2.2 state=done seconds=41 at=...
    [nullius] stage=unit i=3 of=10 unit=HumanEval/2 state=done outcome=passed reward=1.0 at=...

``key=value`` words after the prefix; a value with a space, a quote or an
``=`` is a JSON string. ``state`` is ``start``, ``done``, ``cached`` (there
already: nothing to do), ``failed`` or ``progress`` (a download's bytes so
far); ``at`` is when, in UTC, on the machine that said it. ``stage=plan``
names the stages to come (``stages=image,install,units``), so a page can
show them waiting before they start.

The standard's ``cli`` writes them on stderr - its stdout stays the job's
path and the units, as before - and into the job's ``progress.jsonl``, one
JSON object per line, so a mirrored job carries them. ``execution`` writes
its own for what it does on a host before the run starts (connect, uv, the
tasks, app/), into the run's log; and reads every one that comes through the
part's log, so the page follows a run on another machine as it goes
(``fold``).

Nothing here is a secret: a detail names an image, a package, a file.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import IO, Iterable, List, Optional

PREFIX = "[nullius]"
FILE = "progress.jsonl"
#: where every reporter also writes its lines, unless it was given a stream: the cli sets it to stderr
ECHO: Optional[IO] = None
#: seconds between two lines of one download's bytes
EVERY = 5.0

# What the page calls each stage. One not here is shown by its own name.
LABELS = {"connect": "Connect", "uv": "uv", "tasks": "Send the tasks", "app": "Send app/", "harbor": "Harbor",
          "image": "Base image", "environment": "Environment", "upstream": "Benchmark source", "dataset": "Dataset",
          "driver": "Driver environment", "install": "Install", "runtime": "Agent runtime", "units": "Units"}

_WORD = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|\S*)')
_BARE = re.compile(r'^[^\s"=]+$')


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        v = round(v, 3)
        return str(int(v)) if v == int(v) and abs(v) >= 10 else str(v)
    s = str(v)
    return s if _BARE.match(s) else json.dumps(s, ensure_ascii=False)


def stamp(t: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def line(stage: str, state: Optional[str] = None, t: Optional[float] = None, **fields) -> str:
    """One stage's line: the prefix, ``stage``, the fields in the order given
    (``state`` second), ``at`` last. A field that is None is left out."""
    words = [f"stage={_value(stage)}"] + ([f"state={state}"] if state else [])
    words += [f"{k}={_value(v)}" for k, v in fields.items() if v is not None]
    return " ".join([PREFIX] + words + [f"at={stamp(t)}"])


def _plain(s: str):
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d*(?:[eE][-+]?\d+)?", s):
        return float(s)
    return {"true": True, "false": False}.get(s, s)


def parse(text: str) -> Optional[dict]:
    """The event a line says, or None for any other line. ``t`` is ``at`` in
    seconds since the epoch, when it is one."""
    s = text.strip()
    if not s.startswith(PREFIX + " "):
        return None
    ev = {}
    for k, v in _WORD.findall(s[len(PREFIX):]):
        if v.startswith('"'):
            try:
                ev[k] = json.loads(v)
            except ValueError:
                ev[k] = v.strip('"')
        else:
            ev[k] = _plain(v)
    if not isinstance(ev.get("stage"), str) or not ev["stage"]:
        return None
    try:
        ev["t"] = datetime.strptime(str(ev.get("at", "")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    return ev


def read(job_dir: str) -> List[dict]:
    """A job's ``progress.jsonl``: every event it holds, a line that is not
    one left out; [] when there is none."""
    out = []
    try:
        with open(os.path.join(job_dir, FILE), encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("stage"):
                    out.append(ev)
    except OSError:
        pass
    return out


def refusal(events: Iterable[dict]) -> Optional[str]:
    """Why the run was refused before its first unit, when it was: the detail
    of its ``refused`` event; None otherwise."""
    for ev in events:
        if ev.get("stage") == "refused":
            return str(ev.get("detail") or "refused")
    return None


class Reporter:
    """Says each stage: a line on ``echo`` (``ECHO`` when none is given;
    nowhere when that is None too) and an event in ``<job_dir>/progress.jsonl``
    (none without a job). Safe from several threads: units run at once."""

    def __init__(self, job_dir: Optional[str] = None, echo: Optional[IO] = None):
        self.job_dir = job_dir
        self.echo = echo
        self._lock = threading.Lock()

    def emit(self, stage: str, state: Optional[str] = None, **fields) -> dict:
        t = time.time()
        text = line(stage, state, t, **fields)
        ev = dict({"stage": stage}, **({"state": state} if state else {}),
                  **{k: v for k, v in fields.items() if v is not None}, at=stamp(t), t=round(t, 3))
        with self._lock:
            out = self.echo or ECHO
            if out is not None:
                try:
                    out.write(text + "\n")
                    out.flush()
                except (OSError, ValueError):
                    pass
            if self.job_dir:
                try:
                    os.makedirs(self.job_dir, exist_ok=True)
                    with open(os.path.join(self.job_dir, FILE), "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(ev) + "\n")
                except OSError:
                    pass
        return ev

    @contextlib.contextmanager
    def stage(self, stage: str, *, cached: bool = False, start: Optional[str] = None, **fields):
        """``with reporter.stage("install", agent="codex@2.2.2"):`` says it
        started, then that it is done with its seconds - or failed, with what
        was raised, which goes on up. ``cached``: there already, said once as
        such. ``start``: the start line's detail (what is being done)."""
        if cached:
            self.emit(stage, "cached", **fields)
            yield
            return
        t0 = time.monotonic()
        self.emit(stage, "start", **dict(fields, **({"detail": start} if start else {})))
        try:
            yield
        except BaseException as e:
            self.emit(stage, "failed", seconds=round(time.monotonic() - t0, 1), **dict(fields, detail=str(e)[:300] or type(e).__name__))
            raise
        self.emit(stage, "done", seconds=round(time.monotonic() - t0, 1), **fields)

    def download(self, stage: str, **fields):
        """A callback for a download's ``(done, total)`` bytes, said at most
        every ``EVERY`` seconds - and once more at the end, whatever the time."""
        last = [0.0]

        def say(done: int, total: Optional[int], final: bool = False):
            now = time.monotonic()
            if final or now - last[0] >= EVERY:
                last[0] = now
                self.emit(stage, "progress", bytes=int(done), total=int(total) if total else None, **fields)
        return say


def _key(ev: dict) -> str:
    """One stage of the list: its name, and what it is of when there are
    several (an environment's name, the agent installed)."""
    of = ev.get("name") or ev.get("agent") or ""
    return f"{ev['stage']}:{of}" if of else str(ev["stage"])


def label(ev: dict) -> str:
    base = LABELS.get(str(ev["stage"]), str(ev["stage"]))
    of = ev.get("name") or ev.get("agent")
    return f"{base} {of}" if of else base


def fold(events: Iterable[dict]) -> dict:
    """What the page draws, from the events in the order they came:

    - ``stages``: each stage once, in the order planned (``stage=plan``) or
      first said - ``waiting`` until it starts, then ``running`` (with ``t``,
      when it started), ``done`` or ``cached`` with its ``seconds``, or
      ``failed`` with its detail. A planned stage still waiting when a later
      one has started was not needed (a benchmark with nothing to download),
      and is left out.
    - ``units``: ``n`` to run, the ``noun``, how many ended and how: passed,
      failed, scored, errors; ``running`` the ones started and not ended. A
      run of several parts counts every part's: ``n`` grows as each begins.
    - ``current``: the stage running now, else None; ``ended``: ``done``,
      ``refused`` (with ``detail``) or None while the run goes on.
    """
    stages: dict = {}
    units = None
    ended, detail = None, None
    started_units = 0
    for ev in events:
        st = ev.get("stage")
        state = ev.get("state")
        if st == "plan":
            # a run of several parts (each agent, each dataset) says each part's plan: the one after an end goes on
            ended, detail = None, None
            for name in str(ev.get("stages") or "").split(","):
                name = name.strip()
                if name and name not in stages:
                    stages[name] = {"key": name, "stage": name, "label": LABELS.get(name, name), "state": "waiting"}
            continue
        if st == "unit":
            units = units or {"n": None, "noun": "units", "done": 0, "passed": 0, "failed": 0, "scored": 0, "errors": 0, "running": 0}
            if state == "start":
                started_units += 1
            elif state == "done":
                units["done"] += 1
                outcome = ev.get("outcome")
                bucket = {"passed": "passed", "failed": "failed", "scored": "scored"}.get(outcome, "errors")
                units[bucket] += 1
            units["running"] = max(0, started_units - units["done"])
            continue
        if st in ("done", "refused"):
            ended, detail = st, ev.get("detail")
            continue
        key = _key(ev)
        if key not in stages:
            # a planned stage, now said with what it is of: the plan's entry becomes it
            if key != st and st in stages and stages[st]["state"] == "waiting":
                stages = {(key if k == st else k): v for k, v in stages.items()}
                stages[key]["key"] = key
            stages.setdefault(key, {"key": key, "stage": st, "label": label(ev), "state": "waiting"})
        row = stages[key]
        row["label"] = label(ev)
        if ev.get("detail") is not None:
            row["detail"] = ev["detail"]
        if state == "start":
            row.update(state="running", t=ev.get("t"))
        elif state in ("done", "cached", "failed"):
            row["state"] = state
            if ev.get("seconds") is not None:
                row["seconds"] = ev["seconds"]
        elif state == "progress":
            if row["state"] == "waiting":
                row.update(state="running", t=ev.get("t"))
            row["bytes"], row["total"] = ev.get("bytes"), ev.get("total")
        if st == "units":
            units = units or {"n": None, "noun": "units", "done": 0, "passed": 0, "failed": 0, "scored": 0, "errors": 0, "running": 0}
            if ev.get("n") is not None and state == "start":
                units["n"] = (units["n"] or 0) + ev["n"]     # each part's units: the run's are all of them
            if ev.get("noun"):
                units["noun"] = ev["noun"]
    rows = list(stages.values())
    seen = [i for i, r in enumerate(rows) if r["state"] != "waiting"]
    last = seen[-1] if seen else -1
    rows = [r for i, r in enumerate(rows) if r["state"] != "waiting" or i > last]
    if ended:                                   # a run that ended has nothing running, nor anything still to come
        rows = [dict(r, state="failed") if r["state"] == "running" and ended == "refused" else r
                for r in rows if r["state"] != "waiting"]
    current = next((r for r in rows if r["state"] == "running"), None)
    return {"stages": rows, "units": units, "current": current["key"] if current else None, "ended": ended, "detail": detail}
