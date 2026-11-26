"""The persona's AppWorld tools, as the standard's stdio MCP server.

The official Pi-Bench image carries AppWorld's API server and its MCP server.
This file starts one AppWorld API container per unit, then joins this process's
stdin and stdout to the upstream's own MCP server inside that container
(``appworld serve mcp stdio``) - the tools are the upstream's, not ours.

    python3 appworld_mcp.py CONTAINER UPSTREAM_DATA_DIR PERSONA

The container is created on first use, named CONTAINER, with UPSTREAM_DATA_DIR
(pinned upstream's ``data/``) mounted read-only at ``/opt/proactive/bench/data``
so the persona's ``tools.yaml`` is the upstream's own. The image is
``zzzhr97/pi-bench:latest`` unless ``NULLIUS_PI_BENCH_IMAGE`` names another.
"""
import os
import subprocess
import sys
import time

IMAGE = os.environ.get("NULLIUS_PI_BENCH_IMAGE", "zzzhr97/pi-bench:latest")
API_PORT = 9000
API_WAIT_SECONDS = 180


def say(message):
    print(f"[pi-bench appworld] {message}", file=sys.stderr, flush=True)


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], text=True, capture_output=True, **kwargs)


def running(container):
    got = docker("inspect", "--format", "{{.State.Running}}", container)
    return got.returncode == 0 and got.stdout.strip() == "true"


def start(container, data_dir, persona):
    """Create (or restart) the unit's AppWorld API container, then wait for its port."""
    if docker("image", "inspect", IMAGE).returncode != 0:
        raise SystemExit(f"[pi-bench appworld] image {IMAGE} is not present: docker pull {IMAGE}")
    docker("rm", "-f", container)
    tools = f"/opt/proactive/bench/data/{persona}/tools.yaml"
    command = (
        f"appworld serve apis --root /opt/proactive/appworld --port {API_PORT} "
        ">/tmp/appworld-api.log 2>&1 & sleep infinity"
    )
    got = docker(
        "run", "-d", "--rm", "--name", container, "--entrypoint", "bash",
        "-v", f"{data_dir}:/opt/proactive/bench/data:ro",
        IMAGE, "-lc", command,
    )
    if got.returncode != 0:
        raise SystemExit(f"[pi-bench appworld] docker run failed: {got.stderr.strip()[-800:]}")
    deadline = time.time() + API_WAIT_SECONDS
    probe = (
        "python -c \"import socket; "
        f"s=socket.create_connection(('127.0.0.1',{API_PORT}),1); s.close()\""
    )
    while time.time() < deadline:
        if running(container) and docker("exec", container, "bash", "-lc", probe).returncode == 0:
            if docker("exec", container, "test", "-f", tools).returncode != 0:
                raise SystemExit(f"[pi-bench appworld] missing tools config in image: {tools}")
            return
        time.sleep(1.0)
    log = docker("exec", container, "bash", "-lc", "tail -20 /tmp/appworld-api.log")
    raise SystemExit(f"[pi-bench appworld] API did not answer in {API_WAIT_SECONDS}s: {log.stdout.strip()}")


def main(argv):
    if len(argv) != 4:
        raise SystemExit("usage: appworld_mcp.py CONTAINER UPSTREAM_DATA_DIR PERSONA")
    container, data_dir, persona = argv[1], argv[2], argv[3]
    if not os.path.isdir(data_dir):
        raise SystemExit(f"[pi-bench appworld] no data directory: {data_dir}")
    if not running(container):
        say(f"starting {container} for {persona}")
        start(container, data_dir, persona)
    tools = f"/opt/proactive/bench/data/{persona}/tools.yaml"
    say(f"serving {persona}'s tools from {container}")
    os.execvp("docker", [
        "docker", "exec", "-i", container,
        "appworld", "serve", "mcp", "stdio",
        "--root", "/opt/proactive/appworld",
        "--remote-apis-url", f"http://127.0.0.1:{API_PORT}",
        "--tools-config-file", tools,
    ])


if __name__ == "__main__":
    main(sys.argv)
