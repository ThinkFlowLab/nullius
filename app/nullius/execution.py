"""Running a recipe: on this machine, or on a host from Settings, with the
job mirrored back as it goes - and surviving Nullius going away.

One ``Plan`` is what the builder posts: the run commands compiled for a
runner (they name ``$NULLIUS_HARBOR``, ``$NULLIUS_TASKS`` and
``$NULLIUS_JOBS`` and nothing else about where they run), one part per
agent in the order they run (controls first), the Harbor the recipe pins
(``harbor_pin``, ``harbor/source.py``), the task folder they read, and the
target. ``$NULLIUS_HARBOR`` is a small ``harbor`` script in the run
directory that runs the pinned Harbor through ``uvx``: a release from PyPI,
or a repository's commit built on this machine, whose wheels a host is sent
(only this machine needs access to a private fork; the host resolves the
rest from PyPI). A plan from before the pin names none, and its
``$NULLIUS_HARBOR`` is ``harbor``, whichever is first on the target's PATH.
The job records which Harbor ran. An ``Execution`` carries the plan out in a
thread of its own and writes what it does to a log the page follows:

- on a host: ssh in; docker checked for, with ``uv`` for a pinned Harbor
  (installed when missing) or ``harbor`` on the host's PATH for an unpinned
  one (``~/.local/bin`` included); the task folder sent over; each agent's
  script started **detached** there (``setsid nohup``, or Python session creation when
  ``setsid`` is absent; its claim, pid and
  exit code on disk beside its log: ``run-<part>.sh``, ``.launch``, ``.pid``,
  ``.log``, ``.exit``, and ``.stop`` once a Stop has been said to it), so a
  dropped connection or a stopped Nullius does not end it; the job
  directory synced back with rsync every few seconds while it runs and once
  more at the end, so each trial appears here as it finishes;
- on this machine: the same, with the local paths, in ``runs/<name>``; a
  run that needs docker (Harbor's, or the standard's on its container backend)
  first asks that it answer here (``docker_here``) and starts nothing when
  it does not.

A plan for the Nullius standard (``runner`` ``standard``,
``standard/recipe.py``) runs this repository's own ``cli`` from
``$NULLIUS_STANDARD``: this repository here; from an installed wheel or on
a host, a content-addressed copy of the backend and effective catalogue.
Only code, definitions and their declared resources travel, never user
configuration or credentials. Snapshots live in ``standard-app/<content-id>/``
beside the runs and are sent to ``~/.nullius/app/<content-id>`` the way a task
folder is. There it needs ``python3`` and ``git``, gets ``uv`` when missing
(an agent's pip install builds with it), and docker when the plan says so
(``docker``: a recipe on the container backend) - checked, like a Harbor run's,
before anything is sent. A run split over several machines is one plan per
machine, each one shard (``standard/shard.py``).

The target reads an explicit catalogue: the user's catalogue for a local
run; on a host, a writable copy of the snapshot in the run's ``catalogue/``.
Proofs update that copy, never the shared runner. Both ``jobs/`` and
``catalogue/`` are mirrored back beside the run record; remote proofs do
not replace the local user's active definitions.

An agent's run that fails with more to come pauses the run and the page
asks: continue or stop. Every state change lands in ``runs/<name>/run.json``;
``restore`` reads those records when Nullius starts and reattaches to what
is still running, waits again where it was paused, and settles what ended
while nobody was looking. A record is checked against the part's files and
processes on the target before it is trusted (``_settle``): a part whose pid
was never saved, but which was claimed or started, is followed by the pid it
wrote itself or by its processes - never started a second time; a record
from before a part's end was one save, naming the next part with the ended
one's pid, goes on from that end; and one saved as the next part was
launched, still holding the ended one's pid, follows the next part by its
own processes.

A pid is a hint, never proof: a part's processes are the ones running its
script in its run directory (``_part_pids``). A part is alive while it has
one, gone without an exit code when it has none, and nothing else is
signalled - not a pid a record kept after its part ended, nor one the system
has since given to another process. A Stop ends the part it raced, whether
or not its launcher had answered (``_kill_part``).

The model key and endpoint follow the recipe's provider (``Plan.key_var``,
``url_var``, ``base_url``): on OpenRouter the Settings' OPENROUTER_API_KEY
and OpenRouter's own endpoint; on OpenAI, OPENAI_API_KEY and no endpoint, so
the clients use OpenAI's; on an OpenAI-compatible endpoint, its URL and its
key (MODEL_API_URL and MODEL_API_KEY, or a named endpoint's own) - the key
optional: without it the run is sent
``settings.MODEL_KEY_PLACEHOLDER``, for a server that checks no key - and a
run without that endpoint fails before it starts.
They are read on this machine and handed to the run as environment
(API_TOKEN, OPENAI_API_KEY, LLM_API_KEY; API_URL, OPENAI_BASE_URL,
LLM_BASE_URL) - over stdin to the launcher, never in argv (the key to a
docker relay as a file); a stdin cut short starts nothing and fails the
run at that part (``_launch``), and one that holds a line break is never sent
(``_line_break``) - and they are the only credentials it has: its commands get this
process's environment less every secret in Settings, whichever provider it
belongs to (``withheld``); a host gets nothing of it but stdin.

A standard run on a host is also sent its benchmark's licences
(``Plan.licences``: the benchmark and the names its environments declare,
read from its store, ``nullius.secrets``, or a data provider's token from that
provider's, when each part is launched) as one
more stdin line, ``NULLIUS_LICENCES``: in the launcher's environment, never
on disk there - but for a licence file, which the run writes to
``/dev/shm/nullius-<run>/`` (0600, in memory) and the part's detached shell
removes when it ends, a Stop included. On this machine the run reads the
store itself, and nothing is sent. A record from before ``base_url`` loads with none, and its run
goes on with the settings it named. Log lines pass through
``redact`` before they are kept. Every command runner is a call-time seam,
so the suite runs a plan with none of ssh, uv, rsync or docker present.

What the run is doing is said in stages (``nullius/progress.py``): the steps
here that get a host ready - connect, uv, the tasks, app/, a pinned Harbor -
each a ``[nullius] stage=...`` line in the run's log, and the run's own (the
standard's cli says its image, installs, dataset and units) read from the
part's log as it comes. ``events`` holds them all, read back from the log on
a restart, and ``summary``'s ``stages`` is them folded for the page.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from nullius import hosts, paths, prices as price_list, progress, redact, secrets, settings
from nullius.harbor import source as harbor_source
from nullius.standard import container, payload as standard_payload

REMOTE_HOME = "~/.nullius"                    # on a host: tasks/, runs/, app/, harbor/ (a pinned Harbor's wheels)
# The names the launcher gives a run's key and its endpoint (_launch), read
# from stdin: the only credentials a run's commands get.
KEY_NAMES = ("API_TOKEN", "OPENAI_API_KEY", "LLM_API_KEY")
URL_NAMES = ("API_URL", "OPENAI_BASE_URL", "LLM_BASE_URL")
# The line the launcher's stdin ends with, after the key and the endpoint:
# either may be empty, so only this line says the two arrived whole.
STDIN_END = "@end"
SYNC_EVERY = 10.0                             # seconds between looks at a running part (mirror, log, exit)
STOP_POLL = 0.5                               # how often a running command looks for a Stop
STOP_WAIT = 20.0                              # seconds a Stop's own commands on the target take at most: the kill and the last copy back, together
LAUNCH_GRACE = 5.0                            # seconds a restart waits for a part a launcher claimed to start (_settle)
UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"
RECORD = "run.json"
STANDARD = "standard"                         # the Nullius standard's runner key (standard/recipe.py HARNESS)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # this repository: app/ is in it
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_ID = re.compile(r"[^A-Za-z0-9_.-]")
ALIVE = ("preparing", "running", "paused", "stopping")
NOT_RUN = "not run"


def runs_root(env=os.environ) -> str:
    return env.get("RUNS_DIR") or paths.runs_dir(env)


def app_root(runs: str) -> str:
    """Content-addressed runner snapshots, in ``standard-app/`` beside runs."""
    return os.path.join(os.path.dirname(os.path.abspath(runs)), "standard-app")


def docker_here(sh: Callable, needs: str = "a Harbor run needs it", program: str = "docker") -> Optional[str]:
    """Why a run cannot use ``program`` on this machine, or None when it can
    (``needs`` says why the run asks): ``docker`` for Harbor, the standard's
    engine (``container.DOCKER``) for its container backend. The command on the
    PATH *and* ``<program> ps`` answered, as a host's probe asks
    (``hosts.PROBE``). The command alone is not enough - Docker Desktop's WSL
    integration leaves ``/usr/bin/docker`` in place while its engine is quit.
    ``sh`` is the run's own (returncode, output) runner; this is looked up at
    call time, so the suite can stand in for docker."""
    rc, out = sh(f"command -v {program} >/dev/null 2>&1 || {{ echo @missing; exit 0; }}; {program} ps 2>&1 >/dev/null || echo @down", 60)
    words = out.split()
    if rc != 0 or "@missing" in words:
        return f"{program} is not on this machine; " + needs
    if "@down" in words:
        said = [l for l in out.strip().splitlines() if l.strip() and l.strip() != "@down"]
        return (f"{program} is on this machine but does not answer ({program} ps failed)"
                + (" - is Docker Desktop or the docker daemon running?" if program == "docker" else "")
                + (": " + said[-1].strip()[-300:] if said else ""))
    return None


def _tasks_place(tasks: str, run: str) -> tuple:
    """Where a run's task folder goes on a host, under ~/.nullius: by the
    folder, not the run, so two runs over the same tasks record the same
    dataset and match on the compare page - the folder's name last, under a
    short digest of its whole path, so /a/tasks and /b/tasks do not share
    one copy that ``rsync --delete`` swaps under a run still reading it."""
    src = os.path.realpath(os.path.expanduser(tasks))
    name = _ID.sub("_", os.path.basename(os.path.normpath(src))) or run
    return ("tasks", hashlib.sha256(src.encode()).hexdigest()[:10], name)


def _rq(path: str) -> str:
    """A remote path quoted for the host's shell: ``~`` becomes ``$HOME`` so it
    still expands inside the quotes. The names inside are ours (validated)."""
    return '"' + (("$HOME" + path[1:]) if path.startswith("~") else path).replace('"', '\\"') + '"'


def name_taken(name: str, where: str = "") -> str:
    """What a new run is told when a run directory of its name is there
    already - here (``web/execute.py``, before it starts) or on a host
    (``where``, asked by the run before it sends anything)."""
    return f"a run named {name} exists already{' on ' + where if where else ''}; press Run again for a new name"


def _base(part: dict) -> str:
    """The stem of a part's files in the run directory: ``run-<part>``."""
    return f"run-{_ID.sub('_', part['id'])}"


def _script_rx(base: str) -> str:
    """An extended regex for a command line that runs ``<base>.sh``: the word
    itself, alone or at the end of a path. Its first letter is bracketed so
    the shell that asks, whose own command line holds the regex, never
    matches it."""
    return "(^|[ /])[" + base[0] + "]" + base[1:].replace(".", r"\.") + r"\.sh( |$)"


# Where a target shows its processes. A target without it (no /proc: not
# Linux) cannot prove a pid is still the part's, and falls back to asking
# whether the pid lives at all - what every target was asked before.
PROC = "/proc"


def _part_pids(base: str, pids=()) -> str:
    """Shell, run in the run directory, that prints the part's processes, one
    pid per line: those whose working directory is the run directory and
    whose command line runs ``<base>.sh`` - the detached shell, the script's
    own bash, or a launcher on its way to them. Asked of ``pids`` (what the
    record holds), of the pid the part wrote, and of whatever ``pgrep`` finds
    by that command line, so a part is found with no pid saved. A pid the
    part held, since given to another process, is never printed, so nothing
    that is not the part's is ever signalled - where the target has ``PROC``
    to check it by; where it has none, a pid that lives is taken as the
    part's, as before these checks."""
    rx = _script_rx(base)
    return (f"for p in {' '.join(str(int(p)) for p in pids if p)} $(head -c 32 {base}.pid 2>/dev/null | tr -cd 0-9) "
            f"$(pgrep -f '{rx}' 2>/dev/null); do [ \"$p\" != $$ ] || continue; "
            f"if [ -d {PROC}/$$ ]; then [ {PROC}/$p/cwd -ef . ] && tr '\\0' ' ' <{PROC}/$p/cmdline 2>/dev/null | grep -Eq '{rx}' && echo $p; "
            f"else kill -0 $p 2>/dev/null && echo $p; fi; "
            f"done | sort -un")


@dataclass
class Plan:
    name: str                                 # the run's name: a directory here and on the host
    script: str                               # the whole run, as one script (kept for the record)
    host: Optional[hosts.Host] = None         # None: this machine
    tasks: str = ""                           # a local task folder the recipe reads, or ""
    key_var: str = "MODEL_API_KEY"            # the setting the key is read from, on this machine
    url_var: str = "MODEL_API_URL"            # same for the endpoint, which a run on it cannot go without; "" for none
    base_url: str = ""                        # the endpoint when url_var names none: a provider's own (OpenRouter's), or "" for its clients' default
    model: str = ""
    jobs: List[dict] = field(default_factory=list)   # the job directories the script writes: [{"name", "agent", "dataset"}]
    parts: List[dict] = field(default_factory=list)  # the run, one agent at a time: [{"id", "label", "script"}]; empty: the whole script at once
    recipe: dict = field(default_factory=dict)       # the builder's recipe the script was compiled from, so the run can be opened again as one; {} for a plan from before
    runner: str = "harbor"                    # or "standard" (standard/recipe.py): the Nullius standard's own cli
    harbor_pin: dict = field(default_factory=dict)   # the Harbor the recipe pins (harbor/source.py pin_of); {} for a plan from before: the harbor on PATH
    docker: bool = False                      # a runner that needs docker on its target though it is not Harbor (the standard's container backend)
    keyless: bool = False                     # nothing in it calls a model (the standard's controls): handed no key and no endpoint at all
    licences: dict = field(default_factory=dict)     # the standard's on a host: {"benchmark", "names", "providers"} - what it is sent of that benchmark's licences

    def steps(self) -> List[dict]:
        return [p for p in self.parts if p.get("script")] or [{"id": "run", "label": "the run", "script": self.script}]

    def check(self) -> Optional[str]:
        if not _NAME.match(self.name):
            return "the run needs a name: letters, digits, dots, dashes"
        if not self.script.strip() and not any(p.get("script") for p in self.parts):
            return "nothing to run"
        if self.tasks and not os.path.isdir(os.path.expanduser(self.tasks)):
            return f"the task folder {self.tasks} is not a directory here"
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["host"] = self.host.entry if self.host else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        """A recorded plan; a field this version no longer has (a record
        from before, naming an installed Harbor release) is left out."""
        d = dict(d)
        d["host"] = hosts.parse(d["host"]) if d.get("host") else None
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def subprocess_run(argv: List[str], timeout: float = 600.0, stdin: Optional[str] = None,
                   stop: Optional[threading.Event] = None) -> tuple:
    """(returncode, combined output); a timeout is a return code of its own,
    and so is a stop: with ``stop`` given, the child is killed the moment the
    event is set (130, "stopped") rather than when its timeout runs out -
    an ssh that hangs for 120 s, an rsync for 900 s, would otherwise hold
    a Stop for that long."""
    try:
        # read as UTF-8 whatever the locale, a byte that is not one as U+FFFD: a run is never failed by how
        # its target spelt what it printed
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=_clean_env())
    except OSError as e:
        return 127, str(e)
    deadline = time.monotonic() + timeout
    got = {}

    def talk():
        # one communicate, whole: a retried one after a timeout stopped sending
        # input past the pipe's buffer (a 465 KB price list never landed)
        try:
            got["out"] = proc.communicate(input=stdin if stdin is not None else "")[0]
        except (OSError, ValueError) as e:
            got["out"] = str(e)
    talker = threading.Thread(target=talk, daemon=True)
    talker.start()
    while True:
        talker.join(STOP_POLL if stop is not None else max(0.0, deadline - time.monotonic()))
        if not talker.is_alive():
            return proc.returncode, got.get("out") or ""
        if stop is not None and stop.is_set():
            proc.kill(); talker.join()
            return 130, "stopped"
        if time.monotonic() >= deadline:
            proc.kill(); talker.join()
            return 124, f"no answer within {int(timeout)} s"


# A part's log, as a look at the target prints it: between these, then its size in bytes (_log_since, _logged).
LOG_FROM, LOG_TO = "__log__", "__logged__"


def withheld() -> List[str]:
    """The variables a run's commands never take from this process: every
    secret in Settings (the model keys and endpoints, the judge's key), the
    names other tools give them or this application gave them before
    (``settings.INSTEAD``, those that stand for a secret), the launcher's own
    names for the run's key and endpoint, the agent keys (a Harbor run is
    sent them by its launcher, as the key), the names a standard benchmark's
    own licences go by - kept in its own store (``nullius.secrets``), although
    imported saved settings may still hold a value until it is moved in
    Custom keys - and GitHub tokens from saved settings or the
    shell Nullius was started in. Every named endpoint's settings too, so a
    run gets its own endpoint's key alone. A data provider's token name is not one."""
    secret = {v.name for v in settings.VARIABLES if v.secret or v.kind == "endpoint"} | set(settings.endpoint_names())
    return sorted(secret | {old for old, new in settings.INSTEAD.items() if new in secret}
                  | set(KEY_NAMES + URL_NAMES) | {"GITHUB_TOKEN", "GH_TOKEN"} | set(settings.agent_key_names())
                  | set(_licence_names()))


def _licence_names() -> List[str]:
    """Every name a standard benchmark's environments give a licence of its
    own; a data provider's token (``provider``) is none, so a shell's reaches
    a Harbor run. A standard driver or agent never inherits it (``run.KEEP``)."""
    try:
        from nullius.standard import definitions
        return sorted({s["name"] for b in definitions.load("benchmark", every=True)[0] for s in definitions.secrets_of(b)
                       if not s.get("provider")})
    except Exception:
        return []


def _clean_env() -> Dict[str, str]:
    """The environment a run's commands get: this process's, less what
    ``withheld`` names. ``settings.load`` adds saved settings to this process's
    environment without replacing explicit environment values. A run gets
    its provider's key and endpoint from the launcher alone (``_launch``,
    over stdin); ``--from-run`` prevents the CLI reading saved settings again."""
    gone = set(withheld())
    return {k: v for k, v in os.environ.items() if k not in gone}


class Execution:
    """One run of one plan, in its own thread. ``state`` is one of preparing,
    running, paused, stopping, done, failed, stopped; ``log`` is what
    happened, redacted."""

    def __init__(self, plan: Plan, root: Optional[str] = None, run: Optional[Callable] = None,
                 key: Optional[str] = None, url: Optional[str] = None, env=os.environ, record: Optional[dict] = None,
                 agent_keys: Optional[Dict[str, str]] = None, licences: Optional[Dict[str, bytes]] = None):
        self.plan = plan
        self.root = root or runs_root(env)
        self._catalogue = paths.catalogue_dir(env)
        self.dir = os.path.join(self.root, plan.name)
        self.jobs = os.path.join(self.dir, "jobs")
        self.log_path = os.path.join(self.dir, "nullius.log")
        self.pins = harbor_source.root(self.root, env)   # where a pinned Harbor is built and kept here
        self._stop = threading.Event()
        # Every command the run makes gives way to a Stop; the kill that a
        # Stop itself sends goes through the plain runner, or it would be
        # the first command cut short.
        self._run_plain = run or subprocess_run
        self._run = run or (lambda argv, timeout=600.0, stdin=None: subprocess_run(argv, timeout, stdin, stop=self._stop))
        # an endpoint of the run's own (url_var: an OpenAI-compatible one) may check no key: it is sent the placeholder
        self._key = (key if key is not None else env.get(plan.key_var, "")) or (settings.MODEL_KEY_PLACEHOLDER if plan.url_var else "")
        self._url = url if url is not None else (env.get(plan.url_var, "") if plan.url_var else plan.base_url)
        if plan.keyless:                          # a run that calls no model is given nothing it could leak
            self._key, self._url = "", ""
        # the agent keys Settings holds (CURSOR_API_KEY, ...): a Harbor run's agent may need its own vendor's; the standard's never gets one
        self._agent_keys = dict(agent_keys) if agent_keys is not None else (settings.agent_key_values(env) if plan.runner == "harbor" else {})
        # the benchmark's licences a standard run on a host is sent: read from its store at each launch, unless given
        self._licences_given = licences
        self._redact: List[str] = []              # the licences' values sent, as text: never in a log line
        self._decision = threading.Event()        # set by continue_() or stop() while paused
        # A Stop (the page's thread) and the run's own thread pausing or ending, each with the record
        # it saves, one at a time: a Stop is never written over, a record never half of each.
        self._turn = threading.RLock()
        self._lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self._lines: List[str] = []
        #: what a part is downloading, while it does: {label, done, total} in bytes, from its `@progress` lines
        self.progress: Optional[dict] = None
        #: every stage line the log holds, parsed (nullius/progress.py): this run's own and its part's
        self._events: List[dict] = []
        self._unread = False                      # a restored run that ended: its log is read when first asked for
        self._open: Optional[tuple] = None        # (stage, monotonic start) of the step here under way, said as a stage
        rec = record or {}
        self.id = rec.get("id") or f"{plan.name}-{int(time.time())}"
        self.state = rec.get("state", "preparing")
        self.step = rec.get("step", "")
        self.part = int(rec.get("part", 0))       # index of the part running or next
        self.pid: Optional[int] = rec.get("pid")
        # the part that may be running on the target, from its launch until its end is recorded: what a Stop ends
        self._live: Optional[dict] = None
        # the last part whose launch began, saved before its script is sent, so a restart tells a Stop that cut its
        # launch short from one before it (_kill_part); "" in a record from before
        self.launched: str = rec.get("launched") or ""
        if self.state == "stopping":               # a Stop that a restart interrupted: finish it, do not run on
            self._stop.set()
        self.log_offset = int(rec.get("log_offset", 0))
        self.exit_code: Optional[int] = rec.get("exit_code")
        self.error: Optional[str] = rec.get("error")
        self.question: Optional[str] = rec.get("question")
        #: each part that ran to its end: {part id: {"rc", "at", "label"}} - an agent's own result in a run of several
        self.ends: Dict[str, dict] = dict(rec.get("ends") or {})
        self.started = rec.get("started") or time.time()
        self.finished: Optional[float] = rec.get("finished")
        self.harbor = rec.get("harbor", "")       # the harbor executable on the target
        self.tasks_on_target = rec.get("tasks_on_target", "")
        self.app_on_target = rec.get("app_on_target", "")   # the standard's source tree or runner snapshot on the target
        self.catalogue_on_target = rec.get("catalogue_on_target", "")   # writable definitions, separate from the immutable runner
        # the trials the log has said already, (job, trial), so a restart says each once; none in a record from before
        self._seen_trials: set = {(k[0], k[1]) for k in rec.get("seen_trials") or ()}
        if record:
            # a run still going reads its log now, to go on from it; one that ended (most of them, and
            # history only grows) when its page asks - start-up read every one, seconds and hundreds of MB
            self._unread = True
            if self.state in ALIVE:
                self._read_log()

    def _read_log(self) -> None:
        self._unread = False
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                self._lines = fh.read().splitlines()
        except OSError:
            pass
        self._events = [e for e in map(progress.parse, self._lines) if e]

    @property
    def lines(self) -> List[str]:
        if self._unread:
            self._read_log()
        return self._lines

    @lines.setter
    def lines(self, value: List[str]) -> None:
        self._unread, self._lines = False, value

    @property
    def events(self) -> List[dict]:
        if self._unread:
            self._read_log()
        return self._events

    @events.setter
    def events(self, value: List[dict]) -> None:
        self._unread, self._events = False, value


    def _save(self) -> None:
        with self._turn:
            data = {"id": self.id, "plan": self.plan.to_dict(), "state": self.state, "step": self.step, "part": self.part, "pid": self.pid,
                    "log_offset": self.log_offset, "exit_code": self.exit_code, "error": self.error, "question": self.question, "ends": self.ends,
                    "started": self.started, "finished": self.finished, "harbor": self.harbor, "tasks_on_target": self.tasks_on_target, "app_on_target": self.app_on_target,
                    "catalogue_on_target": self.catalogue_on_target,
                    "launched": self.launched, "seen_trials": [list(k) for k in sorted(self._seen_trials, key=str)]}
            try:
                os.makedirs(self.dir, exist_ok=True)
                tmp = os.path.join(self.dir, RECORD + ".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=1)
                os.replace(tmp, os.path.join(self.dir, RECORD))
            except OSError:
                pass


    def log(self, line: str) -> None:
        if line.startswith("** ") and ("post-quantum" in line or "store now, decrypt later" in line or "openssh.com/pq" in line):
            return                                              # ssh's own warning about the host's key exchange, not the run's
        clean = redact.redact(line.rstrip("\n"))
        if self._key and self._key != settings.MODEL_KEY_PLACEHOLDER:      # the placeholder is no secret, and a common word
            clean = clean.replace(self._key, "[REDACTED]")
        for value in list(self._agent_keys.values()) + self._redact:
            if len(value) >= 8:
                clean = clean.replace(value, "[REDACTED]")
        ev = progress.parse(clean)
        with self._lock:
            self.lines.append(clean)
            if ev is not None:
                self.events.append(ev)
            try:
                os.makedirs(self.dir, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(clean + "\n")
            except OSError:
                pass

    def _log_since(self, base: str) -> str:
        """Shell that prints a part's log from ``log_offset`` to its size
        now, between markers, and that size after them. The offset then
        moves by the log's own bytes: never by what the transport prints
        around it - OpenSSH warns on stderr at every connection to a host
        without a post-quantum key exchange, 222 bytes ahead of the command's
        output - nor by what reading it as text changes (a \\r\\n read as
        \\n), so a look never skips the log nor starts inside a character."""
        off = self.log_offset
        return (f"n=$( {{ wc -c <{base}.log; }} 2>/dev/null | tr -d ' '); [ -n \"$n\" ] || n=0; printf '\\n{LOG_FROM}\\n'; "
                f"[ \"$n\" -gt {off} ] && tail -c +{off + 1} {base}.log | head -c $((n - {off})); printf '\\n{LOG_TO} %s\\n' \"$n\"")

    @staticmethod
    def _logged(out: str) -> tuple:
        """(the log text, the log's size in bytes) from what a `_log_since`
        command printed, and nothing printed around it; ("", None) when it
        did not print them."""
        _, start, rest = out.partition(f"\n{LOG_FROM}\n")
        text, end, size = rest.rpartition(f"\n{LOG_TO} ")
        n = size.strip()
        return (text, int(n)) if start and end and n.isdigit() else ("", None)

    def _read(self, line: str) -> None:
        """One line of a part's log: `@progress <label> <done> <total>` is
        the bar the run's page draws while a part downloads, never a log
        line; any other line ends it and is logged."""
        if line.startswith("@progress "):
            bits = line.split()
            if len(bits) == 4 and bits[2].isdigit() and bits[3].isdigit():
                self.progress = {"label": bits[1], "done": int(bits[2]), "total": int(bits[3])}
            return
        self.progress = None
        self.log(line)

    def tail(self, since: int = 0) -> List[str]:
        with self._lock:
            return self.lines[since:]

    def part_for(self, job_id: str) -> Optional[str]:
        """The part that writes a job: the agent it names (an arm of a run of several), or
        the part its name ends with (a Harbor agent in a run of several); the
        only part of a run of one."""
        steps = self.plan.steps()
        job = next((j for j in self.plan.jobs if str(j.get("name")) == job_id), None)
        if job is None:
            return None
        for p in steps:
            if job.get("agent") == p["id"] or job_id == f"{self.plan.name}-{p['id']}":
                return p["id"]
        return steps[0]["id"] if len(steps) == 1 else None

    def job_state(self, job_id: str) -> Optional[tuple]:
        """``(state, finished_at)`` for one job of this run once it has ended:
        its own part's result when that part ran to its end - an arm that
        answered every question is done though a later one failed - else the
        run's, once the run has ended. None while it is still to come."""
        end = self.ends.get(self.part_for(job_id) or "")
        if end is not None:
            return ("done" if end["rc"] == 0 else "failed"), datetime.fromtimestamp(end["at"], tz=timezone.utc)
        if self.state in ENDED and self.finished:
            return self.state, datetime.fromtimestamp(self.finished, tz=timezone.utc)
        return None

    def refused(self, job_id: str) -> Optional[str]:
        """Why one job of this run was never written, when the run was refused
        before it: the standard's cli said so in the job's folder
        (``stage=refused`` in its ``progress.jsonl``, the only thing it
        wrote there), or the run failed before any part's launch began - a
        key not set, a host without docker. None for a run that got further,
        whose job is elsewhere or still to come. The folder is kept: it holds
        the refusal and the image's build log, and nothing reads it as a job."""
        if self.state in ALIVE:
            return None
        why = progress.refusal(progress.read(os.path.join(self.jobs, job_id)))
        if why is None and self.state == "failed" and not self.launched and not self.ends:
            why = self.error or "the run failed before it started"
        return why

    def live_state(self, job_id: str) -> Optional[str]:
        """What one job of this run is doing while the run lives: the run's
        state for the part running now, ``queued`` for a part still to come
        (the parts run one after another), and the run's own while it
        prepares or stops, which is every part's. ``not run`` for one held
        back by a pause: nothing starts it unless the page says continue. None
        once the run ended."""
        if self.state not in ALIVE:
            return None
        part = self.part_for(job_id)
        if self.state in ("preparing", "stopping") or part is None or part == self.launched:
            return self.state
        return NOT_RUN if self.state == "paused" else "queued"

    def never_ran(self, job_id: str) -> bool:
        """The run ended before this job's part began: nothing of it was launched."""
        ids = [p["id"] for p in self.plan.steps()]
        part = self.part_for(job_id)
        return self.state in ENDED and part in ids and part != self.launched and part not in self.ends and ids.index(part) >= self.part

    def summary(self) -> dict:
        return {"id": self.id, "name": self.plan.name, "state": self.state, "step": self.step, "host": self.plan.host.label if self.plan.host else "this machine",
                "exit_code": self.exit_code, "error": self.error, "started": self.started, "finished": self.finished, "jobs": self.jobs,
                "job_ids": [str(j.get("name")) for j in self.plan.jobs], "lines": len(self.lines), "question": self.question,
                "progress": self.progress, "stages": self.stages()}

    def stages(self) -> dict:
        """The stages said so far, folded for the page (``progress.fold``).
        Once the run has failed or been stopped, a stage still running was
        cut short by it, and nothing is waiting any more."""
        with self._lock:
            events = list(self.events)
        got = progress.fold(events)
        if self.state in ("failed", "stopped"):
            got["stages"] = [dict(r, state="failed") if r["state"] == "running" else r for r in got["stages"] if r["state"] != "waiting"]
            got["current"] = None
        return got


    def start(self) -> "Execution":
        self._save()
        self.thread = threading.Thread(target=self._main, name=f"run-{self.plan.name}", daemon=True)
        self.thread.start()
        return self

    def resume(self) -> "Execution":
        """Pick a recorded run up where it was: the part that was running is
        followed again, a pause waits again, the rest runs on."""
        self.thread = threading.Thread(target=self._main_resumed, name=f"run-{self.plan.name}", daemon=True)
        self.thread.start()
        return self

    def continue_(self) -> None:
        """Answer a pause: go on with the next agent."""
        self.question = None
        self._decision.set()

    def stop(self) -> None:
        """Say so at once - the page reads `stopping` on its next tick - and
        let the run's own thread do the rest: the command it is inside gives
        way (`subprocess_run`, `stop=`), the follow loop returns, `_done`
        ends the part on the target (`_kill_part`) - one whose launcher it
        cut short included - copies the job back from a host once more, and
        records `stopped`: a part that had ended by itself with its own
        result, a part the Stop ended with 130, the same for a Stop a restart
        finishes (`_done` says the rule). Nothing
        here waits on the host, so a host that does not answer cannot hold
        the button."""
        with self._turn:
            if self.state not in ALIVE:
                return
            self.state = "stopping"
            self.log("— stop requested")
            self._save()
            self._stop.set()
            self._decision.set()

    def _kill_part(self) -> Optional[int]:
        """The part that may be running on the target (``_live``) ended by
        the Stop - unless it had ended by itself - and the part's result
        returned. One command, through the plain runner since the stop is
        already set, reads the part's exit code before it ends anything: a
        part that has one ended by itself before the Stop reached it -
        between two of ``_follow``'s looks, or while Nullius was away - and
        its end is recorded as the look would have recorded it (the rest of
        its log, its result in ``ends``, the run moved past it); its exit
        code is returned. Otherwise its processes are ended, by their
        process groups, found as ``_part_pids`` finds them, so a part is
        ended though its launcher was cut short before its pid came back,
        and a pid the record kept is signalled only while it is still the
        part's own. Once they are gone - half a second at most - the exit
        code is read again: a part that ended by itself between the first
        read and the kill has it now, and keeps it. One the kill ended has
        none, since its detached shell, which writes it, dies with it - so
        every process group is signalled before any single process, lest
        the script's own bash die alone and its shell write that down.

        The Stop ended it - 130 - when anything of it was there (its claim,
        pid file, log or a process), the target did not say, or its launch
        had begun (``launched``: its script or its launcher on the way, cut
        short before anything of it reached the target); None when none of
        that, nor any part under way: the Stop ended none.

        A launcher cut short may yet start the detached shell after this has
        looked - on a host the launcher goes on when the ssh that carried it
        is gone. So the Stop is left in the run directory first
        (``run-<part>.stop``), and the detached shell looks for it after
        writing its pid and before running the script: either it sees the
        Stop and runs nothing, or its pid was on disk, and the shell in the
        process table, before this looked."""
        part = self._live
        if part is None:
            return None
        base = _base(part)
        command = ('export PATH="$HOME/.local/bin:$PATH"; ' + f"cd {self._q(self._run_dir())} 2>/dev/null || exit 0; "
                   f"ended() {{ e=$(head -c 32 {base}.exit 2>/dev/null | tr -cd 0-9); [ -z \"$e\" ] || "
                   f"{{ {self._log_since(base)}; printf '\\n__part__ %s' \"$e\"; exit 0; }}; }}; ended; : >{base}.stop; "
                   f"s=none; [ -e {base}.launch ] || [ -e {base}.pid ] || [ -e {base}.log ] && s=stopped; k=$({_part_pids(base, [self.pid])}); "
                   f"for p in $k; do s=stopped; kill -TERM -- -$p 2>/dev/null; done; for p in $k; do kill -TERM $p 2>/dev/null; done; "
                   f"for i in 1 2 3 4 5 6 7 8 9 10; do a=; for p in $k; do kill -0 $p 2>/dev/null && a=1; done; [ -n \"$a\" ] || break; sleep 0.05; done; "
                   + (f"rm -rf {self._licence_dir()}; " if self._licence_dir() else "")
                   + f"ended; printf '\\n__part__ %s' \"$s\"")
        argv = self.plan.host.argv(command, timeout=15) if self.on_host else ["bash", "-c", command]
        rc, out = self._run_plain(argv, STOP_WAIT)
        text, mark, code = out.rpartition("\n__part__ ")
        code = code.strip()
        if rc == 0 and (not mark or code == "none"):
            # nothing of the part there (nor its run directory): the Stop ended none - unless it cut the part's launch short
            return 130 if self.launched == part["id"] else None
        if rc != 0 or not code.isdigit():
            return 130                                   # the Stop ended it, or the target did not say it had ended
        text, size = self._logged(text)
        for line in text.splitlines():
            self._read(line)
        self._trial_lines()
        with self._turn:
            if size is not None and size > self.log_offset:
                self.log_offset = size
            self.ends[part["id"]] = {"rc": int(code), "at": time.time(), "label": part["label"]}
            self.part = [p["id"] for p in self.plan.steps()].index(part["id"]) + 1
            self.pid, self._live = None, None
        return int(code)

    def _main(self) -> None:
        try:
            os.makedirs(self.jobs, exist_ok=True)
            with open(os.path.join(self.dir, "run.sh"), "w", encoding="utf-8") as fh:
                fh.write(self.plan.script if self.plan.script.endswith("\n") else self.plan.script + "\n")
            problem = self.plan.check()
            if problem:
                return self._fail("plan", problem)
            harbor_model = "NULLIUS_HARBOR" in (self.plan.script + "".join(p.get("script", "") for p in self.plan.parts)) and self.plan.model
            if not self._key and harbor_model:
                return self._fail("credentials", f"{self.plan.key_var} is not set in Settings; the run would have no model key")
            if not self._key and self.plan.runner != "harbor" and not self.plan.keyless:
                return self._fail("credentials", f"{self.plan.key_var} is not set in Settings; the producers and the judge would have no model key")
            if not self._url and self.plan.url_var and not self.plan.keyless and (harbor_model or self.plan.runner != "harbor"):
                return self._fail("credentials", f"{self.plan.url_var} is not set in Settings; an OpenAI-compatible endpoint has no default, so the run would have nowhere to send its calls")
            broken = self._line_break()
            if broken:
                return self._fail("credentials", broken)
            if not self._prepare(fresh=True):
                return
            self._parts()
        except Exception as e:                           # a step that raised is a failed run, not a dead thread
            self._fail(self.step or "run", f"{type(e).__name__}: {e}")

    def _main_resumed(self) -> None:
        try:
            self.log(f"— Nullius is back; picking the run up at {self.plan.steps()[min(self.part, len(self.plan.steps()) - 1)]['label']}")
            if self.state == "preparing":
                if not self._prepare(fresh=False):
                    return
            self._parts(resumed=True)
        except Exception as e:
            self._fail(self.step or "run", f"{type(e).__name__}: {e}")

    def _fail(self, step: str, why: str) -> None:
        if self._stop.is_set():                          # a step cut short by a Stop is the stop, not a failure
            return self._done(130)
        self._stage_end("failed", detail=why[:300])
        self.step, self.state, self.error, self.finished = step, "failed", why, time.time()
        self.log(f"✕ {step}: {why}")
        self._save()

    def _done(self, rc: int) -> None:
        """The run's end, recorded once: ``done`` or ``failed`` by ``rc``,
        or ``stopped`` under a Stop. What a Stop records is the same whether
        it was said to this run or read from a record by a restart, which
        has taken the part from the target first (``_settle``):

        - a part that ended by itself keeps its result. The part under way
          (``_live``) is asked for its exit code before anything is ended
          (``_kill_part``), and an end found there - since the run's last
          look, or while Nullius was away - is recorded as that look would
          have recorded it: its job reads done or failed.
        - a part the Stop ended - found on the target without an exit code,
          or on a target that did not answer - is 130, the line is
          ``✕ stopped during <part>: exit 130``, and its job reads
          ``stopped``, the run's.
        - the run's exit code is its last part's result: the exit code of a
          part that ended by itself, 130 for one the Stop ended. With no
          part under way - a pause, the moment between two parts, a part
          with nothing of it on the target yet - it is the exit the run
          had: the last part's own (0 before any), or 130 for a step the
          Stop cut short (``_fail``) - a part's launch included, though
          nothing of the part reached the target: the record names the part
          whose launch began (``launched``), so a restart records the 130
          too. A record from before ``launched`` takes it for a Stop before
          the launch.
        - the line says the run was stopped, and where: ``during <part>``
          for a part the Stop ended, else ``before`` the next part, or
          ``after`` the last one - ``✕ stopped before <part>: exit <rc>``,
          never ``✓ finished``, whatever the exit.
        - on a host, the job is copied back once more before the run is
          recorded, once a part has run: the trials that ended there since
          the run's last look are here, and said. The copy goes through the
          plain runner, and the kill and the copy together take at most
          ``STOP_WAIT``; a host that took it all is not asked again.

        The next part is never started."""
        live, ended = self._live, None
        if self._stop.is_set():
            until = time.monotonic() + STOP_WAIT
            ended = self._kill_part()
            if ended is not None:
                rc = ended
            if self.on_host and (ended is not None or self.ends):
                # the trials that ended there since the run's last look, copied back once more before the run is stopped
                left = until - time.monotonic()
                if left >= 1:
                    self._mirror(self._run_plain, left)
                else:
                    self.log(f"  the job did not sync back: the host took the Stop's {int(STOP_WAIT)} s")
        with self._turn:                                 # a Stop said as the run ends is "stopped", never left "stopping"
            self.exit_code, self.finished, self.pid = rc, time.time(), None
            self.state = "stopped" if self._stop.is_set() else ("done" if rc == 0 else "failed")
            if self.state == "failed":
                failed = [f"{e['label']} exited {e['rc']}" for e in self.ends.values() if e.get("rc")]
                self.error = "; ".join(failed) if failed else f"the run exited {rc}"
            if self.state == "stopped":
                steps = self.plan.steps()
                where = (f"during {live['label']}" if live is not None and ended is not None and live["id"] not in self.ends
                         else f"before {steps[self.part]['label']}" if self.part < len(steps) else f"after {steps[-1]['label']}")
                self.log(f"✕ stopped {where}: exit {rc}")
            else:
                self.log(f"{'✓' if rc == 0 else '✕'} finished: exit {rc}")
            self._save()

    def _begin(self, step: str, what: str, stage: bool = False) -> None:
        """A step of the run, said and saved. ``stage``: one that gets the
        target ready, said as a stage (``progress``) that ends with the next
        such step, the preparation's end (``_stage_end``) or a failure."""
        self.step = step
        if stage:
            self._stage_end()
            self._open = (step, time.monotonic())
            self.log(progress.line(step, "start", detail=what))
        else:
            self.log(f"— {what}")
        self._save()

    def _stage_end(self, state: str = "done", **fields) -> None:
        """The step under way said ended - done, or failed - with its seconds."""
        if self._open is None:
            return
        step, t0 = self._open
        self._open = None
        self.log(progress.line(step, state, seconds=round(time.monotonic() - t0, 1), **fields))

    def _pause(self, label: str, rc: int, remaining: List[dict]) -> bool:
        """An agent's run failed with more to come: wait for the page to say
        continue or stop. True to go on; False at once for a Stop said since
        the part ended, which this would otherwise clear and pause over."""
        return self._ask(label, rc, remaining) and self._wait_decision(remaining)

    def _ask(self, label: str, rc: int, remaining: List[dict]) -> bool:
        """The pause's question, put and saved; False, and nothing asked, once
        a Stop is said."""
        with self._turn:
            if self._stop.is_set():
                return False
            self._decision.clear()                # before the page can see "paused" and answer
            self.state = "paused"
            self.question = f"{label} exited {rc}. Continue with {remaining[0]['label']}, or stop? A failure this early usually means the dataset or the environment, not the agent."
            self.log(f"✕ {label} exited {rc} — waiting: continue with {remaining[0]['label']}, or stop?")
            self._save()
        return True

    def _wait_decision(self, remaining: List[dict]) -> bool:
        self._decision.wait()
        if self._stop.is_set():
            return False
        self.state, self.question = "running", None
        self.log(f"— continuing with {remaining[0]['label']}")
        self._save()
        return True

    def _trial_lines(self) -> None:
        """One line per trial that ended since the last look, read from the
        jobs' own feeds, so the log says what finished as it finishes. The
        trials said are saved with the record as soon as they are said, so a
        restart, which reads the feeds again from their start, says none of
        them twice."""
        try:
            names = sorted(os.listdir(self.jobs))
        except OSError:
            return
        said = False
        for name in names:
            feed = os.path.join(self.jobs, name, "events.jsonl")
            try:
                with open(feed, encoding="utf-8", errors="replace") as fh:
                    for raw in fh:
                        if '"trial.end"' not in raw:
                            continue
                        try:
                            ev = json.loads(raw)
                        except ValueError:
                            continue
                        key = (name, ev.get("trial"))
                        if ev.get("event") != "trial.end" or key in self._seen_trials:
                            continue
                        with self._turn:                     # _save reads the set from the page's thread too
                            self._seen_trials.add(key)
                        said = True
                        d = ev.get("durations") or {}
                        secs = sum(v for v in d.values() if isinstance(v, (int, float)))
                        reward, score = ev.get("reward"), ev.get("score")
                        # a trial its benchmark scored without a pass or fail is neither mark: its score is said
                        mark = "✓" if ev.get("outcome") == "resolved" else "·" if ev.get("outcome") == "scored" else "✕"
                        self.log(f"  {mark} {name} · {ev.get('task') or ev.get('trial')} · {ev.get('outcome')}"
                                 + (f" · reward {reward}" if reward is not None else f" · score {score:.3g}" if isinstance(score, (int, float)) else "")
                                 + (f" · {int(secs // 60)}:{int(secs % 60):02d}" if secs else ""))
            except OSError:
                continue
        if said:
            self._save()


    @property
    def on_host(self) -> bool:
        return self.plan.host is not None

    def _remote(self, *parts: str) -> str:
        return "/".join((REMOTE_HOME,) + parts)

    def _run_dir(self) -> str:
        return self._remote("runs", self.plan.name) if self.on_host else self.dir

    def _q(self, path: str) -> str:
        return _rq(path) if self.on_host else shlex.quote(path)

    def _sh(self, command: str, timeout: float = 600.0, stdin: Optional[str] = None) -> tuple:
        """One command on the target, with uv on the PATH; (returncode, output)."""
        command = 'export PATH="$HOME/.local/bin:$PATH"; ' + command
        if self.on_host:
            return self._run(self.plan.host.argv(command, timeout=15), timeout, stdin)
        return self._run(["bash", "-c", command], timeout, stdin)

    def _ssh_prefix(self) -> str:
        return " ".join(shlex.quote(a) for a in self.plan.host.argv("", timeout=15)[:-2])   # ssh + options, without target and command

    def _target(self) -> str:
        return self.plan.host.argv("")[-2]

    def _copy_to(self, src: str, dest: str) -> tuple:
        return self._run(["rsync", "-az", "--delete", "-e", self._ssh_prefix(), src.rstrip("/") + "/", f"{self._target()}:{dest}"], 900)

    def _copy_back(self, run: Optional[Callable] = None, timeout: float = 900.0) -> tuple:
        started = time.monotonic()
        copy = run or self._run
        result = copy(["rsync", "-az", "-e", self._ssh_prefix(), f"{self._target()}:{self._remote('runs', self.plan.name, 'jobs')}/", self.jobs + "/"], timeout)
        if result[0] == 0 and self.plan.runner == STANDARD and self.catalogue_on_target:
            # Proofs write back to this run's definitions. Keep them beside
            # the job evidence, without replacing this user's catalogue.
            local = os.path.join(self.dir, "catalogue")
            os.makedirs(local, exist_ok=True)
            result = copy(["rsync", "-az", "-e", self._ssh_prefix(),
                           f"{self._target()}:{self.catalogue_on_target}/", local + "/"],
                          max(0.1, timeout - (time.monotonic() - started)))
        return result


    def _prepare(self, fresh: bool = True) -> bool:
        """The target ready for the parts: the tasks, and what the run needs
        there - for a Harbor recipe docker, here or on a host, and the Harbor it pins
        (built here when it is a repository's commit, its wheels sent to a
        host, run through uvx) or, for a plan from before the pin, the harbor
        on the host's PATH; uv and the runner snapshot for the standard. False when a step
        failed and said so.

        A ``fresh`` run on a host first asks whether its run directory is
        there already, and stops before anything is sent when it is: a
        leftover of the same name would otherwise read as this run's parts,
        claimed or ended already. (Here the route asks, ``web/execute.py``.)
        A restored run's directory is its own."""
        self.harbor = "harbor"
        if fresh and not self.on_host and (self.plan.runner == "harbor" or self.plan.docker):
            # before a pin is built: a run here without docker would start, and every trial fail at its container
            why = (docker_here(self._sh) if self.plan.runner == "harbor"
                   else docker_here(self._sh, "this recipe runs its units in containers", container.DOCKER))
            if why:
                self._fail("docker", why)
                return False
        pin = self.plan.harbor_pin if self.plan.runner == "harbor" else {}
        if fresh and (self.on_host or pin):
            self.log(progress.line("plan", stages=",".join(
                (["harbor"] if pin else []) + (["connect", *(["uv"] if pin or self.plan.runner != "harbor" else []),
                                                *(["tasks"] if self.plan.tasks else []), *(["app"] if self.plan.runner == STANDARD else [])]
                                               if self.on_host else []))))
        record = self._pin_here(pin) if pin else None
        if pin and record is None:
            return False
        if not self.on_host:
            if pin:
                rc, _ = self._sh("command -v uvx", 30)
                if rc != 0:
                    self._fail("harbor", "uv is not on this machine's PATH; the Harbor this recipe pins runs through uvx")
                    return False
                os.makedirs(self.dir, exist_ok=True)
                self.harbor = harbor_source.write_wrapper(os.path.join(self.dir, "harbor"), record,
                                                          shlex.quote(os.path.join(self.pins, harbor_source.key_of(pin))), pin.get("extras", []))
            # absolute: the part runs from its run directory, never from where this process started
            self.tasks_on_target = os.path.abspath(os.path.expanduser(self.plan.tasks)) if self.plan.tasks else ""
            if self.plan.runner == STANDARD:
                self.catalogue_on_target = self._catalogue
                if os.path.isfile(os.path.join(REPO, "app", "nullius", "standard", "cli.py")):
                    self.app_on_target = REPO
                else:
                    got = self._app_here()
                    if got is None:
                        return False
                    self.app_on_target = got[1]
            self._stage_end()
            self._save()
            return True
        host = self.plan.host
        self._begin("connect", f"connecting to {host.label}", stage=True)
        probe = hosts.test(host, run=lambda argv, t: _probe(self._run, argv[:-1] + ['export PATH="$HOME/.local/bin:$PATH"; ' + argv[-1]], t))
        if not probe["ok"]:
            self._fail("connect", probe["error"])
            return False
        if fresh:
            rc, out = self._sh(f"[ -e {_rq(self._run_dir())} ] && echo @taken; true", 30)
            if rc != 0:
                self._fail("connect", "the host did not say whether the run's name is free: " + out.strip()[-400:])
                return False
            if "@taken" in out.split():
                self._fail("name", name_taken(self.plan.name, host.label))
                return False
        # what this run needs of the host, and nothing it does not
        if self.plan.runner == "harbor":
            self.log(f"  {probe['hostname'] or host.label}: docker {'yes' if probe['docker'] else 'no'}"
                     + (f" · uv {'yes' if probe['uv'] else 'no'}" if pin else f" · harbor {(probe['harbor_version'] or 'yes') if probe['harbor'] else 'no'}"))
            if not probe["docker"]:
                self._fail("connect", "docker is not on the host; a Harbor run needs it")
                return False
            if probe["docker_ok"] is False:
                self._fail("connect", "docker is on the host but this user cannot use it (docker ps failed)")
                return False
            if not pin and not probe["harbor"]:
                self._fail("connect", "harbor is not on the host's PATH (~/.local/bin included); install the Harbor this recipe needs there")
                return False
        elif self.plan.runner == STANDARD:
            # the standard's own cli: python3 and git there, docker or podman when its units run in containers
            engine = probe["engine"]
            self.log(f"  {probe['hostname'] or host.label}: uv {'yes' if probe['uv'] else 'no'} · {engine or 'docker'} {'yes' if engine else 'no'}")
            if self.plan.docker and not engine:
                self._fail("connect", "neither docker nor podman is on the host: this recipe runs its units in containers")
                return False
            if self.plan.docker and probe[engine + "_ok"] is False:
                self._fail("connect", f"{engine} is on the host but this user cannot use it ({engine} ps failed)")
                return False
            rc, out = self._sh("for c in python3 git; do command -v $c >/dev/null 2>&1 || echo @missing $c; done; true", 30)
            gone = [l.split()[1] for l in out.splitlines() if l.startswith("@missing ")]
            if rc != 0 or gone:
                self._fail("connect", (" and ".join(gone) + " not on the host; the standard's cli needs them") if gone
                           else "the host did not say whether python3 is there: " + out.strip()[-400:])
                return False
        else:
            # a run record from a runner no longer offered (a benchmark's own scripts now run as the standard's Original setup)
            self._fail("connect", f"this run's runner, {self.plan.runner}, is no longer offered")
            return False
        if (pin or self.plan.runner != "harbor") and probe["uv"]:
            self._stage_end()
            self.log(progress.line("uv", "cached", detail="on the host already"))
        if (pin or self.plan.runner != "harbor") and not probe["uv"]:
            # the standard's cli builds its environments with uv; a pinned Harbor runs through uvx
            self._begin("uv", "installing uv on the host", stage=True)
            rc, out = self._sh(f"command -v uv >/dev/null 2>&1 || ({UV_INSTALL}) >/dev/null 2>&1; command -v uv", 300)
            found = [l for l in out.strip().splitlines() if not l.startswith("** ")]
            if rc != 0 or not found:
                self._fail("uv", "uv could not be installed: " + out.strip()[-400:])
                return False
            self.log("  uv: " + found[-1])
        self.tasks_on_target = ""
        if self.plan.tasks:
            self._begin("tasks", f"sending the task folder {self.plan.tasks}", stage=True)
            self.tasks_on_target = self._remote(*_tasks_place(self.plan.tasks, self.plan.name))
            rc, out = self._sh(f"mkdir -p {_rq(self.tasks_on_target)}", 30)
            if rc == 0:
                rc, out = self._copy_to(os.path.expanduser(self.plan.tasks), self.tasks_on_target)
            if rc != 0:
                self._fail("tasks", "the task folder did not copy: " + out.strip()[-400:])
                return False
        if self.plan.runner == STANDARD and not self._app_on_host():
            return False
        run_dir = self._run_dir()
        rc, out = self._sh(f"mkdir -p {_rq(run_dir + '/jobs')} && cat > {_rq(run_dir + '/run.sh')}", 30, stdin=self.plan.script)
        if rc != 0:
            self._fail("run", "the script did not land on the host: " + out.strip()[-400:])
            return False
        if self.plan.runner == STANDARD:
            catalogue = run_dir + "/catalogue"
            stage = catalogue + ".part"
            # A restart keeps the proofs already written by this run.
            # Copy into staging so an interrupted first copy is not trusted.
            rc, out = self._sh(f"if [ ! -d {_rq(catalogue)} ]; then rm -rf {_rq(stage)} && "
                               f"cp -R {_rq(self.app_on_target + '/app/nullius/catalogue')} {_rq(stage)} && "
                               f"mv {_rq(stage)} {_rq(catalogue)}; fi", 300)
            if rc != 0:
                self._fail("app", "the run's catalogue could not be prepared: " + out.strip()[-400:])
                return False
            self.catalogue_on_target = catalogue
        if pin and not self._pin_on_host(pin, record, run_dir):
            return False
        self._stage_end()
        self._save()
        return True

    def _app_here(self) -> Optional[tuple]:
        """(content ID, folder) for this runtime and its effective catalogue.

        The exact code imported here travels, including source edits, so
        installed wheels and editable checkouts have the same behaviour.
        Configuration and credential stores are not payload inputs.
        """
        try:
            return standard_payload.build(app_root(self.root), catalogue=self._catalogue)
        except (OSError, ValueError) as e:
            self._fail("app", "the standard runner could not be packed: " + str(e)[-300:])
            return None

    def _app_on_host(self) -> bool:
        """Send the content-addressed runner and catalogue to the host."""
        got = self._app_here()
        if got is None:
            return False
        content_id, here = got
        remote = self._remote("app", content_id)
        self._begin("app", f"sending the standard runner and catalogue at {content_id[:15]}", stage=True)
        rc, out = self._sh(f"mkdir -p {_rq(remote)}", 30)
        if rc == 0:
            rc, out = self._copy_to(here, remote)
        if rc != 0:
            self._fail("app", "app/ did not copy: " + out.strip()[-400:])
            return False
        self.app_on_target = remote
        return True

    def _pin_here(self, pin: dict) -> Optional[dict]:
        """The pinned Harbor ready on this machine - a repository's commit
        built here, once (``harbor_source.ensure``) - and its record; None
        when it could not be, said."""
        building = pin["kind"] == "repo" and harbor_source.record_of(pin, self.pins) is None
        self._begin("harbor", harbor_source.label(pin) + (": building it here, once for this commit" if building else ""), stage=True)
        try:
            return harbor_source.ensure(pin, self.pins, run=lambda argv, t=600.0: harbor_source.run_command(argv, t, stop=self._stop), log=self.log)
        except harbor_source.BuildError as e:
            self._fail("harbor", str(e))
            return None

    def _pin_on_host(self, pin: dict, record: dict, run_dir: str) -> bool:
        """The pinned Harbor on the host: a repository's wheels sent to
        ``~/.nullius/harbor/<pin>/wheels`` (the host resolves the rest from
        PyPI, so it needs no access to the repository), and the ``harbor``
        script that runs it written into the run directory."""
        remote = self._remote("harbor", harbor_source.key_of(pin))
        if record.get("wheel"):
            self._begin("harbor", f"sending the wheels built for {harbor_source.label(pin)}", stage=True)
            rc, out = self._sh(f"mkdir -p {_rq(remote + '/' + harbor_source.WHEELS)}", 30)
            if rc == 0:
                rc, out = self._copy_to(os.path.join(self.pins, harbor_source.key_of(pin), harbor_source.WHEELS), remote + "/" + harbor_source.WHEELS)
            if rc != 0:
                self._fail("harbor", "the wheels did not copy: " + out.strip()[-400:])
                return False
        exe = run_dir + "/harbor"
        rc, out = self._sh(f"cat > {_rq(exe + harbor_source.FEED_SUFFIX)}", 30, stdin=harbor_source.feed_source())   # the live feed it runs
        if rc == 0:
            rc, out = self._sh(f"cat > {_rq(exe)} && chmod +x {_rq(exe)}", 30, stdin=harbor_source.wrapper(record, _rq(remote), pin.get("extras", [])))
        if rc != 0:
            self._fail("harbor", "the harbor script did not land on the host: " + out.strip()[-400:])
            return False
        self.harbor = exe
        return True

    # --- the parts, one agent at a time -----------------------------------------

    def _parts(self, resumed: bool = False) -> None:
        steps = self.plan.steps()
        follow = resumed and self._settle(steps)       # a restored record, checked against the target first
        i = self.part
        # picked up after a part ended: that part's own exit, as the run had it, not a 0 for the restart
        last = self.ends.get(steps[i - 1]["id"], {}).get("rc", 0) if 0 < i <= len(steps) else 0
        while i < len(steps):
            part = steps[i]
            if resumed and self.state == "paused":
                if not self._wait_decision(steps[i:]):
                    break
                launched = self._launch(part)
                if launched is None:
                    break
                if not launched:
                    return
            elif follow and self.state == "running":
                self.log(f"— following {part['label']} again on {self.plan.host.label if self.on_host else 'this machine'}")
            else:
                launched = self._launch(part)
                if launched is None:
                    break
                if not launched:
                    return
            resumed = follow = False
            last = self._follow(part)
            if self._stop.is_set():
                break
            with self._turn:
                # Ended, and recorded so before anything else is: its exit, the next part, and no pid -
                # a restart launches the next part, never follows it by this one's pid.
                self.ends[part["id"]] = {"rc": last, "at": time.time(), "label": part["label"]}
                i += 1
                self.part, self.pid, self._live = i, None, None
            if last != 0 and i < len(steps):
                if not self._pause(part["label"], last, steps[i:]):     # its one save is the end and the question: a restart asks again
                    break
            else:
                self._save()
        self._done(last)

    def _settle(self, steps: List[dict]) -> bool:
        """A restored record's part and pid, checked against the target
        before either is trusted; True when that part is to be followed, not
        launched. A part has started when its run directory there holds its
        pid file (its detached shell's first act), its log or its exit code,
        or when a process runs its script there (``_part_pids``: the launcher
        before c69f527 wrote no pid file).

        - No pid in the record: it was saved before the pid was. Started, the
          part is followed - by the pid it wrote, else one of its processes,
          else none: its exit code ends it, or, with no process left, it is
          gone without one - its log read from the start. Claimed by a
          launcher (``.launch``) and nothing more: the launcher is on its way
          to starting it, so it is looked at again for ``LAUNCH_GRACE``; still
          nothing - no pid, no process - and the launcher died first: the
          claim is cleared and the part launched. Not claimed: launched.
        - A pid, a part before it, and this part started: the pid is checked
          against the part - the one it wrote, or one of its processes. Not
          its own, it is the part before's: a record an older version saved
          as this part was launched. It is dropped, the part
          before's end taken as below, and this part followed as if no pid
          had been saved.
        - A pid, and the part not started - or, past the last part, no end
          recorded for it: a record from before a part's end was one save,
          saved as the previous part ended. The pid is that part's;
          its end is the record's or, from before ``ends``, its ``.exit``
          (none: gone without one). The run goes on as it would have: asks
          after a failure with more to come, else launches the part or ends
          with that exit.
        - A pause: it follows a part's end, so nothing of the run is running.
          A pid it kept (an older record's: the ended part's) is
          nobody's now, and is dropped.
        - A Stop the restart cut short: the part is taken as above, and the
          Stop is then finished as a live one is (``_done``) - a part that
          ended by itself meanwhile keeps its own result, read through the
          plain runner, since the Stop, set already, would cut a slow host's
          answer short.

        A pid with no part before it stands as recorded: it can be no other
        part's, and it is followed and killed only while it runs the part."""
        i = self.part
        if self.state == "paused":
            if self.pid:
                with self._turn:
                    self.pid = None
                    self._save()
            return False
        if self.state not in ("running", "stopping") or i > len(steps):
            return bool(self.pid)
        cur, prev = (steps[i] if i < len(steps) else None), (steps[i - 1] if i else None)
        self._live = cur                                       # running, or being launched, as far as the record knows
        # a pid with no part before it can be no other part's: it stands (followed and killed only while it runs the part)
        check = cur is not None and (not self.pid or prev is not None)
        mine = [f"{_base(cur)}.{ext}" for ext in ("launch", "pid", "log", "exit")] if check else []
        before = [f"{_base(prev)}.exit"] if prev and prev["id"] not in self.ends else []
        if not mine and not before:
            return bool(self.pid)
        look = self._look(mine + before, cur if check else None, [self.pid])
        if look is None:
            return bool(self.pid)
        found, procs = look
        started = lambda: bool(procs) or any(n in found for n in mine[1:])     # its pid file, log or exit code, or its processes
        if check and mine[0] in found and not started():
            # claimed by a launcher and not started yet: the launcher is on its way to it, or died first
            look = self._await_start(mine + before, cur)
            if look is None:
                return bool(self.pid)
            found, procs = look
            if not started():
                self.log(f"  {cur['label']} was claimed by a launcher that never started it; starting it again")
                self._sh(f"cd {self._q(self._run_dir())} && rm -f {mine[0]}", 30)
        if check and started():
            written = int(found.get(mine[1]) or 0) or None
            if self.pid and prev is not None and self.pid != written and self.pid not in procs:
                # Not this part's: a record an older version saved as it was launched kept the part
                # before's pid - and this part, started by the launcher before its pid file, is found by its processes.
                with self._turn:
                    if before:
                        rc = found.get(before[0], "")
                        self.ends[prev["id"]] = {"rc": int(rc) if rc else 1, "at": time.time(), "label": prev["label"]}
                    self.pid = None
            if not self.pid:
                with self._turn:
                    self.pid, self.log_offset = written or (procs[0] if procs else None), 0
                    self._save()
            return True
        old = bool(self.pid)                                   # a pid, and no part of its own: the part before's
        if not old and not before:
            return False                                       # never started: launched
        with self._turn:
            if before:
                rc = found.get(before[0], "")
                self.ends[prev["id"]] = {"rc": int(rc) if rc else 1, "at": time.time(), "label": prev["label"]}
            self.pid = None
        rc = self.ends[prev["id"]]["rc"]
        if not (old and cur and rc != 0 and self._ask(prev["label"], rc, steps[i:])):    # asked: that save is the record
            self._save()
        return False

    def _look(self, names: List[str], part: Optional[dict] = None, pids=()) -> Optional[tuple]:
        """Which of ``names`` the run directory on the target holds, each with
        the digits it starts with (a pid, an exit code), and - for ``part`` -
        the pids of its processes there (``_part_pids``, asked of ``pids``
        too): ({name: digits}, [pid]). Asked again while the target does not
        answer, as ``_follow`` does; None for a Stop said meanwhile."""
        files = " ".join(shlex.quote(n) for n in names)
        procs = f"for p in $({_part_pids(_base(part), pids)}); do echo \"@proc $p\"; done; " if part else ""
        while True:
            rc, out = self._sh(f"cd {self._q(self._run_dir())} 2>/dev/null || exit 0; for f in {files}; do "
                               f"[ -f \"$f\" ] && printf '@file %s %s\\n' \"$f\" \"$(head -c 32 \"$f\" | tr -cd 0-9)\"; done; {procs}true", 60)
            if rc == 0:
                lines = [l.split() for l in out.splitlines()]
                return ({b[1]: "".join(b[2:]) for b in lines if len(b) >= 2 and b[0] == "@file"},
                        [int(b[1]) for b in lines if len(b) == 2 and b[0] == "@proc" and b[1].isdigit()])
            if self._stop.wait(SYNC_EVERY):
                return None

    def _await_start(self, names: List[str], part: dict) -> Optional[tuple]:
        """A part its launcher claimed and nothing after: looked at again
        for up to ``LAUNCH_GRACE`` seconds, until its pid file, log or exit
        code is there or a process runs it. A launcher that has claimed a
        part runs in its run directory until it has started the part, so
        nothing there, all that time, means the launcher died first. The
        last look; None for a Stop said meanwhile."""
        look = None
        for _ in range(max(1, int(LAUNCH_GRACE / STOP_POLL))):
            if self._stop.wait(min(STOP_POLL, LAUNCH_GRACE)):
                return None
            look = self._look(names, part, [self.pid])
            if look is None or look[1] or any(n in look[0] for n in names[1:4]):
                return look
        return look

    def _stdin(self) -> str:
        """What the launcher reads: the key, the endpoint, the agent keys as
        NAME=value, the benchmark's licences as one more (a standard run on a
        host), the end line."""
        lic = self._licence_line()
        return "".join(f"{line}\n" for line in [self._key, self._url, *(f"{n}={v}" for n, v in self._agent_keys.items()),
                                                 *([f"{secrets.HANDED}={lic}"] if lic else []), STDIN_END])

    def _licence_dir(self) -> str:
        """Where a standard run on a host writes a licence file: in memory, the run's own folder."""
        return f"/dev/shm/nullius-{self.plan.name}" if self.on_host and (self.plan.licences or {}).get("names") else ""

    def _licence_trap(self) -> str:
        """The detached shell's first words when it may hold a licence file:
        the folder removed when it ends, by itself or by a Stop's signal."""
        d = self._licence_dir()
        return f'trap "rm -rf {d}" EXIT; trap "exit 143" TERM HUP INT; ' if d else ""

    def _licence_line(self) -> str:
        """The benchmark's licences, as the one line a host's run reads
        (base64 of {name: base64 bytes}): only the names its environments declare, only
        those set - a data provider's token from that provider's store - and nothing
        at all for a run on this machine, which reads the store itself."""
        lic = self.plan.licences or {}
        if not self._licence_dir():
            return ""
        names = [n for n in lic.get("names") or [] if isinstance(n, str)]
        providers = lic.get("providers") if isinstance(lic.get("providers"), dict) else {}
        if self._licences_given is not None:
            got = {n: v for n, v in self._licences_given.items() if n in names and v}
        else:
            try:
                got = {n: secrets.value(*secrets.store_of(lic.get("benchmark") or "", n, providers.get(n) or "")) for n in names}
            except ValueError:
                got = {}
            got = {n: v for n, v in got.items() if v}
        for v in got.values():
            try:
                text = v.decode().strip()
            except UnicodeDecodeError:
                continue
            if "\n" not in text and text not in self._redact:
                self._redact.append(text)
        if not got:
            return ""
        return base64.b64encode(json.dumps({n: base64.b64encode(v).decode() for n, v in sorted(got.items())},
                                           separators=(",", ":")).encode()).decode()

    def _line_break(self) -> Optional[str]:
        """Why the run's key or endpoint cannot be sent, or None: one that
        holds a line break, which the launcher, reading each as one line
        (``_launch``), would take in pieces. Settings refuses one, so only
        the environment Nullius was started in can hold it. The variable is
        named, never its value."""
        for value, var in ((self._key, self.plan.key_var), (self._url, self.plan.url_var or "the endpoint"), *((v, n) for n, v in self._agent_keys.items())):
            if "\n" in value or "\r" in value:
                return (f"{var} holds a line break; a key or an endpoint is one line, and the run's launcher would take this one "
                        f"in pieces. Set it again on one line in the environment Nullius was started in (Settings refuses a line break)")
        return None

    def _launch(self, part: dict) -> Optional[bool]:
        """Start one part detached on the target and keep its pid. The key and
        endpoint arrive on stdin, become environment for the launcher, and the
        detached shell inherits them - nothing in argv, nothing on disk. They
        are two lines and ``STDIN_END`` a third: either may be empty (a
        recipe of controls needs no key, OpenAI's clients no endpoint), so a
        stdin cut short - Nullius gone as it sent them, a dropped ssh - is
        told by the end line missing, never by an empty value. Without it the
        launcher answers ``@cut`` and does nothing more, the part neither
        claimed nor started, and the run fails at that part. The
        launcher claims the part first (``run-<part>.launch``, created only
        where there is none), and the detached shell writes its own pid
        beside its log before it runs the script: Nullius gone before the pid
        is saved here, a restart finds the claim, the pid or the process
        there (``_settle``) and never starts the part a second time - nor
        does a launcher that reaches the target after the restart's: it finds
        the part claimed, says so and starts nothing, and a restart that
        meets that answer follows the part.

        A key or endpoint that holds a line break is never sent: it would
        reach the launcher in pieces and read as a stdin cut short. A new
        run refuses it before anything is sent (``_main``); a restored one,
        whose key and endpoint are read again, here, before the part.

        True once launched, False when the launch failed (the run is failed),
        None when a Stop was said before it began: nothing is started, and the
        caller ends the run as the Stop records it. The check and the
        ``running`` it sets hold the lock ``stop`` takes, so a Stop is never
        written over."""
        broken = self._line_break()
        if broken:
            self._fail("credentials", broken)
            return False
        with self._turn:                                 # the Stop takes this lock too: said before this, nothing is launched
            if self._stop.is_set():
                return None
            self.state, self._live, self.launched = "running", part, part["id"]
        where = self.plan.host.label if self.on_host else "this machine"
        self._begin("run", f"running {part['label']} on {where}")
        run_dir = self._run_dir()
        script = _base(part) + ".sh"
        rc, out = self._sh(f"cat > {self._q(run_dir + '/' + script)}", 30, stdin=part["script"] if part["script"].endswith("\n") else part["script"] + "\n")
        if rc != 0:
            self._fail("run", f"the script did not land on {where}: " + out.strip()[-400:])
            return False
        prices_on_target = ""
        if self.plan.runner == STANDARD and self.on_host:
            # the price list is this machine's user's, so the run takes it along (nullius.prices) - an empty one
            # too: a host's own list, if it has one, never prices a run started here
            prices_on_target = run_dir + "/prices.json"
            rc, out = self._sh(f"cat > {self._q(prices_on_target)}", 60, stdin=json.dumps(price_list.load()) + "\n")
            if rc != 0:
                self._fail("run", f"the price list did not land on {where}: " + out.strip()[-400:])
                return False
        exports = " ".join(f"{k}={self._q(v) if v.startswith('~') else shlex.quote(v)}" for k, v in
                           [("NULLIUS_HARBOR", self.harbor), ("NULLIUS_TASKS", self.tasks_on_target), ("NULLIUS_JOBS", run_dir + "/jobs"),
                            ("NULLIUS_PROGRESS", "1")]
                           + ([("NULLIUS_STANDARD", self.app_on_target or REPO), ("PYTHONDONTWRITEBYTECODE", "1")]
                              if self.plan.runner == STANDARD else [])
                           + ([("NULLIUS_CATALOGUE", self.catalogue_on_target)]
                              if self.plan.runner == STANDARD and self.catalogue_on_target else [])
                           + ([("NULLIUS_PRICES", prices_on_target)] if prices_on_target else [])
                           + ([("NULLIUS_PYTHON", sys.executable)] if self.plan.runner == STANDARD and not self.on_host else [])
                           + ([("NULLIUS_LICENCE_DIR", self._licence_dir())] if self._licence_dir() else [])
                           + ([("API_MODEL_NAME", self.plan.model)] if self.plan.model else []))
        base = script[:-3]
        # macOS has no setsid executable. Python can create the same session and
        # exec in place, keeping $!, the pid file and the process group identical.
        python = shlex.quote("python3" if self.on_host else sys.executable)
        session = shlex.quote("import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])")
        detach = (f"if ! command -v setsid >/dev/null 2>&1; then "
                  f"command -v {python} >/dev/null 2>&1 || {{ echo 'detached runs need setsid or Python 3'; exit 1; }}; "
                  f"setsid() {{ exec {python} -c {session} \"$@\"; }}; fi; ")
        # stdin: the key, the endpoint, then one NAME=value line per agent key, then the end line - which alone says it all arrived
        launcher = ('IFS= read -r K && IFS= read -r U || { echo @cut; exit 1; }; E=; '
                    'while IFS= read -r L; do [ "$L" = ' + STDIN_END + ' ] && { E=1; break; }; '
                    'case "$L" in [A-Z_]*=*) export "$L";; *) echo @cut; exit 1;; esac; done; [ -n "$E" ] || { echo @cut; exit 1; }; unset E L; '
                    '[ -n "$K" ] && export ' + " ".join(f'{n}="$K"' for n in KEY_NAMES) + '; '
                    '[ -n "$U" ] && export ' + " ".join(f'{n}="$U"' for n in URL_NAMES) + '; unset K U; '
                    f'export {exports}; cd {self._q(run_dir)} || exit 1; '
                    + detach +
                    # The part claimed before anything else is done for it, created only where there is none (noclobber:
                    # O_EXCL): of two launchers - one a Nullius gone since sent, late to arrive, and the restart's - one
                    # starts the part and the other says so and starts nothing. From the claim on, this launcher runs
                    # in the run directory until the part has started, which is how a restart tells it is on its way.
                    f'( set -C; : >{base}.launch ) 2>/dev/null || {{ echo @claimed; exit 0; }}; rm -f {base}.exit {base}.pid; '
                    # `&` binds to the setsid command alone (hence `;` before it): $! is the detached shell, a session
                    # leader of its own, with nothing of the launcher's pipes, so the launcher returns at once. Its
                    # first act is its pid ($$, the same $!) on disk, before the log opens; its second, a look for a
                    # Stop that came while its launcher had not answered (_kill_part).
                    f"setsid nohup bash -c '{self._licence_trap()}echo $$ >{base}.pid; [ -e {base}.stop ] && exit 130; bash {script} </dev/null >{base}.log 2>&1; echo $? >{base}.exit' "
                    f"</dev/null >/dev/null 2>&1 & echo $!")
        rc, out = self._sh(launcher, 60, stdin=self._stdin())
        if rc == 124 and not self._stop.is_set():
            # An ssh that stalled says nothing of whether the launcher ran. Asking again is safe: the claim above
            # lets one launcher start the part, and a second that finds it says @claimed and starts nothing.
            self.log(f"  {where} did not answer the launch within 60 s; asking once more")
            rc, out = self._sh(launcher, 60, stdin=self._stdin())
            if rc == 124:
                out = f"{where} did not answer the launch, twice, within 60 s each; nothing is known to have started there"
        if "@cut" in out.split():
            self._fail("run", f"{part['label']} did not start on {where}: the key and endpoint reached its launcher cut short; nothing was started")
            return False
        if rc == 0 and "@claimed" in out.split():
            self.log(f"  {part['label']} was started already, by a launcher sent before Nullius was gone; following it")
            self.pid, self.log_offset = None, 0
            self._save()
            return True
        pid = next((int(l) for l in out.strip().splitlines() if l.strip().isdigit()), None)
        if rc != 0 or not pid:
            self._fail("run", f"{part['label']} did not start on {where}: " + out.strip()[-400:])
            return False
        self.pid, self.log_offset = pid, 0
        self._save()
        return True

    def _follow(self, part: dict) -> int:
        """Look at the running part every few seconds - its log, the mirror,
        the trials - until its exit code lands; a part with no process left
        and no exit code is gone, a failure of its own."""
        base = _base(part)
        # Alive while a process runs its script in its run directory (_part_pids): its pid when known, else the
        # one it wrote or one found by its command line - a part an older launcher started has no pid file - and
        # never a pid that has since gone to another process.
        alive = f"[ -n \"$({_part_pids(base, [self.pid])})\" ] && echo alive || echo gone"
        while True:
            # Read in the reverse of the order the target writes them (the log, the exit code, the process
            # ends): an exit code seen means the whole log is there, a process gone means none is coming.
            rc, out = self._sh(f"cd {self._q(self._run_dir())} 2>/dev/null; s=$({alive}); [ -f {base}.exit ] && s=$(cat {base}.exit); "
                               f"{self._log_since(base)}; printf '\\n__nullius__ %s' \"$s\"", 120)
            head, _, status = out.rpartition("\n__nullius__ ")
            if rc == 0:
                text, size = self._logged(head)
                if size is not None and size > self.log_offset:
                    self.log_offset = size
                    for line in text.splitlines():
                        self._read(line)
                    self._save()
                status = status.strip()
                if self.on_host:
                    self._mirror()
                else:
                    self._trial_lines()
                if status.isdigit():
                    return int(status)
                if status == "gone":
                    self.log(f"  {part['label']} is gone without an exit code")
                    return 1
            if self._stop.wait(SYNC_EVERY):
                return 130

    def _mirror(self, run: Optional[Callable] = None, timeout: float = 900.0) -> None:
        """The job copied back from the host, and the trials that ended said;
        ``run`` and ``timeout`` for the last copy a Stop makes (``_done``)."""
        rc, out = self._copy_back(run, timeout)
        if rc != 0:
            self.log("  the job did not sync back: " + out.strip()[-300:])
        self._trial_lines()


def _probe(run: Callable, argv: List[str], timeout: float) -> tuple:
    """hosts.test wants (rc, stdout, stderr); the execution runner gives (rc, output)."""
    rc, out = run(argv, timeout)
    return rc, out, ""


# --- the registry the page reads --------------------------------------------

EXECUTIONS: Dict[str, Execution] = {}


def start(plan: Plan, **kw) -> Execution:
    ex = Execution(plan, **kw)
    EXECUTIONS[ex.id] = ex
    return ex.start()


def active() -> List[Execution]:
    return [e for e in EXECUTIONS.values() if e.state in ALIVE]


def for_job(job_id: str) -> Optional[Execution]:
    """The newest execution whose script writes this job, if any - the one
    the job page shows beside the job, or instead of it while Harbor has
    not written it yet."""
    found = [e for e in EXECUTIONS.values() if any(str(j.get("name")) == job_id for j in e.plan.jobs)]
    return max(found, key=lambda e: e.started) if found else None


ENDED = ("stopped", "failed", "done")


def ended_for(job_id: str) -> Optional[tuple]:
    """``(state, finished_at)`` when the run that writes this job has ended
    - stopped, failed or done - by the runner's own record; None while it
    is alive or when no run of this application wrote the job. A Harbor
    killed by a Stop leaves its feed without ``job.end`` and its result
    record with trials "running", so a page reading either alone would say
    the job goes on; this is the fact that says otherwise."""
    ex = for_job(job_id)
    return ex.job_state(job_id) if ex is not None else None


def restore(root: Optional[str] = None, **kw) -> List[Execution]:
    """At start-up: every recorded run under the root. The ones still alive
    are reattached (their thread follows the part that was running, or
    waits at the pause it was at); the finished ones are listed as they
    ended. Returns the ones reattached."""
    root = root or runs_root(kw.get("env", os.environ))
    back = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return back
    for name in names:
        path = os.path.join(root, name, RECORD)
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
            plan = Plan.from_dict(rec["plan"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        ex = Execution(plan, root=root, record=rec, **kw)
        EXECUTIONS[ex.id] = ex
        if ex.state in ALIVE:
            back.append(ex.resume())
    return back
