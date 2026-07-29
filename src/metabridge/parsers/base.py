"""Source parser engine: one interface, thirteen parsers, one IR.

``BaseSourceParser`` defines the contract every source technology implements:

    detect(path)                  how confident is this parser that the path
                                  is its format (0..1, from the detection engine)
    parse_project(path)           whole project/repo -> Pipeline (canonical IR)
    parse_asset(content, name)    a single artifact (one model/XML/JSON/script)
                                  -> Pipeline
    extract_metadata(pipeline)    project facts: counts, strategies, issues
    extract_dependencies(pipe)    model DAG + topological execution waves
    extract_business_rules(pipe)  filters, joins, aggregations, watermarks,
                                  keys — the logic an analyst reviews
    extract_expressions(pipe)     every derived column expression
    extract_source_targets(pipe)  physical inputs and outputs with schemas

Because every parser produces the same canonical IR, the ``extract_*`` methods
are implemented ONCE here and work identically for dbt, PowerCenter, IDMC and
all nine warehouse dialects — that is the payoff of the IR architecture.
Concrete parsers only supply ``parse_project`` / ``parse_asset``.
"""
from __future__ import annotations

import abc
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Type

from ..ir.model import (
    IssueSeverity, LoadStrategy, Mapping, Pipeline, TransformationType,
)


class BaseSourceParser(abc.ABC):
    format_name: str = ""      # stable key ("dbt", "snowflake", ...)
    display_name: str = ""     # human label
    dialect: str = ""          # sqlglot dialect where applicable

    # ------------------------------------------------------------------ #
    # detection                                                           #
    # ------------------------------------------------------------------ #

    def detect(self, path: str) -> float:
        """Confidence (0..1) that *path* is this parser's format."""
        from ..detection.engine import detect as run_detect
        try:
            result = run_detect(path)
        except (ValueError, FileNotFoundError):
            return 0.0
        if result.detected_format == self.format_name:
            return result.confidence_score
        for alt in result.alternative_formats:
            if alt["format"] == self.format_name:
                return alt["confidence"]
        # generic-SQL detection keeps dialect parsers plausible at low confidence
        if result.detected_format == "sql" and self.dialect:
            return 0.2
        return 0.0

    # ------------------------------------------------------------------ #
    # parsing (format-specific)                                           #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def parse_project(self, path: str, dialect: str = "") -> Pipeline:
        """Parse a whole project/directory into the canonical IR."""

    @abc.abstractmethod
    def parse_asset(self, content: str, name: str = "asset") -> Pipeline:
        """Parse one artifact (model SQL / XML export / JSON asset) into IR."""

    # ------------------------------------------------------------------ #
    # extraction (format-agnostic — operates on the canonical IR)         #
    # ------------------------------------------------------------------ #

    def extract_metadata(self, pipeline: Pipeline) -> dict:
        strategies: Dict[str, int] = {}
        tx_count = 0
        for m in pipeline.mappings:
            strategies[m.load_strategy.value] = \
                strategies.get(m.load_strategy.value, 0) + 1
            tx_count += len([t for t in m.transformations
                             if t.name != "__OUTPUT__"])
        issues: Dict[str, int] = {}
        for i in pipeline.all_issues():
            issues[i.severity.value] = issues.get(i.severity.value, 0) + 1
        return {
            "format": pipeline.source_format or self.format_name,
            "project": pipeline.name,
            "dialect": str(pipeline.metadata.get("dialect", self.dialect)),
            "mappings": len(pipeline.mappings),
            "sources": len(pipeline.sources),
            "transformations": tx_count,
            "load_strategies": strategies,
            "issues": issues,
        }

    def extract_dependencies(self, pipeline: Pipeline) -> dict:
        return {
            "dependencies": {m.name: list(m.depends_on)
                             for m in pipeline.mappings},
            "execution_order": pipeline.execution_order(),
        }

    def extract_business_rules(self, pipeline: Pipeline) -> List[dict]:
        rules: List[dict] = []
        for m in pipeline.mappings:
            for t in m.transformations:
                if t.name == "__OUTPUT__":
                    continue
                if t.type == TransformationType.FILTER:
                    kind = ("incremental_watermark"
                            if "$$" in str(t.properties.get("condition", ""))
                            or t.name == "FIL_INCREMENTAL" else "filter")
                    rules.append({"mapping": m.name, "rule_type": kind,
                                  "transformation": t.name,
                                  "detail": {"condition":
                                             str(t.properties.get("condition", ""))}})
                elif t.type == TransformationType.JOINER:
                    rules.append({"mapping": m.name, "rule_type": "join",
                                  "transformation": t.name,
                                  "detail": {
                                      "join_type": str(t.properties.get("join_type", "INNER")),
                                      "condition": str(t.properties.get("condition", "")),
                                      "left": str(t.properties.get("left", "")),
                                      "right": str(t.properties.get("right", ""))}})
                elif t.type == TransformationType.AGGREGATOR:
                    rules.append({"mapping": m.name, "rule_type": "aggregation",
                                  "transformation": t.name,
                                  "detail": {
                                      "group_by": [str(g) for g in
                                                   t.properties.get("group_by", [])],
                                      "aggregates": [
                                          {"port": p.name, "expression": p.expression}
                                          for p in t.ports if p.expression]}})
                elif t.type == TransformationType.SORTER:
                    rules.append({"mapping": m.name, "rule_type": "sort",
                                  "transformation": t.name,
                                  "detail": {
                                      "distinct": bool(t.properties.get("distinct")),
                                      "keys": t.properties.get("sort_keys", [])}})
                elif t.type == TransformationType.UNION:
                    rules.append({"mapping": m.name, "rule_type": "union",
                                  "transformation": t.name,
                                  "detail": {"inputs":
                                             [str(i) for i in
                                              t.properties.get("inputs", [])]}})
                elif t.type == TransformationType.LOOKUP:
                    rules.append({"mapping": m.name, "rule_type": "lookup",
                                  "transformation": t.name,
                                  "detail": {"table": str(t.properties.get("table", "")),
                                             "condition":
                                             str(t.properties.get("condition", ""))}})
            if m.unique_key:
                rules.append({"mapping": m.name, "rule_type": "unique_key",
                              "transformation": "", "detail": {"columns": m.unique_key}})
            if m.load_strategy in (LoadStrategy.MERGE, LoadStrategy.DELETE_INSERT,
                                   LoadStrategy.APPEND):
                rules.append({"mapping": m.name, "rule_type": "load_strategy",
                              "transformation": "",
                              "detail": {"strategy": m.load_strategy.value}})
        return rules

    def extract_expressions(self, pipeline: Pipeline) -> List[dict]:
        out: List[dict] = []
        for m in pipeline.mappings:
            for t in m.transformations:
                if t.name == "__OUTPUT__":
                    continue
                for p in t.ports:
                    if p.expression:
                        out.append({"mapping": m.name, "transformation": t.name,
                                    "port": p.name, "expression": p.expression})
            for t in m.by_type(TransformationType.SOURCE_QUALIFIER):
                override = str(t.properties.get("sql_override", "") or "")
                if override:
                    out.append({"mapping": m.name, "transformation": t.name,
                                "port": "(sql_override)", "expression": override})
        return out

    def extract_source_targets(self, pipeline: Pipeline) -> dict:
        sources: List[dict] = []
        targets: List[dict] = []
        seen_src, seen_tgt = set(), set()
        for s in pipeline.sources:
            key = (s.schema, s.name)
            if key not in seen_src:
                seen_src.add(key)
                sources.append({"table": s.name, "schema": s.schema,
                                "database": s.database,
                                "columns": [{"name": c.name, "type": c.datatype}
                                            for c in s.columns]})
        for m in pipeline.mappings:
            for t in m.by_type(TransformationType.SOURCE):
                key = (str(t.properties.get("schema", "")),
                       str(t.properties.get("table", t.name)))
                if key not in seen_src:
                    seen_src.add(key)
                    sources.append({"table": key[1], "schema": key[0],
                                    "database": str(t.properties.get("database", "")),
                                    "columns": [{"name": p.name, "type": p.datatype}
                                                for p in t.ports]})
            for t in m.by_type(TransformationType.TARGET):
                name = str(t.properties.get("table", t.name))
                if name not in seen_tgt:
                    seen_tgt.add(name)
                    targets.append({"table": name, "mapping": m.name,
                                    "load_strategy": m.load_strategy.value,
                                    "unique_key": m.unique_key,
                                    "columns": [{"name": p.name, "type": p.datatype}
                                                for p in t.ports]})
        return {"sources": sources, "targets": targets}


# ---------------------------------------------------------------------------
# Concrete parsers
# ---------------------------------------------------------------------------

class DbtParser(BaseSourceParser):
    format_name = "dbt"
    display_name = "dbt project"

    def parse_project(self, path: str, dialect: str = "") -> Pipeline:
        from .dbt_parser import parse_dbt_project
        return parse_dbt_project(path, dialect)

    def parse_asset(self, content: str, name: str = "model") -> Pipeline:
        """One dbt model's SQL (Jinja allowed) -> single-mapping pipeline."""
        from ..sqlx.decompose import decompose_model
        from .dbt_parser import _attach_target, _render_jinja
        pipeline = Pipeline(name=name, source_format="dbt")
        probe = Mapping(name=name)
        sources: dict = {}
        sql, refs, _inc = _render_jinja(content, probe, name, sources)
        mapping = decompose_model(name, sql, self.dialect or "snowflake", sources)
        mapping.issues.extend(probe.issues)
        mapping.depends_on = sorted(set(refs))
        mapping.origin = content
        _attach_target(mapping, name, None)
        pipeline.mappings.append(mapping)
        pipeline.sources = list(sources.values())
        return pipeline


class PowerCenterParser(BaseSourceParser):
    format_name = "powercenter"
    display_name = "Informatica PowerCenter"

    def parse_project(self, path: str, dialect: str = "") -> Pipeline:
        from .powercenter_parser import parse_powercenter
        return parse_powercenter(path)

    def parse_asset(self, content: str, name: str = "export") -> Pipeline:
        from .powercenter_parser import parse_powercenter
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / ("%s.xml" % name)
            f.write_text(content)
            return parse_powercenter(str(f))


class IDMCParser(BaseSourceParser):
    format_name = "idmc"
    display_name = "Informatica IDMC"

    def parse_project(self, path: str, dialect: str = "") -> Pipeline:
        from .idmc_parser import parse_idmc
        return parse_idmc(path)

    def parse_asset(self, content: str, name: str = "asset") -> Pipeline:
        from .idmc_parser import parse_idmc
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / ("%s.json" % name)
            f.write_text(content)
            return parse_idmc(str(f))


class SqlScriptParser(BaseSourceParser):
    """Shared behavior for all warehouse-SQL dialect parsers."""

    def parse_project(self, path: str, dialect: str = "") -> Pipeline:
        from .sql_parser import parse_sql_scripts
        return parse_sql_scripts(path, self.format_name,
                                 dialect or self.dialect)

    def parse_asset(self, content: str, name: str = "script") -> Pipeline:
        from .sql_parser import parse_sql_scripts
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / ("%s.sql" % name)
            f.write_text(content)
            return parse_sql_scripts(str(f), self.format_name, self.dialect)


class SnowflakeParser(SqlScriptParser):
    format_name, display_name, dialect = "snowflake", "Snowflake SQL", "snowflake"


class DatabricksParser(SqlScriptParser):
    format_name, display_name, dialect = "databricks", "Databricks SQL", "databricks"


class BigQueryParser(SqlScriptParser):
    format_name, display_name, dialect = "bigquery", "Google BigQuery SQL", "bigquery"


class RedshiftParser(SqlScriptParser):
    format_name, display_name, dialect = "redshift", "Amazon Redshift SQL", "redshift"


class SynapseFabricParser(SqlScriptParser):
    format_name, display_name, dialect = "synapse", "Azure Synapse / Fabric SQL", "tsql"


class TSQLParser(SqlScriptParser):
    format_name, display_name, dialect = "sqlserver", "SQL Server (T-SQL)", "tsql"


class OracleParser(SqlScriptParser):
    format_name, display_name, dialect = "oracle", "Oracle SQL", "oracle"


class PostgreSQLParser(SqlScriptParser):
    format_name, display_name, dialect = "postgres", "PostgreSQL", "postgres"


class TeradataParser(SqlScriptParser):
    format_name, display_name, dialect = "teradata", "Teradata SQL", "teradata"


class AnsiSQLParser(SqlScriptParser):
    format_name, display_name, dialect = "sql", "Generic ANSI SQL", ""


# ---------------------------------------------------------------------------
# Legacy ETL platforms (Command 5) — each adapter emits the same IR
# ---------------------------------------------------------------------------

class _EtlParser(BaseSourceParser):
    """Shared parse_asset: single artifact -> temp file -> parse_project."""
    asset_suffix = ".txt"

    def parse_asset(self, content: str, name: str = "asset") -> Pipeline:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / (name + self.asset_suffix)
            f.write_text(content)
            return self.parse_project(td)


class SSISParser(_EtlParser):
    format_name, display_name, dialect = "ssis", "Microsoft SSIS", "tsql"
    asset_suffix = ".dtsx"

    def parse_project(self, path: str, dialect: str = "") -> Pipeline:
        from .ssis_parser import parse_ssis
        return parse_ssis(path, dialect)


class DataStageParser(_EtlParser):
    format_name, display_name = "datastage", "IBM DataStage"
    asset_suffix = ".dsx"

    def parse_project(self, path: str, dialect: str = "") -> Pipeline:
        from .datastage_parser import parse_datastage
        return parse_datastage(path, dialect)


class TalendParser(_EtlParser):
    format_name, display_name = "talend", "Talend Data Integration"
    asset_suffix = ".item"

    def parse_project(self, path: str, dialect: str = "") -> Pipeline:
        from .talend_parser import parse_talend
        return parse_talend(path, dialect)


class AbInitioParser(_EtlParser):
    format_name, display_name = "abinitio", "Ab Initio"
    asset_suffix = ".mp"

    def parse_project(self, path: str, dialect: str = "") -> Pipeline:
        from .abinitio_parser import parse_abinitio
        return parse_abinitio(path, dialect)


class SAPParser(_EtlParser):
    format_name, display_name = "sap", "SAP (BW / HANA / S4 / Datasphere)"
    asset_suffix = ".xml"

    def parse_project(self, path: str, dialect: str = "") -> Pipeline:
        from ..sap.normalize import normalize_sap
        from ..sap.parsers import parse_sap
        return normalize_sap(parse_sap(path))


ETL_FORMATS = ("ssis", "datastage", "talend", "abinitio")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PARSER_CLASSES: List[Type[BaseSourceParser]] = [
    DbtParser, PowerCenterParser, IDMCParser,
    SnowflakeParser, DatabricksParser, BigQueryParser, RedshiftParser,
    SynapseFabricParser, TSQLParser, OracleParser, PostgreSQLParser,
    TeradataParser, AnsiSQLParser,
    SSISParser, DataStageParser, TalendParser, AbInitioParser,
    SAPParser,
]

_REGISTRY: Dict[str, Type[BaseSourceParser]] = {
    cls.format_name: cls for cls in PARSER_CLASSES}


def get_parser(format_name: str) -> BaseSourceParser:
    cls = _REGISTRY.get(str(format_name).lower())
    if cls is None:
        raise ValueError("No parser for format '%s' (available: %s)"
                         % (format_name, ", ".join(sorted(_REGISTRY))))
    return cls()


def list_parsers() -> List[dict]:
    return [{"format": cls.format_name, "name": cls.display_name,
             "dialect": cls.dialect} for cls in PARSER_CLASSES]


def parser_for_path(path: str) -> BaseSourceParser:
    """Auto-detect the format and return the right parser."""
    from ..detection.engine import detect as run_detect
    return get_parser(run_detect(path).detected_format)
