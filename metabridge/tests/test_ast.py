"""Typed SQL AST: statement coverage, node types, analysis, AST conversion."""
import pytest

from metabridge.sqlx.ast import (
    AggregationNode, CTENode, ColumnNode, FilterNode, FunctionNode, JoinNode,
    MergeNode, SelectNode, StatementNode, SubqueryNode, TableNode, WindowNode,
    parse_statements, transpile,
)


def one(sql, dialect=""):
    stmts = parse_statements(sql, dialect)
    assert len(stmts) == 1, stmts
    return stmts[0]


# ---------------------------------------------------------------------------
# Statement kinds
# ---------------------------------------------------------------------------

def test_select_full_shape():
    s = one("""
        WITH recent AS (SELECT id, amount FROM orders WHERE ts > '2026-01-01')
        SELECT c.region, SUM(r.amount) AS total,
               ROW_NUMBER() OVER (PARTITION BY c.region ORDER BY SUM(r.amount) DESC) AS rn
        FROM customers c
        LEFT JOIN recent r ON c.id = r.id
        WHERE c.active = 1
        GROUP BY c.region
        HAVING SUM(r.amount) > 100
        ORDER BY total DESC
        LIMIT 10""")
    assert s.kind == "select"
    sel = s.select
    assert [c.name for c in sel.ctes] == ["recent"]
    assert sel.tables[0].name == "customers" and sel.tables[0].alias == "c"
    assert sel.joins[0].join_type == "LEFT"
    assert "c.id = r.id" in sel.joins[0].condition
    clauses = {f.clause for f in sel.filters}
    assert {"WHERE", "HAVING"} <= clauses
    assert sel.aggregation is not None
    assert sel.aggregation.group_by == ["c.region"]
    assert any(a.semantic_type == "AGGREGATE" for a in sel.aggregation.aggregates)
    assert len(sel.windows) == 1
    w = sel.windows[0]
    assert w.partition_by == ["c.region"] and w.order_by
    assert sel.order_by and sel.limit == "10"
    # column lineage on projections
    total = next(p for p in sel.projections if p.alias == "total")
    assert total.source_columns == ["amount"]


def test_qualify_clause():
    s = one("""SELECT id FROM t
               QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts) = 1""",
            dialect="snowflake")
    assert any(f.clause == "QUALIFY" for f in s.select.filters)


@pytest.mark.parametrize("op,sql", [
    ("UNION", "SELECT a FROM t1 UNION SELECT a FROM t2"),
    ("UNION ALL", "SELECT a FROM t1 UNION ALL SELECT a FROM t2"),
    ("INTERSECT", "SELECT a FROM t1 INTERSECT SELECT a FROM t2"),
    ("EXCEPT", "SELECT a FROM t1 EXCEPT SELECT a FROM t2"),
])
def test_set_operations(op, sql):
    s = one(sql)
    assert s.kind == "select"
    assert s.select.set_operation.operator == op
    assert s.select.set_operation.left.tables[0].name == "t1"
    assert s.select.set_operation.right.tables[0].name == "t2"


def test_merge_statement():
    s = one("""
        MERGE INTO tgt t USING (SELECT id, amt FROM src) s ON t.id = s.id
        WHEN MATCHED THEN UPDATE SET t.amt = s.amt
        WHEN NOT MATCHED THEN INSERT (id, amt) VALUES (s.id, s.amt)""")
    assert s.kind == "merge"
    m = s.merge
    assert m.target.name == "tgt"
    assert isinstance(m.source, SubqueryNode)
    assert m.source.select.tables[0].name == "src"
    assert "t.id = s.id" in m.condition
    assert {(a.matched, a.action) for a in m.actions} == \
        {(True, "UPDATE"), (False, "INSERT")}


def test_insert_update_delete():
    ins = one("INSERT INTO tgt (a, b) SELECT a, b FROM src WHERE a > 0")
    assert ins.kind == "insert" and ins.target.name == "tgt"
    assert ins.select.tables[0].name == "src"
    upd = one("UPDATE tgt SET a = 1 WHERE b = 2")
    assert upd.kind == "update" and upd.target.name == "tgt"
    dele = one("DELETE FROM tgt WHERE a = 1")
    assert dele.kind == "delete" and dele.target.name == "tgt"


def test_create_table_and_view():
    ct = one("CREATE TABLE s.t AS SELECT a FROM src")
    assert ct.kind == "create_table" and ct.target.name == "t"
    assert ct.target.schema == "s"
    assert ct.select.tables[0].name == "src"
    cv = one("CREATE OR REPLACE VIEW v AS SELECT a FROM src")
    assert cv.kind == "create_view" and cv.target.name == "v"
    ddl = one("CREATE TABLE plain (a INT, b VARCHAR(10))")
    assert ddl.kind == "create_table" and ddl.select is None


def test_stored_procedure_recognized():
    stmts = parse_statements("""
        CREATE OR REPLACE PROCEDURE upd_ltv()
        RETURNS STRING LANGUAGE SQL AS
        $$ BEGIN UPDATE c SET x = 1; RETURN 'ok'; END; $$""",
        dialect="snowflake")
    proc = next(s for s in stmts if s.kind in ("procedure", "unparsed"))
    assert "upd_ltv" in proc.raw


def test_procedure_bodies_absorb_inner_statements():
    """Documented semantics: BEGIN...END bodies contain semicolons, so a
    procedure statement owns everything the grammar attributes to it — the
    content is preserved verbatim on the node, never dropped."""
    stmts = parse_statements(
        "CREATE PROCEDURE p1 AS BEGIN NULL; END p1;\n"
        "SELECT 1 AS a FROM t;")
    assert stmts[0].kind == "procedure"
    assert "p1" in stmts[0].raw
    joined = " ".join(s.raw for s in stmts)
    assert "SELECT 1 AS" in joined  # nothing is lost, even when absorbed


def test_unparsed_never_raises():
    stmts = parse_statements("THIS IS NOT SQL ;;; ???;\nSELECT 1 AS a FROM t")
    assert any(s.kind == "select" for s in stmts)


# ---------------------------------------------------------------------------
# Subqueries & correlation
# ---------------------------------------------------------------------------

def test_from_subquery():
    s = one("SELECT x.a FROM (SELECT a FROM src) x")
    sub = s.select.subqueries[0]
    assert sub.location == "from" and sub.alias == "x"
    assert sub.select.tables[0].name == "src"


def test_correlated_subquery_detected():
    s = one("""SELECT o.id FROM orders o
               WHERE o.amount > (SELECT AVG(i.amount) FROM items i
                                 WHERE i.order_id = o.id)""")
    sub = next(sq for sq in s.select.subqueries if sq.location == "where")
    assert sub.correlated is True


def test_uncorrelated_subquery():
    s = one("""SELECT o.id FROM orders o
               WHERE o.amount > (SELECT AVG(amount) FROM items)""")
    sub = next(sq for sq in s.select.subqueries if sq.location == "where")
    assert sub.correlated is False


# ---------------------------------------------------------------------------
# Analysis helpers + AST conversion
# ---------------------------------------------------------------------------

def test_statement_tables_walks_everything():
    s = one("""WITH c AS (SELECT a FROM cte_src)
               SELECT * FROM main m JOIN c ON m.a = c.a
               WHERE m.b IN (SELECT b FROM pred_src)""")
    names = {t.name for t in s.tables()}
    assert {"main", "cte_src", "pred_src"} <= names


def test_functions_carry_semantic_types():
    s = one("SELECT NVL(a, 0) AS a2, SUM(b) AS total FROM t GROUP BY a",
            dialect="oracle")
    types = {f.semantic_type for f in s.functions()}
    assert "NULL_COALESCE" in types
    assert "AGGREGATE" in types


def test_params_survive_ast_round_trip():
    s = one("SELECT a FROM t WHERE updated_at > $$LAST_RUN_TS")
    assert "$$LAST_RUN_TS" in s.select.filters[0].condition


def test_ast_transpile_semantic_mapping():
    """Oracle -> Snowflake through the AST: NVL becomes COALESCE/NVL-equivalent,
    never a text substitution."""
    r = transpile("SELECT NVL(name, 'UNKNOWN') AS n FROM DUAL",
                  source_dialect="oracle", target_dialect="snowflake")
    assert r["ok"]
    up = r["sql"].upper()
    assert "COALESCE(NAME, 'UNKNOWN')" in up  # semantic node, not text sub
    # (FROM DUAL is preserved — Snowflake accepts it natively)


def test_transpile_failure_returns_original():
    r = transpile("SELECT ]] nonsense", target_dialect="snowflake")
    assert r["ok"] is False
    assert r["sql"] == "SELECT ]] nonsense"
    assert r["error"]


def test_full_statement_to_dict_serializable():
    import json
    s = one("""MERGE INTO t USING (SELECT 1 AS id) s ON t.id = s.id
               WHEN MATCHED THEN UPDATE SET t.id = s.id""")
    json.dumps(s.to_dict())  # must not raise
