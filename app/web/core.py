"""The Flask application object and its configuration. Every capability module registers its routes on it; nothing else lives here."""
from __future__ import annotations

import os
from flask import Flask



# The source app/ directory, or the wheel's _nullius_app/: templates and
# static stay beside the web package in both layouts.
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, root_path=APP_DIR)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GiB per folder upload
# Signs the session cookie (flash messages, the form token). A new random key
# per process: nothing here outlives the run, and nobody needs to set one.
app.secret_key = os.urandom(32)

