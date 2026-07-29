"""Command 5: SSIS source adapter — packages, data flows, control flow,
variables, connection managers, expressions."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import IssueSeverity, LoadStrategy, TransformationType
from metabridge.parsers.etl_expressions import ssis_expression_to_sql

SSIS = Path(__file__).resolve().parent.parent / "examples" / "etl_legacy" / "ssis"


@pytest.fixture(scope="module")
def pipeline():
    return parse_input(str(SSIS), "ssis")


def test_dataflow_becomes_mapping(pipeline):
    m = pipeline.mapping("loadsales_dft_loadsales")
    assert m is not None
    types = [t.type for t in m.transformations]
    assert TransformationType.SOURCE in types
    assert TransformationType.EXPRESSION in types      # Derived Column
    assert TransformationType.LOOKUP in types
    assert TransformationType.ROUTER in types          # Conditional Split
    assert TransformationType.AGGREGATOR in types
    assert TransformationType.TARGET in types


def test_execution_order_preserved(pipeline):
    dag = next(d for d in pipeline.metadata["workflow_dags"]
               if d["workflow"] == "LoadSales")
    order = dag["execution_order"]
    assert order.index("TruncateStage") < order.index("DFT_LoadSales")
    assert order.index("DFT_LoadSales") < order.index("LoadRegionSummary")
    assert ("DFT_LoadSales", "NotifyFailure") in dag["failure_paths"]


def test_execute_sql_task_dml_becomes_mapping(pipeline):
    m = pipeline.mapping("loadsales_loadregionsummary")
    assert m is not None
    agg = [t for t in m.transformations
           if t.type == TransformationType.AGGREGATOR]
    assert agg and agg[0].properties["group_by"] == ["region"]
    tgt = [t for t in m.transformations
           if t.type == TransformationType.TARGET][0]
    assert tgt.properties["table"] == "region_summary"


def test_variables_and_parameters_classified(pipeline):
    params = {p["name"]: p["class"] for p in pipeline.metadata["parameters"]}
    assert params["User::LoadDate"] == "derived_variable"
    assert params["User::Region"] == "static_variable"
    assert params["BatchId"] == "runtime_parameter"
    assert params["Environment"] == "runtime_parameter"   # project .params


def test_connections_carry_no_credentials(pipeline):
    conns = pipeline.metadata["connections"]
    assert {c["name"] for c in conns} >= {"OLTP", "DW"}
    text = str(conns)
    assert "Integrated Security" not in text and "Password" not in text


def test_script_task_declared_manual(pipeline):
    codes = {i.code for i in pipeline.all_issues()
             if i.severity == IssueSeverity.MANUAL}
    assert "SSIS_SCRIPT_TASK" in codes


def test_event_handler_and_loop_flagged(pipeline):
    codes = {i.code for i in pipeline.all_issues()}
    assert "SSIS_EVENT_HANDLER" in codes
    assert "SSIS_LOOP_CONTAINER" in codes


def test_execute_package_is_worklet(pipeline):
    dag = next(d for d in pipeline.metadata["workflow_dags"]
               if d["workflow"] == "MasterLoad")
    node = next(n for n in dag["nodes"] if n["task_key"] == "RunLoadSales")
    assert node["type"] == "worklet"
    assert node["config"]["package"] == "LoadSales"


def test_passthrough_columns_propagate(pipeline):
    m = pipeline.mapping("loadsales_dft_loadsales")
    derived = m.transformation("DerivedCols")
    names = {p.name for p in derived.ports}
    assert {"customer_id", "status", "net_amount", "is_high_value"} <= names


def test_no_error_issues(pipeline):
    assert not [i for i in pipeline.all_issues()
                if i.severity == IssueSeverity.ERROR]


# --- SSIS expression language ------------------------------------------------

def test_ssis_expressions():
    sql, _ = ssis_expression_to_sql('amount > 1000 ? "H" : "L"')
    assert sql == "CASE WHEN amount > 1000 THEN 'H' ELSE 'L' END"
    sql, _ = ssis_expression_to_sql("ISNULL(email) && status == \"A\"")
    assert "email IS NULL" in sql and "AND" in sql and "= 'A'" in sql
    sql, _ = ssis_expression_to_sql("(DT_STR,50,1252)order_id")
    assert sql == "CAST(order_id AS VARCHAR)"
    sql, notes = ssis_expression_to_sql("@[User::LoadDate]")
    assert sql == ":LoadDate" and notes
    sql, _ = ssis_expression_to_sql("GETDATE()")
    assert sql == "CURRENT_TIMESTAMP()" or sql == "CURRENT_TIMESTAMP"


def test_untranslatable_expression_reports():
    sql, notes = ssis_expression_to_sql("TOKEN(col, \",\", @@@nonsense")
    assert sql == "" and any("untranslatable" in n for n in notes)
