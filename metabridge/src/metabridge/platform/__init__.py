"""MetaBridge OS — the platform kernel.

Presents MetaBridge as an Enterprise Data Modernization Operating System:
sixteen modular core engines and nine common platform services, all
composed over a small set of SHARED CANONICAL MODELS (IR, CIR, Digital
Twin, CER, COR, SAP landscape, agent context) rather than point-to-point
implementations. The kernel is self-describing (manifest), health-
checkable, versioned and feature-flag gated.
"""
from .canonical import CANONICAL_MODELS, CanonicalModel, canonical_registry
from .registry import (CATEGORY_ORDER, ENGINES, SERVICES, Category,
                       EngineDescriptor, PlatformRegistry, ServiceDescriptor)
from .flags import FeatureFlags, DEFAULT_FLAGS
from .notifications import NotificationCenter, SEVERITIES
from .versions import VersionRegistry
from .kernel import MetaBridgeOS, OS_NAME, get_os, system_manifest

__all__ = [
    "CANONICAL_MODELS", "CanonicalModel", "canonical_registry",
    "ENGINES", "SERVICES", "Category", "CATEGORY_ORDER",
    "EngineDescriptor", "ServiceDescriptor", "PlatformRegistry",
    "FeatureFlags", "DEFAULT_FLAGS", "NotificationCenter", "SEVERITIES",
    "VersionRegistry", "MetaBridgeOS", "OS_NAME", "get_os",
    "system_manifest",
]
