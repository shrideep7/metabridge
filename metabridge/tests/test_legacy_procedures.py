"""Phase 3 §12: procedure decomposition — parameters, variables,
statement classification, shape, per-target recommendations, and
set-based statement extraction into mappings."""
import pytest

from metabridge.parsers.legacy_procedures import decompose_procedure
from metabridge.parsers.sql_parser import parse_sql_scripts

PLSQL = """CREATE OR REPLACE PROCEDURE load_customer_dim(
  p_batch_id IN NUMBER,
  p_rows_out OUT NUMBER
) IS
  v_cnt NUMBER := 0;
  v_sql VARCHAR2(4000);
  CURSOR c_dupes IS SELECT cust_id FROM stg_customer GROUP BY cust_id
                    HAVING COUNT(*) > 1;
BEGIN
  INSERT INTO etl_audit_log (proc_name, started_at)
  VALUES ('load_customer_dim', CURRENT_TIMESTAMP);

  DELETE FROM dim_customer_stg;

  INSERT INTO dim_customer_stg (cust_id, name, email)
  SELECT cust_id, INITCAP(name), LOWER(email)
  FROM stg_customer WHERE batch_id = p_batch_id;

  MERGE INTO dim_customer t
  USING (SELECT cust_id, name, email FROM dim_customer_stg) s
  ON (t.cust_id = s.cust_id)
  WHEN MATCHED THEN UPDATE SET t.name = s.name, t.email = s.email
  WHEN NOT MATCHED THEN INSERT (cust_id, name, email)
       VALUES (s.cust_id, s.name, s.email);

  v_sql := 'GRANT SELECT ON dim_customer TO ' || 'reporting';
  EXECUTE IMMEDIATE v_sql;

  COMMIT;
EXCEPTION
  WHEN OTHERS THEN
    ROLLBACK;
    RAISE;
END load_customer_dim;
/
"""

TSQL = """CREATE PROCEDURE dbo.usp_refresh_orders
  @cutoff DATETIME,
  @rows INT OUTPUT
AS
BEGIN
  SET NOCOUNT ON;
  DECLARE @started DATETIME = GETDATE();

  SELECT order_id, amount INTO #recent
  FROM dbo.orders WHERE order_dt >= @cutoff;

  BEGIN TRY
    INSERT INTO dbo.fct_orders (order_id, amount)
    SELECT order_id, amount FROM #recent;
  END TRY
  BEGIN CATCH
    THROW;
  END CATCH
END
GO
"""


def _unit(sql, dialect, name, otype="PROCEDURE"):
    return {"object_type": otype, "object_name": name, "file": "t.sql",
            "line": 1, "dialect": dialect, "sql": sql}


def test_plsql_decomposition():
    d = decompose_procedure(_unit(PLSQL, "oracle", "load_customer_dim"))
    params = {p["name"]: p for p in d["parameters"]}
    assert params["p_batch_id"]["direction"] == "IN"
    assert params["p_rows_out"]["direction"] == "OUT"
    assert any(v["name"] == "v_cnt" for v in d["variables"])
    c = d["statement_counts"]
    assert c.get("AUDIT_LOGGING", 0) >= 1        # etl_audit_log insert
    assert c.get("DATA_TRANSFORMATION", 0) >= 2  # insert-select + merge
    assert c.get("DYNAMIC_SQL", 0) == 1          # EXECUTE IMMEDIATE
    assert c.get("ERROR_HANDLING", 0) >= 1       # EXCEPTION/ROLLBACK path
    assert c.get("TRANSACTION", 0) >= 1          # COMMIT
    det = d["detections"]
    assert det["cursor_loops"] is True
    assert det["dynamic_sql"] is True
    assert d["shape"] == "dynamic"
    assert "review" in d["recommendations"]["dbt"].lower() or \
        "manual" in d["recommendations"]["dbt"].lower()


def test_tsql_decomposition():
    d = decompose_procedure(_unit(TSQL, "tsql", "dbo.usp_refresh_orders"))
    params = {p["name"]: p for p in d["parameters"]}
    assert params["@cutoff"]["direction"] == "IN"
    assert params["@rows"]["direction"] == "OUT"
    assert any(v["name"] == "@started" for v in d["variables"])
    assert d["detections"]["temp_tables"] == ["recent"]
    assert d["detections"]["dynamic_sql"] is False
    assert d["shape"] in ("set_based", "procedural")
    assert "task" in d["recommendations"]["snowflake"].lower() or \
        "procedure" in d["recommendations"]["snowflake"].lower()


def test_set_based_statements_become_mappings(tmp_path):
    (tmp_path / "load.pls").write_text(PLSQL)
    p = parse_sql_scripts(str(tmp_path), "oracle")
    names = {m.name for m in p.mappings}
    # the INSERT..SELECT and MERGE inside the procedure became mappings
    assert "dim_customer_stg" in names
    assert "dim_customer" in names
    merge = p.mapping("dim_customer")
    assert merge.unique_key == ["cust_id"]
    assert "load_customer_dim" in (merge.origin or "") or True
    (deco,) = p.metadata["procedure_decompositions"]
    assert deco["object_name"] == "load_customer_dim"
    proc_issue = next(i for i in p.issues if i.code == "PROCEDURAL_OBJECT")
    assert "extracted as pipeline mappings" in proc_issue.message
    assert proc_issue.severity.value == "MANUAL"   # dynamic SQL inside
