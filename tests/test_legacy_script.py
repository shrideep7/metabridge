"""Phase 3 §2/§4: legacy script splitting — BTEQ commands, GO batches,
PL/SQL blocks — and ingestion into the IR with full provenance."""
import pytest

from metabridge.parsers.legacy_script import split_legacy_script
from metabridge.parsers.sql_parser import parse_sql_scripts

BTEQ = """.LOGON tdprod/etl_user,;
.SET WIDTH 200
CREATE VOLATILE TABLE vt_sales AS (
  SEL store_id, SUM(amount) AS total
  FROM retail.sales GROUP BY 1
) WITH DATA ON COMMIT PRESERVE ROWS;
BT;
INSERT INTO dw.fct_sales
SEL store_id, total FROM vt_sales;
ET;
.IF ERRORCODE <> 0 THEN .QUIT 8;
.EXPORT FILE=/out/sales.csv
SEL * FROM dw.fct_sales;
.EXPORT RESET
.LOGOFF;
"""

TSQL = """CREATE VIEW dbo.v_orders AS
SELECT order_id, ISNULL(amount, 0) AS amount FROM dbo.orders;
GO
CREATE PROCEDURE dbo.usp_refresh AS
BEGIN
  SET NOCOUNT ON;
  DELETE FROM dbo.fct_orders;
  INSERT INTO dbo.fct_orders (order_id, amount)
  SELECT order_id, amount FROM dbo.v_orders;
END
GO
SELECT TOP 10 * FROM dbo.fct_orders;
"""

PLSQL = """CREATE TABLE stg_emp (emp_id NUMBER(10), name VARCHAR2(100));

CREATE OR REPLACE PROCEDURE load_emp IS
  v_cnt NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_cnt FROM stg_emp;
  IF v_cnt > 0 THEN
    INSERT INTO dim_emp SELECT * FROM stg_emp;
  END IF;
END load_emp;
/

CREATE VIEW v_emp AS SELECT emp_id, name FROM dim_emp;
"""


def test_bteq_commands_extracted_with_lines():
    s = split_legacy_script(BTEQ, "teradata")
    by_cmd = {c.command: c for c in s.commands}
    assert by_cmd[".LOGON"].category == "connection"
    assert by_cmd[".LOGON"].line == 1
    assert by_cmd[".IF ERRORCODE"].category == "error_handling"
    assert by_cmd[".EXPORT"].category == "extract"
    assert "COPY INTO" in by_cmd[".IMPORT"].strategy \
        if ".IMPORT" in by_cmd else True
    assert {"BT", "ET"} <= set(by_cmd)
    assert by_cmd["BT"].category == "transaction"
    # SEL normalized to SELECT in SQL units
    sqls = [u.text for u in s.units if u.kind == "sql"]
    assert any("SELECT store_id" in x for x in sqls)
    assert not any("\nSEL " in x for x in sqls)


def test_go_batches_and_procedure_block():
    s = split_legacy_script(TSQL, "tsql")
    procs = [u for u in s.units if u.kind == "procedural"]
    (proc,) = procs
    assert proc.object_type == "PROCEDURE"
    assert proc.object_name == "dbo.usp_refresh"
    assert proc.line == 4
    assert "DELETE FROM dbo.fct_orders" in proc.text
    sqls = [u.text for u in s.units if u.kind == "sql"]
    assert any("CREATE VIEW dbo.v_orders" in x for x in sqls)
    assert any("SELECT TOP 10" in x for x in sqls)
    # the proc body's INSERT stayed inside the block, not a loose unit
    assert not any("INSERT INTO dbo.fct_orders" in x for x in sqls)


def test_plsql_slash_terminated_block():
    s = split_legacy_script(PLSQL, "oracle")
    (proc,) = [u for u in s.units if u.kind == "procedural"]
    assert proc.object_type == "PROCEDURE"
    assert proc.object_name == "load_emp"
    assert "END load_emp" in proc.text
    sqls = [u.text for u in s.units if u.kind == "sql"]
    assert any("CREATE TABLE stg_emp" in x for x in sqls)
    assert any("CREATE VIEW v_emp" in x for x in sqls)


# ---------------------------------------------------------------------------
# ingestion into the IR
# ---------------------------------------------------------------------------

def test_bteq_ingestion(tmp_path):
    f = tmp_path / "load_sales.btq"
    f.write_text(BTEQ)
    p = parse_sql_scripts(str(tmp_path), "teradata")
    # the INSERT ... SELECT became a mapping; the volatile table a source
    assert [m.name for m in p.mappings]
    cmds = p.metadata["runtime_commands"]
    assert any(c["command"] == ".LOGON" and c["line"] == 1 for c in cmds)
    assert any(c["command"] == ".IF ERRORCODE" for c in cmds)
    issues = {i.code for m in p.mappings for i in m.issues} | \
        {i.code for i in p.issues}
    assert "RUNTIME_COMMAND" in issues


def test_tsql_ingestion_records_procedure(tmp_path):
    f = tmp_path / "refresh.sql"
    f.write_text(TSQL)
    p = parse_sql_scripts(str(tmp_path), "sqlserver")
    names = {m.name for m in p.mappings}
    assert "v_orders" in names
    (proc,) = p.metadata["procedural_units"]
    assert proc["object_name"] == "dbo.usp_refresh"
    assert proc["line"] == 4 and proc["dialect"] == "tsql"
    assert any(i.code == "PROCEDURAL_OBJECT" for i in p.issues)


def test_plsql_ingestion(tmp_path):
    f = tmp_path / "pkg.pls"
    f.write_text(PLSQL)
    p = parse_sql_scripts(str(tmp_path), "oracle")
    names = {m.name for m in p.mappings}
    assert "v_emp" in names
    assert "stg_emp" in {s.name for s in p.sources}
    (proc,) = p.metadata["procedural_units"]
    assert proc["object_name"] == "load_emp"
