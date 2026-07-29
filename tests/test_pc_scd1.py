"""SCD Type 1 detection: signals, business key extraction, explicit MERGE."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import LoadStrategy

US_EXPR = ("IIF(ISNULL(lkp_cust_id), DD_INSERT, "
           "IIF(cust_name &lt;&gt; lkp_name, DD_UPDATE, DD_REJECT))")


def _scd1_xml(lookup_table="dim_customer") -> str:
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_customer" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="dim_customer" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_scd1" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_c" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="LKP_DIM" TYPE="Lookup Procedure">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="lkp_cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="OUTPUT"/>
     <TRANSFORMFIELD NAME="lkp_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="OUTPUT"/>
     <TABLEATTRIBUTE NAME="Lookup table name" VALUE="%(lkp)s"/>
     <TABLEATTRIBUTE NAME="Lookup condition" VALUE="DIM_CUST_ID = cust_id"/>
     <TABLEATTRIBUTE NAME="Lookup policy on multiple match" VALUE="Use Any Value"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="US_SCD" TYPE="Update Strategy">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TABLEATTRIBUTE NAME="Update Strategy Expression" VALUE="%(us)s"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_c" TYPE="SOURCE" TRANSFORMATION_NAME="src_customer" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_c" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_c" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="LKP_DIM" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="LKP_DIM" TRANSFORMATION_TYPE="Lookup Procedure"/>
    <INSTANCE NAME="US_SCD" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="US_SCD" TRANSFORMATION_TYPE="Update Strategy"/>
    <INSTANCE NAME="TGT_d" TYPE="TARGET" TRANSFORMATION_NAME="dim_customer" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SRC_c" TOFIELD="cust_id" TOINSTANCE="SQ_c"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="SRC_c" TOFIELD="cust_name" TOINSTANCE="SQ_c"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SQ_c" TOFIELD="cust_id" TOINSTANCE="LKP_DIM"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="SQ_c" TOFIELD="cust_name" TOINSTANCE="LKP_DIM"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="LKP_DIM" TOFIELD="cust_id" TOINSTANCE="US_SCD"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="LKP_DIM" TOFIELD="cust_name" TOINSTANCE="US_SCD"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="US_SCD" TOFIELD="cust_id" TOINSTANCE="TGT_d"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="US_SCD" TOFIELD="cust_name" TOINSTANCE="TGT_d"/>
   </MAPPING>
   <SESSION NAME="s_m_scd1" MAPPINGNAME="m_scd1" REUSABLE="YES" VERSIONNUMBER="1" ISVALID="YES">
    <ATTRIBUTE NAME="Treat source rows as" VALUE="Data driven"/>
   </SESSION>
   <WORKFLOW NAME="wf_scd1" ISENABLED="YES" VERSIONNUMBER="1">
    <TASKINSTANCE NAME="s_m_scd1" TASKNAME="s_m_scd1" TASKTYPE="Session"/>
   </WORKFLOW>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"lkp": lookup_table, "us": US_EXPR}


@pytest.fixture()
def scd1(tmp_path):
    f = tmp_path / "s.xml"
    f.write_text(_scd1_xml())
    return parse_input(str(f), "powercenter").mapping("scd1")


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------

def test_scd1_detected_with_all_signals(scd1):
    cir = scd1.properties["scd1_cir"]
    assert cir["type"] == "SCD_TYPE_1"
    assert cir["dimension_table"] == "dim_customer"
    assert set(cir["signals"]) == {"lookup_target", "update_strategy",
                                   "insert_new_records",
                                   "update_changed_records",
                                   "compare_columns"}
    assert cir["existence_check_lookup"] == "LKP_DIM"
    assert any("cust_name <> lkp_name" in c
               for c in cir["compared_columns"])


def test_business_key_extracted_from_lookup(scd1):
    assert scd1.properties["scd1_cir"]["business_key"] == ["cust_id"]
    assert scd1.unique_key == ["cust_id"]     # export declared no PK
    assert scd1.load_strategy == LoadStrategy.MERGE


def test_detection_issues(scd1):
    assert any(i.code == "SCD1_DETECTED" and "explicit column mappings"
               in i.message for i in scd1.issues)
    assert any(i.code == "SCD1_LOOKUP_REDUNDANT" for i in scd1.issues)


def test_lookup_against_other_table_is_not_scd1(tmp_path):
    f = tmp_path / "s.xml"
    f.write_text(_scd1_xml(lookup_table="ref_accounts"))
    m = parse_input(str(f), "powercenter").mapping("scd1")
    assert "scd1_cir" not in m.properties
    assert not any(i.code == "SCD1_DETECTED" for i in m.issues)


# ---------------------------------------------------------------------------
# generation: explicit column mappings, never SET *
# ---------------------------------------------------------------------------

def _convert(tmp_path, target, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "s.xml"
    f.write_text(_scd1_xml())
    from metabridge.engine import convert
    return convert(str(f), str(tmp_path / "out"),
                   source_format="powercenter", target_format=target)


@pytest.mark.parametrize("target", ["databricks", "snowflake"])
def test_merge_into_with_explicit_columns(tmp_path, monkeypatch, target):
    rep = _convert(tmp_path, target, monkeypatch)
    sql = next((tmp_path / "out" / "sql").glob("0*_scd1.sql")).read_text()
    assert "MERGE INTO dim_customer t" in sql
    assert "ON t.cust_id = s.cust_id" in sql
    assert "UPDATE SET t.cust_name = s.cust_name" in sql   # explicit
    assert "INSERT (cust_id, cust_name) VALUES (s.cust_id, s.cust_name)" \
        in sql
    assert "SET *" not in sql and "INSERT *" not in sql    # never
    assert rep["conversion_output"]["errors"]["count"] == 0


def test_dbt_incremental_merge_model(tmp_path, monkeypatch):
    _convert(tmp_path, "dbt", monkeypatch)
    sql = next((tmp_path / "out" / "dbt").rglob("models/**/dim_customer.sql")).read_text()
    assert "materialized='incremental'" in sql
    assert "unique_key='cust_id'" in sql
    assert "incremental_strategy='merge'" in sql
