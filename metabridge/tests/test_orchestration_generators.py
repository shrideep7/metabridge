"""Command 6: target generation matrix — everything from COR, semantics
preserved, no pairwise converters."""
import ast
import json
from pathlib import Path

import pytest

from metabridge.orchestration.generators import (
    ORCH_TARGETS, generate_adf, generate_airflow, generate_autosys,
    generate_execution_doc, generate_orchestration, generate_stepfunctions,
)
from metabridge.orchestration.parsers import parse_orchestration
from metabridge.orchestration.validate import (
    migration_intelligence, validate_cor,
)

ORCH = Path(__file__).resolve().parent.parent / "examples" / "orchestration"
SOURCES = ("airflow", "adf", "stepfunctions", "controlm", "autosys",
           "idmc_taskflow", "dbtcloud", "cron")
_DIR = {"stepfunctions": "stepfunctions", "controlm": "controlm"}


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("target", ORCH_TARGETS)
def test_matrix_generates_valid_artifacts(tmp_path, source, target):
    folder = {"glue_workflow": "glue"}.get(source, source)
    cor = parse_orchestration(str(ORCH / folder))
    m = generate_orchestration(cor, target, str(tmp_path))
    assert m["files"], (source, target)
    for f in m["files"]:
        text = (tmp_path / f).read_text()
        if f.endswith(".py"):
            ast.parse(text)                    # generated DAGs are valid
        if f.endswith(".json"):
            json.loads(text)


def test_airflow_generation_preserves_semantics():
    cor = parse_orchestration(str(ORCH / "adf"))
    wf = cor.workflow("pl_daily_sales")
    code = generate_airflow(wf)
    assert 'dag_id="pl_daily_sales"' in code
    assert "retries=3" in code                       # Copy Sales policy
    assert "retry_delay=timedelta(seconds=60)" in code
    assert 'trigger_rule="one_failed"' in code       # failure path
    assert "# MANUAL" in code                        # choice/loop declared
    # dependency order carried
    assert code.index("Copy_Sales = ") < code.index("Copy_Sales >> ")


def test_adf_generation_preserves_retry_and_failure():
    cor = parse_orchestration(str(ORCH / "stepfunctions"))
    wf = cor.workflows[0]
    out = generate_adf(wf)
    pipeline = out["%s_pipeline.json" % wf.name]
    acts = {a["name"]: a for a in pipeline["properties"]["activities"]}
    ex = acts["ExtractOrders"]
    assert ex["policy"]["retry"] == 3
    notify = acts["NotifyFailure"]
    assert {"activity": "ExtractOrders",
            "dependencyConditions": ["Failed"]} in notify["dependsOn"]


def test_stepfunctions_generation_from_airflow():
    cor = parse_orchestration(str(ORCH / "airflow"))
    asl = generate_stepfunctions(cor.workflows[0])
    assert asl["StartAt"] == "wait_for_extract"
    st = asl["States"]["load_orders"]
    assert st["Retry"][0]["MaxAttempts"] == 2
    assert asl["States"]["publish_marts"].get("Next") or \
        asl["States"]["publish_marts"].get("End")


def test_autosys_jil_from_controlm():
    cor = parse_orchestration(str(ORCH / "controlm"))
    jil = generate_autosys(cor.workflows[0])
    assert "INSERT_JOB: LOAD_GL" in jil.upper()
    assert "condition: s(EXTRACT_GL)" in jil
    assert "n_retrys: 2" in jil


def test_schedule_carried_to_targets(tmp_path):
    cor = parse_orchestration(str(ORCH / "dbtcloud"))
    m = generate_orchestration(cor, "airflow", str(tmp_path))
    code = (tmp_path / m["files"][0]).read_text()
    assert "schedule='0 1 * * *'" in code


def test_execution_doc(tmp_path):
    cor = parse_orchestration(str(ORCH / "autosys"))
    v = validate_cor(cor)
    doc = generate_execution_doc(cor, migration_intelligence(cor, v), v)
    assert "Execution order:" in doc and "mermaid" in doc
    assert "Failure paths:" in doc


def test_no_pairwise_converters_exist():
    src = Path(__file__).resolve().parent.parent / "src" / "metabridge" \
        / "orchestration"
    body = "\n".join(f.read_text() for f in src.glob("*.py"))
    import re
    assert not re.search(
        r"def\s+\w*(airflow_to_adf|adf_to_airflow|controlm_to_autosys|"
        r"\w+_to_(airflow|adf|fabric|stepfunctions|controlm|autosys))",
        body)
