"""MetaBridge Enterprise Plugin SDK.

Every capability in the platform — parsers, target generators, source
connectors, validators, lineage providers, documentation generators, AI
reviewers, report generators, pipeline scaffolds and security analyzers
— is exposed as a plugin behind ONE uniform contract, discovered through
ONE registry. First-party engines are registered as built-in plugins;
third-party plugins drop in via a plugin.yml manifest + hot loading.
"""
from .spec import (METABRIDGE_API_VERSION, PLUGIN_TYPES, Plugin,
                   PluginManifest, version_compatible)
from .registry import PluginRegistry, get_registry

__all__ = ["METABRIDGE_API_VERSION", "PLUGIN_TYPES", "Plugin",
           "PluginManifest", "version_compatible", "PluginRegistry",
           "get_registry"]
