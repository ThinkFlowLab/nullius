"""A run, from its units to its job, on one machine or several.

``run`` sets each unit up, produces and scores it. ``job`` writes the result as
a job in Harbor's shape. ``pace`` slows the start of units when the provider
says the run goes too fast; ``retries`` says which failed attempt is tried
again, and how many times. ``shard`` and ``merge`` split one launch over
several machines and put its jobs back together; ``payload`` is the snapshot of
this runner a host is sent. ``boat_units`` sends that snapshot to a Boat VM per
unit, for a benchmark whose units boot a VM of their own, and merges what
comes back.
"""
