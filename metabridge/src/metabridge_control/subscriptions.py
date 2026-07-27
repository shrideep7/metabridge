"""Subscription & contract service — the commercial relationship lifecycle.

Implements the nine-state subscription machine
(docs/commercialization/03-domain-model.md §5.1): every transition is
validated against the legal-transition table, appends a
``subscription_transitions`` row, writes an audit event, and (where it changes
entitlements) refreshes the resolved entitlement set. Illegal transitions are
rejected at the aggregate.

Contracts carry references only (purchase-order ref, payment terms, document
refs) — no monetary amounts; pricing is a later phase.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine

from . import audit, entitlements as ent, schema
from .context import TenantContext, require_scoped
from .errors import NotFoundError, PlanImmutableError, ValidationError

# legal transitions (from_state -> {to_states})
TRANSITIONS = {
    "DRAFT": {"PENDING_ACTIVATION", "CANCELLED"},
    "PENDING_ACTIVATION": {"TRIALING", "ACTIVE"},
    "TRIALING": {"ACTIVE", "EXPIRED"},
    "ACTIVE": {"PAST_DUE", "CANCELLED", "EXPIRED", "TERMINATED"},
    "PAST_DUE": {"ACTIVE", "SUSPENDED"},
    "SUSPENDED": {"ACTIVE", "TERMINATED"},
    "CANCELLED": {"ACTIVE", "EXPIRED"},
    "EXPIRED": set(),
    "TERMINATED": set(),
}
TERMINAL = {"EXPIRED", "TERMINATED"}
# entering these (re)resolves entitlements
_RESOLVE_ON = {"TRIALING", "ACTIVE"}
_BILLING_MONTHS = {"MONTHLY": 1, "QUARTERLY": 3, "ANNUAL": 12}


def _term_end(start, billing_period):
    months = _BILLING_MONTHS.get(billing_period, 12)
    # naive month math good enough for term windows
    y, m = start.year, start.month + months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    day = min(start.day, 28)
    return start.replace(year=y, month=m, day=day)


# ------------------------------------------------------------- accounts
def create_customer_account(engine: Engine, ctx: TenantContext, *, name: str,
                            billing_email: Optional[str] = None,
                            currency: Optional[str] = None) -> str:
    ctx.require("subscriptions:manage")
    if not (name or "").strip():
        raise ValidationError("account name is required")
    if currency and len(currency) != 3:
        raise ValidationError("currency must be a 3-letter ISO code")
    aid = schema.new_id()
    with engine.begin() as conn:
        conn.execute(schema.customer_accounts.insert().values(
            id=aid, tenant_id=ctx.tenant_id, name=name.strip(),
            billing_email=billing_email, currency=(currency or None),
            created_by=ctx.user_id))
        audit.record(conn, ctx, action="account.create",
                     resource_type="customer_account", resource_id=aid,
                     after={"name": name})
    return aid


# ------------------------------------------------------------- contracts
def create_contract(engine: Engine, ctx: TenantContext, account_id: str, *,
                    purchase_order_ref: Optional[str] = None,
                    payment_terms_days: Optional[int] = None,
                    governing_law: Optional[str] = None,
                    document_refs: Optional[dict] = None) -> str:
    ctx.require("subscriptions:manage")
    cid = schema.new_id()
    with engine.begin() as conn:
        require_scoped(conn, schema.customer_accounts, ctx, account_id)
        conn.execute(schema.contracts.insert().values(
            id=cid, tenant_id=ctx.tenant_id, customer_account_id=account_id,
            state="DRAFT", purchase_order_ref=purchase_order_ref,
            payment_terms_days=payment_terms_days, governing_law=governing_law,
            document_refs=document_refs, created_by=ctx.user_id))
        audit.record(conn, ctx, action="contract.create",
                     resource_type="contract", resource_id=cid,
                     after={"account_id": account_id,
                            "purchase_order_ref": purchase_order_ref})
    return cid


def execute_contract(engine: Engine, ctx: TenantContext,
                     contract_id: str) -> None:
    ctx.require("subscriptions:manage")
    with engine.begin() as conn:
        row = require_scoped(conn, schema.contracts, ctx, contract_id)
        if row["state"] != "DRAFT":
            raise ValidationError("only DRAFT contracts can be executed")
        conn.execute(schema.contracts.update()
                     .where(schema.contracts.c.id == contract_id)
                     .values(state="EXECUTED", executed_at=schema.utcnow(),
                             updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="contract.execute",
                     resource_type="contract", resource_id=contract_id,
                     before={"state": "DRAFT"}, after={"state": "EXECUTED"})


# --------------------------------------------------------- subscriptions
def create_subscription(engine: Engine, ctx: TenantContext, *,
                        account_id: str, plan_version_id: str,
                        contract_id: Optional[str] = None,
                        billing_period: str = "ANNUAL",
                        auto_renew: bool = True) -> str:
    """Create a DRAFT subscription pinned to a PUBLISHED plan version."""
    ctx.require("subscriptions:manage")
    if billing_period not in _BILLING_MONTHS:
        raise ValidationError(f"invalid billing period: {billing_period}")
    sid = schema.new_id()
    with engine.begin() as conn:
        require_scoped(conn, schema.customer_accounts, ctx, account_id)
        if contract_id:
            require_scoped(conn, schema.contracts, ctx, contract_id)
        pv = conn.execute(select(schema.plan_versions).where(
            schema.plan_versions.c.id == plan_version_id)).mappings().first()
        if pv is None:
            raise NotFoundError(f"plan_versions:{plan_version_id}")
        if pv["status"] != "PUBLISHED":
            raise PlanImmutableError(
                "subscriptions may only pin a PUBLISHED plan version")
        conn.execute(schema.subscriptions.insert().values(
            id=sid, tenant_id=ctx.tenant_id, customer_account_id=account_id,
            contract_id=contract_id, plan_version_id=plan_version_id,
            state="DRAFT", billing_period=billing_period,
            auto_renew=auto_renew, version=1, created_by=ctx.user_id))
        conn.execute(schema.subscription_transitions.insert().values(
            id=schema.new_id(), tenant_id=ctx.tenant_id, subscription_id=sid,
            from_state=None, to_state="DRAFT", at=schema.utcnow(),
            actor=ctx.user_id, reason="created"))
        audit.record(conn, ctx, action="subscription.create",
                     resource_type="subscription", resource_id=sid,
                     after={"plan_version_id": plan_version_id,
                            "billing_period": billing_period})
    return sid


def _transition(engine: Engine, ctx: TenantContext, subscription_id: str,
                to_state: str, *, reason: str = "",
                mutate: Optional[dict] = None,
                resolve: bool = False) -> None:
    ctx.require("subscriptions:manage")
    with engine.begin() as conn:
        sub = require_scoped(conn, schema.subscriptions, ctx, subscription_id)
        frm = sub["state"]
        if to_state not in TRANSITIONS.get(frm, set()):
            raise ValidationError(
                f"illegal transition {frm} -> {to_state}")
        values = {"state": to_state, "version": sub["version"] + 1,
                  "updated_at": schema.utcnow()}
        values.update(mutate or {})
        # optimistic compare-and-set: the version guard makes concurrent
        # transitions from the same base state collide (one gets rowcount 0).
        res = conn.execute(schema.subscriptions.update()
                           .where((schema.subscriptions.c.id == subscription_id)
                                  & (schema.subscriptions.c.version
                                     == sub["version"]))
                           .values(**values))
        if res.rowcount != 1:
            raise ValidationError("subscription was modified concurrently")
        conn.execute(schema.subscription_transitions.insert().values(
            id=schema.new_id(), tenant_id=ctx.tenant_id,
            subscription_id=subscription_id, from_state=frm, to_state=to_state,
            at=schema.utcnow(), actor=ctx.user_id, reason=reason))
        audit.record(conn, ctx, action="subscription.transition",
                     resource_type="subscription", resource_id=subscription_id,
                     before={"state": frm}, after={"state": to_state,
                                                   "reason": reason})
        # re-resolve entitlements in the SAME transaction so state and
        # entitlements commit or roll back together (Phase-2 review HIGH).
        if resolve:
            ent.resolve_in_conn(conn, subscription_id, actor=ctx.user_id)


def activate(engine: Engine, ctx: TenantContext, subscription_id: str, *,
             reason: str = "activation") -> None:
    """DRAFT->PENDING_ACTIVATION->ACTIVE with a term window; resolves
    entitlements."""
    with engine.connect() as conn:
        sub = require_scoped(conn, schema.subscriptions, ctx, subscription_id)
    if sub["state"] == "DRAFT":
        _transition(engine, ctx, subscription_id, "PENDING_ACTIVATION",
                    reason=reason)
    now = schema.utcnow()
    _transition(engine, ctx, subscription_id, "ACTIVE", reason=reason,
                mutate={"term_start": now,
                        "term_end": _term_end(now, sub["billing_period"]),
                        "is_trial": False, "trial_end": None},
                resolve=True)


def start_trial(engine: Engine, ctx: TenantContext, subscription_id: str, *,
                days: int = 14, reason: str = "trial") -> None:
    with engine.connect() as conn:
        sub = require_scoped(conn, schema.subscriptions, ctx, subscription_id)
    if sub["state"] == "DRAFT":
        _transition(engine, ctx, subscription_id, "PENDING_ACTIVATION",
                    reason=reason)
    now = schema.utcnow()
    _transition(engine, ctx, subscription_id, "TRIALING", reason=reason,
                mutate={"is_trial": True, "trial_end": now + timedelta(days=days),
                        "term_start": now,
                        "term_end": now + timedelta(days=days)},
                resolve=True)


def convert_trial(engine: Engine, ctx: TenantContext, subscription_id: str,
                  *, reason: str = "trial conversion") -> None:
    with engine.connect() as conn:
        sub = require_scoped(conn, schema.subscriptions, ctx, subscription_id)
    now = schema.utcnow()
    _transition(engine, ctx, subscription_id, "ACTIVE", reason=reason,
                mutate={"is_trial": False, "trial_end": None,
                        "term_start": now,
                        "term_end": _term_end(now, sub["billing_period"])},
                resolve=True)


def mark_past_due(engine, ctx, subscription_id, *, grace_days: int = 14,
                  reason: str = "invoice overdue") -> None:
    _transition(engine, ctx, subscription_id, "PAST_DUE", reason=reason,
                mutate={"grace_until": schema.utcnow()
                        + timedelta(days=grace_days)})


def reinstate(engine, ctx, subscription_id, *, reason: str = "payment received"):
    _transition(engine, ctx, subscription_id, "ACTIVE", reason=reason,
                mutate={"grace_until": None}, resolve=True)


def suspend(engine, ctx, subscription_id, *, reason: str = "dunning exhausted"):
    _transition(engine, ctx, subscription_id, "SUSPENDED", reason=reason)


def cancel(engine, ctx, subscription_id, *, reason: str = "customer notice"):
    """ACTIVE->CANCELLED; entitlements remain until term end."""
    with engine.connect() as conn:
        sub = require_scoped(conn, schema.subscriptions, ctx, subscription_id)
    _transition(engine, ctx, subscription_id, "CANCELLED", reason=reason,
                mutate={"cancel_effective_at": sub["term_end"],
                        "auto_renew": False})


def rescind_cancellation(engine, ctx, subscription_id, *,
                         reason: str = "notice rescinded"):
    _transition(engine, ctx, subscription_id, "ACTIVE", reason=reason,
                mutate={"cancel_effective_at": None}, resolve=True)


def expire(engine, ctx, subscription_id, *, reason: str = "term end"):
    _transition(engine, ctx, subscription_id, "EXPIRED", reason=reason)


def terminate(engine, ctx, subscription_id, *, reason: str = "for cause"):
    _transition(engine, ctx, subscription_id, "TERMINATED", reason=reason)


# --------------------------------------------------------- items & overrides
def add_item(engine: Engine, ctx: TenantContext, subscription_id: str, *,
             kind: str, ref_code: str, quantity: int,
             committed_quantity: Optional[int] = None) -> str:
    ctx.require("subscriptions:manage")
    if kind not in ("SEAT", "ADDON", "METER"):
        raise ValidationError(f"invalid item kind: {kind}")
    if quantity < 0:
        raise ValidationError("quantity must be >= 0")
    iid = schema.new_id()
    with engine.begin() as conn:
        require_scoped(conn, schema.subscriptions, ctx, subscription_id)
        dup = conn.execute(select(schema.subscription_items.c.id).where(
            (schema.subscription_items.c.subscription_id == subscription_id)
            & (schema.subscription_items.c.ref_code == ref_code))).first()
        if dup:
            raise ValidationError(f"item already exists: {ref_code}")
        conn.execute(schema.subscription_items.insert().values(
            id=iid, tenant_id=ctx.tenant_id, subscription_id=subscription_id,
            kind=kind, ref_code=ref_code, quantity=quantity,
            committed_quantity=committed_quantity, created_by=ctx.user_id))
        audit.record(conn, ctx, action="subscription.add_item",
                     resource_type="subscription_item", resource_id=iid,
                     after={"kind": kind, "ref_code": ref_code,
                            "quantity": quantity})
    return iid


def add_override(engine: Engine, staff_ctx: TenantContext, subscription_id: str,
                 *, code: str, value: dict, reason: str,
                 value_kind: Optional[str] = None,
                 period: Optional[str] = None,
                 expires_at=None) -> str:
    """HIGH-RISK: grant a contractual entitlement exception. Requires a staff
    context with ``entitlements:override``, a mandatory reason, and is fully
    audited (before/after). Re-resolves entitlements so the override takes
    effect immediately."""
    staff_ctx.require("entitlements:override")
    if not (reason or "").strip():
        raise ValidationError("an override requires a reason")
    vk = value_kind or ent.value_kind_for(code)
    if vk not in ent.VALUE_KINDS:
        raise ValidationError(f"invalid value kind: {vk}")
    oid = schema.new_id()
    with engine.begin() as conn:
        sub = conn.execute(select(schema.subscriptions).where(
            schema.subscriptions.c.id == subscription_id)).mappings().first()
        if sub is None:
            raise NotFoundError(f"subscriptions:{subscription_id}")
        prior = conn.execute(select(schema.entitlements).where(
            (schema.entitlements.c.subscription_id == subscription_id)
            & (schema.entitlements.c.code == code))).mappings().first()
        conn.execute(schema.entitlement_overrides.insert().values(
            id=oid, tenant_id=sub["tenant_id"],
            subscription_id=subscription_id, code=code, value_kind=vk,
            value=value, period=period, approved_by=staff_ctx.user_id,
            reason=reason, expires_at=expires_at, active=True,
            created_by=staff_ctx.user_id))
        audit.record(conn, staff_ctx, action="entitlement.override",
                     resource_type="subscription", resource_id=subscription_id,
                     before={"code": code,
                             "prior": dict(prior) if prior else None},
                     after={"code": code, "value": value, "value_kind": vk},
                     reason=reason, tenant_id=sub["tenant_id"])
        # apply the override in the same transaction as its audit record
        ent.resolve_in_conn(conn, subscription_id, actor=staff_ctx.user_id)
    return oid


def get_subscription(engine: Engine, ctx: TenantContext,
                     subscription_id: str) -> dict:
    with engine.connect() as conn:
        sub = require_scoped(conn, schema.subscriptions, ctx, subscription_id)
        items = conn.execute(select(schema.subscription_items).where(
            schema.subscription_items.c.subscription_id == subscription_id)) \
            .mappings().all()
    out = dict(sub)
    out["items"] = [dict(i) for i in items]
    return out


def list_transitions(engine: Engine, subscription_id: str) -> list:
    with engine.connect() as conn:
        rows = conn.execute(select(schema.subscription_transitions).where(
            schema.subscription_transitions.c.subscription_id
            == subscription_id).order_by(
            schema.subscription_transitions.c.at))
        return [dict(r) for r in rows.mappings()]
