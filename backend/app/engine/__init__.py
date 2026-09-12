"""The co-scientist engine: pure math, persistence, prompts and the round orchestrator.

`core` is deliberately free of I/O so the loop's decisions (Elo, pairing, collapse
detection, budget estimates) can be tested and replayed without a database or a model.
"""
