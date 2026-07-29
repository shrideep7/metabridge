"""SCD Type 2 detection: signals, preserved columns, snapshot vs
incremental dbt strategy, MERGE-based warehouse SCD2, validation tests."""
import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import LoadStrategy

US_EXPR = ("IIF(ISNULL(lkp_cust_id), DD_INSERT, "
           "IIF(cust_name &lt;&gt; lkp_name, DD_UPDATE, DD_REJECT))")

_SK_FIELD = ('<TRANSFORMFIELD NAME="cust_sk" DATATYPE="integer" '
             'PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>')
_SK_TGT = ('<TARGETFIELD NAME="cust_sk" DATATYPE="integer" PRECISION="10" '
           'SCALE="0" KEYTYPE="PRIMARY KEY" NULLABLE="NOTNULL" '
           'FIELDNUMBER="0"/>')
_SK_CONN = ('<CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SQ_c" '
            'TOFIELD="cust_sk" TOINSTANCE="US_SCD"/>'
            '<CONNECTOR FROMFIELD="cust_sk" FROMINSTANCE="US_SCD" '
            'TOFIELD="cust_sk" TOINSTANCE="TGT_d"/>')


def _scd2_xml(with_surrogate=False, truncate=False) -> str:
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_customer" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="dim_customer" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    %(sk_tgt)s
    <TARGETFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
    <TARGETFIELD NAME="eff_start_dt" DATATYPE="date/time" PRECISION="29" SCALE="9" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="3"/>
    <TARGETFIELD NAME="eff_end_dt" DATATYPE="date/time" PRECISION="29" SCALE="9" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="4"/>
    <TARGETFIELD NAME="current_flag" DATATYPE="string" PRECISION="1" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="5"/>
   </TARGET>
   <MAPPING NAME="m_scd2" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_c" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="LKP_DIM" TYPE="Lookup Procedure">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="lkp_cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="OUTPUT"/>
     <TRANSFORMFIELD NAME="lkp_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="OUTPUT"/>
     <TABLEATTRIBUTE NAME="Lookup table name" VALUE="dim_customer"/>
     <TABLEATTRIBUTE NAME="Lookup condition" VALUE="DIM_CUST_ID = cust_id"/>
     <TABLEATTRIBUTE NAME="Lookup policy on multiple match" VALUE="Use Any Value"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="US_SCD" TYPE="Update Strategy">
     %(sk_field)s
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
    %(sk_conn)s
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="US_SCD" TOFIELD="cust_id" TOINSTANCE="TGT_d"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="US_SCD" TOFIELD="cust_name" TOINSTANCE="TGT_d"/>
   </MAPPING>
   <SESSION NAME="s_m_scd2" MAPPINGNAME="m_scd2" REUSABLE="YES" VERSIONNUMBER="1" ISVALID="YES">
    <ATTRIBUTE NAME="Treat source rows as" VALUE="Data driven"/>
    %(trunc)s
   </SESSION>
   <WORKFLOW NAME="wf_scd2" ISENABLED="YES" VERSIONNUMBER="1">
    <TASKINSTANCE NAME="s_m_scd2" TASKNAME="s_m_scd2" TASKTYPE="Session"/>
   </WORKFLOW>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {
        "us": US_EXPR,
        "sk_tgt": _SK_TGT if with_surrogate else "",
        "sk_field": _SK_FIELD if with_surrogate else "",
        "sk_conn": _SK_CONN if with_surrogate else "",
        "trunc": '<ATTRIBUTE NAME="Truncate target table option" '
                 'VALUE="YES"/>' if truncate else "",
    }


def _mapping(tmp_path, **kw):
    f = tmp_path / "s.xml"
    f.write_text(_scd2_xml(**kw))
    return parse_input(str(f), "powercenter").mapping("scd2")


@pytest.fixture()
def scd2(tmp_path):
    return _mapping(tmp_path)


# ---------------------------------------------------------------------------
# detection: signals + preserved columns
# ---------------------------------------------------------------------------

def test_scd2_detected_with_all_signals(scd2):
    cir = scd2.properties["scd2_cir"]
    assert cir["type"] == "SCD_TYPE_2"
    assert cir["dimension_table"] == "dim_customer"
    assert set(cir["signals"]) == {"lookup_existing_dimension",
                                   "effective_start_date",
                                   "effective_end_date", "current_flag",
                                   "expire_old_row", "insert_new_version"}


def test_scd2_preserves_all_five_column_roles(scd2):
    cir = scd2.properties["scd2_cir"]
    assert cir["business_key"] == ["cust_id"]
    assert cir["effective_start_column"] == "eff_start_dt"
    assert cir["effective_end_column"] == "eff_end_dt"
    assert cir["current_flag_column"] == "current_flag"
    assert any("cust_name <> lkp_name" in c
               for c in cir["change_detection_columns"])


def test_scd2_strategy_survives_data_driven_session(scd2):
    assert scd2.load_strategy == LoadStrategy.SCD2
    assert scd2.unique_key == ["cust_id"]
    assert "scd1_cir" not in scd2.properties     # SCD2 takes precedence
    assert any(i.code == "SCD2_DETECTED" for i in scd2.issues)
    assert any(i.code == "SCD2_FLAG_DOMAIN" for i in scd2.issues)


def test_truncate_session_does_not_erase_history(tmp_path):
    m = _mapping(tmp_path, truncate=True)
    assert m.load_strategy == LoadStrategy.SCD2   # not FULL
    assert any(i.code == "SCD2_TRUNCATE_SESSION" for i in m.issues)


def test_surrogate_key_switches_dbt_to_incremental(tmp_path):
    cir = _mapping(tmp_path, with_surrogate=True).properties["scd2_cir"]
    assert cir["surrogate_key"] == "cust_sk"
    assert cir["dbt_strategy"] == "incremental_scd2"


def test_no_versioning_columns_means_no_scd2(tmp_path, monkeypatch):
    # same shape without eff dates / flag is module 22's SCD1, never SCD2
    from tests.test_pc_scd1 import _scd1_xml
    f = tmp_path / "s.xml"
    f.write_text(_scd1_xml())
    m = parse_input(str(f), "powercenter").mapping("scd1")
    assert "scd2_cir" not in m.properties
    assert "scd1_cir" in m.properties


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------

def _convert(tmp_path, target, monkeypatch, **kw):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "s.xml"
    f.write_text(_scd2_xml(**kw))
    from metabridge.engine import convert
    return convert(str(f), str(tmp_path / "out"),
                   source_format="powercenter", target_format=target)


@pytest.mark.parametrize("target", ["databricks", "snowflake"])
def test_merge_based_scd2_on_warehouses(tmp_path, monkeypatch, target):
    _convert(tmp_path, target, monkeypatch)
    sql = next((tmp_path / "out" / "sql").glob("0*_scd2.sql")).read_text()
    assert "MERGE INTO dim_customer t" in sql
    # expire the current version using the mapping's OWN columns
    assert "t.eff_end_dt = CURRENT_TIMESTAMP" in sql
    assert "t.current_flag = 'N'" in sql
    # open the new version
    assert "INSERT (cust_id, cust_name, eff_start_dt, eff_end_dt, " \
           "current_flag)" in sql
    assert "CURRENT_TIMESTAMP" in sql and "'Y'" in sql
    # changed keys staged twice: keyed expire + NULL-keyed insert
    assert "UNION ALL" in sql and "mb_merge_key_0" in sql
    assert "t.current_flag = 'Y'" in sql          # only current rows match
    assert "SET *" not in sql and "INSERT *" not in sql


def test_dbt_snapshot_when_semantics_match(tmp_path, monkeypatch):
    _convert(tmp_path, "dbt", monkeypatch)
    snap = next((tmp_path / "out" / "dbt").rglob("snapshots/scd2.sql"))
    text = snap.read_text()
    assert "{% snapshot scd2 %}" in text
    assert "unique_key='cust_id'" in text
    assert "strategy='check'" in text
    assert "check_cols=['cust_name']" in text


def test_dbt_incremental_scd2_when_surrogate_key(tmp_path, monkeypatch):
    rep = _convert(tmp_path, "dbt", monkeypatch, with_surrogate=True)
    root = tmp_path / "out" / "dbt"
    assert not list(root.rglob("snapshots/scd2.sql"))
    model = next(root.rglob("models/**/dim_customer.sql")).read_text()
    # versions merge on the SURROGATE key — business key would erase history
    assert "materialized='incremental'" in model
    assert "unique_key='cust_sk'" in model
    # honest degradation: the model does not close superseded versions
    assert "scd2" in rep["conversion_output"]["manual_review_assets"]


# ---------------------------------------------------------------------------
# SCD2 validation tests (testgen)
# ---------------------------------------------------------------------------

def test_scd2_validation_tests_generated(tmp_path, monkeypatch):
    _convert(tmp_path, "snowflake", monkeypatch)
    import json
    doc = json.loads(
        (tmp_path / "out" / "validation_tests" / "tests.json").read_text())
    tests = [t for mp in doc["mappings"] for t in mp["tests"]]
    kinds = {t.get("rule", {}).get("kind") for t in tests
             if isinstance(t.get("rule"), dict)}
    assert {"scd2_current_uniqueness", "scd2_date_range",
            "scd2_flag_end_date_agreement"} <= kinds
    uniq = next(t for t in tests
                if t.get("rule", {}).get("kind") == "scd2_current_uniqueness")
    assert "current_flag = 'Y'" in uniq["target_sql"]
    assert "HAVING COUNT(*) > 1" in uniq["target_sql"]
