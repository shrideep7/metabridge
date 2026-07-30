"""Migration complexity engine: factor detection, scoring bands, rollup."""
from pathlib import Path

import pytest

from metabridge.ir.model import (
    IssueSeverity, LoadStrategy, Mapping, Pipeline, Transformation,
    TransformationType,
)
from metabridge.parsers.base import get_parser
from metabridge.report.complexity import LEVELS, score_mapping, score_pipeline

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _mapping(name="m", origin="", strategy=LoadStrategy.FULL,
             tx_count=3, lookups=0, deps=0) -> Mapping:
    m = Mapping(name=name, origin=origin, load_strategy=strategy,
                depends_on=["d%d" % i for i in range(deps)])
    for i in range(tx_count):
        m.transformations.append(Transformation(
            name="EXP_%d" % i, type=TransformationType.EXPRESSION))
    for i in range(lookups):
        m.transformations.append(Transformation(
            name="LKP_%d" % i, type=TransformationType.LOOKUP,
            properties={"table": "ref"}))
    return m


# ---------------------------------------------------------------------------
# Factor detection
# ---------------------------------------------------------------------------

def test_simple_mapping_is_low():
    a = score_mapping(_mapping(origin="select a from t"))
    assert a.complexity_level == "LOW"
    assert a.complexity_score < 20
    # module 31: Expression nodes score 95 by the transformation table
    assert a.conversion_confidence == 95
    assert a.automation_percentage == 100
    assert a.migration_risks == []


def test_nested_queries_counted_via_ast():
    origin = """with a as (select 1 x from t1), b as (select 2 y from t2)
                select * from a join b on a.x = b.y
                where a.x in (select x from allow_list)"""
    a = score_mapping(_mapping(origin=origin))
    assert a.factors["nested_queries"] >= 3        # 2 CTEs + 1 subquery


def test_dynamic_sql_forces_manual_review():
    a = score_mapping(_mapping(origin="EXECUTE IMMEDIATE 'select ' || col"))
    assert a.factors["dynamic_sql"] == 1
    assert a.complexity_level == "MANUAL_REVIEW_REQUIRED"
    assert any("Dynamic SQL" in r for r in a.migration_risks)
    assert a.conversion_confidence < 90


def test_recursive_and_cdc_and_scd_detected():
    a = score_mapping(_mapping(
        origin="WITH RECURSIVE r AS (SELECT 1) SELECT * FROM CHANGES(t)",
        strategy=LoadStrategy.SCD2))
    assert a.factors["recursive_sql"] == 1
    assert a.factors["cdc_logic"] == 1
    assert a.factors["scd_logic"] == 1
    assert a.complexity_score >= 30


def test_procedures_and_unsupported_functions_raise_score():
    m = _mapping(origin="BEGIN loop_stuff; END")
    m.add_issue(IssueSeverity.MANUAL, "STATEMENT_UNSUPPORTED",
                "proc", detail="CREATE PROCEDURE p AS ...")
    m.add_issue(IssueSeverity.MANUAL, "EXPRESSION_UNCONVERTED", "expr",
                detail="WEIRD_FN(a)")
    a = score_mapping(m)
    assert a.factors["stored_procedures"] == 1
    assert a.factors["unsupported_functions"] == 1
    assert a.factors["procedural_logic"] == 1
    assert a.complexity_score >= 40
    assert a.automation_percentage < 70
    assert a.manual_effort_estimate > 3


def test_lookups_and_dependencies_counted():
    a = score_mapping(_mapping(lookups=2, deps=3))
    assert a.factors["complex_lookups"] == 2
    assert a.factors["external_dependencies"] == 3
    assert any("lookup" in r.lower() for r in a.migration_risks)


def test_error_issue_forces_manual_review():
    m = _mapping()
    m.add_issue(IssueSeverity.ERROR, "SQL_PARSE_ERROR", "broken")
    a = score_mapping(m)
    assert a.complexity_level == "MANUAL_REVIEW_REQUIRED"


# ---------------------------------------------------------------------------
# Contract + rollup
# ---------------------------------------------------------------------------

def test_scores_within_bounds_on_real_project():
    p = get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))
    result = score_pipeline(p)
    assert 0 <= result["complexity_score"] <= 100
    assert 0 <= result["conversion_confidence"] <= 100
    assert 0 <= result["automation_percentage"] <= 100
    assert result["complexity_level"] in LEVELS
    assert result["manual_effort_estimate_hours"] > 0
    assert len(result["assets"]) == 5
    for a in result["assets"]:
        assert a["complexity_level"] in LEVELS
        assert 0 <= a["complexity_score"] <= 100
    # window-function fallback model must score above a clean staging model
    by_name = {a["name"]: a for a in result["assets"]}
    assert by_name["customer_ranking"]["complexity_score"] >= \
        by_name["stg_customers"]["complexity_score"]
    assert sum(result["level_distribution"].values()) == 5


def test_complexity_lands_in_audit_report(tmp_path):
    from metabridge.engine import convert
    report = convert(str(EXAMPLES / "dbt_retail"), str(tmp_path),
                     target_format="powercenter")
    assert "complexity" in report
    assert report["complexity"]["complexity_level"] in LEVELS
    row = next(m for m in report["mappings"] if m["name"] == "stg_orders")
    assert "complexity" in row
    assert row["complexity"]["complexity_level"] in LEVELS


def test_serializable():
    import json
    p = get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))
    json.dumps(score_pipeline(p))
