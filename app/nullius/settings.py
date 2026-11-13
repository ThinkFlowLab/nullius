"""The application's settings: every environment variable it reads, in one
table, and the one file it writes them to.

The user configuration ``settings.json`` is loaded at startup (``load``) with the rule the application has
always had — a variable already in the process environment wins over the
file — and read again (``refresh``) whenever the file has changed on disk
(``sync``, which the application calls before each request), so a file
edited by hand while the application runs counts from the next request,
without a restart; the Settings page edits
the same file (``save``) and names the keys in it that nothing reads (``unread``). What the page
shows for each variable is its *state*, never a secret's value: ``set`` from
the environment or from the file, or ``not set``, with a fixed mask and nothing else. Values are validated per variable before a byte is written;
the file is written whole, to a temporary name first, with mode 0600; a
variable the process environment holds is shown read-only, because the file
could not override it anyway.

``example`` renders ``app/.env.example`` from the same table, so the two
cannot drift (a test compares them).
"""
from __future__ import annotations

import os
import json
import re
import tempfile
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

_PATH_OK = re.compile(r"^[^\x00-\x1f]{1,400}$")
_URL_OK = re.compile(r"^https?://[^\s/?#]+(?:[/?#]\S*)?$")
_REPO_OK = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NAME_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SECRET_OK = re.compile(r"^[!-~]+$")   # printable ASCII, no spaces
_LOCAL_KEY_OK = re.compile(r"^[!-~]+$")   # the same, any length: a local server's key is often a word ("ollama", "EMPTY")
_TEXT_OK = re.compile(r"^[^\x00-\x1f]{1,400}$")
_MODEL_OK = re.compile(r"^~?[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$")     # an OpenRouter model id: [~]vendor/model[:variant]
_S1_OK = re.compile(r"^(typesafe|openrouter)$")
_LLM_JUDGE_OK = re.compile(r"^(openrouter|openai|local(:[A-Z0-9]+(_[A-Z0-9]+)*)?)$")
_ENVNAME_OK = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_BOOL = {"0", "1", "true", "false", "yes", "no"}


def _check(pattern: re.Pattern, what: str) -> Callable[[str], Optional[str]]:
    return lambda v: None if pattern.match(v) else what


def _paths(v: str) -> Optional[str]:
    return None if v and all(_PATH_OK.match(p) for p in v.split(os.pathsep)) else \
        f"one or more paths separated by {os.pathsep!r}, without control characters"


# Names an agent key may not take: the run's own, the shell's, and the ones that steer a process
_NOT_AGENT_KEYS = {"API_TOKEN", "OPENAI_API_KEY", "LLM_API_KEY", "API_URL", "OPENAI_BASE_URL", "LLM_BASE_URL", "API_MODEL_NAME",
                   "PATH", "HOME", "USER", "SHELL", "PWD", "LANG", "TERM", "TMPDIR", "PYTHONPATH", "PYTHONHOME",
                   "LD_PRELOAD", "LD_LIBRARY_PATH", "SSH_AUTH_SOCK"}


def reserved_name(n: str) -> bool:
    """A name that is already something else's: a setting, a start-up option,
    another spelling of one, the run's own or the shell's. An agent key or a
    benchmark's secret may not take it."""
    return n in BY_NAME or n in STARTUP or n in INSTEAD or n in _NOT_AGENT_KEYS or n.startswith("NULLIUS_") or bool(_ENDPOINT_VAR.match(n))


def _agent_key_names(v: str) -> Optional[str]:
    names = [n.strip() for n in v.split(",") if n.strip()]
    for n in names:
        if not _ENVNAME_OK.match(n):
            return f"names in capitals, digits and underscores ({n!r} is not)"
        if reserved_name(n):
            return f"names of their own ({n} is already the name of something else)"
    if len(set(names)) != len(names):
        return "names, each once"
    return None


# Query parameters that carry a credential, whatever the service calls them
_CREDENTIAL_PARAM = re.compile(r"^(key|api[_-]?key|token|access[_-]?token|secret|password|auth|authorization|sig|signature)$", re.I)
_PUT_KEY = "put the key in the Key field"


def endpoint_url_problem(v: str) -> Optional[str]:
    """Why ``v`` cannot be an endpoint's URL, or None: an http(s) URL with a host, and no
    credential in it - no user or password, no query parameter named like a key - since
    the page shows it."""
    try:
        p = urllib.parse.urlsplit(v)
        p.port
    except ValueError:
        return "an http(s) URL with a host"
    if p.scheme not in ("http", "https") or not p.hostname or any(c.isspace() or ord(c) < 32 for c in v):
        return "an http(s) URL with a host"
    if "@" in p.netloc:
        return f"a URL without a user or password in it: {_PUT_KEY}"
    if any(_CREDENTIAL_PARAM.match(k) for k, _ in urllib.parse.parse_qsl(p.query, keep_blank_values=True)):
        return f"a URL without a key in its query: {_PUT_KEY}"
    return None


def endpoint_url_shown(v: str) -> str:
    """An endpoint's URL as the page shows it: as it is, or, when it holds a credential,
    with the user, the password and each key-like query value hidden."""
    if not endpoint_url_problem(v):
        return v
    try:
        p = urllib.parse.urlsplit(v)
    except ValueError:
        return SECRET_SHOWN
    netloc = ("[REDACTED]@" if "@" in p.netloc else "") + p.netloc.rpartition("@")[2]
    query = "&".join(f"{k}=[REDACTED]" if _CREDENTIAL_PARAM.match(k) else f"{k}={w}"
                     for k, w in urllib.parse.parse_qsl(p.query, keep_blank_values=True))
    return urllib.parse.urlunsplit((p.scheme, netloc, p.path, query, p.fragment))


def _hosts(v: str) -> Optional[str]:
    from nullius import hosts
    return hosts.validate_list(v)


def _constraints(v: str) -> Optional[str]:
    from nullius import constraints
    return constraints.validate_setting(v)


def _bool(v: str) -> Optional[str]:
    return None if v.lower() in _BOOL else "0, 1, true, false, yes or no"


@dataclass(frozen=True)
class Variable:
    name: str
    group: str
    purpose: str
    kind: str            # secret | localkey | endpoint | path | paths | url | repo | name | bool | text | envname | hosts | constraints | model | llmprovider | s1provider | envnames
    example: str = ""    # shown as a placeholder, written to .env.example as a comment

    @property
    def secret(self) -> bool:
        return self.kind in ("secret", "localkey")

    def validate(self, value: str) -> Optional[str]:
        """None when acceptable, else what the value should have been."""
        if "\n" in value or "\r" in value:
            return "a single line"
        if value != value.strip():
            return "no leading or trailing whitespace"
        return {
            "secret": _check(_SECRET_OK, "a nonempty single-line value without spaces"),
            "localkey": _check(_LOCAL_KEY_OK, "a nonempty single-line value without spaces"),
            "path": _check(_PATH_OK, "a path without control characters"),
            "paths": _paths,
            "url": _check(_URL_OK, "an http(s) URL"),
            "endpoint": endpoint_url_problem,
            "repo": _check(_REPO_OK, "owner/name"),
            "name": _check(_NAME_OK, "letters, digits, dots, dashes or underscores (64 at most)"),
            "bool": _bool,
            "text": _check(_TEXT_OK, "a single line without control characters"),
            "envname": _check(_ENVNAME_OK, "an environment variable name (capitals, digits, underscores)"),
            "hosts": _hosts,
            "constraints": _constraints,
            "model": _check(_MODEL_OK, "a model id as OpenRouter lists it, vendor/model"),
            "llmprovider": _check(_LLM_JUDGE_OK, "openrouter, openai, local or local:<endpoint id>"),
            "s1provider": _check(_S1_OK, "typesafe or openrouter"),
            "envnames": _agent_key_names,
        }[self.kind](value)


GROUPS = ["Model credentials", "Run hosts", "Custom keys"]
# The key a run on an OpenAI-compatible endpoint is sent with when the
# endpoint has no key: many such servers check none, but an
# OpenAI-compatible client refuses to start without one.
MODEL_KEY_PLACEHOLDER = "local"
# The OpenAI-compatible endpoint MODEL_API_URL and MODEL_API_KEY hold, by this name.
DEFAULT_ENDPOINT_NAME = "OpenAI-compatible"
# More OpenAI-compatible endpoints, each by an id: MODEL_<ID>_NAME, MODEL_<ID>_API_URL and
# MODEL_<ID>_API_KEY. A recipe records the id, not the name, so a rename keeps its recipes.
ENDPOINT_ID = re.compile(r"^(?=.{1,32}$)[A-Z0-9]+(?:_[A-Z0-9]+)*$")
_ENDPOINT_VAR = re.compile(r"^MODEL_([A-Z0-9]+(?:_[A-Z0-9]+)*)_(NAME|API_URL|API_KEY)$")
ENDPOINT_PARTS = ("NAME", "API_URL", "API_KEY")

# What a person sets. Everything else the application reads is a start-up
# option with a command-line form or a sensible default (STARTUP below) and
# is not on the page.
VARIABLES: List[Variable] = [
    Variable("MODEL_API_KEY", GROUPS[0], "The key of the OpenAI-compatible endpoint at MODEL_API_URL: a recipe on that endpoint is sent there with this key. Optional, for a server that checks none: without it the run is sent the word " + repr(MODEL_KEY_PLACEHOLDER) + " as its key. A run gets it as API_TOKEN, OPENAI_API_KEY and LLM_API_KEY in its environment - over stdin to a host - and never in a recipe or a log.", "localkey", "sk-…"),
    Variable("MODEL_API_URL", GROUPS[0], "The URL of an OpenAI-compatible endpoint, API path included, required for a recipe on it: there is no default to fall back on, and the builder offers the endpoint only once it is set. It is shown on the page, so it holds no credential: no user or password, no key in its query. More endpoints, each with a name, are MODEL_<ID>_NAME, MODEL_<ID>_API_URL and MODEL_<ID>_API_KEY. A run gets it as API_URL, OPENAI_BASE_URL and LLM_BASE_URL.", "endpoint", "https://…/v1"),
    Variable("OPENAI_API_KEY", GROUPS[0], "An OpenAI key, for a recipe whose provider is OpenAI: that run is sent to OpenAI's own endpoint, https://api.openai.com/v1, with this key - never with the model API key or its endpoint. It reaches the run as the model key does, and never a recipe or a log.", "secret"),
    Variable("OPENROUTER_API_KEY", GROUPS[0], "An OpenRouter key, for a recipe whose provider is OpenRouter: that run is sent to OpenRouter's own endpoint, https://openrouter.ai/api/v1, with this key - never with the model API key or its endpoint. It reaches the run as the model key does, and never a recipe or a log.", "secret"),
    Variable("ANTHROPIC_API_KEY", GROUPS[0], "An Anthropic key, for a run of the Nullius standard whose provider is Anthropic: its relay sends the calls to Anthropic's own endpoint, https://api.anthropic.com/v1, with this key. Only the relay holds it - never an agent, a recipe or a log.", "secret"),
    Variable("TYPESAFE_API_KEY", GROUPS[0], "TypeSafe.ai key for the System One judgement on TypeSafe's own endpoint, POST https://api.typesafe.ai/v1/systemone — sent as a bearer header there and nowhere else.", "secret", "ts-…"),
    Variable("SYSTEM_ONE_PROVIDER", GROUPS[0], "Where the System One judgement is asked: typesafe (its own endpoint, with TYPESAFE_API_KEY) or openrouter (https://openrouter.ai/api/v1/systemone, the same contract, with OPENROUTER_API_KEY). Unset: TypeSafe when its key is set, else OpenRouter when its key is.", "s1provider"),
    Variable("SYSTEM_ONE_MODEL", GROUPS[0], "The System One model, as the provider names it: on TypeSafe jev-latest, on OpenRouter ~typesafe/jev-latest, typesafe/jev-router, jaredpalmer/kev-4b... Unset: the provider's Jev.", "model"),
    Variable("LLM_JUDGE_PROVIDER", GROUPS[0], "The provider of the LLM judgement, the second judgement beside System One: openrouter, openai, local (the endpoint at MODEL_API_URL) or local:<ID> (the endpoint MODEL_<ID>_API_URL), asked with that provider's own key and endpoint from the model providers above. Unset: the first of them that is set.", "llmprovider"),
    Variable("LLM_JUDGE_MODEL", GROUPS[0], "The model of the LLM judgement, as its provider names it (on OpenRouter, vendor/model). It is asked the same questions as System One and answers them as JSON; its numbers are what it states, so a choice or a score carries no confidence.", "model"),
    Variable("AGENT_KEYS", GROUPS[2], "The names of the agent keys this file holds, comma-separated: the keys an agent needs of its own vendor, beside the model's (Cursor's CLI agent a CURSOR_API_KEY, Copilot's a GH_TOKEN). Each is a line of its own in this file. A Harbor run gets every one of them in its environment - over stdin to a host - and a Nullius standard run's never; none is ever written into a recipe or a log.", "envnames"),
    Variable("HOSTS", GROUPS[1], "Machines a recipe can run on, over ssh: an alias from ~/.ssh/config, or user@host[:port][:/path/to/key]. Only the name is stored; keys stay where they are.", "hosts", "user@host[:port][:/path/to/key]"),
    Variable("HOST_CONSTRAINTS", GROUPS[1], "What Nullius may use on a machine: a JSON object from local (this machine) or a host's entry to its constraints (cpus, memory_mb, processes, concurrency, cpu_set, gpu_ids). A constraint not set is no constraint.", "constraints", '{"local": {"cpus": 4, "memory_mb": 8192}}'),
]
# Read by the application but not offered on the page: each has a
# command-line form, a test-only use, or a default nobody changes. The
# server's address and port are read once, at start: the running server's
# own page could not change them.
STARTUP: Dict[str, str] = {
    "NULLIUS_HOME": "a separate profile with config/, data/ and cache/ directories",
    "NULLIUS_CATALOGUE": "the user catalogue directory (default catalogue/ in the user data directory)",
    "NULLIUS_PRICES": "the model price list file (default prices.json in the user configuration directory); set for a run on a host",
    "APP_HOST": "the address the server binds (default 127.0.0.1, or --host)",
    "APP_PORT": "the port the server listens on (default 5057, or --port)",
    "JOB_ROOTS": "job roots to open at start (or the command-line argument)",
    "WORKSPACES_DIR": "where workspaces are kept (default workspaces/ in the user data directory)",
    "WORKSPACE": "the name the start-up roots open as (or the picker's name field)",
    "DATASET_ROOTS": "local dataset checkouts to look in for task files",
    "DEBUG": "the Werkzeug debugger (or --no-debug)",
    "RUNS_DIR": "where runs started from the builder keep their script, log and job (default runs/ in the user data directory)",
    "HARBOR_PINS": "where the Harbor a recipe pins is built and kept on this machine (default harbor-pins/ beside the runs)",
    "STANDARD_DIR": "where the Nullius standard installs agents, capabilities and benchmark upstreams (default standard/ in the user data directory)",
    "STANDARD_DATA_DIR": "where the Nullius standard keeps the datasets it downloads, which can be gigabytes (default datasets/ in STANDARD_DIR)",
    "SECRETS_DIR": "where each benchmark's licences (Settings → Custom keys) and the Hugging Face token (Settings → Data providers) are kept (default secrets/ in the user configuration directory)",
}
BY_NAME: Dict[str, Variable] = {v.name: v for v in VARIABLES}
LOADED_KEYS: set = set()   # variables ``load`` set from the file (so: not held by the process environment)
_FROM: Dict[str, str] = {}  # which file each of them came from, so a refresh of one file never drops another's
# Names other tools give the model settings, and names this application
# used before: a file written with one is read by nobody here, and the page
# says which of its own names was meant - for HOST and PORT, a start-up
# option, read at start. OPENAI_API_KEY and OPENROUTER_API_KEY are its own:
# the keys a recipe on OpenAI or on OpenRouter is sent with.
INSTEAD: Dict[str, str] = {"API_TOKEN": "MODEL_API_KEY", "API_URL": "MODEL_API_URL", "OPENAI_BASE_URL": "MODEL_API_URL",
                           "API_KEY_TYPESAFE": "TYPESAFE_API_KEY", "JUDGE_MODEL": "LLM_JUDGE_MODEL", "HOST": "APP_HOST", "PORT": "APP_PORT"}


def parse(path: str) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """The file as a list of (line, key, value): key and value are None for
    a comment or blank line. Quotes around a value are dropped, as ``load``
    always has."""
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        if str(path).endswith(".json"):
            try:
                values = json.load(fh)
            except (ValueError, UnicodeError) as e:
                raise ValueError("Settings file is not valid JSON; it has not been changed.") from e
            if not isinstance(values, dict) or any(
                not isinstance(k, str) or not _ENVNAME_OK.fullmatch(k) or
                not isinstance(v, str) or any(c in v for c in "\0\r\n")
                for k, v in values.items()
            ):
                raise ValueError("Settings must be an object of variable names and single-line strings.")
            return [(f"{key}={value}", key, value) for key, value in values.items()]
        for raw in fh:
            line = raw.rstrip("\n")
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                out.append((line, None, None))
                continue
            key, _, value = s.partition("=")
            out.append((line, key.strip(), value.strip().strip('"').strip("'")))
    return out


def load(path: str) -> set:
    """Apply the file to the process environment: existing variables win.
    Returns the names the file supplied."""
    applied = set()
    for _, key, value in parse(path):
        if key and key not in os.environ:
            os.environ[key] = value or ""
            applied.add(key)
    LOADED_KEYS.update(applied)
    _FROM.update((k, os.path.abspath(path)) for k in applied)
    return applied


def refresh(path: str) -> Dict[str, List[str]]:
    """The file as it is now, under ``load``'s rule, for a file written or
    edited while the application runs (``sync`` calls it when the file has
    changed): a key the process environment lacks is applied; a key the file
    supplied follows the file - a new value, or gone when its line is; a
    variable the process was started with still wins. A key taken out of
    the environment (a removal saved from the page, a test) is not put back.
    Returns what changed, by name and never by value: the keys ``loaded``,
    ``updated`` and ``removed``, each sorted."""
    here = os.path.abspath(path)
    values = in_file(path)
    done: Dict[str, List[str]] = {"loaded": [], "updated": [], "removed": []}
    for key in [k for k in LOADED_KEYS if _FROM.get(k) == here and k not in values]:
        if os.environ.pop(key, None) is not None:
            done["removed"].append(key)
        LOADED_KEYS.discard(key)
    for key, value in values.items():
        if key in LOADED_KEYS:
            if key in os.environ and os.environ[key] != value:
                os.environ[key] = value
                done["updated"].append(key)
        elif key not in os.environ:
            os.environ[key] = value
            LOADED_KEYS.add(key)
            _FROM[key] = here
            done["loaded"].append(key)
    return {k: sorted(v) for k, v in done.items()}


_SEEN: Dict[str, Optional[tuple]] = {}   # the file as last read by ``sync``: its stat, or None when it was absent


def sync(path: str) -> Dict[str, List[str]]:
    """``refresh`` when the file is not as it was last read - another
    size, time or inode, or come or gone - and nothing otherwise, so a
    request costs one stat. Returns what ``refresh`` returns, every list
    empty when the file was as read."""
    here = os.path.abspath(path)
    try:
        st = os.stat(here)
        now: Optional[tuple] = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        now = None
    if here in _SEEN and _SEEN[here] == now:
        return {"loaded": [], "updated": [], "removed": []}
    done = refresh(path)
    _SEEN[here] = now
    return done


def in_file(path: str) -> Dict[str, str]:
    return {k: v or "" for _, k, v in parse(path) if k}


def agent_key_names(env=None) -> List[str]:
    """The agent keys' names, as AGENT_KEYS lists them; a list that would
    not validate names none."""
    raw = (os.environ if env is None else env).get("AGENT_KEYS", "")
    if _agent_key_names(raw):
        return []
    return [n.strip() for n in raw.split(",") if n.strip()]


def agent_key_values(env=None) -> Dict[str, str]:
    """Each agent key that holds a value: what a Harbor run is sent."""
    env = os.environ if env is None else env
    return {n: env[n] for n in agent_key_names(env) if env.get(n)}


def endpoint_ids(env=None) -> List[str]:
    """The ids of the named OpenAI-compatible endpoints: every id one of whose settings is set."""
    env = os.environ if env is None else env
    return sorted({m.group(1) for m in map(_ENDPOINT_VAR.match, env) if m and ENDPOINT_ID.match(m.group(1))})


def endpoint_settings(i: str) -> List[str]:
    """The settings of the endpoint ``i``: its name, URL and key."""
    return [f"MODEL_{i}_{part}" for part in ENDPOINT_PARTS]


def endpoint_names(env=None) -> List[str]:
    """Every setting of a named endpoint that ``env`` holds: what a run never inherits."""
    return sorted(n for n in (os.environ if env is None else env) if _ENDPOINT_VAR.match(n))


def endpoint_variable(name: str) -> Optional[Variable]:
    """A named endpoint's setting as the page and ``save_many`` treat it, or None for another name."""
    m = _ENDPOINT_VAR.match(name)
    if not m or not ENDPOINT_ID.match(m.group(1)):
        return None
    what, kind = {"NAME": ("name", "text"), "API_URL": ("URL", "endpoint"), "API_KEY": ("key, optional", "localkey")}[m.group(2)]
    return Variable(name, GROUPS[0], f"The {what} of the OpenAI-compatible endpoint {m.group(1)}.", kind)


def new_endpoint_id(name: str, taken) -> str:
    """An id for a new endpoint, from its name: capitals, digits and underscores, not one of ``taken``."""
    base = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:28].strip("_") or "ENDPOINT"
    n, out = 1, base
    while out in taken:
        n += 1
        out = f"{base}_{n}"
    return out


def agent_variable(name: str) -> Variable:
    """An agent key as the page and ``save_many`` treat it: a secret of its own."""
    return Variable(name, GROUPS[2], f"An agent key, {name}: sent to Harbor runs, to the agent that reads it.", "secret")


def _write_lines(path: str, lines: List[str]) -> None:
    """Atomically replace one owner-only settings file; never print values."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if str(path).endswith(".json"):
                values = {line.partition("=")[0]: line.partition("=")[2]
                          for line in lines if "=" in line and not line.lstrip().startswith("#")}
                json.dump(values, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            else:
                fh.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def take(path: str, name: str) -> Optional[str]:
    """Move a line out of the file: ``name``'s value, its line removed and,
    when the file put it there, its variable gone from the process
    environment; None when the file has no such line. Only for a name no
    setting reads (``unread``) - a benchmark's licence from before licences
    were kept per benchmark (``nullius.secrets``), which its page moves
    there on the user's word."""
    if name in BY_NAME or name in STARTUP or name in set(agent_key_names()):
        raise ValueError(f"{name} is a setting of its own; it is not moved")
    value = in_file(path).get(name)
    if value is None:
        return None
    lines = [line for line, key, _ in parse(path) if key != name]
    while lines and not lines[-1].strip():
        lines.pop()
    _write_lines(path, lines)
    if name in LOADED_KEYS:
        os.environ.pop(name, None)
        LOADED_KEYS.discard(name)
    return value


def unread(path: str) -> List[dict]:
    """Keys in the file that no setting or start-up option reads, each with
    the page's own name when it is a known other spelling (``API_TOKEN``):
    the file applies them to the environment, but nothing here reads them.
    A run inherits those its launcher does not withhold
    (``execution.withheld``: every other spelling of a key among them)."""
    keys = set(agent_key_names()) | set(agent_key_names(in_file(path)))
    return [{"name": k, "instead": INSTEAD.get(k, "")} for k in in_file(path)
            if k not in BY_NAME and k not in STARTUP and k not in keys and not endpoint_variable(k)]


# What a page shows of a secret that is set: the same eight stars whatever it is, so not even its length
# says anything about it.
SECRET_SHOWN = "********"

# Said beside an endpoint set without a key: allowed, the user's choice, but advised.
LOCAL_KEY_ADVICE = "No key: the run is sent the word " + repr(MODEL_KEY_PLACEHOLDER) + ". Fine for a server that checks none, but a key is advised."


def state(var: Variable, file_values: Dict[str, str]) -> dict:
    """What the page shows: where the value comes from, never a secret."""
    value = os.environ.get(var.name)
    source = "unset" if value is None else ("file" if var.name in LOADED_KEYS else "environment")
    shown = "" if value is None else (SECRET_SHOWN if var.secret else endpoint_url_shown(value) if var.kind == "endpoint" else value)
    # an endpoint's URL saved or set before it was shown: said, and not saved again until fixed
    problem = endpoint_url_problem(value) if var.kind == "endpoint" and value else None
    # constraints set in the environment or the file by hand: said, and every run refused until fixed
    invalid = _constraints(value) if var.kind == "constraints" and value else None
    return {"name": var.name, "group": var.group, "purpose": var.purpose, "kind": var.kind, "secret": var.secret,
            "example": var.example, "source": source, "shown": shown,
            "shadowed": source == "environment" and var.name in file_values and file_values[var.name] != value,
            "advice": LOCAL_KEY_ADVICE if var.kind == "localkey" and value is None and os.environ.get(var.name[:-3] + "URL") else "",
            "problem": f"This URL must be {problem}." if problem else f"{var.name} must be {invalid}." if invalid else ""}


def states(path: str) -> List[dict]:
    """Every variable's state as the process environment holds it, the file
    read only to say which values it shadows: a key written by hand after
    start reads as before until ``refresh`` (``sync``, before a request) applies it."""
    fv = in_file(path)
    return ([state(v, fv) for v in VARIABLES] + [state(agent_variable(n), fv) for n in agent_key_names()]
            + [state(endpoint_variable(n), fv) for i in endpoint_ids() for n in endpoint_settings(i)])


def save(path: str, name: str, value: Optional[str]) -> None:
    """One variable: see ``save_many``."""
    save_many(path, {name: value})


def save_many(path: str, changes: Dict[str, Optional[str]]) -> None:
    """Set (or, with None, remove) variables in the file, keeping every
    other line as it was, and apply each change to the process environment
    unless the environment already held that variable. Every value is
    validated before any is written - one bad value and nothing changes;
    the file is written whole to a temporary name beside the target, then
    renamed."""
    problems = []
    listed = changes.get("AGENT_KEYS") if "AGENT_KEYS" in changes else os.environ.get("AGENT_KEYS", "")
    agents = set() if listed is None or _agent_key_names(listed) else {n.strip() for n in listed.split(",") if n.strip()}
    agents |= set(agent_key_names())                  # a key being removed with its name is still one this file may hold
    for name, value in changes.items():
        var = BY_NAME.get(name) or endpoint_variable(name) or (agent_variable(name) if name in agents else None)
        if var is None:
            problems.append(f"{name} is not a setting this application reads.")
        elif value is not None:
            why = var.validate(value)
            if why:
                problems.append(f"{name} must be {why}.")
    if problems:
        raise ValueError(" ".join(problems))
    lines, done = [], set()
    for line, key, _ in parse(path):
        if key in changes:
            value = changes[key]
            if value is not None and key not in done:
                lines.append(f"{key}={value}")
            done.add(key)       # a duplicate line is dropped rather than kept
            continue
        lines.append(line)
    for name, value in changes.items():
        if value is not None and name not in done:
            lines.append(f"{name}={value}")
    while lines and not lines[-1].strip():
        lines.pop()
    _write_lines(path, lines)
    for name, value in changes.items():
        if name in os.environ and name not in LOADED_KEYS:
            continue        # the process environment holds it; the file is for the next start
        if value is None:
            os.environ.pop(name, None)
            LOADED_KEYS.discard(name)
        else:
            os.environ[name] = value
            LOADED_KEYS.add(name)
            _FROM[name] = os.path.abspath(path)


def example() -> str:
    """``app/.env.example``: every variable, its purpose and an example,
    none of them set."""
    out = ["# Environment variable reference. Configure Nullius through Settings,",
           "# which saves to your user settings.json, or export variables for automation.",
           "# Existing app/.env files can be imported with nullius migrate --from CHECKOUT.",
           "# Process environment variables win over saved settings. Never commit secrets.",
           ""]
    out.append("# Start-up options, not on the Settings page:")
    for name, what in STARTUP.items():
        out.append(f"#   {name}: {what}")
    out.append("")
    for g in GROUPS:
        if not any(v.group == g for v in VARIABLES):
            continue                 # a group of names read from elsewhere (the agent keys): none to list here
        out.append(f"# --- {g} ---")
        for v in VARIABLES:
            if v.group != g:
                continue
            out.append(f"# {v.purpose}")
            if v.example:
                out.append(f"#   e.g. {v.example}")
            out.append(f"{v.name}=")
        out.append("")
    return "\n".join(out)
