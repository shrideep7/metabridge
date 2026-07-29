"""Accessors for the phase-3 legacy SQL registries (sections 10/11)."""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Dict, List, Optional

import yaml

_FN_FILE = Path(__file__).parent / "legacy_sql_function_registry.yaml"
_DT_FILE = Path(__file__).parent / "legacy_sql_datatype_registry.yaml"

FUNCTION_CATEGORIES = ("NULL", "CONDITIONAL", "DATE_TIME", "STRING",
                       "NUMERIC", "CONVERSION", "REGEX", "HASH", "JSON",
                       "WINDOW", "AGGREGATE")
TARGETS = ("snowflake", "databricks", "bigquery", "redshift",
           "synapse_fabric", "postgres")


class LegacyFunctionRegistry:
    def __init__(self, path: Optional[Path] = None):
        doc = yaml.safe_load((path or _FN_FILE).read_text(encoding="utf-8")) or {}
        self._rows: List[dict] = doc.get("functions", [])

    def all(self) -> List[dict]:
        return list(self._rows)

    def get(self, dialect: str, function: str) -> Optional[dict]:
        for r in self._rows:
            if r["source_dialect"] == dialect and \
                    r["source_function"].upper() == function.upper():
                return r
        return None

    def by_dialect(self, dialect: str) -> List[dict]:
        return [r for r in self._rows if r["source_dialect"] == dialect]

    def validate(self) -> List[str]:
        problems: List[str] = []
        for r in self._rows:
            key = "%s.%s" % (r.get("source_dialect"),
                             r.get("source_function"))
            for f in ("source_dialect", "source_function",
                      "semantic_function", "category", "risk_level",
                      "target_implementations"):
                if not r.get(f) and f != "target_implementations":
                    problems.append("%s: missing %s" % (key, f))
            if r.get("category") not in FUNCTION_CATEGORIES:
                problems.append("%s: bad category %r"
                                % (key, r.get("category")))
            impls = r.get("target_implementations") or {}
            for t in TARGETS:
                if t not in impls:
                    problems.append("%s: missing target %s" % (key, t))
            if r.get("risk_level") not in ("low", "medium", "high"):
                problems.append("%s: bad risk_level" % key)
        return problems


class LegacyDatatypeRegistry:
    def __init__(self, path: Optional[Path] = None):
        doc = yaml.safe_load((path or _DT_FILE).read_text(encoding="utf-8")) or {}
        self.canonical_types: List[str] = doc.get("canonical_types", [])
        self._maps: Dict[str, Dict[str, dict]] = doc.get("mappings", {})

    def map_type(self, dialect: str, source_type: str) -> Optional[dict]:
        table = self._maps.get(dialect, {})
        st = source_type.upper().strip()
        if st in table:
            return {"source_type": st, **table[st]}
        base = st.split("(")[0].strip()
        # NUMBER(p) / NUMBER(p,s) style disambiguation
        if base == "NUMBER" and "(" in st:
            key = "NUMBER(p,s)" if "," in st else "NUMBER(p)"
            if key in table:
                return {"source_type": st, **table[key]}
        if base + "(MAX)" in table and "MAX" in st:
            return {"source_type": st, **table[base + "(MAX)"]}
        if base in table:
            return {"source_type": st, **table[base]}
        return None

    def dialects(self) -> List[str]:
        return sorted(self._maps)

    def validate(self) -> List[str]:
        problems = []
        for dialect, table in self._maps.items():
            for st, row in table.items():
                if row.get("canonical") not in self.canonical_types:
                    problems.append("%s.%s: bad canonical %r"
                                    % (dialect, st, row.get("canonical")))
                if not isinstance(row.get("warnings"), list):
                    problems.append("%s.%s: warnings must be a list"
                                    % (dialect, st))
        return problems


@functools.lru_cache(maxsize=1)
def get_legacy_function_registry() -> LegacyFunctionRegistry:
    return LegacyFunctionRegistry()


@functools.lru_cache(maxsize=1)
def get_legacy_datatype_registry() -> LegacyDatatypeRegistry:
    return LegacyDatatypeRegistry()
