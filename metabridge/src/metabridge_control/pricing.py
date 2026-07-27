"""Pricing & rating — the replayable money engine.

Design (docs/commercialization/03-domain-model.md §8):

- **Rate cards are versioned data, not code.** A ``PriceBook`` is authored in
  DRAFT, its entries are mutable only while DRAFT, and ACTIVATING it freezes it
  (same immutability contract as published plan versions). Nothing hardcodes a
  price.
- **Rating is deterministic and replayable.** ``rate_subscription`` computes an
  ``inputs_digest`` over exactly the inputs that move the numbers (usage,
  price-book entries, discount rules); the same digest always yields the same
  ``total`` and the same per-line waterfall. Money is ``Decimal`` throughout,
  quantized once at FINAL — never float (see ``schema.Money``).
- **The waterfall is traced per line.** Every step (1..8 of §8.1) writes its
  input, the rule id, and its delta into ``rated_lines.waterfall`` — so an
  invoice can be explained and reproduced from source without re-running.
- **Floor check fails closed.** If an effective unit price drops below the
  entry's ``floor_price`` and no APPROVED ``PriceOverrideApproval`` covers it,
  the run is persisted as FAILED and ``RatingError`` is raised (§8.1 step 7).
- **Segregation of duties.** A below-floor override is *requested* by one staff
  actor and *approved* by a different one — the same principle the data plane
  enforces for ``agents:approve`` (web/auth.py). Enforced on user id, not role.

Deferred, and recorded honestly as such in the trace rather than faked:
CREDITS (§8.1 step 5 — no promo-credit ledger exists) and PARTNER (step 6 —
partner/commission is a later phase). DiscountApplication rows (§8.2) are not a
separate table here; each application is captured in the line's waterfall trace.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from . import audit, metering, schema
from .context import TenantContext
from .errors import NotFoundError, RatingError, ValidationError
from .schema import MONEY_SCALE, ZERO_MONEY

PRICING_MODELS = ("FLAT", "PER_UNIT", "TIERED", "VOLUME")


def _money(value, field: str = "amount") -> Decimal:
    """Coerce a client value to a 4dp Decimal, rejecting floats-as-noise and
    negatives. Accepts str/int/Decimal; a float is refused because it cannot
    represent money exactly (the whole point of numeric(19,4))."""
    if isinstance(value, float):
        raise ValidationError(f"{field} must be a string/Decimal, not float")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        raise ValidationError(f"{field} is not a valid amount: {value!r}")
    if d < 0:
        raise ValidationError(f"{field} must be >= 0")
    return d.quantize(MONEY_SCALE)


def _q(d: Decimal) -> Decimal:
    return d.quantize(MONEY_SCALE)


# --------------------------------------------------------------------------
# Rate cards — price books & entries (vendor-owned, versioned, DRAFT-mutable)
# --------------------------------------------------------------------------
def create_price_book(engine: Engine, ctx: TenantContext, *, code: str,
                      name: str, currency: str = "USD",
                      region: Optional[str] = None, version: int = 1) -> str:
    ctx.require("pricing:manage")
    code = (code or "").strip().lower()
    if not code:
        raise ValidationError("price book code is required")
    if len(currency) != 3:
        raise ValidationError("currency must be a 3-letter ISO code")
    pid = schema.new_id()
    with engine.begin() as conn:
        dup = conn.execute(select(schema.price_books.c.id).where(
            (schema.price_books.c.code == code)
            & (schema.price_books.c.version == version))).first()
        if dup:
            raise ValidationError(
                f"price book {code} v{version} already exists")
        conn.execute(schema.price_books.insert().values(
            id=pid, code=code, name=name, currency=currency.upper(),
            region=region, version=version, status="DRAFT",
            created_by=ctx.user_id))
        audit.record(conn, ctx, action="pricebook.create",
                     resource_type="price_book", resource_id=pid,
                     after={"code": code, "version": version,
                            "currency": currency.upper()},
                     tenant_id=schema.GLOBAL_TENANT)
    return pid


def add_price_entry(engine: Engine, ctx: TenantContext, price_book_id: str, *,
                    meter_code: str, pricing_model: str = "PER_UNIT",
                    unit_amount="0", tiers: Optional[list] = None,
                    floor_price=None, min_commit=None,
                    included_quantity: int = 0) -> str:
    ctx.require("pricing:manage")
    if pricing_model not in PRICING_MODELS:
        raise ValidationError(f"invalid pricing_model: {pricing_model}")
    if int(included_quantity) < 0:
        raise ValidationError("included_quantity must be >= 0")
    eid = schema.new_id()
    with engine.begin() as conn:
        book = conn.execute(select(schema.price_books).where(
            schema.price_books.c.id == price_book_id)).mappings().first()
        if book is None:
            raise NotFoundError(f"price_books:{price_book_id}")
        if book["status"] != "DRAFT":
            raise ValidationError(
                "price book is not DRAFT; entries are immutable once ACTIVE")
        # the meter must exist (rating reads usage against it)
        metering.get_meter(conn, meter_code)
        norm_tiers = _normalize_tiers(tiers) if pricing_model in (
            "TIERED", "VOLUME") else None
        if pricing_model in ("TIERED", "VOLUME") and not norm_tiers:
            raise ValidationError(
                f"{pricing_model} pricing requires a non-empty tiers list")
        conn.execute(schema.price_book_entries.insert().values(
            id=eid, price_book_id=price_book_id, meter_code=meter_code,
            pricing_model=pricing_model, unit_amount=_money(unit_amount,
                                                            "unit_amount"),
            tiers=norm_tiers,
            floor_price=(None if floor_price is None
                         else _money(floor_price, "floor_price")),
            min_commit=(None if min_commit is None
                        else _money(min_commit, "min_commit")),
            included_quantity=int(included_quantity), created_by=ctx.user_id))
        audit.record(conn, ctx, action="pricebook.entry.add",
                     resource_type="price_book_entry", resource_id=eid,
                     after={"price_book_id": price_book_id,
                            "meter_code": meter_code,
                            "pricing_model": pricing_model},
                     tenant_id=schema.GLOBAL_TENANT)
    return eid


def _normalize_tiers(tiers) -> list:
    """Validate/normalize a tier ladder to ascending bands with Decimal rates
    stored as canonical strings. Each tier: {"up_to": int|None, "amount": x}.
    Exactly one open-ended (``up_to`` null) tier is allowed, and it must be
    last."""
    if not isinstance(tiers, list) or not tiers:
        raise ValidationError("tiers must be a non-empty list")
    out = []
    last_bound = 0
    for i, t in enumerate(tiers):
        up_to = t.get("up_to")
        amount = _money(t.get("amount", "0"), f"tiers[{i}].amount")
        if up_to is not None:
            up_to = int(up_to)
            if up_to <= last_bound:
                raise ValidationError("tier up_to bounds must strictly ascend")
            last_bound = up_to
        elif i != len(tiers) - 1:
            raise ValidationError(
                "only the final tier may be open-ended (up_to null)")
        out.append({"up_to": up_to, "amount": str(amount)})
    return out


def activate_price_book(engine: Engine, ctx: TenantContext,
                        price_book_id: str) -> None:
    """DRAFT -> ACTIVE. Freezes the book's entries (they become immutable).
    A book must have at least one entry to activate."""
    ctx.require("pricing:manage")
    with engine.begin() as conn:
        book = conn.execute(select(schema.price_books).where(
            schema.price_books.c.id == price_book_id)).mappings().first()
        if book is None:
            raise NotFoundError(f"price_books:{price_book_id}")
        if book["status"] == "ACTIVE":
            return
        if book["status"] == "RETIRED":
            raise ValidationError("a RETIRED price book cannot be reactivated")
        n = conn.execute(select(schema.price_book_entries.c.id).where(
            schema.price_book_entries.c.price_book_id == price_book_id)).all()
        if not n:
            raise ValidationError("cannot activate a price book with no entries")
        conn.execute(schema.price_books.update().where(
            schema.price_books.c.id == price_book_id).values(
            status="ACTIVE", updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="pricebook.activate",
                     resource_type="price_book", resource_id=price_book_id,
                     before={"status": book["status"]},
                     after={"status": "ACTIVE"},
                     tenant_id=schema.GLOBAL_TENANT)


# --------------------------------------------------------------------------
# Discount rules
# --------------------------------------------------------------------------
def create_discount_rule(engine: Engine, ctx: TenantContext, *, code: str,
                         name: str, percent: int, priority: int = 100,
                         exclusive: bool = False, max_percent: int = 100,
                         applies_to: Optional[str] = None,
                         tenant_id: Optional[str] = None,
                         expires_at: Optional[datetime] = None) -> str:
    ctx.require("pricing:manage")
    if not (0 <= int(percent) <= 100):
        raise ValidationError("percent must be within 0..100")
    if not (0 <= int(max_percent) <= 100):
        raise ValidationError("max_percent must be within 0..100")
    did = schema.new_id()
    with engine.begin() as conn:
        conn.execute(schema.discount_rules.insert().values(
            id=did, tenant_id=tenant_id, code=(code or "").strip().lower(),
            name=name, percent=int(percent), priority=int(priority),
            exclusive=bool(exclusive), max_percent=int(max_percent),
            applies_to=applies_to, active=True, expires_at=expires_at,
            created_by=ctx.user_id))
        audit.record(conn, ctx, action="discount.create",
                     resource_type="discount_rule", resource_id=did,
                     after={"code": code, "percent": percent,
                            "exclusive": bool(exclusive)},
                     tenant_id=tenant_id or schema.GLOBAL_TENANT)
    return did


# --------------------------------------------------------------------------
# Price-override approvals (segregation of duties, §8.1 step 7)
# --------------------------------------------------------------------------
def request_price_override(engine: Engine, ctx: TenantContext, *,
                           subscription_id: str, meter_code: str,
                           floor_price, proposed_price: str,
                           justification: str) -> str:
    """Request approval to price a meter below its floor. The requester may not
    later approve their own request."""
    ctx.require("pricing:manage")
    if not (justification or "").strip():
        raise ValidationError("an override request requires a justification")
    floor = _money(floor_price, "floor_price")
    proposed = _money(proposed_price, "proposed_price")
    if proposed >= floor:
        raise ValidationError(
            "proposed_price is not below floor_price — no override needed")
    oid = schema.new_id()
    with engine.begin() as conn:
        sub = _load_subscription(conn, ctx, subscription_id)
        conn.execute(schema.price_override_approvals.insert().values(
            id=oid, tenant_id=sub["tenant_id"], subscription_id=subscription_id,
            meter_code=meter_code, floor_price=floor, proposed_price=proposed,
            justification=justification, requested_by=ctx.user_id,
            state="REQUESTED", created_by=ctx.user_id))
        audit.record(conn, ctx, action="price_override.request",
                     resource_type="price_override_approval", resource_id=oid,
                     after={"subscription_id": subscription_id,
                            "meter_code": meter_code,
                            "proposed_price": str(proposed)},
                     tenant_id=sub["tenant_id"])
    return oid


def approve_price_override(engine: Engine, ctx: TenantContext, override_id: str,
                           *, approve: bool = True) -> None:
    """Approve or reject a below-floor override. Segregation of duties: the
    approver's user id MUST differ from the requester's (same rule as the data
    plane's ``agents:approve``)."""
    ctx.require("pricing:approve_override")
    with engine.begin() as conn:
        ov = conn.execute(select(schema.price_override_approvals).where(
            schema.price_override_approvals.c.id == override_id)) \
            .mappings().first()
        if ov is None:
            raise NotFoundError(f"price_override_approvals:{override_id}")
        if ctx.tenant_id not in (schema.GLOBAL_TENANT, ov["tenant_id"]):
            raise ValidationError("tenant scope mismatch")
        if ov["state"] != "REQUESTED":
            raise ValidationError(
                f"override is {ov['state']}, not REQUESTED")
        if ov["requested_by"] == ctx.user_id:
            raise ValidationError(
                "segregation of duties: an override cannot be approved by its "
                "requester")
        new_state = "APPROVED" if approve else "REJECTED"
        conn.execute(schema.price_override_approvals.update().where(
            schema.price_override_approvals.c.id == override_id).values(
            state=new_state, approved_by=ctx.user_id,
            decided_at=schema.utcnow(), updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="price_override." + new_state.lower(),
                     resource_type="price_override_approval",
                     resource_id=override_id,
                     before={"requested_by": ov["requested_by"]},
                     after={"state": new_state, "approved_by": ctx.user_id},
                     tenant_id=ov["tenant_id"])


# --------------------------------------------------------------------------
# The rating waterfall (§8.1)
# --------------------------------------------------------------------------
def _load_subscription(conn: Connection, ctx: TenantContext,
                       subscription_id: str) -> dict:
    sub = conn.execute(select(schema.subscriptions).where(
        schema.subscriptions.c.id == subscription_id)).mappings().first()
    if sub is None:
        raise NotFoundError(f"subscriptions:{subscription_id}")
    if ctx.tenant_id not in (schema.GLOBAL_TENANT, sub["tenant_id"]):
        raise ValidationError("tenant scope mismatch")
    return dict(sub)


def _price_line(qty: int, entry: dict) -> tuple[Decimal, int, list]:
    """Steps 1-2: LIST + TIER/VOLUME. Returns (list_amount, billable_qty,
    trace-steps). Pure and deterministic."""
    included = int(entry["included_quantity"] or 0)
    billable = max(0, int(qty) - included)
    model = entry["pricing_model"]
    unit = _money(entry["unit_amount"], "unit_amount")
    steps: list = [{"step": 1, "name": "LIST", "pricing_model": model,
                    "quantity": int(qty), "included": included,
                    "billable_quantity": billable}]
    if billable == 0:
        steps.append({"step": 2, "name": "TIER_VOLUME", "amount": "0.0000",
                      "note": "no billable quantity"})
        return ZERO_MONEY, billable, steps
    if model == "FLAT":
        amount = unit
        steps.append({"step": 2, "name": "TIER_VOLUME", "model": "FLAT",
                      "amount": str(amount)})
    elif model == "PER_UNIT":
        amount = _q(unit * billable)
        steps.append({"step": 2, "name": "TIER_VOLUME", "model": "PER_UNIT",
                      "unit_amount": str(unit), "amount": str(amount)})
    else:  # TIERED | VOLUME
        amount, tier_trace = _tiered(billable, entry["tiers"] or [], model)
        steps.append({"step": 2, "name": "TIER_VOLUME", "model": model,
                      "bands": tier_trace, "amount": str(amount)})
    return _q(amount), billable, steps


def _tiered(qty: int, tiers: list, model: str) -> tuple[Decimal, list]:
    """TIERED = graduated (each band's rate applies to units inside it);
    VOLUME = the single band containing ``qty`` sets the rate for ALL units."""
    if model == "VOLUME":
        rate = _money(tiers[-1]["amount"])
        for t in tiers:
            if t["up_to"] is not None and qty <= int(t["up_to"]):
                rate = _money(t["amount"])
                break
        return _q(rate * qty), [{"applied_rate": str(rate), "units": qty}]
    total = ZERO_MONEY
    lower = 0
    trace = []
    for t in tiers:
        up_to = t["up_to"]
        rate = _money(t["amount"])
        band_top = qty if up_to is None else min(qty, int(up_to))
        units = max(0, band_top - lower)
        if units:
            total += rate * units
            trace.append({"up_to": up_to, "rate": str(rate), "units": units})
        lower = band_top if up_to is not None else qty
        if up_to is not None and qty <= int(up_to):
            break
    return _q(total), trace


def _applicable_discounts(conn: Connection, tenant_id: str,
                          meter_code: str) -> list:
    """Active, unexpired discount rules for this tenant (or global) and meter
    (or all meters), highest precedence first (lowest ``priority`` number)."""
    now = schema.utcnow()
    rows = conn.execute(select(schema.discount_rules).where(
        schema.discount_rules.c.active.is_(True))).mappings().all()
    out = []
    for r in rows:
        if r["tenant_id"] not in (None, tenant_id):
            continue
        if r["applies_to"] not in (None, meter_code):
            continue
        if r["expires_at"] is not None and r["expires_at"] <= now:
            continue
        out.append(dict(r))
    out.sort(key=lambda r: (r["priority"], r["code"]))
    return out


def _apply_discounts(base: Decimal, discounts: list) -> tuple[Decimal, list]:
    """Step 4. Stack discounts in precedence order; an EXCLUSIVE rule is applied
    alone and terminates stacking; cumulative percent is capped by the tightest
    ``max_percent`` among the rules applied."""
    if base <= 0 or not discounts:
        return base, [{"step": 4, "name": "DISCOUNTS", "applied": [],
                       "amount": str(_q(base))}]
    cum_pct = 0
    applied = []
    cap = 100
    for pos, d in enumerate(discounts):
        if d["exclusive"] and applied:
            break  # exclusive rule does not stack on top of earlier ones
        cap = min(cap, int(d["max_percent"]))
        proposed = min(cum_pct + int(d["percent"]), cap)
        delta_pct = proposed - cum_pct
        if delta_pct <= 0:
            applied.append({"discount_rule_id": d["id"], "code": d["code"],
                            "percent": int(d["percent"]),
                            "applied_percent": 0, "stacking_position": pos,
                            "note": "capped out"})
            continue
        cum_pct = proposed
        applied.append({"discount_rule_id": d["id"], "code": d["code"],
                        "percent": int(d["percent"]),
                        "applied_percent": delta_pct,
                        "exclusive": bool(d["exclusive"]),
                        "stacking_position": pos})
        if d["exclusive"]:
            break
    discounted = _q(base * (Decimal(100 - cum_pct) / Decimal(100)))
    return discounted, [{"step": 4, "name": "DISCOUNTS",
                         "cumulative_percent": cum_pct, "cap": cap,
                         "applied": applied, "amount": str(discounted)}]


def _find_override(conn: Connection, subscription_id: str, meter_code: str,
                   effective_unit: Decimal) -> Optional[dict]:
    """An APPROVED override for this (sub, meter) that authorizes a price at
    least as low as the effective unit price (proposed <= effective). The
    override stays APPROVED across runs — rating never mutates it — so a re-rate
    of the same period reproduces the same result."""
    rows = conn.execute(select(schema.price_override_approvals).where(
        (schema.price_override_approvals.c.subscription_id == subscription_id)
        & (schema.price_override_approvals.c.meter_code == meter_code)
        & (schema.price_override_approvals.c.state == "APPROVED"))) \
        .mappings().all()
    for r in rows:
        if _money(r["proposed_price"]) <= effective_unit:
            return dict(r)
    return None


def rate_subscription(engine: Engine, ctx: TenantContext, *,
                      subscription_id: str, price_book_id: str,
                      period_start: datetime, period_end: datetime) -> dict:
    """Run the deterministic §8.1 waterfall for one subscription over a period.

    Reads billable usage (frozen if the period is finalized, else live) and the
    price book, prices each metered line through the waterfall, and persists a
    RatingRun + RatedLines with a replayable per-line trace. Supersedes any
    prior COMPLETE run for the same (subscription, period). Fails closed on a
    below-floor price with no approved override."""
    ctx.require("rating:run")
    if period_end <= period_start:
        raise ValidationError("period_end must be after period_start")

    # 1. Read usage OUTSIDE the write transaction (metering opens its own
    #    connection; the single-writer SQLite engine forbids nesting).
    with engine.begin() as conn:
        sub = _load_subscription(conn, ctx, subscription_id)
    tenant_id = sub["tenant_id"]
    usage = metering.usage_summary(engine, tenant_id=tenant_id,
                                   period_start=period_start,
                                   period_end=period_end)
    qty_by_meter = {m["meter_code"]: int(m["quantity"])
                    for m in usage["meters"] if m["billable"]}

    with engine.begin() as conn:
        book = conn.execute(select(schema.price_books).where(
            schema.price_books.c.id == price_book_id)).mappings().first()
        if book is None:
            raise NotFoundError(f"price_books:{price_book_id}")
        if book["status"] != "ACTIVE":
            raise ValidationError(
                f"price book {book['code']} is {book['status']}, not ACTIVE")
        currency = book["currency"]
        entries = {e["meter_code"]: dict(e) for e in conn.execute(
            select(schema.price_book_entries).where(
                schema.price_book_entries.c.price_book_id == price_book_id))
            .mappings()}

        # deterministic digest over exactly the rating inputs
        digest = _inputs_digest(subscription_id, price_book_id, period_start,
                                period_end, qty_by_meter, entries,
                                conn, tenant_id)

        run_id = schema.new_id()
        lines = []
        failure = None
        total = ZERO_MONEY
        for meter_code in sorted(qty_by_meter):        # sorted => stable order
            entry = entries.get(meter_code)
            if entry is None:
                continue    # metered but not on this rate card -> not billed
            qty = qty_by_meter[meter_code]
            list_amount, billable, steps = _price_line(qty, entry)
            # step 3 CONTRACT: the chosen book *is* the contract book; no
            # separate negotiated-price entity exists in this phase.
            steps.append({"step": 3, "name": "CONTRACT",
                          "note": "rated against selected price book",
                          "amount": str(list_amount)})
            discounts = _applicable_discounts(conn, tenant_id, meter_code)
            after_disc, disc_steps = _apply_discounts(list_amount, discounts)
            steps.extend(disc_steps)
            # steps 5-6 deferred, recorded rather than silently skipped
            steps.append({"step": 5, "name": "CREDITS", "status": "DEFERRED",
                          "amount": str(after_disc)})
            steps.append({"step": 6, "name": "PARTNER", "status": "DEFERRED",
                          "amount": str(after_disc)})
            final = after_disc
            # step 7 FLOOR CHECK (per-unit), fail-closed
            floor = entry["floor_price"]
            floor_step = {"step": 7, "name": "FLOOR_CHECK"}
            if floor is not None and billable > 0:
                floor = _money(floor)
                eff_unit = _q(final / billable)
                floor_step.update({"floor_price": str(floor),
                                   "effective_unit": str(eff_unit)})
                if eff_unit < floor:
                    ov = _find_override(conn, subscription_id, meter_code,
                                        eff_unit)
                    if ov is None:
                        floor_step.update({"result": "FAIL_CLOSED"})
                        steps.append(floor_step)
                        failure = (f"effective unit price {eff_unit} for "
                                   f"{meter_code} is below floor {floor} with "
                                   f"no approved override")
                        lines.append({"meter_code": meter_code, "quantity": qty,
                                      "list_amount": list_amount,
                                      "final_amount": final, "steps": steps})
                        break
                    # An APPROVED override authorizes below-floor pricing for
                    # this (subscription, meter) until revoked/expired — it is
                    # NOT consumed per run, so re-rating the same period stays
                    # deterministic and replayable.
                    floor_step.update({"result": "OVERRIDE_APPLIED",
                                       "override_id": ov["id"]})
                else:
                    floor_step.update({"result": "OK"})
            else:
                floor_step.update({"result": "NO_FLOOR"})
            steps.append(floor_step)
            # step 8 FINAL: min-commit revenue floor, then round + currency
            min_commit = entry["min_commit"]
            if min_commit is not None and final < _money(min_commit):
                final = _money(min_commit)
                steps.append({"step": 8, "name": "FINAL",
                              "min_commit_applied": str(final),
                              "currency": currency, "amount": str(final)})
            else:
                final = _q(final)
                steps.append({"step": 8, "name": "FINAL", "currency": currency,
                              "amount": str(final)})
            total += final
            lines.append({"meter_code": meter_code, "quantity": qty,
                          "list_amount": list_amount, "final_amount": final,
                          "steps": steps})

        state = "FAILED" if failure else "COMPLETE"
        if state == "COMPLETE":
            # supersede a prior COMPLETE run for the same window
            conn.execute(schema.rating_runs.update().where(
                (schema.rating_runs.c.subscription_id == subscription_id)
                & (schema.rating_runs.c.period_start == period_start)
                & (schema.rating_runs.c.period_end == period_end)
                & (schema.rating_runs.c.state == "COMPLETE")).values(
                state="SUPERSEDED", updated_at=schema.utcnow()))
        conn.execute(schema.rating_runs.insert().values(
            id=run_id, tenant_id=tenant_id, subscription_id=subscription_id,
            price_book_id=price_book_id, period_start=period_start,
            period_end=period_end, state=state, inputs_digest=digest,
            total=(ZERO_MONEY if failure else _q(total)), currency=currency,
            reason=failure, created_by=ctx.user_id))
        for ln in lines:
            conn.execute(schema.rated_lines.insert().values(
                id=schema.new_id(), tenant_id=tenant_id, rating_run_id=run_id,
                meter_code=ln["meter_code"], quantity=ln["quantity"],
                list_amount=ln["list_amount"], final_amount=ln["final_amount"],
                currency=currency, waterfall=ln["steps"]))
        audit.record(conn, ctx, action="rating.run",
                     resource_type="rating_run", resource_id=run_id,
                     after={"state": state, "total": str(total),
                            "currency": currency, "digest": digest},
                     reason=failure, result=("FAILURE" if failure else "SUCCESS"),
                     tenant_id=tenant_id)

    if failure:
        raise RatingError(failure, rating_run_id=run_id)
    return {"rating_run_id": run_id, "state": state, "total": _q(total),
            "currency": currency, "inputs_digest": digest,
            "line_count": len(lines)}


def _inputs_digest(subscription_id, price_book_id, period_start, period_end,
                   qty_by_meter, entries, conn, tenant_id) -> str:
    """SHA-256 over exactly the inputs that move the numbers — so two runs with
    identical usage, rate card and discounts produce the same digest."""
    disc = {}
    for mc in qty_by_meter:
        disc[mc] = [{"code": d["code"], "percent": d["percent"],
                     "priority": d["priority"], "exclusive": d["exclusive"],
                     "max_percent": d["max_percent"]}
                    for d in _applicable_discounts(conn, tenant_id, mc)]
    payload = {
        "subscription_id": subscription_id, "price_book_id": price_book_id,
        "period_start": str(period_start), "period_end": str(period_end),
        "usage": {k: qty_by_meter[k] for k in sorted(qty_by_meter)},
        "entries": {mc: {"pricing_model": e["pricing_model"],
                         "unit_amount": str(e["unit_amount"]),
                         "tiers": e["tiers"],
                         "floor_price": (None if e["floor_price"] is None
                                         else str(e["floor_price"])),
                         "min_commit": (None if e["min_commit"] is None
                                        else str(e["min_commit"])),
                         "included_quantity": e["included_quantity"]}
                    for mc, e in sorted(entries.items())},
        "discounts": disc,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Read side
# --------------------------------------------------------------------------
def get_rating_run(engine: Engine, ctx: TenantContext, run_id: str) -> dict:
    ctx.require("pricing:read")
    with engine.connect() as conn:
        run = conn.execute(select(schema.rating_runs).where(
            schema.rating_runs.c.id == run_id)).mappings().first()
        if run is None:
            raise NotFoundError(f"rating_runs:{run_id}")
        if ctx.tenant_id not in (schema.GLOBAL_TENANT, run["tenant_id"]):
            raise NotFoundError(f"rating_runs:{run_id}")
        lines = conn.execute(select(schema.rated_lines).where(
            schema.rated_lines.c.rating_run_id == run_id)
            .order_by(schema.rated_lines.c.meter_code)).mappings().all()
    return {"run": dict(run), "lines": [dict(x) for x in lines]}
