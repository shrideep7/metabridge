"""Instance-side entitlement resolution (EB-501, offline half).

Resolves a capability against a set of entitlements — the snapshot carried in a
verified license file, or a fresh set fetched from the control plane. The value
shapes mirror the control plane's ``check_access`` exactly
(``metabridge_control.entitlements``):

- **BOOLEAN**  (``feature.*``)      — ``value = {"enabled": bool}``
- **NUMERIC_LIMIT** (``limit.*``)   — ``value = {"unlimited": bool, "limit": int}``
- **METERED_QUOTA** (``quota.*``)   — same limit/unlimited shape, per period
- **TIER** / **DATE_WINDOW**        — resolved conservatively (see below)

Decisions carry the same vocabulary as the server so a connected decision and
an offline decision are interchangeable to callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

ALLOW = "ALLOW"
DENY = "DENY"

# reason codes (aligned with metabridge_control.entitlements)
R_OK = "OK"
R_NOT_ENTITLED = "FEATURE_NOT_ENTITLED"
R_LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
R_QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
R_UNKNOWN_CODE = "NO_SUCH_ENTITLEMENT"


@dataclass
class LocalDecision:
    decision: str                       # ALLOW | DENY
    reason_code: str
    remaining: Optional[int] = None
    limit: Optional[int] = None
    code: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW


class EntitlementSet:
    """A resolved set of entitlements keyed by code. Built from a license
    snapshot or a control-plane payload; both use the ``{code, value_kind,
    value}`` shape."""

    def __init__(self, entitlements) -> None:
        self._by_code = {}
        for e in entitlements or []:
            code = e.get("code")
            if code:
                self._by_code[code] = e

    def codes(self) -> list:
        return sorted(self._by_code)

    def has(self, code: str) -> bool:
        return code in self._by_code

    def decide(self, code: str, *, quantity: int = 1,
               current_usage: int = 0) -> LocalDecision:
        """Resolve one capability. Absent codes fail closed to DENY — an
        entitlement the license never granted is never silently allowed."""
        ent = self._by_code.get(code)
        if ent is None:
            return LocalDecision(DENY, R_UNKNOWN_CODE, code=code)
        kind = ent.get("value_kind") or _infer_kind(code)
        value = ent.get("value") or {}

        if kind == "BOOLEAN":
            if bool(value.get("enabled")):
                return LocalDecision(ALLOW, R_OK, code=code)
            return LocalDecision(DENY, R_NOT_ENTITLED, code=code)

        if kind in ("NUMERIC_LIMIT", "METERED_QUOTA"):
            if bool(value.get("unlimited")):
                return LocalDecision(ALLOW, R_OK, code=code, limit=None,
                                     remaining=None)
            limit = int(value.get("limit") or 0)
            remaining = limit - int(current_usage)
            reason_exceeded = (R_LIMIT_EXCEEDED if kind == "NUMERIC_LIMIT"
                               else R_QUOTA_EXHAUSTED)
            if int(current_usage) + int(quantity) <= limit:
                return LocalDecision(ALLOW, R_OK, remaining=remaining,
                                     limit=limit, code=code)
            return LocalDecision(DENY, reason_exceeded, remaining=remaining,
                                 limit=limit, code=code)

        if kind == "TIER":
            # A TIER entitlement grants a level; a capability keyed to it is
            # allowed if the granted tier is present. Ranking comparisons are a
            # server concern (it holds the ladder); offline we grant presence.
            return LocalDecision(ALLOW, R_OK, code=code,
                                 extra={"tier": value.get("tier")})

        # DATE_WINDOW and unknown kinds: the license validity window already
        # gates time (see LicenseFile.status); treat presence as ALLOW so we
        # never dark-fail a capability the file explicitly carries.
        return LocalDecision(ALLOW, R_OK, code=code)


def _infer_kind(code: str) -> str:
    """Fallback kind inference by code prefix (matches the control plane's
    convention) when a snapshot omits ``value_kind``."""
    if code.startswith("feature."):
        return "BOOLEAN"
    if code.startswith("limit."):
        return "NUMERIC_LIMIT"
    if code.startswith("quota."):
        return "METERED_QUOTA"
    if code.startswith("tier."):
        return "TIER"
    return "BOOLEAN"
