"""Validation engine (module 30): transformation validation tests and
validation_plan.json. Schema/data/SCD validation are covered by
test_testgen.py and test_pc_scd2.py; this file covers what module 30
added."""
import json

import pytest


def _xml() -> str:
    """One repository exercising filter counts, join cardinality, lookup
    match rate, aggregator totals and router distribution."""
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_orders" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="amount" DATATYPE="decimal" PRECISION="12" SCALE="2" FIELDNUMBER="3" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="fct_orders" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
    <TARGETFIELD NAME="amount" DATATYPE="decimal" PRECISION="12" SCALE="2" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="3"/>
   </TARGET>
   <TARGET NAME="agg_orders" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="total_amount" DATATYPE="decimal" PRECISION="18" SCALE="2" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_filtered" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_o" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="12" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="FIL_big" TYPE="Filter">
     <TRANSFORMFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="12" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
     <TABLEATTRIBUTE NAME="Filter Condition" VALUE="amount &gt; 100"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="LKP_C" TYPE="Lookup Procedure">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="12" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="lkp_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="OUTPUT"/>
     <TABLEATTRIBUTE NAME="Lookup table name" VALUE="dim_customer"/>
     <TABLEATTRIBUTE NAME="Lookup condition" VALUE="CUST_KEY = cust_id"/>
     <TABLEATTRIBUTE NAME="Lookup policy on multiple match" VALUE="Use First Value"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_o" TYPE="SOURCE" TRANSFORMATION_NAME="src_orders" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_o" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_o" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="FIL_big" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="FIL_big" TRANSFORMATION_TYPE="Filter"/>
    <INSTANCE NAME="LKP_C" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="LKP_C" TRANSFORMATION_TYPE="Lookup Procedure"/>
    <INSTANCE NAME="TGT_o" TYPE="TARGET" TRANSFORMATION_NAME="fct_orders" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="SRC_o" TOFIELD="order_id" TOINSTANCE="SQ_o"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SRC_o" TOFIELD="cust_id" TOINSTANCE="SQ_o"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRC_o" TOFIELD="amount" TOINSTANCE="SQ_o"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="SQ_o" TOFIELD="order_id" TOINSTANCE="FIL_big"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SQ_o" TOFIELD="cust_id" TOINSTANCE="FIL_big"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SQ_o" TOFIELD="amount" TOINSTANCE="FIL_big"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="FIL_big" TOFIELD="order_id" TOINSTANCE="LKP_C"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="FIL_big" TOFIELD="cust_id" TOINSTANCE="LKP_C"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="FIL_big" TOFIELD="amount" TOINSTANCE="LKP_C"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="LKP_C" TOFIELD="order_id" TOINSTANCE="TGT_o"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="LKP_C" TOFIELD="cust_id" TOINSTANCE="TGT_o"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="LKP_C" TOFIELD="amount" TOINSTANCE="TGT_o"/>
   </MAPPING>
   <MAPPING NAME="m_agg" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_a" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="12" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="AGG_c" TYPE="Aggregator">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT" EXPRESSION="cust_id" GROUPBY="YES"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="12" SCALE="2" PORTTYPE="INPUT"/>
     <TRANSFORMFIELD NAME="total_amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="OUTPUT" EXPRESSION="SUM(amount)"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_a" TYPE="SOURCE" TRANSFORMATION_NAME="src_orders" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_a" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_a" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="AGG_c" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="AGG_c" TRANSFORMATION_TYPE="Aggregator"/>
    <INSTANCE NAME="TGT_a" TYPE="TARGET" TRANSFORMATION_NAME="agg_orders" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SRC_a" TOFIELD="cust_id" TOINSTANCE="SQ_a"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRC_a" TOFIELD="amount" TOINSTANCE="SQ_a"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SQ_a" TOFIELD="cust_id" TOINSTANCE="AGG_c"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SQ_a" TOFIELD="amount" TOINSTANCE="AGG_c"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="AGG_c" TOFIELD="cust_id" TOINSTANCE="TGT_a"/>
    <CONNECTOR FROMFIELD="total_amount" FROMINSTANCE="AGG_c" TOFIELD="total_amount" TOINSTANCE="TGT_a"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>"""


@pytest.fixture()
def out(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "v.xml"
    f.write_text(_xml())
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"),
            source_format="powercenter", target_format="snowflake")
    return tmp_path / "out"


def _tests_for(out, mapping):
    doc = json.loads(
        (out / "validation_tests" / "tests.json").read_text())
    pm = next(p for p in doc["mappings"] if p["mapping"] == mapping)
    return pm["tests"]


def test_lookup_match_rate_generated(out):
    tv = [t for t in _tests_for(out, "filtered")
          if t["test_type"] == "transformation_validation"]
    kinds = {t["rule"]["kind"] for t in tv}
    assert "lookup_match_rate" in kinds
    lkp = next(t for t in tv if t["rule"]["kind"] == "lookup_match_rate")
    assert "LEFT JOIN dim_customer" in lkp["target_sql"]
    assert "IS NULL" in lkp["target_sql"]


def test_aggregator_total_generated(out):
    tv = [t for t in _tests_for(out, "agg")
          if t["test_type"] == "transformation_validation"]
    agg = next(t for t in tv if t["rule"]["kind"] == "aggregator_total")
    assert agg["source_sql"] == \
        "SELECT SUM(amount) AS total FROM src_orders"
    assert "SUM(total_amount)" in agg["target_sql"]
    assert agg["expectation"] == "source_value == target_value"


def test_validation_plan_written_with_four_categories(out):
    plan = json.loads((out / "validation_plan.json").read_text())
    assert plan["categories"] == ["schema_validation", "data_validation",
                                  "transformation_validation",
                                  "scd_validation"]
    pm = next(p for p in plan["mappings"] if p["mapping"] == "filtered")
    assert pm["schema_validation"]        # source/target columns + types
    assert pm["data_validation"]          # row/null/distinct/min/max/...
    assert pm["transformation_validation"]
    assert len(pm["reconciliation_scripts"]) == 2
    assert plan["totals"]["data_validation"] > 0


def test_scd_category_in_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from tests.test_pc_scd2 import _scd2_xml
    f = tmp_path / "s.xml"
    f.write_text(_scd2_xml())
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"),
            source_format="powercenter", target_format="snowflake")
    plan = json.loads(
        (tmp_path / "out" / "validation_plan.json").read_text())
    pm = next(p for p in plan["mappings"] if p["mapping"] == "scd2")
    assert pm["scd_validation"]           # one-current-row, date ranges
    assert plan["totals"]["scd_validation"] >= 3
