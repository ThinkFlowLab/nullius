"""A run, from its units to its job, on one machine or several.

``run`` sets each unit up, produces and scores it. ``job`` writes the result as
a job in Harbor's shape. ``pace`` slows the start of units when the provider
says the run goes too fast. ``shard`` and ``merge`` split one launch over
several machines and put its jobs back together; ``payload`` is the snapshot of
this runner a host is sent.
"""
