"""Normalizer handler: OCCURS groups, GCID/GK preservation, UNION ALL."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import TransformationType
from conftest import model_sql


def _norm_xml(occurs: int = 4, drop_instance: bool = False,
              second_group: str = "") -> str:
    instances = "".join(
        '<TRANSFORMFIELD NAME="SALES%d" DATATYPE="decimal" PRECISION="18" '
        'SCALE="2" PORTTYPE="INPUT"/>' % i
        for i in range(1, occurs + 1)
        if not (drop_instance and i == occurs))
    conn_in = "".join(
        '<CONNECTOR FROMFIELD="q%d" FROMINSTANCE="SQ_s" TOFIELD="SALES%d" '
        'TOINSTANCE="NRM_1"/>' % (i, i)
        for i in range(1, occurs + 1)
        if not (drop_instance and i == occurs))
    src_fields = "".join(
        '<SOURCEFIELD NAME="q%d" DATATYPE="decimal" PRECISION="18" '
        'SCALE="2" FIELDNUMBER="%d" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>'
        % (i, i + 1) for i in range(1, occurs + 1))
    sq_fields = "".join(
        '<TRANSFORMFIELD NAME="q%d" DATATYPE="decimal" PRECISION="18" '
        'SCALE="2" PORTTYPE="INPUT/OUTPUT"/>' % i
        for i in range(1, occurs + 1))
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="store_q" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="store_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    %(src_fields)s
   </SOURCE>
   <TARGET NAME="store_sales" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="store_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="sales" DATATYPE="decimal" PRECISION="18" SCALE="2" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
    <TARGETFIELD NAME="quarter" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="3"/>
    <TARGETFIELD NAME="row_key" DATATYPE="bigint" PRECISION="19" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="4"/>
   </TARGET>
   <MAPPING NAME="m_norm" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_s" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="store_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     %(sq_fields)s
    </TRANSFORMATION>
    <TRANSFORMATION NAME="NRM_1" TYPE="Normalizer">
     <TRANSFORMFIELD NAME="store_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT" OCCURS="0"/>
     %(instances)s
     <TRANSFORMFIELD NAME="SALES" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="OUTPUT" OCCURS="%(occurs)d"/>
     <TRANSFORMFIELD NAME="GCID_SALES" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="OUTPUT"/>
     <TRANSFORMFIELD NAME="GK_SALES" DATATYPE="bigint" PRECISION="19" SCALE="0" PORTTYPE="OUTPUT"/>
     %(second_group)s
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_s" TYPE="SOURCE" TRANSFORMATION_NAME="store_q" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_s" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_s" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="NRM_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="NRM_1" TRANSFORMATION_TYPE="Normalizer"/>
    <INSTANCE NAME="TGT_s" TYPE="TARGET" TRANSFORMATION_NAME="store_sales" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="store_id" FROMINSTANCE="SRC_s" TOFIELD="store_id" TOINSTANCE="SQ_s"/>
    <CONNECTOR FROMFIELD="store_id" FROMINSTANCE="SQ_s" TOFIELD="store_id" TOINSTANCE="NRM_1"/>
    %(conn_in)s
    <CONNECTOR FROMFIELD="SALES" FROMINSTANCE="NRM_1" TOFIELD="sales" TOINSTANCE="TGT_s"/>
    <CONNECTOR FROMFIELD="GCID_SALES" FROMINSTANCE="NRM_1" TOFIELD="quarter" TOINSTANCE="TGT_s"/>
    <CONNECTOR FROMFIELD="GK_SALES" FROMINSTANCE="NRM_1" TOFIELD="row_key" TOINSTANCE="TGT_s"/>
    <CONNECTOR FROMFIELD="store_id" FROMINSTANCE="NRM_1" TOFIELD="store_id" TOINSTANCE="TGT_s"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"instances": instances, "conn_in": conn_in,
                   "occurs": occurs, "src_fields": src_fields,
                   "sq_fields": sq_fields, "second_group": second_group}


@pytest.fixture()
def normed(tmp_path):
    f = tmp_path / "n.xml"
    f.write_text(_norm_xml())
    return parse_input(str(f), "powercenter").mapping("norm")


# ---------------------------------------------------------------------------
# CIR + restructuring
# ---------------------------------------------------------------------------

def test_occurs_metadata_parsed_into_branches(normed):
    # normalizer replaced by 4 occurrence branches + a union
    assert normed.transformation("NRM_1") is None
    for i in range(1, 5):
        b = normed.transformation("NRM_NRM_1_%d" % i)
        assert b is not None
        assert b.port("SALES").expression == "SALES%d" % i
        assert b.port("GCID_SALES").expression == str(i)   # identifier kept
    un = normed.transformation("UN_NRM_1")
    assert un.type == TransformationType.UNION
    assert len(un.properties["inputs"]) == 4


def test_generated_key_stamped_before_split(normed):
    gk = normed.transformation("EXP_NRM_1_GK")
    assert gk.port("GK_SALES").expression == \
        "ROW_NUMBER() OVER (ORDER BY 1)"
    edges = {(l.from_transformation, l.to_transformation)
             for l in normed.links}
    # gk node sits between SQ and every branch
    assert ("SQ_s", "EXP_NRM_1_GK") in edges
    for i in range(1, 5):
        assert ("EXP_NRM_1_GK", "NRM_NRM_1_%d" % i) in edges
    assert any(i.code == "NORMALIZER_GK" for i in normed.issues)


def test_unpivot_note_with_stack_alternative(normed):
    issue = next(i for i in normed.issues
                 if i.code == "NORMALIZER_UNPIVOTED")
    assert "occurs 4" in issue.message
    assert "GCID_SALES" in issue.message
    assert "STACK(4, 1, SALES1" in issue.suggestion    # Databricks compact
    assert "dbt_utils.unpivot" in issue.suggestion


# ---------------------------------------------------------------------------
# honesty on unresolvable shapes
# ---------------------------------------------------------------------------

def test_missing_instance_columns_go_manual(tmp_path):
    f = tmp_path / "n.xml"
    f.write_text(_norm_xml(drop_instance=True))
    m = parse_input(str(f), "powercenter").mapping("norm")
    issue = next(i for i in m.issues
                 if i.code == "NORMALIZER_INSTANCES_UNRESOLVED")
    assert issue.severity.value == "MANUAL"
    assert "SALES" in issue.message


def test_multi_group_goes_manual(tmp_path):
    second = ('<TRANSFORMFIELD NAME="RET1" DATATYPE="decimal" '
              'PRECISION="18" SCALE="2" PORTTYPE="INPUT"/>'
              '<TRANSFORMFIELD NAME="RET2" DATATYPE="decimal" '
              'PRECISION="18" SCALE="2" PORTTYPE="INPUT"/>'
              '<TRANSFORMFIELD NAME="RET" DATATYPE="decimal" '
              'PRECISION="18" SCALE="2" PORTTYPE="OUTPUT" OCCURS="2"/>')
    f = tmp_path / "n.xml"
    f.write_text(_norm_xml(second_group=second))
    m = parse_input(str(f), "powercenter").mapping("norm")
    issue = next(i for i in m.issues
                 if i.code == "NORMALIZER_MULTI_GROUP")
    assert issue.severity.value == "MANUAL"
    assert "4 x 2" in issue.message


# ---------------------------------------------------------------------------
# generation on multiple targets
# ---------------------------------------------------------------------------

def _convert(tmp_path, target, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "n.xml"
    f.write_text(_norm_xml())
    from metabridge.engine import convert
    return convert(str(f), str(tmp_path / "out"),
                   source_format="powercenter", target_format=target)


@pytest.mark.parametrize("target", ["databricks", "snowflake"])
def test_union_all_unpivot_on_warehouses(tmp_path, monkeypatch, target):
    rep = _convert(tmp_path, target, monkeypatch)
    sql = next((tmp_path / "out" / "sql").glob("0*_norm.sql")).read_text()
    assert sql.upper().count("UNION ALL") == 3          # 4 branches
    assert "SALES2" in sql and "SALES4" in sql
    assert "GCID_SALES" in sql
    assert rep["conversion_output"]["errors"]["count"] == 0


def test_dbt_union_all_model(tmp_path, monkeypatch):
    _convert(tmp_path, "dbt", monkeypatch)
    sql = model_sql(tmp_path / "out" / "dbt")
    assert sql.count("union all") == 3
    assert "SALES3 as SALES" in sql
    assert "3 as GCID_SALES" in sql
