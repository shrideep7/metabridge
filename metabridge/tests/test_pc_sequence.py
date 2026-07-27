"""Sequence Generator handler: attributes, semantic usage, state honesty."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.parsers.pc_sequence import parse_sequence_attributes

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_XML = EXAMPLES / "powercenter_repo" / "repo_export.xml"


def _seq_xml(seq_attrs: str = "", treat: str = "Insert",
             truncate: str = "YES", currval: bool = False) -> str:
    cv = ('<CONNECTOR FROMFIELD="CURRVAL" FROMINSTANCE="SEQ_1" '
          'TOFIELD="batch_id" TOINSTANCE="EXP_1"/>') if currval else ""
    cv_port = ('<TRANSFORMFIELD NAME="batch_id" DATATYPE="bigint" '
               'PRECISION="19" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>') \
        if currval else ""
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_r" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="val" DATATYPE="string" PRECISION="32" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="tgt_r" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="val" DATATYPE="string" PRECISION="32" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="row_key" DATATYPE="bigint" PRECISION="19" SCALE="0" KEYTYPE="PRIMARY KEY" NULLABLE="NOTNULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_seq" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_r" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="val" DATATYPE="string" PRECISION="32" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="EXP_1" TYPE="Expression">
     <TRANSFORMFIELD NAME="val" DATATYPE="string" PRECISION="32" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="row_key" DATATYPE="bigint" PRECISION="19" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     %(cv_port)s
    </TRANSFORMATION>
    <TRANSFORMATION NAME="SEQ_1" TYPE="Sequence">
     <TRANSFORMFIELD NAME="NEXTVAL" DATATYPE="bigint" PRECISION="19" SCALE="0" PORTTYPE="OUTPUT"/>
     <TRANSFORMFIELD NAME="CURRVAL" DATATYPE="bigint" PRECISION="19" SCALE="0" PORTTYPE="OUTPUT"/>
     %(seq_attrs)s
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_r" TYPE="SOURCE" TRANSFORMATION_NAME="src_r" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_r" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_r" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="EXP_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="EXP_1" TRANSFORMATION_TYPE="Expression"/>
    <INSTANCE NAME="SEQ_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SEQ_1" TRANSFORMATION_TYPE="Sequence"/>
    <INSTANCE NAME="TGT_r" TYPE="TARGET" TRANSFORMATION_NAME="tgt_r" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="val" FROMINSTANCE="SRC_r" TOFIELD="val" TOINSTANCE="SQ_r"/>
    <CONNECTOR FROMFIELD="val" FROMINSTANCE="SQ_r" TOFIELD="val" TOINSTANCE="EXP_1"/>
    <CONNECTOR FROMFIELD="NEXTVAL" FROMINSTANCE="SEQ_1" TOFIELD="row_key" TOINSTANCE="EXP_1"/>
    %(cv)s
    <CONNECTOR FROMFIELD="val" FROMINSTANCE="EXP_1" TOFIELD="val" TOINSTANCE="TGT_r"/>
    <CONNECTOR FROMFIELD="row_key" FROMINSTANCE="EXP_1" TOFIELD="row_key" TOINSTANCE="TGT_r"/>
   </MAPPING>
   <SESSION NAME="s_m_seq" MAPPINGNAME="m_seq" REUSABLE="YES" VERSIONNUMBER="1" ISVALID="YES">
    <ATTRIBUTE NAME="Treat source rows as" VALUE="%(treat)s"/>
    <ATTRIBUTE NAME="Truncate target table option" VALUE="%(truncate)s"/>
   </SESSION>
   <WORKFLOW NAME="wf_seq" ISENABLED="YES" VERSIONNUMBER="1">
    <TASKINSTANCE NAME="s_m_seq" TASKNAME="s_m_seq" TASKTYPE="Session"/>
   </WORKFLOW>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"seq_attrs": seq_attrs, "treat": treat,
                   "truncate": truncate, "cv": cv, "cv_port": cv_port}


def _attr(name, value):
    return '<TABLEATTRIBUTE NAME="%s" VALUE="%s"/>' % (name, value)


def _parse(tmp_path, **kw):
    f = tmp_path / "s.xml"
    f.write_text(_seq_xml(**kw))
    return parse_input(str(f), "powercenter").mapping("seq")


# ---------------------------------------------------------------------------
# attribute parsing
# ---------------------------------------------------------------------------

def test_parse_all_six_attributes():
    cir = parse_sequence_attributes({
        "Start Value": "1000", "Increment By": "5", "End Value": "99999",
        "Current Value": "1500", "Cycle": "YES",
        "Number of Cached Values": "100"})
    assert cir == {"start_value": 1000, "increment_by": 5,
                   "end_value": 99999, "current_value": 1500,
                   "cycle": True, "cached_values": 100}


def test_defaults():
    cir = parse_sequence_attributes({})
    assert cir["start_value"] == 1 and cir["increment_by"] == 1
    assert cir["cycle"] is False


# ---------------------------------------------------------------------------
# semantic usage + folding decisions
# ---------------------------------------------------------------------------

def test_full_load_surrogate_folds_with_strategy_note(tmp_path):
    m = _parse(tmp_path)
    exp = m.transformation("EXP_SEQ_1_1")
    assert exp.port("row_key").expression == \
        "ROW_NUMBER() OVER (ORDER BY 1)"
    issue = next(i for i in m.issues
                 if i.code == "SEQUENCE_SURROGATE_STRATEGY")
    assert "generate_surrogate_key" in issue.suggestion   # dbt strategy
    assert "IDENTITY" in issue.suggestion                 # Delta identity
    assert "monotonically_increasing_id" in issue.suggestion
    assert "NOT auto-replaced with a hash" in issue.suggestion
    # FULL load: no state-continuity warning
    assert not any(i.code == "SEQUENCE_STATE_CONTINUITY" for i in m.issues)


def test_start_and_increment_honored(tmp_path):
    m = _parse(tmp_path, seq_attrs=_attr("Start Value", "1000") +
               _attr("Increment By", "5"))
    exp = m.transformation("EXP_SEQ_1_1")
    assert exp.port("row_key").expression == \
        "(ROW_NUMBER() OVER (ORDER BY 1) - 1) * 5 + 1000"


def test_incremental_load_warns_about_state(tmp_path):
    m = _parse(tmp_path, treat="Update", truncate="NO")
    issue = next(i for i in m.issues
                 if i.code == "SEQUENCE_STATE_CONTINUITY")
    assert issue.severity.value == "WARNING"
    assert "WILL COLLIDE" in issue.message
    assert "MAX(row_key)" in issue.suggestion
    assert "IDENTITY" in issue.suggestion


def test_cycle_is_never_folded(tmp_path):
    m = _parse(tmp_path, seq_attrs=_attr("Cycle", "YES") +
               _attr("Start Value", "1") + _attr("End Value", "4"))
    assert m.transformation("SEQ_1") is not None      # kept, not folded
    issue = next(i for i in m.issues if i.code == "SEQUENCE_CYCLE")
    assert issue.severity.value == "MANUAL"
    assert "rotating counter" in issue.message
    assert "MOD(ROW_NUMBER() OVER (ORDER BY 1) - 1, 4) + 1" in \
        issue.suggestion


def test_currval_is_never_folded(tmp_path):
    m = _parse(tmp_path, currval=True)
    assert m.transformation("SEQ_1") is not None
    issue = next(i for i in m.issues if i.code == "SEQUENCE_CURRVAL")
    assert issue.severity.value == "MANUAL"


def test_cached_values_gap_honesty(tmp_path):
    m = _parse(tmp_path, seq_attrs=_attr("Number of Cached Values", "100"))
    issue = next(i for i in m.issues
                 if i.code == "SEQUENCE_CACHED_GAPS")
    assert "GAPS" in issue.message
    assert "never" in issue.message      # gap-free was never guaranteed


# ---------------------------------------------------------------------------
# fixture integration
# ---------------------------------------------------------------------------

def test_enrichment_sequence_records_cir_and_strategy():
    p = parse_input(str(REPO_XML), "powercenter")
    m = p.mapping("customer_enrich")
    (fold,) = m.properties["sequence_folds"]
    assert fold["sequence"] == "SEQ_SK"
    assert fold["surrogate"] is True                  # 'sk' is key-ish
    assert any(i.code == "SEQUENCE_SURROGATE_STRATEGY" for i in m.issues)
