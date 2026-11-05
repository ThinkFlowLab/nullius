"""Parse an ATIF trajectory (``agent/trajectory.json``) into steps + stats.

Each step may carry ``tool_calls`` (a list of {function_name, arguments,
tool_call_id}) and an ``observation`` (results keyed by source_call_id). We
flatten those into a per-step view and compute run-level tool statistics.

One rule about those statistics: a tool call's ``category`` is the only
key that compares across agents; its ``name`` never is. ``function_name`` is
each agent's own vocabulary - OpenCode writes ``bash``, Claude Code ``Bash``,
another agent a sentence, so name frequencies are not comparable. Every summary a
page or a comparison reads (``category_freq``, ``behavior.analyze``, the
``Tool mix`` row) is therefore keyed by category, and the names are kept
beside it (``name_freq``) as the evidence of what the category folded, never
as a key of their own.

A benchmark's own tools reach the agent over MCP, where each agent spells the
same tool its own way (``mcp.kit.get_code``, ``Tool: kit/get_code``,
``kit: get code``). :func:`mcp_tool` reads the server and the tool back from any
of them, and such a tool's category comes from what its name says it does:
``lookup`` or ``change``. No benchmark is named here, and the coding rules below
are left to the agent's own tools.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from nullius.redact import redact

# Coarse tool categories, matched against the (verbose) function_name label.
# Order matters: the first rule to match wins. `plan` comes first because
# opencode's `todowrite` contains "write" but updates a plan rather than code.
_CATEGORY_RULES = [
    ("plan", re.compile(r"(?i)todo|plan|think|note")),
    ("search", re.compile(r"(?i)search|grep|glob|find|list")),
    ("read", re.compile(r"(?i)read|cat|view|open|inspect")),
    ("edit", re.compile(r"(?i)edit|write|patch|replace|apply|create")),
    ("execute", re.compile(r"(?i)execute|run|bash|shell|command|test|python")),
]


# The spellings of an MCP tool that say so themselves. Any other (``kit: get code``,
# ``kit_get_code``) reads as one only for a server the run says it gave the agent.
_MCP_SPELLINGS = (
    re.compile(r"^mcp(?:\.|__)([^\s.]+?)(?:\.|__)(\S+)$"),
    re.compile(r"^Tool: ([^\s/]+)/(\S+)$"),
    re.compile(r"^([A-Za-z0-9-]+)__(\S+)$"),
)
# What an MCP tool's first word says it does. Anything else stays ``other``: a guess would read as a measurement.
_LOOKUP_WORDS = frozenset("get list search find read check fetch lookup query show view retrieve describe count".split())
_CHANGE_WORDS = frozenset("create add update set edit delete remove cancel book send write modify replace move".split())
# An MCP tool over files or a shell is the coding work the rules below count: the edit-to-test loop reads those categories.
_CODE_WORDS = frozenset("file files dir directory patch shell bash command test tests grep glob editor python".split())


def mcp_tool(function_name: str, servers=()) -> tuple | None:
    """``(server, tool)`` when *function_name* is an MCP tool, in any agent's spelling."""
    name = (function_name or "").strip()
    for pat in _MCP_SPELLINGS:
        got = pat.match(name)
        if got:
            return got.group(1), got.group(2)
    for server in servers:
        for joint in ("__", "_", ": ", ":", "/", "."):
            if name.startswith(server + joint) and name[len(server) + len(joint):].strip():
                return server, re.sub(r"\s+", "_", name[len(server) + len(joint):].strip())
    return None


def categorize(function_name: str, servers=()) -> str:
    """The coarse category of a tool call - the one key comparable across agents.

    The name it is derived from is not: agents name the same tool differently
    (module docstring). A name no rule matches is ``other``, which is a
    category like the rest and is summarised like the rest.
    """
    tool = mcp_tool(function_name, servers)
    if tool:
        words = tool[1].lower().split("_")
        if not _CODE_WORDS.intersection(words):
            return "lookup" if words[0] in _LOOKUP_WORDS else "change" if words[0] in _CHANGE_WORDS else "other"
    for name, pat in _CATEGORY_RULES:
        if pat.search(tool[1] if tool else function_name or ""):
            return name
    return "other"


@dataclass
class Step:
    step_id: int
    source: str
    timestamp: str
    message: str
    tool_calls: list = field(default_factory=list)  # [{name, category, arguments, observation}]
    # Seconds until the next step opened — None on the last step, when either
    # timestamp is missing, or when the whole trajectory's clock is degenerate.
    dur: float | None = None


# Observations are serialised as `success=<bool> data={...} error=<repr>`, so
# status is explicit — no need to guess from the text. Fall back to scanning
# for tracebacks only when that prefix is absent (older/other writers).
_SUCCESS_RE = re.compile(r"^success=(True|False)\b")
_ERROR_RE = re.compile(r"error='((?:[^'\\]|\\.)*)'|error=\"((?:[^\"\\]|\\.)*)\"")
_FAILURE_MARKERS = [
    (re.compile(r"Traceback \(most recent call last\)"), "Traceback"),
    (re.compile(r"\b(\w*(?:Error|Exception))\b(?=:)"), None),
    (re.compile(r"command not found"), "command not found"),
    (re.compile(r"No such file or directory"), "No such file or directory"),
]


def _first_line(text: str) -> str:
    """Most informative line of an error blob.

    Skips the exit-code header, and for a traceback takes the *last* line —
    "Traceback (most recent call last):" says nothing, the final
    "ModuleNotFoundError: ..." says everything.
    """
    # The blob arrives as a Python repr, so quotes and newlines are escaped.
    text = text.replace("\\n", "\n").replace("\\'", "'").replace('\\"', '"')
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.lower().startswith("exit code")]
    if not lines:
        return ""
    if any(ln.startswith("Traceback (most recent call last)") for ln in lines):
        return lines[-1][:160]
    return lines[0][:160]

# Argument keys worth showing as the call's "target", most specific first.
_TARGET_KEYS = ("command", "cmd", "file_path", "path", "pattern", "query", "glob")

# Some agents don't name the file in an argument at all — opencode's
# `apply_patch` takes only `patchText`, with the path inside the payload. Both
# the codex-style header and a plain unified diff appear in the wild.
_PATCH_KEYS = ("patchText", "patch", "diff", "content")
PATCH_PATHS = [
    re.compile(r"(?m)^\*\*\* (?:Update|Add|Delete) File:\s*(\S+)"),
    re.compile(r"(?m)^\+\+\+ (?:b/)?(\S+)"),
]
# Public for the same reason as trace.clean: behavior.py reads both this and
# parse_ts, and it stayed with the application when the reader was packaged.
_PATCH_PATHS = PATCH_PATHS


# Container WORKDIRs an agent runs in, such as /testbed or /app. Whether a path arrives absolute or relative is the model's
# choice, not the harness's — one agent passed relative paths on two runs of
# one task and absolute on a third, same model, same task. Left alone, the same
# file is two different strings and every set operation over them lies:
# "which files did both agents touch" answered 0 when the answer was 2.
#
# Normalising here rather than at each comparison keeps one canonical form. The
# agent's original string stays on the tool call, because *that* difference is
# real signal about how the agent behaved — it just must not reach set
# arithmetic. `behavior._same_file` still suffix-matches for gold patches,
# which covers workspace roots this list doesn't know.
_WORKSPACE_ROOTS = ("/testbed/", "/app/")


def workspace_relative(path: str) -> str:
    """Strip a known container workspace root so paths compare equal."""
    for root in _WORKSPACE_ROOTS:
        if path.startswith(root):
            return path[len(root):]
    return path


def patched_files(arguments: dict) -> list:
    """File paths named inside a patch payload, in first-seen order."""
    if not isinstance(arguments, dict):
        return []
    body = next(
        (arguments[k] for k in _PATCH_KEYS if isinstance(arguments.get(k), str)), ""
    )
    found = []
    for pattern in _PATCH_PATHS:
        for path in pattern.findall(body):
            if path not in found and path != "/dev/null":
                found.append(path)
    return found


def failure_of(observation: str) -> str:
    """Describe the failure in *observation*, or '' if the call succeeded."""
    if not observation:
        return ""
    status = _SUCCESS_RE.match(observation)
    if status:
        if status.group(1) == "True":
            return ""
        err = _ERROR_RE.search(observation)
        message = _first_line(err.group(1) or err.group(2) or "") if err else ""
        return message or "failed"
    for pattern, label in _FAILURE_MARKERS:
        hit = pattern.search(observation)
        if hit:
            return label or hit.group(1)
    return ""


def target_of(arguments: dict) -> str:
    """A short, human-readable subject for a tool call."""
    if not isinstance(arguments, dict):
        return ""
    for key in _TARGET_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    # No named argument: a patch payload still tells us what was touched.
    files = patched_files(arguments)
    if files:
        return files[0] if len(files) == 1 else f"{files[0]} +{len(files) - 1}"
    return ""


# Two usage conventions reach this parser. OpenAI-style counts every input
# token in prompt_tokens and the cached subset again in cached_tokens, so
# cached ÷ prompt is a real hit rate. Anthropic-style reports cache *reads*
# (plus a few-token fresh remainder) as prompt_tokens and the new tokens as a
# cache write in a field of its own that never enters the total — cached ÷
# prompt is ~100% there by construction, however the cache performed.
# The number worth comparing across agents is therefore
# reads ÷ every input token, with the writes added back in.
_REMAINDER_CEILING = 16  # max per-call prompt−cached that reads as Anthropic-style


@dataclass
class CacheAccounting:
    convention: str        # "openai" | "anthropic"
    reads: int             # tokens served from cache
    new: int               # every other input token: cache writes + fresh remainder
    reported_prompt: int   # final_metrics' own total_prompt_tokens
    reported_cached: int

    @property
    def total(self) -> int:
        return self.reads + self.new

    @property
    def hit(self) -> float | None:
        return self.reads / self.total if self.total else None

    @property
    def reported_hit(self) -> float | None:
        return self.reported_cached / self.reported_prompt if self.reported_prompt else None

    @property
    def reported_diverges(self) -> bool:
        """Whether the file's own ratio would mislead next to the real one."""
        if self.hit is None or self.reported_hit is None:
            return False
        return abs(self.hit - self.reported_hit) > 0.005


def _extension(container, key):
    """A field ATIF does not name. Newer writers put it in the
    `extra` slot of its level; earlier artifacts carry it beside the named
    fields. The slot wins when both are present, the two are never summed,
    and a null in the slot stays None - the slot said so. A non-dict `extra`
    is no slot: the old position is read."""
    if not isinstance(container, dict):
        return None
    extra = container.get("extra")
    if isinstance(extra, dict) and key in extra:
        return extra[key]
    return container.get(key)


def _cache_accounting(series: list, final_metrics: dict) -> CacheAccounting | None:
    """Fold the token series into reads and new, whichever convention wrote it.

    Also annotates every series entry with ``sent`` and ``new`` so the growth
    strip can draw each call's whole input — under the Anthropic convention a
    call's prompt_tokens is missing its write, which belongs to the call that
    paid for it. The write of the final call never surfaces (nothing read it
    back), so it is counted as zero rather than guessed.
    """
    for e in series:
        e["new"] = max(e["prompt"] - e["cached"], 0)
        e["sent"] = e["prompt"]
    usable = [e for e in series if e["prompt"] > 0]

    reported_prompt = final_metrics.get("total_prompt_tokens")
    reported_cached = final_metrics.get("total_cached_tokens")
    if not isinstance(reported_prompt, (int, float)):
        reported_prompt = sum(e["prompt"] for e in usable)
        reported_cached = sum(e["cached"] for e in usable)
    if not isinstance(reported_cached, (int, float)):
        reported_cached = 0
    reported_prompt, reported_cached = int(reported_prompt), int(reported_cached)

    if len(usable) >= 2 and all(
        e["prompt"] - e["cached"] <= _REMAINDER_CEILING for e in usable
    ):
        for cur, nxt in zip(usable, usable[1:]):
            write = max(nxt["prompt"] - cur["prompt"], 0)
            cur["new"] += write
            cur["sent"] = cur["cached"] + cur["new"]
        reads = sum(e["cached"] for e in usable)
        new = sum(e["new"] for e in usable)
        return CacheAccounting("anthropic", reads, new, reported_prompt, reported_cached)

    if usable:
        reads = sum(e["cached"] for e in usable)
        new = sum(e["new"] for e in usable)
        return CacheAccounting("openai", reads, new, reported_prompt, reported_cached)

    if reported_prompt:
        reads = min(reported_cached, reported_prompt)
        return CacheAccounting("openai", reads, reported_prompt - reads,
                               reported_prompt, reported_cached)
    return None


@dataclass
class Trajectory:
    n_steps: int
    steps: list  # list[Step]
    category_freq: Counter      # by coarse category - the cross-agent key
    n_tool_calls: int
    last_agent_message: str
    final_metrics: dict
    error: str = ""             # set if the file could not be parsed
    calls: list = field(default_factory=list)          # flat, in order
    files_touched: list = field(default_factory=list)  # paths from edit calls
    # False when the writer stamped every step at the same instant (older
    # trajectories) — durations would be noise, so none are set.
    has_step_clock: bool = False
    # One entry per step that carries per-call metrics (ATIF v1.7 writers, and
    # runs where a gateway-log matcher attached them):
    # {step_id, prompt, cached, completion}. Prompt growth across this series
    # is the caching story of the whole session in one curve.
    token_series: list = field(default_factory=list)
    # Reads/new fold over the series, convention detected — None when the
    # file carries no usage at all.
    cache: CacheAccounting | None = None
    # Summed from metrics.extra.reasoning_tokens; None when no step has it —
    # "not reported" and "zero reasoning" are different claims.
    total_reasoning_tokens: int | None = None
    # {category: Counter(name)} over the same calls as category_freq: what each
    # category folded, in the agent's own words. Evidence beside the key, so a
    # reader can check the fold - never a key itself (module docstring).
    name_freq: dict = field(default_factory=dict)

    @property
    def n_failed_calls(self) -> int:
        return sum(1 for c in self.calls if c["failure"])

    @property
    def tools(self) -> list:
        """Each tool once: ``{server, name, category, calls, failed, ns, spellings}``,
        by server, the most called first. ``ns`` is ``None`` when no call has a clock."""
        rows: dict = {}
        for c in self.calls:
            row = rows.setdefault((c["server"], c["tool"]), {"server": c["server"], "name": c["tool"], "category": c["category"],
                                                              "calls": 0, "failed": 0, "ns": None, "spellings": []})
            row["calls"] += 1
            row["failed"] += 1 if c["failure"] else 0
            if c["ns"] is not None:
                row["ns"] = (row["ns"] or 0) + c["ns"]
            if c["name"] not in row["spellings"]:
                row["spellings"].append(c["name"])
        return sorted(rows.values(), key=lambda r: (bool(r["server"]) is False, r["server"], -r["calls"], r["name"]))

    def names_of(self, category: str) -> list:
        """``[(name, count), …]`` the agent used for *category*, most frequent first."""
        return self.name_freq.get(category, Counter()).most_common()


def parse_ts(value: str):
    """An ISO timestamp, or None — a record with an unreadable clock is not
    an error here, it is a record whose clock we decline to guess at."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_parse_ts = parse_ts


def _tool_ns(step: dict) -> int | None:
    """How long a step's tool call ran, when the step says when it ended (a standard run's do)."""
    extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
    start, end = parse_ts(extra.get("running_at") or step.get("timestamp")), parse_ts(extra.get("ended_at"))
    return max(0, int((end - start).total_seconds() * 1e9)) if start and end else None


# Below this, the trajectory's whole clock spread is treated as one write-time
# stamp, not a real per-step clock. Same refuse-don't-guess policy as the
# agent-side metrics matcher.
_MIN_CLOCK_SPAN_SEC = 1.0


def _attach_durations(steps: list) -> bool:
    """Set each step's ``dur`` from consecutive timestamps.

    Returns whether the trajectory has a usable step clock at all; when it
    does not, every ``dur`` is left None.
    """
    stamps = [_parse_ts(s.timestamp) for s in steps]
    known = [t for t in stamps if t]
    if len(known) < 2 or (max(known) - min(known)).total_seconds() < _MIN_CLOCK_SPAN_SEC:
        return False
    for i, step in enumerate(steps[:-1]):
        here, there = stamps[i], stamps[i + 1]
        if here and there and there >= here:
            step.dur = (there - here).total_seconds()
    return True


def _extract_observation(step: dict, call_id: str) -> str:
    obs = step.get("observation")
    if not isinstance(obs, dict):
        return ""
    for r in obs.get("results", []):
        if r.get("source_call_id") == call_id:
            content = r.get("content", "")
            # content is often a JSON-encoded list of {type, content:{text}}
            try:
                parsed = json.loads(content)
                texts = []
                for item in parsed:
                    c = item.get("content", {})
                    if isinstance(c, dict) and "text" in c:
                        texts.append(c["text"])
                if texts:
                    return redact("\n".join(texts))
            except Exception:
                pass
            return redact(str(content))
    return ""


def message_text(message) -> str:
    """A step's message as text. ATIF v1.6 lets it be a list of content parts
    ({"type": "text", "text": ...}, an image, ...): the text parts joined, a
    placeholder for each other part, so a multimodal prompt reads rather
    than failing the page."""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        message = [message]
    if not isinstance(message, list):
        return "" if message is None else str(message)
    out = []
    for part in message:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
        elif isinstance(part, dict):
            out.append(f"[{part.get('type') or 'part'}]")
    return "\n".join(out)


def load(path: str) -> Trajectory:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return Trajectory(0, [], Counter(), 0, "", {}, error=str(exc))

    steps_out: list[Step] = []
    category_freq: Counter = Counter()
    name_freq: dict = {}
    n_tool_calls = 0
    last_agent_message = ""

    all_calls: list = []
    files_touched: list = []
    token_series: list = []
    reasoning_total: int | None = None
    servers = (data.get("extra") or {}).get("mcp_servers") or ()

    for raw in data.get("steps", []):
        metrics = raw.get("metrics")
        if isinstance(metrics, dict):
            token_series.append(
                {
                    "step_id": raw.get("step_id", 0),
                    "prompt": metrics.get("prompt_tokens") or 0,
                    "cached": metrics.get("cached_tokens") or 0,
                    "completion": metrics.get("completion_tokens") or 0,
                }
            )
            reasoning = (metrics.get("extra") or {}).get("reasoning_tokens")
            if reasoning is not None:
                reasoning_total = (reasoning_total or 0) + reasoning
        calls_out = []
        for tc in raw.get("tool_calls") or []:
            name = tc.get("function_name") or tc.get("name") or "(tool)"
            cat = categorize(name, servers)
            server, tool = mcp_tool(name, servers) or ("", name)
            args = tc.get("arguments", {})
            observation = _extract_observation(raw, tc.get("tool_call_id"))
            call = {
                "step_id": raw.get("step_id", 0),
                # An ATIF id is source evidence, not the call's position. Keep
                # it so parallel tools and their observations stay identifiable.
                "tool_call_id": tc.get("tool_call_id"),
                "name": redact(name),
                "server": redact(server),
                "tool": redact(tool),
                "ns": _tool_ns(raw),
                "category": cat,
                "target": redact(target_of(args)),
                "arguments": redact(json.dumps(args, ensure_ascii=False)),
                "observation": observation,
                # Size of what came back — the closest thing to "work done" this
                # schema offers, since the timestamps are not per-step.
                "obs_bytes": len(observation),
                "failure": failure_of(observation),
            }
            calls_out.append(call)
            # The writer emits placeholder entries — no name, no arguments, no
            # observation — which would otherwise double the tool-call count.
            # The summaries count the same calls the list keeps, so the chips,
            # the names behind them and the total describe one population.
            if args or observation or name != "Using tool":
                all_calls.append(call)
                n_tool_calls += 1
                category_freq[cat] += 1
                name_freq.setdefault(cat, Counter())[call["name"]] += 1
            if cat == "edit":
                # Prefer a named argument; fall back to paths inside the patch
                # body for agents that pass only a diff.
                touched = patched_files(args) or ([target_of(args)] if target_of(args) else [])
                for path in touched:
                    path = workspace_relative(path)
                    if path and path not in files_touched:
                        files_touched.append(path)

        msg = redact(message_text(raw.get("message")))
        src = raw.get("source", "")
        if src == "agent" and msg and msg != "(tool use)":
            last_agent_message = msg

        steps_out.append(
            Step(
                step_id=raw.get("step_id", 0),
                source=src,
                timestamp=raw.get("timestamp", ""),
                message=msg,
                tool_calls=calls_out,
            )
        )

    has_clock = _attach_durations(steps_out)
    # A call inherits its step's span (None without a clock) — coarse, but the
    # schema gives one timestamp per step, not per call.
    for step in steps_out:
        for call in step.tool_calls:
            call["dur"] = step.dur

    # Agents whose LLM calls cannot be matched to steps leave per-step metrics
    # unset and report the total in final_metrics instead. Reading only the
    # per-step path renders nothing while the number sits in the file.
    if reasoning_total is None:
        fallback = _extension(data.get("final_metrics") or {}, "total_reasoning_tokens")
        if isinstance(fallback, (int, float)):
            reasoning_total = int(fallback)

    cache = _cache_accounting(token_series, data.get("final_metrics") or {})

    return Trajectory(
        n_steps=len(steps_out),
        steps=steps_out,
        category_freq=category_freq,
        n_tool_calls=n_tool_calls,
        last_agent_message=last_agent_message,
        final_metrics=data.get("final_metrics") or {},
        calls=all_calls,
        files_touched=files_touched,
        has_step_clock=has_clock,
        token_series=token_series,
        cache=cache,
        total_reasoning_tokens=reasoning_total,
        name_freq=name_freq,
    )
