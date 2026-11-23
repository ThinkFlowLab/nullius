"""Flask integration checks, separate from the framework-free data suite."""

import tests  # noqa: F401  (no test reaches past this machine: tests/__init__.py)
from nullius import execution, prerequisites

# The suite runs where docker may not answer, and a run here asks it before it starts
# (execution.docker_here): answered as
# present, unless a test says otherwise.
execution.docker_here = lambda sh, needs="", program="docker": None
prerequisites.docker_here = lambda fresh=False: None
