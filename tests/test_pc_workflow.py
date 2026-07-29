"""Workflow parser: WORKFLOW DAG CIR, condition classification, and the
per-target orchestration specs (dbt job, Databricks Workflow, generic)."""
import json

import pytest
import yaml

from metabridge.engine import parse_input
from metabridge.parsers.pc_workflow import (build_workflow_dag,
                                            classify_link_condition)


def _mapping_block(n: str) -> str:
    return """
   <MAPPING NAME="m_%(n)s" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_%(n)s" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_%(n)s" TYPE="SOURCE" TRANSFORMATION_NAME="src_%(n)s" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_%(n)s" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_%(n)s" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="TGT_%(n)s" TYPE="TARGET" TRANSFORMATION_NAME="tgt_%(n)s" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SRC_%(n)s" TOFIELD="id" TOINSTANCE="SQ_%(n)s"/>
    <CONNECTOR FROMFIELD="id" FROMINSTANCE="SQ_%(n)s" TOFIELD="id" TOINSTANCE="TGT_%(n)s"/>
   </MAPPING>""" % {"n": n}


def _tables_block(n: str) -> str:
    return """
   <SOURCE NAME="src_%(n)s" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="tgt_%(n)s" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
   </TARGET>""" % {"n": n}


def _xml() -> str:
    """The spec's example: START -> SESSION_CUSTOMER -SUCCESS->
    SESSION_ACCOUNT -FAILURE-> EMAIL_FAILURE, plus one of every other
    orchestration task type."""
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   %(tables)s
   %(mappings)s
   <SESSION NAME="SESSION_CUSTOMER" MAPPINGNAME="m_customer" REUSABLE="YES" VERSIONNUMBER="1" ISVALID="YES"/>
   <SESSION NAME="SESSION_ACCOUNT" MAPPINGNAME="m_account" REUSABLE="YES" VERSIONNUMBER="1" ISVALID="YES"/>
   <TASK NAME="EMAIL_FAILURE" TYPE="Email">
    <ATTRIBUTE NAME="Email User Name" VALUE="oncall@corp.com"/>
    <ATTRIBUTE NAME="Email Subject" VALUE="account load failed"/>
   </TASK>
   <TASK NAME="CMD_ARCHIVE" TYPE="Command">
    <VALUEPAIR NAME="Command1" VALUE="sh /scripts/archive.sh"/>
   </TASK>
   <TASK NAME="DEC_VOLUME" TYPE="Decision">
    <ATTRIBUTE NAME="Decision Expression" VALUE="$SESSION_CUSTOMER.TgtSuccessRows &gt; 0"/>
   </TASK>
   <TASK NAME="TIMER_WAIT" TYPE="Timer">
    <ATTRIBUTE NAME="Relative time: from the start time of this task" VALUE="00:05:00"/>
   </TASK>
   <TASK NAME="EW_FILE" TYPE="Event Wait">
    <ATTRIBUTE NAME="Event Name" VALUE="ev_file_arrived"/>
   </TASK>
   <TASK NAME="ER_DONE" TYPE="Event Raise">
    <ATTRIBUTE NAME="Event Name" VALUE="ev_load_done"/>
   </TASK>
   <TASK NAME="ASG_RUNDATE" TYPE="Assignment">
    <VALUEPAIR NAME="$$RUN_DATE" VALUE="SYSDATE"/>
   </TASK>
   <TASK NAME="CTL_ABORT" TYPE="Control">
    <ATTRIBUTE NAME="Control Option" VALUE="Fail top-level workflow"/>
   </TASK>
   <WORKLET NAME="wklt_post" VERSIONNUMBER="1">
    <SESSION NAME="s_post" MAPPINGNAME="m_post" REUSABLE="NO" VERSIONNUMBER="1" ISVALID="YES"/>
    <TASKINSTANCE NAME="s_post" TASKNAME="s_post" TASKTYPE="Session"/>
   </WORKLET>
   <WORKFLOW NAME="wf_daily" ISENABLED="YES" VERSIONNUMBER="1">
    <TASKINSTANCE NAME="SESSION_CUSTOMER" TASKNAME="SESSION_CUSTOMER" TASKTYPE="Session"/>
    <TASKINSTANCE NAME="SESSION_ACCOUNT" TASKNAME="SESSION_ACCOUNT" TASKTYPE="Session"/>
    <TASKINSTANCE NAME="EMAIL_FAILURE" TASKNAME="EMAIL_FAILURE" TASKTYPE="Email"/>
    <TASKINSTANCE NAME="CMD_ARCHIVE" TASKNAME="CMD_ARCHIVE" TASKTYPE="Command"/>
    <TASKINSTANCE NAME="DEC_VOLUME" TASKNAME="DEC_VOLUME" TASKTYPE="Decision"/>
    <TASKINSTANCE NAME="TIMER_WAIT" TASKNAME="TIMER_WAIT" TASKTYPE="Timer"/>
    <TASKINSTANCE NAME="EW_FILE" TASKNAME="EW_FILE" TASKTYPE="Event Wait"/>
    <TASKINSTANCE NAME="ER_DONE" TASKNAME="ER_DONE" TASKTYPE="Event Raise"/>
    <TASKINSTANCE NAME="ASG_RUNDATE" TASKNAME="ASG_RUNDATE" TASKTYPE="Assignment"/>
    <TASKINSTANCE NAME="CTL_ABORT" TASKNAME="CTL_ABORT" TASKTYPE="Control"/>
    <TASKINSTANCE NAME="wklt_post" TASKNAME="wklt_post" TASKTYPE="Worklet"/>
    <WORKFLOWLINK FROMTASK="Start" TOTASK="SESSION_CUSTOMER" CONDITION=""/>
    <WORKFLOWLINK FROMTASK="SESSION_CUSTOMER" TOTASK="SESSION_ACCOUNT" CONDITION="$SESSION_CUSTOMER.Status = SUCCEEDED"/>
    <WORKFLOWLINK FROMTASK="SESSION_ACCOUNT" TOTASK="EMAIL_FAILURE" CONDITION="$SESSION_ACCOUNT.Status = FAILED"/>
    <WORKFLOWLINK FROMTASK="SESSION_ACCOUNT" TOTASK="DEC_VOLUME" CONDITION="$SESSION_ACCOUNT.Status = SUCCEEDED"/>
    <WORKFLOWLINK FROMTASK="DEC_VOLUME" TOTASK="CMD_ARCHIVE" CONDITION="$DEC_VOLUME.Condition = 1"/>
    <WORKFLOWLINK FROMTASK="CMD_ARCHIVE" TOTASK="TIMER_WAIT" CONDITION=""/>
    <WORKFLOWLINK FROMTASK="TIMER_WAIT" TOTASK="EW_FILE" CONDITION=""/>
    <WORKFLOWLINK FROMTASK="EW_FILE" TOTASK="ASG_RUNDATE" CONDITION=""/>
    <WORKFLOWLINK FROMTASK="ASG_RUNDATE" TOTASK="wklt_post" CONDITION=""/>
    <WORKFLOWLINK FROMTASK="wklt_post" TOTASK="ER_DONE" CONDITION=""/>
    <WORKFLOWLINK FROMTASK="EMAIL_FAILURE" TOTASK="CTL_ABORT" CONDITION=""/>
   </WORKFLOW>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {
        "tables": "".join(_tables_block(n)
                          for n in ("customer", "account", "post")),
        "mappings": "".join(_mapping_block(n)
                            for n in ("customer", "account", "post")),
    }


@pytest.fixture()
def dag(tmp_path):
    f = tmp_path / "wf.xml"
    f.write_text(_xml())
    p = parse_input(str(f), "powercenter")
    return p.metadata["workflow_dags"][0]


# ---------------------------------------------------------------------------
# condition classification
# ---------------------------------------------------------------------------

def test_classify_link_condition():
    assert classify_link_condition("") == ("always", "")
    assert classify_link_condition("$s.Status = SUCCEEDED")[0] == "success"
    assert classify_link_condition("$s.Status = FAILED")[0] == "failure"
    kind, cond = classify_link_condition("$DEC.Condition = 1")
    assert kind == "conditional" and cond == "$DEC.Condition = 1"


# ---------------------------------------------------------------------------
# the DAG CIR
# ---------------------------------------------------------------------------

def test_all_eleven_task_types_parsed(dag):
    types = {n["type"] for n in dag["nodes"]}
    assert {"start", "session", "worklet", "command", "email", "decision",
            "timer", "event_wait", "event_raise", "assignment",
            "control"} <= types


def test_spec_example_paths_preserved(dag):
    # START -> SESSION_CUSTOMER -SUCCESS-> SESSION_ACCOUNT
    #                                     -FAILURE-> EMAIL_FAILURE
    assert ["SESSION_CUSTOMER", "SESSION_ACCOUNT"] in dag["success_paths"]
    assert ["SESSION_ACCOUNT", "EMAIL_FAILURE"] in dag["failure_paths"]
    start_edge = next(e for e in dag["edges"] if e["from"] == "Start")
    assert start_edge["kind"] == "always"
    cond_edge = next(e for e in dag["edges"] if e["to"] == "CMD_ARCHIVE")
    assert cond_edge["kind"] == "conditional"
    assert cond_edge["condition"] == "$DEC_VOLUME.Condition = 1"


def test_execution_order_and_payloads(dag):
    order = dag["execution_order"]
    assert order.index("SESSION_CUSTOMER") < order.index("SESSION_ACCOUNT")
    assert order.index("SESSION_ACCOUNT") < order.index("EMAIL_FAILURE")
    assert order.index("DEC_VOLUME") < order.index("CMD_ARCHIVE")
    by_key = {n["task_key"]: n for n in dag["nodes"]}
    assert by_key["SESSION_CUSTOMER"]["mapping"] == "customer"
    assert by_key["CMD_ARCHIVE"]["config"]["commands"] == \
        ["sh /scripts/archive.sh"]
    assert "TgtSuccessRows" in \
        by_key["DEC_VOLUME"]["config"]["Decision Expression"]
    assert by_key["wklt_post"]["worklet_dag"]["nodes"][0]["mapping"] == \
        "post"


# ---------------------------------------------------------------------------
# orchestration outputs per target
# ---------------------------------------------------------------------------

def _convert(tmp_path, target, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "wf.xml"
    f.write_text(_xml())
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"),
            source_format="powercenter", target_format=target)
    return tmp_path / "out"


def test_databricks_job_spec(tmp_path, monkeypatch):
    out = _convert(tmp_path, "databricks", monkeypatch)
    spec = json.loads(
        (out / "sql" / "orchestration" / "wf_daily_job.json").read_text())
    tasks = {t["task_key"]: t for t in spec["tasks"]}
    # task_key + depends_on + run_if, per the spec
    acct = tasks["SESSION_ACCOUNT"]
    assert acct["depends_on"] == [{"task_key": "SESSION_CUSTOMER"}]
    assert acct["run_if"] == "ALL_SUCCESS"
    assert "sql_task" in acct and "account" in \
        acct["sql_task"]["file"]["path"]
    # failure handling
    mail = tasks["EMAIL_FAILURE"]
    assert mail["run_if"] == "AT_LEAST_ONE_FAILED"
    assert spec["email_notifications"]["on_failure"] == ["oncall@corp.com"]
    # entry task has no depends_on (START folded away)
    assert "depends_on" not in tasks["SESSION_CUSTOMER"]
    # worklet becomes its own job spec
    assert "run_job_task" in tasks["wklt_post"]
    wl = json.loads(
        (out / "sql" / "orchestration" / "wklt_post_job.json").read_text())
    assert wl["tasks"][0]["task_key"] == "s_post"


def test_dbt_job_spec(tmp_path, monkeypatch):
    out = _convert(tmp_path, "dbt", monkeypatch)
    spec = yaml.safe_load(
        (out / "dbt" / "orchestration" / "wf_daily_job.yml").read_text())
    run_step = spec["job"]["steps"][0]
    assert run_step.startswith("dbt run --select ")
    assert run_step.index("customer") < run_step.index("account")
    kinds = {t["type"]: t for t in spec["non_model_tasks"]}
    assert kinds["email"]["on"] == "failure"
    assert kinds["command"]["payload"]["commands"] == \
        ["sh /scripts/archive.sh"]
    assert "recommendation" in kinds["decision"]


def test_generic_spec_for_other_warehouses(tmp_path, monkeypatch):
    out = _convert(tmp_path, "snowflake", monkeypatch)
    spec = json.loads(
        (out / "sql" / "orchestration" / "wf_daily_workflow.json")
        .read_text())
    assert "Snowflake TASK" in spec["recommendation"]
    acct = next(t for t in spec["tasks"]
                if t["task_key"] == "SESSION_ACCOUNT")
    deps = [d for d in acct["depends_on"] if d["task_key"] != "Start"]
    assert deps == [{"task_key": "SESSION_CUSTOMER", "when": "success",
                     "condition": "$SESSION_CUSTOMER.Status = SUCCEEDED"}]


def test_worklet_dag_standalone_builder():
    wf = {"name": "w", "kind": "workflow",
          "tasks": [{"instance": "A", "task": "A", "type": "Session"}],
          "links": [{"from": "Start", "to": "A", "condition": ""}],
          "sessions": {"A": {"name": "A", "mapping": "ma"}},
          "task_defs": {}, "worklets": {}}
    dag = build_workflow_dag(wf)
    assert dag["execution_order"][0] == "Start"
    assert dag["nodes"][0]["mapping"] == "ma"
