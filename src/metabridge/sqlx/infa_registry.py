"""Accessor for the Informatica function registry (Phase 2, module 7).

``informatica_function_registry.yaml`` is the declared catalog of
Informatica expression functions and their semantic conversions. The
conversion itself is AST-based (sqlx.expressions); the registry documents
it and the tests pin every example/expected_sql pair to the live engine.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Dict, List, Optional

import yaml

_FILE = Path(__file__).parent / "informatica_function_registry.yaml"

REQUIRED_FIELDS = ("category", "semantic", "supported", "example",
                   "group", "semantic_function", "automation_level",
                   "semantic_risk")

CATEGORIES = ("conditional", "null_handling", "conversion", "datetime",
              "numeric", "string", "hashing", "phonetic")

# module-29 conversion-matrix groups (+ STATEFUL for repository-persisted
# state functions, which tie into the module-26 parameter engine)
GROUPS = ("NULL", "CONDITIONAL", "STRING", "DATE", "NUMERIC", "CONVERSION",
          "REGEX", "ENCODING", "HASH", "AGGREGATION", "STATEFUL")
AUTOMATION_LEVELS = ("full", "partial", "manual")
SEMANTIC_RISKS = ("none", "low", "medium", "high")


class InfaFunctionRegistry:
    def __init__(self, path: Optional[Path] = None):
        self._doc: Dict[str, dict] = yaml.safe_load(
            (path or _FILE).read_text(encoding="utf-8")) or {}

    def all(self) -> Dict[str, dict]:
        return dict(self._doc)

    def get(self, name: str) -> Optional[dict]:
        row = self._doc.get((name or "").upper())
        return {"function": (name or "").upper(), **row} if row else None

    def by_category(self, category: str) -> Dict[str, dict]:
        return {k: v for k, v in self._doc.items()
                if v.get("category") == category}

    def coverage(self) -> dict:
        cats: Dict[str, int] = {}
        for row in self._doc.values():
            cats[row["category"]] = cats.get(row["category"], 0) + 1
        return {"functions": len(self._doc),
                "supported": sum(1 for r in self._doc.values()
                                 if r.get("supported")),
                "by_category": cats}

    def validate(self) -> List[str]:
        problems: List[str] = []
        for name, row in self._doc.items():
            for f in REQUIRED_FIELDS:
                if f not in row:
                    problems.append("%s: missing '%s'" % (name, f))
            if row.get("category") not in CATEGORIES:
                problems.append("%s: bad category %r"
                                % (name, row.get("category")))
            if row.get("group") not in GROUPS:
                problems.append("%s: bad group %r" % (name, row.get("group")))
            if row.get("automation_level") not in AUTOMATION_LEVELS:
                problems.append("%s: bad automation_level %r"
                                % (name, row.get("automation_level")))
            if row.get("semantic_risk") not in SEMANTIC_RISKS:
                problems.append("%s: bad semantic_risk %r"
                                % (name, row.get("semantic_risk")))
            if row.get("supported"):
                for f in ("expected_sql", "dbt_default", "databricks_sql"):
                    if not row.get(f):
                        problems.append("%s: supported entries must pin "
                                        "%s" % (name, f))
        return problems

    def by_group(self, group: str) -> Dict[str, dict]:
        return {k: v for k, v in self._doc.items()
                if v.get("group") == group}


@functools.lru_cache(maxsize=1)
def get_infa_function_registry() -> InfaFunctionRegistry:
    return InfaFunctionRegistry()
