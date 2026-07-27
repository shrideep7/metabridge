"""Control-plane schema — single source of truth for every table.

Conventions (docs/commercialization/03-domain-model.md §1):
- IDs are UUID4 hex strings, generated server-side — never client-supplied.
- Every table holding customer data carries ``tenant_id`` plus
  ``created_by`` / ``created_at`` / ``updated_at``.
- The product catalog is vendor-owned (no ``tenant_id``): it is not customer
  data. Vendor-scoped audit events use the sentinel tenant ``'*'``.
- Feature flags use tenant sentinel ``'*'`` for global defaults so the
  (key, tenant_id) uniqueness holds identically on SQLite and PostgreSQL.

Migrations (metabridge_control.migrations) own DDL application; this module
only *defines* structure.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, ForeignKey, Integer, MetaData, Numeric,
    String, Table, Text, UniqueConstraint, Index,
)
from sqlalchemy.types import TypeDecorator

metadata = MetaData()

GLOBAL_TENANT = "*"  # vendor scope sentinel (flags defaults, vendor audit chain)

MONEY_SCALE = Decimal("0.0001")   # numeric(19,4): four decimal places
ZERO_MONEY = Decimal("0.0000")


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, portable


class Money(TypeDecorator):
    """Exact fixed-point money — ``numeric(19,4)`` + a separate ``currency``
    column, per the domain-model §1 convention. **Never a float.**

    On PostgreSQL this is a native ``NUMERIC(19,4)`` (exact, as mandated). On
    SQLite — dev/test only — SQLAlchemy's ``Numeric`` round-trips through
    ``float``, which would silently reintroduce rounding error into a rating
    engine that must be replayable (same inputs → same invoice). So on SQLite
    we store the canonical 4dp decimal *string* and parse it back to
    ``Decimal``. Either way the Python side only ever sees ``Decimal``.

    Because money is TEXT on SQLite, services sum monetary columns in Python
    (over ``Decimal``), never via SQL ``SUM`` — exact and backend-uniform.
    """
    impl = Numeric(19, 4)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(40))
        return dialect.type_descriptor(Numeric(19, 4))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        d = value if isinstance(value, Decimal) else Decimal(str(value))
        d = d.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        return str(d) if dialect.name == "sqlite" else d

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(str(value)).quantize(MONEY_SCALE)


def _std_cols():
    return (
        Column("created_by", String(64), nullable=False, default="system"),
        Column("created_at", DateTime, nullable=False, default=utcnow),
        Column("updated_at", DateTime, nullable=False, default=utcnow,
               onupdate=utcnow),
    )


# --------------------------------------------------------------------------
# Tenancy & identity
# --------------------------------------------------------------------------
tenants = Table(
    "tenants", metadata,
    Column("id", String(36), primary_key=True),
    Column("slug", String(64), nullable=False, unique=True),
    Column("legal_name", String(255), nullable=False),
    Column("status", String(16), nullable=False, default="ACTIVE"),
    # PROSPECT | ACTIVE | SUSPENDED | OFFBOARDED
    Column("home_region", String(32), nullable=True),
    *_std_cols(),
)

organizations = Table(
    "organizations", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("slug", String(64), nullable=False),
    Column("name", String(255), nullable=False),
    Column("country_code", String(2), nullable=True),
    *_std_cols(),
    UniqueConstraint("tenant_id", "slug", name="uq_org_tenant_slug"),
    Index("ix_org_tenant", "tenant_id"),
)

business_units = Table(
    "business_units", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("organization_id", String(36), ForeignKey("organizations.id"),
           nullable=False),
    Column("name", String(255), nullable=False),
    Column("cost_center_code", String(64), nullable=True),
    *_std_cols(),
    UniqueConstraint("organization_id", "name", name="uq_bu_org_name"),
    Index("ix_bu_tenant", "tenant_id"),
)

workspaces = Table(
    "workspaces", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("business_unit_id", String(36), ForeignKey("business_units.id"),
           nullable=False),
    Column("slug", String(64), nullable=False),
    Column("name", String(255), nullable=False),
    Column("purpose", String(16), nullable=False, default="PRODUCTION"),
    # PRODUCTION | PROJECT | SANDBOX
    *_std_cols(),
    UniqueConstraint("business_unit_id", "slug", name="uq_ws_bu_slug"),
    Index("ix_ws_tenant", "tenant_id"),
)

environments = Table(
    "environments", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("workspace_id", String(36), ForeignKey("workspaces.id"),
           nullable=False),
    Column("name", String(64), nullable=False),
    Column("kind", String(16), nullable=False, default="PROD"),
    # PROD | NONPROD | DR | ENCLAVE
    Column("region", String(32), nullable=True),
    *_std_cols(),
    UniqueConstraint("workspace_id", "name", name="uq_env_ws_name"),
    Index("ix_env_tenant", "tenant_id"),
)

users = Table(
    "users", metadata,
    Column("id", String(36), primary_key=True),
    Column("email", String(255), nullable=False, unique=True),
    Column("display_name", String(255), nullable=False, default=""),
    Column("status", String(16), nullable=False, default="ACTIVE"),
    # ACTIVE | DISABLED
    Column("auth_provider", String(16), nullable=False, default="PASSWORD"),
    *_std_cols(),
)

memberships = Table(
    "memberships", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("role_code", String(32), nullable=False),
    Column("state", String(16), nullable=False, default="ACTIVE"),
    # INVITED | ACTIVE | REVOKED
    *_std_cols(),
    UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
    Index("ix_membership_tenant", "tenant_id"),
    Index("ix_membership_user", "user_id"),
)

regions = Table(
    "regions", metadata,
    Column("code", String(32), primary_key=True),
    Column("cloud", String(16), nullable=False, default="aws"),
    Column("status", String(16), nullable=False, default="ACTIVE"),
)

# --------------------------------------------------------------------------
# Audit (tamper-evident, per-tenant hash chain — DB-backed adaptation of the
# verified pattern in src/metabridge/agents/audit.py)
# --------------------------------------------------------------------------
audit_events = Table(
    "audit_events", metadata,
    Column("event_id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),  # '*' = vendor scope
    Column("seq", Integer, nullable=False),
    Column("actor_type", String(16), nullable=False),  # USER | STAFF | SYSTEM
    Column("actor_id", String(255), nullable=False),
    Column("action", String(64), nullable=False),
    Column("resource_type", String(64), nullable=False),
    Column("resource_id", String(255), nullable=False),
    Column("before_state", JSON, nullable=True),
    Column("after_state", JSON, nullable=True),
    Column("ip_address", String(64), nullable=True),
    Column("user_agent", String(255), nullable=True),
    Column("correlation_id", String(64), nullable=True),
    Column("occurred_at", DateTime, nullable=False, default=utcnow),
    Column("result", String(16), nullable=False, default="SUCCESS"),
    Column("reason", Text, nullable=True),
    Column("prev_hash", String(64), nullable=False),
    Column("entry_hash", String(64), nullable=False),
    UniqueConstraint("tenant_id", "seq", name="uq_audit_tenant_seq"),
    Index("ix_audit_tenant", "tenant_id"),
)

# Per-tenant tamper-evident head anchor. Serializes seq allocation (the head
# row is locked/updated on every append) and lets verify_chain detect tail
# truncation or full deletion of a tenant's events (which a purely
# row-recomputing check cannot see).
audit_chain_heads = Table(
    "audit_chain_heads", metadata,
    Column("tenant_id", String(36), primary_key=True),
    Column("max_seq", Integer, nullable=False, default=0),
    Column("head_hash", String(64), nullable=False),
    Column("updated_at", DateTime, nullable=False, default=utcnow,
           onupdate=utcnow),
)

# --------------------------------------------------------------------------
# Feature flags (tenant-scoped, fail-closed — semantics mirror the verified
# src/metabridge/platform/flags.py: strict booleans, SHA-256 rollout buckets)
# --------------------------------------------------------------------------
feature_flags = Table(
    "feature_flags", metadata,
    Column("id", String(36), primary_key=True),
    Column("key", String(128), nullable=False),
    Column("tenant_id", String(36), nullable=False, default=GLOBAL_TENANT),
    Column("enabled", Boolean, nullable=False, default=False),
    Column("rollout_pct", Integer, nullable=False, default=100),
    Column("allowed_roles", JSON, nullable=True),  # list[str] | None = any
    Column("description", Text, nullable=True),
    *_std_cols(),
    UniqueConstraint("key", "tenant_id", name="uq_flag_key_tenant"),
    Index("ix_flag_tenant", "tenant_id"),
)

# --------------------------------------------------------------------------
# Product catalog (vendor-owned; versioned plans)
# --------------------------------------------------------------------------
products = Table(
    "products", metadata,
    Column("id", String(36), primary_key=True),
    Column("code", String(64), nullable=False, unique=True),
    Column("name", String(255), nullable=False),
    Column("description", Text, nullable=True),
    Column("product_type", String(16), nullable=False, default="PLATFORM"),
    # PLATFORM | ADDON | PACK
    Column("active", Boolean, nullable=False, default=True),
    *_std_cols(),
)

features = Table(
    "features", metadata,
    Column("id", String(36), primary_key=True),
    Column("code", String(64), nullable=False, unique=True),
    Column("name", String(255), nullable=False),
    Column("description", Text, nullable=True),
    Column("value_kind", String(16), nullable=False),  # BOOLEAN | LIMIT
    Column("unit", String(32), nullable=True),
    *_std_cols(),
)

plans = Table(
    "plans", metadata,
    Column("id", String(36), primary_key=True),
    Column("product_id", String(36), ForeignKey("products.id"), nullable=False),
    Column("code", String(64), nullable=False),
    Column("name", String(255), nullable=False),
    Column("description", Text, nullable=True),
    *_std_cols(),
    UniqueConstraint("product_id", "code", name="uq_plan_product_code"),
)

plan_versions = Table(
    "plan_versions", metadata,
    Column("id", String(36), primary_key=True),
    Column("plan_id", String(36), ForeignKey("plans.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", String(16), nullable=False, default="DRAFT"),
    # DRAFT | PUBLISHED | RETIRED
    Column("notes", Text, nullable=True),
    Column("published_at", DateTime, nullable=True),
    Column("retired_at", DateTime, nullable=True),
    *_std_cols(),
    UniqueConstraint("plan_id", "version", name="uq_planver_plan_version"),
)

plan_version_features = Table(
    "plan_version_features", metadata,
    Column("id", String(36), primary_key=True),
    Column("plan_version_id", String(36), ForeignKey("plan_versions.id"),
           nullable=False),
    Column("feature_code", String(64), ForeignKey("features.code"),
           nullable=False),
    Column("bool_value", Boolean, nullable=True),
    Column("limit_value", Integer, nullable=True),
    Column("unlimited", Boolean, nullable=False, default=False),
    UniqueConstraint("plan_version_id", "feature_code",
                     name="uq_pvf_version_feature"),
)

# --------------------------------------------------------------------------
# Commercial core (Phase 2) — accounts, contracts, subscriptions, licensing,
# entitlements, quota reservations. No monetary amounts here: pricing/rating
# is a later phase and is never hardcoded.
# --------------------------------------------------------------------------
customer_accounts = Table(
    "customer_accounts", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("name", String(255), nullable=False),
    Column("billing_email", String(255), nullable=True),
    Column("status", String(16), nullable=False, default="ACTIVE"),
    Column("currency", String(3), nullable=True),   # ISO code only, no amounts
    *_std_cols(),
    Index("ix_acct_tenant", "tenant_id"),
)

contracts = Table(
    "contracts", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("customer_account_id", String(36),
           ForeignKey("customer_accounts.id"), nullable=False),
    Column("state", String(16), nullable=False, default="DRAFT"),
    # DRAFT | EXECUTED | EXPIRED | TERMINATED
    Column("purchase_order_ref", String(128), nullable=True),
    Column("payment_terms_days", Integer, nullable=True),
    Column("governing_law", String(64), nullable=True),
    Column("document_refs", JSON, nullable=True),
    Column("executed_at", DateTime, nullable=True),
    *_std_cols(),
    Index("ix_contract_tenant", "tenant_id"),
)

subscriptions = Table(
    "subscriptions", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("customer_account_id", String(36),
           ForeignKey("customer_accounts.id"), nullable=False),
    Column("contract_id", String(36), ForeignKey("contracts.id"),
           nullable=True),
    Column("plan_version_id", String(36), ForeignKey("plan_versions.id"),
           nullable=False),
    Column("state", String(20), nullable=False, default="DRAFT"),
    Column("billing_period", String(12), nullable=False, default="ANNUAL"),
    Column("term_start", DateTime, nullable=True),
    Column("term_end", DateTime, nullable=True),
    Column("auto_renew", Boolean, nullable=False, default=True),
    Column("is_trial", Boolean, nullable=False, default=False),
    Column("trial_end", DateTime, nullable=True),
    Column("grace_until", DateTime, nullable=True),
    Column("cancel_effective_at", DateTime, nullable=True),
    Column("version", Integer, nullable=False, default=1),  # optimistic lock
    *_std_cols(),
    Index("ix_sub_tenant", "tenant_id"),
    Index("ix_sub_state", "state"),
)

subscription_items = Table(
    "subscription_items", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("subscription_id", String(36), ForeignKey("subscriptions.id"),
           nullable=False),
    Column("kind", String(12), nullable=False),   # SEAT | ADDON | METER
    Column("ref_code", String(64), nullable=False),  # entitlement code / addon
    Column("quantity", Integer, nullable=False, default=0),
    Column("committed_quantity", Integer, nullable=True),
    *_std_cols(),
    UniqueConstraint("subscription_id", "ref_code",
                     name="uq_subitem_sub_ref"),
    Index("ix_subitem_tenant", "tenant_id"),
)

subscription_transitions = Table(
    "subscription_transitions", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("subscription_id", String(36), ForeignKey("subscriptions.id"),
           nullable=False),
    Column("from_state", String(20), nullable=True),
    Column("to_state", String(20), nullable=False),
    Column("at", DateTime, nullable=False, default=utcnow),
    Column("actor", String(64), nullable=False),
    Column("reason", Text, nullable=True),
    Index("ix_subtrans_sub", "subscription_id"),
)

licenses = Table(
    "licenses", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("subscription_id", String(36), ForeignKey("subscriptions.id"),
           nullable=False),
    Column("instance_id", String(64), nullable=True),
    Column("state", String(12), nullable=False, default="ISSUED"),
    # ISSUED | ACTIVE | REVOKED | EXPIRED
    Column("not_before", DateTime, nullable=True),
    Column("not_after", DateTime, nullable=True),
    Column("offline_grace_days", Integer, nullable=False, default=7),
    Column("entitlement_snapshot", JSON, nullable=True),
    *_std_cols(),
    Index("ix_lic_tenant", "tenant_id"),
    Index("ix_lic_sub", "subscription_id"),
)

license_files = Table(
    "license_files", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("license_id", String(36), ForeignKey("licenses.id"),
           nullable=False),
    Column("serial", Integer, nullable=False),
    Column("canonical_payload", JSON, nullable=False),
    Column("signature", Text, nullable=False),
    Column("signer_key_id", String(64), nullable=False),
    Column("supersedes_serial", Integer, nullable=True),
    Column("issued_at", DateTime, nullable=False, default=utcnow),
    UniqueConstraint("license_id", "serial", name="uq_licfile_serial"),
)

entitlements = Table(
    "entitlements", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("subscription_id", String(36), ForeignKey("subscriptions.id"),
           nullable=False),
    Column("code", String(64), nullable=False),
    Column("value_kind", String(16), nullable=False),
    # BOOLEAN | NUMERIC_LIMIT | METERED_QUOTA | TIER | DATE_WINDOW
    Column("value", JSON, nullable=False),
    Column("period", String(12), nullable=True),   # for METERED_QUOTA
    Column("source", String(12), nullable=False, default="PLAN"),
    # PLAN | ADDON | OVERRIDE
    Column("expires_at", DateTime, nullable=True),  # set for time-boxed OVERRIDE
    *_std_cols(),
    UniqueConstraint("subscription_id", "code", name="uq_ent_sub_code"),
    Index("ix_ent_tenant", "tenant_id"),
)

entitlement_overrides = Table(
    "entitlement_overrides", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("subscription_id", String(36), ForeignKey("subscriptions.id"),
           nullable=False),
    Column("code", String(64), nullable=False),
    Column("value_kind", String(16), nullable=False),
    Column("value", JSON, nullable=False),
    Column("period", String(12), nullable=True),
    Column("approved_by", String(64), nullable=False),
    Column("reason", Text, nullable=False),
    Column("expires_at", DateTime, nullable=True),
    Column("active", Boolean, nullable=False, default=True),
    *_std_cols(),
    Index("ix_entover_sub", "subscription_id"),
)

quota_consumption = Table(
    "quota_consumption", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("subscription_id", String(36), ForeignKey("subscriptions.id"),
           nullable=False),
    Column("meter_code", String(64), nullable=False),
    Column("period_key", String(16), nullable=False),   # e.g. 2026 / 2026-07
    Column("consumed", Integer, nullable=False, default=0),
    Column("updated_at", DateTime, nullable=False, default=utcnow,
           onupdate=utcnow),
    UniqueConstraint("subscription_id", "meter_code", "period_key",
                     name="uq_qc_sub_meter_period"),
    Index("ix_qc_tenant", "tenant_id"),
)

quota_reservations = Table(
    "quota_reservations", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("subscription_id", String(36), ForeignKey("subscriptions.id"),
           nullable=False),
    Column("meter_code", String(64), nullable=False),
    Column("period_key", String(16), nullable=False),
    Column("quantity_reserved", Integer, nullable=False),
    Column("quantity_committed", Integer, nullable=True),
    Column("state", String(12), nullable=False, default="RESERVED"),
    # RESERVED | COMMITTED | RELEASED | EXPIRED
    Column("job_ref", String(128), nullable=True),
    Column("idempotency_key", String(128), nullable=False),
    Column("expires_at", DateTime, nullable=True),
    *_std_cols(),
    UniqueConstraint("tenant_id", "idempotency_key",
                     name="uq_resv_tenant_idem"),
    Index("ix_resv_sub_meter", "subscription_id", "meter_code"),
    Index("ix_resv_state", "state"),
)

entitlement_decision_log = Table(
    "entitlement_decision_log", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("subscription_id", String(36), nullable=True),
    Column("principal", String(128), nullable=True),
    Column("code", String(64), nullable=False),
    Column("mode", String(12), nullable=False),
    Column("quantity", Integer, nullable=False, default=1),
    Column("decision", String(12), nullable=False),
    Column("reason_code", String(32), nullable=False),
    Column("at", DateTime, nullable=False, default=utcnow),
    Index("ix_declog_tenant", "tenant_id"),
)


# --------------------------------------------------------------------------
# Phase 3 — Usage metering & reporting (finance-grade)
# docs/commercialization/03-domain-model.md §7
# --------------------------------------------------------------------------
meter_definitions = Table(
    "meter_definitions", metadata,
    Column("meter_code", String(64), primary_key=True),
    Column("unit", String(32), nullable=False),
    Column("aggregation", String(16), nullable=False, default="SUM"),
    # SUM | DISTINCT_COUNT | MAX | LAST
    Column("dedup_dimension", String(64), nullable=True),  # for DISTINCT_COUNT
    Column("billable", Boolean, nullable=False, default=True),
    Column("entitlement_code", String(64), nullable=True),  # links to a quota.*
    Column("description", Text, nullable=True),
    *_std_cols(),
)

# Immutable atomic usage fact. Append-only — never updated or deleted;
# corrections go through adjustment_events.
usage_events = Table(
    "usage_events", metadata,
    Column("id", String(36), primary_key=True),          # event_id
    Column("tenant_id", String(36), nullable=False),
    Column("subscription_id", String(36), nullable=True),
    Column("meter_code", String(64), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("unit", String(32), nullable=True),
    Column("occurred_at", DateTime, nullable=False),
    Column("received_at", DateTime, nullable=False, default=utcnow),
    Column("instance_id", String(36), nullable=True),
    Column("workspace_id", String(36), nullable=True),
    Column("environment_id", String(36), nullable=True),
    Column("source", String(20), nullable=False, default="CONTROL_PLANE"),
    # INSTANCE_BATCH | SIGNED_STATEMENT | CONTROL_PLANE
    Column("idempotency_key", String(128), nullable=False),
    Column("dimensions", JSON, nullable=True),   # project, source_format, job_id
    Column("schema_version", Integer, nullable=False, default=1),
    Column("created_by", String(64), nullable=False, default="system"),
    UniqueConstraint("tenant_id", "idempotency_key",
                     name="uq_usage_tenant_idem"),
    Index("ix_usage_tenant_meter_time", "tenant_id", "meter_code",
          "occurred_at"),
    Index("ix_usage_sub", "subscription_id"),
)

# The only correction mechanism — a signed, compensating delta. Original rows
# are never mutated; aggregates recompute over events + adjustments.
adjustment_events = Table(
    "adjustment_events", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("original_event_id", String(36),
           ForeignKey("usage_events.id"), nullable=False),
    Column("meter_code", String(64), nullable=False),
    Column("quantity_delta", Integer, nullable=False),   # signed
    Column("reason_code", String(64), nullable=False),
    Column("approved_by", String(64), nullable=False),
    Column("evidence_ref", String(255), nullable=True),
    Column("occurred_at", DateTime, nullable=False, default=utcnow),
    Column("created_at", DateTime, nullable=False, default=utcnow),
    Index("ix_adj_tenant", "tenant_id"),
    Index("ix_adj_original", "original_event_id"),
)

# Materialized rollups — always recomputable from source events.
usage_aggregates = Table(
    "usage_aggregates", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("meter_code", String(64), nullable=False),
    Column("level", String(16), nullable=False),   # DAILY | BILLING_PERIOD
    Column("scope", String(16), nullable=False, default="TENANT"),
    # TENANT | WORKSPACE | ENVIRONMENT | INSTANCE
    Column("scope_id", String(36), nullable=False, default="*"),
    Column("period_start", DateTime, nullable=False),
    Column("period_end", DateTime, nullable=False),
    Column("quantity", Integer, nullable=False, default=0),
    Column("distinct_basis", JSON, nullable=True),
    Column("computed_at", DateTime, nullable=False, default=utcnow),
    Column("is_final", Boolean, nullable=False, default=False),
    UniqueConstraint("tenant_id", "meter_code", "level", "scope", "scope_id",
                     "period_start", name="uq_agg_scope_period"),
    Index("ix_agg_tenant_period", "tenant_id", "period_start"),
)

# Signed usage document for the air-gapped path (instance -> control plane).
usage_statements = Table(
    "usage_statements", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("instance_id", String(36), nullable=True),
    Column("serial", String(64), nullable=False),
    Column("period_start", DateTime, nullable=True),
    Column("period_end", DateTime, nullable=True),
    Column("canonical_payload", JSON, nullable=False),
    Column("signature", Text, nullable=True),
    Column("signer_key_id", String(64), nullable=True),
    Column("state", String(12), nullable=False, default="RECEIVED"),
    # RECEIVED | VERIFIED | INGESTED | REJECTED
    Column("event_count", Integer, nullable=False, default=0),
    Column("verified_at", DateTime, nullable=True),
    Column("created_at", DateTime, nullable=False, default=utcnow),
    # identity includes instance_id: two instances may legitimately use the
    # same serial namespace, so they must not alias to one statement.
    UniqueConstraint("tenant_id", "instance_id", "serial",
                     name="uq_stmt_tenant_instance_serial"),
    Index("ix_stmt_tenant", "tenant_id"),
)


# --------------------------------------------------------------------------
# Phase 4 — Pricing, rating & billing integration
# docs/commercialization/03-domain-model.md §8. Money is numeric(19,4) via the
# Money type (Decimal, never float); every amount carries an ISO-4217 currency.
# --------------------------------------------------------------------------
price_books = Table(
    "price_books", metadata,
    Column("id", String(36), primary_key=True),
    Column("code", String(64), nullable=False),
    Column("name", String(255), nullable=False),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("region", String(32), nullable=True),          # null = global
    Column("version", Integer, nullable=False, default=1),
    Column("status", String(12), nullable=False, default="DRAFT"),
    # DRAFT | ACTIVE | RETIRED
    *_std_cols(),
    UniqueConstraint("code", "version", name="uq_pricebook_code_version"),
)

price_book_entries = Table(
    "price_book_entries", metadata,
    Column("id", String(36), primary_key=True),
    Column("price_book_id", String(36), ForeignKey("price_books.id"),
           nullable=False),
    Column("meter_code", String(64), nullable=False),
    Column("pricing_model", String(12), nullable=False, default="PER_UNIT"),
    # FLAT | PER_UNIT | TIERED | VOLUME
    Column("unit_amount", Money, nullable=False, default=ZERO_MONEY),
    Column("tiers", JSON, nullable=True),   # [{"up_to": n|null, "amount": "x"}]
    Column("floor_price", Money, nullable=True),   # per-unit floor
    Column("min_commit", Money, nullable=True),
    Column("included_quantity", Integer, nullable=False, default=0),
    *_std_cols(),
    UniqueConstraint("price_book_id", "meter_code",
                     name="uq_pbe_book_meter"),
)

discount_rules = Table(
    "discount_rules", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=True),   # null = global rule
    Column("code", String(64), nullable=False),
    Column("name", String(255), nullable=False),
    Column("percent", Integer, nullable=False, default=0),   # 0..100
    Column("priority", Integer, nullable=False, default=100),
    Column("exclusive", Boolean, nullable=False, default=False),
    Column("max_percent", Integer, nullable=False, default=100),
    Column("applies_to", String(64), nullable=True),   # meter_code | null=all
    Column("active", Boolean, nullable=False, default=True),
    Column("expires_at", DateTime, nullable=True),
    *_std_cols(),
    Index("ix_discount_tenant", "tenant_id"),
)

price_override_approvals = Table(
    "price_override_approvals", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("subscription_id", String(36), nullable=False),
    Column("meter_code", String(64), nullable=False),
    Column("floor_price", Money, nullable=False),
    Column("proposed_price", Money, nullable=False),
    Column("justification", Text, nullable=False),
    Column("requested_by", String(64), nullable=False),
    Column("approved_by", String(64), nullable=True),
    Column("state", String(12), nullable=False, default="REQUESTED"),
    # REQUESTED | APPROVED | REJECTED | CONSUMED
    Column("decided_at", DateTime, nullable=True),
    *_std_cols(),
    Index("ix_override_sub", "subscription_id"),
)

rating_runs = Table(
    "rating_runs", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("subscription_id", String(36), nullable=False),
    Column("price_book_id", String(36), nullable=False),
    Column("period_start", DateTime, nullable=False),
    Column("period_end", DateTime, nullable=False),
    Column("state", String(12), nullable=False, default="RUNNING"),
    # RUNNING | COMPLETE | FAILED | SUPERSEDED
    Column("inputs_digest", String(64), nullable=True),
    Column("total", Money, nullable=False, default=ZERO_MONEY),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("reason", Text, nullable=True),
    *_std_cols(),
    Index("ix_rating_tenant", "tenant_id"),
    Index("ix_rating_sub", "subscription_id"),
)

rated_lines = Table(
    "rated_lines", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("rating_run_id", String(36), ForeignKey("rating_runs.id"),
           nullable=False),
    Column("meter_code", String(64), nullable=False),
    Column("quantity", Integer, nullable=False, default=0),
    Column("list_amount", Money, nullable=False, default=ZERO_MONEY),
    Column("final_amount", Money, nullable=False, default=ZERO_MONEY),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("waterfall", JSON, nullable=True),   # replayable step trace
    Index("ix_ratedline_run", "rating_run_id"),
)

billing_accounts = Table(
    "billing_accounts", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("provider", String(20), nullable=False, default="MANUAL"),
    # MANUAL | STRIPE | RAZORPAY | AWS_MARKETPLACE
    Column("external_customer_ref", String(128), nullable=True),
    Column("payment_terms_days", Integer, nullable=False, default=30),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("tax_ids", JSON, nullable=True),
    *_std_cols(),
    Index("ix_billacct_tenant", "tenant_id"),
)

invoices = Table(
    "invoices", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("billing_account_id", String(36), nullable=True),
    Column("rating_run_id", String(36), nullable=True),
    Column("number", String(48), nullable=False),
    Column("state", String(16), nullable=False, default="DRAFT"),
    # DRAFT | ISSUED | PARTIALLY_PAID | PAID | OVERDUE | VOID
    Column("issued_at", DateTime, nullable=True),
    Column("due_at", DateTime, nullable=True),
    Column("subtotal", Money, nullable=False, default=ZERO_MONEY),
    Column("tax", Money, nullable=False, default=ZERO_MONEY),
    Column("total", Money, nullable=False, default=ZERO_MONEY),
    Column("amount_paid", Money, nullable=False, default=ZERO_MONEY),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("external_ref", String(128), nullable=True),
    *_std_cols(),
    UniqueConstraint("tenant_id", "number", name="uq_invoice_tenant_number"),
    Index("ix_invoice_tenant", "tenant_id"),
)

invoice_lines = Table(
    "invoice_lines", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("invoice_id", String(36), ForeignKey("invoices.id"),
           nullable=False),
    Column("rated_line_id", String(36), nullable=True),
    Column("description", String(255), nullable=False),
    Column("amount", Money, nullable=False, default=ZERO_MONEY),
    Column("currency", String(3), nullable=False, default="USD"),
    Index("ix_invline_invoice", "invoice_id"),
)

payments = Table(
    "payments", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("invoice_id", String(36), ForeignKey("invoices.id"), nullable=False),
    Column("provider", String(20), nullable=False, default="MANUAL"),
    Column("provider_ref", String(128), nullable=True),
    Column("amount", Money, nullable=False),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("method", String(32), nullable=True),
    Column("received_at", DateTime, nullable=False, default=utcnow),
    Column("idempotency_key", String(128), nullable=False),
    *_std_cols(),
    UniqueConstraint("tenant_id", "idempotency_key",
                     name="uq_payment_tenant_idem"),
    Index("ix_payment_invoice", "invoice_id"),
)

credit_notes = Table(
    "credit_notes", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("invoice_id", String(36), ForeignKey("invoices.id"), nullable=False),
    Column("amount", Money, nullable=False),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("reason", Text, nullable=False),
    *_std_cols(),
    Index("ix_creditnote_invoice", "invoice_id"),
)

dunning_attempts = Table(
    "dunning_attempts", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("invoice_id", String(36), ForeignKey("invoices.id"), nullable=False),
    Column("step", Integer, nullable=False, default=1),
    Column("channel", String(16), nullable=False, default="EMAIL"),
    Column("outcome", String(32), nullable=True),
    Column("at", DateTime, nullable=False, default=utcnow),
    Index("ix_dunning_invoice", "invoice_id"),
)

# Idempotent, signature-verified inbound provider webhooks.
billing_webhook_events = Table(
    "billing_webhook_events", metadata,
    Column("id", String(36), primary_key=True),
    Column("provider", String(20), nullable=False),
    Column("external_id", String(128), nullable=False),
    Column("event_type", String(64), nullable=True),
    Column("payload", JSON, nullable=True),
    Column("signature_verified", Boolean, nullable=False, default=False),
    Column("processed", Boolean, nullable=False, default=False),
    Column("error", Text, nullable=True),
    Column("received_at", DateTime, nullable=False, default=utcnow),
    UniqueConstraint("provider", "external_id",
                     name="uq_webhook_provider_extid"),
)


# --------------------------------------------------------------------------
# Phase 5 — Instance enrollment (the data-plane <-> control-plane trust anchor)
# docs/commercialization/03-domain-model.md §3.1. An Instance is a data-plane
# deployment bound to a tenant (and optionally a workspace/environment). Its
# credential is how the tenant is *derived* on every instance-authenticated
# request — never taken from the request body (the tenancy mandate, §1).
# Secrets (enrollment tokens, instance credentials) are stored only as SHA-256
# hashes; the plaintext is shown once at creation.
# --------------------------------------------------------------------------
instances = Table(
    "instances", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("workspace_id", String(36), nullable=True),
    Column("environment_id", String(36), nullable=True),
    Column("name", String(255), nullable=False, default=""),
    Column("delivery_model", String(24), nullable=False,
           default="MODEL_B_CONNECTED"),
    # MODEL_A_SAAS | MODEL_B_CONNECTED | MODEL_C_AIRGAPPED
    Column("state", String(16), nullable=False, default="ACTIVE"),
    # ACTIVE | SUSPENDED | REVOKED
    Column("public_key", String(64), nullable=True),   # Ed25519 statement key hex
    Column("fingerprint", String(128), nullable=True),
    Column("credential_hash", String(64), nullable=True),  # sha256(bearer cred)
    Column("subscription_id", String(36), nullable=True),
    Column("last_seen_at", DateTime, nullable=True),
    Column("enrolled_at", DateTime, nullable=False, default=utcnow),
    *_std_cols(),
    UniqueConstraint("credential_hash", name="uq_instance_credential"),
    Index("ix_instance_tenant", "tenant_id"),
)

# Single-use, expiring bootstrap secret an operator places on a new instance.
instance_enrollment_tokens = Table(
    "instance_enrollment_tokens", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("workspace_id", String(36), nullable=True),
    Column("environment_id", String(36), nullable=True),
    Column("token_hash", String(64), nullable=False),
    Column("delivery_model", String(24), nullable=False,
           default="MODEL_B_CONNECTED"),
    Column("subscription_id", String(36), nullable=True),
    Column("state", String(12), nullable=False, default="PENDING"),
    # PENDING | CONSUMED | EXPIRED | REVOKED
    Column("expires_at", DateTime, nullable=True),
    Column("consumed_by", String(36), nullable=True),   # instance id on consume
    *_std_cols(),
    UniqueConstraint("token_hash", name="uq_enroll_token_hash"),
    Index("ix_enroll_tenant", "tenant_id"),
)


# --------------------------------------------------------------------------
# Phase 8 — Partner & Commission context (docs/commercialization/03-domain-model
# §9). Vendor-owned (no tenant_id, like the product catalog); customer
# attribution is carried as an explicit ``customer_tenant_id`` column. Money is
# numeric(19,4) via Money. Commissions are the vendor's payout obligations;
# reversals/clawbacks are explicit compensating rows, never silent netting.
# --------------------------------------------------------------------------
partner_tiers = Table(
    "partner_tiers", metadata,
    Column("code", String(24), primary_key=True),   # REGISTERED..STRATEGIC
    Column("rank", Integer, nullable=False),
    Column("name", String(128), nullable=False),
    Column("requirements", JSON, nullable=True),     # certified engineers, ARR
    Column("benefits", JSON, nullable=True),         # margin %, protection days
)

partners = Table(
    "partners", metadata,
    Column("id", String(36), primary_key=True),
    Column("kind", String(12), nullable=False, default="SI"),
    # SI | OEM | RESELLER | REFERRAL
    Column("name", String(255), nullable=False),
    Column("status", String(16), nullable=False, default="ACTIVE"),
    # ACTIVE | SUSPENDED | TERMINATED
    Column("country_code", String(2), nullable=True),
    Column("tier_code", String(24), ForeignKey("partner_tiers.code"),
           nullable=True),
    Column("partner_tenant_id", String(36), nullable=True),  # if also a tenant
    *_std_cols(),
    Index("ix_partner_status", "status"),
)

partner_tier_assignments = Table(
    "partner_tier_assignments", metadata,
    Column("id", String(36), primary_key=True),
    Column("partner_id", String(36), ForeignKey("partners.id"), nullable=False),
    Column("tier_code", String(24), ForeignKey("partner_tiers.code"),
           nullable=False),
    Column("effective_from", DateTime, nullable=False, default=utcnow),
    Column("effective_to", DateTime, nullable=True),
    Column("review_ref", String(128), nullable=True),
    *_std_cols(),
    Index("ix_tierassign_partner", "partner_id"),
)

commission_plans = Table(
    "commission_plans", metadata,
    Column("id", String(36), primary_key=True),
    Column("code", String(64), nullable=False, unique=True),
    Column("basis", String(16), nullable=False, default="FIRST_YEAR"),
    # FIRST_YEAR | ALL_INVOICED
    Column("rate_table", JSON, nullable=False),   # {tier: {kind: pct}} or {tier: pct}
    Column("clawback_window_days", Integer, nullable=False, default=90),
    *_std_cols(),
)

partner_agreements = Table(
    "partner_agreements", metadata,
    Column("id", String(36), primary_key=True),
    Column("partner_id", String(36), ForeignKey("partners.id"), nullable=False),
    Column("state", String(16), nullable=False, default="DRAFT"),
    # DRAFT | ACTIVE | SUSPENDED | TERMINATED
    Column("commission_plan_id", String(36),
           ForeignKey("commission_plans.id"), nullable=True),
    Column("banking_verified", Boolean, nullable=False, default=False),
    Column("tax_docs_verified", Boolean, nullable=False, default=False),
    Column("effective_from", DateTime, nullable=True),
    *_std_cols(),
    Index("ix_agreement_partner", "partner_id"),
)

deal_registrations = Table(
    "deal_registrations", metadata,
    Column("id", String(36), primary_key=True),
    Column("partner_id", String(36), ForeignKey("partners.id"), nullable=False),
    Column("prospect_name", String(255), nullable=False),
    Column("customer_tenant_id", String(36), nullable=True),  # attribution
    Column("estimated_value", Money, nullable=True),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("state", String(12), nullable=False, default="SUBMITTED"),
    # SUBMITTED | APPROVED | REJECTED | WON | LOST | EXPIRED
    Column("protection_expires_at", DateTime, nullable=True),
    Column("order_ref", String(64), nullable=True),   # contract/subscription id
    *_std_cols(),
    Index("ix_deal_partner", "partner_id"),
    Index("ix_deal_state", "state"),
)

payout_batches = Table(
    "payout_batches", metadata,
    Column("id", String(36), primary_key=True),
    Column("partner_id", String(36), ForeignKey("partners.id"), nullable=False),
    Column("total", Money, nullable=False, default=ZERO_MONEY),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("state", String(12), nullable=False, default="DRAFT"),
    # DRAFT | EXECUTED
    Column("executed_at", DateTime, nullable=True),
    Column("provider_ref", String(128), nullable=True),
    *_std_cols(),
    Index("ix_payout_partner", "partner_id"),
)

commissions = Table(
    "commissions", metadata,
    Column("id", String(36), primary_key=True),
    Column("partner_id", String(36), ForeignKey("partners.id"), nullable=False),
    Column("agreement_id", String(36), ForeignKey("partner_agreements.id"),
           nullable=True),
    Column("deal_id", String(36), ForeignKey("deal_registrations.id"),
           nullable=True),
    Column("invoice_id", String(36), nullable=True),      # the paid invoice
    Column("order_ref", String(64), nullable=True),
    Column("customer_tenant_id", String(36), nullable=True),
    Column("basis_amount", Money, nullable=False, default=ZERO_MONEY),
    Column("amount", Money, nullable=False, default=ZERO_MONEY),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("state", String(12), nullable=False, default="ACCRUED"),
    # ACCRUED | APPROVED_C | PAYABLE | PAID | REVERSED | CLAWED_BACK
    Column("clawback_of_id", String(36), ForeignKey("commissions.id"),
           nullable=True),
    Column("payout_batch_id", String(36), ForeignKey("payout_batches.id"),
           nullable=True),
    Column("paid_at", DateTime, nullable=True),
    *_std_cols(),
    Index("ix_commission_partner", "partner_id"),
    Index("ix_commission_state", "state"),
    Index("ix_commission_invoice", "invoice_id"),
)


# --------------------------------------------------------------------------
# Phase 8 — Customer Success context (docs/commercialization/03-domain-model
# §12). Tenant-scoped. Derives from signals instances already report (usage,
# heartbeats, statements) — no new data-plane behaviour. Scores are labelled
# *assessments*: the formula version is stored with every score, and the health
# score is never presented as a measurement.
# --------------------------------------------------------------------------
health_score_snapshots = Table(
    "health_score_snapshots", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("scope", String(12), nullable=False, default="TENANT"),
    # TENANT | WORKSPACE
    Column("scope_id", String(36), nullable=False, default="*"),
    Column("score", Integer, nullable=False),        # 0..100
    Column("formula_version", String(32), nullable=False),
    Column("inputs", JSON, nullable=True),           # the components that fed it
    Column("computed_at", DateTime, nullable=False, default=utcnow),
    *_std_cols(),
    Index("ix_health_tenant", "tenant_id"),
)

adoption_signals = Table(
    "adoption_signals", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("signal_code", String(64), nullable=False),
    # FIRST_ASSESSMENT | FIRST_CONVERSION | TWIN_USED | AI_ENABLED | ...
    Column("instance_id", String(36), nullable=True),
    Column("occurred_at", DateTime, nullable=False, default=utcnow),
    Column("evidence_ref", String(128), nullable=True),   # usage event id
    *_std_cols(),
    UniqueConstraint("tenant_id", "signal_code",
                     name="uq_adoption_tenant_signal"),
    Index("ix_adoption_tenant", "tenant_id"),
)

lifecycle_stage_transitions = Table(
    "lifecycle_stage_transitions", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("from_stage", String(16), nullable=True),
    Column("to_stage", String(16), nullable=False),
    # PROSPECT|ONBOARDING|ADOPTING|EXPANDING|RENEWING|AT_RISK|CHURNED
    Column("at", DateTime, nullable=False, default=utcnow),
    Column("trigger", String(8), nullable=False, default="MANUAL"),  # RULE|MANUAL
    Column("reason", Text, nullable=True),
    *_std_cols(),
    Index("ix_lifecycle_tenant", "tenant_id"),
)

risk_flags = Table(
    "risk_flags", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("code", String(48), nullable=False),
    # USAGE_DECLINE | UNPAID_INVOICE | LICENSE_EXPIRING | STATEMENT_OVERDUE ...
    Column("severity", String(8), nullable=False, default="MEDIUM"),
    # LOW | MEDIUM | HIGH | CRITICAL
    Column("detail", JSON, nullable=True),
    Column("opened_at", DateTime, nullable=False, default=utcnow),
    Column("resolved_at", DateTime, nullable=True),
    Column("owner_membership_id", String(36), nullable=True),
    *_std_cols(),
    Index("ix_risk_tenant_open", "tenant_id", "resolved_at"),
)


# --------------------------------------------------------------------------
# Phase 8 — AI Cost Governance (docs/commercialization/03-domain-model.md §10).
# Rates are money PER 1,000,000 TOKENS (numeric(19,4) handles per-1M pricing
# cleanly; per-single-token is sub-$0.0001). est_cost is a LABELLED ESTIMATE —
# the provider's own bill remains the source of truth. Budget breach never
# blocks the deterministic engines; BLOCK_AI_ONLY is the hardest action.
# --------------------------------------------------------------------------
ai_rate_cards = Table(
    "ai_rate_cards", metadata,
    Column("id", String(36), primary_key=True),
    Column("provider", String(16), nullable=False),      # ANTHROPIC | BEDROCK
    Column("model_id", String(128), nullable=False),
    Column("input_rate", Money, nullable=False),         # per 1M input tokens
    Column("output_rate", Money, nullable=False),        # per 1M output tokens
    Column("currency", String(3), nullable=False, default="USD"),
    Column("effective_from", DateTime, nullable=False, default=utcnow),
    Column("active", Boolean, nullable=False, default=True),
    *_std_cols(),
    Index("ix_airate_model", "provider", "model_id"),
)

# Immutable, append-only — one model invocation reported by an instance.
ai_usage_records = Table(
    "ai_usage_records", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("instance_id", String(36), nullable=True),
    Column("provider", String(16), nullable=False),
    Column("model_id", String(128), nullable=False),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("purpose", String(64), nullable=True),        # explain / assist area
    Column("job_ref", String(128), nullable=True),
    Column("est_cost", Money, nullable=False, default=ZERO_MONEY),  # labelled est
    Column("currency", String(3), nullable=False, default="USD"),
    Column("occurred_at", DateTime, nullable=False, default=utcnow),
    Column("idempotency_key", String(128), nullable=False),
    Column("created_at", DateTime, nullable=False, default=utcnow),
    Column("created_by", String(64), nullable=False, default="system"),
    UniqueConstraint("tenant_id", "idempotency_key",
                     name="uq_aiusage_tenant_idem"),
    Index("ix_aiusage_tenant_time", "tenant_id", "occurred_at"),
)

ai_budgets = Table(
    "ai_budgets", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(36), ForeignKey("tenants.id"), nullable=False),
    Column("scope", String(12), nullable=False, default="TENANT"),
    # TENANT | WORKSPACE
    Column("scope_id", String(36), nullable=False, default="*"),
    Column("period", String(12), nullable=False, default="MONTHLY"),
    Column("limit_tokens", Integer, nullable=True),
    Column("limit_est_cost", Money, nullable=True),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("action_on_breach", String(16), nullable=False, default="WARN"),
    # WARN | THROTTLE | BLOCK_AI_ONLY
    Column("active", Boolean, nullable=False, default=True),
    *_std_cols(),
    UniqueConstraint("tenant_id", "scope", "scope_id", "period",
                     name="uq_aibudget_scope_period"),
    Index("ix_aibudget_tenant", "tenant_id"),
)
