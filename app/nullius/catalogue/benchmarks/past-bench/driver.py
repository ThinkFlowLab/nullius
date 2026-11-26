"""PAST-Bench (official, 26 families) on the Nullius standard.

The benchmark is the publisher's: 26 task families over four abilities, 211
episodes, each family an ordered sequence in which an early episode teaches and
later episodes ask, in different words, for what was taught. What it measures
is not one answer but whether the teaching survived: the same family run with
its persistence taken away is the control. This driver is glue only:

units  - the families the chosen dataset names. A family is one unit because
         its episodes run in the publisher's order, share a workspace and are
         judged as a sequence; a unit is a session that starts again between
         episodes while the workspace stays.
setup  - the family's episodes from the publisher's own sequence manifest
         (``configs/self_evolve_v2/*_only.yaml``: order, fresh-session rule,
         history anchors, whether persistence is allowed), the first episode's
         mock services on ports this driver picks, and the tools those
         services expose, as an MCP server of the benchmark's own (``past_mcp.py``)
turn   - one episode per turn. The agent's reply is kept with the episode's
         service audit logs and its tool calls; then the next episode's
         services start and its prompt goes out, in a new session when the
         manifest says ``requires_fresh_session``.
score  - each episode through the publisher's own grader for that episode
         (``<episode>/grader.py``, ``expectations.json``) and the publisher's
         own composite (``0.8*completion + 0.2*robustness``, times safety),
         then the family's buckets: cold, learn, eval_near, eval_far, control.

Two arms, two datasets: ``past-bench`` keeps persistence, and ``past-bench-nop``
runs the same families with the workspace reset before every episode - the
control the publisher's AblationLift is measured against. The agent's own
memory store (its home directory) belongs to the unit and is not addressable
here, so a fresh session and a cleaned workspace are what "no persistence"
means for this driver; the publisher's own no-persistence episodes
(``persistence_disabled``) are honoured inside both arms.

The publisher's harness (its runner, sandbox and simulated tools) is not used:
the Nullius agent under test takes the agent's place, the benchmark's tools
come over MCP, and its services run beside the driver.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tarfile
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import services as mock                                            # noqa: E402

UPSTREAM = Path(os.environ["NULLIUS_UPSTREAM"]).resolve()
SRC = UPSTREAM / "src"
TASKS = UPSTREAM / "self-evolve-tasks-v2"
MANIFESTS = UPSTREAM / "configs" / "self_evolve_v2"
MODEL_ID = "nullius"
STATE: Dict[str, dict] = {}
DATASET_ID = "past-bench"
# LOCAL PATCH (nullius): the agent under test may run on one provider while the
# episode judges stay on another, so scores keep coming from the provider
# earlier runs were scored on. The file holds {"base_url", "key"}.
EVAL_ROUTE = Path("/root/.config/nullius/eval-route.json")


def eval_route(route):
    """The judge's route, moved to the evaluation provider when one is set."""
    if not isinstance(route, dict):
        return route
    try:
        got = json.loads(EVAL_ROUTE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return route
    base, key = got.get("base_url"), got.get("key")
    if not (base and key):
        return route
    return dict(route, base_url=base, key=key)

PRELUDE = (
    "This session works through a series of tasks for the same user. That is all this message says: "
    "the tasks follow one at a time, each as its own message, and your final message answers the task "
    "in front of you. Tools for the current task are the MCP tools given to you; they change with the "
    "task. Do not act on this message - reply READY and wait for the first task."
)

DATASETS: Dict[str, tuple] = {
    "past-bench": ("persist", None),
    "past-bench-nop": ("nop", None),
    "past-bench-isolated": ("isolated", None),
    "past-bench-smoke": ("persist", ["SM01_preference_adoption"]),
    "past-bench-smoke-nop": ("nop", ["SM01_preference_adoption"]),
    "past-bench-smoke-isolated": ("isolated", ["SM01_preference_adoption"]),
    "past-bench-memory": ("persist", ["memory_ability"]),
    "past-bench-procedural": ("persist", ["procedural_ability"]),
    "past-bench-information-gathering": ("persist", ["proactive_information_gathering"]),
    "past-bench-update": ("persist", ["update_ability"]),
}
NOP = ".nop"          # the control arm's unit id: a driver process does not see the dataset
ISO = ".iso."         # one evaluation episode alone, in an instance that remembers nothing


def arm_of(unit: str) -> tuple:
    """A unit id says its arm, because ``setup`` never learns the dataset's name.

    ``persist``: the family's whole sequence, one instance, workspace kept.
    ``nop``: the same sequence with the workspace reset before every episode -
             a workspace-only control; the agent's own memory lives in its home,
             which belongs to the instance and survives.
    ``isolated``: one evaluation episode alone in its own unit - own container,
             own home, own workspace, no learn episodes at all. This is the
             control an ablation needs: nothing can carry over.
    """
    if unit.endswith(NOP):
        return unit[:-len(NOP)], "nop"
    if ISO in unit:
        return unit.split(ISO, 1)[0], "isolated"
    return unit, "persist"


# ── the publisher's own files ────────────────────────────────────────────────
def upstream():
    """The publisher's models, graders and composite, imported once per driver."""
    if "modules" not in STATE:
        sys.path.insert(0, str(SRC))
        sys.path.insert(0, str(UPSTREAM))
        from past_bench.models.task import TaskDefinition
        from past_bench.models.trace import MediaLoad, ToolDispatch, TraceMessage
        from past_bench.models.message import Message
        from past_bench.models.scoring import compute_task_score
        from past_bench.graders.base import AbstractGrader
        from past_bench.graders.llm_judge import LLMJudge
        STATE["modules"] = {"TaskDefinition": TaskDefinition, "TraceMessage": TraceMessage,
                            "ToolDispatch": ToolDispatch, "MediaLoad": MediaLoad,
                            "Message": Message, "compute_task_score": compute_task_score,
                            "AbstractGrader": AbstractGrader, "LLMJudge": LLMJudge}
    return STATE["modules"]


def families() -> Dict[str, dict]:
    """Every family on disk, keyed by id, with the ability folder it sits in."""
    out = {}
    for path in sorted(TASKS.glob("*/*/family.yaml")):
        meta = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out[path.parent.name] = {"id": path.parent.name, "ability": path.parent.parent.name,
                                 "dir": path.parent, "meta": meta}
    return out


def manifests() -> Dict[str, dict]:
    """The publisher's sequence manifests, keyed by the family each one covers."""
    out = {}
    for path in sorted(MANIFESTS.glob("*_only.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for episode in data.get("episodes") or []:
            family = episode.get("family_id")
            if family:
                out.setdefault(family, {"path": path, "episodes": data["episodes"]})
    return out


def episodes(family: str) -> List[dict]:
    """One family's sequence: the manifest's order and its rules, per episode."""
    got = manifests().get(family)
    if not got:
        raise ValueError(f"{family}: the publisher ships no sequence manifest")
    out = []
    for episode in got["episodes"]:
        path = (got["path"].parent / str(episode["task"])).resolve()
        if path.is_dir():
            path = path / "task.yaml"
        task = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out.append({
            "dir": path.parent, "yaml": path, "family": family,
            "stage": str(episode.get("stage") or episode.get("bucket") or "episode"),
            "bucket": str(episode.get("bucket") or "episode"),
            "label": str(episode.get("label") or path.parent.name),
            "fresh": bool(episode.get("requires_fresh_session")),
            "persistence": bool(episode.get("persistence_allowed", True)),
            "mode": str(episode.get("history_mode") or "continue"),
            "save_anchor": str(episode.get("history_save_anchor") or ""),
            "load_anchor": str(episode.get("history_load_anchor") or ""),
            "task": task,
            "prompt": str((task.get("prompt") or {}).get("text") or ""),
        })
    return out


def units(dataset, setup=None):
    """The dataset's units: every family of the ability it names, or the one it names."""
    global DATASET_ID
    if dataset not in DATASETS:
        raise ValueError(f"unknown PAST-Bench dataset {dataset!r}")
    DATASET_ID = dataset
    arm, which = DATASETS[dataset]
    known = families()
    if which is None:
        chosen = sorted(known)
    else:
        chosen = sorted(f for f in known
                        if f in which or known[f]["ability"] in which)
    if not chosen:
        raise ValueError(f"{dataset}: no family matches {which!r}")
    if arm == "isolated":
        # One unit per evaluation episode: its own instance, so no learn episode,
        # no earlier session and no agent home can reach it.
        listed = [f"{family}{ISO}{e['dir'].name}" for family in chosen
                  for e in episodes(family) if e["bucket"] == "evaluation"]
        if not listed:
            raise ValueError(f"{dataset}: no evaluation episode to isolate")
        return {"units": listed}
    for family in chosen:
        episodes(family)
    return {"units": [f + NOP if arm == "nop" else f for f in chosen]}


# ── the unit ─────────────────────────────────────────────────────────────────
def _token(unit: str) -> str:
    return hashlib.sha256(f"{unit}:{os.getpid()}:{time.time()}".encode()).hexdigest()[:12]


def _state_dir(workspace: str, unit: str) -> Path:
    """Where the driver keeps its evidence: outside the workspace the agent may read."""
    trial = Path(workspace).resolve().parent
    job = trial.parent
    for root in (job, trial):
        try:
            path = root / ".past-bench" / f"{unit}-{_token(unit)}"
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue
    path = Path(workspace).resolve() / ".past-bench-state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def snapshot(workspace: Path, path: Path) -> None:
    """The workspace as it stands, tarred: what a history anchor restores."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tar:
        for item in workspace.iterdir():
            if item.name.startswith(".past-bench"):
                continue
            tar.add(item, arcname=item.name)


def restore(workspace: Path, path: Path) -> None:
    """The workspace back to a snapshot: everything the agent wrote since, gone."""
    for item in workspace.iterdir():
        if item.name.startswith(".past-bench"):
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item, ignore_errors=True)
        else:
            try:
                item.unlink()
            except OSError:
                pass
    if path.exists():
        with tarfile.open(path, "r") as tar:
            tar.extractall(workspace)


def tools_for(task: dict, service_ports: Dict[str, int]) -> List[dict]:
    """The episode's tools as MCP tools: its own schema, its service's port."""
    endpoints = {str(e.get("tool_name")): e for e in task.get("tool_endpoints") or [] if isinstance(e, dict)}
    specs = task.get("services") or []
    out = []
    for tool in task.get("tools") or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        endpoint = endpoints.get(str(tool["name"])) or {}
        url = str(endpoint.get("url") or "")
        if url:
            url = mock.rewrite(url, specs, service_ports)
        out.append({"name": str(tool["name"]), "description": str(tool.get("description") or ""),
                    "input_schema": tool.get("input_schema") or {"type": "object", "properties": {}},
                    "url": url, "method": str(endpoint.get("method") or "POST")})
    return out


def control(state: dict, index: int) -> None:
    """Tell the MCP server which episode is in front of the agent."""
    episode = state["episodes"][index]
    payload = {"episode": episode["label"], "stage": episode["stage"], "index": index,
               "family": state["family"], "tools": state["tools"]}
    path = state["state"] / "current.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def open_episode(state: dict, index: int) -> str:
    """Start the episode's services and hand back its prompt text."""
    episode = state["episodes"][index]
    if state["services"] is not None:
        state["services"].stop()
    workspace = Path(state["dir"])
    if state["arm"] in ("nop", "isolated") or not episode["persistence"]:
        restore(workspace, state["state"] / "anchors" / "initial.tar")
    elif episode["load_anchor"] and (state["state"] / "anchors" / f"{episode['load_anchor']}.tar").exists():
        restore(workspace, state["state"] / "anchors" / f"{episode['load_anchor']}.tar")
    episode_services = mock.Episode(UPSTREAM, episode["task"], sys.executable)
    ports = episode_services.start()
    state["services"] = episode_services
    state["tools"] = tools_for(episode["task"], ports)
    state["dispatch_at"] = dispatch_count(state)     # this episode's tool calls start here
    state["index"] = index
    control(state, index)
    return episode["prompt"]


def dispatch_path(state: dict) -> Path:
    return state["state"] / "dispatch.jsonl"


def setup(unit, dir):
    """The family's episodes, the first one's services, and the benchmark's tools."""
    family, arm = arm_of(unit)
    episodes_here = episodes(family)
    if arm == "isolated":
        name = unit.split(ISO, 1)[1]
        episodes_here = [e for e in episodes_here if e["dir"].name == name]
        if len(episodes_here) != 1:
            raise ValueError(f"{unit}: {len(episodes_here)} episodes match that name")
    workspace = Path(os.path.abspath(dir))
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = _state_dir(str(workspace), family)
    state = {"family": family, "unit": unit, "dir": str(workspace), "arm": arm,
             "dataset": {"persist": "past-bench", "nop": "past-bench-nop",
                         "isolated": "past-bench-isolated"}[arm],
             "episodes": episodes_here, "index": -1, "services": None, "tools": [],
             "evidence": [], "state": state_dir, "dispatch_at": 0, "awaiting": "prelude",
             "started": datetime.now().strftime("%Y%m%d_%H%M%S")}
    STATE[unit] = state
    snapshot(workspace, state_dir / "anchors" / "initial.tar")
    dispatch_path(state).write_text("", encoding="utf-8")
    open_episode(state, 0)
    return {"prompt": PRELUDE, "agent_seconds": 21600,
            "mcp": [{"name": "past", "server": "past", "args": [str(state_dir)]}]}


def dispatch_count(state: dict) -> int:
    try:
        return sum(1 for _ in dispatch_path(state).open(encoding="utf-8"))
    except OSError:
        return 0


def dispatches_since(state: dict, start: int) -> List[dict]:
    out = []
    try:
        with dispatch_path(state).open(encoding="utf-8") as f:
            for n, line in enumerate(f):
                if n < start:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def last_agent(transcript) -> str:
    for item in reversed(transcript or []):
        if item.get("role") == "agent":
            return str(item.get("text") or "")
    return ""


def capture(state: dict, reply: str) -> None:
    """Keep one episode's evidence: what was asked, what was said, what ran, what it touched."""
    episode = state["episodes"][state["index"]]
    audit = state["services"].audit() if state["services"] is not None else {}
    state["evidence"].append({
        "index": state["index"], "stage": episode["stage"], "bucket": episode["bucket"],
        "label": episode["label"], "dir": str(episode["dir"]), "yaml": str(episode["yaml"]),
        "prompt": episode["prompt"], "reply": reply, "audit": audit,
        "dispatches": dispatches_since(state, state["dispatch_at"]),
    })
    if episode["save_anchor"]:
        snapshot(Path(state["dir"]), state["state"] / "anchors" / f"{episode['save_anchor']}.tar")


def turn(unit, dir, transcript, simulator=None, judge=None, attempt=None):
    """One message at a time: the prelude first, then each episode, until none is left."""
    state = STATE.get(unit)
    if state is None:
        raise ValueError(f"turn before setup for {unit!r}")
    if state["awaiting"] == "prelude":        # the agent only said READY: the first task comes now
        state["awaiting"] = "episode"
        return {"message": state["episodes"][0]["prompt"]}
    capture(state, last_agent(transcript))
    if state["index"] + 1 >= len(state["episodes"]):
        return {"done": True}
    index = state["index"] + 1
    text = open_episode(state, index)
    fresh = state["episodes"][index]["fresh"] or state["arm"] in ("nop", "isolated") or \
        not state["episodes"][index]["persistence"]
    return {"message": text, "restart": bool(fresh)}


# ── grading: the publisher's graders, over what the driver kept ──────────────
def grader_for(entry: dict):
    path = Path(entry["dir"]) / "grader.py"
    spec = importlib.util.spec_from_file_location(f"past_grader_{abs(hash(entry['label']))}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no grader at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = upstream()["AbstractGrader"]
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and issubclass(obj, base) and obj is not base and obj.__module__ == module.__name__:
            return obj()
    raise ValueError(f"{path} defines no grader")


def grade(entry: dict, judge) -> float:
    """One episode, through its own grader: 0.8*completion + 0.2*robustness, times safety."""
    mods = upstream()
    task = mods["TaskDefinition"].from_yaml(entry["yaml"])
    trace_id = f"{MODEL_ID}:{entry['label']}"
    messages = []
    for role, text in (("user", entry["prompt"]), ("assistant", entry["reply"])):
        messages.append(mods["TraceMessage"](trace_id=trace_id,
                                             message=mods["Message"](role=role, content=text)))
    dispatches = []
    for i, call in enumerate(entry["dispatches"]):
        dispatches.append(mods["ToolDispatch"](
            trace_id=trace_id, tool_use_id=f"{trace_id}-{i}", tool_name=str(call.get("tool") or ""),
            endpoint_url=str(call.get("endpoint_url") or ""),
            request_body=call.get("request_body") if isinstance(call.get("request_body"), dict) else {},
            response_status=int(call.get("response_status") or 200),
            response_body=call.get("response_body"),
            latency_ms=float(call.get("latency_ms") or 0.0)))
    grader = grader_for(entry)
    scores = grader.grade(messages, dispatches, task,
                          audit_data=entry["audit"] or {}, judge=judge, media_events=[])
    return float(mods["compute_task_score"](scores)), scores


def mean(values: List[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def score(unit, dir, transcript, answer, judge=None, **_):
    """The family's buckets, and the number the run shows: the evaluation episodes' mean."""
    state = STATE.get(unit)
    if state is None:
        return {"reward": 0.0, "detail": {"reason": "no conversation", "dataset": DATASET_ID}}
    try:
        if state["index"] >= 0 and (not state["evidence"] or state["evidence"][-1]["index"] != state["index"]):
            text = last_agent(transcript)
            if text:
                capture(state, text)
        if not state["evidence"]:
            return {"reward": 0.0, "detail": {"reason": "no episode was answered", "dataset": DATASET_ID}}
        if not isinstance(judge, dict):
            raise ValueError("PAST-Bench's graders need the judge route")
        judge = eval_route(judge)             # the episode judges are evaluation, not the agent
        mods = upstream()
        llm = mods["LLMJudge"](model_id=judge["model"], api_key=judge["key"], base_url=judge["base_url"])
        graded = []
        for entry in state["evidence"]:
            item = {"stage": entry["stage"], "bucket": entry["bucket"], "label": entry["label"]}
            try:
                value, dimensions = grade(entry, llm)
                item.update({"score": round(value, 4), "completion": dimensions.completion,
                             "robustness": dimensions.robustness, "safety": dimensions.safety})
            except Exception as exc:       # one episode's grader failing is that episode's score
                item.update({"score": 0.0, "error": f"{type(exc).__name__}: {exc}",
                             "trace": traceback.format_exc()[-1200:]})
            graded.append(item)
        def by(prefix: str) -> List[float]:
            return [g["score"] for g in graded if g["stage"] == prefix or g["stage"].startswith(prefix + "_")]
        evaluation = [g["score"] for g in graded if g["bucket"] == "evaluation"]
        cold, learn = by("cold"), by("learn")
        near, far = by("eval_near"), by("eval_far")
        controls = [g["score"] for g in graded if g["bucket"] == "control"]
        reward = mean(evaluation if evaluation else [g["score"] for g in graded])
        lift = round(mean(evaluation) - cold[0], 4) if evaluation and cold else None
        reuse = round(sum(1 for v in evaluation if v >= 0.5) / len(evaluation), 4) if evaluation else 0.0
        transfer = round(min(1.0, mean(far) / mean(near)), 4) if far and near and mean(near) > 0 else None
        evolve = None
        if lift is not None and transfer is not None:
            evolve = round(max(0.0, lift) * reuse * transfer, 4)
        return {"reward": reward,
                "detail": {"family": state["family"], "arm": state["arm"], "dataset": state["dataset"],
                           "episodes": len(graded), "cold": mean(cold), "learn": mean(learn),
                           "eval_near": near, "eval_far": far, "control": mean(controls),
                           "lift": lift, "reuse": reuse, "transfer": transfer,
                           "evolve": evolve, "judge": judge.get("model"),
                           "graded": graded},
                "parts": {"n": len(graded), "by": "episode",
                          "parts": [{"n": i + 1, "text": g["label"], "group": g["bucket"],
                                     "score": g["score"]} for i, g in enumerate(graded)]}}
    finally:
        if state.get("services") is not None:
            state["services"].stop()
            state["services"] = None


HANDLERS = {"units": units, "setup": setup, "turn": turn, "score": score}
# The publisher's graders print to stdout, which is this driver's JSON channel.
_REPLY = sys.stdout
sys.stdout = sys.stderr
for line in sys.stdin:
    try:
        request = json.loads(line)
        result = HANDLERS[request.pop("op")](**request)
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False, default=str), file=_REPLY, flush=True)
    _REPLY.flush()
