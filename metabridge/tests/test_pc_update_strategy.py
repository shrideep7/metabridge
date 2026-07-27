"""Update Strategy handler: DML routing CIR, merge clauses on every
warehouse dialect, reject exception dataset."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import LoadStrategy
from metabridge.parsers.pc_update_strategy import parse_dml_routing
from metabridge.sqlx.expressions import infa_to_sql


# ---------------------------------------------------------------------------
# DML routing CIR
# ---------------------------------------------------------------------------

def test_spec_example():
    sql = infa_to_sql("IIF(ISNULL(TGT_ID), DD_INSERT, DD_UPDATE)")
    cir = parse_dml_routing(sql)
    assert cir["routes"] == [
        {"action": "INSERT", "condition": "TGT_ID IS NULL"},
        {"action": "UPDATE", "condition": "NOT (TGT_ID IS NULL)"},
    ]
    assert cir["actions"] == ["INSERT", "UPDATE"]
    assert cir["unknown_paths"] is False


def test_nested_four_way_routing():
    sql = infa_to_sql(
        "IIF(deleted_flag = 'Y', DD_DELETE, "
        "IIF(ISNULL(TGT_ID), DD_INSERT, "
        "IIF(amount < 0, DD_REJECT, DD_UPDATE)))")
    cir = parse_dml_routing(sql)
    by_action = {r["action"]: r["condition"] for r in cir["routes"]}
    assert by_action["DELETE"] == "deleted_flag = 'Y'"
    assert "NOT (deleted_flag = 'Y')" in by_action["INSERT"]
    assert "TGT_ID IS NULL" in by_action["INSERT"]
    assert "amount < 0" in by_action["REJECT"]
    # UPDATE path negates all prior branches
    assert by_action["UPDATE"].count("NOT (") == 3


def test_unconditional_and_numeric_forms():
    assert parse_dml_routing("DD_UPDATE")["routes"] == \
        [{"action": "UPDATE", "condition": ""}]
    cir = parse_dml_routing("CASE WHEN x > 1 THEN 0 ELSE 1 END")
    assert {r["action"] for r in cir["routes"]} == {"INSERT", "UPDATE"}


def test_opaque_routing_flagged():
    cir = parse_dml_routing("CASE WHEN x THEN some_function(y) END")
    assert cir["unknown_paths"] is True


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------

US_EXPR = ("IIF(deleted_flag = 'Y', DD_DELETE, "
           "IIF(ISNULL(tgt_id), DD_INSERT, "
           "IIF(amount &lt; 0, DD_REJECT, DD_UPDATE)))")

XML = """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_acct" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="tgt_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="deleted_flag" DATATYPE="string" PRECISION="1" SCALE="0" FIELDNUMBER="3" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="accounts" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="tgt_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="PRIMARY KEY" NULLABLE="NOTNULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
    <TARGETFIELD NAME="deleted_flag" DATATYPE="string" PRECISION="1" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="3"/>
   </TARGET>
   <MAPPING NAME="m_dml" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_a" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="tgt_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="deleted_flag" DATATYPE="string" PRECISION="1" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="US_1" TYPE="Update Strategy">
     <TRANSFORMFIELD NAME="tgt_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="deleted_flag" DATATYPE="string" PRECISION="1" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TABLEATTRIBUTE NAME="Update Strategy Expression" VALUE="%(expr)s"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_a" TYPE="SOURCE" TRANSFORMATION_NAME="src_acct" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_a" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_a" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="US_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="US_1" TRANSFORMATION_TYPE="Update Strategy"/>
    <INSTANCE NAME="TGT_a" TYPE="TARGET" TRANSFORMATION_NAME="accounts" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="tgt_id" FROMINSTANCE="SRC_a" TOFIELD="tgt_id" TOINSTANCE="SQ_a"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRC_a" TOFIELD="amount" TOINSTANCE="SQ_a"/>
    <CONNECTOR FROMFIELD="deleted_flag" FROMINSTANCE="SRC_a" TOFIELD="deleted_flag" TOINSTANCE="SQ_a"/>
    <CONNECTOR FROMFIELD="tgt_id" FROMINSTANCE="SQ_a" TOFIELD="tgt_id" TOINSTANCE="US_1"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SQ_a" TOFIELD="amount" TOINSTANCE="US_1"/>
    <CONNECTOR FROMFIELD="deleted_flag" FROMINSTANCE="SQ_a" TOFIELD="deleted_flag" TOINSTANCE="US_1"/>
    <CONNECTOR FROMFIELD="tgt_id" FROMINSTANCE="US_1" TOFIELD="tgt_id" TOINSTANCE="TGT_a"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="US_1" TOFIELD="amount" TOINSTANCE="TGT_a"/>
    <CONNECTOR FROMFIELD="deleted_flag" FROMINSTANCE="US_1" TOFIELD="deleted_flag" TOINSTANCE="TGT_a"/>
   </MAPPING>
   <SESSION NAME="s_m_dml" MAPPINGNAME="m_dml" REUSABLE="YES" VERSIONNUMBER="1" ISVALID="YES">
    <ATTRIBUTE NAME="Treat source rows as" VALUE="Data driven"/>
   </SESSION>
   <WORKFLOW NAME="wf_dml" ISENABLED="YES" VERSIONNUMBER="1">
    <TASKINSTANCE NAME="s_m_dml" TASKNAME="s_m_dml" TASKTYPE="Session"/>
   </WORKFLOW>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"expr": US_EXPR}


@pytest.fixture()
def parsed(tmp_path):
    f = tmp_path / "u.xml"
    f.write_text(XML)
    return parse_input(str(f), "powercenter")


def test_semantic_intent_and_merge_clauses(parsed):
    m = parsed.mapping("dml")
    assert m.load_strategy == LoadStrategy.MERGE      # data-driven honored
    clauses = m.properties["merge_clauses"]
    assert clauses["delete"] == "deleted_flag = 'Y'"
    assert "tgt_id IS NULL" in clauses["insert"]
    assert clauses["update"].count("NOT (") == 3
    assert m.properties["reject_condition"].startswith(
        "NOT (deleted_flag = 'Y')")
    assert any(i.code == "UPDATE_STRATEGY_INTENT" for i in m.issues)


def test_rejects_sibling_mapping(parsed):
    sib = parsed.mapping("dml__rejects")
    assert sib is not None
    assert sib.load_strategy == LoadStrategy.FULL
    tgt = sib.by_type(__import__("metabridge.ir.model",
                                 fromlist=["TransformationType"]
                                 ).TransformationType.TARGET)[0]
    assert tgt.properties["table"] == "accounts_rejects"
    fil = sib.transformation("FIL_TGT_a_REJECTS")
    assert "amount < 0" in fil.properties["condition"]
    assert any(i.code == "REJECTS_DATASET"
               for i in parsed.mapping("dml").issues)


def _convert(tmp_path, target, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "u.xml"
    f.write_text(XML)
    from metabridge.engine import convert
    return convert(str(f), str(tmp_path / "out"),
                   source_format="powercenter", target_format=target)


@pytest.mark.parametrize("target", ["snowflake", "databricks",
                                    "sqlserver"])
def test_merge_clauses_on_every_warehouse(tmp_path, monkeypatch, target):
    """Not just Databricks: the same ANSI MERGE shape on every dialect."""
    rep = _convert(tmp_path, target, monkeypatch)
    sql = next((tmp_path / "out" / "sql").glob("0*_dml.sql")).read_text()
    assert "MERGE INTO accounts t" in sql
    assert "WHEN MATCHED AND s.deleted_flag = 'Y' THEN DELETE" in sql
    assert "WHEN MATCHED AND" in sql and "THEN UPDATE SET" in sql
    assert "WHEN NOT MATCHED AND" in sql and "THEN INSERT" in sql
    # delete clause precedes update (precedence)
    assert sql.index("THEN DELETE") < sql.index("THEN UPDATE")
    # rejects exception dataset generated too
    rejects = next((tmp_path / "out" / "sql").glob(
        "0*_dml__rejects.sql")).read_text()
    assert "accounts_rejects" in rejects
    assert "amount < 0" in rejects
    assert rep["conversion_output"]["errors"]["count"] == 0


def test_dbt_incremental_with_delete_warning(tmp_path, monkeypatch):
    _convert(tmp_path, "dbt", monkeypatch)
    sql = next((tmp_path / "out" / "dbt").rglob("*_dml.sql")).read_text()
    assert "materialized='incremental'" in sql
    assert "incremental_strategy='merge'" in sql
    # rejects model exists as its own dbt model
    assert next((tmp_path / "out" / "dbt").rglob("*dml__rejects.sql"),
                None) is not None
    # dbt-core merge cannot delete: flagged, hook only where necessary
    import json
    report = json.loads(
        (tmp_path / "out" / "conversion_report.json").read_text())
    pool = [i for mm in report["mappings"] for i in mm["issues"]]
    issue = next(i for i in pool if i["code"] == "MERGE_DELETE_DBT")
    assert "post_hook" in issue["suggestion"]
