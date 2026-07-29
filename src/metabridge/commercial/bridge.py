"""The enforcement seam (EB-503, core).

``CommercialBridge`` is the single object an engine's enforcement point talks
to. It is deliberately a thin, flag-gated wrapper around entitlement resolution
so that:

- **Flag OFF (the default) is a pure no-op.** ``evaluate`` fail-closes to OFF
  for the unseeded ``commercial_enforcement`` flag, so ``check`` returns ALLOW
  without ever touching entitlements — the engine behaves exactly like today.
- **WARN mode never blocks.** With enforcement on but ``commercial_enforcement_deny``
  off, a would-be denial is surfaced (and logged) but the operation proceeds —
  the safe rollout step before hard denial.
- **DENY mode blocks expansion only.** ``enforce`` raises ``EntitlementDenied``
  for a disallowed capability; callers gate *licensed expansion* on it, never
  the deterministic engines or customer data.

Three flags, all unseeded (⇒ OFF until an operator sets them), so rollout is
gradual and reversible per instance/subject via the existing rollout buckets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..platform.flags import FeatureFlags
from .entitlements import DENY, EntitlementSet, LocalDecision
from .licensefile import LicenseFile

FLAG_ENFORCE = "commercial_enforcement"            # master; OFF ⇒ allow all
FLAG_DENY = "commercial_enforcement_deny"          # ON ⇒ hard-deny; else warn
FLAG_USAGE_REPORTING = "commercial_usage_reporting"  # gates the usage reporter

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_DENY = "deny"

R_ENFORCEMENT_OFF = "ENFORCEMENT_OFF"
R_LICENSE_INVALID = "LICENSE_INVALID"


@dataclass
class BridgeDecision:
    allowed: bool                 # effective outcome (WARN/OFF never block)
    entitled: bool                # would the underlying entitlement allow it?
    mode: str                     # off | warn | deny
    reason_code: str
    capability: str
    remaining: Optional[int] = None
    limit: Optional[int] = None
    message: str = ""

    @property
    def would_block(self) -> bool:
        """True when enforcement is active and the capability is not entitled —
        i.e. DENY mode would have blocked (and WARN mode is only letting it
        through provisionally)."""
        return self.mode != MODE_OFF and not self.entitled


class EntitlementDenied(Exception):
    """Raised by ``enforce`` in DENY mode for a non-entitled capability."""

    def __init__(self, decision: BridgeDecision) -> None:
        super().__init__(
            f"{decision.capability}: {decision.reason_code}"
            + (f" (limit {decision.limit})" if decision.limit is not None
               else ""))
        self.decision = decision


class CommercialBridge:
    def __init__(self, entitlements: Optional[EntitlementSet] = None,
                 flags: Optional[FeatureFlags] = None, *,
                 subject: str = "", role: str = "",
                 license_file: Optional[LicenseFile] = None,
                 logger: Optional[Callable[[dict], None]] = None) -> None:
        self._ent = entitlements if entitlements is not None else EntitlementSet([])
        self._flags = flags or FeatureFlags()
        self._subject = subject
        self._role = role
        self._license = license_file
        self._log = logger

    # -- mode -----------------------------------------------------------------
    def mode(self) -> str:
        if not self._flags.evaluate(FLAG_ENFORCE, self._subject, self._role):
            return MODE_OFF
        if self._flags.evaluate(FLAG_DENY, self._subject, self._role):
            return MODE_DENY
        return MODE_WARN

    def usage_reporting_enabled(self) -> bool:
        return self._flags.evaluate(FLAG_USAGE_REPORTING, self._subject,
                                    self._role)

    # -- decide ---------------------------------------------------------------
    def check(self, capability: str, *, quantity: int = 1,
              current_usage: int = 0) -> BridgeDecision:
        """Resolve a capability under the current mode. Never raises."""
        mode = self.mode()
        if mode == MODE_OFF:
            return BridgeDecision(True, True, MODE_OFF, R_ENFORCEMENT_OFF,
                                  capability)

        # Offline license validity gates expansion first: an expired/not-yet
        # -valid license denies new expansion (deterministic engines are
        # unaffected — callers only gate expansion on this).
        if self._license is not None and not self._license.is_currently_valid():
            d = LocalDecision(DENY, R_LICENSE_INVALID, code=capability)
        else:
            d = self._ent.decide(capability, quantity=quantity,
                                  current_usage=current_usage)

        effective = d.allowed or (mode == MODE_WARN)
        decision = BridgeDecision(
            allowed=effective, entitled=d.allowed, mode=mode,
            reason_code=d.reason_code, capability=capability,
            remaining=d.remaining, limit=d.limit,
            message=("" if d.allowed else _explain(d)))
        if not d.allowed and self._log is not None:
            self._log({"event": "entitlement.denied", "mode": mode,
                       "capability": capability, "reason": d.reason_code,
                       "enforced": mode == MODE_DENY,
                       "subject": self._subject})
        return decision

    def enforce(self, capability: str, *, quantity: int = 1,
                current_usage: int = 0) -> BridgeDecision:
        """Check and, in DENY mode, raise ``EntitlementDenied`` if not
        entitled. In OFF/WARN modes it always returns (never raises)."""
        decision = self.check(capability, quantity=quantity,
                              current_usage=current_usage)
        if decision.mode == MODE_DENY and not decision.entitled:
            raise EntitlementDenied(decision)
        return decision


def _explain(d: LocalDecision) -> str:
    if d.reason_code == "FEATURE_NOT_ENTITLED":
        return f"{d.code} is not included in your plan."
    if d.reason_code in ("LIMIT_EXCEEDED", "QUOTA_EXHAUSTED"):
        return (f"{d.code} limit reached"
                + (f" ({d.limit})" if d.limit is not None else "") + ".")
    if d.reason_code == "NO_SUCH_ENTITLEMENT":
        return f"{d.code} is not part of your license."
    if d.reason_code == R_LICENSE_INVALID:
        return "License is expired or not yet valid."
    return d.reason_code
