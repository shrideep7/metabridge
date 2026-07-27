"""Usage metering & reporting — the finance-grade meter of record.

Design (docs/commercialization/03-domain-model.md §7):

- ``usage_events`` is **immutable and append-only**. Duplicate submissions are
  rejected by ``(tenant_id, idempotency_key)`` and return the existing event —
  aggregation is therefore reproducible from source.
- Corrections are **never** mutations: ``adjust_usage`` appends a signed
  compensating ``adjustment_event`` referencing the original, and is audited.
- ``aggregate`` recomputes over events + adjustments, honouring each meter's
  aggregation rule (SUM / DISTINCT_COUNT / MAX / LAST). Billing-period
  aggregates can be frozen (``is_final``); a finalized aggregate is never
  silently recomputed.
- Every billable figure on a statement links back to the source events; a
  statement shows opening entitlement, usage, adjustments, remaining, overage.
- Air-gapped instances export an Ed25519-**signed** usage statement (reusing
  the licensing signer); the control plane verifies fail-closed and ingests it
  idempotently on statement serial.

Tenant isolation: every read/write is filtered by ``tenant_id``; a caller may
pass ``expected_tenant`` (a data-plane instance proving its own tenant) and a
mismatch is rejected before any row is touched.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from . import audit, schema
from .context import TenantContext
from .errors import NotFoundError, ValidationError

VALID_SOURCES = ("CONTROL_PLANE", "INSTANCE_BATCH", "SIGNED_STATEMENT")


# --------------------------------------------------------------------------
# Meter catalogue
# --------------------------------------------------------------------------
def get_meter(conn: Connection, meter_code: str) -> dict:
    row = conn.execute(select(schema.meter_definitions).where(
        schema.meter_definitions.c.meter_code == meter_code)).mappings().first()
    if row is None:
        raise ValidationError(f"unknown meter code: {meter_code}")
    return dict(row)


def list_meters(engine: Engine) -> list:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(schema.meter_definitions)
            .order_by(schema.meter_definitions.c.meter_code)).mappings()]


# --------------------------------------------------------------------------
# Ingestion — immutable, idempotent
# --------------------------------------------------------------------------
def _record_in_conn(conn: Connection, *, tenant_id: str, meter_code: str,
                    quantity: int, idempotency_key: str,
                    occurred_at: datetime, source: str, dimensions: dict,
                    subscription_id: Optional[str], instance_id: Optional[str],
                    workspace_id: Optional[str], environment_id: Optional[str],
                    actor: str) -> tuple[str, bool]:
    """Insert one usage event; returns (event_id, created). Idempotent on
    (tenant, idempotency_key) — a replay returns the stored event."""
    meter = get_meter(conn, meter_code)
    if quantity < 0:
        raise ValidationError("usage quantity must be >= 0")
    if source not in VALID_SOURCES:
        raise ValidationError(f"invalid usage source: {source}")
    existing = conn.execute(select(schema.usage_events).where(
        (schema.usage_events.c.tenant_id == tenant_id)
        & (schema.usage_events.c.idempotency_key
           == idempotency_key))).mappings().first()
    if existing:
        if existing["meter_code"] != meter_code:
            raise ValidationError(
                "idempotency_key already used for a different meter")
        return existing["id"], False
    eid = schema.new_id()
    try:
        conn.execute(schema.usage_events.insert().values(
            id=eid, tenant_id=tenant_id, subscription_id=subscription_id,
            meter_code=meter_code, quantity=int(quantity), unit=meter["unit"],
            occurred_at=occurred_at, received_at=schema.utcnow(),
            instance_id=instance_id, workspace_id=workspace_id,
            environment_id=environment_id, source=source,
            idempotency_key=idempotency_key, dimensions=dimensions or {},
            schema_version=1, created_by=actor))
    except IntegrityError:                      # concurrent identical submit
        row = conn.execute(select(schema.usage_events.c.id).where(
            (schema.usage_events.c.tenant_id == tenant_id)
            & (schema.usage_events.c.idempotency_key
               == idempotency_key))).first()
        if row:
            return row[0], False
        raise
    return eid, True


def record_usage(engine: Engine, *, tenant_id: str, meter_code: str,
                 quantity: int, idempotency_key: str,
                 occurred_at: Optional[datetime] = None,
                 source: str = "CONTROL_PLANE", dimensions: Optional[dict] = None,
                 subscription_id: Optional[str] = None,
                 instance_id: Optional[str] = None,
                 workspace_id: Optional[str] = None,
                 environment_id: Optional[str] = None,
                 expected_tenant: Optional[str] = None,
                 actor: str = "system") -> dict:
    """Record a single usage event. Requires an idempotency key."""
    if not (idempotency_key or "").strip():
        raise ValidationError("record_usage requires an idempotency_key")
    if expected_tenant is not None and expected_tenant != tenant_id:
        raise ValidationError("tenant scope mismatch")
    occurred_at = occurred_at or schema.utcnow()
    with engine.begin() as conn:
        eid, created = _record_in_conn(
            conn, tenant_id=tenant_id, meter_code=meter_code, quantity=quantity,
            idempotency_key=idempotency_key, occurred_at=occurred_at,
            source=source, dimensions=dimensions or {},
            subscription_id=subscription_id, instance_id=instance_id,
            workspace_id=workspace_id, environment_id=environment_id,
            actor=actor)
    return {"event_id": eid, "created": created}


def ingest_batch(engine: Engine, *, tenant_id: str, events: list,
                 source: str = "INSTANCE_BATCH",
                 expected_tenant: Optional[str] = None,
                 actor: str = "system") -> dict:
    """Ingest a batch of events atomically; each is idempotent. Returns counts
    of created vs. duplicate. A malformed event aborts the whole batch."""
    if expected_tenant is not None and expected_tenant != tenant_id:
        raise ValidationError("tenant scope mismatch")
    if not isinstance(events, list) or not events:
        raise ValidationError("events must be a non-empty list")
    created = dup = 0
    now = schema.utcnow()
    with engine.begin() as conn:
        for e in events:
            key = e.get("idempotency_key")
            if not key:
                raise ValidationError("each event requires an idempotency_key")
            oa = e.get("occurred_at") or now
            if isinstance(oa, str):
                oa = datetime.fromisoformat(oa)
            _eid, was_new = _record_in_conn(
                conn, tenant_id=tenant_id, meter_code=e["meter_code"],
                quantity=int(e.get("quantity", 1)), idempotency_key=key,
                occurred_at=oa, source=source, dimensions=e.get("dimensions"),
                subscription_id=e.get("subscription_id"),
                instance_id=e.get("instance_id"),
                workspace_id=e.get("workspace_id"),
                environment_id=e.get("environment_id"), actor=actor)
            created += int(was_new)
            dup += int(not was_new)
    return {"created": created, "duplicates": dup, "total": len(events)}


# --------------------------------------------------------------------------
# Corrections — append-only adjustments (audited)
# --------------------------------------------------------------------------
def adjust_usage(engine: Engine, staff_ctx: TenantContext,
                 original_event_id: str, *, quantity_delta: int,
                 reason_code: str, evidence_ref: Optional[str] = None) -> str:
    """Append a signed compensating adjustment. The original event is never
    mutated. High-risk + audited (``usage:adjust``)."""
    staff_ctx.require("usage:adjust")
    if not (reason_code or "").strip():
        raise ValidationError("an adjustment requires a reason_code")
    if quantity_delta == 0:
        raise ValidationError("quantity_delta must be non-zero")
    aid = schema.new_id()
    with engine.begin() as conn:
        ev = conn.execute(select(schema.usage_events).where(
            schema.usage_events.c.id == original_event_id)).mappings().first()
        if ev is None:
            raise NotFoundError(f"usage_events:{original_event_id}")
        # tenant scope: a tenant-scoped staff context may only adjust events in
        # its own tenant; only a global (SUPER_ADMIN) context crosses tenants.
        if staff_ctx.tenant_id not in (schema.GLOBAL_TENANT, ev["tenant_id"]):
            raise ValidationError("tenant scope mismatch")
        # a signed delta only has well-defined meaning for SUM meters; reject
        # corrections to DISTINCT_COUNT / MAX / LAST rather than accept a delta
        # that would be audited but never affect the billable figure.
        meter = get_meter(conn, ev["meter_code"])
        if meter["aggregation"] != "SUM":
            raise ValidationError(
                f"adjustments are only supported for SUM meters; "
                f"{ev['meter_code']} is {meter['aggregation']}")
        conn.execute(schema.adjustment_events.insert().values(
            id=aid, tenant_id=ev["tenant_id"],
            original_event_id=original_event_id, meter_code=ev["meter_code"],
            quantity_delta=int(quantity_delta), reason_code=reason_code,
            approved_by=staff_ctx.user_id, evidence_ref=evidence_ref,
            occurred_at=ev["occurred_at"]))   # correction lands in event's period
        audit.record(conn, staff_ctx, action="usage.adjust",
                     resource_type="usage_event",
                     resource_id=original_event_id,
                     before={"quantity": ev["quantity"]},
                     after={"quantity_delta": quantity_delta,
                            "reason_code": reason_code},
                     reason=reason_code, tenant_id=ev["tenant_id"])
    return aid


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def _raw_usage(conn: Connection, tenant_id: str, meter_code: str,
               start: datetime, end: datetime, aggregation: str,
               dedup_dimension: Optional[str]) -> int:
    """Compute usage over [start, end) from events + adjustments per the
    meter's aggregation rule."""
    ev = schema.usage_events
    base = and_(ev.c.tenant_id == tenant_id, ev.c.meter_code == meter_code,
                ev.c.occurred_at >= start, ev.c.occurred_at < end)
    if aggregation == "DISTINCT_COUNT":
        if not dedup_dimension:
            raise ValidationError(
                f"meter {meter_code} is DISTINCT_COUNT but has no "
                "dedup_dimension configured")
        rows = conn.execute(select(ev.c.dimensions).where(base)).mappings().all()
        seen = set()
        for r in rows:
            dims = r["dimensions"] or {}
            key = dims.get(dedup_dimension)
            # events missing the dim are NOT silently dropped — they collapse
            # into one explicit "unknown" bucket so the count is never inflated
            # nor understated to zero.
            seen.add(key if key is not None else "\x00__missing__")
        return len(seen)
    if aggregation in ("MAX", "LAST"):
        if aggregation == "MAX":
            val = conn.execute(select(func.max(ev.c.quantity)).where(base)) \
                .scalar()
            return int(val or 0)
        row = conn.execute(select(ev.c.quantity).where(base)
                           .order_by(ev.c.occurred_at.desc()).limit(1)).first()
        return int(row[0]) if row else 0
    # SUM (default): events + signed adjustment deltas. Adjustments carry the
    # ORIGINAL event's occurred_at, so a correction lands in the period the
    # corrected usage occurred in, not the period the correction was made.
    total = conn.execute(select(func.coalesce(func.sum(ev.c.quantity), 0))
                         .where(base)).scalar() or 0
    adj = schema.adjustment_events
    delta = conn.execute(
        select(func.coalesce(func.sum(adj.c.quantity_delta), 0)).where(
            (adj.c.tenant_id == tenant_id) & (adj.c.meter_code == meter_code)
            & (adj.c.occurred_at >= start) & (adj.c.occurred_at < end))).scalar()
    return int(total) + int(delta or 0)


def _frozen_quantity(conn: Connection, tenant_id: str, meter_code: str,
                     start: datetime, end: datetime) -> Optional[int]:
    """Return the finalized aggregate quantity for an exact billing period, or
    None to recompute live. Once a period is frozen (is_final), every finance
    output must serve the frozen figure — never a live recompute that could
    diverge from what was invoiced."""
    agg = schema.usage_aggregates
    row = conn.execute(select(agg.c.quantity).where(
        (agg.c.tenant_id == tenant_id) & (agg.c.meter_code == meter_code)
        & (agg.c.scope == "TENANT") & (agg.c.scope_id == "*")
        & (agg.c.period_start == start) & (agg.c.period_end == end)
        & (agg.c.is_final.is_(True)))).first()
    return int(row[0]) if row else None


def _usage(conn: Connection, tenant_id: str, meter: dict, start: datetime,
           end: datetime) -> int:
    """Frozen figure if the period is finalized, else a live recompute."""
    frozen = _frozen_quantity(conn, tenant_id, meter["meter_code"], start, end)
    if frozen is not None:
        return frozen
    return _raw_usage(conn, tenant_id, meter["meter_code"], start, end,
                      meter["aggregation"], meter["dedup_dimension"])


def aggregate(engine: Engine, *, tenant_id: str, meter_code: str,
              period_start: datetime, period_end: datetime,
              level: str = "BILLING_PERIOD", finalize: bool = False,
              actor: str = "system") -> dict:
    """Recompute and materialize an aggregate for a tenant/meter/period.

    Refuses to overwrite an already-``is_final`` aggregate (post-freeze
    corrections must go to a credit/true-up, never mutate a finalized figure)."""
    with engine.begin() as conn:
        meter = get_meter(conn, meter_code)
        qty = _raw_usage(conn, tenant_id, meter_code, period_start,
                         period_end, meter["aggregation"],
                         meter["dedup_dimension"])
        agg = schema.usage_aggregates
        existing = conn.execute(select(agg).where(
            (agg.c.tenant_id == tenant_id) & (agg.c.meter_code == meter_code)
            & (agg.c.level == level) & (agg.c.scope == "TENANT")
            & (agg.c.scope_id == "*")
            & (agg.c.period_start == period_start))).mappings().first()
        if existing and existing["is_final"]:
            raise ValidationError(
                "aggregate is finalized; post-freeze corrections require a "
                "credit or next-period true-up")
        now = schema.utcnow()
        if existing:
            conn.execute(agg.update().where(agg.c.id == existing["id"]).values(
                quantity=qty, computed_at=now, is_final=finalize,
                period_end=period_end))
            aid = existing["id"]
        else:
            aid = schema.new_id()
            conn.execute(agg.insert().values(
                id=aid, tenant_id=tenant_id, meter_code=meter_code, level=level,
                scope="TENANT", scope_id="*", period_start=period_start,
                period_end=period_end, quantity=qty, computed_at=now,
                is_final=finalize))
        if finalize:
            audit.record(conn, None, action="usage.aggregate.finalize",
                         resource_type="usage_aggregate", resource_id=aid,
                         after={"meter_code": meter_code, "quantity": qty,
                                "period_start": str(period_start)},
                         tenant_id=tenant_id, actor_type="SYSTEM", actor_id=actor)
    return {"aggregate_id": aid, "meter_code": meter_code, "quantity": qty,
            "is_final": finalize}


def usage_summary(engine: Engine, *, tenant_id: str, period_start: datetime,
                  period_end: datetime) -> dict:
    """Per-meter usage summary for a tenant over a window (computed live from
    source events + adjustments)."""
    with engine.connect() as conn:
        meters = conn.execute(select(schema.meter_definitions)).mappings().all()
        out = []
        for m in meters:
            qty = _usage(conn, tenant_id, dict(m), period_start, period_end)
            if qty:
                out.append({"meter_code": m["meter_code"], "unit": m["unit"],
                            "quantity": qty, "billable": m["billable"],
                            "aggregation": m["aggregation"]})
    return {"tenant_id": tenant_id, "period_start": str(period_start),
            "period_end": str(period_end), "meters": out}


# --------------------------------------------------------------------------
# Overage detection — usage vs. entitlement limits
# --------------------------------------------------------------------------
def overage_report(engine: Engine, *, tenant_id: str, period_start: datetime,
                   period_end: datetime) -> dict:
    """For each billable meter linked to a quota entitlement, compare usage to
    the effective limit and report overage."""
    from . import entitlements as ent
    lines = []
    with engine.connect() as conn:
        sub = ent._active_subscription(conn, tenant_id)
        meters = conn.execute(select(schema.meter_definitions).where(
            (schema.meter_definitions.c.billable.is_(True))
            & (schema.meter_definitions.c.entitlement_code.isnot(None)))) \
            .mappings().all()
        for m in meters:
            used = _usage(conn, tenant_id, dict(m), period_start, period_end)
            limit = None
            unlimited = False
            if sub is not None:
                e = ent._entitlement(conn, sub["id"], m["entitlement_code"])
                if e is not None:
                    unlimited = bool(e["value"].get("unlimited"))
                    limit = None if unlimited else e["value"].get("limit")
            over = 0
            if limit is not None:
                over = max(0, used - int(limit))
            if used or over:
                lines.append({"meter_code": m["meter_code"],
                              "entitlement_code": m["entitlement_code"],
                              "used": used,
                              "limit": (None if unlimited else limit),
                              "unlimited": unlimited, "overage": over})
    return {"tenant_id": tenant_id, "subscription_id":
            (sub["id"] if sub else None), "period_start": str(period_start),
            "period_end": str(period_end), "lines": lines}


# --------------------------------------------------------------------------
# Finance-grade statement (per §6.4 statement fields)
# --------------------------------------------------------------------------
def usage_statement(engine: Engine, *, tenant_id: str,
                    period_start: datetime, period_end: datetime,
                    subscription_id: Optional[str] = None) -> dict:
    """A finance-acceptable statement: per meter — opening entitlement, usage,
    adjustments, remaining, overage — with source-event linkage counts."""
    from . import entitlements as ent
    ev = schema.usage_events
    adj = schema.adjustment_events
    with engine.connect() as conn:
        tenant = conn.execute(select(schema.tenants).where(
            schema.tenants.c.id == tenant_id)).mappings().first()
        sub = None
        if subscription_id:
            sub = conn.execute(select(schema.subscriptions).where(
                (schema.subscriptions.c.id == subscription_id)
                & (schema.subscriptions.c.tenant_id == tenant_id))) \
                .mappings().first()
        else:
            sub = ent._active_subscription(conn, tenant_id)
        meters = conn.execute(select(schema.meter_definitions)).mappings().all()
        lines = []
        for m in meters:
            mc = m["meter_code"]
            base = and_(ev.c.tenant_id == tenant_id, ev.c.meter_code == mc,
                        ev.c.occurred_at >= period_start,
                        ev.c.occurred_at < period_end)
            n_events = conn.execute(select(func.count()).select_from(ev)
                                    .where(base)).scalar_one()
            if n_events == 0:
                continue
            gross = _usage(conn, tenant_id, dict(m), period_start, period_end)
            adj_total = conn.execute(select(
                func.coalesce(func.sum(adj.c.quantity_delta), 0)).where(
                (adj.c.tenant_id == tenant_id) & (adj.c.meter_code == mc)
                & (adj.c.occurred_at >= period_start)
                & (adj.c.occurred_at < period_end))).scalar() or 0
            opening = None
            remaining = None
            overage = 0
            if sub is not None and m["entitlement_code"]:
                e = ent._entitlement(conn, sub["id"], m["entitlement_code"])
                if e is not None and not e["value"].get("unlimited"):
                    opening = e["value"].get("limit")
                    if opening is not None:
                        remaining = max(0, int(opening) - gross)
                        overage = max(0, gross - int(opening))
            lines.append({
                "meter_code": mc, "unit": m["unit"], "billable": m["billable"],
                "opening_entitlement": opening, "usage": gross,
                "adjustments": int(adj_total), "remaining": remaining,
                "overage": overage, "source_events": int(n_events)})
    return {
        "tenant": (tenant["legal_name"] if tenant else tenant_id),
        "tenant_id": tenant_id,
        "subscription_id": (sub["id"] if sub else None),
        "period_start": str(period_start), "period_end": str(period_end),
        "generated_at": str(schema.utcnow()),
        "lines": lines,
    }


def statement_csv(statement: dict) -> str:
    """Render a usage_statement() dict as CSV (finance export)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["tenant", "period_start", "period_end", "meter_code", "unit",
                "opening_entitlement", "usage", "adjustments", "remaining",
                "overage", "billable", "source_events"])
    for ln in statement["lines"]:
        w.writerow([statement["tenant"], statement["period_start"],
                    statement["period_end"], ln["meter_code"], ln["unit"],
                    ln["opening_entitlement"], ln["usage"], ln["adjustments"],
                    ln["remaining"], ln["overage"], ln["billable"],
                    ln["source_events"]])
    return buf.getvalue()


# --------------------------------------------------------------------------
# Signed usage statements (air-gapped instance -> control plane)
# --------------------------------------------------------------------------
def export_signed_statement(engine: Engine, *, tenant_id: str, instance_id: str,
                            period_start: datetime, period_end: datetime,
                            serial: str) -> dict:
    """Build a canonical usage document for an instance and sign it (Ed25519,
    reusing the licensing signer). This is what an air-gapped instance would
    produce; here it lets the control plane round-trip and verify."""
    from . import licensing
    stmt = usage_statement(engine, tenant_id=tenant_id,
                           period_start=period_start, period_end=period_end)
    payload = {"tenant_id": tenant_id, "instance_id": instance_id,
               "serial": serial, "period_start": str(period_start),
               "period_end": str(period_end),
               "lines": [{"meter_code": ln["meter_code"], "usage": ln["usage"]}
                         for ln in stmt["lines"]]}
    priv, key_id = licensing._load_signer()
    signature = priv.sign(licensing._canonical(payload)).hex()
    return {"payload": payload, "signature": signature, "signer_key_id": key_id}


def _resolve_trust_key(conn: Connection, tenant_id: str,
                       instance_id: str) -> str:
    """Return the pinned public key to verify an instance's signed statement.

    The trust anchor is server-side, NEVER caller-supplied (a caller-supplied
    key is forgeable). Phase 5: pin the enrolled instance's registered public
    key, looked up by (tenant_id, instance_id). Falls back to the control-plane
    signer for the pre-enrollment self-check path (what ``export_signed_statement``
    uses when no instance has registered a key)."""
    row = conn.execute(select(schema.instances.c.public_key).where(
        (schema.instances.c.tenant_id == tenant_id)
        & (schema.instances.c.id == instance_id)
        & (schema.instances.c.public_key.isnot(None)))).first()
    if row and row[0]:
        return row[0]
    from . import licensing
    return licensing.public_key_hex()


def ingest_signed_statement(engine: Engine, *, payload: dict, signature: str,
                            actor: str = "system") -> dict:
    """Verify (fail-closed, against a SERVER-PINNED trust anchor) and ingest a
    signed usage statement. Idempotent on (tenant, instance, serial)."""
    from . import licensing
    tenant_id = payload.get("tenant_id")
    instance_id = payload.get("instance_id")
    serial = payload.get("serial")
    if not tenant_id or not instance_id or not serial:
        raise ValidationError(
            "signed statement requires tenant_id, instance_id and serial")
    with engine.begin() as conn:
        # the tenant must exist — never mint events into an arbitrary tenant id
        tenant = conn.execute(select(schema.tenants.c.id).where(
            schema.tenants.c.id == tenant_id)).first()
        if tenant is None:
            raise ValidationError("unknown tenant for signed statement")
        trust_key = _resolve_trust_key(conn, tenant_id, instance_id)
        if not licensing.verify_license_file(payload, signature, trust_key):
            raise ValidationError("usage statement signature is invalid")
        dup = conn.execute(select(schema.usage_statements.c.id).where(
            (schema.usage_statements.c.tenant_id == tenant_id)
            & (schema.usage_statements.c.instance_id == instance_id)
            & (schema.usage_statements.c.serial == serial))).first()
        if dup:
            return {"statement_id": dup[0], "state": "DUPLICATE", "ingested": 0}
        sid = schema.new_id()
        ingested = 0
        for ln in payload.get("lines", []):
            key = f"stmt:{instance_id}:{serial}:{ln['meter_code']}"
            _eid, created = _record_in_conn(
                conn, tenant_id=tenant_id, meter_code=ln["meter_code"],
                quantity=int(ln.get("usage", 0)), idempotency_key=key,
                occurred_at=schema.utcnow(), source="SIGNED_STATEMENT",
                dimensions={"statement_serial": serial,
                            "instance_id": instance_id},
                subscription_id=None, instance_id=instance_id,
                workspace_id=None, environment_id=None, actor=actor)
            ingested += int(created)
        conn.execute(schema.usage_statements.insert().values(
            id=sid, tenant_id=tenant_id, instance_id=instance_id,
            serial=serial, canonical_payload=payload, signature=signature,
            state="INGESTED", event_count=ingested, verified_at=schema.utcnow()))
        audit.record(conn, None, action="usage.statement.ingest",
                     resource_type="usage_statement", resource_id=sid,
                     after={"serial": serial, "instance_id": instance_id,
                            "ingested": ingested},
                     tenant_id=tenant_id, actor_type="SYSTEM", actor_id=actor)
    return {"statement_id": sid, "state": "INGESTED", "ingested": ingested}
