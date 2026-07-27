"""Router handler: independent branches, multi-match, NULL-safe DEFAULT."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import TransformationType

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_XML = EXAMPLES / "powercenter_repo" / "repo_export.xml"


def _router_xml(g1_cond="AMOUNT &gt; 100000",
                g2_cond="AMOUNT &gt;= 50000 AND AMOUNT &lt;= 100000",
                g2_name="MEDIUM_VALUE") -> str:
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="txns" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="txn_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="t_high" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="txn_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
   </TARGET>
   <TARGET NAME="t_med" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="txn_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
   </TARGET>
   <TARGET NAME="t_other" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="txn_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
   </TARGET>
   <MAPPING NAME="m_route" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_t" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="txn_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="RTR_V" TYPE="Router">
     <TRANSFORMFIELD NAME="txn_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT"/>
     <TRANSFORMFIELD NAME="txn_id1" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="OUTPUT" REF_FIELD="txn_id" GROUP="HIGH_VALUE"/>
     <TRANSFORMFIELD NAME="txn_id2" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="OUTPUT" REF_FIELD="txn_id" GROUP="%(g2n)s"/>
     <TRANSFORMFIELD NAME="txn_id3" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="OUTPUT" REF_FIELD="txn_id" GROUP="DEFAULT"/>
     <GROUP NAME="HIGH_VALUE" EXPRESSION="%(g1)s" TYPE="OUTPUT"/>
     <GROUP NAME="%(g2n)s" EXPRESSION="%(g2)s" TYPE="OUTPUT"/>
     <GROUP NAME="DEFAULT" TYPE="OUTPUT/DEFAULT"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_t" TYPE="SOURCE" TRANSFORMATION_NAME="txns" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_t" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_t" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="RTR_V" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="RTR_V" TRANSFORMATION_TYPE="Router"/>
    <INSTANCE NAME="TGT_high" TYPE="TARGET" TRANSFORMATION_NAME="t_high" TRANSFORMATION_TYPE="Target Definition"/>
    <INSTANCE NAME="TGT_med" TYPE="TARGET" TRANSFORMATION_NAME="t_med" TRANSFORMATION_TYPE="Target Definition"/>
    <INSTANCE NAME="TGT_other" TYPE="TARGET" TRANSFORMATION_NAME="t_other" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="txn_id" FROMINSTANCE="SRC_t" TOFIELD="txn_id" TOINSTANCE="SQ_t"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRC_t" TOFIELD="amount" TOINSTANCE="SQ_t"/>
    <CONNECTOR FROMFIELD="txn_id" FROMINSTANCE="SQ_t" TOFIELD="txn_id" TOINSTANCE="RTR_V"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SQ_t" TOFIELD="amount" TOINSTANCE="RTR_V"/>
    <CONNECTOR FROMFIELD="txn_id1" FROMINSTANCE="RTR_V" TOFIELD="txn_id" TOINSTANCE="TGT_high"/>
    <CONNECTOR FROMFIELD="txn_id2" FROMINSTANCE="RTR_V" TOFIELD="txn_id" TOINSTANCE="TGT_med"/>
    <CONNECTOR FROMFIELD="txn_id3" FROMINSTANCE="RTR_V" TOFIELD="txn_id" TOINSTANCE="TGT_other"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"g1": g1_cond, "g2": g2_cond, "g2n": g2_name}


@pytest.fixture()
def routed(tmp_path):
    f = tmp_path / "r.xml"
    f.write_text(_router_xml())
    return parse_input(str(f), "powercenter").mapping("route")


# ---------------------------------------------------------------------------
# CIR ROUTER + branch structure
# ---------------------------------------------------------------------------

def test_cir_routes_contract(routed):
    routes = routed.properties["routers"]["RTR_V"]["routes"]
    by_name = {x["name"]: x for x in routes}
    assert by_name["HIGH_VALUE"]["condition"] == "AMOUNT > 100000"
    assert by_name["HIGH_VALUE"]["output"] == ["TGT_high"]
    assert by_name["MEDIUM_VALUE"]["output"] == ["TGT_med"]
    assert by_name["DEFAULT"]["default"] is True


def test_router_replaced_by_independent_branches(routed):
    assert routed.transformation("RTR_V") is None      # node retired
    for g in ("HIGH_VALUE", "MEDIUM_VALUE", "DEFAULT"):
        fil = routed.transformation("FIL_RTR_V_%s" % g)
        rte = routed.transformation("RTE_RTR_V_%s" % g)
        assert fil is not None and fil.type == TransformationType.FILTER
        assert rte is not None
    edges = {(l.from_transformation, l.to_transformation)
             for l in routed.links}
    # every branch reads the SAME upstream — multi-match preserved
    assert ("SQ_t", "FIL_RTR_V_HIGH_VALUE") in edges
    assert ("SQ_t", "FIL_RTR_V_MEDIUM_VALUE") in edges
    assert ("RTE_RTR_V_HIGH_VALUE", "TGT_high") in edges
    assert ("RTE_RTR_V_MEDIUM_VALUE", "TGT_med") in edges


def test_default_group_negation_is_null_safe(routed):
    fil = routed.transformation("FIL_RTR_V_DEFAULT")
    cond = fil.properties["condition"]
    assert "NOT COALESCE((AMOUNT > 100000), FALSE)" in cond
    assert cond.count("NOT COALESCE") == 2
    assert " AND " in cond


def test_multi_match_note_recorded(routed):
    issue = next(i for i in routed.issues if i.code == "ROUTER_BRANCHED")
    assert "multi-match semantics preserved" in issue.message


def test_no_single_case_statement_generated(tmp_path, monkeypatch):
    """The forbidden shape: one CASE picking a single route."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "r.xml"
    f.write_text(_router_xml())
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"), source_format="powercenter",
            target_format="databricks")
    sql = next((tmp_path / "out" / "sql").glob("0*_route.sql")).read_text()
    assert "_mb_route_group" not in sql
    assert "WHERE" in sql and "AMOUNT > 100000" in sql


def test_overlapping_groups_both_receive_the_row(tmp_path, monkeypatch):
    """HIGH_VALUE: amount > 100000 and ALL_POS: amount > 0 OVERLAP — a
    150000 row must flow to BOTH branches (never first-match-wins)."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "r.xml"
    f.write_text(_router_xml(g2_cond="AMOUNT &gt; 0", g2_name="ALL_POS"))
    p = parse_input(str(f), "powercenter")
    m = p.mapping("route")
    high = m.transformation("FIL_RTR_V_HIGH_VALUE")
    allpos = m.transformation("FIL_RTR_V_ALL_POS")
    assert high.properties["condition"] == "AMOUNT > 100000"
    assert allpos.properties["condition"] == "AMOUNT > 0"
    # independent branches from the same upstream: both match 150000
    edges = {(l.from_transformation, l.to_transformation)
             for l in m.links}
    assert ("SQ_t", "FIL_RTR_V_HIGH_VALUE") in edges
    assert ("SQ_t", "FIL_RTR_V_ALL_POS") in edges


def test_dead_group_skipped_with_note(tmp_path):
    xml = _router_xml().replace(
        '<CONNECTOR FROMFIELD="txn_id3" FROMINSTANCE="RTR_V" '
        'TOFIELD="txn_id" TOINSTANCE="TGT_other"/>', "")
    f = tmp_path / "r.xml"
    f.write_text(xml)
    m = parse_input(str(f), "powercenter").mapping("route")
    assert m.transformation("FIL_RTR_V_DEFAULT") is None
    assert any(i.code == "ROUTER_DEAD_GROUP" and "DEFAULT" in i.message
               for i in m.issues)


def test_convergent_branches_get_a_union(tmp_path):
    """Two groups feeding ONE consumer: PC unions the routed rows."""
    xml = _router_xml().replace(
        'TOFIELD="txn_id" TOINSTANCE="TGT_med"',
        'TOFIELD="txn_id" TOINSTANCE="TGT_high"')
    f = tmp_path / "r.xml"
    f.write_text(xml)
    m = parse_input(str(f), "powercenter").mapping("route")
    un = m.transformation("UN_RTR_V_TGT_high")
    assert un is not None and un.type == TransformationType.UNION
    assert sorted(un.properties["inputs"]) == \
        ["RTE_RTR_V_HIGH_VALUE", "RTE_RTR_V_MEDIUM_VALUE"]
    edges = {(l.from_transformation, l.to_transformation)
             for l in m.links}
    assert ("UN_RTR_V_TGT_high", "TGT_high") in edges


# ---------------------------------------------------------------------------
# fixture regression: the enrichment router now truly filters
# ---------------------------------------------------------------------------

def test_enrichment_router_filters_for_real(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    rep = convert(str(REPO_XML), str(tmp_path / "out"),
                  source_format="powercenter", target_format="databricks")
    sql = next((tmp_path / "out" / "sql").glob(
        "0*_customer_enrich.sql")).read_text()
    assert "WHERE" in sql and "status = 'A'" in sql   # real filtering now
    assert "_mb_route_group" not in sql
    assert rep["conversion_output"]["errors"]["count"] == 0
