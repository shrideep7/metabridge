"""MetaBridge Enterprise Marketplace.

Publish, sign, distribute and install marketplace items — connectors,
validators, AI skills, pipeline templates, industry accelerators,
migration templates, business rules and transformation libraries — with
versioning, Ed25519 signing against a publisher trust store,
compatibility and license gates, health status, auto-updates and
dependency resolution.
"""
from .package import (ITEM_TYPES, MarketplaceItem, TrustStore,
                      generate_keypair, sign_payload, sign_item,
                      verify_item)
from .catalog import get_catalog, MarketplaceCatalog
from .install import InstallManager, get_install_manager

__all__ = ["ITEM_TYPES", "MarketplaceItem", "TrustStore",
           "generate_keypair", "sign_payload", "sign_item", "verify_item",
           "get_catalog", "MarketplaceCatalog", "InstallManager",
           "get_install_manager"]
