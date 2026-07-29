"""Plugin registry — registration, discovery, health, hot loading.

Holds every registered plugin, keyed by id. Enforces API-version
compatibility and dependency availability at registration, so an
incompatible or under-provisioned plugin never silently half-loads.
Third-party plugins are hot-loaded from a directory containing a
``plugin.yml`` + a Python entrypoint module.
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .spec import (METABRIDGE_API_VERSION, PLUGIN_TYPES, Plugin,
                   PluginError, PluginManifest)


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: Dict[str, Plugin] = {}
        self._builtins_loaded = False

    # -- registration -------------------------------------------------------
    def register(self, plugin: Plugin, replace: bool = False) -> Plugin:
        m = plugin.manifest
        if not m.compatible():
            raise PluginError(
                "plugin %s supports MetaBridge API %s but this build is %s"
                % (m.id, m.supported_versions, METABRIDGE_API_VERSION))
        ok, missing = self.check_dependencies(m)
        if not ok:
            raise PluginError("plugin %s has unmet dependencies: %s"
                              % (m.id, ", ".join(missing)))
        if m.id in self._plugins and not replace:
            raise PluginError("plugin %s already registered (pass "
                              "replace=True to override)" % m.id)
        self._plugins[m.id] = plugin
        return plugin

    def unregister(self, plugin_id: str) -> bool:
        return self._plugins.pop(plugin_id, None) is not None

    # -- lookup -------------------------------------------------------------
    def get(self, plugin_id: str) -> Optional[Plugin]:
        self._ensure_builtins()
        return self._plugins.get(plugin_id)

    def all(self) -> List[Plugin]:
        self._ensure_builtins()
        return sorted(self._plugins.values(),
                      key=lambda p: (p.type, p.id))

    def by_type(self, plugin_type: str) -> List[Plugin]:
        if plugin_type not in PLUGIN_TYPES:
            raise PluginError("unknown plugin type: %s" % plugin_type)
        return [p for p in self.all() if p.type == plugin_type]

    def capabilities(self) -> List[dict]:
        out = []
        for p in self.all():
            for cap in p.capabilities():
                out.append({"plugin": p.id, "type": p.type,
                            "capability": cap})
        return out

    def counts_by_type(self) -> Dict[str, int]:
        out = {t: 0 for t in PLUGIN_TYPES}
        for p in self.all():
            out[p.type] = out.get(p.type, 0) + 1
        return out

    # -- dependencies -------------------------------------------------------
    def check_dependencies(self, manifest: PluginManifest
                           ) -> Tuple[bool, List[str]]:
        """A dependency is met when its ``name`` is an importable module
        OR an already-registered plugin id."""
        missing = []
        for dep in manifest.dependencies:
            name = str(dep.get("name", ""))
            if not name:
                continue
            if name in self._plugins:
                continue
            if importlib.util.find_spec(name.split(".")[0]) is not None:
                continue
            missing.append(name)
        return (not missing, missing)

    # -- health -------------------------------------------------------------
    def health(self) -> dict:
        results = {}
        summary = {"ok": 0, "degraded": 0, "error": 0}
        for p in self.all():
            h = p.health()
            status = h.get("status", "ok")
            summary[status] = summary.get(status, 0) + 1
            results[p.id] = {"type": p.type, **h,
                             "compatible": p.manifest.compatible()}
        return {"api_version": METABRIDGE_API_VERSION,
                "total": len(results), "summary": summary,
                "plugins": results}

    # -- hot loading --------------------------------------------------------
    def load_manifest(self, yml_path: str) -> PluginManifest:
        return PluginManifest.from_yaml(Path(yml_path).read_text(encoding="utf-8"))

    def load_from_file(self, yml_path: str,
                       replace: bool = False) -> Plugin:
        """Load one plugin from a ``plugin.yml`` whose ``entrypoint`` is
        ``module_file:factory`` — the module (a .py file alongside the
        manifest) is imported and ``factory(manifest)`` must return a
        Plugin."""
        path = Path(yml_path)
        manifest = self.load_manifest(str(path))
        if not manifest.entrypoint or ":" not in manifest.entrypoint:
            raise PluginError("plugin %s: entrypoint must be "
                              "'module:factory'" % manifest.id)
        if not manifest.compatible():
            raise PluginError("plugin %s supports API %s, incompatible "
                              "with %s" % (manifest.id,
                                           manifest.supported_versions,
                                           METABRIDGE_API_VERSION))
        mod_name, func = manifest.entrypoint.split(":", 1)
        base = path.parent.resolve()
        mod_file = (base / (mod_name + ".py")).resolve()
        # the manifest validator already requires a bare-identifier
        # module, but assert the resolved file is inside the plugin dir
        # so the loader can only ever run code beside the manifest
        if base not in mod_file.parents and mod_file.parent != base:
            raise PluginError("plugin %s: entrypoint module escapes the "
                              "plugin directory" % manifest.id)
        if not mod_file.exists():
            raise PluginError("plugin %s: entrypoint module %s not found "
                              "beside the manifest" % (manifest.id,
                                                       mod_file.name))
        unique = "metabridge_plugin_%s" % manifest.id.replace(".", "_") \
            .replace("-", "_")
        spec = importlib.util.spec_from_file_location(unique, mod_file)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)   # runs plugin code
        except Exception as e:  # noqa: BLE001
            raise PluginError("plugin %s failed to import: %s"
                              % (manifest.id, e))
        factory = getattr(module, func, None)
        if not callable(factory):
            raise PluginError("plugin %s: entrypoint %s is not callable"
                              % (manifest.id, manifest.entrypoint))
        plugin = factory(manifest)
        if not isinstance(plugin, Plugin):
            raise PluginError("plugin %s: factory did not return a Plugin"
                              % manifest.id)
        if plugin.manifest.id != manifest.id:
            raise PluginError("plugin %s: factory returned a different id "
                              "(%s)" % (manifest.id, plugin.manifest.id))
        return self.register(plugin, replace=replace)

    def load_from_dir(self, directory: str,
                      replace: bool = False) -> List[str]:
        """Discover and load every ``*/plugin.yml`` (and a top-level
        plugin.yml) under a directory. Returns the loaded plugin ids;
        a plugin that fails to load is skipped (recorded, not fatal)."""
        base = Path(directory)
        loaded, errors = [], []
        candidates = list(base.glob("plugin.yml")) + \
            list(base.glob("*/plugin.yml"))
        for yml in sorted(candidates):
            try:
                p = self.load_from_file(str(yml), replace=replace)
                loaded.append(p.id)
            except PluginError as e:
                errors.append({"manifest": str(yml), "error": str(e)})
        self._load_errors = errors
        return loaded

    # -- marketplace --------------------------------------------------------
    def marketplace(self) -> List[dict]:
        """Marketplace-facing listing: manifest metadata for every
        registered plugin (first-party + installed third-party)."""
        return [{**p.manifest.to_dict(),
                 "source": "first-party" if p.manifest.builtin
                 else "installed"}
                for p in self.all()]

    # -- builtins -----------------------------------------------------------
    def _ensure_builtins(self) -> None:
        if self._builtins_loaded:
            return
        self._builtins_loaded = True     # set first to avoid recursion
        try:
            from . import builtins
            builtins.register_builtins(self)
        except Exception:  # noqa: BLE001 — never break lookup on a bad
            pass                          # first-party registration


_REGISTRY: Optional[PluginRegistry] = None


def get_registry() -> PluginRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = PluginRegistry()
    return _REGISTRY
