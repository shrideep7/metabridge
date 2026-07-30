"""Aggregator handler: CIR contract, 11 functions, sorted input,
incremental aggregation."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.sqlx.expressions import infa_to_sql

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_XML = EXAMPLES / "powercenter_repo" / "repo_export.xml"


@pytest.fixture(scope="module")
def agg_mapping():
    return parse_input(str(REPO_XML), "powercenter").mapping("agg_sales")


# ---------------------------------------------------------------------------
# function conversions (semantic, engine-level)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("infa,sql", [
    ("SUM(x)", "SUM(x)"), ("AVG(x)", "AVG(x)"), ("MIN(x)", "MIN(x)"),
    ("MAX(x)", "MAX(x)"), ("COUNT(x)", "COUNT(x)"),
    ("STDDEV(x)", "STDDEV(x)"), ("VARIANCE(x)", "VARIANCE(x)"),
    ("MEDIAN(x)", "MEDIAN(x)"),
    ("PERCENTILE(x, 90)",
     "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY x)"),
    ("PERCENTILE(x, 0.5)",
     "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x)"),
    ("FIRST(x)", "MIN(x)"),
    ("LAST(x)", "MAX(x)"),
    ("FIRST(x, status = 'A')", "MIN(CASE WHEN status = 'A' THEN x END)"),
])
def test_all_eleven_aggregate_functions(infa, sql):
    assert infa_to_sql(infa) == sql


# ---------------------------------------------------------------------------
# CIR AGGREGATOR
# ---------------------------------------------------------------------------

def test_cir_aggregator_contract(agg_mapping):
    agg = agg_mapping.transformation("AGG_1")
    cir = agg.properties["aggregator_cir"]
    assert cir["group_by"] == ["region_up"]
    fns = {a["port"]: a["function"] for a in cir["aggregates"]}
    assert fns["total_amount"] == "SUM"
    assert fns["max_amount"] == "MAX"                # LAST -> MAX
    assert fns["p90_amount"] == "PERCENTILE_CONT"
    assert cir["sorted_input"] is True


def test_first_last_cache_order_flagged(agg_mapping):
    issue = next(i for i in agg_mapping.issues
                 if i.code == "AGG_FIRST_LAST_ORDER")
    assert issue.severity.value == "WARNING"
    assert "cache" in issue.message.lower()
    assert "MIN_BY" in issue.suggestion or "MAX_BY" in issue.suggestion


def test_sorted_input_is_recommendation_only(agg_mapping):
    issue = next(i for i in agg_mapping.issues
                 if i.code == "AGG_SORTED_INPUT")
    assert issue.severity.value == "INFO"
    assert "not a semantic change" in issue.message


# ---------------------------------------------------------------------------
# incremental aggregation (session property)
# ---------------------------------------------------------------------------

def test_incremental_aggregation_migration_warning(agg_mapping):
    assert agg_mapping.properties["incremental_aggregation"] is True
    issue = next(i for i in agg_mapping.issues
                 if i.code == "AGG_INCREMENTAL")
    assert issue.severity.value == "WARNING"
    assert "aggregate cache" in issue.message
    # target-specific incremental strategies recommended
    assert "dbt" in issue.suggestion
    assert "MERGE INTO" in issue.suggestion or \
        "materialized" in issue.suggestion


def test_other_mappings_not_flagged(agg_mapping):
    p = parse_input(str(REPO_XML), "powercenter")
    assert "incremental_aggregation" not in \
        p.mapping("load_sales").properties


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------

def test_dbt_group_by_cte(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    convert(str(REPO_XML), str(tmp_path / "out"),
            source_format="powercenter", target_format="dbt")
    sql = next((tmp_path / "out" / "dbt").rglob("int_agg_sales.sql")).read_text()
    assert "group by" in sql
    assert "SUM(amount) as total_amount" in sql
    assert "MAX(amount) as max_amount" in sql
    assert "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY amount)" in sql


def test_databricks_spark_aggregation(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    rep = convert(str(REPO_XML), str(tmp_path / "out"),
                  source_format="powercenter", target_format="databricks")
    sql = next((tmp_path / "out" / "sql").glob(
        "0*_agg_sales.sql")).read_text()
    assert "GROUP BY" in sql.upper()
    assert "SUM(amount)" in sql
    assert rep["conversion_output"]["errors"]["count"] == 0
