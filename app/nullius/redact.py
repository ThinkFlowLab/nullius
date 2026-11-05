"""Secret redaction.

Job directories are arbitrary text from wherever an exception was raised, and
this app renders that text into a browser. Never surface raw text from a job
directory without passing it through :func:`redact` first.

``app/tests/test_redact.py`` pins what the patterns below catch and what they
leave alone.

Two properties matter:

* **Anchored.** ``sk-`` must start at a token boundary, or the pattern fires
  inside base64 payloads and eats the rest of a diff.
* **Separator-agnostic.** Credentials arrive as ``KEY=v`` from shell commands
  *and* as ``"key": "v"`` from JSON — ``trajectory.py`` renders tool arguments
  with ``json.dumps``, so the JSON form is the common one here.
"""

from __future__ import annotations

import re

_PATTERNS = (
    # OpenAI-style keys. The lookbehind is the whole point: without it this
    # matches "sk-" appearing mid-base64 and swallows everything after it.
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}"),
    # Bearer tokens in headers. Keeps the word "bearer" so the line still reads.
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # KEY=value and "key": "value", quoted or not, escaped or not. Longer
    # alternatives lead: `access_token` must win before `token` can match it.
    # The name may carry a prefix joined by `_` or `-` (GH_TOKEN,
    # CURSOR_API_KEY, AWS_SECRET_ACCESS_KEY), which `\b` would refuse since `_`
    # is a word character; a letter or digit before it still does not count.
    re.compile(
        r"(?i)(?<![A-Za-z0-9])(api[_-]?key|access[_-]?token|access[_-]?key|authorization|password|secret|token)"
        r"(\s*[=:]\s*|\\?[\"']?\s*[=:]\s*\\?[\"']?)"
        r"[A-Za-z0-9._~+/=-]{8,}"
    ),
)

# Credentials that need no name beside them: their prefix says what they are
# (GitHub, Hugging Face, AWS access key ids, Google API keys, Slack).
_BARE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[abeoprs]-[A-Za-z0-9-]{10,})"
)
# "Authorization: token X" and "Authorization: Basic X": the scheme word is
# kept, as with Bearer. Only after the header name, where "basic" is no word.
_SCHEME = re.compile(r"(?i)(authorization\\?[\"']?\s*[=:]\s*\\?[\"']?\s*(?:token|basic)\s+)[A-Za-z0-9._~+/=-]{8,}")
# user:password@ in a URL: the user stays, the password goes.
_USERINFO = re.compile(r"(?<=://)([^/\s:@\[\]]+):([^/\s@\[\]]+)@")

# Angle brackets would survive Jinja's autoescaping as &lt;…&gt;. Square
# brackets read the same in a browser as they do in a terminal.
_REPLACEMENT = "[REDACTED]"


def redact(text: str | None) -> str:
    """Return *text* with anything credential-shaped replaced.

    The key name survives; only the value goes. An error message that reads
    "auth failed for [REDACTED]" is still diagnostic, and over-redaction
    destroys exactly the text someone opened the page to read.
    """
    if not text:
        return ""
    out = _PATTERNS[0].sub(_REPLACEMENT, text)
    out = _BARE.sub(_REPLACEMENT, out)
    out = _USERINFO.sub(lambda m: m.group(0) if m.group(2) == _REPLACEMENT else f"{m.group(1)}:{_REPLACEMENT}@", out)
    out = _SCHEME.sub(rf"\1{_REPLACEMENT}", out)
    out = _PATTERNS[1].sub(rf"\1 {_REPLACEMENT}", out)
    return _PATTERNS[2].sub(rf"\1\2{_REPLACEMENT}", out)
