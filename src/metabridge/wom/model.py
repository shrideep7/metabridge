"""Canonical Warehouse Object Model.

One object shape for every schema-level thing a warehouse holds. The kind
vocabulary is deliberately platform-neutral: a Snowflake TASK, a Databricks
JOB and a SQL Server Agent job are all ``task``; an S3 stage, a UC external
location and a BigQuery connection are all ``stage``. Platform specifics ride
in ``properties`` and the original text always rides in ``definition`` —
declared loss, never silent loss.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

OBJECT_KINDS = (
    "table", "external_table", "view", "materialized_view", "dynamic_table",
    "procedure", "function", "sequence", "task", "stream", "pipe", "stage",
    "file_format", "masking_policy", "row_access_policy", "tag", "grant",
    "constraint", "comment", "share", "warehouse", "resource_monitor",
)

# statuses a feasibility classification can assign
STATUSES = ("AUTOMATED", "PARTIAL", "MANUAL", "NO_EQUIVALENT")


@dataclass
class DbObject:
    kind: str
    name: str
    schema: str = ""
    database: str = ""
    definition: str = ""            # SQL / body when the role can read it
    language: str = ""              # sql | javascript | python | java |
    #                                 scala | plpgsql | plpythonu | ...
    properties: Dict[str, object] = field(default_factory=dict)

    def qualified(self) -> str:
        parts = [p for p in (self.schema, self.name) if p]
        return ".".join(parts) or self.name

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "name": self.name}
        for k in ("schema", "database", "language"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.definition:
            d["definition"] = self.definition[:20000]
        if self.properties:
            d["properties"] = self.properties
        return d


@dataclass
class ObjectInventory:
    """Everything enumerated from one live system, plus what could NOT be
    read (a category a role can't see is reported, never silently empty)."""
    connector: str
    database: str = ""
    schema: str = ""
    objects: List[DbObject] = field(default_factory=list)
    unreadable: List[dict] = field(default_factory=list)   # {category, reason}
    elapsed_ms: int = 0

    def by_kind(self) -> Dict[str, List[DbObject]]:
        out: Dict[str, List[DbObject]] = {}
        for o in self.objects:
            out.setdefault(o.kind, []).append(o)
        return out

    def counts(self) -> Dict[str, int]:
        return {k: len(v) for k, v in sorted(self.by_kind().items())}

    def to_dict(self) -> dict:
        return {"connector": self.connector, "database": self.database,
                "schema": self.schema, "elapsed_ms": self.elapsed_ms,
                "counts": self.counts(),
                "objects": [o.to_dict() for o in self.objects],
                "unreadable": self.unreadable}


def inventory_from_dict(doc: dict) -> ObjectInventory:
    inv = ObjectInventory(connector=doc.get("connector", ""),
                          database=doc.get("database", ""),
                          schema=doc.get("schema", ""),
                          elapsed_ms=int(doc.get("elapsed_ms", 0)),
                          unreadable=list(doc.get("unreadable", [])))
    for od in doc.get("objects", []):
        inv.objects.append(DbObject(
            kind=od.get("kind", ""), name=od.get("name", ""),
            schema=od.get("schema", ""), database=od.get("database", ""),
            definition=od.get("definition", ""),
            language=od.get("language", ""),
            properties=od.get("properties", {}) or {}))
    return inv
