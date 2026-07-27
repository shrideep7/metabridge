"""0007 — Phase 8 customer-success analytics.

Creates the health-score, adoption-signal, lifecycle-stage, and risk-flag
tables (docs/commercialization/03-domain-model.md §12). All derive from signals
instances already report — no new data-plane behaviour. Nothing is seeded.
"""
from __future__ import annotations

from ... import schema

_TABLES = [
    schema.health_score_snapshots,
    schema.adoption_signals,
    schema.lifecycle_stage_transitions,
    schema.risk_flags,
]


def up(conn) -> None:
    schema.metadata.create_all(conn, tables=_TABLES)


def down(conn) -> None:
    schema.metadata.drop_all(conn, tables=list(reversed(_TABLES)))
