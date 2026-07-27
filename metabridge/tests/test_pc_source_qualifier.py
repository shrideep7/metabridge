"""Source Qualifier handler: 7 attributes, condition CIR, graph synthesis."""
import json
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import TransformationType
from metabridge.parsers.pc_source_qualifier import (
    condition_to_cir, normalize_override, parse_sq_attributes,
    validate_hook_sql,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_XML = EXAMPLES / "powercenter_repo" / "repo_export.xml"


@pytest.fixture(scope="module")
def finance():
    p = parse_input(str(REPO_XML), "powercenter")
    return p.mapping("load_sales__finance")


# ---------------------------------------------------------------------------
# attribute parsing
# ---------------------------------------------------------------------------

def test_parse_all_seven_attributes():
    cfg = parse_sq_attributes({
        "Sql Query": "SELECT 1", "Source Filter": "STATUS = 'ACTIVE'",
        "User Defined Join": "a.id = b.a_id",
        "Number Of Sorted Ports": "2", "Select Distinct": "YES",
        "Pre SQL": "DELETE FROM t", "Post SQL": "INSERT INTO t VALUES (1)"})
    assert cfg.sql_query == "SELECT 1"
    assert cfg.source_filter == "STATUS = 'ACTIVE'"
    assert cfg.user_defined_join == "a.id = b.a_id"
    assert cfg.sorted_ports == 2
    assert cfg.select_distinct is True
    assert cfg.pre_sql and cfg.post_sql


# ---------------------------------------------------------------------------
# condition CIR — the spec example, exactly
# ---------------------------------------------------------------------------

def test_spec_example():
    assert condition_to_cir("STATUS = 'ACTIVE'") == {
        "column": "STATUS", "operator": "EQUALS", "value": "ACTIVE"}


def test_operator_coverage():
    assert condition_to_cir("amount > 100")["operator"] == "GREATER_THAN"
    assert condition_to_cir("amount >= 100")["operator"] == \
        "GREATER_OR_EQUAL"
    assert condition_to_cir("x <> 'A'")["operator"] == "NOT_EQUALS"
    assert condition_to_cir("email IS NULL") == {
        "column": "email", "operator": "IS_NULL"}
    assert condition_to_cir("email IS NOT NULL")["operator"] == \
        "IS_NOT_NULL"
    assert condition_to_cir("region IN ('EU', 'US')") == {
        "column": "region", "operator": "IN", "values": ["EU", "US"]}
    assert condition_to_cir("x NOT IN (1, 2)")["operator"] == "NOT_IN"
    between = condition_to_cir("amount BETWEEN 1 AND 9")
    assert (between["operator"], between["low"], between["high"]) == \
        ("BETWEEN", 1, 9)
    assert condition_to_cir("name LIKE 'A%'")["operator"] == "LIKE"
    join = condition_to_cir("a_id = b_id")
    assert join == {"column": "a_id", "operator": "EQUALS",
                    "other_column": "b_id"}


def test_nested_and_or():
    cir = condition_to_cir(
        "STATUS = 'ACTIVE' AND amount > 100 OR region IN ('EU')")
    assert cir["operator"] == "OR"
    inner = cir["conditions"][0]
    assert inner["operator"] == "AND"
    assert inner["conditions"][0]["value"] == "ACTIVE"


def test_raw_sql_fallback_is_honest():
    cir = condition_to_cir("REGEXP_LIKE(code, '^[A-Z]+$')")
    assert cir["operator"] == "RAW_SQL"
    assert "REGEXP_LIKE" in cir["sql"]
    assert condition_to_cir("((broken")["operator"] == "RAW_SQL"


# ---------------------------------------------------------------------------
# SQL Query: AST, never a blind copy
# ---------------------------------------------------------------------------

def test_normalize_override_parses_and_extracts_tables():
    info = normalize_override(
        "SELECT a.id, b.name FROM customers a JOIN accounts b "
        "ON a.id = b.cust_id WHERE a.status = 'A'")
    assert info["parsed"] is True
    assert info["statements"] == 1
    assert info["tables"] == ["accounts", "customers"]
    assert "SELECT" in info["canonical_sql"]


def test_unparseable_override_is_flagged_manual(tmp_path):
    xml = REPO_XML.read_text().replace(
        '<TABLEATTRIBUTE NAME="Source Filter" VALUE="balance &gt; 0"/>',
        '<TABLEATTRIBUTE NAME="Sql Query" VALUE="SELEC broken FROM ((("/>',
        1)
    f = tmp_path / "bad.xml"
    f.write_text(xml)
    p = parse_input(str(f), "powercenter")
    m = p.mapping("load_sales__finance")
    assert any(i.code == "SQL_OVERRIDE_UNPARSEABLE" and
               i.severity.value == "MANUAL" for i in m.issues)


def test_existing_override_gets_ast_metadata():
    p = parse_input(str(EXAMPLES / "powercenter" /
                        "wf_retail_analytics.xml"), "powercenter")
    m = p.mapping("customer_ranking")
    sq = next(t for t in m.transformations
              if t.type == TransformationType.SOURCE_QUALIFIER)
    ast = sq.properties["sql_override_ast"]
    assert ast["parsed"] is True
    assert "customer_orders" in ast["tables"]


# ---------------------------------------------------------------------------
# graph synthesis
# ---------------------------------------------------------------------------

def test_source_filter_becomes_filter_node(finance):
    fil = finance.transformation("FIL_SQ_raw_gl_SRC")
    assert fil is not None and fil.type == TransformationType.FILTER
    assert fil.properties["condition"] == "balance > 0"
    assert fil.properties["condition_cir"] == {
        "column": "balance", "operator": "GREATER_THAN", "value": 0}
    # spliced into the dataflow: SQ -> FIL -> (rest)
    edges = {(l.from_transformation, l.to_transformation)
             for l in finance.links}
    assert ("SQ_raw_gl", "FIL_SQ_raw_gl_SRC") in edges
    assert any(i.code == "SQ_FILTER_CONVERTED" for i in finance.issues)


def test_distinct_and_sorted_ports_become_sorter(finance):
    srt = finance.transformation("SRT_SQ_raw_gl_SRC")
    assert srt is not None and srt.type == TransformationType.SORTER
    assert srt.properties["distinct"] is True
    assert srt.properties["sort_keys"] == [{"port": "gl_id",
                                            "order": "ASC"}]
    edges = {(l.from_transformation, l.to_transformation)
             for l in finance.links}
    assert ("FIL_SQ_raw_gl_SRC", "SRT_SQ_raw_gl_SRC") in edges


def test_pre_post_sql_become_mapping_hooks(finance):
    assert finance.properties["pre_sql"].startswith("DELETE FROM etl_audit")
    assert finance.properties["post_sql"].startswith("INSERT INTO etl_audit")
    assert validate_hook_sql(finance.properties["pre_sql"]) is None
    assert validate_hook_sql("DELETE FROM ((;") is not None


def test_user_defined_join_becomes_joiner(tmp_path):
    xml = """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="orders" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <SOURCE NAME="customers" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="cname" DATATYPE="string" PRECISION="64" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="ord_c" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="cname" DATATYPE="string" PRECISION="64" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_udj" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_multi" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cname" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TABLEATTRIBUTE NAME="User Defined Join" VALUE="cust_id = cust_id"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_orders" TYPE="SOURCE" TRANSFORMATION_NAME="orders" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SRC_customers" TYPE="SOURCE" TRANSFORMATION_NAME="customers" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_multi" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_multi" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="TGT_ord_c" TYPE="TARGET" TRANSFORMATION_NAME="ord_c" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SRC_orders" TOFIELD="id" TOINSTANCE="SQ_multi"/>
    <CONNECTOR FROMFIELD="cname" FROMINSTANCE="SRC_customers" TOFIELD="cname" TOINSTANCE="SQ_multi"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SQ_multi" TOFIELD="id" TOINSTANCE="TGT_ord_c"/>
    <CONNECTOR FROMFIELD="cname" FROMINSTANCE="SQ_multi" TOFIELD="cname" TOINSTANCE="TGT_ord_c"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>"""
    f = tmp_path / "udj.xml"
    f.write_text(xml)
    p = parse_input(str(f), "powercenter")
    m = p.mapping("udj")
    jnr = m.transformation("JNR_SQ_multi")
    assert jnr is not None and jnr.type == TransformationType.JOINER
    assert jnr.properties["condition"] == "cust_id = cust_id"
    assert jnr.properties["join_type"] == "INNER"
    assert {jnr.properties["left"], jnr.properties["right"]} == \
        {"SRC_orders", "SRC_customers"}
    edges = {(l.from_transformation, l.to_transformation) for l in m.links}
    assert ("SRC_orders", "JNR_SQ_multi") in edges
    assert ("SRC_customers", "JNR_SQ_multi") in edges
    assert ("JNR_SQ_multi", "SQ_multi") in edges
    assert any(i.code == "SQ_JOIN_CONVERTED" for i in m.issues)


# ---------------------------------------------------------------------------
# generation: dbt WHERE + hooks, Spark SQL WHERE + statements
# ---------------------------------------------------------------------------

def test_dbt_generates_where_distinct_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    convert(str(REPO_XML), str(tmp_path / "out"),
            source_format="powercenter", target_format="dbt")
    sql = next((tmp_path / "out" / "dbt").rglob(
        "int_sales__finance.sql")).read_text()
    assert "where balance > 0" in sql
    assert "select distinct" in sql
    mart = next((tmp_path / "out" / "dbt").rglob("fct_gl.sql")).read_text()
    assert 'pre_hook="DELETE FROM etl_audit' in mart
    assert 'post_hook="INSERT INTO etl_audit' in mart


def test_databricks_generates_where_and_hook_statements(tmp_path,
                                                        monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    convert(str(REPO_XML), str(tmp_path / "out"),
            source_format="powercenter", target_format="databricks")
    sql = next((tmp_path / "out" / "sql").glob(
        "*load_sales__finance.sql")).read_text()
    assert "balance > 0" in sql
    assert "SELECT DISTINCT" in sql
    assert sql.startswith("-- pre-SQL")
    assert "post-SQL" in sql and "INSERT INTO etl_audit" in sql


def test_condition_cir_serializable(finance):
    json.dumps(finance.transformation(
        "FIL_SQ_raw_gl_SRC").properties["condition_cir"])
