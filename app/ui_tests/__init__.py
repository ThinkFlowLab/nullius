"""Flask integration checks, separate from the framework-free data suite."""

from nullius import execution, prerequisites

# The suite runs where docker may not answer, and a run here asks it before it starts
# (execution.docker_here), as Review does (prerequisites.docker_here): answered as
# present, unless a test says otherwise.
execution.docker_here = lambda sh, needs="": None
prerequisites.docker_here = lambda fresh=False: None
