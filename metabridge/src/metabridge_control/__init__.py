"""MetaBridge commercial control plane (Phase 1 — Foundation).

A separate, multi-tenant bounded-context package: tenancy & identity, RBAC,
tamper-evident audit, tenant-aware feature flags, and the versioned product
catalog — backed by SQL (SQLite dev/test, PostgreSQL production) with explicit
migrations.

Deliberately imports **nothing** from the ``metabridge`` product package: the
product stays a single-tenant data plane (its isolation moat), and this
package is extractable into its own service later
(docs/commercialization/02-target-architecture.md).
"""
from __future__ import annotations

__version__ = "0.1.0"

from .errors import (AuditIntegrityError, ControlPlaneError, MigrationError,
                     NotFoundError, PermissionDenied, PlanImmutableError,
                     TenantAccessDenied, ValidationError)  # noqa: F401
from .context import TenantContext, resolve_context, staff_context  # noqa: F401
