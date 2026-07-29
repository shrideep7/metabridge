"""Billing integration — invoices, payments, and provider webhooks.

The control plane owns the invoice of record (§8.2); providers are adapters
(``billing_providers``). Money is ``Decimal`` (``schema.Money``); monetary
rollups (amount paid vs. total) are summed in Python over Decimals, never via
SQL SUM — exact and identical on SQLite and PostgreSQL.

Invariants:
- An invoice is rated output: ``issue_invoice`` mirrors a COMPLETE RatingRun's
  lines and is idempotent per rating run (re-issuing returns the same invoice).
- Payments are idempotent on ``(tenant, idempotency_key)``; invoice state is
  derived from the sum of its payments (ISSUED -> PARTIALLY_PAID -> PAID).
- Inbound webhooks are verified fail-closed and processed at most once per
  ``(provider, external_id)``, so a replayed provider event can't double-pay.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Connection, Engine

from . import audit, billing_providers as bp, schema
from .context import TenantContext
from .errors import NotFoundError, ValidationError
from .schema import ZERO_MONEY

_PAYABLE_STATES = ("ISSUED", "PARTIALLY_PAID", "OVERDUE")


def _money(value, field="amount") -> Decimal:
    if isinstance(value, float):
        raise ValidationError(f"{field} must be a string/Decimal, not float")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        raise ValidationError(f"{field} is not a valid amount: {value!r}")
    return d.quantize(schema.MONEY_SCALE)


# --------------------------------------------------------------------------
# Billing accounts
# --------------------------------------------------------------------------
def create_billing_account(engine: Engine, ctx: TenantContext, *,
                           tenant_id: Optional[str] = None,
                           provider: str = "MANUAL",
                           external_customer_ref: Optional[str] = None,
                           payment_terms_days: int = 30,
                           currency: str = "USD",
                           tax_ids: Optional[dict] = None) -> str:
    ctx.require("billing:manage")
    if provider not in bp.PROVIDERS:
        raise ValidationError(f"unsupported provider: {provider}")
    tid = tenant_id or (None if ctx.tenant_id == schema.GLOBAL_TENANT
                        else ctx.tenant_id)
    if not tid:
        raise ValidationError("a tenant_id is required for a billing account")
    if ctx.tenant_id not in (schema.GLOBAL_TENANT, tid):
        raise ValidationError("tenant scope mismatch")
    aid = schema.new_id()
    with engine.begin() as conn:
        conn.execute(schema.billing_accounts.insert().values(
            id=aid, tenant_id=tid, provider=provider,
            external_customer_ref=external_customer_ref,
            payment_terms_days=int(payment_terms_days),
            currency=currency.upper(), tax_ids=tax_ids, created_by=ctx.user_id))
        audit.record(conn, ctx, action="billing_account.create",
                     resource_type="billing_account", resource_id=aid,
                     after={"provider": provider}, tenant_id=tid)
    return aid


# --------------------------------------------------------------------------
# Invoices (from rating runs)
# --------------------------------------------------------------------------
def _next_number(conn: Connection, tenant_id: str) -> str:
    n = conn.execute(select(func.count()).select_from(schema.invoices).where(
        schema.invoices.c.tenant_id == tenant_id)).scalar_one()
    return f"INV-{n + 1:06d}"


def issue_invoice(engine: Engine, ctx: TenantContext, *, rating_run_id: str,
                  billing_account_id: Optional[str] = None,
                  due_days: Optional[int] = None) -> dict:
    """Issue an invoice from a COMPLETE rating run. Idempotent per rating run:
    if one already exists it is returned unchanged."""
    ctx.require("billing:manage")
    with engine.begin() as conn:
        run = conn.execute(select(schema.rating_runs).where(
            schema.rating_runs.c.id == rating_run_id)).mappings().first()
        if run is None:
            raise NotFoundError(f"rating_runs:{rating_run_id}")
        if ctx.tenant_id not in (schema.GLOBAL_TENANT, run["tenant_id"]):
            raise ValidationError("tenant scope mismatch")
        if run["state"] != "COMPLETE":
            raise ValidationError(
                f"rating run is {run['state']}, not COMPLETE — cannot invoice")
        tid = run["tenant_id"]
        existing = conn.execute(select(schema.invoices).where(
            (schema.invoices.c.tenant_id == tid)
            & (schema.invoices.c.rating_run_id == rating_run_id))) \
            .mappings().first()
        if existing:
            return {"invoice_id": existing["id"], "number": existing["number"],
                    "state": existing["state"], "created": False}
        rated = conn.execute(select(schema.rated_lines).where(
            schema.rated_lines.c.rating_run_id == rating_run_id)
            .order_by(schema.rated_lines.c.meter_code)).mappings().all()
        subtotal = sum((_money(r["final_amount"]) for r in rated), ZERO_MONEY)
        tax = ZERO_MONEY               # delegated to the provider (§8.1 step 8)
        total = _money(subtotal + tax)
        terms = due_days
        if terms is None and billing_account_id:
            ba = conn.execute(select(schema.billing_accounts).where(
                schema.billing_accounts.c.id == billing_account_id)) \
                .mappings().first()
            terms = ba["payment_terms_days"] if ba else 30
        terms = 30 if terms is None else int(terms)
        now = schema.utcnow()
        iid = schema.new_id()
        for attempt in range(5):       # number races resolve via the uq index
            number = _next_number(conn, tid)
            try:
                with conn.begin_nested():
                    conn.execute(schema.invoices.insert().values(
                        id=iid, tenant_id=tid,
                        billing_account_id=billing_account_id,
                        rating_run_id=rating_run_id, number=number,
                        state="ISSUED", issued_at=now,
                        due_at=now + timedelta(days=terms), subtotal=subtotal,
                        tax=tax, total=total, amount_paid=ZERO_MONEY,
                        currency=run["currency"], created_by=ctx.user_id))
                break
            except IntegrityError:
                if attempt == 4:
                    raise
                continue
        for r in rated:
            conn.execute(schema.invoice_lines.insert().values(
                id=schema.new_id(), tenant_id=tid, invoice_id=iid,
                rated_line_id=r["id"], description=r["meter_code"],
                amount=_money(r["final_amount"]), currency=r["currency"]))
        audit.record(conn, ctx, action="invoice.issue",
                     resource_type="invoice", resource_id=iid,
                     after={"number": number, "total": str(total),
                            "rating_run_id": rating_run_id}, tenant_id=tid)
    return {"invoice_id": iid, "number": number, "state": "ISSUED",
            "total": total, "created": True}


def void_invoice(engine: Engine, ctx: TenantContext, invoice_id: str, *,
                 reason: str = "") -> None:
    ctx.require("billing:manage")
    with engine.begin() as conn:
        inv = _load_invoice(conn, ctx, invoice_id)
        if inv["state"] in ("PAID", "VOID"):
            raise ValidationError(f"cannot void a {inv['state']} invoice")
        conn.execute(schema.invoices.update().where(
            schema.invoices.c.id == invoice_id).values(
            state="VOID", updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="invoice.void",
                     resource_type="invoice", resource_id=invoice_id,
                     before={"state": inv["state"]}, after={"state": "VOID"},
                     reason=reason, tenant_id=inv["tenant_id"])


# --------------------------------------------------------------------------
# Payments (idempotent; state derived from the payment sum)
# --------------------------------------------------------------------------
def _load_invoice(conn: Connection, ctx: TenantContext, invoice_id: str) -> dict:
    inv = conn.execute(select(schema.invoices).where(
        schema.invoices.c.id == invoice_id)).mappings().first()
    if inv is None:
        raise NotFoundError(f"invoices:{invoice_id}")
    if ctx.tenant_id not in (schema.GLOBAL_TENANT, inv["tenant_id"]):
        raise NotFoundError(f"invoices:{invoice_id}")
    return dict(inv)


def _apply_payment(conn: Connection, inv: dict, *, amount: Decimal,
                   idempotency_key: str, provider: str,
                   provider_ref: Optional[str], method: Optional[str],
                   received_at: datetime, actor: str) -> tuple[str, bool, str]:
    """Insert one payment (idempotent on tenant+key) and recompute invoice
    state from the sum of payments. Returns (payment_id, created, new_state)."""
    tid = inv["tenant_id"]
    dup = conn.execute(select(schema.payments).where(
        (schema.payments.c.tenant_id == tid)
        & (schema.payments.c.idempotency_key == idempotency_key))) \
        .mappings().first()
    if dup:
        return dup["id"], False, inv["state"]
    if inv["state"] in ("VOID",):
        raise ValidationError("cannot record a payment against a VOID invoice")
    if inv["state"] == "PAID":
        raise ValidationError("invoice is already fully PAID")
    if amount <= 0:
        raise ValidationError("payment amount must be > 0")
    pid = schema.new_id()
    try:
        conn.execute(schema.payments.insert().values(
            id=pid, tenant_id=tid, invoice_id=inv["id"], provider=provider,
            provider_ref=provider_ref, amount=amount,
            currency=inv["currency"], method=method, received_at=received_at,
            idempotency_key=idempotency_key, created_by=actor))
    except IntegrityError:                       # concurrent identical submit
        row = conn.execute(select(schema.payments.c.id).where(
            (schema.payments.c.tenant_id == tid)
            & (schema.payments.c.idempotency_key == idempotency_key))).first()
        if row:
            return row[0], False, inv["state"]
        raise
    paid = sum((_money(r[0]) for r in conn.execute(
        select(schema.payments.c.amount).where(
            schema.payments.c.invoice_id == inv["id"]))), ZERO_MONEY)
    new_state = "PAID" if paid >= _money(inv["total"]) else "PARTIALLY_PAID"
    conn.execute(schema.invoices.update().where(
        schema.invoices.c.id == inv["id"]).values(
        amount_paid=paid, state=new_state, updated_at=schema.utcnow()))
    return pid, True, new_state


def record_payment(engine: Engine, ctx: TenantContext, *, invoice_id: str,
                   amount, idempotency_key: str, provider: str = "MANUAL",
                   provider_ref: Optional[str] = None,
                   method: Optional[str] = None,
                   received_at: Optional[datetime] = None) -> dict:
    """Record a payment against an invoice (manual entry path). Idempotent on
    ``(tenant, idempotency_key)``."""
    ctx.require("billing:manage")
    if not (idempotency_key or "").strip():
        raise ValidationError("record_payment requires an idempotency_key")
    amt = _money(amount)
    with engine.begin() as conn:
        inv = _load_invoice(conn, ctx, invoice_id)
        pid, created, state = _apply_payment(
            conn, inv, amount=amt, idempotency_key=idempotency_key,
            provider=provider, provider_ref=provider_ref, method=method,
            received_at=received_at or schema.utcnow(), actor=ctx.user_id)
        if created:
            audit.record(conn, ctx, action="payment.record",
                         resource_type="payment", resource_id=pid,
                         after={"invoice_id": invoice_id, "amount": str(amt),
                                "invoice_state": state},
                         tenant_id=inv["tenant_id"])
    return {"payment_id": pid, "created": created, "invoice_state": state}


# --------------------------------------------------------------------------
# Credit notes & dunning
# --------------------------------------------------------------------------
def issue_credit_note(engine: Engine, ctx: TenantContext, *, invoice_id: str,
                      amount, reason: str) -> str:
    ctx.require("billing:manage")
    amt = _money(amount)
    if amt <= 0:
        raise ValidationError("credit note amount must be > 0")
    if not (reason or "").strip():
        raise ValidationError("a credit note requires a reason")
    cid = schema.new_id()
    with engine.begin() as conn:
        inv = _load_invoice(conn, ctx, invoice_id)
        if amt > _money(inv["total"]):
            raise ValidationError("credit note exceeds invoice total")
        conn.execute(schema.credit_notes.insert().values(
            id=cid, tenant_id=inv["tenant_id"], invoice_id=invoice_id,
            amount=amt, currency=inv["currency"], reason=reason,
            created_by=ctx.user_id))
        audit.record(conn, ctx, action="credit_note.issue",
                     resource_type="credit_note", resource_id=cid,
                     after={"invoice_id": invoice_id, "amount": str(amt),
                            "reason": reason}, tenant_id=inv["tenant_id"])
    return cid


def record_dunning_attempt(engine: Engine, ctx: TenantContext, *,
                           invoice_id: str, step: int = 1,
                           channel: str = "EMAIL",
                           outcome: Optional[str] = None) -> str:
    ctx.require("billing:manage")
    did = schema.new_id()
    with engine.begin() as conn:
        inv = _load_invoice(conn, ctx, invoice_id)
        conn.execute(schema.dunning_attempts.insert().values(
            id=did, tenant_id=inv["tenant_id"], invoice_id=invoice_id,
            step=int(step), channel=channel, outcome=outcome))
    return did


def mark_overdue(engine: Engine, ctx: TenantContext, *,
                 as_of: Optional[datetime] = None) -> int:
    """Flip ISSUED/PARTIALLY_PAID invoices past due to OVERDUE. Returns count."""
    ctx.require("billing:manage")
    now = as_of or schema.utcnow()
    scope = None if ctx.tenant_id == schema.GLOBAL_TENANT else ctx.tenant_id
    with engine.begin() as conn:
        q = select(schema.invoices).where(
            (schema.invoices.c.state.in_(("ISSUED", "PARTIALLY_PAID")))
            & (schema.invoices.c.due_at.isnot(None))
            & (schema.invoices.c.due_at < now))
        if scope:
            q = q.where(schema.invoices.c.tenant_id == scope)
        rows = conn.execute(q).mappings().all()
        for inv in rows:
            conn.execute(schema.invoices.update().where(
                schema.invoices.c.id == inv["id"]).values(
                state="OVERDUE", updated_at=schema.utcnow()))
    return len(rows)


def get_invoice(engine: Engine, ctx: TenantContext, invoice_id: str) -> dict:
    ctx.require("billing:read")
    with engine.connect() as conn:
        inv = _load_invoice(conn, ctx, invoice_id)
        lines = conn.execute(select(schema.invoice_lines).where(
            schema.invoice_lines.c.invoice_id == invoice_id)).mappings().all()
        pays = conn.execute(select(schema.payments).where(
            schema.payments.c.invoice_id == invoice_id)).mappings().all()
    return {"invoice": inv, "lines": [dict(x) for x in lines],
            "payments": [dict(x) for x in pays]}


# --------------------------------------------------------------------------
# Inbound provider webhooks — verified fail-closed, processed at most once
# --------------------------------------------------------------------------
def _webhook_secret(provider: str) -> str:
    """Server-side webhook secret from the environment — NEVER from the request
    (a caller-supplied secret is forgeable)."""
    return os.environ.get(f"{provider.upper()}_WEBHOOK_SECRET", "")


def ingest_webhook(engine: Engine, *, provider: str, payload: dict,
                   signature: str, raw_body: Optional[bytes] = None) -> dict:
    """Verify a provider webhook (fail-closed) and process it at most once.

    A PAYMENT_SUCCEEDED event carrying our ``invoice_id`` records an idempotent
    payment. Verification uses a server-side secret; an unset secret or a bad
    signature is rejected and recorded, never processed."""
    provider = (provider or "").upper()
    adapter = bp.get_provider(provider)
    body = raw_body if raw_body is not None else bp.canonical_body(payload)
    secret = _webhook_secret(provider)
    verified = adapter.verify_signature(body, signature, secret)

    if not verified:
        with engine.begin() as conn:
            conn.execute(schema.billing_webhook_events.insert().values(
                id=schema.new_id(), provider=provider,
                external_id=(payload.get("id") or schema.new_id()),
                event_type=payload.get("type") or payload.get("event"),
                payload=payload, signature_verified=False, processed=False,
                error="signature verification failed"))
        raise ValidationError("webhook signature verification failed")

    event = adapter.parse_event(payload)
    external_id = event.get("external_id") or ""
    with engine.begin() as conn:
        dup = conn.execute(select(schema.billing_webhook_events).where(
            (schema.billing_webhook_events.c.provider == provider)
            & (schema.billing_webhook_events.c.external_id == external_id))) \
            .mappings().first()
        if dup and dup["processed"]:
            return {"status": "DUPLICATE", "external_id": external_id}
        wid = dup["id"] if dup else schema.new_id()
        if not dup:
            conn.execute(schema.billing_webhook_events.insert().values(
                id=wid, provider=provider, external_id=external_id,
                event_type=event.get("event_type"), payload=payload,
                signature_verified=True, processed=False))

        result = {"status": "IGNORED", "external_id": external_id}
        if event.get("kind") == "PAYMENT_SUCCEEDED" and event.get("invoice_ref"):
            inv = conn.execute(select(schema.invoices).where(
                schema.invoices.c.id == event["invoice_ref"])) \
                .mappings().first()
            if inv is None:
                conn.execute(schema.billing_webhook_events.update().where(
                    schema.billing_webhook_events.c.id == wid).values(
                    processed=True, error="unknown invoice_ref"))
                return {"status": "NO_INVOICE", "external_id": external_id}
            amount = _money(event.get("amount") or "0")
            key = f"webhook:{provider}:{external_id}"
            pid, created, state = _apply_payment(
                conn, dict(inv), amount=amount, idempotency_key=key,
                provider=provider, provider_ref=event.get("provider_ref"),
                method="WEBHOOK", received_at=schema.utcnow(), actor="webhook")
            audit.record(conn, None, action="payment.webhook",
                         resource_type="payment", resource_id=pid,
                         after={"invoice_id": inv["id"], "amount": str(amount),
                                "provider": provider, "invoice_state": state,
                                "created": created},
                         tenant_id=inv["tenant_id"], actor_type="SYSTEM",
                         actor_id=f"webhook:{provider}")
            result = {"status": "PAYMENT_RECORDED", "external_id": external_id,
                      "payment_id": pid, "invoice_state": state,
                      "created": created}
        conn.execute(schema.billing_webhook_events.update().where(
            schema.billing_webhook_events.c.id == wid).values(processed=True))
    return result
