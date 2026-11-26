"""Pi-Bench (official, 100 tasks) on the Nullius standard.

The benchmark is the publisher's: 100 multi-turn tasks across five personas,
20 tasks each, drawn out by the upstream's own hidden-intent user simulator and
scored by the upstream's own checklist and proactiveness evaluators. This
driver is glue only:

units  - the personas the chosen dataset names; a persona is one unit because
         its tasks run in the upstream's episode order, share a workspace and
         depend on earlier tasks
setup  - the upstream's data checked, the unit's prompt, and the persona's
         AppWorld tools as an MCP server (``appworld_mcp.py`` starts the
         official image's API and serves the upstream's own ``appworld serve
         mcp stdio``)
turn   - the upstream's ``UserAgent`` and ``BenchmarkRunner`` in a thread of
         this driver, its own hidden-intent simulator and termination rules;
         the agent's final message in each turn is what the runner receives
score  - the unit's turns written in the upstream nanobot trace-log shape, then
         the upstream's ``TraceEvaluationRunner`` (checklist + proactiveness)
         run over them; COMP is the reward, PROC is reported beside it

The official harness's nanobot agent is not used: the Nullius agent under test
takes its place, with the persona's AppWorld tools over MCP. The official
Docker image provides AppWorld, not the agent.
"""
import asyncio
import glob
import hashlib
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

UPSTREAM = Path(os.environ["NULLIUS_UPSTREAM"])
DATA = UPSTREAM / "data"
HERE = Path(__file__).resolve().parent
HISTORY = UPSTREAM / "config" / "bench" / "evaluation" / "trace_history.yaml"
MODEL_ID = "nullius"                      # the trace/log folder: one driver, one run
STATE = {}
DATASET_ID = "pi-bench"
# LOCAL PATCH (nullius): the agent under test may run on one provider while the
# benchmark's simulated user and judge stay on another, so that scores keep
# coming from the provider earlier runs were scored on. The file, when it
# exists, holds {"base_url", "key"}; without it both use the run's own route.
EVAL_ROUTE = Path("/root/.config/nullius/eval-route.json")


def eval_route(route):
    """The simulator's and judge's route, moved to the evaluation provider when one is set."""
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


PERSONAS = ["researcher", "marketer", "pharmacist", "law_trainee", "Financier"]
DATASETS = {
    "pi-bench": PERSONAS,
    "pi-bench-researcher": ["researcher"],
    "pi-bench-marketer": ["marketer"],
    "pi-bench-pharmacist": ["pharmacist"],
    "pi-bench-law-trainee": ["law_trainee"],
    "pi-bench-financier": ["Financier"],
}
INSTRUCTION = (
    "You are the user's personal assistant in a long-horizon session. The user speaks in turns; "
    "your final message in each turn is sent to them, and earlier turns stay in the conversation. "
    "The AppWorld tools given to this session are your tools; your workspace is shared across the session. "
    "Work the request to completion, and end each turn with what the user should know."
)
TURN_WAIT = 580          # a turn request's own limit is 600 s; leave the driver a margin
STOP = object()


class Stop(BaseException):
    """The unit is being scored: end the upstream loop, do not try again."""


def repository():
    from src.data import UserDataRepository
    return UserDataRepository(data_root=str(DATA))


def persona_tasks(persona):
    profile, episode, tasks = repository().load_user(persona)
    order = list(episode.task_order)
    if len(order) != 20 or len(set(order)) != 20:
        raise ValueError(f"{persona}: the upstream episode must name 20 distinct tasks, found {len(order)}")
    missing = [task_id for task_id in order if task_id not in tasks]
    if missing:
        raise ValueError(f"{persona}: task files missing for {', '.join(missing)}")
    return order


def units(dataset, setup=None):
    global DATASET_ID
    if dataset not in DATASETS:
        raise ValueError(f"unknown Pi-Bench dataset {dataset!r}")
    DATASET_ID = dataset
    chosen = DATASETS[dataset]
    for persona in chosen:
        persona_tasks(persona)
    return {"units": list(chosen)}


def setup(unit, dir):
    persona = unit
    if persona not in PERSONAS:
        raise ValueError(f"unknown Pi-Bench persona {persona!r}")
    order = persona_tasks(persona)
    absolute = os.path.abspath(dir)
    token = hashlib.sha256(f"{persona}:{os.getpid()}:{time.time()}".encode()).hexdigest()[:12]
    out = os.path.join(os.path.dirname(absolute), f"pi-bench-{token}")
    os.makedirs(out, exist_ok=True)
    container = f"nullius-pi-{token}"
    STATE[persona] = {
        "persona": persona, "dir": absolute, "out": out, "trace": os.path.join(out, "trace_logs"),
        "dataset": DATASET_ID, "container": container, "tasks": order,
        "messages": {}, "session": {}, "turn_no": {}, "current": None,
        "in_q": queue.Queue(), "out_q": queue.Queue(),
        "user_agent": None, "runner": None, "thread": None, "done": False,
    }
    return {
        "prompt": INSTRUCTION,
        "agent_seconds": 21600,
        "mcp": [{"name": "appworld", "server": "appworld",
                 "args": [container, str(DATA), persona]}],
    }


class Bridge:
    """The upstream's channel, with Nullius on the other side: what the runner
    sends is ``turn``'s answer; what the agent says is put into ``in_q``."""

    def __init__(self, state):
        self.s = state

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def send(self, message):
        if message == "/new":                     # the runner's task reset, not a user message
            return
        task = self.s["current"]
        if task is None:
            raise RuntimeError("the upstream sent a message with no active task")
        self.s["messages"].setdefault(task, []).append({"role": "user", "content": message})
        self.s["out_q"].put(("message", message))

    async def wait_for_reply(self, timeout=None):
        reply = await asyncio.wait_for(asyncio.to_thread(self.s["in_q"].get), timeout)
        if reply is STOP:
            raise Stop()
        return reply

    async def reset(self):
        return None

    def set_runtime_identity(self, *, sender_id, chat_id):
        task = str(chat_id or "")
        if not task:
            return
        if task != self.s["current"]:
            self.s["current"] = task
            self.s["session"].setdefault(task, datetime.now().strftime("%Y%m%d_%H%M%S"))


def start_runner(state, simulator):
    from src.llm import LLMClient
    from src.runner.runner import BenchmarkRunner
    from src.user_agent.user_agent import UserAgent

    simulator = eval_route(simulator)         # the simulated user is evaluation, not the agent
    llm = LLMClient(model=simulator["model"], base_url=simulator["base_url"],
                    api_key=simulator["key"], temperature=0.0,
                    request_timeout=360.0, max_concurrency=8, max_retries=4)
    agent = UserAgent(
        user_root=str(DATA / state["persona"]), llm_client=llm, repository=repository(), task_ids=None,
        output_root=state["out"], model_id=MODEL_ID, workspace_dir=state["dir"],
        copy_task_assets_to_workspace=True, history_config_path=str(HISTORY),
    )
    runner = BenchmarkRunner(channel=Bridge(state), user_agent=agent,
                             max_turns=30, turn_timeout=2400.0)
    state["user_agent"], state["runner"] = agent, runner

    def run():
        try:
            state["result"] = asyncio.run(runner.run())
            state["done"] = True
            state["out_q"].put(("done", None))
        except Stop:
            return
        except BaseException as exc:              # the unit's error, said to Nullius
            if not state.get("stopping"):
                state["out_q"].put(("error", f"{type(exc).__name__}: {exc}"))

    state["thread"] = threading.Thread(target=run, daemon=True, name=f"pi-bench-{state['persona']}")
    state["thread"].start()


def last_agent(transcript):
    for item in reversed(transcript or []):
        if item.get("role") == "agent":
            return str(item.get("text") or "")
    raise ValueError("the turn carries no agent message")


def write_turn(state, task, force=False):
    messages = state["messages"].get(task) or []
    if not force and not any(message.get("role") == "assistant" for message in messages):
        return
    if not messages:
        return
    number = state["turn_no"].get(task, 0) + 1
    state["turn_no"][task] = number
    folder = Path(state["trace"]) / MODEL_ID / state["persona"] / task / state["session"][task]
    folder.mkdir(parents=True, exist_ok=True)
    output = next((message["content"] for message in reversed(messages)
                   if message.get("role") == "assistant"), "")
    payload = {
        "session_key": f"{state['persona']}:{task}",
        "session_started_at": state["session"][task],
        "channel": "nullius", "chat_id": task, "sender_id": state["persona"],
        "model": MODEL_ID, "input": messages[0].get("content", ""),
        "messages": messages, "llm_steps": [], "tool_steps": [],
        "output": output, "iterations": 1, "tools_used": [],
    }
    (folder / f"turn_{number:04d}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def ensure_task_records(state):
    """Every episode task needs a turn file and the user simulator's side logs.

    The upstream proactiveness evaluator walks the whole episode order and
    refuses a task with no turn history. A unit the agent left early (its
    timeout, an error) therefore gets its opening request recorded, with no
    answer, and scores nothing for itself rather than failing the evaluation.
    """
    _, _, tasks = repository().load_user(state["persona"])
    history = Path(state["out"]) / MODEL_ID / state["persona"]
    for task in state["tasks"]:
        state["session"].setdefault(task, datetime.now().strftime("%Y%m%d_%H%M%S"))
        messages = state["messages"].setdefault(task, [])
        if not messages:
            messages.append({"role": "user", "content": tasks[task].initial_input})
        if not glob.glob(str(Path(state["trace"]) / MODEL_ID / state["persona"] / task / "*" / "turn_*.json")):
            write_turn(state, task, force=True)
        folder = history / task / "history"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = state["session"][task]
        if not list(folder.glob("*-messages.jsonl")):
            message_path = folder / f"{stamp}-messages.jsonl"
            with message_path.open("w", encoding="utf-8") as f:
                for index, message in enumerate(messages, start=1):
                    f.write(json.dumps({
                        "task_id": task, "agent_id": MODEL_ID, "round": index,
                        "role": message.get("role"), "message": message.get("content", ""),
                        "chars": len(message.get("content", "")), "timestamp": stamp,
                        "timestamp_iso": stamp,
                    }, ensure_ascii=False) + "\n")
        if not list(folder.glob("*-log.jsonl")):
            (folder / f"{stamp}-log.jsonl").write_text("", encoding="utf-8")


def wait_out(state, timeout=TURN_WAIT):
    try:
        kind, value = state["out_q"].get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"the upstream user simulator did not answer in {timeout} s") from None
    if kind == "error":
        raise RuntimeError(value)
    if kind == "done":
        return {"done": True}
    return {"message": value}


def turn(unit, dir, transcript, simulator=None, judge=None, attempt=None):
    state = STATE.get(unit)
    if state is None:
        raise ValueError(f"turn before setup for {unit!r}")
    if state["runner"] is None:
        if not isinstance(simulator, dict):
            raise ValueError("Pi-Bench needs a simulated user: the run must give the simulator route")
        start_runner(state, simulator)
        return wait_out(state)
    reply = last_agent(transcript)
    task = state["current"]
    if task is None:
        raise RuntimeError("the upstream runner has no active task")
    state["messages"].setdefault(task, []).append({"role": "assistant", "content": reply})
    write_turn(state, task)
    state["in_q"].put(reply)
    return wait_out(state)


def task_parts(state):
    root = Path(state["out"]) / MODEL_ID / state["persona"]
    latest = {}
    for path in glob.glob(str(root / "*" / "eval" / "results" / "*_result.json")):
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task = payload.get("task") or {}
        task_id = task.get("task_id") or Path(path).parents[2].name
        latest[task_id] = payload
    parts = []
    for task_id in sorted(latest):
        task = latest[task_id]["task"]
        comp = (task.get("checklist") or {}).get("average_score")
        proc = (task.get("proactiveness") or {}).get("average_score")
        score = comp if comp is not None else proc
        if score is None:
            continue
        parts.append({"n": len(parts) + 1, "text": task_id,
                      "group": "checklist" if comp is not None else "proactiveness",
                      "score": round(float(score), 6)})
    return parts


def stop_runner(state):
    state["stopping"] = True
    state["in_q"].put(STOP)
    thread = state.get("thread")
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)


def cleanup(state):
    stop_runner(state)
    name = state.get("container")
    if not name:
        return
    try:
        import subprocess
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    except OSError:
        pass


def score(unit, dir, transcript, answer, judge=None, **_):
    state = STATE.get(unit)
    if state is None or state["runner"] is None:
        return {"reward": 0.0, "detail": {"reason": "no conversation", "dataset": DATASET_ID}}
    try:
        if not any(any(message.get("role") == "assistant" for message in messages)
                   for messages in state["messages"].values()):
            return {"reward": 0.0, "detail": {"reason": "no turn was completed", "dataset": DATASET_ID}}
        if not isinstance(judge, dict):
            raise ValueError("Pi-Bench's checklist evaluator needs the judge route")
        judge = eval_route(judge)             # the checklist judge is evaluation, not the agent
        ensure_task_records(state)
        from src.evaluation import TraceEvaluationRunner
        from src.llm import LLMClient

        llm = LLMClient(model=judge["model"], base_url=judge["base_url"], api_key=judge["key"],
                        temperature=0.0, request_timeout=360.0, max_concurrency=8, max_retries=4)
        evaluator = TraceEvaluationRunner(
            model_id=MODEL_ID, user_id=state["persona"], logs_dir=Path(state["trace"]),
            workspace_dir=Path(state["dir"]), config_path=HISTORY, output_dir=Path(state["out"]),
            scoring="both", llm_client=llm, data_root=DATA,
        )
        result = asyncio.run(evaluator.run())
        comp = result.overall_average_score
        proc = result.overall_proactiveness_average_score
        parts = task_parts(state)
        reward = comp if comp is not None else (proc if proc is not None else 0.0)
        return {"reward": float(reward),
                "detail": {"comp": comp, "proc": proc, "tasks": len(parts),
                           "persona": state["persona"], "judge": judge.get("model"),
                           "dataset": state["dataset"]},
                **({"parts": {"n": len(parts), "by": "task", "parts": parts}} if parts else {})}
    finally:
        cleanup(state)


HANDLERS = {"units": units, "setup": setup, "turn": turn, "score": score}
# LOCAL PATCH (nullius): the upstream logs to stdout, which is this driver's JSON
# channel. Send everything to stderr and keep the real stdout for replies only.
_REPLY = sys.stdout
sys.stdout = sys.stderr
for line in sys.stdin:
    try:
        request = json.loads(line)
        result = HANDLERS[request.pop("op")](**request)
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False), file=_REPLY, flush=True)
    _REPLY.flush()
