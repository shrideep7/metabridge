"""Command 5: Ab Initio adapter — text graphs, DML, XFR, plans, psets,
binary-graph honesty."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import IssueSeverity, TransformationType
from metabridge.parsers.abinitio_parser import parse_dml, parse_xfr
from metabridge.parsers.etl_expressions import xfr_expression_to_sql

ABI = Path(__file__).resolve().parent.parent / "examples" / "etl_legacy" / "abinitio"


@pytest.fixture(scope="module")
def pipeline():
    return parse_input(str(ABI), "abinitio")


def test_dml_record_format():
    ports = parse_dml((ABI / "customers.dml").read_text())
    assert [p.name for p in ports][:3] == ["customer_id", "first_name",
                                           "last_name"]
    assert ports[0].datatype == "decimal"


def test_xfr_rules():
    rules = parse_xfr((ABI / "clean_customers.xfr").read_text())
    assert {r["column"] for r in rules} == {
        "customer_id", "full_name", "email", "region", "balance", "band"}


def test_text_graph_becomes_mapping(pipeline):
    m = pipeline.mapping("load_customers")
    assert m is not None
    reformat = m.transformation("Clean_Customers")
    exprs = {p.name: p.expression for p in reformat.ports if p.expression}
    assert exprs["full_name"] == \
        "CONCAT(TRIM(first_name), ' ', TRIM(last_name))"
    assert exprs["region"] == "COALESCE(region, 'UNKNOWN')"
    rollup = m.transformation("Rollup_by_Region")
    assert rollup.type == TransformationType.AGGREGATOR
    assert rollup.properties["group_by"] == ["region"]
    agg_exprs = {p.name: p.expression.upper() for p in rollup.ports
                 if p.expression}
    assert agg_exprs["total_balance"] == "SUM(BALANCE)"
    tgt = next(t for t in m.transformations
               if t.type == TransformationType.TARGET)
    assert tgt.properties["table"] == "region_balance_summary"
    assert tgt.properties["schema"] == "dw"


def test_execution_order_flows(pipeline):
    m = pipeline.mapping("load_customers")
    pairs = {(l.from_transformation, l.to_transformation) for l in m.links}
    assert ("Clean_Customers", "Filter_Active") in pairs
    assert ("Filter_Active", "Rollup_by_Region") in pairs


def test_binary_graph_declared_not_guessed(pipeline):
    errs = [i for i in pipeline.all_issues()
            if i.code == "ABINITIO_BINARY_GRAPH"]
    assert errs and "air object save" in errs[0].suggestion
    # the binary graph produced no mapping
    assert pipeline.mapping("legacy_binary") is None


def test_scan_and_partition_semantics(pipeline):
    codes = {i.code: i.severity for i in pipeline.all_issues()}
    assert codes["ABINITIO_SCAN_MANUAL"] == IssueSeverity.MANUAL
    assert codes["ABINITIO_PARALLELISM"] == IssueSeverity.INFO


def test_plan_becomes_workflow_dag(pipeline):
    dag = next(d for d in pipeline.metadata["workflow_dags"]
               if d["workflow"] == "nightly")
    types = {n["task_key"]: n["type"] for n in dag["nodes"]}
    assert types["LoadCustomers"] == "session"      # resolves to the graph
    assert types["ArchiveInputs"] == "command"
    assert ("LoadCustomers", "PageOncall") in dag["failure_paths"]
    order = dag["execution_order"]
    assert order.index("LoadCustomers") < order.index("ArchiveInputs")


def test_pset_parameters(pipeline):
    names = {p["name"] for p in pipeline.metadata["parameters"]}
    assert {"AI_SERIAL", "AI_MFS", "LOAD_DATE"} <= names


def test_xfr_expression_translation():
    sql, _ = xfr_expression_to_sql("string_upcase(in.city)")
    assert sql == "UPPER(city)"
    sql, _ = xfr_expression_to_sql('if (in.qty > 0) "Y" else "N"')
    assert sql == "CASE WHEN qty > 0 THEN 'Y' ELSE 'N' END"
    sql, _ = xfr_expression_to_sql('first_defined(in.region, "NA")')
    assert sql == "COALESCE(region, 'NA')"
    sql, notes = xfr_expression_to_sql('lookup("rates", in.ccy)')
    assert sql == "" and notes                      # declared, not guessed
