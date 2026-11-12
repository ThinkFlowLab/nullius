"""τ³-bench's driver for the Nullius standard (JSON lines on stdin and stdout).

An agent benchmark of conversations: a customer, played by a model, calls a
service whose system the agent works through tools. Everything the
benchmark's authors decide is their code, ``tau2-bench`` at the release tag
``standard.json`` pins, installed as this driver's environment
(``driver.install``: the upstream as a project, with its own lock file) and
imported - never copied:

units  - the domain's tasks in the dataset file's order (the upstream's
         ``load_file`` and ``Task`` model), those of the split the upstream
         runs by default (``TextRunConfig.task_split_name``, ``base``) when
         its registry has splits for the domain: the file may hold more
         tasks than the upstream's task set runs.
         A dataset with no file (banking_knowledge, its tasks one file each) is
         the upstream's own task loader (``registry.get_tasks_loader``)
setup  - the agent's instructions and the domain's policy as the upstream's
         agent is given them (``LLMAgent.system_prompt``: its
         ``SYSTEM_PROMPT`` and ``AGENT_INSTRUCTION`` over
         ``Environment.get_policy``), the environment built for the task as the
         upstream's runner builds it (``_build_env_kwargs``: a knowledge domain's
         retrieval config, the run's ``retrieval`` option); the domain's tools as the MCP server
         ``mcp_server.py`` (``mcp_servers`` in ``standard.json``), which
         executes nothing itself: it passes each call to this driver
turn   - the conversation is the upstream's: its batch runner,
         ``runner.batch.run_tasks``, run on the unit's one task in a thread of
         this driver, with a ``TextRunConfig`` at the upstream's defaults (max
         steps, max errors, no timeout, one trial, its seed 300, three
         retries, its user simulator). It draws the trial's seed from the
         base seed as it does for every run: the unit's first attempt is
         trial 0 of a one-trial run, its attempt N (``--repeat``) trial N-1
         at the seed run_tasks draws for that trial (``trial``), and each
         attempt is its ``run_single_task``: an ``Orchestrator`` built by its
         ``build_text_orchestrator`` at that seed, run by its
         ``run_simulation``; an attempt that raises is made again by its
         ``run_with_retry``, and after the last the runner records
         ``INFRASTRUCTURE_ERROR``. The agent in it is
         ``AcpAgent``, an upstream ``HalfDuplexAgent`` registered with the
         upstream's registry: each of the ACP agent's tool calls, arriving
         from the MCP server, is the agent's next message (one ``ToolCall``),
         which the Orchestrator executes in its own environment, counts and
         answers - the answer goes back to the ACP agent through the server;
         its final message in a turn is the agent's next message to the user.
         A ``turn`` hands that message to the Orchestrator and answers with
         what the upstream's user simulator says next, or ``done`` when the
         Orchestrator ended the conversation - by its rules: the user's stop,
         its step and error limits, an agent or user error
answer - the original: the upstream's own agent (``LLMAgent``, or the
         variant the ``upstream_agent`` option names) asked as the run's
         model through the unit's relay, in the same batch runner - no
         ACP agent, no MCP server; the user simulator and the judge as for
         an ACP agent's run
score  - what ``run_tasks`` returned: the Orchestrator's own
         ``SimulationRun``, evaluated by the upstream's ``evaluate_simulation``
         (``ALL``) inside ``run_simulation``; a run the batch runner recorded
         as ``INFRASTRUCTURE_ERROR`` is the unit's error, never a score, as
         the upstream's metrics leave it out. The NL-assertion judge, when a
         task asks it, is the upstream's evaluator at its own model name,
         reached through the relay's judge route (litellm's own ``api_base``
         and ``api_key``, which it reads before ``OPENAI_BASE_URL``: that one
         is a search option's route, pinning embeddings and reranking
         separately for the upstream's OpenAI clients)
oracle - the reference: the task's gold actions made through the upstream's
         ``Environment.get_response``, from the task's initial state set by
         its ``Environment.set_state`` as the Orchestrator sets it, then a message carrying every fact the
         task requires; scored by the upstream's evaluator as
         ``ALL_IGNORE_BASIS`` (every check but the NL assertions, which the
         reference, having no conversation, is not asked). It and the null
         control hold no conversation, so their ``SimulationRun`` is put
         together here, the only one that is

What is ours, glue only: ``AcpAgent`` (a call in, a ``ToolCall`` message
out; a ``ToolMessage`` in, its text back to the call; the ACP agent's final
text in, an ``AssistantMessage`` out), the socket the MCP server reaches it
by, the routes (the simulator's as ``llm_user``/``llm_args_user``, the
judge's as litellm's environment), and the lines of the agent's first prompt
that say how its turns reach the user and where its tools are, after the
upstream's instructions. A call that arrives after the Orchestrator ended the
conversation is not executed: it is answered as an error saying so, so the
ACP agent's turn can end.

An attempt the batch runner makes again starts a new conversation: a new
Orchestrator, environment and agent. The ACP agent is told so by the next
``turn``'s answer, ``restart`` with the user's first message, and Nullius
gives it a new session; a call or a final message of the ACP agent's that
belongs to the attempt before is not made, and answered as an error saying the
conversation was started again.
"""
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

import litellm

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")     # no download at import: the relay is the only way out
PROTOCOL, sys.stdout = sys.stdout, sys.stderr                      # stdout is the protocol's, for good: whatever the upstream prints,
                                                                    # from any thread, goes to the log
from loguru import logger
from tau2 import config
from tau2.agent.base_agent import HalfDuplexAgent
from tau2.agent.llm_agent import LLMAgent
from tau2.data_model.message import AssistantMessage, MultiToolMessage, ToolCall, ToolMessage, UserMessage
from tau2.data_model.simulation import SimulationRun, TerminationReason, TextRunConfig
from tau2.data_model.tasks import RewardType, Task
from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
from tau2.orchestrator.orchestrator import DEFAULT_FIRST_AGENT_MESSAGE
from tau2.registry import registry
from tau2.runner.batch import run_single_task, run_tasks
from tau2.runner.build import _build_env_kwargs
from tau2.runner.progress import run_with_retry
from tau2.utils import load_file

from nullius.standard.jsonl import serve

logger.remove()
logger.add(sys.stderr, level=config.DEFAULT_LOG_LEVEL)

AGENT = "nullius_acp"          # the name AcpAgent is registered by, in the upstream's registry
HOW = """

How this session works: the tools of the MCP server named {domain}, given to this session, are your tools. \
Your final message in each turn is sent to the user, and their reply comes as your next message.

You said to the user: {greeting}
The user says:"""
ENDED_CALL = "Error: the conversation has ended ({why}); this call was not made."
RESTARTED_CALL = "Error: the conversation was started again; this call was not made."
WAIT = 3600                    # the most a turn waits on the conversation thread: the run's own limits come first
STATE = {}
_TASKS = {}


class Aborted(BaseException):
    """The unit was left before its conversation ended. Not an ``Exception``:
    the upstream's run_with_retry retries those, and a unit left is not tried
    again."""


class AcpAgent(HalfDuplexAgent):
    """The ACP agent as the upstream's Orchestrator sees an agent: each of its
    next messages is a tool call it made over MCP, or its final text in a
    turn. Its stop rule is the base class's, as the upstream's LLMAgent's is."""

    def __init__(self, tools, domain_policy, conversation):
        super().__init__(tools=tools, domain_policy=domain_policy)
        self.c = conversation
        self.attempt = conversation.new_attempt()      # the batch runner's attempt this agent was built for

    def get_init_state(self, message_history=None):
        return None

    def generate_next_message(self, message, state):
        c = self.c
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
            if what == "call":                         # the ACP agent's, from an attempt before: not made
                value[2](RESTARTED_CALL, True)
        if what == "call":
            name, arguments, reply = value
            tc = ToolCall(id="call_" + uuid.uuid4().hex[:16], name=str(name),
                          arguments=arguments if isinstance(arguments, dict) else {}, requestor="assistant")
            c.pending[tc.id] = reply
            return AssistantMessage(role="assistant", content=None, tool_calls=[tc]), state
        return AssistantMessage(role="assistant", content=value), state          # "said": its final text in the turn


def _make_agent(tools=None, domain_policy=None, **kw):
    return AcpAgent(tools, domain_policy, _CURRENT[0])


_CURRENT = [None]              # the conversation running (one at a time: a driver holds one unit at a time)
registry.register_agent_factory(_make_agent, AGENT)


class Conversation:
    """One unit's conversation: the upstream's Orchestrator in a thread, a
    socket the MCP server passes calls through, and the queues between them."""

    def __init__(self):
        self.inbox = queue.Queue()            # to AcpAgent: ("call", (name, arguments, reply), attempt), ("said", text, attempt),
                                              # ("abort", None, None)
        self.out = queue.Queue()              # to a turn: ("say", text, attempt), ("done", None, None)
        self.pending = {}                     # tool call id -> its reply
        self.attempt = 0                      # the batch runner's attempt running (1, 2, ...)
        self.given = 0                        # the attempt the ACP agent was last given a message of
        self.lock = threading.Lock()
        self.over = None                      # why the conversation ended, once it has
        self.result = None                    # the SimulationRun run_tasks returned
        self.error = None
        self.config = None                    # the run's TextRunConfig, once started
        self.thread = None
        self.dir = tempfile.mkdtemp(prefix="nullius-tau-")
        os.chmod(self.dir, 0o700)
        self.at = os.path.join(self.dir, "s")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.at)
        self.sock.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    # -- the MCP server's side: one connection per call, a JSON line each way
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
            line = f.readline()
            req = json.loads(line or b"{}")
        except (OSError, ValueError):
            conn.close()
            return

        def reply(text, error):
            try:
                f.write((json.dumps({"text": text, "isError": error}) + "\n").encode())
                f.flush()
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    conn.close()

        with self.lock:
            if self.over is None and self.thread is not None:
                if self.given == self.attempt:
                    self.inbox.put(("call", (req.get("name"), req.get("arguments"), reply), self.given))
                    return
                why = None                         # an attempt the ACP agent has not been told of yet
            else:
                why = self.over or "not started"
        reply(RESTARTED_CALL if why is None else ENDED_CALL.format(why=why), True)

    def new_attempt(self) -> int:
        """The batch runner builds an agent: a new attempt. The calls the
        attempt before left waiting are answered, never made."""
        with self.lock:
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

    # -- the conversation: the upstream's batch runner, on the one task, in a thread
    def start(self, cfg, task, attempt=None):
        self.config = cfg
        self.thread = threading.Thread(target=self._run, args=(cfg, task, attempt), daemon=True)
        self.thread.start()

    def _run(self, cfg, task, attempt):
        _CURRENT[0] = self
        try:
            self.result = trial(cfg, task, attempt)
        except BaseException as e:                       # the runner raised (the unit was left): no SimulationRun
            self.error = e
        with self.lock:
            why = self.result.termination_reason if self.result is not None else None
            self.over = getattr(why, "value", None) or (str(why) if why else "stopped")
            left = list(self.pending.values())
            self.pending.clear()
            while True:
                try:
                    what, value, _ = self.inbox.get_nowait()
                except queue.Empty:
                    break
                if what == "call":
                    left.append(value[2])
        for reply in left:                                # never made: the Orchestrator was done
            reply(ENDED_CALL.format(why=self.over), True)
        self.out.put(("done", None, None))

    def said(self, text):
        """The ACP agent's final message in its turn - for the attempt it was
        last given a message of: one the batch runner has since left goes nowhere."""
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
            if self.error is not None and not isinstance(self.error, Aborted):
                raise RuntimeError(f"the upstream's run raised {type(self.error).__name__}: {self.error}")
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


def trial_seed(base, attempt):
    """The seed the upstream's batch runner gives trial ``attempt - 1``: run_tasks draws one per trial
    from its base seed (batch.py, "Seed management": ``random.seed(config.seed)``, then
    ``random.randint(0, 1000000)`` for each trial in order) - the same draws, from a generator of
    their own so nothing else's randomness moves."""
    r = random.Random(base)
    return [r.randint(0, 1000000) for _ in range(attempt)][-1]


def trial(cfg, t, attempt=None):
    """One unit's run: trial ``attempt - 1`` of the upstream's batch runner on the unit's task. The first
    attempt is run_tasks itself (a one-trial run: trial 0 at its seed), as every run was before attempts;
    a later one is what run_tasks does for that trial of a run of as many (its ``_run_tracked``): its
    run_single_task at the trial's seed, made again by its run_with_retry, which records
    INFRASTRUCTURE_ERROR after the last retry."""
    n = int(attempt or 1)
    if n <= 1:
        return run_tasks(cfg, [t], evaluation_type=EvaluationType.ALL).simulations[0]
    seed = trial_seed(cfg.seed, n)
    return run_with_retry(lambda: run_single_task(cfg, t, seed=seed, evaluation_type=EvaluationType.ALL),
                          task=t, trial=n - 1, seed=seed, max_retries=cfg.max_retries, retry_delay=cfg.retry_delay)


def domain():
    return os.environ.get("NULLIUS_DATASET_ID") or ""


def tasks():
    if not _TASKS:
        if not os.environ.get("NULLIUS_DATASET"):          # a dataset with no file: the upstream's own task loader at its pin
            for task in registry.get_tasks_loader(domain())():
                _TASKS[str(task.id)] = task
            return _TASKS
        splits = registry.get_task_splits_loader(domain())
        if splits and split() not in splits():           # as the domain's own get_tasks refuses it
            raise ValueError(f"Invalid task split name: {split()}. Valid splits are: {splits().keys()}")
        keep = set(splits()[split()]) if splits else None
        for t in load_file(os.environ["NULLIUS_DATASET"]):
            task = Task.model_validate(t)
            if keep is None or task.id in keep:
                _TASKS[str(task.id)] = task
    return _TASKS


def task(unit):
    t = tasks().get(unit)
    if t is None:
        raise ValueError(f"no task {unit!r} in {domain()}")
    return t


def split():
    """The task split run, the upstream's --task-split-name: the run's ``task_split`` option, else the
    upstream's default (``TextRunConfig.task_split_name``, ``base``)."""
    return os.environ.get("NULLIUS_OPTION_TASK_SPLIT") or TextRunConfig.model_fields["task_split_name"].default


def effort_args(option):
    """A model's arguments with a reasoning effort, as ``tau2 run --agent-llm-args`` / ``--user-llm-args``
    '{"reasoning_effort": "low"}' passes them (the JSON replaces the default, temperature 0); None at
    ``default``, the upstream's own arguments."""
    v = os.environ.get("NULLIUS_OPTION_" + option.upper()) or "default"
    return None if v == "default" else {"reasoning_effort": v}


def upstream_agent():
    """The upstream's agent an original runs, its --agent: the run's ``upstream_agent`` option."""
    return os.environ.get("NULLIUS_OPTION_UPSTREAM_AGENT") or config.DEFAULT_AGENT_IMPLEMENTATION


def retrieval():
    """The search a knowledge domain's agent is given, the upstream's --retrieval-config: the run's
    ``retrieval`` option (standard.json, its datasets banking_knowledge), set for that dataset only."""
    return os.environ.get("NULLIUS_OPTION_RETRIEVAL") or None


def run_config(**kw):
    """The upstream's TextRunConfig for this domain, its retrieval config and task split set when the run chose one."""
    return TextRunConfig(domain=domain(), task_split_name=split(),
                         **({"retrieval_config": retrieval()} if retrieval() else {}), **kw)


def user_config(simulator):
    """The user simulator's model and arguments: the simulator's route, at the upstream's default
    arguments (temperature 0) or the run's ``user_reasoning_effort``, as --user-llm-args gives one."""
    llm, at = route(simulator)
    return {"llm_user": llm, "llm_args_user": dict(effort_args("user_reasoning_effort") or config.DEFAULT_LLM_ARGS_USER, **at)}


def use_judge(t, judge, st):
    """The NL-assertion judge's route, as litellm's own api_base and api_key, when the task asks one."""
    if asks_judge(t):
        if not judge:
            raise ValueError("this task's NL assertions are graded by a judge model, and none was given")
        litellm.api_base, litellm.api_key = judge["base_url"], judge["key"]
        st["judge"] = judge["model"]


def env_kwargs(t):
    """The environment's arguments for one task, as the upstream's runner builds them (_build_env_kwargs)."""
    return _build_env_kwargs(run_config(agent=AGENT), t)


def environment(t):
    return registry.get_env_constructor(domain())(**env_kwargs(t))


def route(r):
    """litellm's arguments for a relay route: the model as an OpenAI-compatible one at the route's address."""
    return "openai/" + r["model"], {"api_base": r["base_url"], "api_key": r["key"]}


def asks_judge(t):
    return bool(RewardType.NL_ASSERTION in (t.evaluation_criteria.reward_basis or []) and t.evaluation_criteria.nl_assertions)


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
    """The tasks run; an original's upstream agent may keep only some (its registered task filter, as
    run_domain applies it: llm_agent_gt and llm_agent_solo take only the tasks they can run)."""
    keep = registry.get_agent_task_filter(upstream_agent()) if setup == "original" else None
    return {"units": [u for u, t in tasks().items() if keep is None or keep(t)]}


def setup(unit, dir, attempt=None):
    t = task(unit)
    _leave()                                              # one unit at a time: an earlier one left unfinished is ended
    env = environment(t)
    agent = LLMAgent(tools=env.get_tools(), domain_policy=env.get_policy(), llm=config.DEFAULT_LLM_AGENT)
    c = Conversation()
    STATE[unit] = {"conversation": c, "dir": dir, "oracle": None}
    return {"prompt": agent.system_prompt + HOW.format(domain=domain(), greeting=DEFAULT_FIRST_AGENT_MESSAGE.content),
            "mcp": [{"name": domain(), "server": "domain", "env": {"TAU_DOMAIN": domain(), "TAU_AT": c.at,
                                                                  **({"TAU_RETRIEVAL": retrieval()} if retrieval() else {})}}]}


def turn(unit, dir, transcript, simulator=None, judge=None, attempt=None):
    """The user's next message, or done. With nothing said yet, the upstream's
    Orchestrator starts: its first agent message, then the user's answer - in
    trial ``attempt - 1`` of the upstream's batch runner (``trial``)."""
    st, t = _state(unit), task(unit)
    c = st["conversation"]
    if not transcript:
        if not simulator:
            raise ValueError("the user is played by a model, and none was given")
        use_judge(t, judge, st)
        c.start(run_config(agent=AGENT, **user_config(simulator)), t, attempt)
        return c.next()
    if c.thread is None:
        raise ValueError("the conversation was not started: the first turn has an empty transcript")
    if c.over is not None:
        return {"done": True}
    c.said((transcript[-1].get("text") or "") if transcript[-1].get("role") == "agent" else "")
    return c.next()


def answer(unit, dir, setup, model, base_url, key, simulator=None, judge=None, attempt=None):
    """The original: the upstream's own agent (its --agent, the ``upstream_agent`` option: LLMAgent by
    default) asked as ``model`` through the unit's relay, at the upstream's agent arguments
    (temperature 0) or the run's ``agent_reasoning_effort``, as --agent-llm-args gives one; the
    conversation run by the upstream's batch runner exactly as for an ACP agent (``trial``), its user
    simulator and judge on their own routes. llm_agent_solo talks to no one: its user is the upstream's
    dummy_user, as its docs pair them."""
    if setup != "original":
        raise ValueError(f"τ³-bench has no {setup} setup")
    t = task(unit)
    _leave()
    st = STATE[unit] = {"conversation": None, "dir": dir, "oracle": None}
    solo = registry.get_agent_metadata(upstream_agent(), "solo_mode", default=False)
    if not solo and not simulator:
        raise ValueError("the user is played by a model, and none was given: the run passes the simulator's route to answer")
    use_judge(t, judge, st)
    llm, at = route({"model": model, "base_url": base_url, "key": key})
    cfg = run_config(agent=upstream_agent(), llm_agent=llm,
                     llm_args_agent=dict(effort_args("agent_reasoning_effort") or config.DEFAULT_LLM_ARGS_AGENT, **at),
                     **({"user": "dummy_user"} if solo else user_config(simulator)))
    st.update(original=trial(cfg, t, attempt), config=cfg)
    said = [m.content for m in st["original"].messages if m.role == "assistant" and m.content]
    return {"answer": said[-1] if said else ""}


def score(unit, dir, transcript, answer, judge=None, setup=None):
    st, t = _state(unit), task(unit)
    c = st["conversation"]
    if st.get("original") is not None:                    # the original: what its batch runner returned
        run, kind = st["original"], EvaluationType.ALL
        if run.termination_reason == TerminationReason.INFRASTRUCTURE_ERROR:
            info = run.info or {}
            _keep(dir, run)
            raise ValueError(f"the upstream's batch runner recorded INFRASTRUCTURE_ERROR after {info.get('failed_after_attempts')} "
                             f"attempts: {info.get('error_type')}: {info.get('error')}")
    elif st["oracle"] is not None:
        messages = st["oracle"] + [AssistantMessage(role="assistant", content=answer)]
        run = _assembled(t, messages, TerminationReason.AGENT_STOP)
        kind = EvaluationType.ALL_IGNORE_BASIS
        run.reward_info = evaluate_simulation(run, t, kind, solo_mode=False, domain=domain(), env_kwargs=env_kwargs(t))
    elif c.thread is None:                                # the null control: nothing was said, an empty message
        run, kind = _assembled(t, [], TerminationReason.AGENT_ERROR), EvaluationType.ALL
        run.reward_info = evaluate_simulation(run, t, kind, solo_mode=False, domain=domain(), env_kwargs=env_kwargs(t))
    else:
        if c.over is None:
            raise ValueError("the conversation has not ended: the run's turns ran out before the upstream's limits")
        if c.error is not None:
            raise ValueError(f"the upstream's run raised {type(c.error).__name__}: {c.error}")
        run, kind = c.result, EvaluationType.ALL
        if run.termination_reason == TerminationReason.INFRASTRUCTURE_ERROR:
            info = run.info or {}
            _keep(dir, run)
            raise ValueError(f"the upstream's batch runner recorded INFRASTRUCTURE_ERROR after {info.get('failed_after_attempts')} "
                             f"attempts: {info.get('error_type')}: {info.get('error')}")
    calls = [m for m in run.messages if isinstance(m, ToolMessage)]
    detail = {"domain": domain(), "termination": getattr(run.termination_reason, "value", run.termination_reason),
              "evaluation": kind.value, "tool_calls": len(calls), "tool_errors": sum(1 for m in calls if m.error),
              "user_turns": sum(1 for m in run.messages if m.role == "user"),
              "reward_info": run.reward_info.model_dump(mode="json", exclude_none=True)}
    cfg = st.get("config") or (c.config if c is not None else None)
    if cfg is not None:
        detail.update(max_steps=cfg.effective_max_steps, max_errors=cfg.max_errors, seed=run.seed, trial=run.trial,
                      split=cfg.task_split_name, **({"attempts": c.attempt} if c is not None else {"agent": cfg.agent}))
    if st.get("judge") and kind == EvaluationType.ALL:
        detail["judge"] = st["judge"]
    _keep(dir, run)
    if c is not None:
        c.close()
    return {"reward": run.reward_info.reward, "passed": run.reward_info.reward == 1.0, "detail": detail}


def _assembled(t, messages, ended):
    """A control's SimulationRun: it held no conversation for the Orchestrator to record."""
    now = time.time()
    return SimulationRun(id=str(uuid.uuid4()), task_id=t.id, start_time=str(now), end_time=str(now), duration=0.0,
                         termination_reason=ended, messages=messages)


def _keep(dir, run):
    """The SimulationRun as the upstream writes it, beside the trial's verdict."""
    out = os.path.join(os.path.dirname(os.path.abspath(dir)), "verifier")
    with contextlib.suppress(OSError):
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "simulation.json"), "w") as f:
            f.write(run.model_dump_json(indent=2))


def oracle(unit, dir):
    """The gold actions, made through the upstream's environment, recorded as
    the Orchestrator records a call and its answer."""
    st, t = _state(unit), task(unit)
    env = environment(t)
    init = t.initial_state                                # the task's starting state, set as the Orchestrator and evaluator set it
    if init is not None:
        env.set_state(initialization_data=init.initialization_data, initialization_actions=init.initialization_actions,
                      message_history=[])
    messages = []
    for i, a in enumerate(t.evaluation_criteria.actions or []):
        tc = ToolCall(id=f"oracle_{i}", name=a.name, arguments=a.arguments, requestor=a.requestor)
        messages += [AssistantMessage(role="assistant", content=None, tool_calls=[tc]), env.get_response(tc)]
    st["oracle"] = messages
    facts = t.evaluation_criteria.communicate_info or []
    return {"answer": " ".join(facts) if facts else "Your request is handled."}


if __name__ == "__main__":
    serve({"units": units, "setup": setup, "turn": turn, "answer": answer, "score": score, "oracle": oracle}, out=PROTOCOL)
