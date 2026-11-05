"""A fake Harbor upstream on disk, for the pin's tests: never the network.

``upstream(tmp)`` makes bare git repositories served by ``file://`` URLs:

- ``harbor.git`` - a package named ``harbor`` whose console script answers
  ``harbor agents list --json`` the way Harbor's does, with a private
  dependency under an extra (``jw = ["priv-core==0.1.9"]``, the package named
  in ``[tool.uv.sources]`` as a git repository, as a fork names
  its private runtime) and one named by URL (``url = ["other-core @ git+file://…"]``);
  branches ``main`` (the default) and ``dev``, tags ``v0.9`` (annotated) and
  ``v0.10``;
- ``priv.git`` and ``other.git`` - those two dependencies;
- ``old.git`` - a Harbor whose CLI has no ``agents list``.

Every package builds with a PEP 517 backend kept in its own tree that
requires nothing, so ``uv build`` needs no index; set ``UV_OFFLINE=1`` and the
scan resolves from the wheels alone.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

BACKEND = '''"""A PEP 517 backend that requires nothing: pyproject.toml in, a wheel out."""
import base64, hashlib, os, tomllib, zipfile


def get_requires_for_build_wheel(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    with open("pyproject.toml", "rb") as fh:
        p = tomllib.load(fh)["project"]
    name, version = p["name"], p["version"]
    dist = name.replace("-", "_")
    tag = dist + "-" + version
    lines = ["Metadata-Version: 2.1", "Name: " + name, "Version: " + version]
    if p.get("requires-python"):
        lines.append("Requires-Python: " + p["requires-python"])
    lines += ["Requires-Dist: " + r for r in p.get("dependencies", [])]
    for extra, reqs in (p.get("optional-dependencies") or {}).items():
        lines.append("Provides-Extra: " + extra)
        lines += ["Requires-Dist: %s; extra == \\"%s\\"" % (r, extra) for r in reqs]
    files = {tag + ".dist-info/METADATA": "\\n".join(lines) + "\\n",
             tag + ".dist-info/WHEEL": "Wheel-Version: 1.0\\nGenerator: fake\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"}
    if p.get("scripts"):
        files[tag + ".dist-info/entry_points.txt"] = "[console_scripts]\\n" + "".join("%s = %s\\n" % kv for kv in p["scripts"].items())
    for top, _, names in os.walk(dist):
        for n in names:
            if n.endswith(".py"):
                with open(os.path.join(top, n), encoding="utf-8") as fh:
                    files[os.path.join(top, n).replace(os.sep, "/")] = fh.read()
    out = os.path.join(wheel_directory, tag + "-py3-none-any.whl")
    record = []
    with zipfile.ZipFile(out, "w") as z:
        for arc, text in files.items():
            data = text.encode()
            z.writestr(arc, data)
            record.append("%s,sha256=%s,%d" % (arc, base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), len(data)))
        record.append(tag + ".dist-info/RECORD,,")
        z.writestr(tag + ".dist-info/RECORD", "\\n".join(record) + "\\n")
    return os.path.basename(out)
'''

CLI = '''import json, sys


def main():
    args = sys.argv[1:]
    if args[:3] == ["agents", "list", "--json"]:
        names = ["nop", "oracle", "terminus-2", "claude-code"]
        try:
            import priv_core  # noqa: F401 - the extra's own agent registers only when the extra is there
            names.append("jw-agent")
        except ImportError:
            pass
        print(json.dumps(names, indent=2))
        return 0
    if args[:1] == ["run"]:
        print("fake harbor run " + " ".join(args[1:]))
        return 0
    print("No such command " + " ".join(args[:2]), file=sys.stderr)
    return 2
'''

OLD_CLI = '''import sys


def main():
    print("No such command 'agents'.", file=sys.stderr)
    return 2
'''


def git(*args, cwd=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def package(directory, name, version, files, dependencies=(), extras=None, scripts=None, sources=None):
    os.makedirs(os.path.join(directory, "_backend"), exist_ok=True)
    with open(os.path.join(directory, "_backend", "fakeback.py"), "w") as fh:
        fh.write(BACKEND)
    lines = ["[project]", f'name = "{name}"', f'version = "{version}"', 'requires-python = ">=3.8"',
             "dependencies = " + json.dumps(list(dependencies))]
    if extras:
        lines.append("[project.optional-dependencies]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in extras.items()]
    if scripts:
        lines.append("[project.scripts]")
        lines += [f'{k} = "{v}"' for k, v in scripts.items()]
    lines += ["[build-system]", "requires = []", 'build-backend = "fakeback"', 'backend-path = ["_backend"]']
    if sources:
        lines.append("[tool.uv.sources]")
        lines += [f"{k} = {{ {', '.join(f'{a} = {json.dumps(b)}' for a, b in v.items())} }}" for k, v in sources.items()]
    with open(os.path.join(directory, "pyproject.toml"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    for path, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(directory, path)), exist_ok=True)
        with open(os.path.join(directory, path), "w") as fh:
            fh.write(text)


def publish(work, bare):
    """``work`` committed and pushed to a new bare repository; its commit."""
    git("init", "-q", "-b", "main", cwd=work)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "init", cwd=work)
    git("init", "-q", "--bare", "-b", "main", bare)
    git("push", "-q", bare, "main", cwd=work)
    return git("rev-parse", "HEAD", cwd=work)


def upstream(tmp):
    """The fake repositories under ``tmp``: ``{url, commit, dev, v09, v010,
    priv_url, priv_commit, other_url, old_url, old_commit}``."""
    out = {}
    work = os.path.join(tmp, "work")
    for name, pkg, version in (("priv", "priv-core", "0.1.9"), ("other", "other-core", "1.0")):
        d = os.path.join(work, name)
        package(d, pkg, version, {f"{pkg.replace('-', '_')}/__init__.py": "X = 1\n"})
        out[f"{name}_commit"] = publish(d, os.path.join(tmp, f"{name}.git"))
        out[f"{name}_url"] = "file://" + os.path.join(tmp, f"{name}.git")
    d = os.path.join(work, "harbor")
    package(d, "harbor", "0.22.0+fork.1", {"harbor/__init__.py": "", "harbor/cli.py": CLI},
            extras={"jw": ["priv-core==0.1.9"], "url": [f"other-core @ git+{out['other_url']}@{out['other_commit']}"], "self": ["harbor[jw]"]},
            scripts={"harbor": "harbor.cli:main"}, sources={"priv-core": {"git": out["priv_url"], "rev": out["priv_commit"]}})
    bare = os.path.join(tmp, "harbor.git")
    out["commit"] = publish(d, bare)
    out["url"] = "file://" + bare
    git("tag", "-a", "v0.9", "-m", "v0.9", cwd=d)
    with open(os.path.join(d, "harbor", "later.py"), "w") as fh:
        fh.write("LATER = 1\n")
    git("add", "-A", cwd=d); git("commit", "-q", "-m", "later", cwd=d)
    out["v010"] = git("rev-parse", "HEAD", cwd=d)
    git("tag", "v0.10", cwd=d)
    git("branch", "dev", cwd=d)
    git("reset", "-q", "--hard", out["commit"], cwd=d)
    git("push", "-q", bare, "dev", "v0.9", "v0.10", cwd=d)
    out["v09"], out["dev"] = out["commit"], out["v010"]
    o = os.path.join(work, "old")
    package(o, "harbor", "0.1.0", {"harbor/__init__.py": "", "harbor/cli.py": OLD_CLI}, scripts={"harbor": "harbor.cli:main"})
    out["old_commit"] = publish(o, os.path.join(tmp, "old.git"))
    out["old_url"] = "file://" + os.path.join(tmp, "old.git")
    shutil.rmtree(work)
    return out
