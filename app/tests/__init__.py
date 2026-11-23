"""Tests for the Nullius application backend.

    python3 -m unittest discover -s app/tests -t app

Bare `python3` — no Flask, no install of any kind. `nullius` imports only the
standard library.
The tests hold that line; if one of them ever needs Flask, the layering has been
broken rather than the test being awkward. Route tests live in `app/ui_tests`.
"""

import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

# No test reaches past this machine. A run asks its provider for its model list
# before it starts, and a capped run fetches the price list: asked of a real
# address here, each fails as on a machine offline, and what it serves is
# skipped. A test of either serves its own on the loopback, or patches it.
import urllib.parse as _urls                                    # noqa: E402
import urllib.request as _request                               # noqa: E402

from nullius import providers as _providers                     # noqa: E402

_LOCAL = ("127.0.0.1", "localhost", "::1")
_get, _urlopen = _providers._get, _request.urlopen


def _local(url):
    if (_urls.urlsplit(url).hostname or "") not in _LOCAL:
        raise OSError(f"the test suite reaches no address past this machine: {url}")


def _local_get(url, headers, opener=None, timeout=12):
    if opener is None:
        _local(url)
    return _get(url, headers, opener, timeout)


def _local_urlopen(url, *a, **kw):
    _local(url.full_url if isinstance(url, _request.Request) else url)
    return _urlopen(url, *a, **kw)


_providers._get, _request.urlopen = _local_get, _local_urlopen
