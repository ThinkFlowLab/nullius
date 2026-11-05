"""Judgement: structured, confidence-scored answers about a recorded trial
from a judge model (TypeSafe.ai System One), shown beside the deterministic
evidence and never in its place.

The contract: the question
vocabulary (``questions.py``, one id per question, versioned as a set), the
state each question reads (``state.py``, built from the redacted views only,
truncated head-and-tail with a marker), the record shape (``client.py``),
and where a judgement lives - the workspace store, keyed by trial and
question version (``store.py``), never under the job's own tree. A surface
asks through ``batch.py``: the questions grouped by state, one call per
state, the records merged and stored - so no page groups or merges on its
own.
"""
