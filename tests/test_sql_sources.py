"""Warehouse-SQL source formats (Snowflake, Databricks, ...) → IR → targets."""
from pathlib import Path

import pytest

from metabridge.engine import FORMATS, convert, detect_format, parse_input
from metabridge.parsers.sql_parser import SQL_DIALECT_FORMATS, parse_sql_scripts

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
SNOW = str(EXAMPLES / "snowflake_sql")


def test_formats_include_warehouse_dialects():
    for fmt in ("snowflake", "databricks", "bigquery", "redshift", "synapse",
                "sqlserver", "oracle", "postgres", "teradata", "sql"):
        assert fmt in FORMATS
        assert fmt in SQL_DIALECT_FORMATS


def test_detect_sql_directory():
    # the detection engine recognizes the dialect, not just "some SQL"
    assert detect_format(SNOW) == "snowflake"


def test_parse_snowflake_scripts():
    p = parse_sql_scripts(SNOW, "snowflake")
    names = {m.name for m in p.mappings}
    assert names == {"STG_EVENTS", "DAILY_EVENT_SUMMARY", "USER_ACTIVITY"}

    stg = p.mapping("STG_EVENTS")
    assert stg.load_strategy.value == "VIEW"

    ctas = p.mapping("DAILY_EVENT_SUMMARY")
    assert ctas.load_strategy.value == "FULL"
    assert ctas.depends_on == ["STG_EVENTS"]

    merge = p.mapping("USER_ACTIVITY")
    assert merge.load_strategy.value == "MERGE"
    assert merge.unique_key == ["USER_ID"]

    # DDL table registered as a typed source
    raw = next(s for s in p.sources if s.name == "RAW_EVENTS")
    types = {c.name: c.datatype for c in raw.columns}
    assert types["EVENT_TS"] == "timestamp"
    assert types["AMOUNT"] == "decimal"
    # derived view registered with its full output schema for consumers
    stg_src = next(s for s in p.sources if s.name == "STG_EVENTS")
    assert {c.name for c in stg_src.columns} == {
        "EVENT_ID", "USER_ID", "EVENT_TYPE", "EVENT_TS", "AMOUNT", "USER_EMAIL"}


def test_snowflake_to_dbt(tmp_path):
    report = convert(SNOW, str(tmp_path), source_format="snowflake",
                     target_format="dbt")
    assert report["summary"]["automated_conversion_rate"] == 100.0
    # one model per mapping: the logic and the incremental config are in the
    # same file, and the layer follows what the mapping does
    mart = next((tmp_path / "dbt" / "models").rglob(
        "*user_activity.sql")).read_text()
    events = next((tmp_path / "dbt" / "models").rglob("*events*.sql")).stem
    assert "{{ ref('%s') }}" % events in mart
    assert "materialized='incremental'" in mart
    assert "unique_key='USER_ID'" in mart


def test_snowflake_to_powercenter_validates(tmp_path):
    report = convert(SNOW, str(tmp_path), source_format="snowflake",
                     target_format="powercenter")
    assert report["summary"]["automated_conversion_rate"] == 100.0
    assert report["validation"]["ok"], report["validation"]["findings"]


def test_default_target_for_sql_sources(tmp_path):
    report = convert(SNOW, str(tmp_path), source_format="snowflake")
    assert report["target_format"] == "dbt"


def test_databricks_dialect(tmp_path):
    f = tmp_path / "v.sql"
    f.write_text("CREATE OR REPLACE VIEW clean_sales AS "
                 "SELECT order_id, upper(region) AS region, "
                 "date_add(order_date, 1) AS next_day "
                 "FROM raw_sales WHERE amount > 0")
    p = parse_input(str(tmp_path), "databricks")
    assert [m.name for m in p.mappings] == ["clean_sales"]
    assert p.metadata["dialect"] == "databricks"


def test_unsupported_statement_is_inventoried(tmp_path):
    f = tmp_path / "p.sql"
    f.write_text("CREATE VIEW v1 AS SELECT a FROM t1;\n"
                 "CALL some_procedure('x');")
    p = parse_sql_scripts(str(tmp_path), "snowflake")
    assert len(p.mappings) == 1
    assert any(i.code == "STATEMENT_UNSUPPORTED" for i in p.issues)


# ---------------------------------------------------------------------------
# Warehouse SQL as TARGET formats
# ---------------------------------------------------------------------------

def test_dbt_to_snowflake_scripts(tmp_path):
    report = convert(str(EXAMPLES / "dbt_retail"), str(tmp_path),
                     target_format="snowflake")
    assert report["target_format"] == "snowflake"
    sql_dir = tmp_path / "sql"
    files = sorted(f.name for f in sql_dir.glob("*.sql"))
    assert "deploy_all.sql" in files
    assert "00_sources_ddl.sql" in files

    merge = next(sql_dir.glob("*stg_orders.sql")).read_text()
    assert merge.startswith("-- parameters")
    assert ":LAST_RUN_TS" in merge and "$$" not in merge
    assert "MERGE INTO stg_orders t" in merge
    assert "ON t.order_id = s.order_id" in merge

    view_or_table = next(sql_dir.glob("*customer_orders.sql")).read_text()
    assert "CREATE OR REPLACE TABLE customer_orders" in view_or_table

    ddl = (sql_dir / "00_sources_ddl.sql").read_text()
    assert "RAW.raw_orders" in ddl


def test_powercenter_to_databricks_scripts(tmp_path):
    report = convert(str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
                     str(tmp_path), target_format="databricks")
    assert report["summary"]["automated_conversion_rate"] == 100.0
    assert list((tmp_path / "sql").glob("*daily_revenue.sql"))


def test_sqlserver_target_uses_select_into(tmp_path):
    convert(str(EXAMPLES / "dbt_retail"), str(tmp_path), target_format="sqlserver")
    t = next((tmp_path / "sql").glob("*customer_orders.sql")).read_text()
    assert "SELECT * INTO customer_orders" in t


def test_snowflake_to_snowflake_rejected(tmp_path):
    with pytest.raises(ValueError):
        convert(SNOW, str(tmp_path), source_format="snowflake",
                target_format="snowflake")


def test_template_variables_do_not_crash(tmp_path):
    """Regression: schemachange &{var} templating (sf-samples) caused a 500."""
    f = tmp_path / "V1.1__idr.sql"
    f.write_text(
        "INSERT INTO &{deployment_db}.SILVER.IDR_INCLUDE "
        "SELECT id, name FROM &{deployment_db}.BRONZE.RAW_IDR;\n"
        "CREATE OR REPLACE VIEW &{deployment_db}.GOLD.V_IDR AS "
        "SELECT id FROM &{deployment_db}.SILVER.IDR_INCLUDE;\n")
    p = parse_sql_scripts(str(tmp_path), "snowflake")
    # both statements survive as pipelines with shielded identifiers
    assert len(p.mappings) == 2
    assert any(i.code == "TEMPLATE_VARIABLES" for i in p.issues)


def test_hostile_statement_never_raises(tmp_path):
    f = tmp_path / "bad.sql"
    f.write_text("INSERT INTO &{a} &{b}..X SELECT 1;\n"
                 "THIS IS NOT SQL AT ALL ;;; &&& {{{}}};\n"
                 "CREATE VIEW ok_view AS SELECT 1 AS a FROM dual;")
    p = parse_sql_scripts(str(tmp_path), "snowflake")  # must not raise
    assert p.mapping("ok_view") is not None or p.issues
