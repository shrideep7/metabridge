"""0002 — Phase 2 commercial core.

Adds customer accounts, contracts, subscriptions (+items, +transitions),
licensing (licenses, license_files), entitlements (+overrides), and the quota
ledger (consumption, reservations, decision log). Purely additive; the Phase-1
foundation and the product data plane are untouched.
"""
from __future__ import annotations

from ... import schema

_TABLES = [
    schema.customer_accounts, schema.contracts,
    schema.subscriptions, schema.subscription_items,
    schema.subscription_transitions,
    schema.licenses, schema.license_files,
    schema.entitlements, schema.entitlement_overrides,
    schema.quota_consumption, schema.quota_reservations,
    schema.entitlement_decision_log,
]


def up(conn) -> None:
    schema.metadata.create_all(conn, tables=_TABLES)


def down(conn) -> None:
    schema.metadata.drop_all(conn, tables=list(reversed(_TABLES)))
