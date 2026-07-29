"""SQL transformation handler: modes, AST extraction, dynamic SQL honesty."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from metabridge.engine import parse_input


def _sqlt_xml(query="SELECT acct_name FROM accounts WHERE acct_id = ?in_id?",
              script_mode="NO", active="NO", connected=True,
              extra_ports="") -> str:
    conns = ("""
    <CONNECTOR FROMFIELD="in_id" FROMINSTANCE="SQ_o" TOFIELD="in_id" TOINSTANCE="SQLT_1"/>
    <CONNECTOR FROMFIELD="acct_name" FROMINSTANCE="SQLT_1" TOFIELD="acct_name" TOINSTANCE="TGT_o"/>
    <CONNECTOR FROMFIELD="in_id" FROMINSTANCE="SQ_o" TOFIELD="id" TOINSTANCE="TGT_o"/>"""
             if connected else """
    <CONNECTOR FROMFIELD="in_id" FROMINSTANCE="SQ_o" TOFIELD="id" TOINSTANCE="TGT_o"/>""")
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_o" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="in_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="tgt_o" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="acct_name" DATATYPE="string" PRECISION="64" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_sqlt" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_o" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="in_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="SQLT_1" TYPE="SQL Transformation">
     <TRANSFORMFIELD NAME="in_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT"/>
     <TRANSFORMFIELD NAME="acct_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="OUTPUT"/>
     %(extra_ports)s
     <TABLEATTRIBUTE NAME="SQL Query" VALUE="%(query)s"/>
     <TABLEATTRIBUTE NAME="Script Mode" VALUE="%(script)s"/>
     <TABLEATTRIBUTE NAME="Active" VALUE="%(active)s"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_o" TYPE="SOURCE" TRANSFORMATION_NAME="src_o" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_o" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_o" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="SQLT_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQLT_1" TRANSFORMATION_TYPE="SQL Transformation"/>
    <INSTANCE NAME="TGT_o" TYPE="TARGET" TRANSFORMATION_NAME="tgt_o" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="in_id" FROMINSTANCE="SRC_o" TOFIELD="in_id" TOINSTANCE="SQ_o"/>
    %(conns)s
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"query": query, "script": script_mode,
                   "active": active, "conns": conns,
                   "extra_ports": extra_ports}


def _parse(tmp_path, monkeypatch=None, **kw):
    if monkeypatch is not None:      # isolate from any host AI provider
        monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "q.xml"
    f.write_text(_sqlt_xml(**kw))
    p = parse_input(str(f), "powercenter")
    return p.mapping("sqlt"), \
        p.mapping("sqlt").transformation("SQLT_1")


# ---------------------------------------------------------------------------
# detection matrix
# ---------------------------------------------------------------------------

def test_query_mode_passive_connected(tmp_path):
    _, t = _parse(tmp_path)
    cir = t.properties["sql_transformation_cir"]
    assert cir["mode"] == "QUERY"
    assert cir["active"] is False
    assert cir["connected"] is True
    assert cir["static"] is True


def test_active_flag(tmp_path):
    _, t = _parse(tmp_path, active="YES")
    assert t.properties["sql_transformation_cir"]["active"] is True


def test_script_mode_is_manual(tmp_path):
    m, t = _parse(tmp_path, script_mode="YES", query="")
    cir = t.properties["sql_transformation_cir"]
    assert cir["mode"] == "SCRIPT"
    issue = next(i for i in m.issues if i.code == "SQLT_SCRIPT_MODE")
    assert issue.severity.value == "MANUAL"
    assert "EXTERNAL SCRIPT" in issue.message


def test_unconnected_flagged(tmp_path):
    m, t = _parse(tmp_path, connected=False)
    assert t.properties["sql_transformation_cir"]["connected"] is False
    assert any(i.code == "SQLT_UNCONNECTED" for i in m.issues)


# ---------------------------------------------------------------------------
# AST extraction: inputs / outputs / parameters
# ---------------------------------------------------------------------------

def test_static_query_ast_and_extraction(tmp_path):
    m, t = _parse(tmp_path)
    cir = t.properties["sql_transformation_cir"]
    assert cir["inputs"] == ["in_id"]
    assert cir["outputs"] == ["acct_name"]
    assert cir["parameters"] == ["in_id"]           # ?in_id? binding
    assert cir["substitution_ports"] == []
    assert cir["ast"]["parsed"] is True
    assert cir["ast"]["tables"] == ["accounts"]
    assert cir["requires_manual_review"] is False   # static SELECT
    issue = next(i for i in m.issues if i.code == "SQLT_STATIC_QUERY")
    assert "join" in issue.suggestion.lower()
    assert "accounts" in issue.message


def test_per_row_dml_is_manual(tmp_path):
    m, t = _parse(tmp_path,
                  query="DELETE FROM audit_run WHERE run_id = ?in_id?")
    cir = t.properties["sql_transformation_cir"]
    assert cir["requires_manual_review"] is True
    issue = next(i for i in m.issues if i.code == "SQLT_PER_ROW_DML")
    assert "per input row" in issue.message


def test_unparseable_static_query(tmp_path):
    m, _ = _parse(tmp_path, query="SELEC broken FROM (((")
    assert any(i.code == "SQLT_QUERY_UNPARSEABLE" and
               i.severity.value == "MANUAL" for i in m.issues)


# ---------------------------------------------------------------------------
# dynamic SQL: manual review + explained, never executed
# ---------------------------------------------------------------------------

def test_dynamic_substitution_requires_manual_review(tmp_path,
                                                     monkeypatch):
    m, t = _parse(tmp_path, monkeypatch,
                  query="SELECT * FROM ~table_name~ WHERE id = ?in_id?")
    cir = t.properties["sql_transformation_cir"]
    assert cir["static"] is False
    assert cir["substitution_ports"] == ["table_name"]
    assert cir["requires_manual_review"] is True
    issue = next(i for i in m.issues if i.code == "SQLT_DYNAMIC")
    assert issue.severity.value == "MANUAL"
    assert "NEVER executed" in issue.message
    # offline: the explanation is rules-generated and labeled
    exp = cir["dynamic_explanation"]
    assert exp["generated_by"] == "rules"
    assert "~table_name~" in exp["text"]
    assert "runtime" in exp["text"]
    assert issue.detail.startswith("[rules]")


def test_dynamic_explanation_uses_agent_when_configured(tmp_path,
                                                        monkeypatch):
    msg = SimpleNamespace(content=[SimpleNamespace(
        type="text", text="The template swaps the target table per row; "
                          "predicates stay fixed.")])
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: msg))
    import metabridge.llm.assist as assist
    monkeypatch.setattr(assist, "llm_available", lambda: True)
    monkeypatch.setattr(assist, "make_client",
                        lambda: (client, {"model": "fake"}))
    m, t = _parse(tmp_path,
                  query="INSERT INTO ~tgt~ VALUES (?in_id?)")
    exp = t.properties["sql_transformation_cir"]["dynamic_explanation"]
    assert exp["generated_by"] == "agent"
    assert "swaps the target table" in exp["text"]


def test_dynamic_query_from_port_only(tmp_path, monkeypatch):
    """No query template at all: fully runtime-constructed."""
    m, t = _parse(tmp_path, monkeypatch, query="")
    cir = t.properties["sql_transformation_cir"]
    assert cir["requires_manual_review"] is True
    issue = next(i for i in m.issues if i.code == "SQLT_DYNAMIC")
    assert "constructed at runtime" in issue.message


def test_end_to_end_conversion_never_breaks(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "q.xml"
    f.write_text(_sqlt_xml(query="SELECT * FROM ~t~"))
    from metabridge.engine import convert
    rep = convert(str(f), str(tmp_path / "out"),
                  source_format="powercenter", target_format="databricks")
    assert rep["conversion_output"]["errors"]["count"] == 0
    assert "sqlt" in rep["conversion_output"]["manual_review_assets"]
