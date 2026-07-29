"""Sorter handler: keys/direction/case/distinct, ordering-required
analysis, hint preservation."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input


def _sorter_xml(distinct="NO", case_sensitive="YES",
                stateful_downstream=False) -> str:
    stateful = """
    <TRANSFORMATION NAME="EXP_RUN" TYPE="Expression">
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="v_running" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="LOCAL VARIABLE" EXPRESSION="v_running + amount"/>
     <TRANSFORMFIELD NAME="running_total" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="OUTPUT" EXPRESSION="v_running"/>
    </TRANSFORMATION>""" if stateful_downstream else ""
    stateful_inst = ('<INSTANCE NAME="EXP_RUN" TYPE="TRANSFORMATION" '
                     'TRANSFORMATION_NAME="EXP_RUN" '
                     'TRANSFORMATION_TYPE="Expression"/>') \
        if stateful_downstream else ""
    chain = ("""
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRT_1" TOFIELD="amount" TOINSTANCE="EXP_RUN"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="EXP_RUN" TOFIELD="amount" TOINSTANCE="TGT_o"/>"""
             if stateful_downstream else """
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRT_1" TOFIELD="amount" TOINSTANCE="TGT_o"/>""")
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="txn" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="txn_dt" DATATYPE="date/time" PRECISION="29" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="txn_out" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
   </TARGET>
   <MAPPING NAME="m_sort" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_t" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="txn_dt" DATATYPE="date/time" PRECISION="29" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="SRT_1" TYPE="Sorter">
     <TRANSFORMFIELD NAME="txn_dt" DATATYPE="date/time" PRECISION="29" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
     <TABLEATTRIBUTE NAME="Sort Keys" VALUE="txn_dt ASCENDING, amount DESCENDING"/>
     <TABLEATTRIBUTE NAME="Distinct" VALUE="%(distinct)s"/>
     <TABLEATTRIBUTE NAME="Case Sensitive" VALUE="%(cs)s"/>
    </TRANSFORMATION>
    %(stateful)s
    <INSTANCE NAME="SRC_t" TYPE="SOURCE" TRANSFORMATION_NAME="txn" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_t" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_t" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="SRT_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SRT_1" TRANSFORMATION_TYPE="Sorter"/>
    %(stateful_inst)s
    <INSTANCE NAME="TGT_o" TYPE="TARGET" TRANSFORMATION_NAME="txn_out" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="txn_dt" FROMINSTANCE="SRC_t" TOFIELD="txn_dt" TOINSTANCE="SQ_t"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRC_t" TOFIELD="amount" TOINSTANCE="SQ_t"/>
    <CONNECTOR FROMFIELD="txn_dt" FROMINSTANCE="SQ_t" TOFIELD="txn_dt" TOINSTANCE="SRT_1"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SQ_t" TOFIELD="amount" TOINSTANCE="SRT_1"/>
    %(chain)s
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"distinct": distinct, "cs": case_sensitive,
                   "stateful": stateful, "stateful_inst": stateful_inst,
                   "chain": chain}


def _parse(tmp_path, **kw):
    f = tmp_path / "s.xml"
    f.write_text(_sorter_xml(**kw))
    return parse_input(str(f), "powercenter").mapping("sort")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def test_sorter_cir_contract(tmp_path):
    m = _parse(tmp_path)
    cir = m.transformation("SRT_1").properties["sorter_cir"]
    assert cir["sort_keys"] == [{"port": "txn_dt", "order": "ASCENDING"},
                                {"port": "amount", "order": "DESCENDING"}]
    assert cir["case_sensitive"] is True
    assert cir["distinct"] is False
    assert cir["ordering_required"] is False


def test_hint_preserved_no_order_by(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    m = _parse(tmp_path)
    issue = next(i for i in m.issues if i.code == "SORTER_HINT_PRESERVED")
    assert "not semantically required" in issue.message
    assert "ZORDER" in issue.suggestion or "clustering" in issue.suggestion
    f = tmp_path / "s.xml"
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"), source_format="powercenter",
            target_format="databricks")
    sql = next((tmp_path / "out" / "sql").glob("0*_sort.sql")).read_text()
    assert "ORDER BY" not in sql.upper()      # never generated needlessly


def test_ordering_required_when_stateful_downstream(tmp_path):
    m = _parse(tmp_path, stateful_downstream=True)
    cir = m.transformation("SRT_1").properties["sorter_cir"]
    assert cir["ordering_required"] is True
    issue = next(i for i in m.issues if i.code == "SORTER_ORDER_REQUIRED")
    assert issue.severity.value == "WARNING"
    assert "EXP_RUN" in issue.message
    assert "txn_dt ASCENDING" in issue.suggestion    # window ORDER BY fix
    assert not any(i.code == "SORTER_HINT_PRESERVED" for i in m.issues)


# ---------------------------------------------------------------------------
# distinct
# ---------------------------------------------------------------------------

def test_distinct_generates_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    m = _parse(tmp_path, distinct="YES")
    assert any(i.code == "SORTER_DISTINCT" for i in m.issues)
    f = tmp_path / "s.xml"
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"), source_format="powercenter",
            target_format="snowflake")
    sql = next((tmp_path / "out" / "sql").glob("0*_sort.sql")).read_text()
    assert "SELECT DISTINCT" in sql.upper()


def test_case_insensitive_distinct_flagged(tmp_path):
    m = _parse(tmp_path, distinct="YES", case_sensitive="NO")
    issue = next(i for i in m.issues
                 if i.code == "SORTER_CASE_INSENSITIVE_DISTINCT")
    assert issue.severity.value == "WARNING"
    assert "'ABC' and 'abc'" in issue.message
    assert "UPPER" in issue.suggestion


def test_case_sensitive_distinct_not_flagged(tmp_path):
    m = _parse(tmp_path, distinct="YES", case_sensitive="YES")
    assert not any(i.code == "SORTER_CASE_INSENSITIVE_DISTINCT"
                   for i in m.issues)
