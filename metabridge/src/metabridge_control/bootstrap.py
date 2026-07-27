"""Bootstrap a control-plane tenant from an existing product instance.

Reuses the instance's existing identity data (``users.json`` written by
``web/auth.py``) **read-only** — the product's files are never modified, and
re-running is safe (get-or-create everywhere). This is how an existing
customer instance is enrolled without invalidating any production data:

    Tenant (slug you choose)
      └─ Organization  'default'
           └─ BusinessUnit 'General'
                └─ Workspace 'primary'   (the instance's commercial shadow)
                     └─ Environment 'production'

Every instance user becomes a control-plane user; product roles map onto the
customer role ladder (owner→cp_owner, admin→cp_admin, engineer→cp_member,
viewer→cp_viewer).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine

from . import rbac, schema, tenancy
from .context import TenantContext
from .errors import ValidationError


def read_instance_users(auth_dir: str) -> list:
    """Read (email, product_role, display_name) from an instance's
    users.json. Read-only; tolerates missing file (returns [])."""
    path = Path(auth_dir) / "users.json"
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"unreadable users.json: {exc}")
    out = []
    for email, rec in sorted(doc.items()):
        if not isinstance(rec, dict):
            continue
        out.append((email.strip().lower(), str(rec.get("role", "viewer")),
                    str(rec.get("name", "") or email)))
    return out


def _system_ctx(tenant_id: str) -> TenantContext:
    """Internal SYSTEM context used only by bootstrap; carries the owner
    permission set so the standard audited services can be reused."""
    return TenantContext(tenant_id=tenant_id, user_id="bootstrap",
                         role="cp_owner",
                         permissions=rbac.permissions_for("cp_owner"),
                         actor_type="SYSTEM")


def bootstrap_from_instance(engine: Engine, *, auth_dir: str,
                            tenant_slug: str,
                            legal_name: Optional[str] = None,
                            home_region: Optional[str] = None) -> dict:
    """Idempotently enroll one existing instance as a tenant. Returns ids."""
    with engine.connect() as conn:
        existing = conn.execute(select(schema.tenants).where(
            schema.tenants.c.slug == tenant_slug.strip().lower())) \
            .mappings().first()
    if existing:
        tenant_id = existing["id"]
    else:
        tenant_id = tenancy.create_tenant(
            engine, slug=tenant_slug,
            legal_name=legal_name or tenant_slug, home_region=home_region,
            system=True)
    ctx = _system_ctx(tenant_id)

    def _get_or_create(table, where_clause, create):
        """Look up by the row's TRUE uniqueness scope (parent + key) so a
        re-run binds to the node created in this run, never a namesake."""
        with engine.connect() as conn:
            row = conn.execute(select(table).where(
                (table.c.tenant_id == tenant_id) & where_clause)) \
                .mappings().first()
        return row["id"] if row else create()

    org_id = _get_or_create(
        schema.organizations, schema.organizations.c.slug == "default",
        lambda: tenancy.create_organization(engine, ctx, slug="default",
                                            name="Default organization"))
    bu_id = _get_or_create(
        schema.business_units,
        (schema.business_units.c.organization_id == org_id)
        & (schema.business_units.c.name == "General"),
        lambda: tenancy.create_business_unit(engine, ctx, org_id,
                                             name="General"))
    ws_id = _get_or_create(
        schema.workspaces,
        (schema.workspaces.c.business_unit_id == bu_id)
        & (schema.workspaces.c.slug == "primary"),
        lambda: tenancy.create_workspace(engine, ctx, bu_id, slug="primary",
                                         name="Primary workspace"))
    env_id = _get_or_create(
        schema.environments,
        (schema.environments.c.workspace_id == ws_id)
        & (schema.environments.c.name == "production"),
        lambda: tenancy.create_environment(engine, ctx, ws_id,
                                           name="production", kind="PROD",
                                           region=home_region))

    imported = 0
    for email, product_role, display in read_instance_users(auth_dir):
        user_id = tenancy.create_user(engine, email=email,
                                      display_name=display, system=True)
        with engine.connect() as conn:
            has = conn.execute(select(schema.memberships.c.id).where(
                (schema.memberships.c.tenant_id == tenant_id)
                & (schema.memberships.c.user_id == user_id))).first()
        if not has:
            tenancy.add_membership(engine, ctx, user_id=user_id,
                                   role_code=rbac.map_product_role(
                                       product_role))
            imported += 1

    return {"tenant_id": tenant_id, "organization_id": org_id,
            "business_unit_id": bu_id, "workspace_id": ws_id,
            "environment_id": env_id, "members_imported": imported}
