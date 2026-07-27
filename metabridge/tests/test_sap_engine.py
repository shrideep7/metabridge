"""Command 7: SAP parsers, ABAP analysis, normalization, semantics."""
from pathlib import Path

import pytest

from metabridge.engine import detect_format, parse_input
from metabridge.ir.model import IssueSeverity, LoadStrategy, TransformationType
from metabridge.sap.parsers import analyze_abap, detect_sap, parse_cds, parse_sap

SAP = Path(__file__).resolve().parent.parent / "examples" / "sap_landscape"


@pytest.fixture(scope="module")
def land():
    return parse_sap(str(SAP))


@pytest.fixture(scope="module")
def pipeline():
    return parse_input(str(SAP), "sap")


def test_detection(land):
    assert detect_sap(str(SAP))["detected"]
    assert detect_format(str(SAP)) == "sap"
    assert land.platform == "bw4hana"


def test_inventory_covers_all_object_kinds(land):
    inv = land.inventory()
    assert inv["business_objects"] == 3
    assert inv["infoproviders"] == 3          # 2 ADSO + composite
    assert inv["transformations"] == 1
    assert inv["process_chains"] == 1
    assert inv["calculation_views"] == 1
    assert inv["cds_views"] == 1
    assert inv["queries"] == 1
    assert inv["hierarchies"] == 1
    assert inv["master_data_objects"] == 1
    assert inv["authorizations"] == 1


def test_cds_semantics(land):
    v = land.cds_views[0]
    assert v.name == "ZI_SalesOrder"
    assert "vbak" in [t.lower() for t in v.source_tables]
    assert v.parameters[0]["name"] == "p_from_date"
    assert v.currency_semantics == [{"amount_field": "NetAmount",
                                     "currency_field": "WAERK"}]
    assert v.authorization_check == "#CHECK"
    assert v.associations and v.associations[0]["target"] == "kna1"
    assert v.sql.lower().startswith("select")


def test_abap_analyzer_verdicts(land):
    by = {u.name: u for u in land.abap_units}
    rpt = by["z_sales_report"]
    assert rpt.verdict == "PARTIAL"           # Open SQL + loop + BAPI + RFC
    assert rpt.open_sql and "FROM vbak" in rpt.open_sql[0]
    assert rpt.loops == 1
    assert "BAPI_SALESORDER_GETLIST" in rpt.bapi_calls
    assert "Z_RFC_PUSH" in rpt.rfc_calls
    assert any("row-by-row" in r for r in rpt.business_rules)
    start = by["TR_SALES_start_routine"]
    assert start.verdict == "CONVERTIBLE"     # pure Open SQL


def test_abap_never_silently_converted(pipeline):
    manual = [i for i in pipeline.all_issues()
              if i.severity == IssueSeverity.MANUAL]
    codes = {i.code for i in manual}
    assert "SAP_ABAP_ROUTINE_MANUAL" in codes      # field routine
    assert "SAP_START_ROUTINE" in codes
    m = pipeline.mapping("tr_tr_sales")
    rules = m.transformation("RULES_TR_SALES")
    region = next(p for p in rules.ports if p.name == "region")
    assert region.expression == "NULL"             # placeholder, declared


def test_bw_transformation_lowering(pipeline):
    m = pipeline.mapping("tr_tr_sales")
    assert m.load_strategy == LoadStrategy.MERGE
    assert m.unique_key == ["doc_number"]
    exprs = {p.name: p.expression for t in m.transformations
             for p in t.ports if p.expression}
    assert exprs["amount"] == "amount * 1.0"
    assert exprs["load_date"] == "'20240101'"


def test_composite_provider_and_query(pipeline):
    cp = pipeline.mapping("cp_zcp_sales")
    assert any(t.type == TransformationType.UNION
               for t in cp.transformations)
    q = pipeline.mapping("qry_zq_sales_by_region")
    agg = next(t for t in q.transformations
               if t.type == TransformationType.AGGREGATOR)
    assert agg.properties["group_by"] == ["region", "customer"]
    flt = next(t for t in q.transformations
               if t.type == TransformationType.FILTER)
    assert "load_date >= '20240101'" in flt.properties["condition"]


def test_calc_view_graph(pipeline):
    cv = pipeline.mapping("cv_cv_sales_summary")
    types = [t.type for t in cv.transformations]
    assert TransformationType.JOINER in types
    assert TransformationType.AGGREGATOR in types
    assert TransformationType.FILTER in types
    j = next(t for t in cv.transformations
             if t.type == TransformationType.JOINER)
    assert j.properties["join_type"] == "LEFT"
    assert "kunnr = kunnr" in j.properties["condition"]


def test_hierarchy_and_master_data_never_ignored(pipeline):
    h = pipeline.mapping("dim_customer_hierarchy")
    assert h is not None and h.origin == "sap:hierarchy"
    d = pipeline.mapping("dim_customer")
    assert d is not None
    assert "texts" in d.description


def test_currency_unit_authorization_declared(pipeline):
    codes = {i.code for i in pipeline.all_issues()}
    assert "SAP_CURRENCY_KYF" in codes
    assert "SAP_UNIT_KYF" in codes
    assert "SAP_CURRENCY_SEMANTICS" in codes
    assert "SAP_AUTHORIZATION" in codes
    assert pipeline.metadata["authorizations"][0]["iobj"] == "ZREGION"


def test_process_chain_becomes_workflow_dag(pipeline):
    dag = pipeline.metadata["workflow_dags"][0]
    assert dag["workflow"] == "PC_SALES_DAILY"
    types = {n["task_key"]: n["type"] for n in dag["nodes"]}
    assert types["RUN_DTP"] == "session"
    assert types["NOTIFY"] == "email"
    assert ("RUN_DTP", "NOTIFY") in dag["failure_paths"]
    node = next(n for n in dag["nodes"] if n["task_key"] == "RUN_DTP")
    assert node["mapping"] == "tr_tr_sales"    # DTP resolved to the mapping


def test_cds_parse_unit():
    v = parse_cds("""
@EndUserText.label: 'X'
define view ZTEST as select from mara {
  key mara.matnr as Material,
  mara.mtart as MaterialType
}""")
    assert v.name == "ZTEST" and "FROM mara" in v.sql


def test_abap_unit_pure_sql_convertible():
    a = analyze_abap("SELECT matnr, mtart FROM mara INTO TABLE @lt WHERE "
                     "mtart = 'FERT'.", "unit_test")
    assert a.verdict == "CONVERTIBLE" and a.open_sql
