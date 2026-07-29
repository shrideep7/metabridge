"""0003 — Phase 3 usage metering & reporting.

Creates the immutable usage-event log, adjustment events, materialized
aggregates, and signed usage statements, and seeds the meter catalogue
(docs/commercialization/03-domain-model.md §7). Meters link to the Phase-2
entitlement quota codes so overage detection can compare usage to limits.
"""
from __future__ import annotations

from ... import schema

_TABLES = [
    schema.meter_definitions,
    schema.usage_events,
    schema.adjustment_events,
    schema.usage_aggregates,
    schema.usage_statements,
]

# (meter_code, unit, aggregation, dedup_dimension, billable, entitlement_code)
_SEED_METERS = [
    ("OBJECTS_ASSESSED", "object", "DISTINCT_COUNT", "project", True,
     "quota.objects_assessed"),
    ("OBJECTS_CONVERTED", "object", "SUM", None, True, None),
    ("ASSESSMENTS", "assessment", "SUM", None, True, "quota.assessments"),
    ("PROGRAMS", "program", "MAX", None, False, "limit.active_programs"),
    ("TWIN_NODES", "node", "LAST", None, False, None),
    ("TWIN_EDGES", "edge", "LAST", None, False, None),
    ("JOBS_RUN", "job", "SUM", None, False, None),
    ("AI_TOKENS_IN", "token", "SUM", None, True, None),
    ("AI_TOKENS_OUT", "token", "SUM", None, True, None),
    ("AI_CREDITS", "credit", "SUM", None, True, "quota.ai_credits"),
    ("API_CALLS", "call", "SUM", None, True, "quota.api_calls"),
    ("REPORT_EXPORTS", "export", "SUM", None, True, "quota.report_exports"),
    ("ACTIVE_USERS", "user", "MAX", None, False, "limit.users"),
    ("STORAGE_GB", "gb", "LAST", None, False, "limit.storage_gb"),
    ("INSTANCE_MONTHS", "instance-month", "SUM", None, True, None),
]


def up(conn) -> None:
    schema.metadata.create_all(conn, tables=_TABLES)
    existing = {r[0] for r in conn.execute(
        schema.meter_definitions.select().with_only_columns(
            schema.meter_definitions.c.meter_code))}
    for code, unit, agg, dedup, billable, ent in _SEED_METERS:
        if code in existing:
            continue
        conn.execute(schema.meter_definitions.insert().values(
            meter_code=code, unit=unit, aggregation=agg,
            dedup_dimension=dedup, billable=billable, entitlement_code=ent,
            description=f"{code} usage meter", created_by="migration"))


def down(conn) -> None:
    schema.metadata.drop_all(conn, tables=list(reversed(_TABLES)))
