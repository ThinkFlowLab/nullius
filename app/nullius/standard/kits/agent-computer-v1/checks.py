"""The computer kit's first checks: what each one draws, and its verdict.

A verdict is a function of three records and nothing else: the fixture's log
(``fixture.py``: one dict an event, ``kind`` and ``t`` on the desktop's
clock), the actions' times (``desktop.Desktop.act``: one dict an action,
``start`` and ``end`` on the same clock) and the agent's last message.

- ``see``: the last message holds the code the window drew;
- ``click``: the log holds a left press on the named button and on none of
  the five others;
- ``type``: the field held exactly the word at the last Enter.

``click`` and ``type`` pass only when every pointer and key event lies
inside an action's time: input sent any other way, through the desktop's
Python for one, fails them. An event counts as inside from the action's start
to ``SLACK`` seconds after its end, the time the fixture may take to take an
event in once the action has sent it. The kit's pause after each action is
longer, so nothing sent after an action has answered falls in it.
"""
import re
import secrets

SLACK = 0.5
CODE_LENGTH = 12
ALPHABET = "ACDEFGHJKMNPRTWXY34679"         # no pair a glance confuses: 0/O, 1/I/L, 5/S, 2/Z, 8/B, U/V
LABELS = ("APPLE", "BRICK", "CLOUD", "DELTA", "EMBER", "FROST", "GRAPE", "HONEY", "IVORY", "JUMBO",
          "KNOT", "LEMON", "MAPLE", "NORTH", "OCEAN", "PIANO", "QUILT", "RIVER", "STONE", "TIGER",
          "UMBRA", "VELVET", "WHEAT", "YEAST")
POINTER = ("press", "release", "scroll")
KEYS = ("key", "keyup")
CHECKS = ("see", "click", "type")


def word(rng=None, length=8):
    """A pronounceable word that is no word: consonant, vowel, and again."""
    rng = rng or secrets.SystemRandom()
    return "".join(rng.choice("bdfgklmnprstvz" if i % 2 == 0 else "aeiou") for i in range(length))


def draw(check, rng=None):
    """What the fixture draws for ``check`` (its stdin) and what the verdict
    expects: ``(values, expected)``. Nothing the verdict expects is in the
    values but what the screen shows."""
    rng = rng or secrets.SystemRandom()
    if check == "see":
        code = "".join(rng.choice(ALPHABET) for _ in range(CODE_LENGTH))
        return {"screen": "see", "code": code}, {"code": code}
    if check == "click":
        labels = rng.sample(LABELS, 6)
        return {"screen": "click", "labels": labels}, {"target": rng.choice(labels), "labels": labels}
    if check == "type":
        return {"screen": "type"}, {"word": word(rng)}
    raise ValueError(f"no such check: {check!r}")


def inside(t, actions, slack=SLACK):
    """Whether the time ``t`` lies in one action's time."""
    return any(a["start"] <= t <= a["end"] + slack for a in actions)


def outside(events, actions, slack=SLACK):
    """The pointer and key events that lie in no action's time."""
    return [e for e in events if e.get("kind") in POINTER + KEYS and not inside(e.get("t", -1.0), actions, slack)]


def _flat(text):
    return re.sub(r"[^A-Za-z0-9]", "", str(text or "")).upper()


def _verdict(why):
    return {"passed": not why, "why": why}


def _input_rule(events, actions):
    stray = outside(events, actions)
    if stray:
        first = stray[0]
        return f"{len(stray)} input event(s) outside every action, the first a {first['kind']} at {first.get('t')}"
    return ""


def see(expected, events, actions, message):
    code = expected["code"]
    return _verdict("" if code in _flat(message) else "the last message does not hold the code")


def click(expected, events, actions, message):
    target = expected["target"]
    presses = [e for e in events if e.get("kind") == "press" and e.get("button") == 1]
    hits = {e.get("hit") for e in presses}
    if target not in hits:
        return _verdict(f"no left press on the button {target}")
    others = sorted(h for h in hits if h != target and h in expected.get("labels", ()))
    if others:
        return _verdict(f"a press on another button as well: {', '.join(others)}")
    return _verdict(_input_rule(events, actions))


def type_word(expected, events, actions, message):
    entered = [e for e in events if e.get("kind") == "enter"]
    if not entered:
        return _verdict("Enter was never pressed in the field")
    if entered[-1].get("value") != expected["word"]:
        return _verdict(f"the field held {entered[-1].get('value')!r} at the last Enter")
    return _verdict(_input_rule(events, actions))


VERDICTS = {"see": see, "click": click, "type": type_word}


def verdict(check, expected, events, actions, message):
    """``{passed, why}`` for one check; ``why`` is empty when it passed."""
    return VERDICTS[check](expected, events, actions, message)
