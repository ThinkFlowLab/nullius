"""What a run needs of the machine before its first unit.

``install`` puts what a definition names - an agent's or a capability's
packages, a driver's environment, an upstream, a dataset - under
``STANDARD_DIR``, once. ``hostreq`` checks the programs and the GPU memory a
benchmark's ``host`` asks for, and says the commands that install them.
``network_requirements`` checks the public web access an environment declares.

What Review asks of each machine before Run is ``nullius.prerequisites``, one
row per need; it asks ``hostreq`` here.
"""
