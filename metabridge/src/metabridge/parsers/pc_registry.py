"""PowerCenter transformation semantic registry (Phase 2, module 5).

Accessor over ``powercenter_transformation_registry.yaml`` — the single
declared source of PowerCenter-transformation conversion knowledge.
Transformation logic is never scattered through source code: the parser
resolves unknown types here, reports use the coverage matrix, and the
no-drift tests pin the registry to what the engine actually implements.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Dict, List, Optional

import yaml

_FILE = Path(__file__).parent / "powercenter_transformation_registry.yaml"

AUTOMATION_LEVELS = ("FULL", "HIGH", "PARTIAL", "LOW", "MANUAL")

REQUIRED_FIELDS = ("powercenter_type", "cir_type", "automation_level",
                   "dbt_strategy", "databricks_strategy",
                   "requires_manual_review", "semantic_handler")

_UNKNOWN = {
    "key": "UNKNOWN",
    "powercenter_type": "",
    "cir_type": "CUSTOM_CODE",
    "automation_level": "MANUAL",
    "dbt_strategy": "manual_reimplementation",
    "databricks_strategy": "manual_reimplementation",
    "requires_manual_review": True,
    "semantic_handler": "handle_unknown",
    "row_semantics": "active",
    "native_in_engine": False,
    "workaround": "Transformation type is not in the registry — inspect "
                  "the original object and port its logic manually.",
}


class PCTransformationRegistry:
    def __init__(self, path: Optional[Path] = None):
        self._doc: Dict[str, dict] = yaml.safe_load(
            (path or _FILE).read_text(encoding="utf-8")) or {}
        self._by_alias: Dict[str, str] = {}
        for key, row in self._doc.items():
            self._by_alias[key.lower()] = key
            self._by_alias.setdefault(
                str(row.get("powercenter_type", "")).lower(), key)
            for alias in row.get("aliases") or []:
                self._by_alias.setdefault(str(alias).lower(), key)

    def all(self) -> Dict[str, dict]:
        return dict(self._doc)

    def get(self, key: str) -> Optional[dict]:
        row = self._doc.get(key)
        return {"key": key, **row} if row else None

    def classify(self, pc_type: str) -> dict:
        """Resolve a TYPE string from an export ('Lookup Procedure',
        'Union Transformation', ...) to its registry entry. Unknown types
        return the honest UNKNOWN template, never None."""
        key = self._by_alias.get((pc_type or "").strip().lower())
        if key is None:
            return {**_UNKNOWN, "powercenter_type": pc_type or ""}
        return {"key": key, **self._doc[key]}

    def coverage(self) -> dict:
        by_level = {lvl: 0 for lvl in AUTOMATION_LEVELS}
        for row in self._doc.values():
            by_level[row["automation_level"]] += 1
        return {
            "transformations": len(self._doc),
            "by_automation_level": by_level,
            "native_in_engine": sum(1 for r in self._doc.values()
                                    if r.get("native_in_engine")),
            "manual_review_required": sum(
                1 for r in self._doc.values()
                if r.get("requires_manual_review")),
        }

    def validate(self) -> List[str]:
        problems: List[str] = []
        for key, row in self._doc.items():
            for f in REQUIRED_FIELDS:
                if f not in row:
                    problems.append("%s: missing field '%s'" % (key, f))
            lvl = row.get("automation_level")
            if lvl not in AUTOMATION_LEVELS:
                problems.append("%s: bad automation_level %r" % (key, lvl))
            if lvl in ("LOW", "MANUAL") and \
                    not row.get("requires_manual_review"):
                problems.append("%s: %s automation must require manual "
                                "review" % (key, lvl))
            if row.get("row_semantics") not in ("passive", "active",
                                                "generator", "boundary"):
                problems.append("%s: bad row_semantics" % key)
            if not str(row.get("semantic_handler", "")).startswith("handle_"):
                problems.append("%s: semantic_handler must be a handle_* "
                                "dispatch id" % key)
        return problems


@functools.lru_cache(maxsize=1)
def get_pc_registry() -> PCTransformationRegistry:
    return PCTransformationRegistry()
