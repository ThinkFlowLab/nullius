"""A run's model calls: relayed, recorded, capped.

``relay`` is the model endpoint every agent, driver and capability of a run is
given: it holds the key and records each call. ``ledger`` is the run's spending
cap, the one file every relay of a job books its calls in.
"""
