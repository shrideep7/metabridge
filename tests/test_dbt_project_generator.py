"""dbt project generator (module 27): required structure, deterministic
naming (the m_LOAD_CUSTOMER_DIM example), migration_manifest.json."""
import json

import pytest

from metabridge.generators.dbt_naming import (base_name, mart_name,
                                              plan_names, stg_name)
from metabridge.ir.model import Mapping


def _xml() -> str:
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_customer" DATABASETYPE="Oracle" DBDNAME="CRM" OWNERNAME="crm" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="dim_customer" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="PRIMARY KEY" NULLABLE="NOTNULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_LOAD_CUSTOMER_DIM" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_c" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="EXP_CLEAN" TYPE="Expression">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="OUTPUT" EXPRESSION="UPPER(LTRIM(RTRIM(cust_name)))"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_c" TYPE="SOURCE" TRANSFORMATION_NAME="src_customer" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_c" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_c" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="EXP_CLEAN" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="EXP_CLEAN" TRANSFORMATION_TYPE="Expression"/>
    <INSTANCE NAME="TGT_d" TYPE="TARGET" TRANSFORMATION_NAME="dim_customer" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SRC_c" TOFIELD="cust_id" TOINSTANCE="SQ_c"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="SRC_c" TOFIELD="cust_name" TOINSTANCE="SQ_c"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SQ_c" TOFIELD="cust_id" TOINSTANCE="EXP_CLEAN"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="SQ_c" TOFIELD="cust_name" TOINSTANCE="EXP_CLEAN"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="EXP_CLEAN" TOFIELD="cust_id" TOINSTANCE="TGT_d"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="EXP_CLEAN" TOFIELD="cust_name" TOINSTANCE="TGT_d"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>"""


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "p.xml"
    f.write_text(_xml())
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"),
            source_format="powercenter", target_format="dbt")
    return tmp_path / "out" / "dbt"


# ---------------------------------------------------------------------------
# deterministic naming
# ---------------------------------------------------------------------------

def test_base_name_strips_powercenter_noise():
    assert base_name("m_LOAD_CUSTOMER_DIM") == "customer"
    assert base_name("LOAD_CUSTOMER_DIM") == "customer"
    assert base_name("src_customer") == "customer"
    assert base_name("raw_sales") == "sales"
    assert base_name("customer_orders") == "customer_orders"


def test_mart_prefix_by_signal():
    m = Mapping(name="LOAD_CUSTOMER_DIM")
    m.properties["scd1_cir"] = {"type": "SCD_TYPE_1"}
    assert mart_name(m) == "dim_customer"
    f = Mapping(name="LOAD_SALES_FACT")
    assert mart_name(f) == "fct_sales"
    assert stg_name("src_customer") == "stg_customer"


def test_plan_is_deterministic(tmp_path):
    from metabridge.engine import parse_input
    f = tmp_path / "p.xml"
    f.write_text(_xml())
    a = plan_names(parse_input(str(f), "powercenter"))
    b = plan_names(parse_input(str(f), "powercenter"))
    assert a == b


# ---------------------------------------------------------------------------
# the spec's example: m_LOAD_CUSTOMER_DIM -> stg_/int_/dim_customer
# ---------------------------------------------------------------------------

def test_example_decomposition(project):
    stg = (project / "models" / "staging" / "stg_customer.sql").read_text()
    assert "{{ source('crm', 'src_customer') }}" in stg
    intm = (project / "models" / "intermediate" / "int_customer.sql"
            ).read_text()
    assert "{{ ref('stg_customer') }}" in intm
    assert "UPPER(LTRIM(RTRIM(cust_name)))" in intm
    assert "materialized='view'" in intm
    mart = (project / "models" / "marts" / "dim_customer.sql").read_text()
    assert "select * from {{ ref('int_customer') }}" in mart


def test_required_structure(project):
    assert (project / "dbt_project.yml").exists()
    assert (project / "models" / "staging" / "sources.yml").exists()
    assert (project / "models" / "schema.yml").exists()
    for d in ("models/staging", "models/intermediate", "models/marts",
              "snapshots", "macros", "tests"):
        assert (project / d).is_dir(), d


def test_schema_yml_documents_and_links_back(project):
    import yaml
    doc = yaml.safe_load((project / "models" / "schema.yml").read_text())
    by_name = {m["name"]: m for m in doc["models"]}
    assert "m_LOAD_CUSTOMER_DIM" in \
        by_name["dim_customer"]["meta"]["powercenter_mapping"]
    assert "description" in by_name["stg_customer"]


# ---------------------------------------------------------------------------
# migration_manifest.json: PC object -> CIR object -> dbt object
# ---------------------------------------------------------------------------

def test_migration_manifest(project):
    doc = json.loads((project / "migration_manifest.json").read_text())
    objs = {o["powercenter_object"]: o for o in doc["objects"]}
    m = objs["m_LOAD_CUSTOMER_DIM"]
    assert m["cir_object"] == "LOAD_CUSTOMER_DIM"
    assert m["cir_type"] == "Mapping"
    names = {o["name"]: o for o in m["dbt_objects"]}
    assert names["int_customer"]["role"] == "transformation_logic"
    assert names["dim_customer"]["role"] == "mart"
    assert names["dim_customer"]["path"] == "models/marts/dim_customer.sql"
    src = objs["src_customer"]
    assert src["dbt_objects"][0]["name"] == "stg_customer"
