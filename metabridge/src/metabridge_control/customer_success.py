"""Customer-success analytics (docs/commercialization/03-domain-model.md §12).

Derives adoption, lifecycle, risk, and a composite health *assessment* from
signals instances already report (usage, heartbeats, statements) — nothing here
requires new data-plane behaviour.

Two deliberate stances:

- **Deterministic rules, not ML.** ``evaluate_risks`` opens risk flags from
  explicit, inspectable conditions checked against real data (overdue invoices,
  expiring licenses, declining usage). ML-based scoring is a post-launch
  enhancement (gap analysis) — deterministic first.
- **A score is a labelled assessment, not a measurement.** Every
  ``HealthScoreSnapshot`` stores the ``formula_version`` that produced it and
  the component ``inputs``, so a score is always explainable and reproducible.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine

from . import audit, schema
from .context import TenantContext
from .errors import NotFoundError, ValidationError

STAGES = ("PROSPECT", "ONBOARDING", "ADOPTING", "EXPANDING", "RENEWING",
          "AT_RISK", "CHURNED")
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_SEVERITY_PENALTY = {"CRITICAL": 40, "HIGH": 25, "MEDIUM": 10, "LOW": 3}
HEALTH_FORMULA = "cs.health/1"


def _require_tenant(conn: Connection, tenant_id: str) -> None:
    if conn.execute(select(schema.tenants.c.id).where(
            schema.tenants.c.id == tenant_id)).first() is None:
        raise NotFoundError(f"tenants:{tenant_id}")


# --------------------------------------------------------------- adoption
def record_adoption_signal(engine: Engine, ctx: TenantContext, tenant_id: str, *,
                           signal_code: str, instance_id: Optional[str] = None,
                           occurred_at=None, evidence_ref: Optional[str] = None) -> dict:
    """Record a discrete adoption fact, once per (tenant, signal_code). A replay
    returns the existing signal — "first X" is recorded exactly once."""
    ctx.require("cs:manage")
    with engine.begin() as conn:
        _require_tenant(conn, tenant_id)
        existing = conn.execute(select(schema.adoption_signals).where(
            (schema.adoption_signals.c.tenant_id == tenant_id)
            & (schema.adoption_signals.c.signal_code == signal_code))) \
            .mappings().first()
        if existing:
            return {"signal_id": existing["id"], "created": False}
        sid = schema.new_id()
        conn.execute(schema.adoption_signals.insert().values(
            id=sid, tenant_id=tenant_id, signal_code=signal_code,
            instance_id=instance_id, occurred_at=occurred_at or schema.utcnow(),
            evidence_ref=evidence_ref, created_by=ctx.user_id))
        audit.record(conn, ctx, action="cs.adoption.signal",
                     resource_type="adoption_signal", resource_id=sid,
                     after={"signal_code": signal_code}, tenant_id=tenant_id)
    return {"signal_id": sid, "created": True}


# --------------------------------------------------------------- lifecycle
def current_stage(engine: Engine, tenant_id: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(select(schema.lifecycle_stage_transitions.c.to_stage)
                           .where(schema.lifecycle_stage_transitions.c.tenant_id
                                  == tenant_id)
                           .order_by(schema.lifecycle_stage_transitions.c.at
                                     .desc())).first()
    return row[0] if row else "PROSPECT"


def transition_stage(engine: Engine, ctx: TenantContext, tenant_id: str, *,
                     to_stage: str, trigger: str = "MANUAL",
                     reason: Optional[str] = None) -> str:
    """Record a lifecycle-stage transition. The journey is intentionally not a
    strict linear machine (a tenant can drop to AT_RISK from anywhere and
    recover), so any known stage is allowed; the history is the record."""
    ctx.require("cs:manage")
    if to_stage not in STAGES:
        raise ValidationError(f"unknown lifecycle stage: {to_stage}")
    if trigger not in ("RULE", "MANUAL"):
        raise ValidationError("trigger must be RULE or MANUAL")
    tid = schema.new_id()
    with engine.begin() as conn:
        _require_tenant(conn, tenant_id)
        frm = conn.execute(select(schema.lifecycle_stage_transitions.c.to_stage)
                           .where(schema.lifecycle_stage_transitions.c.tenant_id
                                  == tenant_id)
                           .order_by(schema.lifecycle_stage_transitions.c.at
                                     .desc())).first()
        from_stage = frm[0] if frm else None
        if from_stage == to_stage:
            raise ValidationError(f"already in stage {to_stage}")
        conn.execute(schema.lifecycle_stage_transitions.insert().values(
            id=tid, tenant_id=tenant_id, from_stage=from_stage,
            to_stage=to_stage, at=schema.utcnow(), trigger=trigger,
            reason=reason, created_by=ctx.user_id))
        audit.record(conn, ctx, action="cs.lifecycle.transition",
                     resource_type="lifecycle_stage", resource_id=tid,
                     before={"stage": from_stage},
                     after={"stage": to_stage, "trigger": trigger},
                     reason=reason, tenant_id=tenant_id)
    return tid


# --------------------------------------------------------------- risk flags
def open_risk_flag(engine: Engine, ctx: TenantContext, tenant_id: str, *,
                   code: str, severity: str = "MEDIUM",
                   detail: Optional[dict] = None,
                   owner_membership_id: Optional[str] = None) -> dict:
    """Open a risk flag, deduped on (tenant, code) while one is still open — a
    persisting condition doesn't pile up duplicate flags."""
    ctx.require("cs:manage")
    if severity not in SEVERITIES:
        raise ValidationError(f"invalid severity: {severity}")
    with engine.begin() as conn:
        _require_tenant(conn, tenant_id)
        existing = conn.execute(select(schema.risk_flags).where(
            (schema.risk_flags.c.tenant_id == tenant_id)
            & (schema.risk_flags.c.code == code)
            & (schema.risk_flags.c.resolved_at.is_(None)))).mappings().first()
        if existing:
            return {"flag_id": existing["id"], "created": False}
        fid = schema.new_id()
        conn.execute(schema.risk_flags.insert().values(
            id=fid, tenant_id=tenant_id, code=code, severity=severity,
            detail=detail, opened_at=schema.utcnow(),
            owner_membership_id=owner_membership_id, created_by=ctx.user_id))
        audit.record(conn, ctx, action="cs.risk.open",
                     resource_type="risk_flag", resource_id=fid,
                     after={"code": code, "severity": severity},
                     tenant_id=tenant_id)
    return {"flag_id": fid, "created": True}


def resolve_risk_flag(engine: Engine, ctx: TenantContext, flag_id: str, *,
                      note: str = "") -> None:
    ctx.require("cs:manage")
    with engine.begin() as conn:
        flag = conn.execute(select(schema.risk_flags).where(
            schema.risk_flags.c.id == flag_id)).mappings().first()
        if flag is None:
            raise NotFoundError(f"risk_flags:{flag_id}")
        if flag["resolved_at"] is not None:
            return
        conn.execute(schema.risk_flags.update().where(
            schema.risk_flags.c.id == flag_id).values(
            resolved_at=schema.utcnow(), updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="cs.risk.resolve",
                     resource_type="risk_flag", resource_id=flag_id,
                     after={"resolved": True}, reason=note or None,
                     tenant_id=flag["tenant_id"])


def _open_flags(conn: Connection, tenant_id: str) -> list:
    return [dict(r) for r in conn.execute(select(schema.risk_flags).where(
        (schema.risk_flags.c.tenant_id == tenant_id)
        & (schema.risk_flags.c.resolved_at.is_(None)))).mappings()]


# --------------------------------------------------------------- risk rules
def _billable_usage_total(engine: Engine, tenant_id: str, start, end) -> int:
    from . import metering
    summ = metering.usage_summary(engine, tenant_id=tenant_id,
                                  period_start=start, period_end=end)
    return sum(int(m["quantity"]) for m in summ["meters"] if m["billable"])


def evaluate_risks(engine: Engine, ctx: TenantContext, tenant_id: str, *,
                   expiry_days: int = 30, decline_ratio: float = 0.5) -> dict:
    """Deterministic risk rules over real data. Opens (deduped) flags for:
    overdue invoices, licenses expiring within ``expiry_days``, and a >50%
    usage decline vs the prior window. Returns the codes opened this run."""
    ctx.require("cs:manage")
    now = schema.utcnow()
    opened = []

    with engine.connect() as conn:
        _require_tenant(conn, tenant_id)
        overdue = conn.execute(select(schema.invoices.c.id).where(
            (schema.invoices.c.tenant_id == tenant_id)
            & (schema.invoices.c.state == "OVERDUE"))).all()
        expiring = conn.execute(select(schema.licenses.c.id,
                                       schema.licenses.c.not_after).where(
            (schema.licenses.c.tenant_id == tenant_id)
            & (schema.licenses.c.state == "ACTIVE")
            & (schema.licenses.c.not_after.isnot(None))
            & (schema.licenses.c.not_after > now)
            & (schema.licenses.c.not_after
               <= now + timedelta(days=expiry_days)))).all()

    if overdue:
        r = open_risk_flag(engine, ctx, tenant_id, code="UNPAID_INVOICE",
                           severity="HIGH",
                           detail={"invoice_count": len(overdue)})
        if r["created"]:
            opened.append("UNPAID_INVOICE")
    if expiring:
        r = open_risk_flag(engine, ctx, tenant_id, code="LICENSE_EXPIRING",
                           severity="MEDIUM",
                           detail={"license_count": len(expiring)})
        if r["created"]:
            opened.append("LICENSE_EXPIRING")

    prior = _billable_usage_total(engine, tenant_id, now - timedelta(days=60),
                                  now - timedelta(days=30))
    recent = _billable_usage_total(engine, tenant_id, now - timedelta(days=30),
                                   now)
    if prior > 0 and recent < prior * decline_ratio:
        r = open_risk_flag(engine, ctx, tenant_id, code="USAGE_DECLINE",
                           severity="MEDIUM",
                           detail={"prior": prior, "recent": recent})
        if r["created"]:
            opened.append("USAGE_DECLINE")
    return {"tenant_id": tenant_id, "opened": opened}


# --------------------------------------------------------------- health score
def compute_health(engine: Engine, ctx: TenantContext, tenant_id: str, *,
                   formula_version: str = HEALTH_FORMULA) -> dict:
    """Compute + persist a health snapshot. Deterministic ``cs.health/1``:
    start at 100, subtract weighted penalties for open risk flags, subtract for
    a usage decline, floor a CHURNED tenant to 0. Inputs are stored so the score
    is explainable."""
    ctx.require("cs:manage")
    now = schema.utcnow()
    stage = current_stage(engine, tenant_id)
    prior = _billable_usage_total(engine, tenant_id, now - timedelta(days=60),
                                  now - timedelta(days=30))
    recent = _billable_usage_total(engine, tenant_id, now - timedelta(days=30),
                                   now)

    sid = schema.new_id()
    with engine.begin() as conn:
        _require_tenant(conn, tenant_id)
        flags = _open_flags(conn, tenant_id)
        by_sev = {}
        penalty = 0
        for f in flags:
            by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
            penalty += _SEVERITY_PENALTY.get(f["severity"], 0)
        declining = prior > 0 and recent < prior * 0.5
        score = 100 - penalty - (15 if declining else 0)
        if stage == "CHURNED":
            score = 0
        elif stage == "AT_RISK":
            score = min(score, 50)
        score = max(0, min(100, score))
        inputs = {"stage": stage, "open_risk_flags": len(flags),
                  "risk_by_severity": by_sev, "usage_prior": prior,
                  "usage_recent": recent, "usage_declining": declining}
        conn.execute(schema.health_score_snapshots.insert().values(
            id=sid, tenant_id=tenant_id, scope="TENANT", scope_id="*",
            score=score, formula_version=formula_version, inputs=inputs,
            computed_at=now, created_by=ctx.user_id))
        audit.record(conn, ctx, action="cs.health.compute",
                     resource_type="health_score_snapshot", resource_id=sid,
                     after={"score": score, "formula_version": formula_version},
                     tenant_id=tenant_id)
    return {"snapshot_id": sid, "score": score,
            "formula_version": formula_version, "inputs": inputs}


# --------------------------------------------------------------- read side
def cs_overview(engine: Engine, ctx: TenantContext, tenant_id: str) -> dict:
    """A CS account view: current stage, latest health score, open risks, and
    adoption signals."""
    ctx.require("cs:read")
    with engine.connect() as conn:
        _require_tenant(conn, tenant_id)
        latest = conn.execute(select(schema.health_score_snapshots)
                              .where(schema.health_score_snapshots.c.tenant_id
                                     == tenant_id)
                              .order_by(schema.health_score_snapshots.c
                                        .computed_at.desc())).mappings().first()
        flags = _open_flags(conn, tenant_id)
        signals = conn.execute(select(schema.adoption_signals.c.signal_code)
                               .where(schema.adoption_signals.c.tenant_id
                                      == tenant_id)).all()
    return {
        "tenant_id": tenant_id,
        "stage": current_stage(engine, tenant_id),
        "health": (None if latest is None
                   else {"score": latest["score"],
                         "formula_version": latest["formula_version"],
                         "computed_at": str(latest["computed_at"]),
                         "inputs": latest["inputs"]}),
        "open_risks": [{"code": f["code"], "severity": f["severity"],
                        "detail": f["detail"]} for f in flags],
        "adoption_signals": sorted(s[0] for s in signals),
    }
