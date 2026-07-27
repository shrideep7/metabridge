"""MetaBridge AI Intermediate Representation (IR).

Every supported format (dbt, PowerCenter, IDMC) is parsed INTO this model and
generated FROM this model. The IR is deliberately closer to the Informatica
"dataflow graph" world-view (typed ports, transformation nodes, links) because
that is the richer of the two representations; dbt's SQL world is decomposed
into it and reconstructed from it.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class TransformationType(str, enum.Enum):
    SOURCE = "SOURCE"                    # physical source table
    SOURCE_QUALIFIER = "SOURCE_QUALIFIER"  # reads from source(s); may hold SQL override
    EXPRESSION = "EXPRESSION"            # row-level derivations
    FILTER = "FILTER"                    # row filter (WHERE / HAVING)
    JOINER = "JOINER"                    # two-input join
    AGGREGATOR = "AGGREGATOR"            # GROUP BY + aggregate expressions
    SORTER = "SORTER"                    # ORDER BY
    UNION = "UNION"                      # UNION ALL of n inputs
    LOOKUP = "LOOKUP"                    # lookup against a table
    ROUTER = "ROUTER"                    # multi-way filter
    RANK = "RANK"                        # top/bottom-n
    SEQUENCE = "SEQUENCE"                # surrogate key generator
    UPDATE_STRATEGY = "UPDATE_STRATEGY"  # insert/update/delete flagging
    TARGET = "TARGET"                    # physical target table


class LoadStrategy(str, enum.Enum):
    FULL = "FULL"                # truncate + load  (dbt: table)
    VIEW = "VIEW"                # dbt: view — no Informatica load, flagged
    APPEND = "APPEND"            # insert only     (dbt: incremental append)
    MERGE = "MERGE"              # upsert          (dbt: incremental merge)
    DELETE_INSERT = "DELETE_INSERT"  # dbt: incremental delete+insert
    EPHEMERAL = "EPHEMERAL"      # dbt: ephemeral — inlined upstream
    SCD2 = "SCD2"                # slowly-changing dimension type 2 (dbt snapshot)


class IssueSeverity(str, enum.Enum):
    INFO = "INFO"          # converted, nothing to do
    WARNING = "WARNING"    # converted with assumptions — review recommended
    MANUAL = "MANUAL"      # could not be converted — manual work required
    ERROR = "ERROR"        # object skipped entirely


@dataclass
class ConversionIssue:
    severity: IssueSeverity
    code: str                     # stable machine code, e.g. "JINJA_MACRO_UNSUPPORTED"
    message: str
    obj: str = ""                 # object (mapping/model) the issue belongs to
    detail: str = ""              # offending snippet
    suggestion: str = ""          # what the engineer should do
    resolved_by_llm: bool = False

    def to_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "object": self.obj,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "resolved_by_llm": self.resolved_by_llm,
        }


@dataclass
class Port:
    """A typed column/field on a transformation."""
    name: str
    datatype: str = "string"      # canonical: string,integer,bigint,decimal,double,date,timestamp,boolean,binary
    precision: int = 0
    scale: int = 0
    nullable: bool = True
    # For EXPRESSION/AGGREGATOR ports: the expression (in canonical ANSI SQL)
    # that computes this port. Empty string = pass-through.
    expression: str = ""
    direction: str = "INPUT_OUTPUT"  # INPUT | OUTPUT | INPUT_OUTPUT | VARIABLE
    # Whether `datatype` came from a KNOWN, explicitly-declared source type
    # (vs. the "string" fallback used when a type is missing/unknown). The
    # fallback stays usable for processing, but assessment must record it as
    # a data-quality penalty rather than treat it as a genuine string column.
    type_declared: bool = True


@dataclass
class Transformation:
    name: str
    type: TransformationType
    ports: List[Port] = field(default_factory=list)
    # Type-specific properties. Conventions:
    #  SOURCE / TARGET:        table, schema, database, connection
    #  SOURCE_QUALIFIER:       sql_override (canonical SQL, optional)
    #  FILTER:                 condition (canonical SQL boolean expr)
    #  JOINER:                 join_type (INNER|LEFT|RIGHT|FULL), condition,
    #                          left (upstream transformation name), right
    #  AGGREGATOR:             group_by (list of port names)
    #  SORTER:                 sort_keys (list of {port, order})
    #  UNION:                  inputs (list of upstream names)
    #  LOOKUP:                 table, condition
    #  UPDATE_STRATEGY:        strategy expression
    properties: Dict[str, object] = field(default_factory=dict)
    description: str = ""

    def port(self, name: str) -> Optional[Port]:
        for p in self.ports:
            if p.name.lower() == name.lower():
                return p
        return None


@dataclass
class Link:
    """Dataflow edge: from_transformation.from_port -> to_transformation.to_port.

    Empty port names mean 'link all ports by name'.
    """
    from_transformation: str
    to_transformation: str
    from_port: str = ""
    to_port: str = ""


@dataclass
class Mapping:
    """One unit of data movement. dbt model == Informatica mapping."""
    name: str
    transformations: List[Transformation] = field(default_factory=list)
    links: List[Link] = field(default_factory=list)
    load_strategy: LoadStrategy = LoadStrategy.FULL
    unique_key: List[str] = field(default_factory=list)   # for MERGE/DELETE_INSERT
    description: str = ""
    # Names of upstream mappings/models this mapping depends on (project DAG).
    depends_on: List[str] = field(default_factory=list)
    # Original artifact for traceability (raw SQL / XML fragment).
    origin: str = ""
    # mapping-level extras (SCD config, incremental filters, ...)
    properties: Dict[str, object] = field(default_factory=dict)
    issues: List[ConversionIssue] = field(default_factory=list)

    def transformation(self, name: str) -> Optional[Transformation]:
        for t in self.transformations:
            if t.name == name:
                return t
        return None

    def by_type(self, ttype: TransformationType) -> List[Transformation]:
        return [t for t in self.transformations if t.type == ttype]

    def upstream_of(self, name: str) -> List[Transformation]:
        ups = []
        for link in self.links:
            if link.to_transformation == name:
                t = self.transformation(link.from_transformation)
                if t and t not in ups:
                    ups.append(t)
        return ups

    def downstream_of(self, name: str) -> List[Transformation]:
        downs = []
        for link in self.links:
            if link.from_transformation == name:
                t = self.transformation(link.to_transformation)
                if t and t not in downs:
                    downs.append(t)
        return downs

    def add_issue(self, severity: IssueSeverity, code: str, message: str,
                  detail: str = "", suggestion: str = "") -> None:
        self.issues.append(ConversionIssue(
            severity=severity, code=code, message=message,
            obj=self.name, detail=detail, suggestion=suggestion))


@dataclass
class SourceTable:
    """Project-level source metadata (dbt sources / Informatica source defs)."""
    name: str
    schema: str = ""
    database: str = ""
    columns: List[Port] = field(default_factory=list)


@dataclass
class Pipeline:
    """A whole project: the top-level IR object."""
    name: str
    mappings: List[Mapping] = field(default_factory=list)
    sources: List[SourceTable] = field(default_factory=list)
    issues: List[ConversionIssue] = field(default_factory=list)
    source_format: str = ""        # "dbt" | "powercenter" | "idmc"
    metadata: Dict[str, object] = field(default_factory=dict)

    def mapping(self, name: str) -> Optional[Mapping]:
        for m in self.mappings:
            if m.name == name:
                return m
        return None

    def all_issues(self) -> List[ConversionIssue]:
        out = list(self.issues)
        for m in self.mappings:
            out.extend(m.issues)
        return out

    def execution_order(self) -> List[List[str]]:
        """Topologically-ordered waves of mapping names (for workflow generation)."""
        names = {m.name for m in self.mappings}
        deps = {m.name: [d for d in m.depends_on if d in names] for m in self.mappings}
        waves: List[List[str]] = []
        done: set = set()
        remaining = set(names)
        while remaining:
            wave = sorted(n for n in remaining if all(d in done for d in deps[n]))
            if not wave:  # cycle — emit the rest as one wave rather than loop forever
                waves.append(sorted(remaining))
                break
            waves.append(wave)
            done.update(wave)
            remaining.difference_update(wave)
        return waves


# ---------------------------------------------------------------------------
# Canonical datatype helpers
# ---------------------------------------------------------------------------

CANONICAL_TYPES = {
    "string", "integer", "bigint", "decimal", "double",
    "date", "timestamp", "boolean", "binary",
}

_SQL_TO_CANONICAL = {
    "varchar": "string", "char": "string", "text": "string", "nvarchar": "string",
    "string": "string", "nchar": "string",
    "int": "integer", "integer": "integer", "smallint": "integer", "tinyint": "integer",
    "bigint": "bigint",
    "number": "decimal", "numeric": "decimal", "decimal": "decimal",
    "float": "double", "double": "double", "real": "double", "double precision": "double",
    "date": "date",
    "datetime": "timestamp", "timestamp": "timestamp", "timestamp_ntz": "timestamp",
    "timestamp_ltz": "timestamp", "timestamp_tz": "timestamp",
    "timestampntz": "timestamp", "timestampltz": "timestamp", "timestamptz": "timestamp",
    "boolean": "boolean", "bool": "boolean",
    "binary": "binary", "varbinary": "binary", "bytea": "binary",
}


def canonical_type(sql_type: str) -> str:
    base = sql_type.strip().lower().split("(")[0].strip()
    return _SQL_TO_CANONICAL.get(base, "string")


def is_known_type(sql_type: str) -> bool:
    """True when `sql_type` is a non-empty type we can map to a canonical
    type. False for a missing/blank type or one that only survives via the
    "string" fallback — the signal assessment uses to penalize untyped
    schemas instead of silently crediting them as string columns."""
    base = (sql_type or "").strip().lower().split("(")[0].strip()
    return bool(base) and base in _SQL_TO_CANONICAL
