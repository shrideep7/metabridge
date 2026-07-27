"""MetaBridge OS kernel — the one place the whole platform describes
itself.

The kernel enumerates the shared canonical models, the sixteen core
engines (grouped by category, each declaring the canonical models it
consumes/produces), the nine common platform services, component
versions, feature flags and the notification feed — with a live,
honest health roll-up. It is a thin composition layer: it OWNS nothing
the engines do, it just makes the already-shared-canonical-model
architecture navigable, health-checkable, versioned and flag-gated.
"""
from __future__ import annotations

from typing import Optional

from .canonical import canonical_registry
from .flags import FeatureFlags
from .notifications import NotificationCenter
from .registry import ENGINES, PlatformRegistry
from .versions import VersionRegistry

try:
    from .. import __version__ as MB_VERSION
except Exception:                                # pragma: no cover
    MB_VERSION = "0"

OS_NAME = "MetaBridge Enterprise Data Modernization OS"


class MetaBridgeOS:
    def __init__(self, data_dir: str = "") -> None:
        self.registry = PlatformRegistry()
        self.flags = FeatureFlags(data_dir)
        self.versions = VersionRegistry(data_dir)
        self.notifications = NotificationCenter(data_dir)

    def health(self) -> dict:
        return self.registry.health()

    def manifest(self) -> dict:
        reg = self.registry
        return {
            "os": {"name": OS_NAME, "version": MB_VERSION,
                   "tagline": "Modular engines and platform services over "
                              "shared canonical models — not point-to-point.",
                   "engine_count": len(ENGINES),
                   "service_count": len(reg.services()),
                   "canonical_model_count": len(canonical_registry())},
            "canonical_models": canonical_registry(),
            "engines_by_category": reg.by_category(),
            "engines": [e.to_dict() for e in reg.engines()],
            "services": [s.to_dict() for s in reg.services()],
            "versions": self.versions.manifest(),
            "feature_flags": self.flags.all(),
            "notifications": self.notifications.counts(),
            "health": self.health(),
            "integrity": {
                "canonical_ref_problems": reg.validate_canonical_refs()},
        }


_OS: Optional[MetaBridgeOS] = None


def get_os(data_dir: str = "") -> MetaBridgeOS:
    """A default OS bound to the given (or env) data dir. Callers that need
    strict per-tenant isolation should construct MetaBridgeOS directly."""
    global _OS
    if _OS is None or data_dir:
        _OS = MetaBridgeOS(data_dir)
    return _OS


def system_manifest(data_dir: str = "") -> dict:
    return MetaBridgeOS(data_dir).manifest()
