"""Marketplace install lifecycle.

Install order for every item: verify signature -> check compatibility ->
require license acceptance -> resolve & install dependencies first ->
materialize (write content / hot-load a plugin) -> record state. An
unverified, incompatible, unlicensed or unmet-dependency install is
refused BEFORE anything is written or executed.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from ..plugins.spec import version_compatible, METABRIDGE_API_VERSION
from .catalog import MarketplaceCatalog, get_catalog, _ver_key
from .package import (MarketplaceError, MarketplaceItem, TrustStore,
                      verify_item)


def _satisfies(version: str, spec: str) -> bool:
    """Does a concrete version satisfy a dependency comparator spec
    (reusing the plugin version comparator)?"""
    return version_compatible(spec or "*", version)


def _safe_components(name: str) -> List[str]:
    parts = []
    for raw in str(name).replace("\\", "/").split("/"):
        if raw in ("", ".", ".."):
            continue
        parts.append("".join(c if (c.isalnum() or c in "._- ") else "_"
                             for c in raw))
    return parts


class InstallManager:
    def __init__(self, catalog: Optional[MarketplaceCatalog] = None,
                 data_dir: str = "", plugin_registry=None) -> None:
        self.catalog = catalog or get_catalog()
        self.trust: TrustStore = self.catalog.trust
        base = Path(data_dir or os.environ.get("METABRIDGE_DATA_DIR",
                                               ".")) / "marketplace"
        self._root = base / "installed"
        self._state_file = base / "installed.json"
        self._registry = plugin_registry
        base.mkdir(parents=True, exist_ok=True)

    # -- state --------------------------------------------------------------
    def _load(self) -> dict:
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return {}
        return {}

    def _save(self, state: dict) -> None:
        self._state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")

    def installed(self) -> dict:
        return self._load()

    def is_installed(self, item_id: str, version: str = "") -> bool:
        rec = self._load().get(item_id)
        if rec is None:
            return False
        return not version or rec.get("version") == version

    def _plugins(self):
        if self._registry is not None:
            return self._registry
        from ..plugins import get_registry
        return get_registry()

    def _install_dir(self, item_id: str) -> Path:
        """The install directory for an item id, sanitized to a single safe
        component that can NEVER be '.', '..' or escape the install root.
        Used by install, uninstall and health so a hostile or corrupt id
        can never rmtree/write outside the sandbox."""
        name = "".join(c if (c.isalnum() or c in "._-") else "_"
                       for c in str(item_id))
        name = name.strip().strip(".")        # kill '', '.', '..', dot-runs
        if not name:
            raise MarketplaceError("invalid item id for install dir: %r"
                                   % item_id)
        dest = self._root / name
        root_r = self._root.resolve()
        dest_r = dest.resolve()
        if dest_r == root_r or root_r not in dest_r.parents:
            raise MarketplaceError("install dir escapes the sandbox: %r"
                                   % item_id)
        return dest

    # -- dependency resolution ---------------------------------------------
    def resolve(self, item_id: str, version: str = "") -> List[MarketplaceItem]:
        """Return items in install order (dependencies first). Detects
        cycles and version conflicts."""
        constraints: Dict[str, List[str]] = {}

        def collect(iid, spec, chain):
            if iid in chain:
                raise MarketplaceError("dependency cycle: %s"
                                       % " -> ".join(chain + [iid]))
            constraints.setdefault(iid, [])
            if spec:
                constraints[iid].append(spec)
            item = self._choose(iid, constraints[iid])
            for dep in item.dependencies:
                collect(str(dep["id"]), str(dep.get("version", "*")),
                        chain + [iid])

        root_spec = ("==%s" % version) if version else ""
        collect(item_id, root_spec, [])
        # topological order (deps before dependents)
        order: List[str] = []
        seen: set = set()

        def visit(iid, chain):
            if iid in seen:
                return
            if iid in chain:
                raise MarketplaceError("dependency cycle: %s"
                                       % " -> ".join(chain + [iid]))
            item = self._choose(iid, constraints.get(iid, []))
            for dep in item.dependencies:
                visit(str(dep["id"]), chain + [iid])
            seen.add(iid)
            order.append(iid)

        visit(item_id, [])
        return [self._choose(iid, constraints.get(iid, []))
                for iid in order]

    def _choose(self, item_id: str, specs: List[str]) -> MarketplaceItem:
        """The highest catalog version of item_id satisfying ALL specs."""
        avail = self.catalog.versions(item_id)
        if not avail:
            raise MarketplaceError("dependency not in catalog: %s"
                                   % item_id)
        ok = [v for v in avail
              if all(_satisfies(v, s) for s in specs if s)]
        if not ok:
            raise MarketplaceError(
                "no version of %s satisfies %s (available: %s)"
                % (item_id, ", ".join(s for s in specs if s) or "*",
                   ", ".join(avail)))
        best = max(ok, key=_ver_key)
        item = self.catalog.get(item_id, best)
        if item is None:
            raise MarketplaceError("catalog missing %s@%s"
                                   % (item_id, best))
        return item

    # -- install ------------------------------------------------------------
    def install(self, item_id: str, version: str = "",
                accept_license: bool = False, allow_unverified: bool = False,
                auto_deps: bool = True, installed_at: str = "") -> dict:
        root = self.catalog.get(item_id, version)
        if root is None:
            raise MarketplaceError("unknown item: %s%s"
                                   % (item_id,
                                      "@" + version if version else ""))
        chain = (self.resolve(item_id, version) if auto_deps
                 else [root])
        # gate the WHOLE chain before writing/executing anything
        for it in chain:
            v = verify_item(it, self.trust)
            # allow_unverified is scoped to the EXPLICITLY requested item —
            # it must never suppress signature checks on transitive
            # dependencies (which would force-install untrusted/forged deps)
            allow = allow_unverified and it.id == item_id
            if not v["verified"] and not allow:
                raise MarketplaceError(
                    "refusing to install %s: signature %s"
                    % (it.id, v["status"]))
            compat = it.platform_compatible()
            if not compat["compatible"]:
                raise MarketplaceError(
                    "refusing to install %s: incompatible (%s)"
                    % (it.id, "; ".join(compat["reasons"])))
            if it.requires_license_acceptance() and not accept_license:
                raise MarketplaceError(
                    "%s requires accepting its %s license — pass "
                    "accept_license=true" % (it.id,
                                             it.license.get("type",
                                                            "commercial")))
        state = self._load()
        report = {"installed": [], "skipped": [], "plugin_ids": []}
        # Materialize deps-first, persisting state after EACH item so a
        # failure part-way through never leaves a live plugin or written
        # files with no state record to clean up later (uninstall can reach
        # everything installed so far). The failing item itself is atomic
        # (see _materialize).
        for it in chain:
            rec = state.get(it.id)
            if rec and rec.get("version") == it.version:
                report["skipped"].append(it.id)   # already at this version
                continue
            plugin_id = self._materialize(it)
            state[it.id] = {"version": it.version, "type": it.type,
                            "license_accepted":
                                it.requires_license_acceptance(),
                            "publisher": it.publisher,
                            "plugin_id": plugin_id}
            if installed_at:
                state[it.id]["installed_at"] = installed_at
            self._save(state)
            report["installed"].append(it.id)
            if plugin_id:
                report["plugin_ids"].append(plugin_id)
        return report

    def _materialize(self, item: MarketplaceItem) -> Optional[str]:
        """Write the item's files into its install dir (sanitizing every
        path), and hot-load it into the plugin registry if it is a
        plugin package. Returns the registered plugin id, or None.

        Atomic: the dir is cleared first so only files from the CURRENT
        verified payload exist on disk (a stale .py from a prior install
        can never be hot-loaded), and if anything fails the dir is removed
        so no partial write is left behind."""
        dest = self._install_dir(item.id)
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        base = dest.resolve()
        try:
            files = item.payload.get("files", {}) or {}
            for name, content in files.items():
                parts = _safe_components(name)
                if not parts:
                    continue
                target = (dest.joinpath(*parts)).resolve()
                if base not in target.parents and target != base:
                    raise MarketplaceError("item %s: file path escapes the "
                                           "install directory: %s"
                                           % (item.id, name))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(content), encoding="utf-8")
            if item.payload.get("plugin") and (dest / "plugin.yml").exists():
                plugin = self._plugins().load_from_file(
                    str(dest / "plugin.yml"), replace=True)
                return plugin.id
            return None
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)   # no partial install
            raise

    # -- uninstall ----------------------------------------------------------
    def uninstall(self, item_id: str) -> bool:
        state = self._load()
        rec = state.pop(item_id, None)
        if rec is None:
            return False
        if rec.get("plugin_id"):
            try:
                self._plugins().unregister(rec["plugin_id"])
            except Exception:  # noqa: BLE001
                pass
        try:
            dest = self._install_dir(item_id)   # guaranteed within sandbox
            shutil.rmtree(dest, ignore_errors=True)
        except MarketplaceError:
            pass                                # never rmtree outside root
        self._save(state)
        return True

    # -- health -------------------------------------------------------------
    def health(self) -> dict:
        state = self._load()
        out = {}
        for iid, rec in state.items():
            if rec.get("plugin_id"):
                p = self._plugins().get(rec["plugin_id"])
                out[iid] = (p.health() if p is not None
                            else {"status": "error",
                                  "detail": "plugin not registered"})
            else:
                try:
                    dest = self._install_dir(iid)
                    ok = dest.exists() and any(dest.iterdir())
                except MarketplaceError:
                    ok = False
                out[iid] = {"status": "ok" if ok else "error",
                            "detail": "content present" if ok
                            else "install files missing"}
        summary = {"ok": 0, "degraded": 0, "error": 0}
        for h in out.values():
            summary[h.get("status", "ok")] = \
                summary.get(h.get("status", "ok"), 0) + 1
        return {"total": len(out), "summary": summary, "items": out}

    # -- updates ------------------------------------------------------------
    def check_updates(self) -> List[dict]:
        state = self._load()
        updates = []
        for iid, rec in state.items():
            latest = self.catalog.get(iid)
            if latest is None:
                continue
            avail = _ver_key(latest.version) > _ver_key(rec["version"])
            updates.append({"id": iid, "installed": rec["version"],
                            "latest": latest.version,
                            "update_available": avail})
        return updates

    def update(self, item_id: str, accept_license: bool = False,
               allow_unverified: bool = False) -> dict:
        if not self.is_installed(item_id):
            raise MarketplaceError("%s is not installed" % item_id)
        latest = self.catalog.get(item_id)
        if latest is None:
            raise MarketplaceError("%s not in catalog" % item_id)
        return self.install(item_id, latest.version,
                            accept_license=accept_license,
                            allow_unverified=allow_unverified)

    def auto_update(self, policy: str = "notify",
                    accept_license: bool = False) -> dict:
        if policy not in ("notify", "latest", "pinned"):
            raise MarketplaceError("policy must be notify|latest|pinned")
        pending = [u for u in self.check_updates()
                   if u["update_available"]]
        applied = []
        if policy == "latest":
            for u in pending:
                try:
                    self.update(u["id"], accept_license=accept_license)
                    applied.append(u["id"])
                except MarketplaceError:
                    pass                          # skip gated updates
        return {"policy": policy, "pending": pending, "applied": applied}


_MANAGER: Optional[InstallManager] = None


def get_install_manager() -> InstallManager:
    global _MANAGER
    # rebind if the data dir changed (tests / multi-tenant). Compare the
    # resolved install root by EQUALITY — a substring test leaks one
    # tenant's install state to another whose data dir happens to be a
    # substring (or shared prefix) of the cached one.
    base = os.environ.get("METABRIDGE_DATA_DIR", ".")
    want = Path(base) / "marketplace" / "installed"
    if _MANAGER is None or _MANAGER._root != want:
        _MANAGER = InstallManager()
    return _MANAGER
