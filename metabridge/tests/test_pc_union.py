"""Union handler: input groups, count/type/order validation, UNION ALL."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input


def _union_xml(g2_fields: str = "", out_extra: str = "") -> str:
    g2 = g2_fields or """
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT" GROUP="in_new"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT" GROUP="in_new"/>"""
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="hist" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <SOURCE NAME="fresh" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="t_all" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_union" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_hist" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="SQ_fresh" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="UN_1" TYPE="Union Transformation">
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="OUTPUT"/>%(out_extra)s
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT" GROUP="in_hist"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT" GROUP="in_hist"/>
     %(g2)s
     <GROUP NAME="in_hist" TYPE="INPUT"/>
     <GROUP NAME="in_new" TYPE="INPUT"/>
     <GROUP NAME="OUTPUT" TYPE="OUTPUT"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_hist" TYPE="SOURCE" TRANSFORMATION_NAME="hist" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SRC_fresh" TYPE="SOURCE" TRANSFORMATION_NAME="fresh" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_hist" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_hist" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="SQ_fresh" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_fresh" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="UN_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="UN_1" TRANSFORMATION_TYPE="Union Transformation"/>
    <INSTANCE NAME="TGT_all" TYPE="TARGET" TRANSFORMATION_NAME="t_all" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SRC_hist" TOFIELD="id" TOINSTANCE="SQ_hist"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRC_hist" TOFIELD="amount" TOINSTANCE="SQ_hist"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SRC_fresh" TOFIELD="id" TOINSTANCE="SQ_fresh"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRC_fresh" TOFIELD="amount" TOINSTANCE="SQ_fresh"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SQ_hist" TOFIELD="id" TOINSTANCE="UN_1"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SQ_hist" TOFIELD="amount" TOINSTANCE="UN_1"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SQ_fresh" TOFIELD="id" TOINSTANCE="UN_1"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SQ_fresh" TOFIELD="amount" TOINSTANCE="UN_1"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="UN_1" TOFIELD="id" TOINSTANCE="TGT_all"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="UN_1" TOFIELD="amount" TOINSTANCE="TGT_all"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"g2": g2, "out_extra": out_extra}


def _parse(tmp_path, **kw):
    f = tmp_path / "u.xml"
    f.write_text(_union_xml(**kw))
    p = parse_input(str(f), "powercenter")
    return p.mapping("union"), \
        p.mapping("union").transformation("UN_1")


# ---------------------------------------------------------------------------
# parsing + CIR
# ---------------------------------------------------------------------------

def test_input_groups_parsed(tmp_path):
    m, un = _parse(tmp_path)
    cir = un.properties["union_cir"]
    assert cir["operation"] == "UNION_ALL"
    assert [g["name"] for g in cir["input_groups"]] == ["in_hist", "in_new"]
    assert [c["name"] for c in cir["output_columns"]] == ["id", "amount"]
    assert not any(i.code.startswith("UNION_") for i in m.issues)


def test_column_count_mismatch_is_manual(tmp_path):
    g2 = ('<TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" '
          'SCALE="0" PORTTYPE="INPUT" GROUP="in_new"/>')     # 1 vs 2 cols
    m, _ = _parse(tmp_path, g2_fields=g2)
    issue = next(i for i in m.issues if i.code == "UNION_COLUMN_COUNT")
    assert issue.severity.value == "MANUAL"
    assert "1 port(s)" in issue.message and "2" in issue.message


def test_numeric_widening_is_warning(tmp_path):
    g2 = ("""
     <TRANSFORMFIELD NAME="id" DATATYPE="bigint" PRECISION="19" SCALE="0" PORTTYPE="INPUT" GROUP="in_new"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT" GROUP="in_new"/>""")
    m, _ = _parse(tmp_path, g2_fields=g2)
    issue = next(i for i in m.issues if i.code == "UNION_TYPE_WIDENING")
    assert "bigint" in issue.message and "integer" in issue.message


def test_incompatible_types_are_manual(tmp_path):
    g2 = ("""
     <TRANSFORMFIELD NAME="id" DATATYPE="string" PRECISION="10" SCALE="0" PORTTYPE="INPUT" GROUP="in_new"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT" GROUP="in_new"/>""")
    m, _ = _parse(tmp_path, g2_fields=g2)
    issue = next(i for i in m.issues
                 if i.code == "UNION_TYPE_INCOMPATIBLE")
    assert issue.severity.value == "MANUAL"
    assert "CAST" in issue.suggestion


def test_port_order_mismatch_flagged(tmp_path):
    g2 = ("""
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT" GROUP="in_new"/>
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT" GROUP="in_new"/>""")
    m, _ = _parse(tmp_path, g2_fields=g2)
    issues = [i for i in m.issues if i.code == "UNION_PORT_ORDER"]
    assert len(issues) == 2                       # both positions swapped
    assert "POSITION" in issues[0].message
    # swapped numeric/decimal also warns as widening, never silently ok
    assert any(i.code == "UNION_TYPE_WIDENING" for i in m.issues)


# ---------------------------------------------------------------------------
# generation: UNION ALL, explicit projection, never bare UNION
# ---------------------------------------------------------------------------

def test_union_all_with_explicit_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "u.xml"
    f.write_text(_union_xml())
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"), source_format="powercenter",
            target_format="dbt")
    sql = next((tmp_path / "out" / "dbt").rglob("int_union.sql")).read_text()
    assert "union all" in sql
    assert "select id, amount from sq_hist" in sql
    assert "select id, amount from sq_fresh" in sql
    # never a deduplicating bare UNION
    import re
    assert not re.search(r"union(?!\s+all)", sql.split("union all")[0] +
                         sql.split("union all")[-1])


def test_databricks_union_all(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "u.xml"
    f.write_text(_union_xml())
    from metabridge.engine import convert
    rep = convert(str(f), str(tmp_path / "out"),
                  source_format="powercenter",
                  target_format="databricks")
    sql = next((tmp_path / "out" / "sql").glob("0*_union.sql")).read_text()
    assert "UNION ALL" in sql.upper()
    assert rep["conversion_output"]["errors"]["count"] == 0
