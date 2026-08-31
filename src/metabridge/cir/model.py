"""Canonical Intermediate Representation (CIR) — the platform-neutral
semantic model of a data estate.

Relationship to ``ir.model``: the graph IR is the *working* representation
parsers emit and generators consume (ports, links, dataflow). The CIR is the
*semantic* representation built on top of it (``cir.builder.build_cir``):
richer entities, stable IDs, lineage, business rules, semantic expressions,
confidence scores. Integrations, estimation models, lineage viewers and
partner tooling consume the CIR; conversion internals keep using the IR.

Every entity has a deterministic ``id`` (stable across runs for unchanged
input — diffs between two CIR exports show real change, not noise) and a
``to_dict`` so ``Project.to_dict()`` is a complete, versioned JSON document.
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CIR_VERSION = "1.0"


def make_id(kind: str, *parts: str) -> str:
    digest = hashlib.sha1(("\x1f".join(parts)).encode()).hexdigest()[:12]
    return "%s_%s" % (kind, digest)


class CirTransformationType(str, enum.Enum):
    SOURCE = "SOURCE"
    TARGET = "TARGET"
    EXPRESSION = "EXPRESSION"
    FILTER = "FILTER"
    JOIN = "JOIN"
    LOOKUP = "LOOKUP"
    AGGREGATOR = "AGGREGATOR"
    SORTER = "SORTER"
    ROUTER = "ROUTER"
    UNION = "UNION"
    SEQUENCE = "SEQUENCE"
    RANK = "RANK"
    WINDOW = "WINDOW"
    NORMALIZER = "NORMALIZER"
    DENORMALIZER = "DENORMALIZER"
    PIVOT = "PIVOT"
    UNPIVOT = "UNPIVOT"
    SQL = "SQL"                    # opaque SQL block (override / passthrough)
    PROCEDURE = "PROCEDURE"
    MACRO = "MACRO"
    INCREMENTAL = "INCREMENTAL"
    MERGE = "MERGE"
    SNAPSHOT = "SNAPSHOT"
    CDC = "CDC"
    SCD_TYPE_1 = "SCD_TYPE_1"
    SCD_TYPE_2 = "SCD_TYPE_2"


class _Entity:
    """Shared to_dict: dataclass fields, enums to values, nested entities."""

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {}
        for k, v in self.__dict__.items():
            out[k] = _plain(v)
        return out


def _plain(v: Any) -> Any:
    if isinstance(v, enum.Enum):
        return v.value
    if hasattr(v, "to_dict"):
        return v.to_dict()
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


# ---------------------------------------------------------------------------
# Columns, expressions, datasets
# ---------------------------------------------------------------------------

@dataclass
class Column(_Entity):
    id: str
    name: str
    data_type: str = "string"       # canonical types (ir.model.CANONICAL_TYPES)
    precision: int = 0
    scale: int = 0
    nullable: bool = True
    description: str = ""
    classification: str = ""        # governance tag (pii.direct.email, ...)


@dataclass
class Expression(_Entity):
    id: str
    output_column: str
    semantic: dict                  # SemanticExpression.to_dict()
    canonical_sql: str
    source_sql: str = ""
    confidence_score: float = 1.0


@dataclass
class Dataset(_Entity):
    id: str
    name: str
    kind: str = "dataset"           # dataset | table | view
    schema: str = ""
    database: str = ""
    # the source SYSTEM (see ir.model.SourceTable.system). Carried here so a
    # CIR round trip produces the same artifacts as the IR did directly —
    # without it the dbt source name fell back to the schema and the generated
    # project changed shape depending on whether it went through the CIR.
    system: str = ""
    columns: List[Column] = field(default_factory=list)
    description: str = ""


@dataclass
class Table(Dataset):
    kind: str = "table"


@dataclass
class View(Dataset):
    kind: str = "view"


# ---------------------------------------------------------------------------
# Business rules (typed detail records on transformations)
# ---------------------------------------------------------------------------

@dataclass
class Join(_Entity):
    id: str
    join_type: str                  # INNER | LEFT | RIGHT | FULL
    condition: str
    left_input: str = ""
    right_input: str = ""
    rule_type: str = "join"


@dataclass
class Filter(_Entity):
    id: str
    condition: str
    is_incremental_watermark: bool = False
    rule_type: str = "filter"


@dataclass
class Aggregation(_Entity):
    id: str
    group_by: List[str] = field(default_factory=list)
    aggregates: List[dict] = field(default_factory=list)   # {column, expression}
    rule_type: str = "aggregation"


@dataclass
class WindowFunction(_Entity):
    id: str
    expression: str
    output_column: str = ""
    rule_type: str = "window"


@dataclass
class Lookup(_Entity):
    id: str
    table: str
    condition: str
    rule_type: str = "lookup"


@dataclass
class Router(_Entity):
    id: str
    groups: List[dict] = field(default_factory=list)        # {name, condition}
    rule_type: str = "router"


@dataclass
class Union(_Entity):
    id: str
    inputs: List[str] = field(default_factory=list)
    distinct: bool = False
    rule_type: str = "union"


@dataclass
class Sequence(_Entity):
    id: str
    name: str
    start: int = 1
    increment: int = 1
    rule_type: str = "sequence"


# ---------------------------------------------------------------------------
# Code assets
# ---------------------------------------------------------------------------

@dataclass
class StoredProcedure(_Entity):
    id: str
    name: str
    source_platform: str
    code: str
    language: str = "sql"
    conversion_notes: List[str] = field(default_factory=list)


@dataclass
class Macro(_Entity):
    id: str
    name: str
    source_platform: str
    code: str
    conversion_notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Configuration entities
# ---------------------------------------------------------------------------

@dataclass
class Variable(_Entity):
    id: str
    name: str
    default_value: str = ""
    scope: str = "project"


@dataclass
class Parameter(_Entity):
    id: str
    name: str
    data_type: str = "string"
    default_value: str = ""
    source_syntax: str = ""         # $$X | {{ var('x') }} | :x


@dataclass
class Connection(_Entity):
    id: str
    name: str
    platform: str = ""
    database: str = ""
    schema: str = ""
    properties: Dict[str, str] = field(default_factory=dict)


@dataclass
class Dependency(_Entity):
    id: str
    from_id: str
    to_id: str
    dependency_type: str = "data"   # data | control | reference


@dataclass
class DataQualityRule(_Entity):
    id: str
    name: str
    dataset: str
    column: str = ""
    rule: str = ""                  # unique | not_null | accepted_values | custom
    severity: str = "error"


@dataclass
class Test(_Entity):
    id: str
    name: str
    dataset: str
    definition: str = ""
    origin: str = ""                # dbt test / manual / generated


@dataclass
class Schedule(_Entity):
    id: str
    name: str
    cron: str = ""
    timezone: str = "UTC"
    enabled: bool = True


@dataclass
class RuntimeConfiguration(_Entity):
    id: str
    dialect: str = ""
    parameters: List[Parameter] = field(default_factory=list)
    variables: List[Variable] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Transformation — the core CIR node
# ---------------------------------------------------------------------------

@dataclass
class Transformation(_Entity):
    id: str
    name: str
    source_platform: str
    transformation_type: CirTransformationType
    inputs: List[str] = field(default_factory=list)          # upstream tx ids
    outputs: List[str] = field(default_factory=list)         # downstream tx ids
    input_columns: List[str] = field(default_factory=list)
    output_columns: List[Column] = field(default_factory=list)
    expressions: List[Expression] = field(default_factory=list)
    business_rules: List[Any] = field(default_factory=list)  # typed rule records
    dependencies: List[Dependency] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_location: str = ""       # file / object the logic came from
    source_lineage: List[str] = field(default_factory=list)  # upstream datasets
    conversion_notes: List[str] = field(default_factory=list)
    confidence_score: float = 1.0


# ---------------------------------------------------------------------------
# Orchestration + containers
# ---------------------------------------------------------------------------

@dataclass
class Task(_Entity):
    id: str
    name: str
    task_type: str = "pipeline"     # pipeline | procedure | command
    pipeline_id: str = ""
    depends_on: List[str] = field(default_factory=list)      # task ids


@dataclass
class Workflow(_Entity):
    id: str
    name: str
    tasks: List[Task] = field(default_factory=list)
    execution_waves: List[List[str]] = field(default_factory=list)
    schedule: Optional[Schedule] = None


@dataclass
class Pipeline(_Entity):
    """One unit of data movement (dbt model == Informatica mapping)."""
    id: str
    name: str
    source_platform: str
    transformations: List[Transformation] = field(default_factory=list)
    load_strategy: str = "FULL"
    unique_key: List[str] = field(default_factory=list)
    target_dataset: str = ""
    depends_on: List[str] = field(default_factory=list)      # pipeline names
    conversion_notes: List[str] = field(default_factory=list)
    confidence_score: float = 1.0


@dataclass
class Asset(_Entity):
    """Any source artifact worth tracking that is not a pipeline."""
    id: str
    name: str
    asset_type: str                 # procedure | macro | script | document
    source_platform: str = ""
    content: str = ""
    conversion_notes: List[str] = field(default_factory=list)


@dataclass
class Project(_Entity):
    id: str
    name: str
    source_platform: str
    cir_version: str = CIR_VERSION
    pipelines: List[Pipeline] = field(default_factory=list)
    workflows: List[Workflow] = field(default_factory=list)
    datasets: List[Dataset] = field(default_factory=list)
    assets: List[Asset] = field(default_factory=list)
    stored_procedures: List[StoredProcedure] = field(default_factory=list)
    macros: List[Macro] = field(default_factory=list)
    connections: List[Connection] = field(default_factory=list)
    dependencies: List[Dependency] = field(default_factory=list)
    data_quality_rules: List[DataQualityRule] = field(default_factory=list)
    tests: List[Test] = field(default_factory=list)
    runtime: Optional[RuntimeConfiguration] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def pipeline(self, name: str) -> Optional[Pipeline]:
        for p in self.pipelines:
            if p.name == name:
                return p
        return None

    def summary(self) -> dict:
        return {
            "project": self.name,
            "source_platform": self.source_platform,
            "cir_version": self.cir_version,
            "pipelines": len(self.pipelines),
            "transformations": sum(len(p.transformations) for p in self.pipelines),
            "datasets": len(self.datasets),
            "stored_procedures": len(self.stored_procedures),
            "data_quality_rules": len(self.data_quality_rules),
            "avg_confidence": round(
                sum(p.confidence_score for p in self.pipelines)
                / len(self.pipelines), 3) if self.pipelines else 1.0,
        }
