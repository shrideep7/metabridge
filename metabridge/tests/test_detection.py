"""Format detection engine: one fingerprint test per platform + edge cases."""
from pathlib import Path

import pytest

from metabridge.detection.engine import detect

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _detect_sql(tmp_path, sql: str):
    (tmp_path / "sample.sql").write_text(sql)
    return detect(str(tmp_path))


# ---------------------------------------------------------------------------
# Project formats
# ---------------------------------------------------------------------------

def test_detect_dbt_project():
    r = detect(str(EXAMPLES / "dbt_retail"))
    assert r.detected_format == "dbt"
    assert r.confidence_score >= 0.7
    assert any("dbt_project.yml" in reason for reason in r.detection_reasons)
    assert "{{ source( }}" in r.detected_features or "{{ ref( }}" in r.detected_features


def test_detect_dbt_by_jinja_only(tmp_path):
    (tmp_path / "model.sql").write_text(
        "select * from {{ ref('stg_orders') }} where {{ config(materialized='table') }}")
    r = detect(str(tmp_path))
    assert r.detected_format == "dbt"


def test_detect_powercenter():
    r = detect(str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"))
    assert r.detected_format == "powercenter"
    assert r.confidence_score >= 0.7
    assert "POWERMART" in r.detected_features


def test_detect_idmc(tmp_path):
    import json
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"bundleType": "metabridge.idmc.bundle", "objects": []}))
    (tmp_path / "m_x.json").write_text(json.dumps(
        {"@type": "mapping", "name": "m_x", "transformations": []}))
    r = detect(str(tmp_path))
    assert r.detected_format == "idmc"


# ---------------------------------------------------------------------------
# SQL dialects
# ---------------------------------------------------------------------------

def test_detect_snowflake(tmp_path):
    r = _detect_sql(tmp_path, """
        CREATE OR REPLACE TASK nightly WAREHOUSE=WH AS
        SELECT v:payload::VARCHAR, ZEROIFNULL(amt)
        FROM raw, LATERAL FLATTEN(input => v:items) f
        QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts) = 1;
        COPY INTO t FROM @stage;""")
    assert r.detected_format == "snowflake"
    assert "LATERAL FLATTEN" in r.detected_features


def test_detect_databricks(tmp_path):
    r = _detect_sql(tmp_path, """
        CREATE TABLE events USING DELTA TBLPROPERTIES ('delta.appendOnly'='true');
        OPTIMIZE events ZORDER BY (event_date);
        VACUUM events RETAIN 168 HOURS;
        SELECT from_json(payload, 'STRUCT<id:INT>') FROM raw
        LATERAL VIEW EXPLODE(items) t AS item;""")
    assert r.detected_format == "databricks"
    assert "USING DELTA" in r.detected_features


def test_detect_bigquery(tmp_path):
    r = _detect_sql(tmp_path, """
        CREATE OR REPLACE TABLE `my-proj.analytics.orders` AS
        SELECT SAFE_CAST(amount AS NUMERIC), item
        FROM `my-proj.raw.events`, UNNEST(items) AS item
        QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts) = 1;""")
    assert r.detected_format == "bigquery"
    assert "`project.dataset.table` identifier" in r.detected_features


def test_detect_redshift(tmp_path):
    r = _detect_sql(tmp_path, """
        CREATE TABLE sales (id INT IDENTITY(1,1), amt DECIMAL(18,2) ENCODE az64)
        DISTKEY(id) SORTKEY(sale_date) DISTSTYLE KEY;
        COPY sales FROM 's3://bucket/data' IAM_ROLE 'arn:aws:iam::1:role/x';""")
    assert r.detected_format == "redshift"
    assert "DISTKEY" in r.detected_features


def test_detect_synapse(tmp_path):
    r = _detect_sql(tmp_path, """
        CREATE TABLE dbo.FactSales WITH (DISTRIBUTION = HASH(sale_id)) AS SELECT 1 a;
        SELECT * FROM OPENROWSET(BULK 'file.parquet', FORMAT='PARQUET') AS rows;""")
    assert r.detected_format == "synapse"


def test_detect_sqlserver(tmp_path):
    r = _detect_sql(tmp_path, """
        SELECT TOP 100 o.id, ISNULL(o.total, 0), TRY_CONVERT(NVARCHAR(50), o.ref)
        FROM [dbo].[orders] o WITH (NOLOCK)
        CROSS APPLY dbo.fn_items(o.id) i
        WHERE o.created > DATEADD(day, -7, GETDATE())
        GO""")
    assert r.detected_format == "sqlserver"
    assert "CROSS APPLY" in r.detected_features


def test_detect_oracle(tmp_path):
    r = _detect_sql(tmp_path, """
        SELECT NVL(mgr_id, -1), SYSDATE FROM employees
        WHERE ROWNUM <= 10
        CONNECT BY PRIOR emp_id = mgr_id START WITH mgr_id IS NULL;
        SELECT 1 FROM DUAL;
        CREATE TABLE t (name VARCHAR2(100), amt NUMBER(18,2));""")
    assert r.detected_format == "oracle"
    assert "CONNECT BY" in r.detected_features


def test_detect_postgres(tmp_path):
    r = _detect_sql(tmp_path, """
        CREATE TABLE users (id BIGSERIAL PRIMARY KEY, meta JSONB);
        INSERT INTO users (meta) VALUES ('{}') ON CONFLICT DO NOTHING RETURNING id;
        SELECT DISTINCT ON (email) email FROM users WHERE name ILIKE '%a%';
        SELECT generate_series(1, 10);""")
    assert r.detected_format == "postgres"
    assert "JSONB" in r.detected_features


def test_detect_teradata(tmp_path):
    r = _detect_sql(tmp_path, """
        CREATE VOLATILE TABLE tmp_sales, NO LOG AS (SEL * FROM sales) WITH DATA
        PRIMARY INDEX (sale_id) ON COMMIT PRESERVE ROWS;
        COLLECT STATISTICS ON tmp_sales COLUMN (sale_id);
        SEL OREPLACE(region, 'N/A', ''), ZEROIFNULL(amt) FROM tmp_sales SAMPLE 100;""")
    assert r.detected_format == "teradata"
    assert "VOLATILE TABLE" in r.detected_features


# ---------------------------------------------------------------------------
# Fallback, alternatives, contract
# ---------------------------------------------------------------------------

def test_ansi_fallback_for_plain_sql(tmp_path):
    r = _detect_sql(tmp_path, "SELECT id, name FROM customers WHERE active = 1")
    assert r.detected_format == "sql"
    assert any("ANSI" in reason for reason in r.detection_reasons)


def test_alternatives_reported_for_shared_syntax(tmp_path):
    # QUALIFY + ZEROIFNULL exist on both Snowflake and Teradata; the winner
    # must still list the other as an alternative.
    r = _detect_sql(tmp_path, """
        SELECT ZEROIFNULL(amt) FROM t
        QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts) = 1;
        SELECT v:field::VARIANT FROM raw, LATERAL FLATTEN(input => v) f;""")
    assert r.detected_format == "snowflake"
    assert any(a["format"] == "teradata" for a in r.alternative_formats)


def test_result_contract(tmp_path):
    r = _detect_sql(tmp_path, "SELECT NVL(a, 0) FROM DUAL CONNECT BY ROWNUM < 5")
    d = r.to_dict()
    for key in ("detected_format", "confidence_score", "detection_reasons",
                "detected_features", "alternative_formats", "files_scanned"):
        assert key in d
    assert 0.0 <= d["confidence_score"] <= 1.0
    for alt in d["alternative_formats"]:
        assert set(alt) == {"format", "confidence", "reasons"}


def test_empty_directory_raises(tmp_path):
    with pytest.raises(ValueError):
        detect(str(tmp_path))


def test_scan_budget_caps_files(tmp_path):
    for i in range(300):
        (tmp_path / ("f%03d.sql" % i)).write_text("SELECT NVL(a,0) FROM DUAL;")
    r = detect(str(tmp_path))
    assert r.files_scanned <= 200
    assert r.detected_format == "oracle"
