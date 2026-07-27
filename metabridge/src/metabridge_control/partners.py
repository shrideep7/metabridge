"""Partner & commission engine (docs/commercialization/03-domain-model.md §9).

The SI go-to-market layer: a partner registry with a tier ladder, signed
agreements, deal registration with protection windows, and a commission engine
whose correctness is the point.

Two disciplines carried from elsewhere in the platform:

- **Segregation of duties.** The commercial admin *accrues* commissions; a
  *different* role (finance) approves, marks payable, and pays them — enforced
  by permissions (``commissions:manage`` vs ``commissions:approve``).
- **No silent netting.** A reversal or clawback is an explicit compensating
  ``Commission`` row (``clawback_of_id``), never a mutation of the original —
  the same append-only discipline as usage adjustments (§7).

Payability is gated on *real billed revenue*: ``mark_payable`` verifies the
underlying Phase-4 ``Invoice`` is PAID, the clawback window has elapsed since
payment, the agreement is ACTIVE with banking/tax verified, the deal was
APPROVED, and there is no open credit note — all five §9.3 preconditions.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine

from . import audit, schema
from .context import TenantContext
from .errors import NotFoundError, ValidationError
from .schema import MONEY_SCALE, ZERO_MONEY

PARTNER_KINDS = ("SI", "OEM", "RESELLER", "REFERRAL")
COMMISSION_BASES = ("FIRST_YEAR", "ALL_INVOICED")

_DEAL_TX = {
    "SUBMITTED": {"APPROVED", "REJECTED"},
    "APPROVED": {"WON", "LOST", "EXPIRED"},
}
_COMMISSION_TX = {
    "ACCRUED": {"APPROVED_C", "REVERSED"},
    "APPROVED_C": {"PAYABLE", "REVERSED"},
    "PAYABLE": {"PAID"},
    "PAID": {"CLAWED_BACK"},
}


def _money(v) -> Decimal:
    if isinstance(v, float):
        raise ValidationError("money must be a string/Decimal, not float")
    return (v if isinstance(v, Decimal) else Decimal(str(v or 0))) \
        .quantize(MONEY_SCALE)


def _row(conn: Connection, table, row_id: str) -> dict:
    r = conn.execute(select(table).where(table.c.id == row_id)) \
        .mappings().first()
    if r is None:
        raise NotFoundError(f"{table.name}:{row_id}")
    return dict(r)


# --------------------------------------------------------------- partners
def create_partner(engine: Engine, ctx: TenantContext, *, kind: str, name: str,
                   country_code: Optional[str] = None,
                   tier_code: str = "REGISTERED",
                   partner_tenant_id: Optional[str] = None) -> str:
    ctx.require("partners:manage")
    if kind not in PARTNER_KINDS:
        raise ValidationError(f"invalid partner kind: {kind}")
    pid = schema.new_id()
    with engine.begin() as conn:
        if tier_code and conn.execute(select(schema.partner_tiers.c.code).where(
                schema.partner_tiers.c.code == tier_code)).first() is None:
            raise ValidationError(f"unknown tier: {tier_code}")
        conn.execute(schema.partners.insert().values(
            id=pid, kind=kind, name=name, status="ACTIVE",
            country_code=country_code, tier_code=tier_code,
            partner_tenant_id=partner_tenant_id, created_by=ctx.user_id))
        if tier_code:
            conn.execute(schema.partner_tier_assignments.insert().values(
                id=schema.new_id(), partner_id=pid, tier_code=tier_code,
                effective_from=schema.utcnow(), created_by=ctx.user_id))
        audit.record(conn, ctx, action="partner.create",
                     resource_type="partner", resource_id=pid,
                     after={"kind": kind, "name": name, "tier": tier_code},
                     tenant_id=schema.GLOBAL_TENANT)
    return pid


def assign_tier(engine: Engine, ctx: TenantContext, partner_id: str, *,
                tier_code: str, review_ref: Optional[str] = None) -> None:
    """Effective-dated tier change (§9.1). Staff-driven; never automatic, so a
    partner is never demoted mid-agreement by a background job."""
    ctx.require("partners:manage")
    now = schema.utcnow()
    with engine.begin() as conn:
        _row(conn, schema.partners, partner_id)
        if conn.execute(select(schema.partner_tiers.c.code).where(
                schema.partner_tiers.c.code == tier_code)).first() is None:
            raise ValidationError(f"unknown tier: {tier_code}")
        # close the open assignment, open a new one
        conn.execute(schema.partner_tier_assignments.update().where(
            (schema.partner_tier_assignments.c.partner_id == partner_id)
            & (schema.partner_tier_assignments.c.effective_to.is_(None)))
            .values(effective_to=now))
        conn.execute(schema.partner_tier_assignments.insert().values(
            id=schema.new_id(), partner_id=partner_id, tier_code=tier_code,
            effective_from=now, review_ref=review_ref, created_by=ctx.user_id))
        conn.execute(schema.partners.update().where(
            schema.partners.c.id == partner_id).values(
            tier_code=tier_code, updated_at=now))
        audit.record(conn, ctx, action="partner.tier.assign",
                     resource_type="partner", resource_id=partner_id,
                     after={"tier": tier_code}, tenant_id=schema.GLOBAL_TENANT)


# --------------------------------------------------------------- plans/agreements
def create_commission_plan(engine: Engine, ctx: TenantContext, *, code: str,
                           basis: str, rate_table: dict,
                           clawback_window_days: int = 90) -> str:
    ctx.require("partners:manage")
    if basis not in COMMISSION_BASES:
        raise ValidationError(f"invalid basis: {basis}")
    if not isinstance(rate_table, dict) or not rate_table:
        raise ValidationError("rate_table must be a non-empty object")
    pid = schema.new_id()
    with engine.begin() as conn:
        conn.execute(schema.commission_plans.insert().values(
            id=pid, code=code, basis=basis, rate_table=rate_table,
            clawback_window_days=int(clawback_window_days),
            created_by=ctx.user_id))
        audit.record(conn, ctx, action="commission_plan.create",
                     resource_type="commission_plan", resource_id=pid,
                     after={"code": code, "basis": basis},
                     tenant_id=schema.GLOBAL_TENANT)
    return pid


def create_agreement(engine: Engine, ctx: TenantContext, partner_id: str, *,
                     commission_plan_id: Optional[str] = None) -> str:
    ctx.require("partners:manage")
    aid = schema.new_id()
    with engine.begin() as conn:
        _row(conn, schema.partners, partner_id)
        if commission_plan_id:
            _row(conn, schema.commission_plans, commission_plan_id)
        conn.execute(schema.partner_agreements.insert().values(
            id=aid, partner_id=partner_id, state="DRAFT",
            commission_plan_id=commission_plan_id, created_by=ctx.user_id))
        audit.record(conn, ctx, action="partner_agreement.create",
                     resource_type="partner_agreement", resource_id=aid,
                     after={"partner_id": partner_id},
                     tenant_id=schema.GLOBAL_TENANT)
    return aid


def verify_agreement(engine: Engine, ctx: TenantContext, agreement_id: str, *,
                     banking: Optional[bool] = None,
                     tax_docs: Optional[bool] = None) -> None:
    ctx.require("partners:manage")
    patch = {}
    if banking is not None:
        patch["banking_verified"] = bool(banking)
    if tax_docs is not None:
        patch["tax_docs_verified"] = bool(tax_docs)
    if not patch:
        raise ValidationError("nothing to verify")
    with engine.begin() as conn:
        _row(conn, schema.partner_agreements, agreement_id)
        patch["updated_at"] = schema.utcnow()
        conn.execute(schema.partner_agreements.update().where(
            schema.partner_agreements.c.id == agreement_id).values(**patch))
        audit.record(conn, ctx, action="partner_agreement.verify",
                     resource_type="partner_agreement", resource_id=agreement_id,
                     after=patch, tenant_id=schema.GLOBAL_TENANT)


def activate_agreement(engine: Engine, ctx: TenantContext,
                       agreement_id: str) -> None:
    """DRAFT/SUSPENDED -> ACTIVE. Requires banking + tax verified (an agreement
    can't be active — and thus commissions can't be paid — without them)."""
    ctx.require("partners:manage")
    with engine.begin() as conn:
        ag = _row(conn, schema.partner_agreements, agreement_id)
        if not (ag["banking_verified"] and ag["tax_docs_verified"]):
            raise ValidationError(
                "cannot activate: banking and tax docs must be verified first")
        conn.execute(schema.partner_agreements.update().where(
            schema.partner_agreements.c.id == agreement_id).values(
            state="ACTIVE", effective_from=ag["effective_from"]
            or schema.utcnow(), updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="partner_agreement.activate",
                     resource_type="partner_agreement", resource_id=agreement_id,
                     before={"state": ag["state"]}, after={"state": "ACTIVE"},
                     tenant_id=schema.GLOBAL_TENANT)


# --------------------------------------------------------------- deals (§9.2)
def register_deal(engine: Engine, ctx: TenantContext, partner_id: str, *,
                  prospect_name: str, estimated_value=None,
                  currency: str = "USD",
                  customer_tenant_id: Optional[str] = None) -> str:
    ctx.require("partners:manage")
    if not (prospect_name or "").strip():
        raise ValidationError("prospect_name is required")
    did = schema.new_id()
    with engine.begin() as conn:
        _row(conn, schema.partners, partner_id)
        conn.execute(schema.deal_registrations.insert().values(
            id=did, partner_id=partner_id, prospect_name=prospect_name,
            customer_tenant_id=customer_tenant_id,
            estimated_value=(None if estimated_value is None
                             else _money(estimated_value)),
            currency=currency, state="SUBMITTED", created_by=ctx.user_id))
        audit.record(conn, ctx, action="deal.register",
                     resource_type="deal_registration", resource_id=did,
                     after={"partner_id": partner_id,
                            "prospect": prospect_name},
                     tenant_id=schema.GLOBAL_TENANT)
    return did


def _deal_transition(engine: Engine, ctx: TenantContext, deal_id: str,
                     to_state: str, *, action: str, extra: Optional[dict] = None,
                     order_ref: Optional[str] = None,
                     protection_days: Optional[int] = None) -> None:
    ctx.require("partners:manage")
    with engine.begin() as conn:
        d = _row(conn, schema.deal_registrations, deal_id)
        if to_state not in _DEAL_TX.get(d["state"], set()):
            raise ValidationError(
                f"illegal deal transition {d['state']} -> {to_state}")
        values = {"state": to_state, "updated_at": schema.utcnow()}
        if to_state == "APPROVED":
            days = protection_days
            if days is None:                   # default from the partner's tier
                p = _row(conn, schema.partners, d["partner_id"])
                tier = conn.execute(select(schema.partner_tiers.c.benefits)
                                    .where(schema.partner_tiers.c.code
                                           == p["tier_code"])).first()
                days = int((tier[0] or {}).get("deal_protection_days", 30)) \
                    if tier else 30
            values["protection_expires_at"] = schema.utcnow() + timedelta(
                days=int(days))
        if order_ref is not None:
            values["order_ref"] = order_ref
        conn.execute(schema.deal_registrations.update().where(
            schema.deal_registrations.c.id == deal_id).values(**values))
        audit.record(conn, ctx, action=action,
                     resource_type="deal_registration", resource_id=deal_id,
                     before={"state": d["state"]},
                     after={"state": to_state, **(extra or {})},
                     tenant_id=schema.GLOBAL_TENANT)


def approve_deal(engine, ctx, deal_id, *, protection_days=None):
    _deal_transition(engine, ctx, deal_id, "APPROVED", action="deal.approve",
                     protection_days=protection_days)


def reject_deal(engine, ctx, deal_id, *, reason=""):
    _deal_transition(engine, ctx, deal_id, "REJECTED", action="deal.reject",
                     extra={"reason": reason})


def win_deal(engine, ctx, deal_id, *, order_ref):
    _deal_transition(engine, ctx, deal_id, "WON", action="deal.win",
                     order_ref=order_ref, extra={"order_ref": order_ref})


def lose_deal(engine, ctx, deal_id):
    _deal_transition(engine, ctx, deal_id, "LOST", action="deal.lose")


def expire_deal(engine, ctx, deal_id):
    _deal_transition(engine, ctx, deal_id, "EXPIRED", action="deal.expire")


# --------------------------------------------------------------- commissions (§9.3)
def compute_commission(rate_table: dict, tier_code: str, kind: str,
                       basis_amount) -> Decimal:
    """rate_table is ``{tier: pct}`` or ``{tier: {kind: pct}}`` (``*`` wildcard
    allowed at either level). Commission = basis_amount x pct%."""
    entry = (rate_table or {}).get(tier_code, (rate_table or {}).get("*", 0))
    if isinstance(entry, dict):
        pct = entry.get(kind, entry.get("*", 0))
    else:
        pct = entry
    return (_money(basis_amount) * Decimal(str(pct)) / Decimal("100")) \
        .quantize(MONEY_SCALE)


def accrue_commission(engine: Engine, ctx: TenantContext, *, partner_id: str,
                      agreement_id: str, basis_amount, currency: str = "USD",
                      invoice_id: Optional[str] = None,
                      deal_id: Optional[str] = None,
                      order_ref: Optional[str] = None,
                      customer_tenant_id: Optional[str] = None) -> str:
    """Accrue a commission from billed revenue (ACCRUED). Amount is computed
    from the agreement's plan rate table and the partner's current tier."""
    ctx.require("commissions:manage")
    cid = schema.new_id()
    with engine.begin() as conn:
        partner = _row(conn, schema.partners, partner_id)
        ag = _row(conn, schema.partner_agreements, agreement_id)
        if ag["partner_id"] != partner_id:
            raise ValidationError("agreement does not belong to this partner")
        if not ag["commission_plan_id"]:
            raise ValidationError("agreement has no commission plan")
        plan = _row(conn, schema.commission_plans, ag["commission_plan_id"])
        amount = compute_commission(plan["rate_table"], partner["tier_code"],
                                    partner["kind"], basis_amount)
        conn.execute(schema.commissions.insert().values(
            id=cid, partner_id=partner_id, agreement_id=agreement_id,
            deal_id=deal_id, invoice_id=invoice_id, order_ref=order_ref,
            customer_tenant_id=customer_tenant_id,
            basis_amount=_money(basis_amount), amount=amount, currency=currency,
            state="ACCRUED", created_by=ctx.user_id))
        audit.record(conn, ctx, action="commission.accrue",
                     resource_type="commission", resource_id=cid,
                     after={"partner_id": partner_id, "amount": str(amount),
                            "invoice_id": invoice_id},
                     tenant_id=schema.GLOBAL_TENANT)
    return cid


def approve_commission(engine: Engine, ctx: TenantContext,
                       commission_id: str) -> None:
    """ACCRUED -> APPROVED_C (finance review). Requires ``commissions:approve``
    — a different party than the accruer (SoD)."""
    ctx.require("commissions:approve")
    _commission_set_state(engine, ctx, commission_id, "APPROVED_C",
                          action="commission.approve")


def _payability_reason(conn: Connection, comm: dict) -> Optional[str]:
    """Return None if all five §9.3 preconditions hold, else the failing one."""
    ag = conn.execute(select(schema.partner_agreements).where(
        schema.partner_agreements.c.id == comm["agreement_id"])) \
        .mappings().first()
    if ag is None or ag["state"] != "ACTIVE" or not ag["banking_verified"] \
            or not ag["tax_docs_verified"]:
        return "AGREEMENT_NOT_ACTIVE_OR_UNVERIFIED"          # precondition 3
    if comm["deal_id"]:
        deal = conn.execute(select(schema.deal_registrations.c.state).where(
            schema.deal_registrations.c.id == comm["deal_id"])).first()
        if deal is None or deal[0] not in ("APPROVED", "WON"):
            return "DEAL_NOT_APPROVED"                        # precondition 4
    if not comm["invoice_id"]:
        return "NO_INVOICE"
    inv = conn.execute(select(schema.invoices).where(
        schema.invoices.c.id == comm["invoice_id"])).mappings().first()
    if inv is None or inv["state"] != "PAID":
        return "INVOICE_NOT_PAID"                             # precondition 1
    credit = conn.execute(select(func.count()).select_from(schema.credit_notes)
                          .where(schema.credit_notes.c.invoice_id
                                 == comm["invoice_id"])).scalar_one()
    if credit:
        return "OPEN_CREDIT_NOTE"                             # precondition 5
    paid_at = conn.execute(select(func.max(schema.payments.c.received_at))
                           .where(schema.payments.c.invoice_id
                                  == comm["invoice_id"])).scalar()
    if paid_at is None:
        return "NO_PAYMENT_RECORD"
    plan = conn.execute(select(schema.commission_plans.c.clawback_window_days)
                        .where(schema.commission_plans.c.id
                               == ag["commission_plan_id"])).first()
    window = int(plan[0]) if plan else 0
    if schema.utcnow() < paid_at + timedelta(days=window):
        return "CLAWBACK_WINDOW_OPEN"                         # precondition 2
    return None


def mark_payable(engine: Engine, ctx: TenantContext,
                 commission_id: str) -> None:
    """APPROVED_C -> PAYABLE, gated on all five payability preconditions checked
    against real billed revenue. Raises ValidationError naming the failing
    precondition; never advances a commission that isn't truly payable."""
    ctx.require("commissions:approve")
    with engine.begin() as conn:
        comm = _row(conn, schema.commissions, commission_id)
        if "PAYABLE" not in _COMMISSION_TX.get(comm["state"], set()):
            raise ValidationError(
                f"illegal commission transition {comm['state']} -> PAYABLE")
        reason = _payability_reason(conn, comm)
        if reason is not None:
            raise ValidationError(f"not payable: {reason}")
        conn.execute(schema.commissions.update().where(
            schema.commissions.c.id == commission_id).values(
            state="PAYABLE", updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="commission.payable",
                     resource_type="commission", resource_id=commission_id,
                     before={"state": comm["state"]}, after={"state": "PAYABLE"},
                     tenant_id=schema.GLOBAL_TENANT)


def pay_partner(engine: Engine, ctx: TenantContext, partner_id: str, *,
                provider_ref: Optional[str] = None) -> dict:
    """Bundle all PAYABLE commissions for a partner into a PayoutBatch, execute
    it, and mark them PAID. Returns the batch summary."""
    ctx.require("commissions:approve")
    bid = schema.new_id()
    now = schema.utcnow()
    with engine.begin() as conn:
        rows = conn.execute(select(schema.commissions).where(
            (schema.commissions.c.partner_id == partner_id)
            & (schema.commissions.c.state == "PAYABLE"))).mappings().all()
        if not rows:
            raise ValidationError("no payable commissions for this partner")
        total = sum((_money(r["amount"]) for r in rows), ZERO_MONEY)
        currency = rows[0]["currency"]
        conn.execute(schema.payout_batches.insert().values(
            id=bid, partner_id=partner_id, total=total, currency=currency,
            state="EXECUTED", executed_at=now, provider_ref=provider_ref,
            created_by=ctx.user_id))
        for r in rows:
            conn.execute(schema.commissions.update().where(
                schema.commissions.c.id == r["id"]).values(
                state="PAID", payout_batch_id=bid, paid_at=now, updated_at=now))
        audit.record(conn, ctx, action="commission.payout",
                     resource_type="payout_batch", resource_id=bid,
                     after={"partner_id": partner_id, "total": str(total),
                            "count": len(rows)}, tenant_id=schema.GLOBAL_TENANT)
    return {"payout_batch_id": bid, "total": total, "currency": currency,
            "commissions": len(rows)}


def reverse_commission(engine: Engine, ctx: TenantContext, commission_id: str, *,
                       reason: str) -> str:
    """Reverse an ACCRUED/APPROVED_C commission (order cancelled / invoice
    voided). Writes a compensating negative row and marks the original
    REVERSED — never a silent netting."""
    ctx.require("commissions:manage")
    return _compensate(engine, ctx, commission_id, terminal="REVERSED",
                       allowed_from={"ACCRUED", "APPROVED_C"},
                       action="commission.reverse", reason=reason)


def clawback_commission(engine: Engine, ctx: TenantContext, commission_id: str,
                        *, reason: str) -> str:
    """Claw back a PAID commission (refund inside the clawback window). Writes a
    compensating negative row and marks the original CLAWED_BACK. Finance-only."""
    ctx.require("commissions:approve")
    return _compensate(engine, ctx, commission_id, terminal="CLAWED_BACK",
                       allowed_from={"PAID"}, action="commission.clawback",
                       reason=reason)


def _compensate(engine: Engine, ctx: TenantContext, commission_id: str, *,
                terminal: str, allowed_from: set, action: str,
                reason: str) -> str:
    if not (reason or "").strip():
        raise ValidationError("a reason is required")
    comp_id = schema.new_id()
    with engine.begin() as conn:
        comm = _row(conn, schema.commissions, commission_id)
        if comm["state"] not in allowed_from:
            raise ValidationError(
                f"cannot {action.split('.')[-1]} a {comm['state']} commission")
        if terminal not in _COMMISSION_TX.get(comm["state"], set()):
            raise ValidationError(
                f"illegal transition {comm['state']} -> {terminal}")
        conn.execute(schema.commissions.update().where(
            schema.commissions.c.id == commission_id).values(
            state=terminal, updated_at=schema.utcnow()))
        # explicit compensating row (mirrors the metering adjustment discipline)
        conn.execute(schema.commissions.insert().values(
            id=comp_id, partner_id=comm["partner_id"],
            agreement_id=comm["agreement_id"], deal_id=comm["deal_id"],
            invoice_id=comm["invoice_id"], order_ref=comm["order_ref"],
            customer_tenant_id=comm["customer_tenant_id"],
            basis_amount=ZERO_MONEY, amount=-_money(comm["amount"]),
            currency=comm["currency"], state=terminal,
            clawback_of_id=commission_id, created_by=ctx.user_id))
        audit.record(conn, ctx, action=action, resource_type="commission",
                     resource_id=commission_id, before={"state": comm["state"]},
                     after={"state": terminal, "compensating_row": comp_id},
                     reason=reason, tenant_id=schema.GLOBAL_TENANT)
    return comp_id


def _commission_set_state(engine: Engine, ctx: TenantContext,
                          commission_id: str, to_state: str, *,
                          action: str) -> None:
    with engine.begin() as conn:
        comm = _row(conn, schema.commissions, commission_id)
        if to_state not in _COMMISSION_TX.get(comm["state"], set()):
            raise ValidationError(
                f"illegal commission transition {comm['state']} -> {to_state}")
        conn.execute(schema.commissions.update().where(
            schema.commissions.c.id == commission_id).values(
            state=to_state, updated_at=schema.utcnow()))
        audit.record(conn, ctx, action=action, resource_type="commission",
                     resource_id=commission_id, before={"state": comm["state"]},
                     after={"state": to_state}, tenant_id=schema.GLOBAL_TENANT)


# --------------------------------------------------------------- read side
def partner_statement(engine: Engine, ctx: TenantContext,
                      partner_id: str) -> dict:
    """Auditable commission statement for a partner: every commission with its
    state, and the net payable/paid totals (compensating rows included, so the
    numbers reconcile to what was actually billed and paid)."""
    ctx.require("partners:read")
    with engine.connect() as conn:
        partner = _row(conn, schema.partners, partner_id)
        rows = conn.execute(select(schema.commissions).where(
            schema.commissions.c.partner_id == partner_id)
            .order_by(schema.commissions.c.created_at)).mappings().all()
    net_paid = sum((_money(r["amount"]) for r in rows
                    if r["state"] in ("PAID", "CLAWED_BACK")), ZERO_MONEY)
    payable = sum((_money(r["amount"]) for r in rows
                   if r["state"] == "PAYABLE"), ZERO_MONEY)
    return {"partner": partner["name"], "partner_id": partner_id,
            "tier": partner["tier_code"], "net_paid": net_paid,
            "payable": payable, "commissions": [dict(r) for r in rows]}
