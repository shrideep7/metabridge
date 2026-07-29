"""Target generator engine: one interface, thirteen generators, CIR in.

``BaseTargetGenerator.generate(source, out_dir)`` accepts either a CIR
``Project`` (the module contract — reversed to the working IR internally) or
an IR ``Pipeline`` directly, and produces platform-native output:

  * dbt          — complete layered project (staging/intermediate/marts,
                   sources.yml, schema.yml, snapshots/, macros/, tests/)
  * PowerCenter  — import-compatible POWERMART XML (mappings, transformations,
                   connectors, instances, sessions, workflows) with schema
                   validation run on the result
  * IDMC         — asset bundle + migration specification (manifest with
                   deployment instructions)
  * 10 warehouses — dialect-native SQL plus platform capability files
                   (Delta OPTIMIZE/ZORDER, Snowflake clustering + stream/task
                   templates, partition/DISTKEY/PRIMARY INDEX recommendations)

Never generic SQL with a renamed extension — a test pins that the same IR
produces different, dialect-correct artifacts per platform.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Type, Union

from ..cir import model as cir
from ..ir.model import Pipeline as IrPipeline


@dataclass
class GenerationResult:
    format: str
    output_dir: str
    files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    validation: Optional[dict] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _as_ir(source: Union[cir.Project, IrPipeline]) -> IrPipeline:
    if isinstance(source, IrPipeline):
        return source
    if isinstance(source, cir.Project):
        from ..cir.reverse import cir_to_ir
        return cir_to_ir(source)
    raise TypeError("generate() expects a CIR Project or an IR Pipeline, "
                    "got %s" % type(source).__name__)


def _collect_files(out: Path) -> List[str]:
    return sorted(str(f.relative_to(out)) for f in out.rglob("*") if f.is_file())


def _warnings_of(ir: IrPipeline) -> List[str]:
    return ["[%s] %s: %s" % (i.severity.value, i.code, i.message)
            for i in ir.all_issues() if i.severity.value in ("WARNING", "MANUAL")]


class BaseTargetGenerator(abc.ABC):
    format_name: str = ""
    display_name: str = ""
    dialect: str = ""

    def generate(self, source: Union[cir.Project, IrPipeline],
                 out_dir: str) -> GenerationResult:
        ir = _as_ir(source)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        result = self._generate(ir, out)
        result.files = result.files or _collect_files(out)
        result.warnings = result.warnings or _warnings_of(ir)
        return result

    @abc.abstractmethod
    def _generate(self, ir: IrPipeline, out: Path) -> GenerationResult:
        ...


class DbtGenerator(BaseTargetGenerator):
    format_name, display_name = "dbt", "dbt project (layered)"

    def _generate(self, ir: IrPipeline, out: Path) -> GenerationResult:
        from .dbt_generator import generate_dbt_project
        generate_dbt_project(ir, str(out), layout="layered")
        return GenerationResult(
            format=self.format_name, output_dir=str(out),
            notes=["layered layout: models/staging|intermediate|marts, "
                   "snapshots/, macros/, tests/; ref()/source() dependencies "
                   "resolved"])


class PowerCenterGenerator(BaseTargetGenerator):
    format_name, display_name = "powercenter", "Informatica PowerCenter XML"

    def _generate(self, ir: IrPipeline, out: Path) -> GenerationResult:
        from ..validate.powercenter_validator import validate_powercenter_xml
        from .powercenter_generator import generate_powercenter
        xml = generate_powercenter(ir)
        xml_file = out / ("wf_%s.xml" % _safe(ir.name))
        xml_file.write_text(xml, encoding="utf-8")
        validation = validate_powercenter_xml(str(xml_file)).to_dict()
        return GenerationResult(
            format=self.format_name, output_dir=str(out),
            validation=validation,
            notes=["import-compatible POWERMART XML: sources, targets, "
                   "mappings, transformations, connectors, instances, "
                   "sessions, workflow; schema-validated"])


class IDMCGenerator(BaseTargetGenerator):
    format_name, display_name = "idmc", "Informatica IDMC bundle"

    def _generate(self, ir: IrPipeline, out: Path) -> GenerationResult:
        from .idmc_generator import generate_idmc
        generate_idmc(ir, str(out))
        return GenerationResult(
            format=self.format_name, output_dir=str(out),
            notes=["asset bundle + migration specification (manifest.json "
                   "carries deployment instructions for the v3 REST API)"])


class SqlTargetGenerator(BaseTargetGenerator):
    """Shared behavior for the warehouse-SQL generators."""

    def _generate(self, ir: IrPipeline, out: Path) -> GenerationResult:
        from .sql_generator import generate_sql_scripts
        generate_sql_scripts(ir, str(out), self.format_name, self.dialect)
        return GenerationResult(format=self.format_name, output_dir=str(out),
                                notes=self._notes())

    def _notes(self) -> List[str]:
        return ["dialect-native SQL in dependency order (deploy_all.sql)"]


class SnowflakeGenerator(SqlTargetGenerator):
    format_name, display_name, dialect = "snowflake", "Snowflake SQL", "snowflake"

    def _notes(self) -> List[str]:
        return ["Snowflake-native SQL; clustering recommendations and "
                "stream/task automation templates for incremental pipelines"]


class DatabricksGenerator(SqlTargetGenerator):
    format_name, display_name, dialect = "databricks", "Databricks (Delta Lake)", "databricks"

    def _notes(self) -> List[str]:
        return ["Delta Lake CTAS (USING DELTA), MERGE INTO, OPTIMIZE/ZORDER "
                "maintenance and partition recommendations"]


class BigQueryGenerator(SqlTargetGenerator):
    format_name, display_name, dialect = "bigquery", "Google BigQuery SQL", "bigquery"


class RedshiftGenerator(SqlTargetGenerator):
    format_name, display_name, dialect = "redshift", "Amazon Redshift SQL", "redshift"


class SynapseFabricGenerator(SqlTargetGenerator):
    format_name, display_name, dialect = "synapse", "Azure Synapse / Fabric SQL", "tsql"


class TSQLGenerator(SqlTargetGenerator):
    format_name, display_name, dialect = "sqlserver", "SQL Server (T-SQL)", "tsql"


class OracleGenerator(SqlTargetGenerator):
    format_name, display_name, dialect = "oracle", "Oracle SQL", "oracle"


class PostgreSQLGenerator(SqlTargetGenerator):
    format_name, display_name, dialect = "postgres", "PostgreSQL", "postgres"


class TeradataGenerator(SqlTargetGenerator):
    format_name, display_name, dialect = "teradata", "Teradata SQL", "teradata"


class AnsiSQLGenerator(SqlTargetGenerator):
    format_name, display_name, dialect = "sql", "Generic ANSI SQL", ""


GENERATOR_CLASSES: List[Type[BaseTargetGenerator]] = [
    DbtGenerator, PowerCenterGenerator, IDMCGenerator,
    SnowflakeGenerator, DatabricksGenerator, BigQueryGenerator,
    RedshiftGenerator, SynapseFabricGenerator, TSQLGenerator,
    OracleGenerator, PostgreSQLGenerator, TeradataGenerator, AnsiSQLGenerator,
]

_REGISTRY: Dict[str, Type[BaseTargetGenerator]] = {
    cls.format_name: cls for cls in GENERATOR_CLASSES}


def get_generator(format_name: str) -> BaseTargetGenerator:
    cls = _REGISTRY.get(str(format_name).lower())
    if cls is None:
        raise ValueError("No generator for format '%s' (available: %s)"
                         % (format_name, ", ".join(sorted(_REGISTRY))))
    return cls()


def list_generators() -> List[dict]:
    return [{"format": cls.format_name, "name": cls.display_name,
             "dialect": cls.dialect} for cls in GENERATOR_CLASSES]


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)
