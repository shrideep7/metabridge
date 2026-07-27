"""Filter handler: CIR predicates, 3VL validation, NULL semantic changes."""
import json
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.parsers.pc_filter import analyze_filter_sql

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


# ---------------------------------------------------------------------------
# structure preservation: AND / OR / NOT / nesting
# ---------------------------------------------------------------------------

def test_nested_and_or_not_preserved():
    a = analyze_filter_sql(
        "STATUS = 'ACTIVE' AND NOT (region = 'EU' OR amount > 100)")
    assert a["verdict"] == "EQUIVALENT"
    assert a["normalized_sql"] == \
        "STATUS = 'ACTIVE' AND NOT (region = 'EU' OR amount > 100)"
    cir = a["condition_cir"]
    assert cir["operator"] == "AND"
    assert cir["conditions"][0] == {"column": "STATUS",
                                    "operator": "EQUALS",
                                    "value": "ACTIVE"}
    assert cir["conditions"][1]["operator"] == "NOT"
    inner = cir["conditions"][1]["conditions"][0]
    assert inner["operator"] == "OR"


def test_null_handling_preserved():
    a = analyze_filter_sql("email IS NOT NULL AND status IS NULL")
    assert a["verdict"] == "EQUIVALENT"
    kinds = {c["operator"] for c in a["condition_cir"]["conditions"]}
    assert kinds == {"IS_NOT_NULL", "IS_NULL"}


def test_date_and_string_comparisons_documented():
    a = analyze_filter_sql(
        "order_date > TO_DATE('2024-01-01', 'YYYY-MM-DD') "
        "AND region = 'EU'")
    kinds = {c["kind"] for c in a["checks"]}
    assert "date_comparison" in kinds
    assert "case_sensitivity" in kinds          # string literal comparison
    assert "null_comparison_3vl" in kinds


# ---------------------------------------------------------------------------
# three-valued logic validation
# ---------------------------------------------------------------------------

def test_every_column_comparison_documents_null_behavior():
    a = analyze_filter_sql("a > 1 AND b = 'x'")
    tvl = [c for c in a["checks"] if c["kind"] == "null_comparison_3vl"]
    assert len(tvl) == 2
    for c in tvl:
        assert "DROPPED" in c["when_null"]


def test_not_wrapped_comparison_notes_null_propagation():
    a = analyze_filter_sql("NOT (status = 'CLOSED')")
    (tvl,) = [c for c in a["checks"]
              if c["kind"] == "null_comparison_3vl"]
    assert "NOT(NULL) is still NULL" in tvl["when_null"]


# ---------------------------------------------------------------------------
# semantic changes from NULL behavior
# ---------------------------------------------------------------------------

def test_null_literal_comparison_is_semantic_risk():
    a = analyze_filter_sql("status <> NULL")
    assert a["verdict"] == "SEMANTIC_RISK"
    (change,) = a["semantic_changes"]
    assert change["kind"] == "null_literal_always_false"
    assert "NEVER passes" in change["detail"]
    assert "IS NULL" in change["fix"]


def test_empty_string_is_platform_divergent():
    a = analyze_filter_sql("name <> ''")
    assert a["verdict"] == "REVIEW"
    assert a["semantic_changes"][0]["kind"] == "empty_string_platform"
    assert "Oracle" in a["semantic_changes"][0]["detail"]


def test_numeric_truthiness_bare_flag():
    a = analyze_filter_sql("active_flag")
    assert a["verdict"] == "NORMALIZED"
    assert a["normalized_sql"] == "active_flag <> 0"
    assert a["condition_cir"] == {"column": "active_flag",
                                  "operator": "NOT_EQUALS", "value": 0}


def test_iif_one_zero_collapses_to_condition():
    a = analyze_filter_sql("CASE WHEN active_flag = 1 THEN 1 ELSE 0 END")
    assert a["verdict"] == "NORMALIZED"
    assert a["normalized_sql"] == "active_flag = 1"


def test_boolean_condition_untouched():
    a = analyze_filter_sql("amount > 0")
    assert a["verdict"] == "EQUIVALENT"
    assert a["semantic_changes"] == []


def test_unparseable_is_honest():
    a = analyze_filter_sql("((broken")
    assert a["verdict"] == "UNPARSEABLE"
    assert a["condition_cir"]["operator"] == "RAW_SQL"


# ---------------------------------------------------------------------------
# parser integration
# ---------------------------------------------------------------------------

def _mapping_xml(condition: str) -> str:
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_t" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="status" DATATYPE="string" PRECISION="10" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="tgt_t" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
   </TARGET>
   <MAPPING NAME="m_f" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_t" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="status" DATATYPE="string" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="FIL_1" TYPE="Filter">
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="status" DATATYPE="string" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TABLEATTRIBUTE NAME="Filter Condition" VALUE="%s"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_t" TYPE="SOURCE" TRANSFORMATION_NAME="src_t" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_t" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_t" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="FIL_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="FIL_1" TRANSFORMATION_TYPE="Filter"/>
    <INSTANCE NAME="TGT_t" TYPE="TARGET" TRANSFORMATION_NAME="tgt_t" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SRC_t" TOFIELD="id" TOINSTANCE="SQ_t"/>
    <CONNECTOR FROMFIELD="status" FROMINSTANCE="SRC_t" TOFIELD="status" TOINSTANCE="SQ_t"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SQ_t" TOFIELD="id" TOINSTANCE="FIL_1"/>
    <CONNECTOR FROMFIELD="status" FROMINSTANCE="SQ_t" TOFIELD="status" TOINSTANCE="FIL_1"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="FIL_1" TOFIELD="id" TOINSTANCE="TGT_t"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % condition


def test_parser_normalizes_truthiness_filter(tmp_path):
    f = tmp_path / "m.xml"
    f.write_text(_mapping_xml("IIF(status = 'A', 1, 0)"))
    p = parse_input(str(f), "powercenter")
    fil = p.mapping("f").transformation("FIL_1")
    assert fil.properties["condition"] == "status = 'A'"
    assert fil.properties["condition_cir"] == {
        "column": "status", "operator": "EQUALS", "value": "A"}
    assert any(i.code == "FILTER_TRUTHINESS_NORMALIZED"
               for i in p.mapping("f").issues)


def test_parser_flags_null_literal_filter(tmp_path):
    f = tmp_path / "m.xml"
    f.write_text(_mapping_xml("status != NULL"))
    p = parse_input(str(f), "powercenter")
    m = p.mapping("f")
    issue = next(i for i in m.issues if i.code == "FILTER_NULL_LITERAL")
    assert issue.severity.value == "MANUAL"
    assert "never passes" in issue.message.lower()


def test_source_filter_gets_the_same_analysis():
    p = parse_input(str(EXAMPLES / "powercenter_repo" / "repo_export.xml"),
                    "powercenter")
    fil = p.mapping("load_sales__finance").transformation(
        "FIL_SQ_raw_gl_SRC")
    fa = fil.properties["filter_analysis"]
    assert fa["verdict"] == "EQUIVALENT"
    assert any(c["kind"] == "null_comparison_3vl" for c in fa["checks"])
    json.dumps(fa)


def test_generated_sql_uses_normalized_condition(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "m.xml"
    f.write_text(_mapping_xml("IIF(status = 'A', 1, 0)"))
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"), source_format="powercenter",
            target_format="databricks")
    sql = next((tmp_path / "out" / "sql").glob("0*_f.sql")).read_text()
    assert "WHERE" in sql and "status = 'A'" in sql
    assert "CASE WHEN" not in sql               # truthiness collapsed
