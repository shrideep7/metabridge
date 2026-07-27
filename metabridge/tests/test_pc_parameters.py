"""Parameter and variable engine: classification, per-target strategies,
and the stateful-variable guardrail (never silently a static var)."""
import pytest

from metabridge.engine import parse_input
from metabridge.ir.model import IssueSeverity
from metabridge.parsers.pc_parameters import classify


def _xml() -> str:
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_sales" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="sale_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="updated_at" DATATYPE="date/time" PRECISION="29" SCALE="9" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="tgt_sales" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="sale_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="load_run_id" DATATYPE="string" PRECISION="64" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_sales" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <MAPPINGVARIABLE NAME="$$LOAD_DATE" DATATYPE="date/time" DEFAULTVALUE="01/01/2026" ISPARAM="YES" AGGFUNCTION=""/>
    <MAPPINGVARIABLE NAME="$$LAST_RUN_DATE" DATATYPE="date/time" DEFAULTVALUE="01/01/1970" ISPARAM="NO" AGGFUNCTION="MAX"/>
    <TRANSFORMATION NAME="SQ_s" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="sale_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="updated_at" DATATYPE="date/time" PRECISION="29" SCALE="9" PORTTYPE="INPUT/OUTPUT"/>
     <TABLEATTRIBUTE NAME="Source Filter" VALUE="updated_at &gt; $$LAST_RUN_DATE"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="EXP_ID" TYPE="Expression">
     <TRANSFORMFIELD NAME="sale_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="load_run_id" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="OUTPUT" EXPRESSION="$PMWorkflowRunId"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_s" TYPE="SOURCE" TRANSFORMATION_NAME="src_sales" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_s" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_s" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="EXP_ID" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="EXP_ID" TRANSFORMATION_TYPE="Expression"/>
    <INSTANCE NAME="TGT_s" TYPE="TARGET" TRANSFORMATION_NAME="tgt_sales" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="sale_id" FROMINSTANCE="SRC_s" TOFIELD="sale_id" TOINSTANCE="SQ_s"/>
    <CONNECTOR FROMFIELD="updated_at" FROMINSTANCE="SRC_s" TOFIELD="updated_at" TOINSTANCE="SQ_s"/>
    <CONNECTOR FROMFIELD="sale_id" FROMINSTANCE="SQ_s" TOFIELD="sale_id" TOINSTANCE="EXP_ID"/>
    <CONNECTOR FROMFIELD="sale_id" FROMINSTANCE="EXP_ID" TOFIELD="sale_id" TOINSTANCE="TGT_s"/>
    <CONNECTOR FROMFIELD="load_run_id" FROMINSTANCE="EXP_ID" TOFIELD="load_run_id" TOINSTANCE="TGT_s"/>
   </MAPPING>
   <SESSION NAME="s_m_sales" MAPPINGNAME="m_sales" REUSABLE="YES" VERSIONNUMBER="1" ISVALID="YES">
    <ATTRIBUTE NAME="Parameter Filename" VALUE="/infa/params/daily.prm"/>
   </SESSION>
   <WORKFLOW NAME="wf_sales" ISENABLED="YES" VERSIONNUMBER="1">
    <WORKFLOWVARIABLE NAME="$$WF_RETRIES" DATATYPE="integer" DEFAULTVALUE="3" ISPERSISTENT="NO" USERDEFINED="YES"/>
    <WORKFLOWVARIABLE NAME="$$WF_LAST_FILE" DATATYPE="string" DEFAULTVALUE="" ISPERSISTENT="YES" USERDEFINED="YES"/>
    <TASKINSTANCE NAME="s_m_sales" TASKNAME="s_m_sales" TASKTYPE="Session"/>
   </WORKFLOW>
  </FOLDER>
 </REPOSITORY>
</POWERMART>"""


@pytest.fixture()
def pipe(tmp_path):
    f = tmp_path / "p.xml"
    f.write_text(_xml())
    return parse_input(str(f), "powercenter")


def _reg(pipe):
    return {e["name"]: e
            for e in pipe.metadata["parameter_registry"]["parameters"]}


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def test_classify_the_four_kinds():
    assert classify("$$LOAD_DATE", is_param=True) == "runtime_parameter"
    assert classify("$$LAST_RUN_DATE", is_param=False,
                    aggregation="MAX") == "stateful_variable"
    assert classify("$PMWorkflowRunId") == "system_variable"
    assert classify("$PMWorkflowName") == "system_variable"
    assert classify("$PMSourceFileDir") == "environment_configuration"


def test_registry_collects_all_scopes(pipe):
    reg = _reg(pipe)
    assert reg["$$LOAD_DATE"]["classification"] == "runtime_parameter"
    assert reg["$$LOAD_DATE"]["scope"] == "mapping"
    assert reg["$$LOAD_DATE"]["used_by"] == ["sales"]
    assert reg["$$LAST_RUN_DATE"]["classification"] == "stateful_variable"
    assert reg["$$LAST_RUN_DATE"]["aggregation"] == "MAX"
    assert reg["$PMWorkflowRunId"]["classification"] == "system_variable"
    assert reg["$PMWorkflowRunId"]["scope"] == "system"
    assert reg["$$WF_RETRIES"]["classification"] == "runtime_parameter"
    assert reg["$$WF_RETRIES"]["scope"] == "workflow"
    assert reg["$$WF_LAST_FILE"]["classification"] == "stateful_variable"
    files = pipe.metadata["parameter_registry"]["parameter_files"]
    assert files == ["/infa/params/daily.prm"]
    summary = pipe.metadata["parameter_registry"]["summary"]
    assert summary["stateful_variable"] == 2


def test_per_target_strategies(pipe):
    reg = _reg(pipe)
    load = reg["$$LOAD_DATE"]
    assert load["dbt"] == "{{ var('LOAD_DATE', '01/01/2026') }}"
    assert "widget" in load["databricks"]
    assert ":LOAD_DATE" in load["warehouses"]
    runid = reg["$PMWorkflowRunId"]
    assert runid["dbt"] == "{{ invocation_id }}"
    assert "job.run_id" in runid["databricks"]
    stateful = reg["$$LAST_RUN_DATE"]
    assert "NOT a static var" in stateful["dbt"]
    assert "taskValues" in stateful["databricks"]
    assert "watermark" in stateful["warehouses"]


# ---------------------------------------------------------------------------
# the guardrail: stateful is never silent
# ---------------------------------------------------------------------------

def test_stateful_variables_raise_manual_review(pipe):
    m = pipe.mapping("sales")
    issue = next(i for i in m.issues if i.code == "STATEFUL_VARIABLE")
    assert issue.severity == IssueSeverity.MANUAL
    assert "$$LAST_RUN_DATE" in issue.message
    assert "NOT silently converted" in issue.message
    assert "COALESCE(MAX(" in (issue.suggestion or "")
    wf_issue = next(i for i in pipe.issues
                    if i.code == "STATEFUL_VARIABLE")
    assert "$$WF_LAST_FILE" in wf_issue.message


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------

def _convert(tmp_path, target, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "p.xml"
    f.write_text(_xml())
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"),
            source_format="powercenter", target_format=target)
    return tmp_path / "out"


def test_dbt_substitution_by_classification(tmp_path, monkeypatch):
    out = _convert(tmp_path, "dbt", monkeypatch)
    sql = next((out / "dbt").rglob("models/**/int_sales.sql")).read_text()
    # stateful: substituted BUT loudly marked — never a silent static var
    assert "{{ var('LAST_RUN_DATE') }} /* STATEFUL" in sql
    # system variable -> dbt run context
    assert "{{ invocation_id }}" in sql
    assert "$PMWorkflowRunId" not in sql


def test_warehouse_binds_system_variables(tmp_path, monkeypatch):
    out = _convert(tmp_path, "snowflake", monkeypatch)
    sql = next((out / "sql").glob("0*_sales.sql")).read_text()
    assert ":PMWorkflowRunId" in sql
    assert ":LAST_RUN_DATE" in sql
    assert "$PMWorkflowRunId" not in sql
    assert "scheduler run context" in sql.splitlines()[0] \
        or "system variables" in sql.splitlines()[0]
