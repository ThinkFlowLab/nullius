"""Prepare DigitalMate and WorkSwarm in Nullius's container installation."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess

from nullius import paths, settings

settings.load(paths.settings_file())
from nullius.standard import container, definitions, install


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Fetch the same pinned commit from a local Git checkout")
    args = parser.parse_args()
    definition = definitions.get("agent", "digitalmate")
    if definition is None:
        raise SystemExit("DigitalMate is missing from the effective catalogue")
    if args.source:
        source = args.source.resolve()
        subprocess.run(["git", "-C", str(source), "cat-file", "-e",
                        definition["upstream"]["tested"] + "^{commit}"], check=True)
        # Fetch the same commit locally without changing either repo's Git config.
        index = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
        os.environ["GIT_CONFIG_COUNT"] = str(index + 1)
        os.environ[f"GIT_CONFIG_KEY_{index}"] = "url." + source.as_uri() + ".insteadOf"
        os.environ[f"GIT_CONFIG_VALUE_{index}"] = definition["upstream"]["repository"]
    image = container.base_image()
    run_in = lambda cmd, where, env=None: container.install_argv(image, where, cmd, env)
    location = Path(install.packages(definition, run_in=run_in, image=image))
    marker = location / ".digitalmate-runtime-ready"
    if marker.exists() and marker.read_text().strip() == definition["upstream"]["tested"]:
        print("DigitalMate runtime already prepared:", location)
        return
    runtime = location / "runtime"
    python = str(runtime / "bin/python")
    source = location / "src"
    commands = []
    # When Nullius installs root contracts, add the service as the Dockerfile does.
    if definition["install"].get("project") == ".":
        commands.append(["uv", "pip", "install", "--python", str(location / ".venv/bin/python"),
                         str(source / "harness/personal-agent/services/agent_bridge") + "[geo]"])
    commands += [
        ["uv", "venv", "--clear", "--seed", "--python", str(location / ".venv/bin/python"), str(runtime)],
        [python, str(source / "harness/personal-agent/deploy/install_runtime.py")],
    ]
    try:
        with (location / "digitalmate-runtime-install.log").open("a") as log:
            for command in commands:
                subprocess.run(run_in(command, str(location), {"PIP_NO_CACHE_DIR": "1"}),
                               env=install.install_env(), stdout=log, stderr=log, check=True)
    finally:
        install.remove_scratch(str(location))
    marker.write_text(definition["upstream"]["tested"] + "\n")
    print("DigitalMate runtime prepared:", location)


if __name__ == "__main__":
    main()
