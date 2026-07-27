"""Phase 3 §3: legacy SQL dialect detection — feature evidence,
confidence, alternatives, auto wiring."""
from pathlib import Path

import pytest

from metabridge.detection.sql_dialect import detect_sql_dialect

ORACLE = """
CREATE OR REPLACE PROCEDURE load_dim_customer IS
  v_cnt NUMBER(10);
BEGIN
  SELECT NVL(COUNT(*), 0) INTO v_cnt FROM dual;
  INSERT INTO dim_customer (id, name, created_dt)
  SELECT cust_seq.NEXTVAL, INITCAP(name), SYSDATE
  FROM stg_customer WHERE ROWNUM <= 1000;
EXCEPTION WHEN OTHERS THEN
  RAISE;
END;
/
SELECT emp_id, DECODE(status, 'A', 'ACTIVE', 'INACTIVE') st
FROM employees START WITH mgr_id IS NULL CONNECT BY PRIOR emp_id = mgr_id;
"""

TERADATA = """
.LOGON tdprod/etl_user,;
CREATE VOLATILE TABLE vt_sales AS (
  SEL store_id, ZEROIFNULL(SUM(amount)) total
  FROM retail.sales GROUP BY 1
) WITH DATA ON COMMIT PRESERVE ROWS;
SEL store_id, total,
    ROW_NUMBER() OVER (ORDER BY total DESC) rnk
FROM vt_sales QUALIFY rnk <= 10;
COLLECT STATISTICS ON vt_sales COLUMN (store_id);
.IF ERRORCODE <> 0 THEN .QUIT 8;
.LOGOFF;
"""

SQLSERVER = """
CREATE PROCEDURE dbo.usp_load_orders AS
BEGIN
  SET NOCOUNT ON;
  DECLARE @cutoff DATETIME = GETDATE();
  SELECT TOP 100 order_id, ISNULL(amount, 0) amt
  INTO #recent
  FROM dbo.orders WITH (NOLOCK)
  WHERE order_dt >= DATEADD(day, -7, @cutoff);
  BEGIN TRY
    MERGE dbo.fct_orders t USING #recent s ON t.order_id = s.order_id
    WHEN NOT MATCHED THEN INSERT (order_id, amt) VALUES (s.order_id, s.amt);
  END TRY
  BEGIN CATCH
    THROW;
  END CATCH
END
GO
"""


@pytest.mark.parametrize("sql,expected,must_see", [
    (ORACLE, "oracle", {"ROWNUM", "CONNECT BY", "DECODE", "SYSDATE"}),
    (TERADATA, "teradata", {"QUALIFY", "VOLATILE TABLE", ".LOGON",
                            "ZEROIFNULL"}),
    (SQLSERVER, "sqlserver", {"WITH (NOLOCK)", "GETDATE", "#temp table",
                              "BEGIN TRY"}),
])
def test_dialect_detected_with_evidence(sql, expected, must_see):
    r = detect_sql_dialect(sql)
    assert r["detected_dialect"] == expected
    assert r["confidence_score"] >= 60
    assert must_see <= set(r["detected_features"])
    assert r["detection_reasons"]
    assert all(a["dialect"] != expected
               for a in r["alternative_dialects"])


def test_generic_sql_stays_generic():
    r = detect_sql_dialect("SELECT a, b FROM t WHERE a > 1 GROUP BY a, b")
    assert r["detected_dialect"] == "generic"
    assert r["confidence_score"] == 0


def test_features_in_comments_and_strings_do_not_count():
    r = detect_sql_dialect(
        "-- migrated from ROWNUM CONNECT BY VARCHAR2 logic\n"
        "SELECT a FROM t WHERE note = 'use GETDATE() here'")
    assert r["detected_dialect"] == "generic"


def test_auto_detection_end_to_end(tmp_path):
    from metabridge.engine import detect_format_detailed
    (tmp_path / "load.btq").write_text(TERADATA)
    r = detect_format_detailed(str(tmp_path))
    assert r.detected_format == "teradata"
    f = tmp_path / "ora"
    f.mkdir()
    (f / "pkg_load.pks").write_text(ORACLE)
    r2 = detect_format_detailed(str(f))
    assert r2.detected_format == "oracle"
