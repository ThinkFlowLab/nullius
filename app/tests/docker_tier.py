"""The docker tier: the tests that build and run containers for real, run
only when asked - ``parallel.py --docker`` here, the nightly ``docker tests``
workflow in CI - and skipped with the reason otherwise. The rest of each
module runs every time."""
from __future__ import annotations

import os
import subprocess

from nullius.standard import container

VAR = "NULLIUS_TEST_DOCKER"


def why_not(*images: str) -> str:
    """Why a docker test cannot run now, or '' when it can: the tier was not
    asked for, docker is not usable, or the base image (and ``images``) is
    not on this machine."""
    if not os.environ.get(VAR):
        return "the docker tier runs with parallel.py --docker"
    why = container.unusable()
    if why:
        return why
    try:
        tags = [container.base_tag(), *images]
    except container.Unavailable as e:
        return str(e)
    for tag in tags:
        if subprocess.run([container.DOCKER, "image", "inspect", tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            return f"{tag} is not here (a docker run builds it)"
    return ""
