"""Joiner handler: master/detail semantics, join-type mapping, Sorted Input."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.parsers.pc_joiner import PC_JOIN_TYPES


def _joiner_xml(join_type: str, sorted_input: str = "NO",
                master_flags: bool = True) -> str:
    master_suffix = "/MASTER" if master_flags else ""
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="orders" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <SOURCE NAME="customers" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="c_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="cname" DATATYPE="string" PRECISION="64" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="ord_c" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="cname" DATATYPE="string" PRECISION="64" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_join" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_orders" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="SQ_customers" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="c_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cname" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="JNR_1" TYPE="Joiner">
     <TRANSFORMFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="c_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT%(ms)s"/>
     <TRANSFORMFIELD NAME="cname" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT%(ms)s"/>
     <TABLEATTRIBUTE NAME="Join Type" VALUE="%(jt)s"/>
     <TABLEATTRIBUTE NAME="Join Condition" VALUE="cust_id = c_id"/>
     <TABLEATTRIBUTE NAME="Sorted Input" VALUE="%(si)s"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_orders" TYPE="SOURCE" TRANSFORMATION_NAME="orders" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SRC_customers" TYPE="SOURCE" TRANSFORMATION_NAME="customers" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_orders" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_orders" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="SQ_customers" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_customers" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="JNR_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="JNR_1" TRANSFORMATION_TYPE="Joiner"/>
    <INSTANCE NAME="TGT_ord_c" TYPE="TARGET" TRANSFORMATION_NAME="ord_c" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="SRC_orders" TOFIELD="order_id" TOINSTANCE="SQ_orders"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SRC_orders" TOFIELD="cust_id" TOINSTANCE="SQ_orders"/>
    <CONNECTOR FROMFIELD="c_id" FROMINSTANCE="SRC_customers" TOFIELD="c_id" TOINSTANCE="SQ_customers"/>
    <CONNECTOR FROMFIELD="cname" FROMINSTANCE="SRC_customers" TOFIELD="cname" TOINSTANCE="SQ_customers"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="SQ_orders" TOFIELD="order_id" TOINSTANCE="JNR_1"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SQ_orders" TOFIELD="cust_id" TOINSTANCE="JNR_1"/>
    <CONNECTOR FROMFIELD="c_id" FROMINSTANCE="SQ_customers" TOFIELD="c_id" TOINSTANCE="JNR_1"/>
    <CONNECTOR FROMFIELD="cname" FROMINSTANCE="SQ_customers" TOFIELD="cname" TOINSTANCE="JNR_1"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="JNR_1" TOFIELD="order_id" TOINSTANCE="TGT_ord_c"/>
    <CONNECTOR FROMFIELD="cname" FROMINSTANCE="JNR_1" TOFIELD="cname" TOINSTANCE="TGT_ord_c"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"jt": join_type, "si": sorted_input, "ms": master_suffix}


def _parse(tmp_path, join_type, **kw):
    f = tmp_path / "j.xml"
    f.write_text(_joiner_xml(join_type, **kw))
    p = parse_input(str(f), "powercenter")
    return p, p.mapping("join").transformation("JNR_1")


# ---------------------------------------------------------------------------
# semantic join-type mapping (orientation: left = DETAIL, right = MASTER)
# ---------------------------------------------------------------------------

def test_join_type_map_contract():
    assert PC_JOIN_TYPES == {"Normal Join": "INNER",
                             "Master Outer Join": "LEFT",
                             "Detail Outer Join": "RIGHT",
                             "Full Outer Join": "FULL"}


def test_master_detail_identified_from_port_flags(tmp_path):
    _, jnr = _parse(tmp_path, "Normal Join")
    # customers side carries the MASTER port flags
    assert jnr.properties["master_input"] == "SQ_customers"
    assert jnr.properties["detail_input"] == "SQ_orders"
    assert jnr.properties["left"] == "SQ_orders"        # left = DETAIL
    assert jnr.properties["right"] == "SQ_customers"    # right = MASTER
    assert jnr.properties["condition_cir"]["operator"] == "EQUALS"


@pytest.mark.parametrize("pc_type,sql_type,preserved", [
    ("Normal Join", "INNER", None),
    ("Master Outer Join", "LEFT", "detail"),   # ALL DETAIL rows kept
    ("Detail Outer Join", "RIGHT", "master"),  # ALL MASTER rows kept
    ("Full Outer Join", "FULL", "both"),
])
def test_all_four_join_types(tmp_path, pc_type, sql_type, preserved):
    _, jnr = _parse(tmp_path, pc_type)
    assert jnr.properties["join_type"] == sql_type
    assert jnr.properties["pc_join_type"] == pc_type
    # with left=detail/right=master, LEFT preserves detail, RIGHT master
    if preserved == "detail":
        assert jnr.properties["left"] == jnr.properties["detail_input"]
    if preserved == "master":
        assert jnr.properties["right"] == jnr.properties["master_input"]


def test_outer_join_without_master_flags_is_flagged(tmp_path):
    p, jnr = _parse(tmp_path, "Master Outer Join", master_flags=False)
    m = p.mapping("join")
    issue = next(i for i in m.issues
                 if i.code == "JOINER_ORIENTATION_ASSUMED")
    assert issue.severity.value == "WARNING"
    assert "DETAIL" in issue.suggestion
    assert "left" in jnr.properties          # fallback orientation still set


def test_inner_join_without_flags_is_silent(tmp_path):
    p, _ = _parse(tmp_path, "Normal Join", master_flags=False)
    assert not any(i.code == "JOINER_ORIENTATION_ASSUMED"
                   for i in p.mapping("join").issues)


# ---------------------------------------------------------------------------
# Sorted Input: recommendation only, semantics untouched
# ---------------------------------------------------------------------------

def test_sorted_input_generates_recommendation_not_semantics(tmp_path):
    p, jnr = _parse(tmp_path, "Master Outer Join", sorted_input="YES")
    assert jnr.properties["sorted_input"] is True
    issue = next(i for i in p.mapping("join").issues
                 if i.code == "JOINER_SORTED_INPUT")
    assert issue.severity.value == "INFO"            # never a blocker
    assert "not a semantic change" in issue.message
    assert "ZORDER" in issue.suggestion or "cluster" in issue.suggestion
    # join semantics identical to the unsorted variant
    p2, jnr2 = _parse(tmp_path, "Master Outer Join", sorted_input="NO")
    assert jnr.properties["join_type"] == jnr2.properties["join_type"]
    assert jnr.properties["left"] == jnr2.properties["left"]


# ---------------------------------------------------------------------------
# generation: dbt SQL JOIN + Databricks SQL JOIN
# ---------------------------------------------------------------------------

def test_dbt_join_orientation(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "j.xml"
    f.write_text(_joiner_xml("Master Outer Join"))
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"), source_format="powercenter",
            target_format="dbt")
    sql = next((tmp_path / "out" / "dbt").rglob("int_join.sql")).read_text()
    # all detail (orders) rows preserved: orders LEFT JOIN customers
    assert "left join" in sql
    assert sql.index("sq_orders") < sql.index("left join")
    assert "sq_customers" in sql.split("left join", 1)[1]
    assert "l.cust_id = r.c_id" in sql


def test_databricks_join_orientation(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "j.xml"
    f.write_text(_joiner_xml("Detail Outer Join"))
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"), source_format="powercenter",
            target_format="databricks")
    sql = next((tmp_path / "out" / "sql").glob("0*_join.sql")).read_text()
    # all master (customers) rows preserved: orders RIGHT JOIN customers
    assert "RIGHT JOIN" in sql.upper()
