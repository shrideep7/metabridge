"""Commercial enforcement bridge (data-plane side).

This is the product's half of the two-plane commercial system: the small,
carefully-scoped set of instance-side capabilities that let a MetaBridge
deployment check entitlements, verify a signed license offline, and report
usage back to the control plane.

Design constraints (docs/commercialization/04-implementation-roadmap.md, Phase
5 — "Enforcement bridge"):

- **Additive and inert by default.** Nothing here runs in an engine's hot path
  until an enforcement point calls it *and* the operator turns on a feature
  flag. The flags are unseeded, so ``FeatureFlags.evaluate`` fail-closes to
  OFF — an un-configured instance behaves byte-for-byte like today's product.
- **Never imports the control plane.** The product and the control plane are
  separate deployables; this package re-derives the license canonicalization
  and Ed25519 verification rather than importing ``metabridge_control``.
- **Fail soft then dark.** Enforcement gates licensed *expansion* (new
  users/programs/AI spend), never the deterministic engines or customer data
  (mirrors the control plane's entitlement posture).

Public surface:
- ``LicenseFile`` / ``load_license_file`` — offline, signature-verified license.
- ``EntitlementSet`` — resolve a capability/limit against entitlements.
- ``CommercialBridge`` / ``enforce`` — the flag-gated enforcement seam engines
  call. ``EntitlementDenied`` is the only exception it raises (deny mode only).
- ``UsageSpool`` / ``UsageReporter`` — durable, idempotent usage reporting.
- ``build_usage_statement`` / ``sign_statement`` — air-gapped signed export.
"""
from __future__ import annotations

from .bridge import (CommercialBridge, BridgeDecision, EntitlementDenied,
                     FLAG_ENFORCE, FLAG_DENY, FLAG_USAGE_REPORTING)
from .entitlements import EntitlementSet, LocalDecision
from .licensefile import LicenseFile, LicenseError, load_license_file
from .usage import UsageSpool, UsageReporter
from .airgap import build_usage_statement, sign_statement
from .transport import (ControlPlaneClient, ControlPlaneUnavailable,
                        TransportError, connected_bridge, drain_usage)

__all__ = [
    "CommercialBridge", "BridgeDecision", "EntitlementDenied",
    "FLAG_ENFORCE", "FLAG_DENY", "FLAG_USAGE_REPORTING",
    "EntitlementSet", "LocalDecision",
    "LicenseFile", "LicenseError", "load_license_file",
    "UsageSpool", "UsageReporter",
    "build_usage_statement", "sign_statement",
    "ControlPlaneClient", "ControlPlaneUnavailable", "TransportError",
    "connected_bridge", "drain_usage",
]
