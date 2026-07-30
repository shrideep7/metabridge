"""Backend enforcement points — the thin, named API the data plane calls.

Maps each licensed operation to its entitlement code and the correct check
shape (standing NUMERIC_LIMIT vs per-period METERED_QUOTA vs async
RESERVE/COMMIT/RELEASE), so callers never have to know the entitlement
convention. This is the ONLY surface the product (Phase 5 bridge) needs.

Nine enforced operations (Phase 2 scope):
  users · programs · assessments · assessment objects · AI operations ·
  storage · connectors · API calls · report exports.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.engine import Engine

from . import entitlements as ent
from .entitlements import Decision, Mode

# operation -> (entitlement_code, kind). kind drives which check to run.
RESOURCES = {
    "users":            ("limit.users",            "STANDING"),
    "programs":         ("limit.active_programs",   "STANDING"),
    "storage_gb":       ("limit.storage_gb",        "STANDING"),
    "connectors":       ("limit.connectors",        "STANDING"),
    "environments":     ("limit.environments",      "STANDING"),
    "assessments":      ("quota.assessments",       "METERED"),
    "objects_assessed": ("quota.objects_assessed",  "METERED"),
    "api_calls":        ("quota.api_calls",         "METERED"),
    "report_exports":   ("quota.report_exports",    "METERED"),
    "ai_credits":       ("quota.ai_credits",        "ASYNC"),   # reserve/commit
}


def _resolve(resource: str):
    if resource not in RESOURCES:
        raise KeyError(f"unknown enforced resource: {resource}")
    return RESOURCES[resource]


def check(engine: Engine, *, tenant_id: str, resource: str,
          current_usage: int = 0, quantity: int = 1,
          principal: str = "") -> Decision:
    """Pre-flight check for a STANDING limit (pass the live count in
    ``current_usage``) or a METERED quota (period remaining)."""
    code, _kind = _resolve(resource)
    return ent.check_access(engine, tenant_id=tenant_id, code=code,
                            quantity=quantity, current_usage=current_usage,
                            mode=Mode.CHECK, principal=principal)


def consume(engine: Engine, *, tenant_id: str, resource: str, quantity: int = 1,
            idempotency_key: Optional[str] = None,
            principal: str = "") -> Decision:
    """Consume a METERED quota synchronously and idempotently (assessment
    created, objects assessed, API call, report export)."""
    code, kind = _resolve(resource)
    if kind != "METERED":
        raise ValueError(f"{resource} is not a synchronously-metered resource")
    return ent.consume(engine, tenant_id=tenant_id, code=code,
                       quantity=quantity, idempotency_key=idempotency_key,
                       principal=principal)


# --- async AI operations: reserve -> run -> commit/release -----------------
def reserve_ai(engine: Engine, *, tenant_id: str, quantity: int,
               idempotency_key: str, job_ref: str = "",
               ttl_minutes: int = ent.DEFAULT_RESERVATION_TTL_MIN,
               principal: str = "") -> Decision:
    code, _ = _resolve("ai_credits")
    return ent.check_access(engine, tenant_id=tenant_id, code=code,
                            quantity=quantity, mode=Mode.RESERVE,
                            idempotency_key=idempotency_key, job_ref=job_ref,
                            ttl_minutes=ttl_minutes, principal=principal)


def commit_ai(engine: Engine, *, tenant_id: str, reservation_id: str,
              actual: int, principal: str = "") -> Decision:
    code, _ = _resolve("ai_credits")
    return ent.check_access(engine, tenant_id=tenant_id, code=code,
                            quantity=actual, mode=Mode.COMMIT,
                            reservation_id=reservation_id, principal=principal)


def release_ai(engine: Engine, *, tenant_id: str, reservation_id: str,
               principal: str = "") -> Decision:
    code, _ = _resolve("ai_credits")
    return ent.check_access(engine, tenant_id=tenant_id, code=code,
                            mode=Mode.RELEASE, reservation_id=reservation_id,
                            principal=principal)


# --- boolean feature gate --------------------------------------------------
def feature_enabled(engine: Engine, *, tenant_id: str,
                    feature_code: str, principal: str = "") -> bool:
    if not feature_code.startswith("feature."):
        feature_code = "feature." + feature_code
    return ent.check_access(engine, tenant_id=tenant_id, code=feature_code,
                            mode=Mode.CHECK, principal=principal).allowed
