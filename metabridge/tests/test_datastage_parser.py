"""Command 5: IBM DataStage DSX adapter — jobs, stages, links, transformer
derivations, sequences, parameters."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import IssueSeverity, TransformationType
from metabridge.parsers.datastage_parser import parse_dsx
from metabridge.parsers.etl_expressions import datastage_expression_to_sql

DS = Path(__file__).resolve().parent.parent / "examples" / "etl_legacy" / "datastage"


@pytest.fixture(scope="module")
def pipeline():
    return parse_input(str(DS), "datastage")


def test_dsx_block_reader():
    jobs = parse_dsx((DS / "warehouse_load.dsx").read_text())
    assert [j["name"] for j in jobs] == ["LoadCustomerDim", "SeqNightlyLoad"]
    assert any(r.get("OLEType") == "CTransformerStage"
               for r in jobs[0]["records"])


def test_job_becomes_mapping(pipeline):
    m = pipeline.mapping("loadcustomerdim")
    assert m is not None
    types = [t.type for t in m.transformations]
    assert types.count(TransformationType.SOURCE) == 1
    assert TransformationType.EXPRESSION in types      # Transformer
    assert TransformationType.FILTER in types          # link constraint
    assert TransformationType.AGGREGATOR in types
    tgt = next(t for t in m.transformations
               if t.type == TransformationType.TARGET)
    assert tgt.properties["table"] == "dim_customer_band"
    assert tgt.properties["schema"] == "dw"


def test_derivations_translated(pipeline):
    m = pipeline.mapping("loadcustomerdim")
    x = m.transformation("Xfm_Clean")
    exprs = {p.name: p.expression for p in x.ports if p.expression}
    assert exprs["full_name"] == "TRIM(first_name) || ' ' || TRIM(last_name)"
    assert exprs["email"] == "LOWER(email)"
    assert exprs["balance_band"].startswith("CASE WHEN balance > 10000")


def test_constraint_filters_before_transformer(pipeline):
    m = pipeline.mapping("loadcustomerdim")
    f = m.transformation("Xfm_Clean_constraint")
    assert f.properties["condition"] == "status = 'ACTIVE'"
    # SQ feeds the constraint, the constraint feeds the transformer
    pairs = {(l.from_transformation, l.to_transformation) for l in m.links}
    assert ("SQ_Read_Customers", "Xfm_Clean_constraint") in pairs
    assert ("Xfm_Clean_constraint", "Xfm_Clean") in pairs


def test_aggregator_expressions(pipeline):
    m = pipeline.mapping("loadcustomerdim")
    agg = m.transformation("Agg_ByBand")
    assert agg.properties["group_by"] == ["balance_band"]
    exprs = {p.name: p.expression.upper() for p in agg.ports
             if p.expression}
    assert exprs["customer_count"] == "COUNT(CUSTOMER_ID)"
    assert exprs["total_balance"] == "SUM(BALANCE)"


def test_stage_variable_declared_manual(pipeline):
    codes = {i.code for i in pipeline.all_issues()
             if i.severity == IssueSeverity.MANUAL}
    assert "DS_STAGE_VARIABLE" in codes


def test_sequence_becomes_workflow_dag(pipeline):
    dag = next(d for d in pipeline.metadata["workflow_dags"]
               if d["workflow"] == "SeqNightlyLoad")
    types = {n["task_key"]: n["type"] for n in dag["nodes"]}
    assert types["RunLoadCustomerDim"] == "session"
    assert types["ArchiveExtract"] == "command"
    assert types["MailOnFailure"] == "email"
    assert ("RunLoadCustomerDim", "MailOnFailure") in dag["failure_paths"]
    node = next(n for n in dag["nodes"]
                if n["task_key"] == "RunLoadCustomerDim")
    assert node["mapping"] == "loadcustomerdim"    # links to the real job


def test_runtime_parameters_captured(pipeline):
    names = {p["name"] for p in pipeline.metadata["parameters"]}
    assert {"pSourceDir", "pLoadDate"} <= names


def test_basic_expression_translation():
    sql, _ = datastage_expression_to_sql(
        'If lnk.qty > 0 Then "IN_STOCK" Else "OUT"')
    assert sql == "CASE WHEN qty > 0 THEN 'IN_STOCK' ELSE 'OUT' END"
    sql, _ = datastage_expression_to_sql("UpCase(lnk.name)")
    assert sql == "UPPER(name)"
    sql, notes = datastage_expression_to_sql('Oconv(lnk.d, "D4-")')
    assert sql == "" and notes                     # declared, not guessed
