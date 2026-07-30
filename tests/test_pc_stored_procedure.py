"""Stored procedure handler: types, classification, hooks, strategy menu."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.parsers.pc_stored_procedure import classify_procedure_sql


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def test_classify_ddl():
    assert "DDL" in classify_procedure_sql(
        "TRUNCATE TABLE stage_t; CREATE TABLE x (a INT)")["classes"]


def test_classify_dml_logging_audit():
    c = classify_procedure_sql(
        "DELETE FROM etl_log WHERE run_dt < CURRENT_DATE; "
        "INSERT INTO run_audit (dt) VALUES (CURRENT_DATE)")
    assert {"DML", "logging", "audit"} <= set(c["classes"])


def test_classify_business_transformation():
    c = classify_procedure_sql(
        "INSERT INTO fct SELECT a.id, SUM(b.amt) FROM a "
        "JOIN b ON a.id = b.a_id GROUP BY a.id")
    assert "business_transformation" in c["classes"]


def test_classify_control_logic():
    c = classify_procedure_sql(
        "BEGIN DECLARE done INT; WHILE done = 0 LOOP FETCH c1; END LOOP; END")
    assert c["classes"] == ["control_logic"]
    assert c["parsed"] is False


def test_classify_unavailable():
    assert classify_procedure_sql("")["classes"] == ["unavailable"]


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------

def _sp_xml(sp_type="Target Pre Load",
            call_text="DELETE FROM stage_orders",
            order="1", extra_sp="") -> str:
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_o" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="tgt_o" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
   </TARGET>
   <MAPPING NAME="m_sp" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_o" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="SP_1" TYPE="Stored Procedure">
     <TRANSFORMFIELD NAME="in_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT"/>
     <TRANSFORMFIELD NAME="out_val" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="OUTPUT"/>
     <TABLEATTRIBUTE NAME="Stored Procedure Name" VALUE="sp_prepare"/>
     <TABLEATTRIBUTE NAME="Stored Procedure Type" VALUE="%(sp_type)s"/>
     <TABLEATTRIBUTE NAME="Call Text" VALUE="%(call)s"/>
     <TABLEATTRIBUTE NAME="Execution Order" VALUE="%(order)s"/>
    </TRANSFORMATION>
    %(extra)s
    <INSTANCE NAME="SRC_o" TYPE="SOURCE" TRANSFORMATION_NAME="src_o" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_o" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_o" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="SP_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SP_1" TRANSFORMATION_TYPE="Stored Procedure"/>
    <INSTANCE NAME="TGT_o" TYPE="TARGET" TRANSFORMATION_NAME="tgt_o" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SRC_o" TOFIELD="id" TOINSTANCE="SQ_o"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SQ_o" TOFIELD="id" TOINSTANCE="TGT_o"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"sp_type": sp_type, "call": call_text,
                   "order": order, "extra": extra_sp}


def _parse(tmp_path, **kw):
    f = tmp_path / "sp.xml"
    f.write_text(_sp_xml(**kw))
    return parse_input(str(f), "powercenter").mapping("sp")


def test_cir_contract(tmp_path):
    m = _parse(tmp_path)
    sp = m.transformation("SP_1")
    cir = sp.properties["stored_procedure_cir"]
    assert cir["procedure_name"] == "sp_prepare"
    assert cir["procedure_type"] == "PRE_LOAD_TARGET"
    assert cir["input_parameters"] == ["in_id"]
    assert cir["output_parameters"] == ["out_val"]
    assert cir["execution_order"] == 1
    assert "DML" in cir["classification"]["classes"]


def test_preload_sql_becomes_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    m = _parse(tmp_path)
    assert m.properties["pre_sql"] == "DELETE FROM stage_orders"
    assert any(i.code == "SP_HOOK_ATTACHED" and i.severity.value == "INFO"
               for i in m.issues)
    from metabridge.engine import convert
    convert(str(tmp_path / "sp.xml"), str(tmp_path / "out"),
            source_format="powercenter", target_format="dbt")
    sql = next((tmp_path / "out" / "dbt").rglob("*_sp.sql")).read_text()
    assert 'pre_hook="DELETE FROM stage_orders"' in sql


def test_execution_order_and_stage_ordering(tmp_path):
    extra = """
    <TRANSFORMATION NAME="SP_2" TYPE="Stored Procedure">
     <TABLEATTRIBUTE NAME="Stored Procedure Name" VALUE="sp_src_init"/>
     <TABLEATTRIBUTE NAME="Stored Procedure Type" VALUE="Source Pre Load"/>
     <TABLEATTRIBUTE NAME="Call Text" VALUE="TRUNCATE TABLE work_area"/>
     <TABLEATTRIBUTE NAME="Execution Order" VALUE="2"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="SP_3" TYPE="Stored Procedure">
     <TABLEATTRIBUTE NAME="Stored Procedure Name" VALUE="sp_post"/>
     <TABLEATTRIBUTE NAME="Stored Procedure Type" VALUE="Target Post Load"/>
     <TABLEATTRIBUTE NAME="Call Text" VALUE="INSERT INTO etl_log (dt) VALUES (CURRENT_DATE)"/>
     <TABLEATTRIBUTE NAME="Execution Order" VALUE="1"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SP_2" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SP_2" TRANSFORMATION_TYPE="Stored Procedure"/>
    <INSTANCE NAME="SP_3" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SP_3" TRANSFORMATION_TYPE="Stored Procedure"/>"""
    m = _parse(tmp_path, extra_sp=extra)
    # source pre-load runs before target pre-load
    assert m.properties["pre_sql"] == \
        "TRUNCATE TABLE work_area;\nDELETE FROM stage_orders"
    assert m.properties["post_sql"] == \
        "INSERT INTO etl_log (dt) VALUES (CURRENT_DATE)"


def test_proc_call_text_wrapped_with_body_note(tmp_path):
    m = _parse(tmp_path, call_text="collect_stats('T1', 10)")
    assert m.properties["pre_sql"] == "CALL collect_stats('T1', 10)"
    issue = next(i for i in m.issues if i.code == "SP_HOOK_ATTACHED")
    assert issue.severity.value == "MANUAL"           # body must be ported
    assert "BODY must exist on the target" in issue.message


def test_normal_proc_gets_strategy_menu(tmp_path):
    m = _parse(tmp_path, sp_type="Normal",
               call_text="INSERT INTO fct SELECT a.id, SUM(b.amt) FROM a "
                         "JOIN b ON a.id = b.a_id GROUP BY a.id")
    issue = next(i for i in m.issues if i.code == "SP_NORMAL_CALL")
    assert issue.severity.value == "MANUAL"
    assert "1 in / 1 out" in issue.message
    assert "business_transformation" in issue.message
    for word in ("macro", "notebook", "workflow", "Python"):
        assert word in issue.suggestion
    assert "pre_sql" not in m.properties          # normal proc: no hook


def test_procedural_logic_is_manual_review(tmp_path):
    m = _parse(tmp_path, call_text="BEGIN LOOP FETCH c1; END LOOP; END")
    issue = next(i for i in m.issues if i.code == "SP_PROCEDURAL")
    assert issue.severity.value == "MANUAL"
    assert "cannot be represented safely" in issue.message
    assert "pre_sql" not in m.properties          # never attached blindly
