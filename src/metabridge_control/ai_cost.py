"""AI cost governance (docs/commercialization/03-domain-model.md §10).

Records model invocations reported by instances, estimates their cost from a
versioned rate card, and evaluates per-tenant/workspace budgets.

Two invariants that are easy to get wrong:

- **est_cost is a labelled estimate, never a bill.** It is computed from the
  vendor-maintained ``ai_rate_cards`` (money per 1,000,000 tokens) purely for
  visibility; the provider's own invoice remains the source of truth. Token
  counts, by contrast, are facts and roll into the usage meters (AI_TOKENS_*).
- **A budget breach never blocks a deterministic engine.** ``check_ai_budget``
  advises the *AI* path only; ``BLOCK_AI_ONLY`` is the hardest action, and even
  then the caller degrades AI to unavailable — it must never withhold a
  modernization result or customer data.

Usage records are append-only and idempotent on ``(tenant, idempotency_key)``,
so an instance re-reporting after a retry is counted exactly once.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine

from . import audit, metering, schema
from .context import TenantContext
from .errors import ValidationError
from .schema import MONEY_SCALE, ZERO_MONEY

PROVIDERS = ("ANTHROPIC", "BEDROCK")
BREACH_ACTIONS = ("WARN", "THROTTLE", "BLOCK_AI_ONLY")
_PER_MILLION = Decimal("1000000")


def _money(v) -> Decimal:
    if isinstance(v, float):
        raise ValidationError("money must be a string/Decimal, not float")
    return (v if isinstance(v, Decimal) else Decimal(str(v or 0))) \
        .quantize(MONEY_SCALE)


def _rate_for(conn: Connection, provider: str, model_id: str) -> Optional[dict]:
    return conn.execute(select(schema.ai_rate_cards).where(
        (schema.ai_rate_cards.c.provider == provider)
        & (schema.ai_rate_cards.c.model_id == model_id)
        & (schema.ai_rate_cards.c.active.is_(True)))
        .order_by(schema.ai_rate_cards.c.effective_from.desc())) \
        .mappings().first()


def estimate_cost(input_tokens: int, output_tokens: int,
                  input_rate, output_rate) -> Decimal:
    """Cost estimate: tokens / 1e6 x per-million rate, for each direction."""
    cost = (Decimal(int(input_tokens)) / _PER_MILLION) * _money(input_rate) \
        + (Decimal(int(output_tokens)) / _PER_MILLION) * _money(output_rate)
    return cost.quantize(MONEY_SCALE)


# --------------------------------------------------------------- rate cards
def create_rate_card(engine: Engine, ctx: TenantContext, *, provider: str,
                     model_id: str, input_rate, output_rate,
                     currency: str = "USD") -> str:
    ctx.require("ai:manage")
    if provider not in PROVIDERS:
        raise ValidationError(f"invalid provider: {provider}")
    rid = schema.new_id()
    with engine.begin() as conn:
        # supersede any prior active card for the model (keep history)
        conn.execute(schema.ai_rate_cards.update().where(
            (schema.ai_rate_cards.c.provider == provider)
            & (schema.ai_rate_cards.c.model_id == model_id)).values(
            active=False, updated_at=schema.utcnow()))
        conn.execute(schema.ai_rate_cards.insert().values(
            id=rid, provider=provider, model_id=model_id,
            input_rate=_money(input_rate), output_rate=_money(output_rate),
            currency=currency, effective_from=schema.utcnow(), active=True,
            created_by=ctx.user_id))
        audit.record(conn, ctx, action="ai.rate_card.create",
                     resource_type="ai_rate_card", resource_id=rid,
                     after={"provider": provider, "model_id": model_id},
                     tenant_id=schema.GLOBAL_TENANT)
    return rid


# --------------------------------------------------------------- usage records
def record_ai_usage(engine: Engine, *, tenant_id: str, provider: str,
                    model_id: str, input_tokens: int, output_tokens: int,
                    idempotency_key: str, instance_id: Optional[str] = None,
                    purpose: Optional[str] = None,
                    job_ref: Optional[str] = None,
                    occurred_at: Optional[datetime] = None,
                    actor: str = "system") -> dict:
    """Append an immutable AI usage record (idempotent) and roll its tokens into
    the AI_TOKENS_IN / AI_TOKENS_OUT meters. Cost is estimated from the current
    rate card (0 if none is configured — tokens are still recorded)."""
    if not (idempotency_key or "").strip():
        raise ValidationError("record_ai_usage requires an idempotency_key")
    if int(input_tokens) < 0 or int(output_tokens) < 0:
        raise ValidationError("token counts must be >= 0")
    occurred_at = occurred_at or schema.utcnow()
    rid = schema.new_id()
    with engine.begin() as conn:
        existing = conn.execute(select(schema.ai_usage_records).where(
            (schema.ai_usage_records.c.tenant_id == tenant_id)
            & (schema.ai_usage_records.c.idempotency_key
               == idempotency_key))).mappings().first()
        if existing:
            return {"record_id": existing["id"],
                    "est_cost": _money(existing["est_cost"]), "created": False}
        rate = _rate_for(conn, provider, model_id)
        if rate is not None:
            est = estimate_cost(input_tokens, output_tokens,
                                rate["input_rate"], rate["output_rate"])
            currency = rate["currency"]
        else:
            est, currency = ZERO_MONEY, "USD"
        conn.execute(schema.ai_usage_records.insert().values(
            id=rid, tenant_id=tenant_id, instance_id=instance_id,
            provider=provider, model_id=model_id,
            input_tokens=int(input_tokens), output_tokens=int(output_tokens),
            purpose=purpose, job_ref=job_ref, est_cost=est, currency=currency,
            occurred_at=occurred_at, idempotency_key=idempotency_key,
            created_by=actor))
    # roll tokens into the usage meters (each idempotent + append-only)
    for meter, qty in (("AI_TOKENS_IN", input_tokens),
                       ("AI_TOKENS_OUT", output_tokens)):
        if qty:
            metering.record_usage(
                engine, tenant_id=tenant_id, meter_code=meter, quantity=int(qty),
                idempotency_key=f"ai:{idempotency_key}:{meter}",
                occurred_at=occurred_at, instance_id=instance_id,
                dimensions={"provider": provider, "model_id": model_id},
                actor=actor)
    return {"record_id": rid, "est_cost": est, "created": True}


# --------------------------------------------------------------- budgets
def set_ai_budget(engine: Engine, ctx: TenantContext, tenant_id: str, *,
                  scope: str = "TENANT", scope_id: str = "*",
                  period: str = "MONTHLY", limit_tokens: Optional[int] = None,
                  limit_est_cost=None, action_on_breach: str = "WARN",
                  currency: str = "USD") -> str:
    ctx.require("ai:manage")
    if action_on_breach not in BREACH_ACTIONS:
        raise ValidationError(f"invalid action_on_breach: {action_on_breach}")
    if limit_tokens is None and limit_est_cost is None:
        raise ValidationError("a budget needs a token or cost limit")
    bid = schema.new_id()
    with engine.begin() as conn:
        existing = conn.execute(select(schema.ai_budgets.c.id).where(
            (schema.ai_budgets.c.tenant_id == tenant_id)
            & (schema.ai_budgets.c.scope == scope)
            & (schema.ai_budgets.c.scope_id == scope_id)
            & (schema.ai_budgets.c.period == period))).first()
        values = dict(
            limit_tokens=(None if limit_tokens is None else int(limit_tokens)),
            limit_est_cost=(None if limit_est_cost is None
                            else _money(limit_est_cost)),
            currency=currency, action_on_breach=action_on_breach, active=True,
            updated_at=schema.utcnow())
        if existing:
            bid = existing[0]
            conn.execute(schema.ai_budgets.update().where(
                schema.ai_budgets.c.id == bid).values(**values))
        else:
            conn.execute(schema.ai_budgets.insert().values(
                id=bid, tenant_id=tenant_id, scope=scope, scope_id=scope_id,
                period=period, created_by=ctx.user_id, **values))
        audit.record(conn, ctx, action="ai.budget.set",
                     resource_type="ai_budget", resource_id=bid,
                     after={"scope": scope, "action_on_breach": action_on_breach},
                     tenant_id=tenant_id)
    return bid


def _consumed(conn: Connection, tenant_id: str, start: datetime,
              end: datetime) -> tuple:
    rec = schema.ai_usage_records
    base = ((rec.c.tenant_id == tenant_id) & (rec.c.occurred_at >= start)
            & (rec.c.occurred_at < end))
    toks = conn.execute(select(
        func.coalesce(func.sum(rec.c.input_tokens + rec.c.output_tokens), 0))
        .where(base)).scalar() or 0
    rows = conn.execute(select(rec.c.est_cost).where(base)).all()
    cost = sum((_money(r[0]) for r in rows), ZERO_MONEY)
    return int(toks), cost


def check_ai_budget(engine: Engine, *, tenant_id: str, period_start: datetime,
                    period_end: datetime, scope: str = "TENANT",
                    scope_id: str = "*") -> dict:
    """Evaluate AI budget for a window. Advises the AI path ONLY — the returned
    ``ai_allowed`` is False only when a budget with ``BLOCK_AI_ONLY`` is
    breached. Deterministic engines are never gated by this."""
    with engine.connect() as conn:
        budget = conn.execute(select(schema.ai_budgets).where(
            (schema.ai_budgets.c.tenant_id == tenant_id)
            & (schema.ai_budgets.c.scope == scope)
            & (schema.ai_budgets.c.scope_id == scope_id)
            & (schema.ai_budgets.c.active.is_(True)))).mappings().first()
        tokens, cost = _consumed(conn, tenant_id, period_start, period_end)
    if budget is None:
        return {"decision": "ALLOW", "ai_allowed": True, "action": None,
                "consumed_tokens": tokens, "consumed_cost": cost,
                "limit_tokens": None, "limit_est_cost": None}
    over = False
    if budget["limit_tokens"] is not None and tokens > int(budget["limit_tokens"]):
        over = True
    if budget["limit_est_cost"] is not None \
            and cost > _money(budget["limit_est_cost"]):
        over = True
    action = budget["action_on_breach"]
    decision = ("ALLOW" if not over
                else ("BLOCKED" if action == "BLOCK_AI_ONLY" else action))
    return {"decision": decision,
            "ai_allowed": not (over and action == "BLOCK_AI_ONLY"),
            "action": (action if over else None),
            "consumed_tokens": tokens, "consumed_cost": cost,
            "limit_tokens": budget["limit_tokens"],
            "limit_est_cost": (None if budget["limit_est_cost"] is None
                               else _money(budget["limit_est_cost"]))}


# --------------------------------------------------------------- rollup
def ai_cost_report(engine: Engine, ctx: TenantContext, *, tenant_id: str,
                   period_start: datetime, period_end: datetime) -> dict:
    """Per-model rollup of AI tokens + estimated cost for a tenant/window."""
    ctx.require("ai:read")
    rec = schema.ai_usage_records
    with engine.connect() as conn:
        rows = conn.execute(select(rec).where(
            (rec.c.tenant_id == tenant_id) & (rec.c.occurred_at >= period_start)
            & (rec.c.occurred_at < period_end))).mappings().all()
    by_model: dict = {}
    total_cost = ZERO_MONEY
    total_tokens = 0
    for r in rows:
        key = f"{r['provider']}/{r['model_id']}"
        m = by_model.setdefault(key, {"provider": r["provider"],
                                      "model_id": r["model_id"],
                                      "input_tokens": 0, "output_tokens": 0,
                                      "est_cost": ZERO_MONEY, "calls": 0})
        m["input_tokens"] += int(r["input_tokens"])
        m["output_tokens"] += int(r["output_tokens"])
        m["est_cost"] = _money(m["est_cost"] + _money(r["est_cost"]))
        m["calls"] += 1
        total_cost = _money(total_cost + _money(r["est_cost"]))
        total_tokens += int(r["input_tokens"]) + int(r["output_tokens"])
    return {"tenant_id": tenant_id, "period_start": str(period_start),
            "period_end": str(period_end), "total_tokens": total_tokens,
            "total_est_cost": total_cost, "by_model": list(by_model.values())}
