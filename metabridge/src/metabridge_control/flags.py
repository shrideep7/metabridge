"""Tenant-aware feature flags — fail-closed, deterministic rollout.

Semantics mirror the verified product flag service
(src/metabridge/platform/flags.py):

- **Fail closed**: unknown key → off; malformed value → off; only a strict
  boolean True enables.
- **Deterministic rollout**: bucket = SHA-256(key | subject) % 100 — never
  random, so evaluation is reproducible and testable.
- **Precedence**: a tenant-specific row overrides the global default row
  (tenant sentinel ``'*'``).

Writes are audited in the same transaction.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from . import audit, schema
from .context import TenantContext
from .errors import ValidationError

GLOBAL = schema.GLOBAL_TENANT


def _bucket(key: str, subject: str) -> int:
    h = hashlib.sha256(f"{key}|{subject}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 100


def set_flag(engine: Engine, ctx: TenantContext, key: str, *,
             enabled: bool, rollout_pct: int = 100,
             allowed_roles: Optional[list] = None,
             description: str = "", tenant_scope: Optional[str] = None) -> None:
    """Create or update a flag. ``tenant_scope`` defaults to the caller's
    tenant; staff may write the global default by passing GLOBAL explicitly."""
    ctx.require("flags:manage")
    scope = tenant_scope or ctx.tenant_id
    if scope != ctx.tenant_id and scope != GLOBAL:
        # customers can only ever write their own tenant's overrides
        raise ValidationError("cannot write flags for another tenant")
    if scope == GLOBAL and ctx.actor_type != "STAFF":
        raise ValidationError("global flag defaults are staff-only")
    if not isinstance(enabled, bool):
        raise ValidationError("enabled must be a strict boolean")
    rollout_pct = int(rollout_pct)
    if not 0 <= rollout_pct <= 100:
        raise ValidationError("rollout_pct must be 0..100")
    if allowed_roles is not None and not (
            isinstance(allowed_roles, list)
            and all(isinstance(r, str) for r in allowed_roles)):
        raise ValidationError("allowed_roles must be a list of role codes")

    tbl = schema.feature_flags
    with engine.begin() as conn:
        row = conn.execute(select(tbl).where(
            (tbl.c.key == key) & (tbl.c.tenant_id == scope))).mappings().first()
        after = {"key": key, "tenant_id": scope, "enabled": enabled,
                 "rollout_pct": rollout_pct, "allowed_roles": allowed_roles}
        if row is None:
            conn.execute(tbl.insert().values(
                id=schema.new_id(), key=key, tenant_id=scope, enabled=enabled,
                rollout_pct=rollout_pct, allowed_roles=allowed_roles,
                description=description, created_by=ctx.user_id))
            before = None
        else:
            conn.execute(tbl.update().where(tbl.c.id == row["id"]).values(
                enabled=enabled, rollout_pct=rollout_pct,
                allowed_roles=allowed_roles,
                description=description or row["description"],
                updated_at=schema.utcnow()))
            before = {"enabled": row["enabled"],
                      "rollout_pct": row["rollout_pct"],
                      "allowed_roles": row["allowed_roles"]}
        audit.record(conn, ctx, action="flag.set", resource_type="feature_flag",
                     resource_id=f"{scope}:{key}", before=before, after=after,
                     tenant_id=scope if scope != GLOBAL else GLOBAL)


def is_enabled(conn: Connection, key: str, *, tenant_id: str,
               subject: str = "", role: str = "") -> bool:
    """Evaluate a flag for a tenant (and optional subject/role). Fail-closed
    on every ambiguous input."""
    tbl = schema.feature_flags
    rows = conn.execute(select(tbl).where(
        (tbl.c.key == key)
        & (tbl.c.tenant_id.in_([tenant_id, GLOBAL])))).mappings().all()
    if not rows:
        return False
    # tenant-specific row wins over the global default
    row = next((r for r in rows if r["tenant_id"] == tenant_id), None) \
        or next((r for r in rows if r["tenant_id"] == GLOBAL), None)
    if row is None:
        return False
    if row["enabled"] is not True:          # strict boolean, fail closed
        return False
    roles = row["allowed_roles"]
    if roles is not None:                    # [] means "no role" -> deny all
        if not isinstance(roles, list) or role not in roles:
            return False
    pct = row["rollout_pct"]
    if not isinstance(pct, int):
        return False
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    return _bucket(key, subject or tenant_id) < pct
