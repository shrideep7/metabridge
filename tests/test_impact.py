"""Impact analysis engine: entity kinds, blast radius, risk, reports."""
from pathlib import Path

import pytest

from metabridge.parsers.base import get_parser
from metabridge.report.impact import RISK_LEVELS, analyze_impact

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def retail():
    return get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))


# ---------------------------------------------------------------------------
# Column impact — the spec question
# ---------------------------------------------------------------------------

def test_customer_id_datatype_change(retail):
    """'What happens if CUSTOMER_ID datatype changes?'"""
    r = analyze_impact(retail, "raw_customers.id")
    assert r["entity_type"] == "column"
    # direct: the staging model's renamed column
    assert "stg_customers.customer_id" in r["direct_dependencies"]
    # indirect: it propagates through the join/aggregation to the mart
    assert "customer_orders.customer_id" in r["indirect_dependencies"]
    assert {"stg_customers", "customer_orders"} <= set(r["affected_pipelines"])
    assert "customer_orders" in r["affected_target_tables"]
    assert r["risk_level"] in ("HIGH", "CRITICAL")
    # evidence chains are returned
    assert any("raw_customers.id" in p for p in r["evidence_paths"])


def test_column_used_as_merge_key_is_critical(retail):
    r = analyze_impact(retail, "raw_orders.order_id")
    assert any("merge/unique key" in n for n in r["risk_factors"])
    assert r["risk_level"] == "CRITICAL"


def test_join_and_grain_usage_flagged(retail):
    r = analyze_impact(retail, "stg_customers.customer_id")
    notes = " | ".join(r["risk_factors"])
    assert "join condition" in notes
    assert "aggregation grain" in notes


def test_watermark_column_flagged(retail):
    r = analyze_impact(retail, "raw_orders.updated_at")
    assert any("incremental watermark" in n for n in r["risk_factors"])


# ---------------------------------------------------------------------------
# Table / model / transformation impact
# ---------------------------------------------------------------------------

def test_table_impact(retail):
    r = analyze_impact(retail, "stg_orders", entity_type="table")
    assert set(r["direct_dependencies"]) == {"customer_orders", "daily_revenue"}
    assert "customer_ranking" in r["indirect_dependencies"]
    assert r["risk_level"] in ("MEDIUM", "HIGH", "CRITICAL")


def test_model_impact_uses_its_target(retail):
    r = analyze_impact(retail, "customer_orders")
    assert r["entity_type"] == "model"
    assert r["direct_dependencies"] == ["customer_ranking"]
    assert r["affected_dbt_models"] == ["customer_ranking"]


def test_terminal_model_has_no_impact(retail):
    r = analyze_impact(retail, "customer_ranking")
    assert r["risk_level"] == "NONE"
    assert r["direct_dependencies"] == []
    assert r["indirect_dependencies"] == []


def test_transformation_impact(retail):
    r = analyze_impact(retail, "FIL_INCREMENTAL", entity_type="transformation")
    assert "stg_orders" in r["affected_pipelines"]
    assert "stg_orders" in r["affected_target_tables"]
    # everything downstream of stg_orders' table is in the blast radius
    assert {"customer_orders", "daily_revenue"} <= set(r["affected_target_tables"])


# ---------------------------------------------------------------------------
# Reports metadata
# ---------------------------------------------------------------------------

def test_reports_affected_when_catalog_provided(retail):
    catalog = [
        {"name": "Executive Revenue Dashboard", "tables": ["daily_revenue"]},
        {"name": "Customer 360", "columns": ["customer_orders.lifetime_value"]},
        {"name": "Unrelated Ops Report", "tables": ["warehouse_stock"]},
    ]
    r = analyze_impact(retail, "stg_orders", entity_type="table",
                       reports_catalog=catalog)
    names = {x["name"] for x in r["affected_reports"]}
    assert "Executive Revenue Dashboard" in names
    assert "Unrelated Ops Report" not in names
    assert r["reports_metadata_provided"] is True


def test_reports_empty_without_catalog(retail):
    r = analyze_impact(retail, "stg_orders", entity_type="table")
    assert r["affected_reports"] == []
    assert r["reports_metadata_provided"] is False


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

def test_contract_and_serializable(retail):
    import json
    r = analyze_impact(retail, "raw_customers.id")
    for key in ("direct_dependencies", "indirect_dependencies",
                "affected_pipelines", "affected_dbt_models",
                "affected_target_tables", "affected_reports", "risk_level"):
        assert key in r, key
    assert r["risk_level"] in RISK_LEVELS
    json.dumps(r)
