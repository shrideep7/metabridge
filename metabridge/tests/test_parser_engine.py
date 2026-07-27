"""Source parser engine: interface contract across all 13 parsers."""
from pathlib import Path

import pytest

from metabridge.parsers.base import (
    PARSER_CLASSES, BaseSourceParser, get_parser, list_parsers,
    parser_for_path,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_has_all_eighteen_parsers():
    formats = {c.format_name for c in PARSER_CLASSES}
    assert formats == {"dbt", "powercenter", "idmc", "snowflake", "databricks",
                       "bigquery", "redshift", "synapse", "sqlserver", "oracle",
                       "postgres", "teradata", "sql",
                       "ssis", "datastage", "talend", "abinitio", "sap"}
    assert len(list_parsers()) == 18


def test_get_parser_and_unknown():
    assert get_parser("dbt").format_name == "dbt"
    assert get_parser("SNOWFLAKE").format_name == "snowflake"  # case-insensitive
    with pytest.raises(ValueError):
        get_parser("ab_initio_binary")


def test_every_parser_implements_the_interface():
    for cls in PARSER_CLASSES:
        p = cls()
        assert isinstance(p, BaseSourceParser)
        for method in ("detect", "parse_project", "parse_asset",
                       "extract_metadata", "extract_dependencies",
                       "extract_business_rules", "extract_expressions",
                       "extract_source_targets"):
            assert callable(getattr(p, method)), (cls.__name__, method)


def test_parser_for_path_autodetects():
    assert parser_for_path(str(EXAMPLES / "dbt_retail")).format_name == "dbt"
    assert parser_for_path(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml")
    ).format_name == "powercenter"


def test_detect_confidence_right_vs_wrong_format():
    dbt = get_parser("dbt")
    oracle = get_parser("oracle")
    path = str(EXAMPLES / "dbt_retail")
    assert dbt.detect(path) >= 0.7
    assert oracle.detect(path) < dbt.detect(path)


# ---------------------------------------------------------------------------
# extract_* contract (IR-derived, so one fixture proves them all)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dbt_pipeline():
    return get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))


def test_extract_metadata(dbt_pipeline):
    meta = get_parser("dbt").extract_metadata(dbt_pipeline)
    assert meta["format"] == "dbt"
    assert meta["project"] == "retail_analytics"
    assert meta["mappings"] == 5
    assert meta["sources"] == 2
    assert meta["load_strategies"]["MERGE"] == 1
    assert meta["transformations"] > 10


def test_extract_dependencies(dbt_pipeline):
    deps = get_parser("dbt").extract_dependencies(dbt_pipeline)
    assert deps["dependencies"]["customer_orders"] == ["stg_customers", "stg_orders"]
    assert deps["execution_order"][0] == ["stg_customers", "stg_orders"]


def test_extract_business_rules(dbt_pipeline):
    rules = get_parser("dbt").extract_business_rules(dbt_pipeline)
    kinds = {r["rule_type"] for r in rules}
    assert {"filter", "join", "aggregation", "incremental_watermark",
            "unique_key", "load_strategy"} <= kinds
    join = next(r for r in rules if r["rule_type"] == "join")
    assert join["detail"]["join_type"] == "LEFT"
    watermark = next(r for r in rules if r["rule_type"] == "incremental_watermark")
    assert "$$LAST_RUN_TS" in watermark["detail"]["condition"]


def test_extract_expressions(dbt_pipeline):
    exprs = get_parser("dbt").extract_expressions(dbt_pipeline)
    assert any(e["expression"].startswith("UPPER(") for e in exprs)
    assert any(e["port"] == "(sql_override)" for e in exprs)  # window-fn fallback
    assert all({"mapping", "transformation", "port", "expression"} == set(e)
               for e in exprs)


def test_extract_source_targets(dbt_pipeline):
    st = get_parser("dbt").extract_source_targets(dbt_pipeline)
    src_tables = {s["table"] for s in st["sources"]}
    assert {"raw_customers", "raw_orders"} <= src_tables
    tgt = next(t for t in st["targets"] if t["table"] == "stg_orders")
    assert tgt["load_strategy"] == "MERGE"
    assert tgt["unique_key"] == ["order_id"]


# ---------------------------------------------------------------------------
# parse_asset per family
# ---------------------------------------------------------------------------

def test_dbt_parse_asset_single_model():
    p = get_parser("dbt")
    pipe = p.parse_asset(
        "{{ config(materialized='table') }}\n"
        "select id, upper(name) as name from {{ ref('stg_x') }} where id > 0",
        name="my_model")
    assert [m.name for m in pipe.mappings] == ["my_model"]
    assert pipe.mappings[0].depends_on == ["stg_x"]
    rules = p.extract_business_rules(pipe)
    assert any(r["rule_type"] == "filter" for r in rules)


def test_sql_parse_asset_each_dialect():
    cases = {
        "snowflake": "CREATE OR REPLACE VIEW v AS SELECT ZEROIFNULL(a) AS a FROM t;",
        "oracle": "CREATE VIEW v AS SELECT NVL(a, 0) AS a FROM t;",
        "sqlserver": "CREATE VIEW v AS SELECT ISNULL(a, 0) AS a FROM t;",
        "postgres": "CREATE VIEW v AS SELECT COALESCE(a, 0) AS a FROM t;",
        "sql": "CREATE VIEW v AS SELECT a FROM t;",
    }
    for fmt, sql in cases.items():
        pipe = get_parser(fmt).parse_asset(sql, name="one")
        assert len(pipe.mappings) == 1, fmt
        assert pipe.mappings[0].name == "v"


def test_powercenter_parse_asset_roundtrip():
    from metabridge.generators.powercenter_generator import generate_powercenter
    src = get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))
    xml = generate_powercenter(src)
    pipe = get_parser("powercenter").parse_asset(xml, name="wf")
    assert {m.name for m in pipe.mappings} == {m.name for m in src.mappings}


def test_idmc_parse_asset_single_mapping():
    import json
    doc = json.dumps({"@type": "mapping", "name": "m_demo", "transformations": [
        {"name": "SRC_T", "type": "Source", "object": "t",
         "connection": {"schema": "raw"},
         "fields": [{"name": "a", "type": "integer"}]},
        {"name": "TGT_demo", "type": "Target",
         "fields": [{"name": "a", "type": "integer"}],
         "properties": {"table": "demo"}},
    ], "links": [{"from": "SRC_T", "to": "TGT_demo"}],
        "runtime": {"loadStrategy": "FULL"}})
    pipe = get_parser("idmc").parse_asset(doc, name="m_demo")
    assert [m.name for m in pipe.mappings] == ["demo"]
