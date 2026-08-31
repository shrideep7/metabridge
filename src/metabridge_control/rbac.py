"""Control-plane RBAC — roles and the permissions they carry.

Two role families:

- **Customer roles** (held via a Membership inside a tenant). The codes mirror
  the product's proven owner/admin/engineer/viewer ladder (web/auth.py) as a
  *convention* — no code is shared across the planes.
- **Staff roles** (vendor operators of the control plane). Least-privilege
  split per the commercialization mandate §9.1.

Roles are code-defined in Phase 1 and treated as configuration; a DB-backed
role editor is a later-phase concern. Permission checks are exact-match with
a single ``*`` wildcard (SUPER_ADMIN) — no pattern grammar to get wrong.
"""
from __future__ import annotations

from .errors import PermissionDenied

# ---------------------------------------------------------------- customer
CUSTOMER_PERMISSIONS: dict[str, frozenset] = {
    "cp_owner": frozenset({
        "org:manage", "workspace:manage", "members.view", "members.invite",
        "members.update", "members.role_change", "members.remove",
        "members.deactivate", "members.activate",
        "flags:manage", "flags:read", "audit:read", "tenant:read",
    }),
    "cp_admin": frozenset({
        "org:manage", "workspace:manage", "members.view", "members.invite",
        "members.update", "members.role_change", "members.remove",
        "flags:read", "audit:read", "tenant:read",
    }),
    "cp_member": frozenset({
        "workspace:manage", "flags:read", "tenant:read",
    }),
    "cp_viewer": frozenset({
        "flags:read", "tenant:read",
    }),
}

# Customer-role rank for privilege-escalation guards (higher = more power).
# A caller may never grant or revoke a role that outranks their own.
CUSTOMER_ROLE_RANK = {
    "cp_viewer": 1, "cp_member": 2, "cp_admin": 3, "cp_owner": 4,
}


def customer_rank(role_code: str) -> int:
    return CUSTOMER_ROLE_RANK.get(role_code, 0)


# product role (data plane) -> control-plane customer role
PRODUCT_ROLE_MAP = {
    "owner": "cp_owner",
    "admin": "cp_admin",
    "engineer": "cp_member",
    "member": "cp_member",
    "viewer": "cp_viewer",
}

# ------------------------------------------------------------------- staff
_READ_ONLY = frozenset({
    "tenant:read", "audit:read", "flags:read", "catalog:read", "usage:read",
    "pricing:read", "billing:read", "partners:read", "cs:read", "ai:read",
})
STAFF_PERMISSIONS: dict[str, frozenset] = {
    "SUPER_ADMIN": frozenset({"*"}),
    "COMMERCIAL_ADMIN": frozenset({
        "tenant:manage", "tenant:read", "catalog:manage", "catalog:read",
        "plans:publish", "flags:manage", "flags:read", "audit:read",
        "subscriptions:manage", "subscriptions:read",
        "licenses:issue", "licenses:manage",
        "entitlements:override", "usage:read", "access:check",
        "usage:write", "usage:adjust", "usage:ingest",
        "instances:manage",   # Phase 5: enroll/manage data-plane instances
        # Phase 4: may build rate cards, run rating, and request a below-floor
        # override — but NOT approve it (segregation of duties, §8.1).
        "pricing:manage", "pricing:read", "rating:run",
        "billing:manage", "billing:read",
        # Phase 8: manages partners/deals and ACCRUES commissions — but does
        # NOT approve/pay them (SoD; finance approves, §9.3).
        "partners:manage", "partners:read", "commissions:manage",
        "cs:manage", "cs:read",   # customer-success analytics
        "ai:manage", "ai:read",   # AI cost governance (rate cards, budgets)
    }),
    "FINANCE_ADMIN": frozenset({
        "tenant:read", "catalog:read", "usage:read", "audit:read",
        "subscriptions:read", "usage:adjust",
        # Phase 4: finance owns rate cards, approves below-floor overrides
        # (the second party to the requester), and operates billing.
        "pricing:manage", "pricing:read", "pricing:approve_override",
        "rating:run", "billing:manage", "billing:read",
        # Phase 8: finance approves/pays commissions (the second party to the
        # commercial admin who accrued them) and can clawback; sees AI spend.
        "partners:read", "commissions:manage", "commissions:approve",
        "ai:read",
    }),
    "PARTNER_ADMIN": frozenset({"tenant:read", "catalog:read",
                                "partners:manage", "partners:read"}),
    "SUPPORT_ADMIN": frozenset({
        "tenant:read", "flags:read", "audit:read", "subscriptions:read",
        "access:check", "cs:manage", "cs:read",   # CS is a support function
    }),
    "SECURITY_ADMIN": frozenset({
        "tenant:read", "audit:read", "flags:manage", "flags:read",
    }),
    "READ_ONLY_AUDITOR": _READ_ONLY | frozenset({"subscriptions:read"}),
}

ALL_ROLES = {**CUSTOMER_PERMISSIONS, **STAFF_PERMISSIONS}


def permissions_for(role_code: str) -> frozenset:
    """Permission set for a role. Unknown roles fail closed (empty set)."""
    return ALL_ROLES.get(role_code, frozenset())


def has_permission(role_code: str, permission: str) -> bool:
    perms = permissions_for(role_code)
    return "*" in perms or permission in perms


def require(role_code: str, permission: str) -> None:
    """Raise PermissionDenied unless the role carries the permission."""
    if not has_permission(role_code, permission):
        raise PermissionDenied(permission, role_code)


def map_product_role(product_role: str) -> str:
    """Map a data-plane role to its control-plane customer role (fail-closed
    to viewer for anything unrecognized)."""
    return PRODUCT_ROLE_MAP.get((product_role or "").lower(), "cp_viewer")
