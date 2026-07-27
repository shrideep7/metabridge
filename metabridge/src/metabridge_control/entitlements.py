"""Centralized entitlement engine — the revenue-protection primitive.

`check_access` is the single backend gate every licensed operation calls
(docs/commercialization/03-domain-model.md §6.1). It resolves the tenant's
subscription, derives the entitlement, and returns an ALLOW / ALLOW_GRACE /
DENY decision with a stable reason code. It supports four value kinds
(BOOLEAN, NUMERIC_LIMIT, METERED_QUOTA, TIER) plus a DATE_WINDOW derived from
the subscription term, and the RESERVE / COMMIT / RELEASE protocol for async
metered work.

Enforcement posture (deliberate, per Phase 0): fail **soft then dark** —
degrade features during grace, never corrupt or withhold customer data. The
data plane's deterministic engines keep working; only licensed *expansion*
(new users/programs/AI spend) is gated.

Every decision may be logged (sampled/denied) to entitlement_decision_log;
every RESERVE/COMMIT/RELEASE and quota mutation runs in one transaction.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from . import schema
from .context import TenantContext
from .errors import ValidationError

# value-kind convention keyed by entitlement-code prefix
VALUE_KINDS = ("BOOLEAN", "NUMERIC_LIMIT", "METERED_QUOTA", "TIER", "DATE_WINDOW")

# subscription states in which entitlements are honored, and how
_FULL_STATES = {"TRIALING", "ACTIVE", "CANCELLED"}   # CANCELLED runs to term end
_WARM_STATES = {"PAST_DUE"}                            # grace-warm
_READONLY_STATES = {"SUSPENDED"}
# DRAFT / PENDING_ACTIVATION / EXPIRED / TERMINATED => no entitlements

# overage tolerance for COMMIT actual > reserved (fraction)
COMMIT_OVERAGE_TOLERANCE = 0.10
DEFAULT_RESERVATION_TTL_MIN = 60


class Mode(str, enum.Enum):
    CHECK = "CHECK"
    RESERVE = "RESERVE"
    COMMIT = "COMMIT"
    RELEASE = "RELEASE"


@dataclass
class Decision:
    decision: str                      # ALLOW | ALLOW_GRACE | DENY
    reason_code: str
    remaining: Optional[int] = None
    limit: Optional[int] = None
    reservation_id: Optional[str] = None
    subscription_id: Optional[str] = None
    license_expiry: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision in ("ALLOW", "ALLOW_GRACE")


def value_kind_for(code: str) -> str:
    if code.startswith("feature."):
        return "BOOLEAN"
    if code.startswith("limit."):
        return "NUMERIC_LIMIT"
    if code.startswith("quota."):
        return "METERED_QUOTA"
    if code.startswith("tier."):
        return "TIER"
    raise ValidationError(f"cannot infer value kind for entitlement '{code}'")


def period_key(kind: str, now: datetime) -> str:
    if kind == "ANNUAL":
        return f"{now.year}"
    if kind == "QUARTERLY":
        return f"{now.year}-Q{(now.month - 1) // 3 + 1}"
    return f"{now.year}-{now.month:02d}"           # MONTHLY default


# --------------------------------------------------------------------------
# Resolution — fold plan + overrides into effective entitlement rows.
# --------------------------------------------------------------------------
def resolve_in_conn(conn: Connection, subscription_id: str,
                    actor: str = "system", now: Optional[datetime] = None) -> int:
    """(Re)compute effective entitlements for a subscription from its pinned
    plan version plus active, unexpired overrides — **inside the caller's
    transaction** so state changes and entitlement changes commit atomically
    (Phase-2 review HIGH). Idempotent; returns the count.

    Override expiry is carried onto the entitlement row (``expires_at``) so the
    gate can enforce it in real time and self-heal (see ``_entitlement``)."""
    from . import catalog
    now = now or schema.utcnow()
    sub = conn.execute(select(schema.subscriptions).where(
        schema.subscriptions.c.id == subscription_id)).mappings().first()
    if sub is None:
        raise ValidationError(f"subscription not found: {subscription_id}")
    feats = catalog.effective_features(conn, sub["plan_version_id"])
    resolved: dict[str, dict] = {}
    for code, f in feats.items():
        vk = value_kind_for(code)
        if vk == "BOOLEAN":
            value = {"enabled": bool(f.get("enabled"))}
        else:  # NUMERIC_LIMIT / METERED_QUOTA share limit/unlimited shape
            value = {"unlimited": bool(f.get("unlimited")),
                     "limit": f.get("limit")}
        resolved[code] = {"value_kind": vk, "value": value,
                          "period": (sub["billing_period"]
                                     if vk == "METERED_QUOTA" else None),
                          "source": "PLAN", "expires_at": None}
    # active, unexpired overrides win over the plan value
    ovs = conn.execute(select(schema.entitlement_overrides).where(
        (schema.entitlement_overrides.c.subscription_id == subscription_id)
        & (schema.entitlement_overrides.c.active.is_(True)))).mappings()
    for o in ovs:
        if o["expires_at"] is not None and o["expires_at"] < now:
            continue                                  # already lapsed
        resolved[o["code"]] = {"value_kind": o["value_kind"],
                               "value": o["value"], "period": o["period"],
                               "source": "OVERRIDE", "expires_at":
                               o["expires_at"]}
    conn.execute(schema.entitlements.delete().where(
        schema.entitlements.c.subscription_id == subscription_id))
    for code, r in resolved.items():
        conn.execute(schema.entitlements.insert().values(
            id=schema.new_id(), tenant_id=sub["tenant_id"],
            subscription_id=subscription_id, code=code,
            value_kind=r["value_kind"], value=r["value"], period=r["period"],
            source=r["source"], expires_at=r["expires_at"], created_by=actor))
    return len(resolved)


def resolve_entitlements(engine: Engine, subscription_id: str,
                         actor: str = "system") -> int:
    """Standalone re-resolution in its own transaction (external callers)."""
    with engine.begin() as conn:
        return resolve_in_conn(conn, subscription_id, actor)


# --------------------------------------------------------------------------
# check_access — the gate.
# --------------------------------------------------------------------------
# health priority for choosing the authoritative subscription when a tenant
# has more than one in an honored state (Phase-2 review MEDIUM): prefer the
# healthiest, and only then the newest — never "newest-created wins".
_STATE_PRIORITY = {"ACTIVE": 0, "TRIALING": 1, "PAST_DUE": 2, "CANCELLED": 3,
                   "SUSPENDED": 4}


def _active_subscription(conn: Connection, tenant_id: str,
                         lock: bool = False):
    rows = conn.execute(select(schema.subscriptions).where(
        (schema.subscriptions.c.tenant_id == tenant_id)
        & (schema.subscriptions.c.state.in_(
            list(_FULL_STATES | _WARM_STATES | _READONLY_STATES))))
        .order_by(schema.subscriptions.c.created_at.desc())).mappings().all()
    if not rows:
        return None
    chosen = min(rows, key=lambda r: (_STATE_PRIORITY.get(r["state"], 9),))
    if lock and conn.engine.dialect.name not in ("sqlite",):
        # re-read the chosen row under a row lock for RESERVE/COMMIT paths
        return conn.execute(select(schema.subscriptions)
                            .where(schema.subscriptions.c.id == chosen["id"])
                            .with_for_update()).mappings().first()
    return chosen


def _entitlement(conn: Connection, subscription_id: str, code: str,
                 lock: bool = False):
    q = select(schema.entitlements).where(
        (schema.entitlements.c.subscription_id == subscription_id)
        & (schema.entitlements.c.code == code))
    if lock and conn.engine.dialect.name not in ("sqlite",):
        q = q.with_for_update()
    return conn.execute(q).mappings().first()


def _fresh_entitlement(conn: Connection, subscription_id: str, code: str,
                       now: datetime, lock: bool = False):
    """Fetch an entitlement, self-healing a lapsed time-boxed override: if the
    stored override has passed ``expires_at``, re-resolve within this
    transaction so the plan value is restored in real time (Phase-2 review)."""
    ent = _entitlement(conn, subscription_id, code, lock=lock)
    if ent is not None and ent["expires_at"] is not None \
            and ent["expires_at"] < now:
        resolve_in_conn(conn, subscription_id, now=now)
        ent = _entitlement(conn, subscription_id, code, lock=lock)
    return ent


def _consumed(conn: Connection, subscription_id: str, meter_code: str,
              pkey: str) -> int:
    row = conn.execute(select(schema.quota_consumption.c.consumed).where(
        (schema.quota_consumption.c.subscription_id == subscription_id)
        & (schema.quota_consumption.c.meter_code == meter_code)
        & (schema.quota_consumption.c.period_key == pkey))).first()
    return row[0] if row else 0


def _reserved(conn: Connection, subscription_id: str, meter_code: str,
              pkey: str) -> int:
    row = conn.execute(select(func.coalesce(
        func.sum(schema.quota_reservations.c.quantity_reserved), 0)).where(
        (schema.quota_reservations.c.subscription_id == subscription_id)
        & (schema.quota_reservations.c.meter_code == meter_code)
        & (schema.quota_reservations.c.period_key == pkey)
        & (schema.quota_reservations.c.state == "RESERVED"))).first()
    return int(row[0] or 0)


def _time_status(sub, now: datetime) -> Optional[str]:
    """Return a reason code if the subscription's time window blocks use, else
    None. Grace windows yield OK_GRACE via the caller."""
    if sub["is_trial"] and sub["trial_end"] and now > sub["trial_end"]:
        return "TRIAL_LIMIT_REACHED"
    if sub["term_end"] and now > sub["term_end"]:
        if sub["grace_until"] and now <= sub["grace_until"]:
            return "OK_GRACE"
        return "LICENSE_EXPIRED"
    if sub["grace_until"] and sub["state"] in _WARM_STATES \
            and now <= sub["grace_until"]:
        return "OK_GRACE"
    return None


def _log(conn: Connection, tenant_id, sub_id, principal, code, mode, qty, d):
    conn.execute(schema.entitlement_decision_log.insert().values(
        id=schema.new_id(), tenant_id=tenant_id, subscription_id=sub_id,
        principal=(principal or "")[:128], code=code, mode=mode,
        quantity=qty, decision=d.decision, reason_code=d.reason_code,
        at=schema.utcnow()))


def check_access(engine: Engine, *, tenant_id: str, code: str,
                 quantity: int = 1, current_usage: int = 0,
                 mode: Mode = Mode.CHECK, reservation_id: Optional[str] = None,
                 idempotency_key: Optional[str] = None,
                 job_ref: Optional[str] = None, principal: str = "",
                 ttl_minutes: int = DEFAULT_RESERVATION_TTL_MIN,
                 expected_tenant: Optional[str] = None,
                 now: Optional[datetime] = None, log: bool = True) -> Decision:
    """Evaluate (and for RESERVE/COMMIT/RELEASE, mutate) an entitlement.

    - BOOLEAN: ``code`` like ``feature.x`` — enabled or DENY(FEATURE_NOT_ENTITLED).
    - NUMERIC_LIMIT: ``limit.x`` — ``current_usage + quantity <= limit`` (the
      standing count lives in the data plane and is passed in).
    - METERED_QUOTA: ``quota.x`` — consumed + reserved + quantity <= limit,
      per billing period; supports RESERVE/COMMIT/RELEASE.
    """
    now = now or schema.utcnow()
    if quantity < 0:
        raise ValidationError("quantity must be >= 0")
    # scope guard: a non-staff principal (e.g. a Phase-5 data-plane instance)
    # must prove the tenant it is asking about is its own — never trust a bare
    # tenant_id from an untrusted caller.
    if expected_tenant is not None and expected_tenant != tenant_id:
        raise ValidationError("tenant scope mismatch")
    with engine.begin() as conn:
        need_lock = mode in (Mode.RESERVE, Mode.COMMIT, Mode.RELEASE)
        sub = _active_subscription(conn, tenant_id, lock=need_lock)
        if sub is None:
            d = Decision("DENY", "NO_ACTIVE_SUBSCRIPTION")
            if log:
                _log(conn, tenant_id, None, principal, code, mode.value,
                     quantity, d)
            return d
        sub_id = sub["id"]

        # RELEASE / COMMIT operate on an existing reservation regardless of the
        # simple gate (they free or settle already-held quota).
        if mode is Mode.RELEASE:
            return _release(conn, tenant_id, sub_id, reservation_id, principal,
                            code, log)
        if mode is Mode.COMMIT:
            return _commit(conn, tenant_id, sub_id, reservation_id, quantity,
                           principal, code, now, log)

        # time window
        grace = False
        tstat = _time_status(sub, now)
        if tstat == "LICENSE_EXPIRED" or tstat == "TRIAL_LIMIT_REACHED":
            d = Decision("DENY", tstat, subscription_id=sub_id)
            if log:
                _log(conn, tenant_id, sub_id, principal, code, mode.value,
                     quantity, d)
            return d
        if tstat == "OK_GRACE":
            grace = True
        # SUSPENDED = read-only: block any consuming/reserving/limit growth
        if sub["state"] in _READONLY_STATES:
            d = Decision("DENY", "SUBSCRIPTION_SUSPENDED", subscription_id=sub_id)
            if log:
                _log(conn, tenant_id, sub_id, principal, code, mode.value,
                     quantity, d)
            return d
        if sub["state"] in _WARM_STATES:
            grace = True

        ent = _fresh_entitlement(conn, sub_id, code, now, lock=need_lock)
        if ent is None:
            d = Decision("DENY", "FEATURE_NOT_ENTITLED", subscription_id=sub_id)
            if log:
                _log(conn, tenant_id, sub_id, principal, code, mode.value,
                     quantity, d)
            return d

        vk = ent["value_kind"]
        ok_word = "ALLOW_GRACE" if grace else "ALLOW"

        if vk == "BOOLEAN":
            enabled = bool(ent["value"].get("enabled"))
            d = (Decision(ok_word, "OK_GRACE" if grace else "OK",
                          subscription_id=sub_id) if enabled
                 else Decision("DENY", "FEATURE_NOT_ENTITLED",
                               subscription_id=sub_id))
        elif vk == "TIER":
            d = Decision(ok_word, "OK_GRACE" if grace else "OK",
                         subscription_id=sub_id,
                         extra={"tier": ent["value"].get("tier")})
        elif vk == "NUMERIC_LIMIT":
            if ent["value"].get("unlimited"):
                d = Decision(ok_word, "OK_GRACE" if grace else "OK",
                             remaining=None, subscription_id=sub_id)
            else:
                limit = int(ent["value"].get("limit") or 0)
                remaining = limit - current_usage
                if current_usage + quantity <= limit:
                    d = Decision(ok_word, "OK_GRACE" if grace else "OK",
                                 remaining=remaining, limit=limit,
                                 subscription_id=sub_id)
                else:
                    d = Decision("DENY", "LIMIT_EXCEEDED", remaining=remaining,
                                 limit=limit, subscription_id=sub_id)
        elif vk == "METERED_QUOTA":
            pkey = period_key(sub["billing_period"], now)
            if ent["value"].get("unlimited"):
                if mode is Mode.RESERVE:
                    return _reserve(conn, tenant_id, sub_id, code, quantity,
                                    pkey, idempotency_key, job_ref, ttl_minutes,
                                    now, grace, principal, log, unlimited=True)
                d = Decision(ok_word, "OK_GRACE" if grace else "OK",
                             remaining=None, subscription_id=sub_id)
            else:
                limit = int(ent["value"].get("limit") or 0)
                used = _consumed(conn, sub_id, code, pkey)
                held = _reserved(conn, sub_id, code, pkey)
                remaining = limit - used - held
                if mode is Mode.RESERVE:
                    return _reserve(conn, tenant_id, sub_id, code, quantity,
                                    pkey, idempotency_key, job_ref, ttl_minutes,
                                    now, grace, principal, log,
                                    remaining=remaining, limit=limit)
                # CHECK
                if quantity <= remaining:
                    d = Decision(ok_word, "OK_GRACE" if grace else "OK",
                                 remaining=remaining, limit=limit,
                                 subscription_id=sub_id)
                else:
                    d = Decision("DENY", "QUOTA_EXHAUSTED", remaining=remaining,
                                 limit=limit, subscription_id=sub_id)
        else:
            d = Decision("DENY", "FEATURE_NOT_ENTITLED", subscription_id=sub_id)

        if log:
            _log(conn, tenant_id, sub_id, principal, code, mode.value,
                 quantity, d)
        return d


def _reserve(conn, tenant_id, sub_id, code, quantity, pkey, idem, job_ref,
             ttl_minutes, now, grace, principal, log, remaining=None,
             limit=None, unlimited=False) -> Decision:
    if not idem:
        raise ValidationError("RESERVE requires an idempotency_key")
    existing = conn.execute(select(schema.quota_reservations).where(
        (schema.quota_reservations.c.tenant_id == tenant_id)
        & (schema.quota_reservations.c.idempotency_key == idem))).mappings() \
        .first()
    if existing:                         # idempotent replay
        if existing["meter_code"] != code:
            # same key, different meter = a client bug that would otherwise
            # bypass the new meter's quota — reject rather than mis-charge.
            raise ValidationError(
                "idempotency_key already used for a different meter")
        return Decision("ALLOW", "OK", reservation_id=existing["id"],
                        subscription_id=sub_id)
    if not unlimited and quantity > (remaining or 0):
        d = Decision("DENY", "QUOTA_EXHAUSTED", remaining=remaining,
                     limit=limit, subscription_id=sub_id)
        if log:
            _log(conn, tenant_id, sub_id, principal, code, "RESERVE", quantity, d)
        return d
    rid = schema.new_id()
    conn.execute(schema.quota_reservations.insert().values(
        id=rid, tenant_id=tenant_id, subscription_id=sub_id, meter_code=code,
        period_key=pkey, quantity_reserved=quantity, state="RESERVED",
        job_ref=job_ref, idempotency_key=idem,
        expires_at=now + timedelta(minutes=ttl_minutes), created_by=principal
        or "system"))
    d = Decision("ALLOW_GRACE" if grace else "ALLOW", "OK",
                 remaining=(None if unlimited else remaining - quantity),
                 reservation_id=rid, subscription_id=sub_id)
    if log:
        _log(conn, tenant_id, sub_id, principal, code, "RESERVE", quantity, d)
    return d


def _commit(conn, tenant_id, sub_id, reservation_id, actual, principal, code,
            now, log) -> Decision:
    if not reservation_id:
        raise ValidationError("COMMIT requires a reservation_id")
    resv = conn.execute(select(schema.quota_reservations).where(
        (schema.quota_reservations.c.id == reservation_id)
        & (schema.quota_reservations.c.tenant_id == tenant_id))).mappings() \
        .first()
    if resv is None:
        return Decision("DENY", "RESERVATION_NOT_FOUND", subscription_id=sub_id)
    if resv["state"] != "RESERVED":
        if resv["state"] == "EXPIRED":
            return Decision("DENY", "RESERVATION_EXPIRED",
                            subscription_id=sub_id)
        return Decision("DENY", "RESERVATION_NOT_FOUND", subscription_id=sub_id)
    reserved = resv["quantity_reserved"]
    cap = int(reserved * (1 + COMMIT_OVERAGE_TOLERANCE)) if reserved else 0
    actual = int(actual)
    if actual < 0:
        raise ValidationError("commit quantity must be >= 0")
    charged = min(actual, max(reserved, cap))     # tolerate small overage
    # move reserved -> consumed for the actual amount
    _add_consumption(conn, tenant_id, sub_id, resv["meter_code"],
                     resv["period_key"], charged)
    conn.execute(schema.quota_reservations.update()
                 .where(schema.quota_reservations.c.id == reservation_id)
                 .values(state="COMMITTED", quantity_committed=charged,
                         updated_at=now))
    d = Decision("ALLOW", "OK", subscription_id=sub_id,
                 extra={"charged": charged, "overage": max(0, actual - reserved)})
    if log:
        _log(conn, tenant_id, sub_id, principal, code, "COMMIT", actual, d)
    return d


def _release(conn, tenant_id, sub_id, reservation_id, principal, code,
             log) -> Decision:
    if not reservation_id:
        raise ValidationError("RELEASE requires a reservation_id")
    res = conn.execute(schema.quota_reservations.update()
                       .where((schema.quota_reservations.c.id == reservation_id)
                              & (schema.quota_reservations.c.tenant_id
                                 == tenant_id)
                              & (schema.quota_reservations.c.state
                                 == "RESERVED"))
                       .values(state="RELEASED", updated_at=schema.utcnow()))
    if res.rowcount != 1:
        return Decision("DENY", "RESERVATION_NOT_FOUND", subscription_id=sub_id)
    d = Decision("ALLOW", "OK", subscription_id=sub_id)
    if log:
        _log(conn, tenant_id, sub_id, principal, code, "RELEASE", 0, d)
    return d


def _add_consumption(conn, tenant_id, sub_id, meter_code, pkey, delta) -> None:
    if delta == 0:
        return
    tbl = schema.quota_consumption
    row = conn.execute(select(tbl).where(
        (tbl.c.subscription_id == sub_id) & (tbl.c.meter_code == meter_code)
        & (tbl.c.period_key == pkey))).mappings().first()
    if row is None:
        try:
            conn.execute(tbl.insert().values(
                id=schema.new_id(), tenant_id=tenant_id, subscription_id=sub_id,
                meter_code=meter_code, period_key=pkey, consumed=delta,
                updated_at=schema.utcnow()))
            return
        except IntegrityError:            # concurrent creator won
            pass
    conn.execute(tbl.update().where(
        (tbl.c.subscription_id == sub_id) & (tbl.c.meter_code == meter_code)
        & (tbl.c.period_key == pkey)).values(
        consumed=tbl.c.consumed + delta, updated_at=schema.utcnow()))


def consume(engine: Engine, *, tenant_id: str, code: str, quantity: int = 1,
            idempotency_key: Optional[str] = None, principal: str = "",
            expected_tenant: Optional[str] = None,
            now: Optional[datetime] = None, log: bool = True) -> Decision:
    """Atomic, idempotent synchronous metered consumption (for events that
    complete inside the request — assessment created, objects assessed, API
    call, report export). RESERVE+COMMIT collapsed into one transaction, so a
    retry with the same idempotency_key never double-counts."""
    now = now or schema.utcnow()
    if quantity < 0:
        raise ValidationError("quantity must be >= 0")
    if expected_tenant is not None and expected_tenant != tenant_id:
        raise ValidationError("tenant scope mismatch")
    with engine.begin() as conn:
        sub = _active_subscription(conn, tenant_id, lock=True)
        if sub is None:
            d = Decision("DENY", "NO_ACTIVE_SUBSCRIPTION")
            if log:
                _log(conn, tenant_id, None, principal, code, "CONSUME",
                     quantity, d)
            return d
        sub_id = sub["id"]
        tstat = _time_status(sub, now)
        if tstat in ("LICENSE_EXPIRED", "TRIAL_LIMIT_REACHED"):
            d = Decision("DENY", tstat, subscription_id=sub_id)
            if log:
                _log(conn, tenant_id, sub_id, principal, code, "CONSUME",
                     quantity, d)
            return d
        if sub["state"] in _READONLY_STATES:
            d = Decision("DENY", "SUBSCRIPTION_SUSPENDED", subscription_id=sub_id)
            if log:
                _log(conn, tenant_id, sub_id, principal, code, "CONSUME",
                     quantity, d)
            return d
        grace = tstat == "OK_GRACE" or sub["state"] in _WARM_STATES

        # idempotent replay: a COMMITTED reservation with this key already
        # counted the consumption.
        if idempotency_key:
            prior = conn.execute(select(schema.quota_reservations).where(
                (schema.quota_reservations.c.tenant_id == tenant_id)
                & (schema.quota_reservations.c.idempotency_key
                   == idempotency_key))).mappings().first()
            if prior:
                if prior["meter_code"] != code:
                    raise ValidationError(
                        "idempotency_key already used for a different meter")
                return Decision("ALLOW", "OK",
                                reservation_id=prior["id"],
                                subscription_id=sub_id)

        e = _fresh_entitlement(conn, sub_id, code, now, lock=True)
        if e is None or e["value_kind"] != "METERED_QUOTA":
            d = Decision("DENY", "FEATURE_NOT_ENTITLED", subscription_id=sub_id)
            if log:
                _log(conn, tenant_id, sub_id, principal, code, "CONSUME",
                     quantity, d)
            return d
        pkey = period_key(sub["billing_period"], now)
        unlimited = bool(e["value"].get("unlimited"))
        if not unlimited:
            limit = int(e["value"].get("limit") or 0)
            used = _consumed(conn, sub_id, code, pkey)
            held = _reserved(conn, sub_id, code, pkey)
            if used + held + quantity > limit:
                d = Decision("DENY", "QUOTA_EXHAUSTED",
                             remaining=limit - used - held, limit=limit,
                             subscription_id=sub_id)
                if log:
                    _log(conn, tenant_id, sub_id, principal, code, "CONSUME",
                         quantity, d)
                return d
        _add_consumption(conn, tenant_id, sub_id, code, pkey, quantity)
        rid = schema.new_id()
        conn.execute(schema.quota_reservations.insert().values(
            id=rid, tenant_id=tenant_id, subscription_id=sub_id,
            meter_code=code, period_key=pkey, quantity_reserved=quantity,
            quantity_committed=quantity, state="COMMITTED",
            idempotency_key=idempotency_key or schema.new_id(),
            expires_at=None, created_by=principal or "system"))
        d = Decision("ALLOW_GRACE" if grace else "ALLOW", "OK",
                     reservation_id=rid, subscription_id=sub_id)
        if log:
            _log(conn, tenant_id, sub_id, principal, code, "CONSUME",
                 quantity, d)
        return d


def sweep_expired_reservations(engine: Engine,
                               now: Optional[datetime] = None) -> int:
    """Release reservations whose TTL has lapsed (crash safety). Returns the
    number expired."""
    now = now or schema.utcnow()
    with engine.begin() as conn:
        res = conn.execute(schema.quota_reservations.update()
                           .where((schema.quota_reservations.c.state
                                   == "RESERVED")
                                  & (schema.quota_reservations.c.expires_at
                                     < now))
                           .values(state="EXPIRED", updated_at=now))
        return res.rowcount or 0


def entitlements_for(engine: Engine, subscription_id: str) -> list:
    with engine.connect() as conn:
        rows = conn.execute(select(schema.entitlements).where(
            schema.entitlements.c.subscription_id == subscription_id)
            .order_by(schema.entitlements.c.code))
        return [dict(r) for r in rows.mappings()]
