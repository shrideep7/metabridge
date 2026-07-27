"""0001 — Phase 1 foundation schema.

Creates the tenancy/identity, audit, feature-flag and product-catalog tables
and seeds the starting vendor regions (the recommended day-1 cells from
docs/global-operations.md).

Rollback (``down``) drops exactly these tables — it cannot touch anything
else, and it never runs unless explicitly invoked via
``runner.rollback(engine, to=0)``.
"""
from __future__ import annotations

from ... import schema

_TABLES = [
    # dependency order (parents first)
    schema.tenants, schema.users, schema.regions,
    schema.organizations, schema.memberships,
    schema.business_units, schema.workspaces, schema.environments,
    schema.audit_events, schema.audit_chain_heads, schema.feature_flags,
    schema.products, schema.features, schema.plans,
    schema.plan_versions, schema.plan_version_features,
]

_SEED_REGIONS = [
    {"code": "us-east-1", "cloud": "aws", "status": "ACTIVE"},
    {"code": "eu-central-1", "cloud": "aws", "status": "ACTIVE"},
    {"code": "ap-southeast-1", "cloud": "aws", "status": "ACTIVE"},
]


def up(conn) -> None:
    schema.metadata.create_all(conn, tables=_TABLES)
    existing = {r[0] for r in conn.execute(
        schema.regions.select().with_only_columns(schema.regions.c.code))}
    for region in _SEED_REGIONS:
        if region["code"] not in existing:
            conn.execute(schema.regions.insert().values(**region))


def down(conn) -> None:
    schema.metadata.drop_all(conn, tables=list(reversed(_TABLES)))
