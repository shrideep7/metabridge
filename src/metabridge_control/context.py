"""Tenant-context resolution and tenant-isolation enforcement.

The load-bearing rule (docs/commercialization/02-target-architecture.md):
**never trust a client-supplied tenant identifier**. A TenantContext can only
be produced by ``resolve_context``, which derives the tenant from the
authenticated user's ACTIVE membership — the requested tenant is an *input to
verification*, not a source of authority.

Isolation is enforced at the repository layer: every scoped read/write goes
through the helpers here, which always filter by ``ctx.tenant_id``. A row that
exists under another tenant is indistinguishable from a row that does not
exist (no existence oracle).
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
import uuid

from sqlalchemy import select, update as sa_update, delete as sa_delete
from sqlalchemy.engine import Connection

from . import rbac, schema
from .errors import NotFoundError, TenantAccessDenied


@dataclass(frozen=True)
class TenantContext:
    """Proof of an authorized actor inside one tenant."""
    tenant_id: str
    user_id: str
    role: str
    permissions: frozenset = field(default_factory=frozenset)
    correlation_id: str = ""
    actor_type: str = "USER"          # USER | STAFF | SYSTEM

    def require(self, permission: str) -> None:
        rbac.require(self.role, permission)


_current: ContextVar[Optional[TenantContext]] = ContextVar(
    "metabridge_control_ctx", default=None)


def set_current(ctx: Optional[TenantContext]):
    return _current.set(ctx)


def get_current() -> Optional[TenantContext]:
    return _current.get()


def resolve_context(conn: Connection, *, user_id: str = "", email: str = "",
                    tenant_id: str, correlation_id: str = "") -> TenantContext:
    """Derive an authorized TenantContext or raise TenantAccessDenied.

    Verifies, in order: the user exists and is ACTIVE; the tenant exists and
    is usable (not SUSPENDED / OFFBOARDED); the user holds an ACTIVE
    membership *in that tenant*. The membership's role — never anything the
    client sent — determines permissions.
    """
    u = None
    if user_id:
        u = conn.execute(select(schema.users).where(
            schema.users.c.id == user_id)).mappings().first()
    elif email:
        u = conn.execute(select(schema.users).where(
            schema.users.c.email == email.strip().lower())).mappings().first()
    if u is None or u["status"] != "ACTIVE":
        raise TenantAccessDenied("USER_NOT_ACTIVE")

    t = conn.execute(select(schema.tenants).where(
        schema.tenants.c.id == tenant_id)).mappings().first()
    if t is None:
        raise TenantAccessDenied("TENANT_NOT_FOUND")
    if t["status"] in ("SUSPENDED", "OFFBOARDED"):
        raise TenantAccessDenied("TENANT_" + t["status"])

    m = conn.execute(select(schema.memberships).where(
        (schema.memberships.c.tenant_id == tenant_id)
        & (schema.memberships.c.user_id == u["id"]))).mappings().first()
    if m is None or m["state"] != "ACTIVE":
        raise TenantAccessDenied("NO_ACTIVE_MEMBERSHIP")

    role = m["role_code"]
    return TenantContext(
        tenant_id=tenant_id, user_id=u["id"], role=role,
        permissions=rbac.permissions_for(role),
        correlation_id=correlation_id or uuid.uuid4().hex,
        actor_type="USER",
    )


def staff_context(role: str, actor_id: str, tenant_id: str = schema.GLOBAL_TENANT,
                  correlation_id: str = "") -> TenantContext:
    """Context for vendor staff operating the control plane.

    Staff roles are the least-privilege set in rbac.STAFF_PERMISSIONS; an
    unknown role fails closed to zero permissions.
    """
    return TenantContext(
        tenant_id=tenant_id, user_id=actor_id, role=role,
        permissions=rbac.permissions_for(role),
        correlation_id=correlation_id or uuid.uuid4().hex,
        actor_type="STAFF",
    )


# ---------------------------------------------------------------------------
# Repository-layer isolation helpers — the only sanctioned way for services to
# touch tenant-scoped tables.
# ---------------------------------------------------------------------------
def scoped_select(table, ctx: TenantContext):
    """A SELECT pre-filtered to the caller's tenant."""
    return select(table).where(table.c.tenant_id == ctx.tenant_id)


def fetch_scoped(conn: Connection, table, ctx: TenantContext,
                 row_id: str) -> Optional[Mapping[str, Any]]:
    """Fetch one row by id *within the tenant*; other tenants' rows read as
    absent."""
    return conn.execute(
        select(table).where((table.c.id == row_id)
                            & (table.c.tenant_id == ctx.tenant_id))
    ).mappings().first()


def require_scoped(conn: Connection, table, ctx: TenantContext,
                   row_id: str) -> Mapping[str, Any]:
    row = fetch_scoped(conn, table, ctx, row_id)
    if row is None:
        raise NotFoundError(f"{table.name}:{row_id}")
    return row


def update_scoped(conn: Connection, table, ctx: TenantContext, row_id: str,
                  values: dict) -> None:
    """Tenant-guarded UPDATE; refuses to touch rows outside the tenant."""
    res = conn.execute(
        sa_update(table)
        .where((table.c.id == row_id) & (table.c.tenant_id == ctx.tenant_id))
        .values(**values))
    if res.rowcount != 1:
        raise NotFoundError(f"{table.name}:{row_id}")


def delete_scoped(conn: Connection, table, ctx: TenantContext,
                  row_id: str) -> None:
    res = conn.execute(
        sa_delete(table)
        .where((table.c.id == row_id) & (table.c.tenant_id == ctx.tenant_id)))
    if res.rowcount != 1:
        raise NotFoundError(f"{table.name}:{row_id}")
