"""Control-plane error types.

Every commercially meaningful failure is a typed error so API layers (Phase 2+)
can map them to stable HTTP responses, and so tests can assert on semantics
rather than message strings.
"""
from __future__ import annotations


class ControlPlaneError(Exception):
    """Base class for all control-plane errors."""


class ValidationError(ControlPlaneError):
    """Input failed validation (bad slug, wrong value kind, duplicate...)."""


class NotFoundError(ControlPlaneError):
    """The resource does not exist *within the caller's tenant scope*.

    Deliberately also raised when a resource exists under another tenant —
    cross-tenant probes must not be able to distinguish "not there" from
    "not yours" (no existence oracle).
    """


class TenantAccessDenied(ControlPlaneError):
    """Access to a tenant was denied.

    ``reason`` is a UNIFORM, client-safe code (always ``ACCESS_DENIED``) so
    the not-found / suspended / no-membership cases are indistinguishable to
    an untrusted caller — no tenant existence or lifecycle oracle. The
    granular ``audit_reason`` is for server-side logs and audit only and must
    never be surfaced to the client.
    """

    def __init__(self, audit_reason: str = "ACCESS_DENIED") -> None:
        super().__init__("ACCESS_DENIED")
        self.reason = "ACCESS_DENIED"
        self.audit_reason = audit_reason


class PermissionDenied(ControlPlaneError):
    """The caller's role does not include the required permission."""

    def __init__(self, permission: str, role: str = "") -> None:
        super().__init__(f"permission '{permission}' denied for role '{role}'")
        self.permission = permission
        self.role = role


class PlanImmutableError(ControlPlaneError):
    """A published or retired plan version was asked to change."""


class AuditIntegrityError(ControlPlaneError):
    """The tamper-evident audit chain failed verification."""


class MigrationError(ControlPlaneError):
    """Schema migration could not be applied or rolled back."""


class RatingError(ControlPlaneError):
    """A rating run failed closed — e.g. an effective price fell below the
    price-book floor with no approved override (§8.1 step 7). The FAILED
    RatingRun is still persisted for audit; this error carries its id."""

    def __init__(self, message: str, rating_run_id: str = "") -> None:
        super().__init__(message)
        self.rating_run_id = rating_run_id
