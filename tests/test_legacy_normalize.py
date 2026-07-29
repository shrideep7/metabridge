"""Phase 3 §6/§7/§8/§13/§14: legacy AST normalization, dialect
modernization findings, temp-table chains, recursive queries."""
import pytest
import sqlglot

from metabridge.parsers.sql_parser import parse_sql_scripts
from metabridge.sqlx.legacy_normalize import normalize_legacy_statement


def _norm(sql, dialect):
    stmt = sqlglot.parse_one(sql, read=dialect)
    return normalize_legacy_statement(stmt, dialect)


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------

def test_oracle_decode_sysdate_dual():
    stmt, findings, _ = _norm(
        "SELECT DECODE(status,'A','ACTIVE','INACTIVE') s, SYSDATE d "
        "FROM DUAL", "oracle")
    out = stmt.sql()
    assert "CASE WHEN status = 'A'" in out
    assert "CURRENT_TIMESTAMP" in out
    assert "DUAL" not in out.upper()
    assert any(f["code"] == "DECODE_TO_CASE" and
               f["automation"] == "FULLY_AUTOMATED" for f in findings)


def test_oracle_rownum_to_limit():
    stmt, findings, _ = _norm(
        "SELECT ename FROM emp WHERE dept = 10 AND ROWNUM <= 5", "oracle")
    out = stmt.sql()
    assert "LIMIT 5" in out
    assert "ROWNUM" not in out.upper()
    assert "dept = 10" in out
    (f,) = [f for f in findings if f["code"] == "ROWNUM_TO_LIMIT"]
    assert "ORDER BY" in f["suggestion"]


def test_oracle_rownum_complex_is_manual():
    _, findings, _ = _norm(
        "SELECT ename, ROWNUM AS rn FROM emp", "oracle")
    assert any(f["code"] == "ROWNUM_COMPLEX" and
               f["automation"] == "MANUAL_REVIEW_REQUIRED"
               for f in findings)


def test_oracle_connect_by_to_recursive_cte():
    stmt, findings, _ = _norm(
        "SELECT empno, ename, LEVEL FROM emp "
        "START WITH mgr IS NULL CONNECT BY PRIOR empno = mgr", "oracle")
    out = stmt.sql()
    assert "WITH RECURSIVE hierarchy" in out
    assert "UNION ALL" in out
    assert "c.mgr = h.empno" in out            # parent-child preserved
    assert "1 AS hier_level" in out            # root level
    assert "hier_level + 1" in out
    (f,) = [f for f in findings
            if f["code"] == "CONNECT_BY_TO_RECURSIVE_CTE"]
    assert "cycle" in f["suggestion"].lower()
    # transpiles to real targets
    for target in ("databricks", "postgres", "snowflake"):
        sqlglot.transpile(out, write=target)


def test_oracle_connect_by_complex_declared_manual():
    _, findings, _ = _norm(
        "SELECT e.ename FROM emp e JOIN dept d ON e.deptno = d.deptno "
        "CONNECT BY PRIOR e.empno = e.mgr", "oracle")
    assert any(f["code"] == "CONNECT_BY_COMPLEX" for f in findings)


def test_oracle_hint_dropped_with_note():
    stmt, findings, _ = _norm(
        "SELECT /*+ PARALLEL(8) */ a FROM t", "oracle")
    assert "PARALLEL" not in stmt.sql()
    assert any(f["code"] == "HINT_DROPPED" for f in findings)


# ---------------------------------------------------------------------------
# Teradata
# ---------------------------------------------------------------------------

def test_teradata_functions_normalized():
    stmt, _, _ = _norm(
        "SELECT ZEROIFNULL(x), NULLIFZERO(y), OREPLACE(s,'a','b') FROM t",
        "teradata")
    out = stmt.sql()
    assert "COALESCE(x, 0)" in out
    assert "NULLIF(y, 0)" in out
    assert "REPLACE(s, 'a', 'b')" in out and "OREPLACE" not in out


def test_teradata_volatile_create_flags_temporary():
    stmt, findings, flags = _norm(
        "CREATE VOLATILE TABLE vt AS (SELECT a FROM t) WITH DATA "
        "ON COMMIT PRESERVE ROWS", "teradata")
    assert flags["temporary"] is True
    assert "VOLATILE" not in stmt.sql().upper()


def test_teradata_multiset_and_primary_index():
    stmt, findings, _ = _norm(
        "CREATE MULTISET TABLE ms (a INT, b INT) PRIMARY INDEX (a)",
        "teradata")
    codes = {f["code"] for f in findings}
    assert "MULTISET_TABLE" in codes
    assert "PRIMARY_INDEX" in codes
    pi = next(f for f in findings if f["code"] == "PRIMARY_INDEX")
    assert "DISTKEY" in pi["suggestion"]       # every warehouse, not one
    assert "clustering" in pi["suggestion"]


def test_teradata_locking_unwrapped():
    stmt, findings, _ = _norm(
        "LOCKING ROW FOR ACCESS SELECT a FROM t", "teradata")
    assert stmt.sql().upper().startswith("SELECT")
    assert any(f["code"] == "LOCKING_FOR_ACCESS" for f in findings)


# ---------------------------------------------------------------------------
# end-to-end ingestion: temp chains (SQL Server #temp)
# ---------------------------------------------------------------------------

TSQL_CHAIN = """
CREATE TABLE dbo.orders (order_id INT, amount DECIMAL(12,2), status VARCHAR(20));
SELECT order_id, amount INTO #valid FROM dbo.orders WHERE status = 'OK';
SELECT order_id, amount * 0.98 AS net INTO #priced FROM #valid;
CREATE TABLE dbo.fct_orders AS SELECT order_id, net FROM #priced;
"""


def test_tsql_temp_chain_detected(tmp_path):
    (tmp_path / "chain.sql").write_text(TSQL_CHAIN)
    p = parse_sql_scripts(str(tmp_path), "sqlserver")
    names = {m.name for m in p.mappings}
    assert {"valid", "priced", "fct_orders"} <= names
    (chain,) = p.metadata["temp_table_chains"]
    assert chain["final"] == "fct_orders"
    assert chain["temp_chain"] == ["valid", "priced"]
    assert chain["depth"] == 2
    for t in ("valid", "priced"):
        m = p.mapping(t)
        assert m.properties["temporary_object"] is True
        assert any(i.code == "TEMP_OBJECT" for i in m.issues)
    assert any(i.code == "TEMP_CHAIN"
               for i in p.mapping("fct_orders").issues)


def test_dialect_detection_attached(tmp_path):
    (tmp_path / "q.sql").write_text(
        "SELECT TOP 3 ISNULL(a,0) FROM dbo.t WITH (NOLOCK)\nGO\n")
    p = parse_sql_scripts(str(tmp_path), "sqlserver")
    dd = p.metadata["dialect_detection"]
    assert dd["detected_dialect"] == "sqlserver"
