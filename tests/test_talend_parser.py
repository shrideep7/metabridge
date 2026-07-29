"""Command 5: Talend adapter — .item jobs, tMap, contexts, subjob triggers."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import IssueSeverity, LoadStrategy, TransformationType
from metabridge.parsers.etl_expressions import talend_expression_to_sql

TAL = Path(__file__).resolve().parent.parent / "examples" / "etl_legacy" / "talend"


@pytest.fixture(scope="module")
def pipeline():
    return parse_input(str(TAL), "talend")


def test_jobs_become_mappings(pipeline):
    assert {m.name for m in pipeline.mappings} == {"load_orders",
                                                   "housekeeping"}


def test_tmap_becomes_join_plus_expression(pipeline):
    m = pipeline.mapping("load_orders")
    join = m.transformation("tMap_1_join")
    assert join.type == TransformationType.JOINER
    assert join.properties["join_type"] == "LEFT"
    assert join.properties["condition"] == "customer_id = customer_id"
    expr = m.transformation("tMap_1")
    exprs = {p.name: p.expression for p in expr.ports if p.expression}
    assert exprs["status_uc"] == "UPPER(status)"
    assert exprs["value_band"].startswith("CASE WHEN amount > 1000")
    # pass-through mapper entries stay plain ports
    names = {p.name for p in expr.ports}
    assert {"order_id", "customer_name", "segment", "amount"} <= names


def test_filter_and_aggregate(pipeline):
    m = pipeline.mapping("load_orders")
    f = m.transformation("tFilterRow_1")
    assert f.properties["condition"] == "status_uc = 'SHIPPED'"
    agg = m.transformation("tAggregateRow_1")
    assert agg.properties["group_by"] == ["segment", "value_band"]
    exprs = {p.name: p.expression for p in agg.ports if p.expression}
    assert exprs["total_amount"] == "SUM(amount)"
    assert exprs["order_count"] == "COUNT(order_id)"


def test_upsert_action_becomes_merge(pipeline):
    assert pipeline.mapping("load_orders").load_strategy == LoadStrategy.MERGE


def test_source_query_carried_as_override(pipeline):
    m = pipeline.mapping("load_orders")
    sq = m.transformation("SQ_tOracleInput_1")
    assert "FROM ods.orders" in sq.properties["sql_override"]


def test_contexts_become_parameters(pipeline):
    names = {p["name"] for p in pipeline.metadata["parameters"]}
    assert {"db_host", "load_date", "archive_dir"} <= names


def test_tjava_is_workflow_command_and_manual(pipeline):
    m = pipeline.mapping("load_orders")
    assert m.transformation("tJava_1") is None      # not in the data flow
    dag = next(d for d in pipeline.metadata["workflow_dags"]
               if d["workflow"] == "load_orders")
    node = next(n for n in dag["nodes"] if n["task_key"] == "tJava_1")
    assert node["type"] == "command"
    assert "TALEND_JAVA_MANUAL" in {i.code for i in pipeline.all_issues()
                                    if i.severity == IssueSeverity.MANUAL}


def test_trunjob_links_child_job(pipeline):
    dag = next(d for d in pipeline.metadata["workflow_dags"]
               if d["workflow"] == "load_orders")
    node = next(n for n in dag["nodes"] if n["task_key"] == "tRunJob_1")
    assert node["type"] == "worklet"
    assert node["config"]["job"] == "housekeeping"
    assert ("tOracleInput_1", "tJava_1") in dag["failure_paths"]


def test_java_expression_translation():
    sql, _ = talend_expression_to_sql("StringHandling.UPCASE(row1.city)")
    assert sql == "UPPER(city)"
    sql, _ = talend_expression_to_sql('row1.qty > 5 ? "BULK" : "UNIT"')
    assert sql == "CASE WHEN qty > 5 THEN 'BULK' ELSE 'UNIT' END"
    sql, _ = talend_expression_to_sql("row2.name == null")
    assert sql == "name IS NULL"
    sql, notes = talend_expression_to_sql(
        "routines.MyCustom.transform(row1.x)")
    assert sql == "" and notes                     # declared, not guessed
    sql, notes = talend_expression_to_sql("context.load_date")
    assert sql == ":load_date"
