"""The computer kit's checks: what each one draws, and its verdict.

A verdict is a function of the desktop's records and the server's, and
nothing else: the fixture's log (``fixture.py``: one dict an event, ``kind``
and ``t`` on the desktop's monotonic clock), the actions' times
(``desktop.Desktop.act``: one dict an action, ``start`` and ``end`` on the
same clock), the agent's last message, and what the server kept of the
agent's calls (``calls``: the call log's lines, ``{tool, start, end,
isError}``; ``past``: the acting calls sent once every step was taken).

- ``see``: the last message holds the code the window drew;
- ``click``: the log holds a left press on the named button and no press,
  of any mouse button, on the five others;
- ``type``: the field held exactly the word at the last Enter;
- ``limit``: six buttons to press with two steps: at least one press on
  them, no input after the last action, and no acting call sent once the
  steps were taken;
- ``drag``: a left press on the handle, then a release inside the zone;
- ``scroll``: the last message holds the code drawn at the list's end, below
  what it shows at first, and the list moved;
- ``recover``: the kit answers the agent's first acting call with an error,
  without running it; a later acting call succeeds and the named button is
  clicked, as for ``click``;
- ``batch``: each of three fields holds its word, within two steps.

Every check passes only when every pointer and key event, Enter in a field
included, lies inside an action's time: input sent any other way, through the
desktop's Python for one, fails it. An event counts as inside from the
action's start to ``SLACK`` seconds after its end, the time the fixture may
need to receive an event the action sent. An action answers only once that
time has passed on the desktop (``desktop.SETTLE``): nothing sent after it has
answered falls in it.
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
KEYS = ("key", "keyup", "enter")
CHECKS = ("see", "click", "type", "limit", "drag", "scroll", "recover", "batch")
BASIC = ("see", "click", "type", "limit")
STEPS = {"limit": 2, "batch": 2}                # every other check: DEFAULT_STEPS
DEFAULT_STEPS = 8
ORDER = ("ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX")
FIELDS = ("ONE", "TWO", "THREE")
ACTING = ("click", "move", "drag", "scroll", "type", "key", "wait", "step", "done", "fail")


def steps(check):
    """The steps a check gives the agent."""
    return STEPS.get(check, DEFAULT_STEPS)


def code(rng):
    return "".join(rng.choice(ALPHABET) for _ in range(CODE_LENGTH))


def word(rng=None, length=8):
    """A pronounceable word that is no word: consonant, vowel, and again."""
    rng = rng or secrets.SystemRandom()
    return "".join(rng.choice("bdfgklmnprstvz" if i % 2 == 0 else "aeiou") for i in range(length))


def draw(check, rng=None):
    """What the fixture draws for ``check`` (its stdin) and what the verdict
    expects: ``(values, expected)``. Nothing the verdict expects is in the
    values but what the screen shows."""
    rng = rng or secrets.SystemRandom()
    if check in ("see", "scroll"):
        c = code(rng)
        return {"screen": check, "code": c}, {"code": c}
    if check in ("click", "recover"):
        labels = rng.sample(LABELS, 6)
        return {"screen": "click", "labels": labels}, {"target": rng.choice(labels), "labels": labels}
    if check == "type":
        return {"screen": "type"}, {"word": word(rng)}
    if check == "limit":
        labels = list(ORDER)
        rng.shuffle(labels)
        return {"screen": "click", "labels": labels}, {"labels": list(ORDER)}
    if check == "drag":
        return {"screen": "drag"}, {}
    if check == "batch":
        return {"screen": "batch", "labels": list(FIELDS)}, {"words": {f: word(rng) for f in FIELDS}}
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


def _first(*whys):
    return next((w for w in whys if w), "")


def _boxes(events):
    """Where the fixture drew its targets: its ``ready`` record's boxes."""
    ready = next((e for e in events if e.get("kind") == "ready"), None)
    return (ready or {}).get("boxes") or {}


def _in(box, e):
    return bool(box) and box[0] <= e.get("x", -1) < box[2] and box[1] <= e.get("y", -1) < box[3]


def see(expected, events, actions, message, calls=(), past=0):
    return _verdict(_first("" if expected["code"] in _flat(message) else "the last message does not hold the code",
                           _input_rule(events, actions)))


def click(expected, events, actions, message, calls=(), past=0):
    target = expected["target"]
    presses = [e for e in events if e.get("kind") == "press"]
    if not any(e.get("hit") == target and e.get("button") == 1 for e in presses):
        return _verdict(f"no left press on the button {target}")
    others = sorted({e.get("hit") for e in presses} & set(expected.get("labels", ())) - {target})
    if others:
        return _verdict(f"a press on another button as well: {', '.join(others)}")
    return _verdict(_input_rule(events, actions))


def type_word(expected, events, actions, message, calls=(), past=0):
    entered = [e for e in events if e.get("kind") == "enter"]
    if not entered:
        return _verdict("Enter was never pressed in the field")
    if entered[-1].get("value") != expected["word"]:
        return _verdict(f"the field held {entered[-1].get('value')!r} at the last Enter")
    return _verdict(_input_rule(events, actions))


def limit(expected, events, actions, message, calls=(), past=0):
    labels = set(expected["labels"])
    if not any(e.get("kind") == "press" and e.get("hit") in labels for e in events):
        return _verdict("no press on the buttons")
    last = max((a["end"] for a in actions), default=None)
    late = [e for e in events if e.get("kind") in POINTER + KEYS and (last is None or e.get("t", -1.0) > last + SLACK)]
    if late:
        return _verdict(f"{len(late)} input event(s) after the last step")
    if past:
        return _verdict(f"{past} acting call(s) sent once every step was taken")
    return _verdict(_input_rule(events, actions))


def drag(expected, events, actions, message, calls=(), past=0):
    zone = _boxes(events).get("zone")
    pressed = None
    for e in events:
        if e.get("kind") == "press" and e.get("button") == 1:
            pressed = e.get("hit") == "handle"
        elif e.get("kind") == "release" and e.get("button") == 1 and pressed:
            if _in(zone, e):
                return _verdict(_input_rule(events, actions))
            pressed = None
    return _verdict("no press on the handle released inside the zone")


def scroll(expected, events, actions, message, calls=(), past=0):
    moved = any(e.get("kind") == "moved" and e.get("top", 0) > 0 for e in events)
    return _verdict(_first("" if expected["code"] in _flat(message) else "the last message does not hold the code",
                           "" if moved else "the list never moved", _input_rule(events, actions)))


def recover(expected, events, actions, message, calls=(), past=0):
    acting = [c for c in calls if c.get("tool") in ACTING]
    failed = next((i for i, c in enumerate(acting) if c.get("isError")), None)
    if failed is None:
        return _verdict("no acting call was answered with an error")
    if not any(not c.get("isError") for c in acting[failed + 1:]):
        return _verdict("no acting call succeeded after the error")
    return click(expected, events, actions, message)


def batch(expected, events, actions, message, calls=(), past=0):
    last = {}
    for e in events:
        if e.get("kind") == "value":
            last[e.get("field")] = e.get("value")
    wrong = [f for f, w in expected["words"].items() if last.get(f) != w]
    return _verdict(_first(f"the field(s) {', '.join(wrong)} do not hold their word" if wrong else "",
                           _input_rule(events, actions)))


VERDICTS = {"see": see, "click": click, "type": type_word, "limit": limit, "drag": drag, "scroll": scroll,
            "recover": recover, "batch": batch}


def verdict(check, expected, events, actions, message, calls=(), past=0):
    """``{passed, why}`` for one check; ``why`` is empty when it passed."""
    return VERDICTS[check](expected, events, actions, message, calls, past)
