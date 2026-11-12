"""Docker execution for benchmark setups, whose answerer lives in the driver.

Unlike an evaluated agent, this trusted process owns the reference data. It gets
only the selected benchmark, its upstream/data, an isolated Linux install cache,
the job workspace and backend Python code. Its network can fetch benchmark data;
model credentials remain in the relay. No host virtual environment is reused.
Its declared credentials come as an owner-only file in a key folder, mounted
read-only; :func:`main` loads them into the driver's environment, never the
container's, which ``docker inspect`` shows. The folder is removed once the
driver answers or ends.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import uuid
from urllib.parse import urlsplit

from nullius.standard import container, definitions, hostreq, install, jsonl

UV_VERSION = "0.13.4"
CREDENTIALS_IN = "/opt/nullius/credentials"    # the driver's credentials' one file, in its container


def door_base(door):
    # A rootless door is another container. The driver may already be running,
    # so use its bridge address rather than changing the driver's hosts file.
    return door.base.replace("host.docker.internal", door.hosts["host.docker.internal"], 1) if door.container else door.base


def credential_names(bench: dict) -> set:
    """The variables the benchmark's credentials for its driver set
    (``needs_secrets``, ``to: driver``): a value's or a server's name, a
    file's ``env``."""
    return {s.get("env") if s["kind"] == "file" else s["name"]
            for s in definitions.secrets_of(bench) if s["to"] == "driver"} - {None}


def image(base: str, bench: dict, needs: list, log: str) -> str:
    text = f"FROM {base}\nCOPY --from=ghcr.io/astral-sh/uv:{UV_VERSION} /uv /uvx /usr/local/bin/\n"
    commands = hostreq.install_lines(bench, needs, sudo="")
    if commands:
        text += "RUN apt-get update && " + " && ".join(c.strip() for c in commands) + " && rm -rf /var/lib/apt/lists/*\n"
    tag = "nullius/setup:" + hashlib.sha256(text.encode()).hexdigest()[:12]
    if not container.present(tag):
        result = container._docker(["build", "-t", tag, "-"], input=text, timeout=1800, check=False)
        with open(log, "a") as f:
            f.write(result.stdout + result.stderr)
        if result.returncode:
            raise container.Unavailable(f"building {tag}: {(result.stderr or result.stdout)[-300:]}")
    return tag


class Driver:
    def __init__(self, bench, *, image, app, job, work, upstream, data, needs):
        self.bench, self.image, self.app, self.job = bench, image, app, job
        self.work, self.upstream, self.data = work, upstream, data
        self.cache = os.path.join(install.HOME, "docker-setups", image.rsplit(":", 1)[-1])
        os.makedirs(self.cache, exist_ok=True)
        self.config = os.path.join(work, ".work", "setup-driver.json")
        with open(self.config, "w") as f:
            json.dump({"bench": bench, "upstream": upstream, "needs": needs}, f)

    def command(self, env, cwd, *, prepare=False, files=(), credentials=None):
        # Only the backend package, not app/ or any user configuration directory.
        mounts = [(os.path.join(self.app, "nullius"), True), (self.bench["_here"], True),
                  (self.cache, False), (self.work, False)]
        if self.upstream:
            mounts.append((self.upstream, False))  # upstreams may cache data beside their code
        if self.data and not any(self.data == p or self.data.startswith(p + os.sep) for p, _ in mounts):
            mounts.append((self.data, True))
        mounts += [(p, True) for p in dict.fromkeys(files) if p not in {m[0] for m in mounts}]
        values = {k: v for k, v in env.items() if k not in container.HOST_ONLY}
        values.update(PATH=container.IMAGE_PATH, PYTHONPATH=self.app, PYTHONDONTWRITEBYTECODE="1",
                      UV_CACHE_DIR=os.path.join(self.cache, "uv-cache"),
                      UV_PYTHON_INSTALL_DIR=os.path.join(self.cache, "python"))
        name = "nullius-setup-" + uuid.uuid4().hex[:20]
        cmd = ["python3", "-m", "nullius.standard.setup_container", self.config, self.cache,
               "prepare" if prepare else "run"] + ([CREDENTIALS_IN] if credentials else [])
        argv = container.run_argv(self.image, cmd, name=name, network=container.BRIDGE if container.podman() else "bridge", job=self.job,
                                  workdir=cwd, mounted=mounts, env=values, path=container.IMAGE_PATH)
        argv[3:3] = ["--add-host", "host.docker.internal:host-gateway"]
        if credentials:                          # as the relay's key: fails when the engine cannot see it
            argv[3:3] = ["--mount", f"type=bind,source={credentials},target={CREDENTIALS_IN},readonly"]
        return argv, container.program_env(values), name

    def prepare(self):
        container.make_bridge()
        home = os.path.join(self.work, ".work", "setup-install-home")
        os.makedirs(home, exist_ok=True)
        argv, env, name = self.command({"HOME": home}, self.work, prepare=True)
        try:
            result = container._docker(argv[1:], env=env, timeout=1800, check=False)
        finally:
            with contextlib.suppress(container.Unavailable):
                container._docker(["rm", "-f", name], check=False)
        with open(os.path.join(self.work, ".work", "setup-install.log"), "a") as f:
            f.write(result.stdout + result.stderr)
        if result.returncode:
            raise install.InstallFailed("container driver install: " + (result.stderr or result.stdout)[-500:])

    def start(self, env, cwd, log, *, files=(), servers=(), stopped=None):
        # Embedding routes are environment variables; their forwarders live as
        # long as the process. Per-request answer/judge/simulator routes below
        # live only until that protocol reply returns.
        doors, keys, credentials = [], None, None
        env = dict(env)
        try:
            for key, value in env.items():
                target = "tcp://" + value if key in servers else value
                if (key.endswith("BASE_URL") or key in servers) and container.on_loopback(target):
                    door = container.Loopback(target, image=self.image, job=f"{self.job}-{uuid.uuid4().hex[:12]}").start()
                    doors.append(door)
                    env[key] = urlsplit(door_base(door)).netloc if key in servers else door_base(door)
            held = {k: env.pop(k) for k in sorted(credential_names(self.bench)) if k in env}
            if held:
                keys = container.key_folder(self.job)
                credentials = container.secret_file(os.path.join(keys, "credentials"), json.dumps(held))
            argv, client, name = self.command(env, cwd, files=files, credentials=credentials)
            process = Process(argv, cwd, client, log, stopped=stopped, name=name, image=self.image, doors=doors, keys=keys)
            try:
                return process.start()
            except BaseException:
                process.close()
                raise
        except BaseException:
            for door in doors:
                door.stop()
            if keys:
                shutil.rmtree(keys, ignore_errors=True)
            raise


class Process(jsonl.Proc):
    def __init__(self, *args, name, doors, image="", keys=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.name, self.doors, self.image = name, doors, image
        self.keys = keys                         # the credentials' folder, until the driver answers or ends

    def forget(self):
        keys, self.keys = self.keys, None
        if keys:
            shutil.rmtree(keys, ignore_errors=True)

    def request(self, op, timeout=600, **kw):
        doors = []
        def rewrite(value):
            if isinstance(value, dict):
                return {k: url(v) if k == "base_url" and isinstance(v, str) else rewrite(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rewrite(v) for v in value]
            return value
        def url(value):
            if not container.on_loopback(value):
                return value
            door = container.Loopback(value, image=self.image, job=f"{self.name}-{len(doors)}").start()
            doors.append(door)
            return door_base(door)
        try:
            return super().request(op, timeout=timeout, **rewrite(kw))
        finally:
            for door in doors:
                door.stop()
            self.forget()                        # answered or ended: main() read them, or never will

    def close(self):
        try:
            super().close()
        finally:
            self.forget()
            with contextlib.suppress(container.Unavailable):
                container._docker(["rm", "-f", self.name], check=False)
            for door in self.doors:
                door.stop()
            self.doors = []


def main():
    config, cache, mode, *credentials = sys.argv[1:]
    with open(config) as f:
        record = json.load(f)
    bench = record["bench"]
    definition = definitions.driver_install(bench)
    where = install.packages_dir(definition, cache) if definition["install"] else None
    if mode == "prepare":
        missing = hostreq.missing(bench, record["needs"])
        if missing:
            raise install.InstallFailed(f"setup container lacks required programs: {missing}")
        if where:
            install.packages(definition, cache, src=record["upstream"])
        return
    if where and not install.ready(where):
        raise install.InstallFailed("setup container's driver environment has not been prepared")
    env = dict(os.environ)
    for path in credentials:                     # a JSON object: no shell reads a value
        with open(path) as f:
            env.update(json.load(f))
    command, extra = install.driver_command(bench, where, cache)
    env.update(extra)
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main()
