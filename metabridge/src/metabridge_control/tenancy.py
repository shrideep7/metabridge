"""Tenancy & identity services — tenants, orgs, business units, workspaces,
environments, users and memberships.

Rules enforced here (docs/commercialization/03-domain-model.md §3):

- IDs are server-generated UUIDs; slugs validated; emails lowercased.
- Every child row inherits ``tenant_id`` from its verified parent — never
  from the caller's input.
- Every commercial mutation is audited in the same transaction.
- Cross-tenant references are impossible: parents are looked up through the
  tenant-scoped repository helpers, so a foreign parent reads as absent.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from . import audit, rbac, schema
from .context import (TenantContext, fetch_scoped, require_scoped,
                      scoped_select, update_scoped)
from .errors import NotFoundError, PermissionDenied, ValidationError

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TENANT_STATUSES = ("PROSPECT", "ACTIVE", "SUSPENDED", "OFFBOARDED")


def _slug(value: str, what: str) -> str:
    v = (value or "").strip().lower()
    if not _SLUG_RE.match(v):
        raise ValidationError(f"invalid {what} slug: {value!r}")
    return v


def _require_provisioning(staff_ctx: Optional[TenantContext], system: bool,
                          permission: str) -> str:
    """Gate a privileged provisioning primitive. Either an explicit internal
    ``system`` caller (bootstrap/migrations) or a staff context holding
    ``permission``. Returns the actor id for auditing."""
    if system:
        return "system"
    if staff_ctx is None or staff_ctx.actor_type != "STAFF":
        raise PermissionDenied(permission, staff_ctx.role if staff_ctx else "-")
    staff_ctx.require(permission)
    return staff_ctx.user_id


# ------------------------------------------------------------------ tenants
def create_tenant(engine: Engine, *, slug: str, legal_name: str,
                  home_region: Optional[str] = None,
                  staff_ctx: Optional[TenantContext] = None,
                  system: bool = False) -> str:
    actor = _require_provisioning(staff_ctx, system, "tenant:manage")
    slug = _slug(slug, "tenant")
    if not (legal_name or "").strip():
        raise ValidationError("legal_name is required")
    tid = schema.new_id()
    with engine.begin() as conn:
        dup = conn.execute(select(schema.tenants.c.id).where(
            schema.tenants.c.slug == slug)).first()
        if dup:
            raise ValidationError(f"tenant slug already exists: {slug}")
        try:
            conn.execute(schema.tenants.insert().values(
                id=tid, slug=slug, legal_name=legal_name.strip(),
                status="ACTIVE", home_region=home_region, created_by=actor))
        except IntegrityError:  # lost race on the unique slug
            raise ValidationError(f"tenant slug already exists: {slug}")
        audit.record(conn, None, action="tenant.create",
                     resource_type="tenant", resource_id=tid,
                     after={"slug": slug, "legal_name": legal_name,
                            "home_region": home_region},
                     tenant_id=tid, actor_type="SYSTEM", actor_id=actor)
    return tid


def set_tenant_status(engine: Engine, staff_ctx: TenantContext,
                      tenant_id: str, status: str) -> None:
    """Staff-only lifecycle transition (suspend / reactivate / offboard)."""
    staff_ctx.require("tenant:manage")
    if status not in TENANT_STATUSES:
        raise ValidationError(f"invalid tenant status: {status}")
    with engine.begin() as conn:
        row = conn.execute(select(schema.tenants).where(
            schema.tenants.c.id == tenant_id)).mappings().first()
        if row is None:
            raise NotFoundError(f"tenants:{tenant_id}")
        conn.execute(schema.tenants.update()
                     .where(schema.tenants.c.id == tenant_id)
                     .values(status=status, updated_at=schema.utcnow()))
        audit.record(conn, staff_ctx, action="tenant.status",
                     resource_type="tenant", resource_id=tenant_id,
                     before={"status": row["status"]},
                     after={"status": status}, tenant_id=tenant_id)


def get_tenant(engine: Engine, ctx: TenantContext,
               tenant_id: str) -> Optional[dict]:
    """Read a tenant row. A customer may only read their own tenant; staff
    with ``tenant:read`` may read any. A foreign tenant reads as absent
    (no existence oracle)."""
    is_staff = ctx.actor_type == "STAFF" and rbac.has_permission(
        ctx.role, "tenant:read")
    if not is_staff and ctx.tenant_id != tenant_id:
        return None
    with engine.connect() as conn:
        row = conn.execute(select(schema.tenants).where(
            schema.tenants.c.id == tenant_id)).mappings().first()
        return dict(row) if row else None


# ------------------------------------------------------------ organizations
def create_organization(engine: Engine, ctx: TenantContext, *, slug: str,
                        name: str, country_code: Optional[str] = None) -> str:
    ctx.require("org:manage")
    slug = _slug(slug, "organization")
    oid = schema.new_id()
    with engine.begin() as conn:
        dup = conn.execute(scoped_select(schema.organizations, ctx).where(
            schema.organizations.c.slug == slug)).first()
        if dup:
            raise ValidationError(f"organization slug exists: {slug}")
        conn.execute(schema.organizations.insert().values(
            id=oid, tenant_id=ctx.tenant_id, slug=slug, name=name,
            country_code=(country_code or None), created_by=ctx.user_id))
        audit.record(conn, ctx, action="organization.create",
                     resource_type="organization", resource_id=oid,
                     after={"slug": slug, "name": name})
    return oid


def list_organizations(engine: Engine, ctx: TenantContext) -> list:
    with engine.connect() as conn:
        rows = conn.execute(scoped_select(schema.organizations, ctx)
                            .order_by(schema.organizations.c.slug))
        return [dict(r) for r in rows.mappings()]


# ------------------------------------------------------------ business units
def create_business_unit(engine: Engine, ctx: TenantContext,
                         organization_id: str, *, name: str,
                         cost_center_code: Optional[str] = None) -> str:
    ctx.require("org:manage")
    if not (name or "").strip():
        raise ValidationError("business unit name is required")
    bid = schema.new_id()
    with engine.begin() as conn:
        require_scoped(conn, schema.organizations, ctx, organization_id)
        conn.execute(schema.business_units.insert().values(
            id=bid, tenant_id=ctx.tenant_id, organization_id=organization_id,
            name=name.strip(), cost_center_code=cost_center_code,
            created_by=ctx.user_id))
        audit.record(conn, ctx, action="business_unit.create",
                     resource_type="business_unit", resource_id=bid,
                     after={"name": name, "organization_id": organization_id})
    return bid


# ---------------------------------------------------------------- workspaces
def create_workspace(engine: Engine, ctx: TenantContext,
                     business_unit_id: str, *, slug: str, name: str,
                     purpose: str = "PRODUCTION") -> str:
    ctx.require("workspace:manage")
    slug = _slug(slug, "workspace")
    if purpose not in ("PRODUCTION", "PROJECT", "SANDBOX"):
        raise ValidationError(f"invalid workspace purpose: {purpose}")
    wid = schema.new_id()
    with engine.begin() as conn:
        require_scoped(conn, schema.business_units, ctx, business_unit_id)
        conn.execute(schema.workspaces.insert().values(
            id=wid, tenant_id=ctx.tenant_id,
            business_unit_id=business_unit_id, slug=slug, name=name,
            purpose=purpose, created_by=ctx.user_id))
        audit.record(conn, ctx, action="workspace.create",
                     resource_type="workspace", resource_id=wid,
                     after={"slug": slug, "name": name, "purpose": purpose})
    return wid


def get_workspace(engine: Engine, ctx: TenantContext,
                  workspace_id: str) -> Optional[dict]:
    with engine.connect() as conn:
        row = fetch_scoped(conn, schema.workspaces, ctx, workspace_id)
        return dict(row) if row else None


def list_workspaces(engine: Engine, ctx: TenantContext) -> list:
    with engine.connect() as conn:
        rows = conn.execute(scoped_select(schema.workspaces, ctx)
                            .order_by(schema.workspaces.c.slug))
        return [dict(r) for r in rows.mappings()]


def rename_workspace(engine: Engine, ctx: TenantContext, workspace_id: str,
                     *, name: str) -> None:
    ctx.require("workspace:manage")
    if not (name or "").strip():
        raise ValidationError("workspace name is required")
    with engine.begin() as conn:
        before = require_scoped(conn, schema.workspaces, ctx, workspace_id)
        update_scoped(conn, schema.workspaces, ctx, workspace_id,
                      {"name": name.strip(), "updated_at": schema.utcnow()})
        audit.record(conn, ctx, action="workspace.rename",
                     resource_type="workspace", resource_id=workspace_id,
                     before={"name": before["name"]}, after={"name": name})


# --------------------------------------------------------------- environments
def create_environment(engine: Engine, ctx: TenantContext, workspace_id: str,
                       *, name: str, kind: str = "PROD",
                       region: Optional[str] = None) -> str:
    ctx.require("workspace:manage")
    if kind not in ("PROD", "NONPROD", "DR", "ENCLAVE"):
        raise ValidationError(f"invalid environment kind: {kind}")
    eid = schema.new_id()
    with engine.begin() as conn:
        require_scoped(conn, schema.workspaces, ctx, workspace_id)
        if region:
            known = conn.execute(select(schema.regions.c.code).where(
                schema.regions.c.code == region)).first()
            if not known:
                raise ValidationError(f"unknown region: {region}")
        conn.execute(schema.environments.insert().values(
            id=eid, tenant_id=ctx.tenant_id, workspace_id=workspace_id,
            name=name, kind=kind, region=region, created_by=ctx.user_id))
        audit.record(conn, ctx, action="environment.create",
                     resource_type="environment", resource_id=eid,
                     after={"name": name, "kind": kind, "region": region})
    return eid


# --------------------------------------------------------- users & memberships
def create_user(engine: Engine, *, email: str, display_name: str = "",
                ctx: Optional[TenantContext] = None,
                system: bool = False) -> str:
    """Get-or-create a control-plane user (idempotent by email).

    Privileged: requires ``system`` (bootstrap) or a context carrying
    ``member:manage`` (a customer admin inviting a user). Race-safe: a
    concurrent creator's row is returned rather than surfacing IntegrityError.
    """
    if not system:
        if ctx is None or not rbac.has_permission(ctx.role, "member:manage"):
            raise PermissionDenied("member:manage",
                                   ctx.role if ctx else "-")
    actor = "system" if system else ctx.user_id
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValidationError(f"invalid email: {email!r}")
    with engine.begin() as conn:
        existing = conn.execute(select(schema.users.c.id).where(
            schema.users.c.email == email)).first()
        if existing:
            return existing[0]
        uid = schema.new_id()
        try:
            conn.execute(schema.users.insert().values(
                id=uid, email=email, display_name=display_name or email,
                created_by=actor))
        except IntegrityError:            # concurrent creator won the race
            row = conn.execute(select(schema.users.c.id).where(
                schema.users.c.email == email)).first()
            if row:
                return row[0]
            raise
        audit.record(conn, None, action="user.create", resource_type="user",
                     resource_id=uid, after={"email": email},
                     tenant_id=schema.GLOBAL_TENANT, actor_type="SYSTEM",
                     actor_id=actor)
    return uid


def add_membership(engine: Engine, ctx: TenantContext, *, user_id: str,
                   role_code: str) -> str:
    ctx.require("member:manage")
    if role_code not in rbac.CUSTOMER_PERMISSIONS:
        raise ValidationError(f"unknown customer role: {role_code}")
    # privilege-escalation guard: never grant a role that outranks the caller
    if rbac.customer_rank(role_code) > rbac.customer_rank(ctx.role):
        raise PermissionDenied(
            f"grant role '{role_code}' (outranks caller '{ctx.role}')",
            ctx.role)
    mid = schema.new_id()
    with engine.begin() as conn:
        u = conn.execute(select(schema.users).where(
            schema.users.c.id == user_id)).mappings().first()
        if u is None:
            raise NotFoundError(f"users:{user_id}")
        dup = conn.execute(select(schema.memberships.c.id).where(
            (schema.memberships.c.tenant_id == ctx.tenant_id)
            & (schema.memberships.c.user_id == user_id))).first()
        if dup:
            raise ValidationError("user already has a membership in tenant")
        conn.execute(schema.memberships.insert().values(
            id=mid, tenant_id=ctx.tenant_id, user_id=user_id,
            role_code=role_code, state="ACTIVE", created_by=ctx.user_id))
        audit.record(conn, ctx, action="membership.add",
                     resource_type="membership", resource_id=mid,
                     after={"user_id": user_id, "role_code": role_code})
    return mid


def revoke_membership(engine: Engine, ctx: TenantContext,
                      membership_id: str) -> None:
    ctx.require("member:manage")
    with engine.begin() as conn:
        before = require_scoped(conn, schema.memberships, ctx, membership_id)
        target_role = before["role_code"]
        # cannot revoke a membership that outranks you; revoking a peer owner
        # requires being an owner yourself
        if rbac.customer_rank(target_role) > rbac.customer_rank(ctx.role):
            raise PermissionDenied(
                f"revoke role '{target_role}' (outranks caller '{ctx.role}')",
                ctx.role)
        # never leave a tenant without an active owner (lock-out / takeover)
        if target_role == "cp_owner" and before["state"] == "ACTIVE":
            active_owners = conn.execute(
                select(schema.memberships.c.id).where(
                    (schema.memberships.c.tenant_id == ctx.tenant_id)
                    & (schema.memberships.c.role_code == "cp_owner")
                    & (schema.memberships.c.state == "ACTIVE"))).all()
            if len(active_owners) <= 1:
                raise ValidationError(
                    "cannot revoke the last active owner of the tenant")
        update_scoped(conn, schema.memberships, ctx, membership_id,
                      {"state": "REVOKED", "updated_at": schema.utcnow()})
        audit.record(conn, ctx, action="membership.revoke",
                     resource_type="membership", resource_id=membership_id,
                     before={"state": before["state"],
                             "role_code": target_role,
                             "user_id": before["user_id"]},
                     after={"state": "REVOKED"})


def list_memberships(engine: Engine, ctx: TenantContext) -> list:
    with engine.connect() as conn:
        rows = conn.execute(scoped_select(schema.memberships, ctx))
        return [dict(r) for r in rows.mappings()]
