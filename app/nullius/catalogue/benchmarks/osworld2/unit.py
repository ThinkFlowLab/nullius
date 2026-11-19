"""What OSWorld2's two drivers share: the original's (driver.py) and osworld2@hardened's (hardened/driver.py).

The unit's ids and the dataset lists, the upstream's paths, reading its logs
(why a run ended, its setup errors, the grader's records), the unit's pipe, the
errors a score or setup raises, and the processes' liveness and api_logs packing.
Nothing here patches or calls the upstream; each driver keeps its own docker
calls, reaper and run loop."""
import builtins
import contextlib
import glob
import json
import os
import re
import signal
import time

PROVIDER_IMAGE = "happysixd/osworld-docker@sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9"

RESULTS = "results"

STARTED = "Started container with ports"

START_SECONDS = 1000

GRACE = 5

DOCKER_SECONDS = 60

VOLUMES = "volumes"

VOLUME = re.compile(r"[0-9a-f]{64}")

NOTE_SECONDS = 5

SNAPSHOT_SECONDS = 5

NOTE_BYTES = 65536

CLOSE_SECONDS = 60

SETUP_FAILED = "setup_failed.json"

COLOUR = re.compile(r"\x1b\[[0-9;]*m")

FRAME = re.compile(r'^  File ".*[/\\]desktop_env[/\\]desktop_env\.py", line \d+, in (\w+)$')

INNER = re.compile(r'^  File "(?:.*[/\\])?([^/\\"]*)", line \d+, in (\S+)$')

LISTS = {
    "offline36": "001 002 004 010 012 022 029 030 040 042 044 045 047 048 049 051 053 054 059 061 063 064 066 067 "
                 "076 080 084 085 088 094 103 104 105 106 107 108".split(),
    "no_services63": "001 002 003 004 006 009 010 011 012 014 017 022 023 024 025 027 029 030 032 033 034 040 "
                     "042 044 045 047 048 049 051 053 054 059 061 063 064 066 067 071 074 076 078 080 083 084 "
                     "085 086 087 088 089 090 092 093 094 095 096 097 099 103 104 105 106 107 108".split(),
    "websites35": "005 007 008 013 015 016 018 019 020 021 028 031 035 038 039 043 046 052 057 058 060 065 068 069 "
                  "070 072 073 077 079 081 082 091 100 101 102".split(),
}

JUDGED = frozenset("003 007 008 019 025 031 035 041 070 074 075 077 078 079 081 082 087 089 090 092 093".split())

ASKS_USER = frozenset("007 024 026 034 095 098".split())

USER_ROUTE = "OSWORLD_USER_SIM_BASE_URL"

GRADER_FAILED = ("Error in compare_images_with_llm: ", "Error in compare_image_edit_with_llm: ",
                 "Error in _compare_answers_with_llm: ", "Error in compare_text_with_llm: ")

JUDGE_ROUTE = re.compile(r"/([A-Za-z0-9_-]{1,64}-judge)/v1/?$")

IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")

IMAGE_BYTES = 5 * 1024 * 1024

GRADER_STATUS = re.compile(r"\b(?:Error code|[Ss]tatus(?: code)?|HTTP)\s*[:=]?\s*([1-5][0-9]{2})\b")

GRADER_NAME = re.compile(r"^((?:[A-Za-z_]\w*\.)*)([A-Za-z_]\w*)(?=:|\s|$)")

_SDK = ("APIError APIStatusError APIConnectionError APITimeoutError APIResponseValidationError BadRequestError "
        "AuthenticationError PermissionDeniedError NotFoundError ConflictError UnprocessableEntityError RateLimitError "
        "InternalServerError OverloadedError ServiceUnavailableError DeadlineExceededError "
        "LengthFinishReasonError ContentFilterFinishReasonError").split()

_HTTPX = ("HTTPError RequestError TransportError TimeoutException ConnectTimeout ReadTimeout WriteTimeout PoolTimeout "
          "NetworkError ConnectError ReadError WriteError CloseError ProtocolError LocalProtocolError RemoteProtocolError "
          "ProxyError UnsupportedProtocol DecodingError TooManyRedirects HTTPStatusError InvalidURL").split()

_REQUESTS = ("RequestException HTTPError ConnectionError ProxyError SSLError Timeout ConnectTimeout ReadTimeout "
             "URLRequired TooManyRedirects MissingSchema InvalidSchema InvalidURL InvalidHeader ChunkedEncodingError "
             "ContentDecodingError StreamConsumedError RetryError JSONDecodeError").split()

_BUILTIN = sorted(n for n, v in vars(builtins).items() if isinstance(v, type) and issubclass(v, BaseException))

GRADER_CLASSES = {"anthropic": _SDK, "openai": _SDK, "httpx": _HTTPX, "requests": _REQUESTS,
                  "requests.exceptions": _REQUESTS, "builtins": _BUILTIN,
                  "": sorted(set(_SDK + _HTTPX + _REQUESTS + _BUILTIN))}

GRADER_RECORD = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:,\d+)? ERROR "
                           r"(?:llm_metrics/\d+-[^\]]*|desktopenv\.env)\] (Error in \w+: )(.*)$")

HEAD = r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:,\d+)? \w+ "

SET_UP = re.compile(HEAD + r"[^\]]*\] Environment (?:has been used, reverting to snapshot|is clean, skipping snapshot revert)")

SET_UP_DONE = re.compile(HEAD + r"[^\]]*\] Environment setup complete\.$")

SETUP_ERROR = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:,\d+)? ERROR (?:desktopenv\.)?setup(?:/\d+-[^\]]*)?\] (.*)$")

SETUP_RETRY = re.compile(r"retrying|Upload attempt \d+ failed|Failed to upload file \S", re.I)

STEPPED = re.compile(HEAD + r"(?:lib_run_single|desktopenv\.experiment)(?:/\d+-[^\]]*)?\] "
                     r"(?:Done:|(?:Phase \d+ )?(?:Step (\d+)|User simulator Q):)")

SIGNALLED = re.compile(HEAD + r"[^\]]*-MainProcess\] Received signal (\d+)\. Gracefully shutting down\.\.\.$")

MAIN_ERROR = re.compile(HEAD + r"[^\]]*-MainProcess\] Unexpected error in main process: ")

WORKER = re.compile(HEAD + r"[^\]]*\] (?:Process (\S+) started\.|(\S+) cleaning up environment\.\.\.)$")

SCREENSHOT = re.compile(HEAD + r"(?:desktopenv\.pycontroller|python)(?:/\d+-[^\]]*)?\] "
                        r"(Got screenshot successfully|Failed to get screenshot\.)$")

RUNNER = re.compile(HEAD + r"(?:lib_run_single/\d+-[^\]]*|desktopenv\.experiment)\] (?:(?P<step>Done:|(?:Phase (?P<n>\d+) )?(?:Step \d+|User simulator Q):)"
                    r"|(?P<phase>Starting phase \d+ )|(?:Multi-phase r|R)esult: -?\d)")

SETUP_DOC = "docs/benchmarks/osworld2-machine.md"

PACK_LOG = "api_logs.log"



def _unit(unit):
    domain, _, eid = unit.partition("__")
    return domain, eid



def units(dataset=None, setup=None):
    if not os.environ.get("NULLIUS_DATASET"):
        name = os.environ.get("NULLIUS_DATASET_ID") or dataset or ""
        if name not in LISTS:
            raise RuntimeError(f"no tasks file for the dataset {name or '(none)'}, and no list of its own")
        return {"units": [f"tasks__{e}" for e in LISTS[name]]}
    with open(os.environ["NULLIUS_DATASET"], encoding="utf-8") as f:
        meta = json.load(f)
    return {"units": [f"{d}__{e}" for d, ids in meta.items() for e in ids]}



def _alive(pgid):
    """A process that waits for its parent to see its exit does not count, so a stopped script ends its group."""
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return True
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
                state, _, group = f.read().rpartition(")")[2].split()[:3]
        except (OSError, ValueError):
            continue
        if group == str(pgid) and state not in ("Z", "X"):
            return True
    return False



def _gone(pgid, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if not _alive(pgid):
            return True
        time.sleep(0.2)
    return False



def _pack(dir):
    """A failure leaves the folder as it is and the unit's result alone; each outcome is a line in PACK_LOG."""
    try:
        folders = glob.glob(os.path.join(glob.escape(dir), RESULTS, "**", "api_logs"), recursive=True)
        if folders:
            import api_logs
    except Exception as e:
        _packed(dir, f"api_logs not packed: {type(e).__name__}: {e}")
        return
    for folder in folders:
        try:
            done = api_logs.pack(folder)
        except Exception as e:
            done = {"error": f"{type(e).__name__}: {e}"}
        _packed(dir, f"{os.path.relpath(folder, dir)} {json.dumps(done)}")



def _packed(dir, line):
    try:
        fd = os.open(os.path.join(dir, PACK_LOG), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + line + "\n")
    except OSError:
        pass



class EnvironmentSetupError(RuntimeError):
    """The null control's setup failed for good: the score's error, in the driver's own words only."""



class CancelledError(RuntimeError):
    pass



class CauseUnknownError(RuntimeError):
    """Neither the agent's nor the environment's, as far as the log shows."""



class VerifierError(RuntimeError):
    """The task's evaluate() raised: the score's error."""



class NoAnswer(RuntimeError):
    """A docker command its daemon did not answer in time."""



def _setup_failed(failed):
    """Types and attempts only, never the message, which could quote the task."""
    try:
        with open(failed, encoding="utf-8") as f:
            said = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        said = {}
    said = said if isinstance(said, dict) else {}
    cause = said.get("cause")
    return (f"the environment's setup failed ({said.get('error') or 'no type'}"
            + (f" from {cause}" if cause else "") + f", after {said.get('attempts') or '?'} attempt"
            + ("" if said.get("attempts") == 1 else "s") + "), and the task was not scored")



def _tail(log, code=0):
    """The error class the log's last traceback makes, and that traceback's exception by a known class's name only.
    A runner killed, or ended by a signal it did not send itself, is the environment's; one whose worker process never
    reached its cleanup, or that died of another signal with no traceback, is of no known cause."""
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            lines = [COLOUR.sub("", ln).rstrip() for ln in f.read().splitlines()]
    except OSError:
        return RuntimeError, f"no log at {log}"
    start = max((i for i, ln in enumerate(lines) if ln.endswith("Traceback (most recent call last):")), default=len(lines))
    if any(map(RUNNER.match, lines[start:])):
        start = len(lines)
    signalled = [m.group(1) for m in map(SIGNALLED.match, lines) if m]
    if code in (-signal.SIGKILL, -signal.SIGTERM, -signal.SIGHUP) or code >= 0 and signalled and not any(map(MAIN_ERROR.match, lines)):
        return EnvironmentSetupError, f"ended by signal {-code if code < 0 else signalled[-1]} (the whole log: {log})"
    workers = [m.groups() for m in map(WORKER.match, lines) if m]
    if code == 0 and {a for a, _ in workers if a} - {b for _, b in workers} or code < 0 and start == len(lines):
        return CauseUnknownError, (f"{f'ended by signal {-code}, with no traceback' if code else 'its worker process ended without its cleanup'}"
                                   f" (the whole log: {log})")
    block = lines[start:]
    raised = next((ln for ln in block[1:] if ln and not ln[0].isspace()), "")
    named = GRADER_NAME.match(raised)
    module, name = (named.group(1).rstrip("."), named.group(2)) if named else ("", "")
    frames = {m.group(1) for m in map(FRAME.match, block) if m}
    said = name if name == "EnvironmentSetupError" or name in GRADER_CLASSES.get(module.replace("._exceptions", ""), ()) \
        else "an exception of the upstream's own" if name else "no exception named"
    kind = EnvironmentSetupError if name == "EnvironmentSetupError" or frames & {"reset", "_start_emulator", "__init__"} \
        else VerifierError if frames & {"evaluate", "_call_task_evaluate"} else RuntimeError
    shots = [m.group(1) for m in map(SCREENSHOT.match, lines[:start]) if m]
    if kind is RuntimeError and name and shots[-1:] == ["Failed to get screenshot."]:
        inner = ([m.groups() for m in map(INNER.match, block) if m] or [("", "")])[-1]
        step = [m.group(1) for m in map(STEPPED.match, lines[:start]) if m and m.group(1)][-1:]
        where = f"in step {step[0]}" if step else "before the first step"
        if name == "UnidentifiedImageError" or inner[1] == "_screenshot_bytes" \
                or name == "TypeError" and "NoneType" in raised and inner[0] == "lib_run_single.py":
            return EnvironmentSetupError, f"the VM stopped answering {where}: its last screenshot failed (the whole log: {log})"
        return CauseUnknownError, f"{said}, after the VM's last screenshot failed {where} (the whole log: {log})"
    return kind, f"{said} (the whole log: {log})"



def _base(url):
    return url[:-3] if url.endswith("/v1") else url



def _graded(eid, judge):
    """The LLM grader's settings from the run's judge route; a judged task with no route is refused before its VM starts."""
    if not judge:
        if eid in JUDGED:
            raise RuntimeError(f"task {eid} is scored by the LLM grader and this run gives it no judge route: "
                               "run it with the benchmark's own judge")
        return {}
    if not isinstance(judge, dict) or not all(isinstance(judge.get(k), str) and judge[k] for k in ("model", "base_url", "key")):
        raise RuntimeError("the judge route needs a model, a base_url and a key")
    return {"OSWORLD_EVAL_MODEL_PROVIDER": "anthropic", "OSWORLD_EVAL_MODEL_NAME": judge["model"],
            "OSWORLD_EVAL_MODEL_BASE_URL": _base(judge["base_url"]), "OSWORLD_EVAL_MODEL_API_KEY": judge["key"]}



def _asks_user(eid, name, spec):
    """A task with a simulated user and no route for it is refused before its VM starts."""
    if eid in ASKS_USER and not spec.get("env", {}).get(USER_ROUTE):
        raise RuntimeError(f"task {eid} has a simulated user and the {name} agent gives it no route: "
                           f"set {USER_ROUTE} in its agents.json entry")



def _nat():
    try:
        with open("/proc/modules", encoding="utf-8") as f:
            if any(ln.split(" ", 1)[0] == "iptable_nat" for ln in f):
                return True
    except OSError:
        pass
    return os.path.isdir("/sys/module/iptable_nat")



def _fill(value, slots):
    for k, v in slots.items():
        value = value.replace("{" + k + "}", v)
    return value



def _result(dir, domain, eid):
    found = glob.glob(os.path.join(dir, RESULTS, "**", domain, eid, "result.txt"), recursive=True)
    return os.path.dirname(found[0]) if found else None



def _traj(where, name="traj.jsonl"):
    try:
        with open(os.path.join(where, name), encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError:
        return []
    out = []
    for ln in lines:
        with contextlib.suppress(ValueError):
            out.append(json.loads(ln))
    return out



def _stop_file(dir):
    return os.path.join(os.path.dirname(os.path.abspath(dir)), ".work", "relay", "stop")



def _setup_errors(*logs):
    """The upstream's setup errors it logged and went on from, from the last attempt to set the task up to the last step,
    or to that setup's end with no step: none retried, none of the evaluation's own setup."""
    for log in logs:
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                lines = [COLOUR.sub("", ln).rstrip("\n") for ln in f]
        except OSError:
            continue
        start = max((i for i, ln in enumerate(lines) if SET_UP.match(ln)), default=None)
        if start is not None:
            end = max((i for i, ln in enumerate(lines) if i > start and STEPPED.match(ln)), default=None) or next(
                (i for i, ln in enumerate(lines) if i > start and SET_UP_DONE.match(ln)), len(lines))
            errors = [m for m in map(SETUP_ERROR.match, lines[start:end]) if m and not SETUP_RETRY.search(m.group(1))]
            return {"setup_errors": len(errors)} if errors else {}
    return {}



def _grader_cause(said):
    """A grader error record's HTTP status and its class from GRADER_CLASSES, never its words, which can quote the task."""
    said = said.strip()
    status, name = GRADER_STATUS.search(said), GRADER_NAME.match(said)
    module = name.group(1).rstrip(".") if name else ""
    module = module[:-len("._exceptions")] if module.endswith("._exceptions") else module
    known = name and name.group(2) in GRADER_CLASSES.get(module, ())
    found = ([f"{module}.{name.group(2)}" if module else name.group(2)] if known else []) \
        + ([f"HTTP {status.group(1)}"] if status else [])
    return ", ".join(found) or "no HTTP status or known error class in its record"



def _refusal(call):
    """A judge call refused for the agent's file or its size, by the images its request carried (the relay's ``images``),
    never by the provider's words."""
    images = [i for i in call.get("images") or () if isinstance(i, dict)]
    if call.get("status") in (400, 413, 422) and any(type(i.get("bytes")) is int and i["bytes"] > IMAGE_BYTES for i in images):
        return "agent file size"
    if call.get("status") in (400, 422) and any(i.get("is") not in IMAGE_TYPES for i in images):
        return "agent file"
    return None
