"""The text a question reads, built from the redacted views only: the
instruction, the agent's steps (messages and tool calls), the final message,
the recorded error, the verifier text. Truncated head-and-tail to a stated
size with a marker, never silently. Nothing raw leaves this module."""
from __future__ import annotations

import os
import re
from typing import Optional

from nullius.redact import redact

LIMIT = 60_000          # characters of state per call; the judge is priced per input token
OBSERVATION = 600       # characters kept of a tool observation: the head, and the tail below
OBSERVATION_TAIL = 200  # of those, the characters kept from the end - where a traceback names its exception and a command its exit
MARKER = "\n[… {n} characters left out here …]\n"

# Observation relevance. Accuracy falls as state grows with material unrelated
# to the decision, so a tool observation is sent in full (up to OBSERVATION)
# only when it bears on the question - deterministically, in code, by the
# rules in `_relevant` - and is otherwise a one-line stub with a visible
# marker. The instruction, the agent's messages, the tool names and their
# arguments are never dropped; only what a tool answered is. `relevance="all"`
# opts out.
RELEVANCE = "question"
OMITTED = "[observation omitted: {n} characters, not relevant to the question]"
KEPT_MARKER = "[observations: {kept} kept, {omitted} omitted as not relevant to the question]"
MIN_TERM = 5            # characters a word needs to count as a term of the instruction
MIN_OVERLAP = 2         # instruction terms an observation must share to be kept for overlap alone
_ERRORISH = re.compile(r"(?i)\b(error|errno|exception|traceback|fail(?:ed|ure|s)?|denied|not found|no such file|fatal|panic|refused|timed? ?out|cannot|can't)\b")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")

# A state kind a later question group adds: `register(kind, builder)`, the
# builder taking the same keywords `build` does and returning the *raw* text;
# `build` redacts and truncates it, so nothing raw leaves here either way.
BUILDERS: dict = {}


def register(kind: str, builder) -> None:
    """Declare how a state kind is built. `builder(traj, trial=, trial_dir=, other=, **extra) -> str`."""
    if kind in BUILDERS or kind in ("trial", "failure", "final", "pair"):
        raise ValueError(f"state kind {kind!r} is already declared")
    BUILDERS[kind] = builder


def truncate(text: str, limit: int = LIMIT) -> tuple:
    """(text, truncated): the head and the tail with a marker between when
    the text is longer than the limit."""
    if len(text) <= limit:
        return text, False
    left = len(text) - limit
    head = limit * 2 // 3
    return text[:head] + MARKER.format(n=left) + text[-(limit - head):], True


def instruction_of(traj) -> str:
    """The task as the agent was given it: the first user step. Many Harbor
    agents (mini-swe-agent, swe-agent, OpenHands, Codex) open with their own
    system prompt, which is not the task; only a trajectory with no user step
    falls back to its first step that is not the agent's."""
    first = next((s.message for s in traj.steps if s.source == "user" and s.message), "")
    return first or next((s.message for s in traj.steps if s.source not in ("agent", "system") and s.message), "") \
        or next((s.message for s in traj.steps if s.source != "agent" and s.message), "")


def _terms(text: str) -> set:
    return {w.lower() for w in _WORD.findall(text or "") if len(w) >= MIN_TERM}


def _relevant(call: dict, obs: str, instruction: str, terms: set, next_message: str) -> bool:
    """Whether an observation bears on the question, by rule: it reports a
    failure; its call edited or executed something; its target is named by
    the instruction or by the agent's next message; or it shares MIN_OVERLAP
    terms with the instruction."""
    if call.get("failure") or _ERRORISH.search(obs):
        return True
    if call.get("category") in ("edit", "execute", "change"):
        return True
    target = str(call.get("target") or "")
    if target and ((next_message and target in next_message) or target.lower() in instruction.lower()):
        return True
    return len(_terms(obs) & terms) >= MIN_OVERLAP


def transcript_of(traj, relevance: str = RELEVANCE) -> str:
    """The agent's steps in order: each message and its tool calls with a
    bounded observation - in full when relevant to the question
    (`_relevant`), a stub with a marker otherwise; `relevance="all"` keeps
    every observation. A stub carries no observation text at all, so a
    secret in an omitted observation cannot leak through it."""
    return _transcript(traj, relevance)[0]


def cap_observation(obs: str, limit: int = OBSERVATION, tail: int = OBSERVATION_TAIL) -> str:
    """An observation over the cap: its head, a marker saying how much is left out, its tail."""
    if len(obs) <= limit:
        return obs
    head = limit - tail
    return obs[:head] + f" … [{len(obs) - limit} characters left out here] … " + obs[-tail:]


def _transcript(traj, relevance: str) -> tuple:
    instruction = instruction_of(traj) if relevance != "all" else ""
    terms = _terms(instruction)
    steps = [s for s in traj.steps if s.source == "agent"]
    out = []
    kept = omitted = omitted_chars = 0
    for i, s in enumerate(steps):
        if s.message:
            out.append(f"[agent] {s.message}")
        following = next((t.message for t in steps[i + 1:] if t.message), "") or ""
        for c in s.tool_calls or []:
            args = c.get("arguments")
            obs = str(c.get("observation") or "")
            if relevance != "all" and obs and not _relevant(c, obs, instruction, terms, following):
                omitted += 1; omitted_chars += len(obs)
                obs = OMITTED.format(n=len(obs))
            else:
                if obs:
                    kept += 1
                if len(obs) > OBSERVATION:
                    obs = cap_observation(obs)
            out.append(f"[tool {c.get('name')}] {args if args else ''}\n[observation] {obs}".rstrip())
    if relevance != "all" and (kept or omitted):
        out.insert(0, KEPT_MARKER.format(kept=kept, omitted=omitted))
    return "\n".join(out), {"kept": kept, "omitted": omitted, "omitted_chars": omitted_chars}


def verifier_text(trial_dir: str, limit: int = 8_000) -> str:
    """What the verifier wrote, from the files the artifact reader allows and nothing else."""
    parts = []
    for name in ("verifier/reward.txt", "verifier/reward-details.json", "verifier/reward.json", "verifier/report.json", "verifier/test-stdout.txt"):
        path = os.path.join(trial_dir, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(limit + 1)
        except OSError:
            continue
        parts.append(f"[{name}]\n{text[:limit]}")
    return "\n".join(parts)


def build(kind: str, traj, trial=None, trial_dir: Optional[str] = None, other=None, limit: int = LIMIT,
          relevance: str = RELEVANCE, **extra) -> tuple:
    """The state of one kind, redacted, truncated: (text, truncated).
    `relevance` is the observation policy `transcript_of` applies to the
    built-in kinds ("question" by default, "all" keeps every observation).

    `trial`: instruction + steps + final message. `failure`: the same plus
    the recorded error and the verifier text. `final`: the final message.
    `pair`: instruction + transcript A + transcript B (`other` is B's
    trajectory). Any other kind is a registered builder's (`register`),
    given the same keywords."""
    if kind in BUILDERS:
        text = BUILDERS[kind](traj, trial=trial, trial_dir=trial_dir, other=other, **extra)
    elif kind == "final":
        text = "[final message]\n" + (traj.last_agent_message or "")
    elif kind == "pair":
        # each transcript truncated on its own, so a long A cannot push B out of the state
        half = max(2_000, (limit - 4_000) // 2)
        a, ta = truncate(transcript_of(traj, relevance), half)
        b, tb = truncate(transcript_of(other, relevance), half)
        text = "[instruction]\n" + instruction_of(traj) + "\n\n[transcript A]\n" + a + "\n[final message A]\n" + (traj.last_agent_message or "") \
            + "\n\n[transcript B]\n" + b + "\n[final message B]\n" + (other.last_agent_message or "")
        text, t = truncate(redact(text), limit)
        return text, t or ta or tb
    else:
        text = "[instruction]\n" + instruction_of(traj) + "\n\n[transcript]\n" + transcript_of(traj, relevance) + "\n\n[final message]\n" + (traj.last_agent_message or "")
        if kind == "failure":
            err = ""
            if trial is not None and (trial.exception_type or trial.exception_message):
                err = f"{trial.exception_type}: {trial.exception_message}".strip(": ")
            text += "\n\n[recorded error]\n" + (err or "none recorded")
            text += "\n\n[verifier]\n" + (verifier_text(trial_dir) if trial_dir else "")
    return truncate(redact(text), limit)


# --- Purpose-built states, each a slice and never the whole transcript

CONTROL_TRANSCRIPT = 6_000      # characters of a control's own transcript kept in its state

CONTROL_PURPOSE = {
    "nop": "nop does nothing to the task; the verifier must reject it (reward 0), and no previously-passing test may fail.",
    "oracle": "oracle applies the task's reference solution; the verifier must accept it (reward 1).",
}


def control_state(traj, trial=None, trial_dir=None, control: str = "", outcome_label: str = "", **_) -> str:
    """The `control` state: which control and what it is for (written here),
    its recorded outcome and error, the verifier text, and its own transcript
    bounded - the agent's run is never in it."""
    err = ""
    if trial is not None and (getattr(trial, "exception_type", "") or getattr(trial, "exception_message", "")):
        err = f"{trial.exception_type}: {trial.exception_message}".strip(": ")
    transcript = "none recorded"
    if traj is not None and not getattr(traj, "error", None):
        transcript, _cut = truncate(transcript_of(traj) or "none recorded", CONTROL_TRANSCRIPT)
    reward = getattr(trial, "reward", None) if trial is not None else None
    outcome = outcome_label or (getattr(trial, "outcome", "") if trial is not None else "") or "not recorded"
    # what the reader counted, written here as sentences - the judge is never asked to count
    counted = []
    if trial is not None and (getattr(trial, "f2p_pass", 0) or getattr(trial, "f2p_fail", 0)):
        counted.append(f"Tests the task expects to pass after the change: {trial.f2p_pass} passed, {trial.f2p_fail} failed.")
    if trial is not None and (getattr(trial, "p2p_pass", 0) or getattr(trial, "p2p_fail", 0)):
        counted.append(f"Tests that passed before the change: {trial.p2p_pass} still pass, {trial.p2p_fail} now fail.")
        held = list(getattr(trial, "p2p_failures", None) or [])[:10]
        if held:
            counted.append("Previously-passing tests that failed: " + ", ".join(held) + (" …" if getattr(trial, "p2p_fail", 0) > 10 else ""))
    return ("[control]\n" + (CONTROL_PURPOSE.get(control) or f"{control or 'a control'}: a Harbor control run") +
            "\n\n[recorded outcome]\n" + outcome + (f" (reward {reward})" if reward is not None else "") +
            ("\n\n[test counts, from the verifier's report]\n" + "\n".join(counted) if counted else "") +
            "\n\n[recorded error]\n" + (err or "none recorded") +
            "\n\n[verifier]\n" + ((verifier_text(trial_dir) if trial_dir else "") or "nothing recorded") +
            "\n\n[transcript]\n" + transcript)


def task_state(traj, instruction: str = "", **_) -> str:
    """The `task` state: the instruction alone - the same text for every
    attempt at the task, and nothing of any run."""
    return "[instruction]\n" + (instruction or (instruction_of(traj) if traj is not None else ""))


register("control", control_state)
register("task", task_state)
