"""Version Management — a platform service tracking component versions and
compatibility.

Reuses the plugin version comparator (``plugins.spec.version_compatible``)
so version semantics are consistent platform-wide rather than
reimplemented per component. Seeds the versions of the OS, the plugin
API, the CIR and the marketplace API; extra components can be registered
at runtime (file-backed under ``DATA_DIR/platform/versions.json``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from ..plugins.spec import (METABRIDGE_API_VERSION, PluginError,
                            version_compatible)
from ._util import atomic_write_json, file_lock

try:
    from .. import __version__ as MB_VERSION
except Exception:                                # pragma: no cover
    MB_VERSION = "0"


def _seed() -> Dict[str, dict]:
    return {
        "metabridge_os": {"version": MB_VERSION,
                          "description": "MetaBridge OS platform version"},
        "plugin_api": {"version": METABRIDGE_API_VERSION,
                       "description": "Plugin/extension API contract"},
        "cir": {"version": "1.0",
                "description": "Canonical Intermediate Representation"},
        "marketplace_api": {"version": "1.0",
                            "description": "Marketplace package/signing API"},
    }


class VersionRegistry:
    def __init__(self, data_dir: str = "") -> None:
        base = Path(data_dir or os.environ.get("METABRIDGE_DATA_DIR",
                                               ".")) / "platform"
        base.mkdir(parents=True, exist_ok=True)
        self._file = base / "versions.json"
        self._lock = base / "versions.lock"

    def _overrides(self) -> dict:
        if self._file.exists():
            try:
                d = json.loads(self._file.read_text(encoding="utf-8"))
                return d if isinstance(d, dict) else {}
            except (ValueError, OSError):
                return {}
        return {}

    def components(self) -> Dict[str, dict]:
        comps = _seed()
        for k, v in self._overrides().items():
            if isinstance(v, dict) and v.get("version"):
                comps[k] = {"version": str(v["version"]),
                            "description": v.get("description", "")}
        return comps

    def get(self, component: str) -> Optional[str]:
        c = self.components().get(component)
        return c["version"] if c else None

    def register(self, component: str, version: str,
                 description: str = "") -> dict:
        if not component or not version:
            raise ValueError("component and version are required")
        with file_lock(self._lock):
            ov = self._overrides()
            ov[component] = {"version": str(version),
                             "description": description}
            atomic_write_json(self._file, ov)
        return {"component": component, "version": str(version)}

    def compatible(self, component: str, spec: str) -> bool:
        """Does a component's current version satisfy a version spec? A
        malformed spec fails CLOSED (False), never raises."""
        ver = self.get(component)
        if ver is None:
            return False
        try:
            return version_compatible(spec or "*", ver)
        except PluginError:
            return False

    def check(self, requirements: Dict[str, str]) -> dict:
        """Evaluate a {component: spec} requirement map; returns per-item
        results and an overall verdict."""
        results = []
        ok = True
        for comp, spec in (requirements or {}).items():
            have = self.get(comp)
            sat = self.compatible(comp, spec)
            ok = ok and sat
            results.append({"component": comp, "required": spec,
                            "have": have, "satisfied": sat})
        return {"compatible": ok, "results": results}

    def manifest(self) -> List[dict]:
        return [{"component": k, **v}
                for k, v in sorted(self.components().items())]
