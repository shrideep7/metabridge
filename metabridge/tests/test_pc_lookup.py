"""Lookup handler: CIR contract, match policies, cache strategies,
cardinality risk, dedup CTE generation, diamond/sequence folding."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_XML = EXAMPLES / "powercenter_repo" / "repo_export.xml"


def _lookup_xml(extra_attrs: str = "", return_port: bool = False) -> str:
    ret = ('<TRANSFORMFIELD NAME="acct_id" DATATYPE="integer" '
           'PRECISION="10" SCALE="0" PORTTYPE="RETURN"/>') if return_port \
        else ('<TRANSFORMFIELD NAME="acct_id" DATATYPE="integer" '
              'PRECISION="10" SCALE="0" PORTTYPE="OUTPUT"/>')
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="orders" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="t_out" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="acct_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_lkp" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_o" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="LKP_A" TYPE="Lookup Procedure">
     <TRANSFORMFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     %(ret)s
     <TABLEATTRIBUTE NAME="Lookup table name" VALUE="ACCOUNTS"/>
     <TABLEATTRIBUTE NAME="Lookup condition" VALUE="ACC_CUST = cust_id"/>
     %(extra)s
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_o" TYPE="SOURCE" TRANSFORMATION_NAME="orders" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_o" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_o" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="LKP_A" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="LKP_A" TRANSFORMATION_TYPE="Lookup Procedure"/>
    <INSTANCE NAME="TGT_o" TYPE="TARGET" TRANSFORMATION_NAME="t_out" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="SRC_o" TOFIELD="order_id" TOINSTANCE="SQ_o"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SRC_o" TOFIELD="cust_id" TOINSTANCE="SQ_o"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="SQ_o" TOFIELD="order_id" TOINSTANCE="LKP_A"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SQ_o" TOFIELD="cust_id" TOINSTANCE="LKP_A"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="LKP_A" TOFIELD="order_id" TOINSTANCE="TGT_o"/>
    <CONNECTOR FROMFIELD="acct_id" FROMINSTANCE="LKP_A" TOFIELD="acct_id" TOINSTANCE="TGT_o"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"extra": extra_attrs, "ret": ret}


def _attr(name, value):
    return '<TABLEATTRIBUTE NAME="%s" VALUE="%s"/>' % (name, value)


def _parse(tmp_path, extra="", **kw):
    f = tmp_path / "l.xml"
    f.write_text(_lookup_xml(extra, **kw))
    p = parse_input(str(f), "powercenter")
    return p.mapping("lkp"), p.mapping("lkp").transformation("LKP_A")


# ---------------------------------------------------------------------------
# CIR LOOKUP contract
# ---------------------------------------------------------------------------

def test_cir_contract_all_fields(tmp_path):
    _, lkp = _parse(tmp_path, _attr("Lookup policy on multiple match",
                                    "Use Any Value"))
    cir = lkp.properties["lookup_cir"]
    for field in ("lookup_dataset", "lookup_keys", "return_columns",
                  "match_policy", "cache_strategy", "dynamic_lookup",
                  "override_query"):
        assert field in cir, field
    assert cir["lookup_dataset"] == "ACCOUNTS"
    assert cir["lookup_keys"] == [{"lookup_column": "ACC_CUST",
                                   "input_port": "cust_id",
                                   "operator": "EQUALS"}]
    assert cir["return_columns"] == ["acct_id"]
    assert cir["connected"] is True


# ---------------------------------------------------------------------------
# match policies
# ---------------------------------------------------------------------------

def test_use_first_dedups_and_warns_on_ordering(tmp_path):
    m, lkp = _parse(tmp_path, _attr("Lookup policy on multiple match",
                                    "Use First Value"))
    assert lkp.properties["lookup_cir"]["match_policy"] == "USE_FIRST"
    assert lkp.properties["dedup_keys"] == ["ACC_CUST"]
    assert any(i.code == "LOOKUP_ORDER_NONDETERMINISTIC" and
               "CACHE BUILD ORDER" in i.message for i in m.issues)


def test_use_any_dedups_silently(tmp_path):
    m, lkp = _parse(tmp_path, _attr("Lookup policy on multiple match",
                                    "Use Any Value"))
    assert lkp.properties["dedup_keys"] == ["ACC_CUST"]
    assert any(i.code == "LOOKUP_DEDUPLICATED" for i in m.issues)
    assert not any(i.code == "LOOKUP_CARDINALITY" for i in m.issues)


def test_use_all_is_intentional_multi_match(tmp_path):
    m, lkp = _parse(tmp_path, _attr("Lookup policy on multiple match",
                                    "Use All Values"))
    assert "dedup_keys" not in lkp.properties
    assert any(i.code == "LOOKUP_MULTI_MATCH_INTENTIONAL"
               for i in m.issues)


def test_unspecified_policy_gets_cardinality_warning(tmp_path):
    m, _ = _parse(tmp_path)
    issue = next(i for i in m.issues if i.code == "LOOKUP_CARDINALITY")
    assert issue.severity.value == "WARNING"
    assert "ROW COUNT CHANGES" in issue.message
    assert "row_count" in issue.suggestion       # reconciliation catches it


# ---------------------------------------------------------------------------
# cache strategies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attrs,expected", [
    ("", "STATIC"),
    (_attr("Lookup caching enabled", "NO"), "UNCACHED"),
    (_attr("Lookup cache persistent", "YES"), "PERSISTENT"),
    (_attr("Dynamic Lookup Cache", "YES"), "DYNAMIC"),
])
def test_cache_strategy(tmp_path, attrs, expected):
    _, lkp = _parse(tmp_path, attrs)
    assert lkp.properties["lookup_cir"]["cache_strategy"] == expected


def test_static_cache_is_recommendation_only(tmp_path):
    m, _ = _parse(tmp_path)
    issue = next(i for i in m.issues
                 if i.code == "LOOKUP_CACHE_RUNTIME_ONLY")
    assert issue.severity.value == "INFO"
    assert "BROADCAST" in issue.suggestion       # Databricks recommendation


def test_dynamic_cache_is_manual_with_delta_merge(tmp_path):
    m, lkp = _parse(tmp_path, _attr("Dynamic Lookup Cache", "YES"))
    assert lkp.properties["lookup_cir"]["dynamic_lookup"] is True
    issue = next(i for i in m.issues if i.code == "LOOKUP_DYNAMIC_CACHE")
    assert issue.severity.value == "MANUAL"
    assert "MERGE INTO" in issue.suggestion      # Delta MERGE strategy


# ---------------------------------------------------------------------------
# unconnected + override
# ---------------------------------------------------------------------------

def test_unconnected_lookup_flagged(tmp_path):
    m, lkp = _parse(tmp_path, return_port=True)
    assert lkp.properties["lookup_cir"]["connected"] is False
    issue = next(i for i in m.issues if i.code == "LOOKUP_UNCONNECTED")
    assert issue.severity.value == "MANUAL"
    assert "scalar subquer" in issue.suggestion


def test_override_sql_parsed_via_ast(tmp_path):
    _, lkp = _parse(tmp_path, _attr(
        "Lookup Sql Override",
        "SELECT acc_cust, acct_id FROM accounts WHERE active = 1"))
    cir = lkp.properties["lookup_cir"]
    assert cir["override_query"].startswith("SELECT")
    assert lkp.properties["sql_override_ast"]["parsed"] is True
    assert lkp.properties["sql_override_ast"]["tables"] == ["accounts"]


def test_unparseable_override_is_manual(tmp_path):
    m, _ = _parse(tmp_path, _attr("Lookup Sql Override",
                                  "SELEC broken ((("))
    assert any(i.code == "LOOKUP_OVERRIDE_UNPARSEABLE" and
               i.severity.value == "MANUAL" for i in m.issues)


# ---------------------------------------------------------------------------
# generation: dedup CTE, key-aware qualification, override relation
# ---------------------------------------------------------------------------

def test_dbt_dedup_cte_and_qualified_join(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "l.xml"
    f.write_text(_lookup_xml(_attr("Lookup policy on multiple match",
                                   "Use First Value")))
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"), source_format="powercenter",
            target_format="dbt")
    sql = next((tmp_path / "out" / "dbt").rglob("int_lkp.sql")).read_text()
    assert "row_number() over (partition by ACC_CUST" in sql
    assert "_mb_lkp_rn = 1" in sql
    assert "l.cust_id = lkp.ACC_CUST" in sql     # key-aware orientation
    assert "lkp.acct_id" in sql                  # return column qualified


def test_databricks_plain_join_when_use_all(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "l.xml"
    f.write_text(_lookup_xml(_attr("Lookup policy on multiple match",
                                   "Use All Values")))
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"), source_format="powercenter",
            target_format="databricks")
    sql = next((tmp_path / "out" / "sql").glob("0*_lkp.sql")).read_text()
    assert "LEFT JOIN ACCOUNTS" in sql
    assert "_mb_lkp_rn" not in sql               # no dedup for Use All


# ---------------------------------------------------------------------------
# structural regressions the module exposed
# ---------------------------------------------------------------------------

def test_sequence_folds_into_the_stream():
    p = parse_input(str(REPO_XML), "powercenter")
    m = p.mapping("customer_enrich")
    assert m.transformation("SEQ_SK") is None            # folded away
    exp = m.transformation("EXP_SEQ_SK_1")
    assert exp.port("sk").expression == "ROW_NUMBER() OVER (ORDER BY 1)"
    assert any(i.code == "SEQUENCE_AS_ROW_NUMBER" for i in m.issues)


def test_passive_diamond_folded_through_lookup():
    p = parse_input(str(REPO_XML), "powercenter")
    m = p.mapping("customer_enrich")
    lkp = m.transformation("LKP_ACCOUNT")
    ports = {x.name for x in lkp.ports}
    assert {"name_clean", "status"} <= ports     # rode the passive chain
    edges = {(l.from_transformation, l.to_transformation)
             for l in m.links}
    assert ("EXP_CLEAN", "RTR_CUSTOMER") not in edges    # diamond folded


def test_generated_enrich_sql_is_linear_and_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    convert(str(REPO_XML), str(tmp_path / "out"),
            source_format="powercenter", target_format="databricks")
    sql = next((tmp_path / "out" / "sql").glob(
        "0*_customer_enrich.sql")).read_text()
    assert "FROM None" not in sql
    assert "ROW_NUMBER() OVER (ORDER BY 1) AS sk" in sql
    assert "l.name_clean" in sql                 # diamond fields via lookup
