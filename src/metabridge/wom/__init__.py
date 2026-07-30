"""Warehouse Object Model (WOM) — the schema-level sibling of the IR and COR.

The Pipeline IR normalizes mappings; COR normalizes orchestration. WOM
normalizes everything else a warehouse account contains — views, procedures,
functions, sequences, tasks, streams, pipes, stages, policies, tags, grants,
constraints, comments — into one canonical shape, so feasibility analysis and
target generation are written once against the model instead of pairwise per
platform. N parsers in, N generators out; never N x N converters.
"""
