"""Tests for the Nullius application backend.

    python3 -m unittest discover -s app/tests -t app

Bare `python3` — no Flask, no install of any kind. `nullius` imports only the
standard library, which is what lets the backend and its parsing be exercised
from a plain shell or a notebook while the routes cannot.
The tests hold that line; if one of them ever needs Flask, the layering has been
broken rather than the test being awkward. Route tests live in `app/ui_tests`.

Everything under test resolves to this repository's code. Fixtures cover
Harbor jobs, traces, import formats and the Nullius standard.
"""

import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
