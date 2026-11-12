"""A run: a benchmark, an agent or a capability, a model - units, then a job.

The plan is units × stages. The driver names the units (``units``); each is
set up (``setup``: its workspace filled, its prompt and environment
returned), produced - the agent driven over ACP in that workspace, or the
capability driven through its port - and scored (``score``). Units run
``concurrency`` at a time, each with a driver process per worker, an agent or
capability process of its own, a home of its own, and a relay of its own.

A relay per unit, not one for the run: a shared relay is one more thing whose
failure stops every unit, and a network hop for a unit sandboxed elsewhere.
One per unit costs a few megabytes and loses only a shared rate limit - each
unit backs off on its own, as it always did. Its record sits in the trial's
``.work/relay``.

Two backends (``backend``). ``local``: processes on this machine, which are
not a sandbox - what an agent runs, it runs as the user (``docs/standard.md``,
"Safety"). ``docker``: the agent or the capability in a container of its own on
a network whose only way out is the unit's relay, itself a container beside
it (``container``). The driver stays on this machine either way: it owns the
answers. So do a benchmark's own MCP servers (``mcp_servers``), in the
driver's environment: on the local backend the agent starts each from it, on
docker the run does, behind a door on the unit's network (``mcp_machine``).

A unit whose environment is an **own image** (``"image": "own"``) runs in
the task's own container, whatever the backend (a control's too: nothing of
an agent runs there; an agent's needs the docker backend): its setup names
the image (``image``: a folder of the upstream to build, or a reference by
digest) and, optionally, the working directory (``workdir``; the image's own
by default) and a check (``verify``) - files of the upstream copied in, a
command run, a folder of results copied out - which runs in that container
after the agent ends and before ``score``, which is sent what it found
(``verified``: {exit, timed_out, output, dir, collected}) and reads the rest from ``dir``. The
oracle's reference solution is such a step too (its reply's ``solve``). So
the driver never needs docker, and the task's tests reach the container only
once the agent is done and nothing it started still runs there
(``container.Box``).

What a process is given of this machine's environment is a short list
(``KEEP``) - never the keys Settings holds. The relay holds the real key;
everything else gets its address and ``relay.PLACEHOLDER_KEY``.

An agent run on the local backend starts only on the caller's explicit yes
(``unsandboxed``); on either backend its permission requests are granted
(``acp.Client(grant=True)``): no one is there to answer them during a run.
The job's config records both, and the backend.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import traceback
import tempfile
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from nullius import paths, prices as price_list, progress, providers
from nullius.redact import redact
from nullius.harbor import feed as live_feed
from nullius.standard import acp, container, definitions, hostreq, install, job, jsonl, ledger as ledgers, match, mcp_machine, relay, shard as shards

# the totals a unit's end carries onto the live board, from its trial's agent_result
FEED_TOTALS = ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")

# The variables of this machine's environment a run's processes keep: where
# programs are, the locale, the temporary folder. Not a key among them.
KEEP = ("PATH", "LANG", "LC_ALL", "TMPDIR", "TERM", "NVM_DIR", "NODE_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR")
APP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))      # the folder holding the nullius package


def driver_path() -> str:
    """A driver's ``PYTHONPATH``: a folder whose one entry links to the ``nullius`` package, for the
    kits' drivers that import it. Not ``APP``: installed from a wheel, that is the tool's whole
    site-packages, and every package there would come before the driver's own environment's."""
    pkg = os.path.join(APP, "nullius")
    root = os.path.join(paths.cache_dir(), "driver-path", hashlib.sha256(pkg.encode()).hexdigest()[:16])
    link = os.path.join(root, "nullius")
    if os.path.realpath(link) != os.path.realpath(pkg):
        os.makedirs(root, exist_ok=True)
        # written whole beside the folder, then renamed in: drivers start in parallel, and the folder
        # on their PYTHONPATH never holds anything but the link
        part = f"{root}.{os.getpid()}.{threading.get_ident()}.part"
        os.symlink(pkg, part)
        os.replace(part, link)
    return root

PROVIDERS = {
    "openai": {"base": "https://api.openai.com/v1", "key": "OPENAI_API_KEY"},
    "openrouter": {"base": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY"},
    "anthropic": {"base": "https://api.anthropic.com/v1", "key": "ANTHROPIC_API_KEY", "key_header": "x-api-key"},
    "custom": {"base_var": "MODEL_API_URL", "key": "MODEL_API_KEY"},
}
TURN_SECONDS = 900
DRIVER_SECONDS = 600                  # a driver's setup, oracle and score requests, each
IMAGE_SECONDS = 3600                  # an own image's pull or build (container.own_image)
MAX_AGENT_SECONDS = 6 * 3600          # the longest a unit's own agent limit (setup's agent_seconds) may ask
MAX_TURNS = 20
BACKENDS = ("local", "docker")


class Refused(Exception):
    """The run cannot start: the pair does not fit, or a setting is missing."""


def provider_endpoint(provider: str, env=os.environ) -> tuple:
    p = PROVIDERS.get(provider)
    if not p:
        raise Refused(f"provider {provider!r} is not one of {list(PROVIDERS)}")
    base = p.get("base") or env.get(p["base_var"]) or ""
    key = env.get(p["key"]) or ("local" if provider == "custom" else "")
    if not base:
        raise Refused(f"{p['base_var']} is not set in Settings")
    if not key:
        raise Refused(f"{p['key']} is not set in Settings")
    if not key.isprintable() or any(ord(c) > 255 for c in key) or key != key.strip():
        # http.client refuses such a value, and quotes it whole in its error
        raise Refused(f"{p['key']} holds a character a request header cannot carry (a line break, a space or a "
                      "byte-order mark around it, or one outside Latin-1): set it again in Settings")
    return base, key


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_")[:60] or "unit"


def option_env(values: dict) -> dict:
    """A benchmark's option values as its driver reads them: ``NULLIUS_OPTION_<NAME>``."""
    return {"NULLIUS_OPTION_" + n.upper(): str(v) for n, v in (values or {}).items()}


# The endpoint refusing the run itself, not one unit: a key it rejects, a model it does not know.
REFUSALS = ("AgentAuthenticationError", "ModelNotFoundError")
REFUSED_IN_A_ROW = 3
# a unit still running when the run is stopped: Harbor's name for a trial a stopped job cut short
CANCELLED = "CancelledError"


def run_group(cmd: List[str], timeout: float, **kw) -> subprocess.CompletedProcess:
    """``subprocess.run`` for an agent's command, in a session of its own, and
    the whole group ended afterwards - on a timeout, which kills only the
    direct child, and at a normal end, where whatever it left in the
    background would keep running, still writing into the workspace while
    it is scored. Raises ``subprocess.TimeoutExpired`` as ``run`` does.
    The agent ending is what is waited for, not its output's end: a child
    left in the background holding stdout would otherwise hold the unit
    until its timeout."""
    stopped = kw.pop("stopped", None)
    streams = {}
    try:
        for name in ("stdout", "stderr"):
            if kw.get(name) == subprocess.PIPE:
                streams[name] = tempfile.TemporaryFile()
                kw[name] = streams[name]
        p = subprocess.Popen(cmd, start_new_session=True, **kw)
        deadline = time.monotonic() + timeout
        try:
            while p.poll() is None:
                if stopped is not None and stopped.is_set():
                    raise CancelledError("the run was stopped")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    p.wait(timeout=min(remaining, 0.1))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            _kill_group(p)
            p.wait()
        output = {}
        for name, stream in streams.items():
            stream.seek(0)
            data = stream.read()
            if kw.get("text") or kw.get("universal_newlines") or kw.get("encoding"):
                data = data.decode(kw.get("encoding") or "utf-8", errors=kw.get("errors") or "strict")
            output[name] = data
        return subprocess.CompletedProcess(cmd, p.returncode, output.get("stdout"), output.get("stderr"))
    finally:
        for stream in streams.values():
            stream.close()


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def base_env(home: str, extra: Optional[dict] = None) -> dict:
    env = {k: os.environ[k] for k in KEEP if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env["HOME"] = home
    env.update(extra or {})
    return env


def _render(template: str, slots: Dict[str, str]) -> str:
    for k, v in slots.items():
        template = template.replace("{" + k + "}", v)
    return template


def agent_env(agent: dict, inst: str, home: str, base_url: str, model: str, options: Optional[dict] = None) -> dict:
    """The environment of an agent - or of a capability that calls a model - and
    its config files written into its home, from its definition's ``model``
    templates; then its own ``env`` (``install.process_env``). ``options``:
    the run's value of each option (``definitions.options_of``), the slots
    ``{opt_<name>}``; an option left out is filled with its default."""
    options = definitions.options_of(agent, options)[0]
    slots = {"base_url": base_url, "key": relay.PLACEHOLDER_KEY, "model": model, "home": home,
             **{"opt_" + k: v for k, v in options.items()}}
    m = agent.get("model") or {}
    for dest, src in (m.get("files") or {}).items():
        path = _render(dest, slots)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(os.path.join(agent["_here"], src)) as f:
            text = _render(f.read(), slots)
        with open(path, "w") as f:
            f.write(text)
    env = base_env(home, {k: _render(v, slots) for k, v in (m.get("env") or {}).items()})
    env.update(install.process_env(agent, inst, home, options))
    env["PATH"] = os.pathsep.join(install.bin_dirs(inst) + [env["PATH"]])
    return env


def _own_dir(path: str) -> None:
    """``path`` as a folder this user alone owns: made 0700 when absent; when
    there already - a shared place like /dev/shm, where another user could
    have made it first, or put a link there - kept only if it is a real
    folder, this user's, open to no one else. Refused otherwise."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Refused(f"{path} is not a folder of this user's alone: no licence is written there")


class Licences:
    """What a run holds of its benchmark's licences, by name: ``get(name)``
    is {kind, value} for a key or a server, {kind, path} for a file, or None.

    On this machine they are read from the benchmark's store
    (``nullius.secrets``) when asked - when a unit starts - and a file is
    used where it is kept. A run on a host is handed them instead
    (``handed``): its launcher's stdin, a file written to a folder in memory
    (``/dev/shm``) that the run removes when it ends. Only the names the
    benchmark's environments declare are ever looked up, each as the kind
    it is declared."""

    def __init__(self, bench: dict, given: Optional[Dict[str, dict]] = None):
        self.key = bench["key"]
        self.kinds = {s["name"]: s["kind"] for s in definitions.secrets_of(bench)}
        self.given = given

    @classmethod
    def handed(cls, bench: dict, values: Dict[str, bytes], spill: str) -> "Licences":
        """The licences a host's run was sent (name -> bytes), a file among
        them written to ``spill`` (0700) as a 0600 file of its own."""
        me = cls(bench, {})
        for name, data in values.items():
            kind = me.kinds.get(name)
            if kind is None:
                continue
            if kind == "file":
                _own_dir(spill)
                p = os.path.join(spill, name)
                if os.path.lexists(p):               # ours: the folder is checked to be this user's alone
                    os.unlink(p)
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                me.given[name] = {"kind": kind, "path": p}
            else:
                me.given[name] = {"kind": kind, "value": data.decode(errors="replace").strip()}
        return me

    def get(self, name: str) -> Optional[dict]:
        kind = self.kinds.get(name)
        if kind is None:
            return None
        if self.given is not None:
            return self.given.get(name)
        from nullius import secrets
        if kind == "file":
            p = secrets.path(self.key, name)
            return {"kind": kind, "path": p} if p else None
        data = secrets.value(self.key, name)
        text = data.decode(errors="replace").strip() if data else ""
        return {"kind": kind, "value": text} if text else None


class Run:
    def __init__(self, bench: dict, other: dict, *, model: str, provider: str, dataset: Optional[str] = None,
                 limit: Optional[int] = None, concurrency: int = 4, jobs_root: str = "jobs", name: Optional[str] = None,
                 turn_seconds: Optional[int] = None, say: Callable[[str], None] = print, env=os.environ,
                 proving: bool = False, unsandboxed: bool = False, judge_model: Optional[str] = None,
                 backend: str = "local", options: Optional[dict] = None, shard: Optional[tuple] = None,
                 launch: Optional[str] = None, repeat: int = 1, simulator_model: Optional[str] = None,
                 licences: Optional[Licences] = None, bench_options: Optional[dict] = None, which=shutil.which,
                 remove_images: bool = False, prices: Optional[dict] = None, max_usd: Optional[float] = None,
                 max_usd_per_unit: Optional[float] = None):
        """``proving``: the run is a conformance kit's, the one run that may
        start before the port it checks is proven - the family must still match.
        ``unsandboxed``: consent to execute code as the user: required for
        local agents, and for setup checks that run code without Docker.
        ``backend``: ``local`` or ``docker`` (``container``), which needs
        no such yes and is refused when docker is missing or not usable.
        ``options``: the
        agent's or capability's options set for this run, by name; the rest keep
        their defaults, and the job's config records every value.
        ``shard``: (I, N), only the units of shard I of N (``shard``), before
        ``limit``; ``launch``: the id every shard of one launch records.
        ``repeat``: each unit run that many times (a conformance check run for
        confidence, ``conform``), each run a trial of its own tagged
        ``u0042r2``; once by default, tagged ``u0042`` as always.
        ``simulator_model``: a benchmark whose user a model plays
        (``simulator``): this model instead of its official one.
        ``prices``: this run's own prices, by model, over the user's list
        (``nullius.prices``): what a call's cost is estimated from.
        ``max_usd_per_unit``: each unit's own spending cap, kept in the same
        ledger: its own calls and its simulated user's count toward it, its
        judge's do not. A unit that reaches it ends as ``ledger.UNIT_ERROR``,
        an agent error counted against the agent, and the run goes on.
        ``max_usd``: the run's spending cap (``ledger``): every relay of the
        job books a call before sending it, and once one does not fit no
        further call goes out and nothing more is begun. Refused when a model
        the run calls has no price: a call that cannot be priced cannot be
        booked.
        ``licences``: what the run holds of the benchmark's licences
        (:class:`Licences`); by default its store on this machine.
        ``bench_options``: the benchmark's own options for this run
        (``options`` in its definition: a search, a mode), the rest at their
        defaults; a value that needs an embedding model gets a relay of the
        run's own for it, one that needs something of this machine
        (``host``) is refused without it."""
        if backend not in BACKENDS:
            raise Refused(f"backend {backend!r} is not one of {list(BACKENDS)}")
        if other.get("_kind") == "upstream" and backend != "local":
            raise Refused(f"{other['label']} runs {bench['label']}'s own code on this machine, as published: there is no "
                          "agent or capability to put in a container - run it on local processes")
        if backend == "local" and other.get("_kind") == "agent" and not unsandboxed:
            raise Refused(f"{other['label']} would run commands as you: the local backend is not a sandbox. "
                          "Say so explicitly to go on (--unsandboxed), or run it in a container (--backend docker).")
        why = match.fit(bench, other)
        if proving:
            why = [w for w in why if "does not offer" not in w and "has not passed its kit" not in w]
        if why:
            raise Refused(f"{other['label']} cannot run {bench['label']}: " + "; ".join(why))
        if backend == "docker":
            why = container.unusable()
            if why:
                raise Refused(f"the docker backend cannot run here: {why}")
        self.container_check = False
        if backend == "local" and other.get("_kind") == "upstream" and (bench.get("score") or {}).get("runs_code"):
            why = container.unusable()
            if why and not unsandboxed:
                raise Refused(f"{bench['label']}'s check executes generated code, but Docker is unavailable: {why}. "
                              "Make Docker usable, or explicitly allow execution as you (--unsandboxed).")
            self.container_check = not why
        self.bench, self.other, self.model, self.provider, self.backend = bench, other, model, provider, backend
        # A setup (definitions.setup): the upstream's own answerer, asked as one model (``asked``), the relay
        # sending another (``sent``); its units may be its own (a conversation where the paper batches questions).
        self.upstream = other.get("_kind") == "upstream"
        self.asked = other.get("asked", "") if self.upstream else ""
        if self.upstream:
            self.model = other["sent"]
            self.bench = bench = dict(bench, units=other["units"])
        self.licences = licences or Licences(bench)     # where a benchmark's environments find the licences they need
        self.unsandboxed = bool(unsandboxed)
        self.image = ""
        self.options, bad = definitions.options_of(other, options)
        if bad:
            raise Refused("; ".join(bad))
        # A benchmark scored by a model: its official judge, or the one asked for; its calls go through the relay
        # on a route of their own (<tag>-judge), recorded beside the unit's and never counted as the agent's.
        # The official one is written as its maker spells it; the run's provider may spell it otherwise.
        j = (bench.get("score") or {}).get("judge")
        self.judge_model = (judge_model or providers.served_as(j["model"], provider)) if j else None
        # A benchmark whose user a model plays: its driver's turns ask that model the same way, on <tag>-simulator.
        # A control holds no conversation, so it never asks it.
        sim = bench.get("simulator")
        self.simulator_model = (simulator_model or providers.served_as(sim["model"], provider)) if sim else None
        # A control (definitions.control) calls no model: its run needs a key only for the benchmark's judge.
        ds = [d for d in bench["datasets"] if dataset in (None, d["id"])]
        if not ds:
            raise Refused(f"{bench['label']} has no dataset {dataset!r}: {[d['id'] for d in bench['datasets']]}")
        self.ds = ds[0]
        try:
            _, env_def = definitions.environment_of(bench, None, self.ds)
        except ValueError as e:
            raise Refused(str(e))
        if backend != "docker" and other.get("_kind") == "control" and definitions.own_image(env_def):
            why = container.unusable()
            if why:
                raise Refused(f"each {(bench.get('units') or {}).get('noun', 'unit')} of {bench['label']} runs in its own image, "
                              f"which needs docker: {why}")
        # The benchmark's own options on this dataset; what their values need: an embedding model the driver
        # calls through a relay of the run's own (bench_relay), programs on this machine.
        # each option where it applies: a setup's own (a retriever, a top-k), or those of the benchmark's port
        self.bench_options, bad = definitions.bench_options_of(bench, self.ds["id"], bench_options,
                                                               other["key"] if self.upstream else "yours")
        if bad:
            raise Refused("; ".join(bad))
        needs = definitions.option_needs(bench, self.bench_options)
        self.embed_model = needs["embeddings"][0] if needs["embeddings"] else None
        # What a call's cost is estimated from when the provider does not say it: the user's price list
        # (nullius.prices), this run's own prices over it. Read once, here: a run never fetches a price.
        self.run_prices = dict(prices or {})
        self.prices = {**price_list.table(), **self.run_prices}
        self.max_usd, self.unit_max_usd, self.ledger = None, None, None
        self.capped = False                      # whether the run's spending cap stopped it
        for flag, given, what in (("--max-usd", max_usd, "the run's spending cap"), ("--max-usd-per-unit", max_usd_per_unit, "a unit's spending cap")):
            if given is None:
                continue
            try:
                cap = float(given)
            except (TypeError, ValueError):
                cap = 0.0
            if not 0 < cap < float("inf"):
                raise Refused(f"{flag} {given!r}: {what} in USD, above zero")
            if flag == "--max-usd":
                self.max_usd = cap
            else:
                self.unit_max_usd = cap
        if self.max_usd is not None or self.unit_max_usd is not None:
            unpriced = [m for m in self.models_called() if m not in self.prices]
            if unpriced:
                raise Refused("a spending cap needs a price for every model the run calls, and there is none for "
                              + ", ".join(unpriced) + ": nullius prices update, nullius prices set MODEL INPUT OUTPUT, "
                              "or --price MODEL=INPUT,OUTPUT")
        lacking = hostreq.missing(bench, needs["host"], which)
        if lacking:
            raise Refused(f"{', '.join(f'{k}={v}' for k, v in self.bench_options.items())} needs on this machine "
                          + "; ".join(f"{n} ({', '.join(gone)} not on PATH)" for n, gone in lacking.items())
                          + (". Install with: " + " && ".join(lines) if (lines := hostreq.install_lines(bench, list(lacking)))
                             else ". Run it on a machine that has it"))
        self.bench_relay = None
        keyless = other.get("_kind") == "control" and not self.judge_model and not self.embed_model
        self.base, self.key = (None, None) if keyless else provider_endpoint(provider, env)
        self.limit, self.concurrency = limit, max(1, int(concurrency))
        self.repeat = max(1, int(repeat or 1))
        # an own image this run pulled or built stays for the next run, unless the user asks it removed
        # when its last unit ends (never with a repeat): {image: how many units are using it now}
        self.keep_images = not remove_images or self.repeat > 1
        self._image_users: Dict[str, int] = {}
        self.turn_seconds = (turn_seconds or (other.get("turn_seconds") if self.upstream else None)
                             or (bench.get("limits") or {}).get("turn_seconds") or TURN_SECONDS)
        # the most agent turns a conversation runs; a driver whose upstream ends the conversation by its own
        # limits sets this above them (limits.turns), so that its limits, not this one, are the ones met
        self.max_turns = (bench.get("limits") or {}).get("turns") or MAX_TURNS
        self.say = say
        stamp = time.strftime("%Y-%m-%d__%H-%M-%S")
        self.shard = tuple(shard) if shard and shard[1] > 1 else None
        self.launch = launch or (shards.launch_id(definitions.job_word(bench), definitions.job_word(other), model)
                                 if self.shard else None)
        self.name = name or (f"{stamp}__{definitions.job_word(bench)}__{definitions.job_word(other)}"
                             + (f"__shard-{self.shard[0]}of{self.shard[1]}" if self.shard else ""))
        self.job_dir = os.path.abspath(os.path.join(jobs_root, self.name))
        # each stage said as it goes, on the cli's stderr and in the job's progress.jsonl (nullius/progress.py)
        self.progress = progress.Reporter(self.job_dir)
        self._said = threading.Lock()
        # the job page's live board (nullius/runs/live.py): written once the units are known
        self.feed: Optional[live_feed.Writer] = None
        self._ended = 0                      # units ended so far: the i of the next unit's line
        self._envs: set = set()              # environments whose build was said
        self.n_units: Optional[int] = None
        self.local = threading.local()
        self.drivers: List[jsonl.Proc] = []
        self._stopped = threading.Event()

    def driver(self) -> jsonl.Proc:
        d = getattr(self.local, "driver", None)
        if d is not None and d.p is not None and d.p.poll() is not None:
            d = None                     # it ended - a reply that never came closed it: the next unit gets a new one
        if d is None:
            try:
                cmd, extra = install.driver_argv(self.bench)     # its driver.install: installed in ready(), found here
            except install.InstallFailed as e:
                raise Refused(f"could not install the driver's environment: {e}")
            env = base_env(os.path.join(self.job_dir, ".work", "driver-home"),
                           {**extra, "NULLIUS_UPSTREAM": self.up or "", "NULLIUS_DATASET": self.data or "",
                            "NULLIUS_DATASET_ID": self.ds["id"], "PYTHONPATH": driver_path(), "PYTHONDONTWRITEBYTECODE": "1",
                            "NULLIUS_VERSION": str(self.bench.get("_version") or ""),
                            **({container.CHECK_IMAGE: self.image,
                                **{k: os.environ[k] for k in container.CLIENT_KEEP if k.startswith("DOCKER") and k in os.environ}}
                               if self.image else {}),
                            **option_env(getattr(self, "bench_options", {})), **self.embeddings_env(), **self.driver_secrets()})
            os.makedirs(env["HOME"], exist_ok=True)
            # its working directory is the job's, never its catalogue folder: an upstream that writes
            # beside itself (tau2's embeddings cache) would write into the package or the runner snapshot
            cwd = os.path.join(self.job_dir, ".work", "driver")
            os.makedirs(cwd, exist_ok=True)
            d = jsonl.Proc(cmd, cwd, env, os.path.join(self.job_dir, ".work", f"driver-{threading.get_ident()}.log"), stopped=self._stopped).start()
            self.local.driver = d
            self.drivers.append(d)
        return d

    def start(self) -> str:
        t0 = time.time()
        os.makedirs(os.path.join(self.job_dir, ".work"), exist_ok=True)
        self.say(f"job {self.job_dir}")
        p, control = self.progress, self.other["_kind"] == "control"
        p.emit("plan", stages=",".join((["image"] if self.backend == "docker" or self.container_check else [])
                                      + (["upstream"] if install.upstream_dir(self.bench) else [])
                                      + (["dataset"] if self.ds.get("url") else [])
                                      + (["driver"] if install.driver_dir(self.bench) else [])
                                      + ([] if control or self.upstream else ["install"]) + ["units"]))
        if (self.max_usd is not None or self.unit_max_usd is not None) and self.base:
            # one ledger for the job, in a folder of its own: all a unit's relay container is given of the job besides its record
            self.ledger = ledgers.Ledger.create(os.path.join(self.job_dir, ".work", "ledger", "ledger.json"), self.max_usd, self.unit_max_usd)
        try:
            self.ready()
        except Refused as e:
            # said in the job, which holds nothing else: the runs list reads why there (progress.refusal)
            p.emit("refused", detail=str(e))
            raise
        units = self.driver().request("units", dataset=self.ds["id"], timeout=300,
                                      **({"setup": self.other["key"]} if self.upstream else {}))["units"]
        everything = [str(u) for u in units]
        # the shard, then the limit; each unit keeps its place in the whole dataset (its trial's tag)
        mine = shards.select(everything, *self.shard) if self.shard else list(enumerate(everything))
        mine = mine[: self.limit] if self.limit else mine
        units = [u for _, u in mine]
        self.n_units = len(units)
        p.emit("units", "start", n=len(units), of=len(everything), noun=self.bench["units"]["nouns"])
        job.write_config(self.job_dir, job=self.name, bench=self.bench, other=self.other, model=self.model,
                         provider=self.provider, dataset=self.ds["id"], units=units, concurrency=self.concurrency,
                         granted=self.other["_kind"] == "agent", judge=self.judge_model or "", backend=self.backend,
                         image=self.image, options=self.options, launch=self.launch or "", bench_options=self.bench_options,
                         shard=shards.record(*self.shard, len(everything), units, self.limit) if self.shard else None,
                         repeat=self.repeat, prices=self.prices_record(),
                         simulator=self.simulator_model if self.other["_kind"] in ("agent", "upstream") and self.simulator_model else "")
        # every unit once, then every unit again: a repeat cut short still has each unit's first run
        work = [(i, u, r) for r in range(1, self.repeat + 1) for i, u in mine]
        self.n_units = len(work)                 # what the progress counts: every run of every unit
        self.feed = live_feed.Writer(os.path.join(self.job_dir, live_feed.FEED), self.name)
        self.feed.emit("job.start", n_total=len(work), n_concurrent=self.concurrency, agents=[self.other["key"]], tasks=units,
                       **({"budget": {k: v for k, v in (("max_usd", self.max_usd), ("unit_max_usd", self.unit_max_usd)) if v is not None}}
                          if self.ledger is not None else {}))   # the live board says the cap
        for pos, (i, u, r) in enumerate(work):
            self.feed.emit("trial.queued", self.trial_name(i, u, r), agent=self.other["key"], position=pos)
        results = []
        u0 = time.time()
        self._halt, self._refused, self.halted = threading.Event(), [], None
        self._cut: List[str] = []                # units the spending cap cut off
        self._over: List[str] = []               # units that reached their own cap: cut off there, the run going on
        # a model server on this machine's loopback, which the relays' containers cannot reach as written
        self.loopback = (container.Loopback(self.base).start()
                         if self.backend == "docker" and self.base and container.on_loopback(self.base) else None)
        def work_unit(iur):
            if self._stopped.is_set():
                return None
            try:
                return self.unit(*iur)
            except BaseException:
                self._stopped.set()
                self._halt.set()
                raise

        pool = ThreadPoolExecutor(self.concurrency)
        futures = []
        try:
            for iur in work:
                futures.append(pool.submit(work_unit, iur))
            for future in as_completed(futures):
                res = future.result()
                if res is not None:              # None: a unit not begun once the endpoint refused the run
                    results.append(res)
        except BaseException:
            self._stopped.set()
            self._halt.set()
            if self.backend == "docker":
                container.sweep(self.name)
            raise
        finally:
            # a Stop or Ctrl-C: the units not begun are not begun, those running end with their agents
            pool.shutdown(wait=True, cancel_futures=True)
            begun = [not f.cancelled() and f.exception() is None and f.result() is not None for f in futures]
            results = [f.result() for f, ran in zip(futures, begun) if ran]
            for d in self.drivers:
                d.close()
            if self.bench_relay is not None:
                self.bench_relay.stop()
            if self.backend == "docker":
                container.sweep(self.name)          # whatever an interrupted unit left
            if self.loopback is not None:
                self.loopback.stop()
            budget = self.budget([self.attempt_name(u, r) for n, (_, u, r) in enumerate(work) if n >= len(begun) or not begun[n]])
            self.capped = bool(budget and budget["stopped"])
            job.write_result(self.job_dir, self.name, t0, time.time(), results, len(work), budget=budget)
            errored = sum(1 for x in results if x.get("exception_info"))
            self.feed.emit("job.end", n_completed=len(results) - errored, n_errored=errored, n_cancelled=len(work) - len(results))
        # the units' stage says why it ended early: a line of its own would be a stage that never starts
        p.emit("units", "done", seconds=round(time.time() - u0, 1),
               **({"detail": f"stopped at the spending cap of {ledgers.usd(budget['max_usd'])} USD: "
                             f"{len(budget['cut_off'])} cut off, {len(budget['not_run'])} not run"} if budget and budget["stopped"] else {}))
        self.n_verdicts, self.n_ended = sum(1 for x in results if x.get("verifier_result")), len(results)
        if budget and budget["stopped"]:
            spent = "unknown" if budget["spent_usd"] is None else ledgers.usd(budget["spent_usd"])
            self.say(f"stopped: a call did not fit the spending cap of {ledgers.usd(budget['max_usd'])} USD "
                     f"({spent} spent): {len(budget['cut_off'])} cut off, {len(budget['not_run'])} not run")
        elif budget and budget["max_usd"] is not None:
            self.say(f"spent {ledgers.usd(budget['spent_usd'] or 0)} of the {ledgers.usd(budget['max_usd'])} USD cap")
        elif budget:
            self.say(f"spent {ledgers.usd(budget['spent_usd'] or 0)} USD")
        if budget and budget.get("over_unit"):
            self.say(f"{len(budget['over_unit'])} of the {self.bench['units']['nouns']} reached the cap of "
                     f"{ledgers.usd(budget['unit_max_usd'])} USD each and were cut off there")
        if self.halted:
            self.say(f"stopped: the endpoint refused {REFUSED_IN_A_ROW} units in a row ({self.halted[0]}: "
                     f"{redact(str(self.halted[1]))[:200]}); the {len(work) - len(results)} units not begun were not run")
        rewards = [(r.get("verifier_result") or {}).get("rewards", {}) for r in results]
        if self.bench["score"]["kind"] == "pass":
            ok = sum(1 for w in rewards if w.get("reward") == 1.0)
            p.emit("done", n=len(results), passed=ok, seconds=round(time.time() - t0, 1))
            self.say(f"done: {len(results)} {self.bench['units']['nouns']}, {ok} passed")
        else:
            scores = [w["score"] for w in rewards if "score" in w]
            mean = job.mean_score(results)
            p.emit("done", n=len(results), scored=len(scores), mean=round(mean, 3) if mean is not None else None,
                   seconds=round(time.time() - t0, 1))
            self.say(f"done: {len(results)} {self.bench['units']['nouns']}, {len(scores)} scored"
                     + (f", mean {mean:.3f}" if mean is not None else ""))
        return self.job_dir

    def ready(self) -> None:
        """What the run needs before its first unit, each a stage said as it
        goes - or as there already: the base image (docker), the benchmark's
        upstream, its dataset when it is downloaded, its driver's environment
        when it declares one (``driver.install``), the agent's or capability's
        packages. Refused when one cannot be had."""
        p = self.progress
        if self.backend == "docker" or self.container_check:
            try:
                tag = container.base_tag()
                there = container.present(tag)
                with p.stage("image", cached=there, start=f"building {tag} (first time on this machine)", **({"detail": tag} if there else {})):
                    self.image = container.base_image(os.path.join(self.job_dir, ".work", "image.log"))
            except container.Unavailable as e:
                raise Refused(f"the Docker image: {e}")
        # on docker a machine without npm installs in the base image, which carries the Node the agent will run on
        npm_in = (lambda cmd, where: container.install_argv(self.image, where, cmd)) if self.backend == "docker" else None
        try:
            where = install.upstream_dir(self.bench)
            self.up = None
            if where is not None:
                up = self.bench["upstream"]
                with p.stage("upstream", cached=install.ready(where), start=f"fetching {up['repository']} at {str(up['tested'])[:12]}"):
                    self.up = install.upstream(self.bench)
            if self.ds.get("url"):
                there = os.path.isfile(install.download_path(self.ds["url"], self.ds["sha256"]))
                with p.stage("dataset", cached=there, start=f"downloading {self.ds['id']}"):
                    self.data = install.dataset(self.bench, self.ds, self.up, progress=None if there else p.download("dataset"))
            else:
                self.data = install.dataset(self.bench, self.ds, self.up)
            drv = install.driver_dir(self.bench)
            if drv is not None:                  # the environment the upstream's own code needs, for the driver
                with p.stage("driver", cached=install.ready(drv), start=f"installing {self.bench['key']}'s driver environment"):
                    install.driver_packages(self.bench)
            self.inst = ""
            if self.other["_kind"] not in ("control", "upstream"):
                with p.stage("install", cached=install.ready(install.packages_dir(self.other)), agent=self.other.get("_ref") or self.other["key"]):
                    self.inst = install.packages(self.other, npm_in=npm_in)
        except install.InstallFailed as e:
            raise Refused(f"could not install what the run needs: {e}")
        # An own image has none of what the agent runs on: it is mounted in, fetched once on this machine.
        self.runtimes: Dict[str, tuple] = {}
        owns = any(definitions.own_image(e) for e in (self.bench.get("environments") or {}).values())
        if owns and self.other["_kind"] == "agent" and self.backend == "docker" and self.runtimes_needed():
            try:
                with p.stage("runtime", start="fetching " + ", ".join(self.runtimes_needed()), name=",".join(self.runtimes_needed())):
                    self.runtimes = container.runtime_mounts(self.runtimes_needed())
            except container.Unavailable as e:
                raise Refused(f"the agent's runtime for an own image: {e}")
        # An option whose driver embeds (a search over documents): its calls go through a relay of the run's
        # own, recorded in the job's .work/relay-benchmark beside the units', never counted as the agent's.
        if self.embed_model and self.bench_relay is None:
            self.bench_relay = relay.Relay(self.base, self.key, os.path.join(self.job_dir, ".work", "relay-benchmark"),
                                           prices=self.prices, model=self.embed_model, ledger=self.ledger_file(), unit_caps=False,
                                           key_header=PROVIDERS[self.provider].get("key_header", "authorization")).start()

    def runtimes_needed(self) -> List[str]:
        """What the agent needs in an own image that the image may not have:
        Node for an agent npm installs, Python for one started as ``python3``
        with no environment of its own (a uv environment's interpreter is
        mounted as on the base image, ``container.python_homes``)."""
        a = self.other
        start = (a.get("start") or {}).get("acp") or (a.get("start") or {}).get("command") or [""]
        needs = ["node"] if (a.get("install") or {}).get("npm") else []
        if container.bare_python(start[0]) and not container.python_homes(self.inst):
            needs.append("python")
        return needs

    def attempt_kw(self) -> dict:
        """What the driver is told of the attempt this unit's run is (1, 2, ...), when the benchmark
        declares that its upstream tells attempts apart (``limits.attempts``: a seed per trial)."""
        a = getattr(self.local, "attempt", None)
        return {"attempt": a} if a and (self.bench.get("limits") or {}).get("attempts") else {}

    def trial_name(self, i: int, unit: str, r: int = 1) -> str:
        return f"{_slug(unit)}__u{i:04d}" + (f"r{r}" if self.repeat > 1 else "")

    def phase(self, name: str, unit: str, phase: str, timeout: Optional[float] = None) -> None:
        if self.feed is not None:
            self.feed.emit("trial.phase", name, task=unit, agent=self.other["key"], phase=phase, timeout_sec=timeout)

    def unit(self, i: int, unit: str, r: int = 1) -> Optional[dict]:
        if self._stopped.is_set() or (getattr(self, "_halt", None) is not None and self._halt.is_set()):
            return None                          # the endpoint refused the run: nothing more is begun
        if getattr(self, "ledger", None) is not None and self.ledger.closed():
            self._halt.set()                     # the spending cap refused a call: every later one is refused too
            return None
        tag = f"u{i:04d}" + (f"r{r}" if self.repeat > 1 else "")
        self.local.attempt = r
        trial = os.path.join(self.job_dir, self.trial_name(i, unit, r))
        name = os.path.basename(trial)
        ws, home = os.path.join(trial, "workspace"), os.path.join(trial, ".home")
        os.makedirs(ws, exist_ok=True)
        # The workspace is a repository of its own: agents look up to the git root
        # for instructions (AGENTS.md, CLAUDE.md, their config) and, with jobs kept
        # inside a checkout, would otherwise read that checkout's into every prompt.
        subprocess.run(["git", "init", "-q", ws], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        os.makedirs(home, exist_ok=True)
        t0 = time.time()
        self.progress.emit("unit", "start", unit=unit)
        self.phase(name, unit, "env_setup", None if self.upstream else DRIVER_SECONDS)
        produce, score, exc, traj, logs = (None, None), None, None, None, {}
        p0, checking = None, (None, None)        # when the agent began; the check's start and end
        setup: dict = {}
        transcript: List[dict] = []
        rl, environment, box, changes = None, None, None, None
        try:
            try:
                # a setup's unit needs none: the upstream's answerer reads its data itself
                setup = {} if self.upstream else self.driver().request("setup", unit=unit, dir=ws, timeout=DRIVER_SECONDS, **self.attempt_kw())
            except jsonl.Failed as e:
                raise _Ended("EnvironmentSetupError", str(e))
            environment, env_def = self.unit_environment(setup)
            why = self.not_run(environment, env_def)
            if why:
                raise _Ended(*why)
            own = definitions.own_image(env_def)
            if self.other["_kind"] == "agent" and self.backend == "local" and mcp_machine.wanted(setup):
                try:                             # the benchmark's own servers: the agent starts them from the driver's environment
                    setup = dict(setup, mcp=mcp_machine.entries(self.bench, setup, install.driver_dir(self.bench),
                                                                self.machine_env(trial)))
                except mcp_machine.Unknown as e:
                    raise _Ended("EnvironmentSetupError", f"the unit's setup: {e}")
            if setup.get("verify") is not None and not own:
                raise _Ended("EnvironmentSetupError", "the unit's setup: verify runs in an own image, and this unit has none")
            try:
                rl = self.unit_relay(tag, trial, setup, env_def, own)
            except (container.Unavailable, OSError) as e:
                raise _Ended("EnvironmentSetupError", f"the unit's relay: {e}")
            if rl is not None:
                rl.unit_secrets = self.agent_licences(env_def, rl)
            if own:
                self.phase(name, unit, "env_setup", IMAGE_SECONDS)     # its image, pulled or built when not here yet
                box = self.own_box(tag, setup, rl, home)
            p0 = time.time()
            self.phase(name, unit, "agent_run", self.turn_limit(setup) if self.other["_kind"] != "control"
                       else DRIVER_SECONDS if self.other["key"] == "oracle" else None)
            try:
                if self.other["_kind"] == "control":
                    answer = self.control_answer(unit, ws, box, name)
                elif self.upstream:
                    answer, traj = self.drive_upstream(unit, ws, tag, transcript, rl)
                elif self.other["_kind"] == "agent":
                    answer, traj, logs = self.drive_agent(unit, ws, home, tag, setup, transcript, rl, box, name)
                else:
                    answer, traj = self.drive_feature(unit, ws, home, tag, setup, transcript, rl)
            finally:
                produce = (p0, time.time())
                if box is not None:              # what the agent (or the control) left in the container, before the check
                    changes = self.box_changes(box)
            cut = self.cap_reached(rl, tag)
            if cut:                              # cut off at a spending cap: nothing of it is checked, and its judge is asked nothing
                raise _Ended(cut[0], cut[1], traj)
            step = setup.get("verify") if box is not None else None
            checking = (time.time(), None)
            self.phase(name, unit, "verify", DRIVER_SECONDS + (float(step.get("seconds") or self.turn_seconds) if step else 0))
            verified = self.verify(setup["verify"], box, trial) if box is not None and setup.get("verify") else None
            if self.backend == "docker":     # the agent could write here (an MCP server's log folder): no link it left is read
                container.plain_only(os.path.join(trial, "verifier"))
            try:
                judge = self.judge(rl, tag)
                score = self.driver().request("score", unit=unit, dir=ws, transcript=transcript, answer=answer, timeout=DRIVER_SECONDS,
                                              **judge, **({"setup": self.other["key"]} if self.upstream else {}), **({"verified": verified} if verified is not None else {}))
                checking = (checking[0], time.time())
            except jsonl.Failed as e:
                raise _Ended("VerifierError", str(e))
        except _Ended as e:
            exc = (e.kind, e.said)
            traj = traj or e.trajectory
        except Exception as e:                   # anything else is this unit's error, never the whole job's
            exc = (type(e).__name__, f"{type(e).__name__}: {e}\n" + "".join(traceback.format_exc().splitlines(True)[-6:]))
        finally:
            if checking[0] is not None and checking[1] is None:
                checking = (checking[0], time.time())
            if box is not None:
                # the home it wrote as root given back, then the container gone
                box.give_back([home, *getattr(rl, "unit_rw", [])])
                box.stop()
                self.release_image(box.image)
            if rl is not None:
                rl.stop()
                for ln in getattr(rl, "mcp_listeners", None) or []:
                    ln.stop()
        calls = rl.calls(tag) if rl is not None else []
        failed = relay.failure(calls)
        if not failed and rl is not None and exc is not None and exc[0] == "VerifierError":
            # the judge's or the simulated user's model failing (a 429, a refused request) is the
            # unit's infrastructure, as the agent's own would be - not a miss counted against it
            failed = next(filter(None, (relay.failure(rl.calls(f"{tag}-{side}")) for side in ("judge", "simulator"))), None)
        if failed and (exc is None or exc[0] not in ("EnvironmentSetupError",)):
            exc = failed                     # what the endpoint said outranks how the agent took it
            score = None
        cut = self.cap_reached(rl, tag)          # at scoring too: its judge's call, or a refusal its agent made something else of
        if cut:
            exc, score = cut, None
        if exc and exc[0] in ("EnvironmentSetupError", "VerifierError") and getattr(self, "ledger", None) is not None \
                and getattr(self, "bench_relay", None) is not None:
            # its driver failed, at setup or at scoring, and the cap refused a call that driver made for this unit (an
            # option's embeddings, on its thread's route of the benchmark's relay): the unit was cut off there. Another
            # unit's refusal is not this one's, and an agent's own error stays the agent's
            late = [c for c in self.bench_relay.calls(self.embeddings_tag()) if c.get("refused") == "cap" and c.get("t0", 0) >= t0]
            if late:
                exc, score = (ledgers.ERROR, late[0].get("error") or ""), None
        if self._stopped.is_set() and score is None:
            # the Stop ended its agent, its relay or its containers under it: what it then failed with is
            # the Stop's doing, not the agent's nor the environment's (Harbor's name for a stopped trial)
            exc = (CANCELLED, "the run was stopped before this unit ended" + (f"; it then failed with {exc[0]}" if exc else ""))
        if traj is None and self.other["_kind"] == "agent":
            traj = job.trajectory_from_events([], calls, self.other, self.model, f"{self.name}/{unit}")
        if traj is not None and setup.get("mcp"):    # how a reader tells the benchmark's tools in any agent's spelling
            traj["extra"] = dict(traj.get("extra") or {}, mcp_servers=[str(m.get("name")) for m in setup["mcp"] if m.get("name")])
        res = job.write_trial(trial, job=self.name, bench=self.bench, other=self.other, model=self.model, unit=unit,
                              t0=t0, t1=time.time(), produce=produce, score=score, exception=exc, calls=calls,
                              setup_end=p0 or time.time(), checking=checking,
                              trajectory=traj, extra_logs=dict(logs or {}, **({"container-changes.txt": changes} if changes else {})),
                              environment=environment or "",
                              image=(box.image if box is not None else getattr(rl, "unit_image", "")) or "")
        install.prune(self.other, home)          # its declared caches: the trial is written, no one reads them now
        verdict = "error " + exc[0] if exc else f"reward {score.get('reward')}"
        self.say(f"  {unit}: {verdict}")
        self.said_unit(unit, score, exc)
        if self.feed is not None:
            self.feed.emit("trial.end", name, task=unit, agent=self.other["key"],
                           **job.feed_outcome(self.bench["score"]["kind"], score, exc),
                           **{k: v for k, v in (res.get("agent_result") or {}).items() if k in FEED_TOTALS},
                           durations={"agent_run": round(produce[1] - produce[0], 3)} if produce[0] is not None else None)
        self.note_refusal(exc)
        if exc and exc[0] == ledgers.ERROR and getattr(self, "_halt", None) is not None:
            with self._said:
                self._cut.append(self.attempt_name(unit, r))
            self._halt.set()                     # the cap is reached: nothing more is begun
        elif exc and exc[0] == ledgers.UNIT_ERROR:
            with self._said:
                self._over.append(self.attempt_name(unit, r))     # its own cap: the others go on
        if getattr(self, "ledger", None) is not None and self.unit_max_usd is not None:
            try:
                self.ledger.forget(tag)          # the unit ended: its own count goes from the ledger
            except ledgers.Unavailable:
                pass
        return res

    def cap_reached(self, rl, tag: str) -> Optional[tuple]:
        """A spending cap's refusal among a unit's calls - its agent's, its
        judge's or its simulated user's - as what the unit ends with: cut
        off by the run's own limit, never a miss, before its own. None
        without one."""
        if rl is None or getattr(self, "ledger", None) is None:
            return None
        cuts = [f for f in (relay.failure(rl.calls(t)) for t in (tag, f"{tag}-judge", f"{tag}-simulator")) if f]
        return next((f for f in cuts if f[0] == ledgers.ERROR), None) or next((f for f in cuts if f[0] == ledgers.UNIT_ERROR), None)

    def attempt_name(self, unit: str, r: int = 1) -> str:
        """A unit as the budget lists it: by its name, and its attempt when the run repeats its units."""
        return unit if self.repeat <= 1 else f"{unit} (attempt {r})"

    def budget(self, not_run: List[str]) -> Optional[dict]:
        """What ``result.json`` says of the run's spending cap: the cap,
        what was spent, what was booked for calls whose cost was never known
        (``unsettled_usd``, said only when there is some), whether the cap
        stopped the run, the units it cut off and those never begun. With a
        cap per unit, that cap and the units that reached it
        (``unit_max_usd``, ``over_unit``); ``max_usd`` is then None when the
        run itself had none. None for a run without a cap."""
        if getattr(self, "ledger", None) is None:
            return None
        try:
            got = self.ledger.state()
        except ledgers.Unavailable:
            got = {"spent_usd": None, "unsettled_usd": 0.0, "held_usd": 0.0, "closed": True}
        unsettled = round(got["unsettled_usd"] + got["held_usd"], 6)
        stopped = bool(got["closed"]) and self.max_usd is not None     # only the run's cap stops a run
        cut = getattr(self, "_cut", [])
        return {"max_usd": self.max_usd, "spent_usd": None if got["spent_usd"] is None else round(got["spent_usd"], 6),
                **({"unsettled_usd": unsettled} if unsettled else {}), "stopped": stopped,
                "cut_off": list(dict.fromkeys(cut)), "not_run": list(dict.fromkeys(not_run)) if stopped else [],
                **({"unit_max_usd": self.unit_max_usd, "over_unit": list(dict.fromkeys(getattr(self, "_over", [])))}
                   if self.unit_max_usd is not None else {})}

    def note_refusal(self, exc: Optional[tuple]) -> None:
        """A unit's end, counted toward the endpoint refusing the run: a wrong
        key or an unknown model fails every unit the same way, and running
        the rest only repeats it (with their containers and relays). After
        ``REFUSED_IN_A_ROW`` such ends in a row, nothing more is begun."""
        if getattr(self, "_halt", None) is None:
            return
        with self._said:
            if exc and exc[0] in REFUSALS:
                self._refused.append(exc)
                if len(self._refused) >= REFUSED_IN_A_ROW and self.halted is None:
                    self.halted = exc
                    self._halt.set()
            else:
                self._refused.clear()

    def said_unit(self, unit: str, score: Optional[dict], exc: Optional[tuple]) -> None:
        """A unit's end, as a progress line: the i-th to end of the run's n,
        and how - passed or failed (a pass benchmark), scored, or the error."""
        with self._said:
            self._ended += 1
            i = self._ended
        score = score or {}
        if exc:
            how = {"outcome": "error", "error": exc[0]}
        elif self.bench["score"]["kind"] == "pass":
            how = {"outcome": "passed" if score.get("reward") == 1.0 else "failed", "reward": score.get("reward")}
        else:
            how = {"outcome": "scored", "score": score.get("score", score.get("reward"))}
        self.progress.emit("unit", "done", i=i, of=self.n_units, unit=unit, **how)

    def unit_environment(self, setup: dict):
        """(name, definition) of the environment the unit asks for - its
        setup's ``environment``, else its dataset's - or (None, None), the base
        image. A name the benchmark does not declare is the driver's error."""
        try:
            return definitions.environment_of(self.bench, setup, self.ds)
        except ValueError as e:
            raise _Ended("EnvironmentSetupError", f"the unit's setup: {e}")

    def not_run(self, name: Optional[str], env_def: Optional[dict]) -> Optional[tuple]:
        """Why a unit cannot be run in its environment here, as (kind, said),
        or None. A secret it needs that is not set: the unit is not run, and
        that is no one's result (``EnvironmentSecretMissingError``, a setup
        error to the runs pages). An environment the local backend cannot
        give - anything but the base image, unless it says ``local_ok`` -
        refused the same way, for what would run in it (not a control)."""
        if name is None:
            return None
        if self.backend == "local" and self.other["_kind"] not in ("control", "upstream") and not env_def.get("local_ok"):
            return ("EnvironmentSetupError", f"not run: the environment {name} needs the docker backend "
                                             "(--backend docker); it does not say local_ok")
        missing = definitions.missing_secrets(env_def, self.licences)
        if missing:
            return ("EnvironmentSecretMissingError", "not run: needs " + ", ".join(missing))
        return None

    def driver_secrets(self) -> dict:
        """The secrets the benchmark's environments send to its driver
        (``to: driver``) that are set: in its process's environment, never in
        a request, so never in a log."""
        out = {}
        for e in (self.bench.get("environments") or {}).values():
            out.update(definitions.secret_values(e, self.licences, "driver"))
        return out

    # -- the unit's relay, and where its process runs ------------------------
    def agent_licences(self, env_def: Optional[dict], rl) -> dict:
        """The variables the unit's environment gives the agent of its
        licences (``to: agent``): a key; a file's path, where it is mounted
        on docker (``rl.unit_binds``) or kept here; a server's address - on
        docker the one its forwarder answers at (``container.Unit``)."""
        inside = self.backend == "docker"
        out = definitions.secret_values(env_def, self.licences, "agent", inside=inside)
        reach = getattr(rl, "reach", None) or {}
        for s in (env_def or {}).get("needs_secrets") or []:
            if inside and s.get("to") == "agent" and s.get("kind") == "server" and s["name"] in out:
                out[s["name"]] = reach.get(out[s["name"]], out[s["name"]])
        return out

    def agent_files(self, env_def: Optional[dict]) -> List[tuple]:
        """(where it is kept, its path inside) for each licence file the
        environment mounts into the agent's container."""
        out = []
        for s in (env_def or {}).get("needs_secrets") or []:
            h = self.licences.get(s["name"]) if s.get("to") == "agent" and s.get("kind") == "file" else None
            if h and s.get("path"):
                out.append((h["path"], s["path"]))
        return out

    def agent_servers(self, env_def: Optional[dict]) -> List[str]:
        """The licence servers the environment's agent must reach: host:port each."""
        out = []
        for s in (env_def or {}).get("needs_secrets") or []:
            h = self.licences.get(s["name"]) if s.get("to") == "agent" and s.get("kind") == "server" else None
            if h:
                out.append(h["value"])
        return out

    def prices_record(self) -> Optional[dict]:
        """What the job's config keeps of its prices: the rows of the models
        it calls, each with its ``source`` (``list`` the public list, ``own``
        the user's own row, ``run`` this run's ``--price``), and where and
        when the list was fetched when a row is its. None when it has none."""
        rows = price_list.given(self.models_called(), self.prices)
        if not rows:
            return None
        doc, mine = price_list.load(), getattr(self, "run_prices", {})
        rows = {n: {**r, "source": "run" if n in mine else "own" if n in doc["own"] else "list"} for n, r in rows.items()}
        listed = doc["from"] and any(r["source"] == "list" for r in rows.values())
        return {**({"from": doc["from"], "on": doc["on"]} if listed else {}), "models": rows}

    def ledger_file(self) -> Optional[str]:
        """The file of the run's spending cap, for a relay; None without a cap."""
        led = getattr(self, "ledger", None)
        return led.path if led is not None else None

    def models_called(self) -> List[str]:
        """Every model this run's relays may send a call to, as sent: the
        run's, the judge's, the simulator's, an option's embeddings and the
        agent's side models. Any other name a caller asks for is replaced by
        the run's (``relay``), so these are all a run can be priced for."""
        return ledgers.models(self.other, *(getattr(self, n, None) for n in ("model", "judge_model", "simulator_model", "embed_model")),
                              control=self.other.get("_kind") == "control")

    def unit_relay(self, tag: str, trial: str, setup: dict, env_def: Optional[dict] = None, own: bool = False):
        """The unit's own relay, started: in this process on the local
        backend, a sidecar container on docker. Either answers ``url`` (the
        driver's way in, on this machine), ``calls`` and ``stop``;
        ``unit_url`` is the address the agent or capability is given. ``own``:
        the unit runs in its own image (:meth:`own_box`), not one of the
        benchmark's layers on the base."""
        if not self.base:
            return None                          # a control with no judge: no model is called
        record = os.path.join(trial, ".work", "relay")
        side = tuple((self.other.get("model") or {}).get("side_models") or ())
        key_header = PROVIDERS[self.provider].get("key_header", "authorization")
        if self.backend == "local":
            rl = relay.Relay(self.base, self.key, record, prices=getattr(self, "prices", None), model=self.model, side_models=side,
                             key_header=key_header, ledger=self.ledger_file()).start()
            rl.unit_url = rl.url(tag)
            return rl
        routes = {tag + "-judge": self.judge_model} if self.judge_model else {}
        if self.simulator_model:
            routes[tag + "-simulator"] = self.simulator_model
        doors, listeners = [], []
        if self.other["_kind"] == "agent" and mcp_machine.wanted(setup):
            # the benchmark's own servers run here, in the driver's environment; the agent reaches each
            # through a door of its own on the unit's network (mcp_machine), its entry the bridge
            work = os.path.join(trial, ".work")
            try:
                setup["mcp"], doors, listeners = mcp_machine.doors(self.bench, setup, install.driver_dir(self.bench),
                                                                   self.machine_env(trial), work, work)
            except mcp_machine.Unknown as e:
                raise _Ended("EnvironmentSetupError", f"the unit's setup: {e}")
        door = getattr(self, "loopback", None)
        rl = container.Unit(self.name, tag, record, image=self.image, app=APP, base=door.base if door else self.base, key=self.key,
                            model=self.model, routes=routes, side_models=side, key_header=key_header,
                            prices=price_list.given(self.models_called(), getattr(self, "prices", None)),   # a sidecar's argv: its models' rows
                            servers=self.agent_servers(env_def), doors=doors, hosts=door.hosts if door else None,
                            ledger=self.ledger_file())
        rl.mcp_listeners = listeners
        try:
            try:
                rl.unit_image = "" if own else self.unit_image(setup)
            except container.Unavailable as e:
                raise _Ended("EnvironmentSetupError", f"the unit's environment: {e}")
            rl.unit_rw, rl.unit_files = container.mcp_mounts(setup.get("mcp") or [], trial)
            rl.unit_binds = self.agent_files(env_def)
            rl.start()
        except BaseException:
            for ln in listeners:
                ln.stop()
            raise
        rl.unit_url = rl.inside(tag)
        return rl

    def machine_env(self, trial: str) -> dict:
        """The environment of a benchmark's MCP server run on this machine
        (``mcp_machine``): the driver's, less its secrets - the agent may see
        it on the local backend - with a home of its own in the trial."""
        home = os.path.join(trial, ".work", "mcp-home")
        os.makedirs(home, exist_ok=True)
        drv = install.driver_dir(self.bench)
        extra = install.process_env(definitions.driver_install(self.bench), drv, home) if drv else {}
        return base_env(home, {**extra, "NULLIUS_UPSTREAM": self.up or "", "NULLIUS_DATASET": self.data or "",
                               "NULLIUS_DATASET_ID": self.ds["id"], "PYTHONDONTWRITEBYTECODE": "1",
                               **option_env(getattr(self, "bench_options", {})), **self.embeddings_env()})

    def embeddings_env(self) -> dict:
        """Where the driver's (and its MCP servers') embedding calls go: the run's own relay, the
        model pinned on its route, as an OpenAI client reads it; nothing without an embedding option."""
        if getattr(self, "bench_relay", None) is None:
            return {}
        return {"OPENAI_BASE_URL": self.bench_relay.url(self.embeddings_tag(), model=self.embed_model), "OPENAI_API_KEY": relay.PLACEHOLDER_KEY}

    def embeddings_tag(self) -> str:
        """This thread's route on the benchmark's relay. A driver serves one thread, one unit at a time: what
        it was refused there is that unit's, never one running beside it."""
        return f"embeddings-{threading.get_ident()}"

    def unit_image(self, setup: dict) -> str:
        """The image a unit runs in (``container.image_for``); the first unit
        to ask for an environment says its build - or that it is there
        already - as a stage of its own."""
        work = os.path.join(self.job_dir, ".work")
        name, _ = definitions.environment_of(self.bench, setup, self.ds)
        with self._said:
            first = name is not None and name not in self._envs
            self._envs.add(name)
        if not first:
            return container.image_for(self.bench, setup, self.ds, work)
        tag = container.env_tag(self.bench, name, self.image)
        with self.progress.stage("environment", cached=container.present(tag), start=f"building {tag}", name=name):
            return container.image_for(self.bench, setup, self.ds, work)

    def place(self, rl, cmd: List[str], env: dict, workdir: str, home: str, interactive: bool = True, box=None):
        """A unit's command and environment as they are started: unchanged on
        the local backend; on docker, ``docker run`` on the unit's network,
        with the folders it reads at their own paths and the image's ``PATH``
        behind the install's. The secrets its environment sends the agent
        (``to: agent``) are added to its environment - on docker passed by
        name, never on an argument list. In an own image (``box``), ``docker
        exec`` in the unit's container, the image's own ``PATH`` first - the
        task's programs are the task's - then the install's and the runtimes'."""
        env = dict(env, **(getattr(rl, "unit_secrets", None) or {}))
        if box is not None:
            inside = {k: v for k, v in env.items() if k not in container.HOST_ONLY}
            path = os.pathsep.join([box.path] + install.bin_dirs(self.inst) + [d + "/bin" for _, d in box.runtimes.values()])
            argv = box.exec_argv(container.in_runtime(cmd, box.runtimes), inside, path, interactive=interactive)
            return argv, container.client_env({k: v for k, v in inside.items() if k != "PATH"})
        if self.backend == "local":
            return cmd, env
        ws = os.path.join(os.path.dirname(home), "workspace")
        # What the agent's container sees: its workspace and home, its own
        # install and definition, and the files its MCP servers need - never
        # this application's folder (its .env holds the keys) nor the
        # benchmark's (its data holds the answers).
        mounted = container.mounts([ws, home, *getattr(rl, "unit_rw", [])],
                                   [self.inst, self.other.get("_here"), *container.python_homes(self.inst)],
                                   getattr(rl, "unit_files", []))
        env = dict(env, PATH=os.pathsep.join(install.bin_dirs(self.inst) + [container.IMAGE_PATH]))
        return rl.wrap(list(cmd), env, workdir, mounted, image=rl.unit_image, interactive=interactive,
                       binds=getattr(rl, "unit_binds", []))

    # -- a control: nothing, or the benchmark's own reference -----------------
    def control_answer(self, unit, ws, box=None, name: str = "") -> str:
        """``nop`` answers "" and leaves the workspace as setup left it;
        ``oracle`` is the driver's ``oracle`` - its answer, and whatever it
        wrote into the workspace, or, in an own image (``box``), the step its
        reply names (``solve``: the reference solution copied in and run in
        the unit's container). Either goes to ``score`` as an agent's answer
        would; a capability benchmark's skips ``produce``."""
        if self.other["key"] != "oracle":
            return ""
        try:
            reply = self.driver().request("oracle", unit=unit, dir=ws, timeout=DRIVER_SECONDS)
        except jsonl.Failed as e:
            raise _Ended("NonZeroAgentExitCodeError", "the benchmark's oracle: " + str(e))
        if reply.get("solve") is not None:
            if box is None:
                raise _Ended("NonZeroAgentExitCodeError", "the benchmark's oracle: solve runs in an own image, and this unit has none")
            if name:                             # the reference solution, run under its own limit
                self.phase(name, unit, "agent_run", float(reply["solve"].get("seconds") or self.turn_seconds))
            done = self.in_box(box, reply["solve"], collect=False, what="the benchmark's oracle")
            os.makedirs(os.path.join(os.path.dirname(ws), "agent"), exist_ok=True)
            with open(os.path.join(os.path.dirname(ws), "agent", "solve.txt"), "w") as f:
                f.write(done["output"])
            if done["exit"] != 0:
                raise _Ended("NonZeroAgentExitCodeError", "the benchmark's oracle: its solution "
                             + ("did not end in time" if done["timed_out"] else f"ended with exit {done['exit']}"))
        return str(reply.get("answer") or "")

    # -- an own image: the unit's container ------------------------------------
    def own_box(self, tag: str, setup: dict, rl, home: str):
        """The unit's own container, started (``container.Box``): the image its
        setup names, built or pulled on first use and said as a stage; for an
        agent, its install, definition, home and runtimes mounted, and each
        runtime shown to start in the image - a unit where one cannot (a
        glibc runtime in a musl image) is not run, which is no one's result."""
        spec = setup.get("image")
        bad = container.spec_problems(spec)
        if setup.get("verify") is not None:
            bad += ["verify." + p for p in container.step_problems(setup["verify"], collect=True)]
        if setup.get("workdir") is not None and not (isinstance(setup["workdir"], str) and setup["workdir"].startswith("/")):
            bad.append("workdir: an absolute path inside the image")
        if bad:
            raise _Ended("EnvironmentSetupError", "the unit's setup: " + "; ".join(bad))
        why = container.unusable()
        if why:
            raise _Ended("EnvironmentSetupError", f"not run: its own image needs docker, and {why}")
        box = None
        try:
            image = self.own_image(spec)
            cfg = container.image_config(image)
            mounted, runtimes = [], {}
            if self.other["_kind"] == "agent":
                # as on the base: its home, its install and definition, its MCP servers' files -
                # never this application's folder nor the benchmark's; and what it runs on, under /opt/nullius
                mounted = container.mounts([home, *getattr(rl, "unit_rw", [])],
                                           [self.inst, self.other.get("_here"), *container.python_homes(self.inst)],
                                           getattr(rl, "unit_files", []))
                runtimes = self.runtimes
            box = container.Box(self.name, tag, image, workdir=setup.get("workdir") or cfg["workdir"], path=cfg["path"],
                                network=rl.names["network"] if isinstance(rl, container.Unit) else None,
                                mounted=mounted, runtimes=runtimes, memory_mb=spec.get("memory_mb"), cpus=spec.get("cpus"),
                                app=APP).start()
            for name in runtimes:
                why = box.runtime_problem(name)
                if why:
                    raise _Ended("EnvironmentSetupError", "not run: " + why)
            return box
        except (container.Unavailable, ValueError, OSError) as e:
            if box is not None:
                box.stop()
            raise _Ended("EnvironmentSetupError", f"the unit's own image: {e}")
        except _Ended:
            box.stop()
            raise

    @staticmethod
    def box_changes(box) -> Optional[str]:
        """What changed in the unit's container since its image - the work
        itself, where the workspace is the container's: the trial keeps it
        (``agent/container-changes.txt``)."""
        try:
            return box.diff()
        except container.Unavailable:
            return None

    def release_image(self, image: str) -> None:
        """A unit is done with its own image: with ``remove_images``, removed
        when no other unit is using it and this run fetched it
        (``container.drop_image``)."""
        with self._said:
            left = self._image_users.get(image, 1) - 1
            if left > 0:
                self._image_users[image] = left
                return
            self._image_users.pop(image, None)
        if not self.keep_images and container.fetched(image):
            container.drop_image(image)

    def own_image(self, spec: dict) -> str:
        """The image an own-image unit names, built or pulled the first time
        a unit asks for it (``container.own_image``) - said as a stage then, or
        that it is there already."""
        root = self.up or self.bench["_here"]
        tag = spec.get("pull") or container.own_tag(self.bench, spec["build"], container.within(root, spec["build"]))
        work = os.path.join(self.job_dir, ".work")
        with self._said:
            first = tag not in self._envs
            self._envs.add(tag)
            self._image_users[tag] = self._image_users.get(tag, 0) + 1
        if not first:
            return container.own_image(self.bench, spec, root, work)
        with self.progress.stage("environment", cached=container.present(tag),
                                 start=("pulling " if spec.get("pull") else "building ") + tag, name=tag):
            return container.own_image(self.bench, spec, root, work)

    def in_box(self, box, step: dict, *, collect: bool, what: str, out: Optional[str] = None) -> dict:
        """A step (``verify``, ``solve``) run in the unit's container. For the
        check (``collect``), what the agent left running is stopped first
        (``container.Box.settle``) - unless the step keeps it
        (``keep_processes``) - and what was stopped kept in ``out``
        (``stopped.txt``). Then its files are copied in from the upstream and the ones
        the driver gives by their text written in (``write``), a
        way out to public addresses given when it asks for the network
        (``container.Box.online``: a proxy, named to the command alone), and
        its command run in the working directory."""
        bad = container.step_problems(step, collect=collect)
        if bad:
            raise _Ended("VerifierError" if collect else "NonZeroAgentExitCodeError", f"{what}: " + "; ".join(bad))
        root = self.up or self.bench["_here"]
        try:
            if collect and not step.get("keep_processes"):
                stopped = box.settle()
                if stopped and out:
                    with open(os.path.join(out, "stopped.txt"), "w") as f:
                        f.write("".join(s + "\n" for s in stopped))
            for src, dest in (step.get("copy") or {}).items():
                box.copy_in(container.within(root, src), dest)
            for dest, text in (step.get("write") or {}).items():
                box.write_in(dest, text)
            env = box.online() if step.get("network") else None
            if step.get("collect"):
                box.run(["mkdir", "-p", step["collect"]], 60, user="0")
            return box.run(step["run"], float(step.get("seconds") or self.turn_seconds), env=env)
        except (container.Unavailable, ValueError) as e:
            raise _Ended("VerifierError" if collect else "NonZeroAgentExitCodeError", f"{what}: {e}")

    def verify(self, step: dict, box, trial: str) -> dict:
        """The benchmark's check, run in the unit's container once the agent
        has ended (``in_box``), what it collects copied out into the trial's
        ``verifier/`` and its output kept there (``test-stdout.txt``, and what
        its proxy let through and refused, ``egress.log``): what ``score`` is
        sent as ``verified``, the folder to read the rest from."""
        out = os.path.join(trial, "verifier")
        os.makedirs(out, exist_ok=True)
        try:
            done = self.in_box(box, step, collect=True, what="the benchmark's check", out=out)
        finally:
            said = box.egress_said()
            if said:
                with open(os.path.join(out, "egress.log"), "w") as f:
                    f.write(said)
        with open(os.path.join(out, "test-stdout.txt"), "w") as f:
            f.write(done["output"])
        collected = box.copy_out(step["collect"], out) if step.get("collect") else False
        return {"exit": done["exit"], "timed_out": done["timed_out"], "output": done["output"][-4000:],
                "dir": out, "collected": collected}

    # -- an agent, over ACP ----------------------------------------------------
    def simulator(self, rl, tag: str) -> dict:
        """What a ``turn`` request carries when a model plays the benchmark's
        user: that model's route on the unit's relay, recorded beside the
        unit's calls (``<tag>-simulator``) and never counted as the agent's."""
        if not self.simulator_model:
            return {}
        return {"simulator": {"model": self.simulator_model, "key": relay.PLACEHOLDER_KEY,
                              "base_url": rl.url(tag + "-simulator", model=self.simulator_model)}}

    def judge(self, rl, tag: str) -> dict:
        """What a ``score`` request carries when the benchmark is graded by a
        model - and every ``turn`` too, for a driver whose upstream grades the
        conversation as it ends: that model's route on the unit's relay."""
        if not self.judge_model:
            return {}
        return {"judge": {"model": self.judge_model, "base_url": rl.url(tag + "-judge", model=self.judge_model),
                          "key": relay.PLACEHOLDER_KEY}}

    def turn_limit(self, setup: dict) -> float:
        """How long one turn of the agent may take: the unit's own limit when its ``setup`` names one
        (``agent_seconds``: a task that declares its agent timeout, as Terminal-Bench's task.toml does),
        else the run's ``turn_seconds``."""
        own = setup.get("agent_seconds") if isinstance(setup, dict) else None
        if isinstance(own, (int, float)) and not isinstance(own, bool) and 0 < own <= MAX_AGENT_SECONDS:
            return float(own)
        return self.turn_seconds

    def drive_agent(self, unit, ws, home, tag, setup, transcript, rl, box=None, name=""):
        """The agent over ACP, its session's ``cwd`` the unit's workspace - in
        an own image (``box``), the container's working directory, which this
        machine does not see. Files are served (``acp.Client(fs=)``) only to an
        agent running here: one in a container has its workspace mounted, and
        serving it from this machine would let it swap a folder for a link
        between the check and the open and reach any file of the user's."""
        a = self.other
        env = agent_env(a, self.inst, home, rl.unit_url, self.model, self.options)
        log = os.path.join(os.path.dirname(ws), "agent", "agent.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        if "command" in a["start"]:
            return self._command_agent(a, env, ws, home, setup, transcript, log, tag, unit, rl, box)
        cmd = install.words(a["start"]["acp"], {"here": a["_here"], "install": self.inst})
        if self.backend == "local":              # inside a container, its own PATH finds it
            cmd[0] = shutil.which(cmd[0], path=env["PATH"]) or cmd[0]
        cmd, env = self.place(rl, cmd, env, ws, home, box=box)
        c = acp.Client(cmd, env, log, cwd=ws, grant=True, fs=box is None and self.backend == "local", stopped=self._stopped)
        events: List[dict] = []
        try:
            try:
                try:
                    c.start()
                    sid = c.new_session(box.workdir if box is not None else ws, setup.get("mcp") or [])
                except TimeoutError as e:
                    # the agent never answered its start: this unit's setup failed, not the run
                    raise _Ended("AgentSetupTimeoutError", f"the agent did not answer {e} in time ({c.tail()})")
                prompt = setup["prompt"]
                answer = ""
                # a conversation's turns carry its simulated user's route and its judge's (each when it has one)
                sim = dict(self.simulator(rl, tag), **self.judge(rl, tag)) if "agent.conversation/v1" in self.bench["needs"] else {}
                opened = True
                if self.simulator_model:
                    # The simulated user speaks first: the first turn, asked with nothing said yet,
                    # follows setup's prompt (what the agent is told before anyone speaks).
                    first = self.driver().request("turn", unit=unit, dir=ws, transcript=[], timeout=600, **sim, **self.attempt_kw())
                    opened = not first.get("done")
                    if opened:
                        prompt = prompt + "\n\n" + first["message"]
                for n in range(self.max_turns if opened else 0):
                    transcript.append({"role": "user", "text": prompt})
                    if n:                        # the limit is each turn's: the board's clock starts again with it
                        self.phase(name, unit, "agent_run", self.turn_limit(setup))
                    turn = c.prompt(sid, prompt, timeout=self.turn_limit(setup))
                    answer = turn.text
                    transcript.append({"role": "agent", "text": turn.text, "stop": turn.stop})
                    if "agent.conversation/v1" not in self.bench["needs"]:
                        break
                    nxt = self.driver().request("turn", unit=unit, dir=ws, transcript=transcript, timeout=600, **sim, **self.attempt_kw())
                    if nxt.get("done"):
                        break
                    prompt = nxt["message"]
                    if nxt.get("restart"):
                        # the upstream started its conversation again (its own retry of an attempt that
                        # raised): a new session, told what it was told before anyone spoke
                        sid = c.new_session(box.workdir if box is not None else ws, setup.get("mcp") or [])
                        prompt = setup["prompt"] + "\n\n" + prompt
                events = c.events
            except acp.Timeout as e:
                events = c.events
                raise _Ended("AgentTimeoutError", str(e), job.trajectory_from_events(events, rl.calls(tag), a, self.model, f"{self.name}/{unit}"))
            except acp.AgentFailed as e:
                events = c.events
                raise _Ended("NonZeroAgentExitCodeError", str(e), job.trajectory_from_events(events, rl.calls(tag), a, self.model, f"{self.name}/{unit}"))
            except TimeoutError as e:
                events = c.events               # a later request the agent never answered (a new session)
                raise _Ended("AgentTimeoutError", f"the agent did not answer {e} in time",
                             job.trajectory_from_events(events, rl.calls(tag), a, self.model, f"{self.name}/{unit}"))
            except jsonl.Failed as e:
                raise _Ended("VerifierError", "the benchmark's next turn: " + str(e))
        finally:
            c.close()
        raw = "".join(json.dumps(e) + "\n" for e in events)
        return answer, job.trajectory_from_events(events, rl.calls(tag), a, self.model, f"{self.name}/{unit}"), {"acp.jsonl": raw}

    def _command_agent(self, a, env, ws, home, setup, transcript, log, tag, unit, rl, box=None):
        cmd = install.words(a["start"]["command"], {"here": a["_here"], "install": self.inst, "prompt": setup["prompt"]})
        cmd, env = self.place(rl, cmd, env, ws, home, interactive=False, box=box)
        transcript.append({"role": "user", "text": setup["prompt"]})
        t = time.time()
        try:
            with open(log, "a") as err:
                r = run_group(cmd, self.turn_limit(setup), stopped=self._stopped, cwd=ws, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=err, text=True)
        except subprocess.TimeoutExpired:
            raise _Ended("AgentTimeoutError", f"the agent did not end in {self.turn_limit(setup)} s")
        if r.returncode != 0:
            raise _Ended("NonZeroAgentExitCodeError", f"exit {r.returncode}")
        transcript.append({"role": "agent", "text": r.stdout})
        events = [{"t": t, "update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": setup["prompt"]}}},
                  {"t": time.time(), "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": r.stdout}}}]
        return r.stdout.strip(), job.trajectory_from_events(events, rl.calls(tag), a, self.model, f"{self.name}/{unit}"), {}

    # -- a capability, through its port -------------------------------------------
    def drive_feature(self, unit, ws, home, tag, setup, transcript, rl):
        f = self.other
        cmd = install.words(f["start"]["command"], {"here": f["_here"], "install": self.inst})
        env = agent_env(f, self.inst, home, rl.unit_url, self.model, self.options)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cmd, env = self.place(rl, cmd, env, f["_here"], home)
        cwd = os.path.join(os.path.dirname(ws), ".work", "capability")     # the unit's, never the catalogue folder
        os.makedirs(cwd, exist_ok=True)
        proc = jsonl.Proc(cmd, cwd, env, os.path.join(os.path.dirname(ws), "agent", "capability.log"), stopped=self._stopped)
        os.makedirs(os.path.join(os.path.dirname(ws), "agent"), exist_ok=True)
        exchanges: List[dict] = []

        def send(op, **kw):
            t0 = time.time()
            reply = proc.request(op, timeout=self.turn_seconds, **kw)
            exchanges.append({"op": op, "request": kw, "reply": reply, "t0": t0, "t1": time.time()})
            return reply

        try:
            try:
                proc.start()
                plan = setup.get("memory") or {}
                for s in plan.get("add") or []:
                    send("add", session=s)
                recalled = [{"id": q.get("id"), "memory": send("recall", question=q["question"]).get("memory", "")}
                            for q in plan.get("recall") or []]
            except jsonl.Failed as e:
                raise _Ended("NonZeroAgentExitCodeError", "the capability: " + str(e),
                             job.trajectory_from_exchanges(exchanges, [], f, self.model, f"{self.name}/{unit}"))
        finally:
            proc.close()
        try:
            out = self.driver().request("produce", unit=unit, dir=ws, recalled=recalled, model=self.model,
                                        base_url=rl.url(tag), key=relay.PLACEHOLDER_KEY, timeout=self.turn_seconds)
        except jsonl.Failed as e:
            raise _Ended("VerifierError", "the benchmark's reader: " + str(e),
                         job.trajectory_from_exchanges(exchanges, rl.calls(tag), f, self.model, f"{self.name}/{unit}"))
        transcript.append({"role": "capability", "recalled": recalled})
        return out.get("answer"), job.trajectory_from_exchanges(exchanges, rl.calls(tag), f, self.model, f"{self.name}/{unit}")


    def drive_upstream(self, unit, ws, tag, transcript, rl):
        """The setup's unit: the driver runs the upstream's own answerer on it
        (``answer``), asked as ``asked`` through the unit's relay, which sends
        the run's model in its place. A reply with no text - a reasoning model
        that spent the upstream's token budget thinking - is said as such."""
        o = self.other
        try:
            # the benchmark's other models reach the upstream's pipeline as they reach a turn or a score: their own routes
            out = self.driver().request("answer", unit=unit, dir=ws, setup=o["key"], model=self.asked, **self.attempt_kw(),
                                        **self.simulator(rl, tag), **self.judge(rl, tag),
                                        base_url=rl.url(tag), key=relay.PLACEHOLDER_KEY, timeout=self.turn_seconds)
        except jsonl.Failed as e:
            calls = rl.calls(tag)
            silent = [c for c in calls if c.get("status") == 200 and not ((c.get("reply") or {}).get("text") or "").strip()]
            if silent:
                u = (silent[-1].get("reply") or {}).get("usage") or {}
                thought = u.get("reasoning") or 0
                raise _Ended("OutputLengthExceededError",
                             f"{self.model} returned no text" + (f" ({thought} of its {u.get('output')} tokens were reasoning)" if thought else "")
                             + f": {self.bench['label']}'s code asks for a short answer, and a reasoning model can spend it all before it answers"
                             + (" - the original's models do not reason" if o["key"] == "model" else ""),
                             job.trajectory_from_exchanges([], calls, o, self.model, f"{self.name}/{unit}"))
            raise _Ended("NonZeroAgentExitCodeError", f"{o['label']}: " + str(e),
                         job.trajectory_from_exchanges([], calls, o, self.model, f"{self.name}/{unit}"))
        transcript.append({"role": "upstream", "setup": o["key"], "asked": self.asked, "sent": self.model})
        return out.get("answer"), job.trajectory_from_exchanges([], rl.calls(tag), o, self.model, f"{self.name}/{unit}")


class _Ended(Exception):
    def __init__(self, kind, said, trajectory=None):
        super().__init__(said)
        self.kind, self.said, self.trajectory = kind, said, trajectory


def run(bench: dict, other: dict, **kw) -> str:
    return Run(bench, other, **kw).start()


def run_and_report(bench: dict, other: dict, **kw) -> tuple:
    """(job directory, whether it failed): it stopped on a refusing endpoint,
    or units ran and none reached a verdict - what the CLI's exit status says.
    A run its spending cap stopped ended as it was asked to: no failure,
    whatever it reached."""
    r = Run(bench, other, **kw)
    job_dir = r.start()
    return job_dir, bool(r.halted) or (r.n_ended > 0 and r.n_verdicts == 0 and not r.capped)
