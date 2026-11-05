"""Persistent per-user locations, independent of the installation or cwd.

NULLIUS_HOME gives a self-contained config/data/cache tree for a separate
profile. Otherwise use XDG locations on Unix and native user locations on
Windows/macOS. Merely resolving a path creates nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _directory(kind: str, env=None) -> str:
    env = os.environ if env is None else env
    if env.get("NULLIUS_HOME"):
        return os.path.join(os.path.abspath(os.path.expanduser(env["NULLIUS_HOME"])), kind)
    variable = {"config": "XDG_CONFIG_HOME", "data": "XDG_DATA_HOME", "cache": "XDG_CACHE_HOME"}[kind]
    explicit = env.get(variable, "")
    if explicit and os.path.isabs(explicit):
        return os.path.join(explicit, "nullius")
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        return os.path.join(env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local"), "Nullius", kind)
    if sys.platform == "darwin":
        base = "Caches" if kind == "cache" else "Application Support"
        return os.path.join(home, "Library", base, "Nullius", *([] if kind == "cache" else [kind]))
    base = {"config": ".config", "data": os.path.join(".local", "share"), "cache": ".cache"}[kind]
    return os.path.join(home, base, "nullius")


def config_dir(env=None) -> str:
    return _directory("config", env)


def data_dir(env=None) -> str:
    return _directory("data", env)


def cache_dir(env=None) -> str:
    return _directory("cache", env)


def settings_file(env=None) -> str:
    return os.path.join(config_dir(env), "settings.json")


def catalogue_dir(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("NULLIUS_CATALOGUE") or os.path.join(data_dir(env), "catalogue")


def workspaces_dir(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("WORKSPACES_DIR") or os.path.join(data_dir(env), "workspaces")


def runs_dir(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("RUNS_DIR") or os.path.join(data_dir(env), "runs")


def secrets_dir(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("SECRETS_DIR") or os.path.join(config_dir(env), "secrets")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Show Nullius's persistent user directories (no secret values).")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    values = {"settings": settings_file(), "secrets": secrets_dir(), "catalogue": catalogue_dir(),
              "workspaces": workspaces_dir(), "runs": runs_dir(), "cache": cache_dir()}
    if args.json:
        print(json.dumps(values, indent=2))
    else:
        for name, path in values.items():
            print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
