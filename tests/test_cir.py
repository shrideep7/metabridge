"""Canonical Intermediate Representation: semantic model + builder."""
import json
from pathlib import Path

import pytest

from metabridge.cir import model as cir
from metabridge.cir.builder import build_cir
from metabridge.cir.semantic import (
    SemanticFunction, parse_expression, render_expression,
)
from metabridge.parsers.base import get_parser

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


# ---------------------------------------------------------------------------
# Semantic expressions — intent, not syntax
# ---------------------------------------------------------------------------

def test_nvl_example_from_spec():
    """Oracle NVL(customer_name, 'UNKNOWN') -> NULL_COALESCE -> COALESCE(...)"""
    sem = parse_expression("NVL(customer_name, 'UNKNOWN')", dialect="oracle")
    assert sem.function_type == SemanticFunction.NULL_COALESCE
    assert sem.arguments == ["customer_name", "'UNKNOWN'"]
    for target in ("snowflake", "databricks", "tsql"):
        assert sem.render(target) == "COALESCE(customer_name, 'UNKNOWN')"
    # and the Informatica expression language target
    assert sem.render("informatica") == \
        "IIF(ISNULL(customer_name), 'UNKNOWN', customer_name)"


@pytest.mark.parametrize("sql,dialect,expected", [
    ("ISNULL(a, 0)", "tsql", SemanticFunction.NULL_COALESCE),
    ("IFNULL(a, 0)", "", SemanticFunction.NULL_COALESCE),
    ("NULLIF(a, b)", "", SemanticFunction.NULL_IF),
    ("CASE WHEN a > 1 THEN 'x' ELSE 'y' END", "", SemanticFunction.CONDITIONAL),
    ("CASE status WHEN 'A' THEN 1 ELSE 0 END", "", SemanticFunction.VALUE_MAP),
    ("DECODE(status, 'A', 1, 0)", "oracle", SemanticFunction.VALUE_MAP),
    ("CAST(x AS INT)", "", SemanticFunction.CAST),
    ("TRY_CAST(x AS INT)", "tsql", SemanticFunction.SAFE_CAST),
    ("DATEADD(day, 1, d)", "snowflake", SemanticFunction.DATE_ADD),
    ("DATEDIFF(day, a, b)", "snowflake", SemanticFunction.DATE_DIFF),
    ("DATE_TRUNC('month', d)", "snowflake", SemanticFunction.DATE_TRUNC),
    ("EXTRACT(year FROM d)", "", SemanticFunction.DATE_PART),
    ("UPPER(name)", "", SemanticFunction.STRING_UPPER),
    ("a || b", "", SemanticFunction.STRING_CONCAT),
    ("SUBSTRING(x, 1, 3)", "", SemanticFunction.STRING_SUBSTRING),
    ("name LIKE 'A%'", "", SemanticFunction.PATTERN_MATCH),
    ("ROUND(x, 2)", "", SemanticFunction.MATH_ROUND),
    ("a + b", "", SemanticFunction.ARITHMETIC),
    ("SUM(amount)", "", SemanticFunction.AGGREGATE),
    ("customer_id", "", SemanticFunction.COLUMN_REF),
    ("'UNKNOWN'", "", SemanticFunction.LITERAL),
    ("a > 5", "", SemanticFunction.COMPARISON),
    ("a > 1 AND b < 2", "", SemanticFunction.BOOLEAN_LOGIC),
    ("x IN (1, 2)", "", SemanticFunction.MEMBERSHIP),
    ("$$LAST_RUN_TS", "", SemanticFunction.PARAMETER_REF),
])
def test_semantic_classification(sql, dialect, expected):
    assert parse_expression(sql, dialect).function_type == expected


def test_semantic_normalization_across_platforms():
    """Three platforms' null-coalesce spellings collapse to ONE semantic node."""
    oracle = parse_expression("NVL(a, 0)", "oracle")
    tsql = parse_expression("ISNULL(a, 0)", "tsql")
    ansi = parse_expression("COALESCE(a, 0)", "")
    assert oracle.function_type == tsql.function_type == ansi.function_type \
        == SemanticFunction.NULL_COALESCE
    assert oracle.render("postgres") == tsql.render("postgres") \
        == ansi.render("postgres")


def test_render_never_mangles_on_failure():
    weird = "SOME_MADE_UP_FN(a, b)"
    assert render_expression(weird, "snowflake")  # returns something sensible
    sem = parse_expression(weird)
    assert sem.function_type == SemanticFunction.UNKNOWN
    assert sem.raw_sql  # original preserved


# ---------------------------------------------------------------------------
# CIR entities
# ---------------------------------------------------------------------------

def test_all_spec_entities_exist():
    for entity in ("Project", "Asset", "Pipeline", "Workflow", "Task",
                   "Transformation", "Dataset", "Table", "View", "Column",
                   "Expression", "Join", "Filter", "Aggregation",
                   "WindowFunction", "Lookup", "Router", "Union", "Sequence",
                   "StoredProcedure", "Macro", "Variable", "Parameter",
                   "Connection", "Dependency", "DataQualityRule", "Test",
                   "Schedule", "RuntimeConfiguration"):
        assert hasattr(cir, entity), entity


def test_all_spec_transformation_types_exist():
    values = {t.value for t in cir.CirTransformationType}
    assert {"SOURCE", "TARGET", "EXPRESSION", "FILTER", "JOIN", "LOOKUP",
            "AGGREGATOR", "SORTER", "ROUTER", "UNION", "SEQUENCE", "RANK",
            "WINDOW", "NORMALIZER", "DENORMALIZER", "PIVOT", "UNPIVOT", "SQL",
            "PROCEDURE", "MACRO", "INCREMENTAL", "MERGE", "SNAPSHOT", "CDC",
            "SCD_TYPE_1", "SCD_TYPE_2"} == values


def test_ids_are_deterministic():
    a = cir.make_id("tx", "dbt", "m1", "FIL_1")
    b = cir.make_id("tx", "dbt", "m1", "FIL_1")
    c = cir.make_id("tx", "dbt", "m1", "FIL_2")
    assert a == b and a != c and a.startswith("tx_")


# ---------------------------------------------------------------------------
# Builder — full project elevation
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def project():
    ir = get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))
    return build_cir(ir)


def test_transformation_contract(project):
    tx = next(t for p in project.pipelines for t in p.transformations)
    d = tx.to_dict()
    for key in ("id", "name", "source_platform", "transformation_type",
                "inputs", "outputs", "input_columns", "output_columns",
                "expressions", "business_rules", "dependencies", "metadata",
                "source_location", "source_lineage", "conversion_notes",
                "confidence_score"):
        assert key in d, key


def test_pipeline_graph_and_types(project):
    stg = project.pipeline("stg_orders")
    types = {t.transformation_type for t in stg.transformations}
    assert cir.CirTransformationType.SOURCE in types
    assert cir.CirTransformationType.FILTER in types
    assert cir.CirTransformationType.MERGE in types      # incremental target
    # graph wiring: filter has inputs and outputs
    fil = next(t for t in stg.transformations
               if t.transformation_type == cir.CirTransformationType.FILTER)
    assert fil.inputs and fil.outputs
    assert fil.input_columns


def test_semantic_expressions_in_pipeline(project):
    stg = project.pipeline("stg_customers")
    sems = [e for t in stg.transformations for e in t.expressions]
    fns = {e.semantic["function_type"] for e in sems}
    assert "VALUE_MAP" in fns          # CASE status WHEN 'A' ...
    assert "STRING_LOWER" in fns or "STRING_UPPER" in fns


def test_sql_fallback_becomes_sql_type_with_window_rule(project):
    ranking = project.pipeline("customer_ranking")
    sql_tx = next(t for t in ranking.transformations
                  if t.transformation_type == cir.CirTransformationType.SQL)
    assert sql_tx.confidence_score < 0.8
    assert any(getattr(r, "rule_type", "") == "window"
               for r in sql_tx.business_rules)
    assert ranking.confidence_score < project.pipeline("daily_revenue").confidence_score


def test_business_rules_typed(project):
    co = project.pipeline("customer_orders")
    rules = [r for t in co.transformations for r in t.business_rules]
    join = next(r for r in rules if isinstance(r, cir.Join))
    assert join.join_type == "LEFT"
    agg = next(r for r in rules if isinstance(r, cir.Aggregation))
    assert "customer_id" in agg.group_by
    stg = project.pipeline("stg_orders")
    wm = next(r for t in stg.transformations for r in t.business_rules
              if isinstance(r, cir.Filter) and r.is_incremental_watermark)
    assert "$$LAST_RUN_TS" in wm.condition


def test_dq_rules_and_tests_from_dbt(project):
    rules = {(r.dataset, r.rule, r.column) for r in project.data_quality_rules}
    assert ("stg_orders", "unique", "order_id") in rules
    assert ("stg_orders", "not_null", "order_id") in rules
    assert project.tests


def test_workflow_parameters_datasets(project):
    wf = project.workflows[0]
    assert wf.execution_waves[0] == ["stg_customers", "stg_orders"]
    task = next(t for t in wf.tasks if t.name == "customer_orders")
    assert len(task.depends_on) == 2
    assert any(p.name == "LAST_RUN_TS" for p in project.runtime.parameters)
    names = {d.name for d in project.datasets}
    assert {"raw_customers", "raw_orders", "customer_orders"} <= names


def test_full_export_is_json_serializable(project):
    doc = project.to_dict()
    blob = json.dumps(doc)
    assert doc["cir_version"] == "1.0"
    assert len(blob) > 5000
    s = project.summary()
    assert s["pipelines"] == 5 and 0 < s["avg_confidence"] <= 1.0


def test_cir_from_powercenter_source_too(tmp_path):
    """CIR builds from ANY parser — prove with the PowerCenter round-trip."""
    from metabridge.generators.powercenter_generator import generate_powercenter
    ir = get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))
    xml = generate_powercenter(ir)
    ir2 = get_parser("powercenter").parse_asset(xml)
    project = build_cir(ir2)
    assert project.source_platform == "powercenter"
    assert len(project.pipelines) == 5
    assert project.pipeline("stg_orders").load_strategy == "MERGE"
