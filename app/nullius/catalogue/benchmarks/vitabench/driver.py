"""VitaBench's driver for the Nullius standard (JSON lines on stdin and stdout).

Everything the benchmark's authors decide is their code, ``vitabench`` at the commit
``standard.json`` pins, installed as this driver's environment and imported:

units  - the dataset file's tasks, through the upstream's ``Task`` model
setup  - the upstream agent's own prompt (``LLMAgent.system_prompt``) and its
         environment's tools, served by ``mcp_server.py``, which passes each call here
turn   - the upstream's ``run_task`` in a thread: its Orchestrator, user simulator,
         limits, retries and evaluator. The agent in it is ``AcpAgent``: the tool calls made
         over MCP at once are the agent's next message, which the Orchestrator executes; the final
         text of a turn is its next message to the user. ``turn`` answers ``done`` once
         the agent or the user says the upstream's stop token, or the Orchestrator ends it
answer - the original: the upstream's own agent as the run's model, in the same run_task
score  - the ``SimulationRun`` run_task returned, scored by the upstream's evaluator

The run's three models are entries of the upstream's own model configuration
(``vita.config.models``): its ``default`` entry with the route's address and model.

Glue that shows in a result: the lines that tell the ACP agent where its tools are; an
empty final message, which the upstream would ask its model for again twice, counts as
three empty ones (``invalid_agent_message``); and a conversation started again by run_task
is told by ``restart``, except after it ended: an evaluator that raises is then the
unit's error, the ACP agent being gone.
"""
import atexit
import contextlib
import json
import os
import queue
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid

PROTOCOL, sys.stdout = sys.stdout, sys.stderr       # stdout is the protocol's: whatever the upstream prints goes to the log
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")

from vita import config
from vita.agent.llm_agent import LLMAgent
from vita.data_model.message import AssistantMessage, MultiToolMessage, ToolCall, ToolMessage, UserMessage
from vita.data_model.simulation import SimulationRun, TerminationReason
from vita.data_model.tasks import Task
from vita.environment.environment import get_cross_environment
from vita.evaluator.evaluator import evaluate_simulation
from vita.orchestrator.orchestrator import get_default_first_agent_message
from vita.registry import registry
from vita.run import _clear_global_state, run_task
from vita.user.user_simulator import DummyUser, UserSimulator
from vita.utils.utils import get_now

from nullius.standard.protocols.jsonl import serve

DOMAINS = {"cross": "delivery,instore,ota"}          # the upstream CLI's own default domain
HOW = {"english": """

How this session works: the tools of the MCP server named vita, given to this session, are your tools. \
Your final message in each turn is sent to the user, and their reply comes as your next message.

You said to the user: {greeting}
The user says:""",
       "chinese": """

本次会话的方式：本会话提供的名为 vita 的 MCP 服务器中的工具就是你的工具。你每一轮的最后一条消息会发送给用户，用户的回复会作为你的下一条消息。

你对用户说：{greeting}
用户说："""}
ENDED_CALL = "Error: the conversation has ended ({why}); this call was not made."
RESTARTED_CALL = "Error: the conversation was started again; this call was not made."
GATHER = 0.1                                          # how close together calls are parallel ones
WAIT = 590                     # under the 600 s a turn's answer is given
STATE, _TASKS = {}, {}
_CURRENT = [None]              # the conversation running: a driver holds one unit at a time


class Aborted(BaseException):
    """The run cannot go on. Not an ``Exception``: run_task retries those."""


class AcpAgent(LLMAgent):
    """The ACP agent as the upstream's Orchestrator sees an agent."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.c = _CURRENT[0]
        self.attempt = self.c.new_attempt()
        self.empty = None                              # the message the ACP agent answered with nothing

    def get_init_state(self, message_history=None):
        return None

    def generate_next_message(self, message, state):
        c = self.c
        if self.empty is message:                      # the Orchestrator asks again: the turn is over, there is no other answer
            return AssistantMessage(role="assistant", content="", cost=0.0), state
        if isinstance(message, (ToolMessage, MultiToolMessage)):
            for m in (message.tool_messages if isinstance(message, MultiToolMessage) else [message]):
                c.answer(m.id, m.content or "", bool(m.error))
        elif isinstance(message, UserMessage):
            c.out.put(("say", message.content, self.attempt))
        while True:
            what, value, attempt = c.inbox.get()
            if what == "abort":
                raise Aborted("the unit was left before the conversation ended")
            if attempt == self.attempt:
                break
            if what == "call":                         # from an attempt before: not made
                value[2](RESTARTED_CALL, True)
        if what == "call":
            # Calls the agent made at once (within GATHER seconds) are one message, one step, as the upstream's
            # agent's parallel calls are.
            calls, until = [value], time.monotonic() + GATHER
            while True:
                with c.inbox.mutex:
                    while c.inbox.queue and c.inbox.queue[0][0] == "call" and c.inbox.queue[0][2] == self.attempt:
                        calls.append(c.inbox.queue.popleft()[1])
                    other = bool(c.inbox.queue)
                if other or time.monotonic() >= until:
                    break
                time.sleep(0.005)
            tcs = []
            for name, arguments, reply in calls:
                tc = ToolCall(id="call_" + uuid.uuid4().hex[:16], name=str(name),
                              arguments=arguments if isinstance(arguments, dict) else {}, requestor="assistant")
                c.pending[tc.id] = reply
                tcs.append(tc)
            return AssistantMessage(role="assistant", content=None, tool_calls=tcs, cost=0.0), state
        said = AssistantMessage(role="assistant", content=value, cost=0.0)
        if not said.has_text_content():
            self.empty = message
        elif self.is_stop(said):
            c.closing(TerminationReason.AGENT_STOP)
        return said, state


class _Heard:
    """The upstream's user, its stop heard: the judge may take long, and the ACP agent need not wait for it."""

    def generate_next_message(self, message, state):
        said, state = super().generate_next_message(message, state)
        if said.has_text_content() and self.is_stop(said):
            _CURRENT[0].closing(TerminationReason.USER_STOP)
        return said, state


class HeardUser(_Heard, UserSimulator):
    pass


class HeardDummyUser(_Heard, DummyUser):
    pass


registry.register_agent(AcpAgent, "nullius_acp")
registry.register_user(HeardUser, "heard_user_simulator")
registry.register_user(HeardDummyUser, "heard_dummy_user")


class Conversation:
    """One unit's conversation: the upstream's run_task in a thread, the socket
    the MCP server passes calls through, and the queues between them."""

    def __init__(self, tools):
        self.tools = tools
        self.inbox = queue.Queue()            # to AcpAgent: ("call", (name, arguments, reply), attempt), ("said", text, attempt), ("abort", ..)
        self.out = queue.Queue()              # to a turn: ("say", text, attempt), ("done", None, None)
        self.pending = {}                     # tool call id -> its reply
        self.attempt = 0                      # run_task's attempt running (1, 2, ...)
        self.given = 0                        # the attempt the ACP agent was last given a message of
        self.lock = threading.Lock()
        self.over = self.result = self.error = self.thread = None
        self.dir = tempfile.mkdtemp(prefix="nullius-vita-")
        os.chmod(self.dir, 0o700)
        self.at = os.path.join(self.dir, "s")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.at)
        self.sock.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._call, args=(conn,), daemon=True).start()

    def _call(self, conn):
        try:
            f = conn.makefile("rwb")
            req = json.loads(f.readline() or b"{}")
        except (OSError, ValueError):
            conn.close()
            return

        def send(reply):
            with contextlib.suppress(OSError):
                f.write((json.dumps(reply) + "\n").encode())
                f.flush()
            with contextlib.suppress(OSError):
                conn.close()

        def reply(text, error):
            send({"text": text, "isError": error})

        if req.get("list"):
            send({"tools": self.tools})
            return
        with self.lock:
            if self.over is None and self.thread is not None and self.given == self.attempt:
                self.inbox.put(("call", (req.get("name"), req.get("arguments"), reply), self.given))
                return
            why = self.over or (None if self.thread is not None else "not started")
        reply(RESTARTED_CALL if why is None else ENDED_CALL.format(why=why), True)

    def new_attempt(self):
        with self.lock:
            if self.over is not None:
                raise Aborted("the upstream's evaluator raised (the driver's log says why)")
            self.attempt += 1
            left = list(self.pending.values())
            self.pending.clear()
        for reply in left:
            reply(RESTARTED_CALL, True)
        return self.attempt

    def answer(self, call_id, text, error):
        reply = self.pending.pop(call_id, None)
        if reply is not None:
            reply(text, error)

    def closing(self, why):
        """The conversation is over: calls still waiting are answered, never made."""
        with self.lock:
            self.over = self.over or getattr(why, "value", why)
            left = list(self.pending.values())
            self.pending.clear()
            with contextlib.suppress(queue.Empty):
                while True:
                    what, value, _ = self.inbox.get_nowait()
                    if what == "call":
                        left.append(value[2])
        for reply in left:
            reply(ENDED_CALL.format(why=self.over), True)
        self.out.put(("done", None, None))

    def start(self, **kw):
        self.thread = threading.Thread(target=self._run, kwargs=kw, daemon=True)
        self.thread.start()

    def _run(self, **kw):
        try:
            self.result = run_task(**kw)
        except BaseException as e:
            self.error = e
        self.closing(self.result.termination_reason if self.result is not None else "stopped")

    def said(self, text):
        with self.lock:
            if self.given == self.attempt:
                self.inbox.put(("said", text, self.given))

    def next(self):
        """What the user says next - in a new attempt, with ``restart`` - or done."""
        try:
            what, value, attempt = self.out.get(timeout=WAIT)
        except queue.Empty:
            raise TimeoutError(f"the conversation did not answer in {WAIT} s")
        if what == "done":
            return {"done": True}
        with self.lock:
            again, self.given = self.given not in (0, attempt), attempt
        return {"message": value, "restart": True} if again else {"message": value}

    def close(self):
        if self.thread is not None and self.thread.is_alive():
            self.inbox.put(("abort", None, None))
            self.thread.join(timeout=10)
        with contextlib.suppress(OSError):
            self.sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def option(name, default):
    return os.environ.get("NULLIUS_OPTION_" + name.upper()) or default


def dataset():
    """(the upstream's --domain, its --language)."""
    ds = os.environ.get("NULLIUS_DATASET_ID") or ""
    name = ds.removesuffix("-en")
    return DOMAINS.get(name, name), "english" if ds.endswith("-en") else "chinese"


def tasks():
    if not _TASKS:
        with open(os.environ["NULLIUS_DATASET"], encoding="utf-8") as f:
            _TASKS.update((str(t["id"]), Task.model_validate(t)) for t in json.load(f))
    return _TASKS


def task(unit):
    if unit not in tasks():
        raise ValueError(f"no task {unit!r} in {os.environ.get('NULLIUS_DATASET_ID')}")
    return tasks()[unit]


def environment(t):
    """The task's environment, built as the upstream's runner builds it (_run_task_internal)."""
    domain, language = dataset()
    _clear_global_state()
    if "," in domain:
        return get_cross_environment(domain, t.environment, language)
    return registry.get_env_constructor(domain)(t.environment, language)


def use(role, r, **settings):
    """A route as an entry of the upstream's model configuration, over its ``default``; its name there."""
    config.models[role] = config._deep_merge_dict(config.models["default"], {
        "name": role, "model": r["model"], "base_url": r["base_url"].rstrip("/") + "/chat/completions",
        "headers": {"Authorization": "Bearer " + r["key"]}, **settings})
    return role


def run_args(t, agent, simulator, judge, attempt, user_prefix=""):
    """run_task's arguments. The seed is the one the upstream's run_tasks draws for the trial."""
    if not simulator:
        raise ValueError("the user is played by a model, and none was given")
    if not judge:
        raise ValueError("the rubrics are graded by a judge model, and none was given")
    domain, language = dataset()
    r = random.Random(config.DEFAULT_SEED)
    seed = [r.randint(0, 1000000) for _ in range(int(attempt or 1))][-1]
    return dict(domain=domain, task=t, agent=agent, user=user_prefix + option("user", config.DEFAULT_USER_IMPLEMENTATION),
                llm_user=use("nullius-user", simulator), llm_args_user={}, llm_evaluator=use("nullius-judge", judge), llm_args_evaluator={},
                max_steps=int(option("max_steps", config.DEFAULT_MAX_STEPS)), max_errors=int(option("max_errors", config.DEFAULT_MAX_ERRORS)),
                evaluation_type=option("evaluation", config.DEFAULT_EVALUATION_TYPE), seed=seed, language=language)


def _state(unit):
    if unit not in STATE:
        raise ValueError(f"{unit} was not set up")
    return STATE[unit]


def _leave():
    for st in STATE.values():
        if st.get("conversation") is not None:
            st["conversation"].close()
    STATE.clear()
    _CURRENT[0] = None


def units(dataset, setup=None):
    return {"units": list(tasks())}


def setup(unit, dir, attempt=None):
    t = task(unit)
    _leave()
    env, language = environment(t), dataset()[1]
    agent = LLMAgent(tools=env.get_tools(), domain_policy=env.get_policy(), time=env.tools.db.time, language=language)
    c = Conversation([{"name": f["name"], "description": f["description"], "inputSchema": f["parameters"]}
                      for f in (x.openai_schema["function"] for x in env.get_tools())])
    STATE[unit] = {"conversation": c, "judge": None}
    _CURRENT[0] = c
    greeting = get_default_first_agent_message(language).content
    return {"prompt": agent.system_prompt + HOW[language].format(greeting=greeting),
            "mcp": [{"name": "vita", "server": "vita", "env": {"VITA_AT": c.at}}]}


def turn(unit, dir, transcript, simulator=None, judge=None, attempt=None):
    st, t = _state(unit), task(unit)
    c = st["conversation"]
    if not transcript:
        args = run_args(t, "nullius_acp", simulator, judge, attempt, "heard_")
        st.update(judge=judge["model"], args=args)
        c.start(llm_agent="nullius-acp", llm_args_agent={}, **args)
        return c.next()
    if c.thread is None:
        raise ValueError("the conversation was not started: the first turn has an empty transcript")
    if c.over is not None:
        return {"done": True}
    c.said((transcript[-1].get("text") or "") if transcript[-1].get("role") == "agent" else "")
    return c.next()


def answer(unit, dir, setup, model, base_url, key, simulator=None, judge=None, attempt=None):
    """The original: the upstream's own agent as ``model``, its --agent, --enable-think and model settings the run's options."""
    if setup != "original":
        raise ValueError(f"VitaBench has no {setup} setup")
    t = task(unit)
    _leave()
    args = run_args(t, option("upstream_agent", config.DEFAULT_AGENT_IMPLEMENTATION), simulator, judge, attempt)
    think = option("enable_think", "off") == "on"
    settings = {"temperature": float(option("agent_temperature", config.models["default"]["temperature"]))}
    if option("agent_max_tokens", "default") != "default":
        settings["max_tokens"] = int(option("agent_max_tokens", ""))
    if think and option("agent_reasoning_effort", "default") != "default":
        settings["reasoning_effort"] = option("agent_reasoning_effort", "")
    agent = use("nullius-agent", {"model": model, "base_url": base_url, "key": key}, **settings)
    run = run_task(llm_agent=agent, llm_args_agent={}, enable_think=think, **args)
    STATE[unit] = {"conversation": None, "original": run, "judge": judge["model"], "args": args}
    said = [m.content for m in run.messages if m.role == "assistant" and m.content]
    return {"answer": said[-1] if said else ""}


def score(unit, dir, transcript, answer, judge=None, setup=None):
    st, t = _state(unit), task(unit)
    c = st["conversation"]
    try:
        return _scored(st, t, c, dir)
    finally:
        if c is not None:
            c.close()


def _scored(st, t, c, dir):
    if c is None:
        run = st["original"]
    elif c.thread is None:                                # the null control: no message, as the upstream scores one
        now = get_now()
        run = SimulationRun(id=str(uuid.uuid4()), task_id=t.id, start_time=now, end_time=now, duration=0.0,
                            termination_reason=TerminationReason.INVALID_AGENT_MESSAGE, messages=[])
        domain, language = dataset()
        run.reward_info = evaluate_simulation(run, t, config.DEFAULT_EVALUATION_TYPE, domain, language=language)
    else:
        if c.over is None:
            raise ValueError("the conversation has not ended: the run's turns ran out before the upstream's limits")
        c.thread.join()                                   # the upstream's evaluator
        if c.result is None:
            raise ValueError(f"the upstream's run raised {type(c.error).__name__}: {c.error}")
        run = c.result
    info = run.reward_info
    calls = [m for m in run.messages if isinstance(m, ToolMessage)]
    detail = {"domain": dataset()[0], "language": dataset()[1], "termination": getattr(run.termination_reason, "value", run.termination_reason),
              "tool_calls": len(calls), "tool_errors": sum(1 for m in calls if m.error),
              "user_turns": sum(1 for m in run.messages if m.role == "user"),
              "reward_info": info.model_dump(mode="json", exclude_none=True, exclude={"window_evaluations"})}
    if info.nl_rubrics:
        detail["rubrics"] = f"{sum(1 for r in info.nl_rubrics if r.met)}/{len(info.nl_rubrics)}"
        detail["rubrics_missed"] = {r.nl_rubric or "": r.justification for r in info.nl_rubrics if not r.met}
    if st.get("args"):
        a = st["args"]
        detail.update(judge=st["judge"], seed=a["seed"], max_steps=a["max_steps"], max_errors=a["max_errors"], evaluation=a["evaluation_type"], user=a["user"].removeprefix("heard_"),
                      **({"attempts": c.attempt} if c is not None else {"agent": option("upstream_agent", config.DEFAULT_AGENT_IMPLEMENTATION)}))
    out = os.path.join(os.path.dirname(os.path.abspath(dir)), "verifier")
    with contextlib.suppress(OSError):
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "simulation.json"), "w", encoding="utf-8") as f:
            f.write(run.model_dump_json(indent=2))
    return {"reward": info.reward, "passed": info.reward == 1.0, "detail": detail}


if __name__ == "__main__":
    atexit.register(_leave)                               # the last unit's socket folder, scored or not
    serve({"units": units, "setup": setup, "turn": turn, "answer": answer, "score": score}, out=PROTOCOL)
