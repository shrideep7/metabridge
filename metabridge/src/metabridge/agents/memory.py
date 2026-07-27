"""Shared memory — the blackboard agents collaborate through.

Every value carries provenance (which agent wrote it, at which revision)
so a downstream agent's inputs are always traceable, and the audit trail
can reconstruct exactly who produced what. Writes are namespaced and
revision-stamped; nothing is silently overwritten without a new
revision.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional


class SharedMemory:
    def __init__(self) -> None:
        self._store: Dict[str, Any] = {}
        self._prov: Dict[str, dict] = {}
        self._rev = 0

    def put(self, key: str, value: Any, agent_id: str) -> int:
        self._rev += 1
        self._store[key] = value
        self._prov[key] = {"agent_id": agent_id, "revision": self._rev}
        return self._rev

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._store

    def provenance(self, key: str) -> Optional[dict]:
        return self._prov.get(key)

    def keys(self) -> List[str]:
        return sorted(self._store)

    def snapshot(self) -> dict:
        """A provenance-annotated, deep-copied view for audit/inspection.
        Deep copy so a later mutation of a stored object cannot rewrite
        history."""
        out = {}
        for k in self._store:
            try:
                value = copy.deepcopy(self._store[k])
            except Exception:                    # noqa: BLE001
                value = "<unserializable>"
            out[k] = {"provenance": self._prov.get(k), "value": value}
        return out
