"""Session parser: 13-field runtime CIR, recommendations, and the
invariant that session configuration never becomes transformation logic."""
import pytest

from metabridge.engine import parse_input
from metabridge.parsers.pc_session import apply_session_cir, build_session_cir


def _xml(session_extras=True) -> str:
    extras = """
    <ATTRIBUTE NAME="Parameter Filename" VALUE="/infa/params/s_orders.prm"/>
    <ATTRIBUTE NAME="Commit Interval" VALUE="10000"/>
    <ATTRIBUTE NAME="Stop on errors" VALUE="1"/>
    <ATTRIBUTE NAME="DTM buffer size" VALUE="512MB"/>
    <ATTRIBUTE NAME="Pushdown Optimization" VALUE="Full"/>
    <ATTRIBUTE NAME="Override tracing" VALUE="Verbose Data"/>
    <ATTRIBUTE NAME="Target load type" VALUE="Bulk"/>
    <SESSIONCOMPONENT REFOBJECTNAME="cmd_archive" TYPE="Pre-session command">
     <VALUEPAIR NAME="Command" VALUE="sh /scripts/archive.sh"/>
    </SESSIONCOMPONENT>
    <SESSIONCOMPONENT REFOBJECTNAME="cmd_notify" TYPE="Post-session success command">
     <VALUEPAIR NAME="Command" VALUE="sh /scripts/notify.sh done"/>
    </SESSIONCOMPONENT>
    <SESSIONEXTENSION NAME="Relational Reader" SINSTANCENAME="SQ_o" TYPE="READER" SUBTYPE="Relational Reader">
     <CONNECTIONREFERENCE CONNECTIONNAME="ORA_SRC" CONNECTIONTYPE="Relational" VARIABLE=""/>
     <ATTRIBUTE NAME="Partition Type" VALUE="hash auto-keys"/>
     <PARTITION NAME="Partition #1"/>
     <PARTITION NAME="Partition #2"/>
     <PARTITION NAME="Partition #3"/>
    </SESSIONEXTENSION>
    <SESSIONEXTENSION NAME="Relational Writer" SINSTANCENAME="TGT_o" TYPE="WRITER" SUBTYPE="Relational Writer">
     <CONNECTIONREFERENCE CONNECTIONNAME="DWH_TGT" CONNECTIONTYPE="Relational" VARIABLE=""/>
    </SESSIONEXTENSION>""" if session_extras else ""
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_orders" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="amount" DATATYPE="decimal" PRECISION="12" SCALE="2" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="fct_orders" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="amount" DATATYPE="decimal" PRECISION="12" SCALE="2" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_orders" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_o" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="order_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="12" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_o" TYPE="SOURCE" TRANSFORMATION_NAME="src_orders" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_o" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_o" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="TGT_o" TYPE="TARGET" TRANSFORMATION_NAME="fct_orders" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="SRC_o" TOFIELD="order_id" TOINSTANCE="SQ_o"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRC_o" TOFIELD="amount" TOINSTANCE="SQ_o"/>
    <CONNECTOR FROMFIELD="order_id" FROMINSTANCE="SQ_o" TOFIELD="order_id" TOINSTANCE="TGT_o"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SQ_o" TOFIELD="amount" TOINSTANCE="TGT_o"/>
   </MAPPING>
   <SESSION NAME="s_m_orders" MAPPINGNAME="m_orders" REUSABLE="YES" VERSIONNUMBER="1" ISVALID="YES">%(extras)s
   </SESSION>
   <WORKFLOW NAME="wf_orders" ISENABLED="YES" VERSIONNUMBER="1">
    <TASKINSTANCE NAME="s_m_orders" TASKNAME="s_m_orders" TASKTYPE="Session"/>
   </WORKFLOW>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"extras": extras}


def _mapping(tmp_path, **kw):
    f = tmp_path / "s.xml"
    f.write_text(_xml(**kw))
    return parse_input(str(f), "powercenter").mapping("orders")


@pytest.fixture()
def cir(tmp_path):
    return _mapping(tmp_path).properties["session_cir"]


# ---------------------------------------------------------------------------
# extraction: the 13-field contract
# ---------------------------------------------------------------------------

def test_all_thirteen_fields_extracted(cir):
    assert cir["session"] == "s_m_orders"
    assert cir["mapping"] == "orders"                    # mapping reference
    assert cir["source_connections"] == [
        {"instance": "SQ_o", "name": "ORA_SRC", "type": "Relational"}]
    assert cir["target_connections"] == [
        {"instance": "TGT_o", "name": "DWH_TGT", "type": "Relational"}]
    assert cir["parameter_file"] == "/infa/params/s_orders.prm"
    assert cir["commit_interval"] == 10000
    assert cir["error_threshold"] == 1
    assert cir["dtm_buffer_size"] == "512MB"
    assert cir["partitioning"] == {"partition_count": 3,
                                   "types": ["hash auto-keys"]}
    assert cir["pushdown_optimization"] == "full"
    assert cir["tracing_level"] == "Verbose Data"
    assert cir["pre_session_commands"] == ["sh /scripts/archive.sh"]
    assert cir["post_session_commands"] == ["sh /scripts/notify.sh done"]
    assert cir["target_load_type"] == "Bulk"


def test_plain_session_yields_empty_contract(tmp_path):
    cir = _mapping(tmp_path, session_extras=False).properties["session_cir"]
    assert cir["pushdown_optimization"] is None
    assert cir["partitioning"] is None
    assert cir["commit_interval"] is None
    assert cir["pre_session_commands"] == []
    assert cir["target_load_type"] is None


# ---------------------------------------------------------------------------
# migration recommendations
# ---------------------------------------------------------------------------

def test_recommendations_generated(tmp_path):
    m = _mapping(tmp_path)
    codes = {i.code for i in m.issues}
    assert {"SESSION_PUSHDOWN", "SESSION_PARTITIONING",
            "SESSION_COMMIT_INTERVAL", "SESSION_ERROR_THRESHOLD",
            "SESSION_DTM_BUFFER", "SESSION_TRACING",
            "SESSION_SHELL_COMMAND", "SESSION_BULK_LOAD",
            "SESSION_PARAMETER_FILE", "SESSION_CONNECTIONS"} <= codes


def test_pushdown_recommendation_names_native_execution(tmp_path):
    m = _mapping(tmp_path)
    push = next(i for i in m.issues if i.code == "SESSION_PUSHDOWN")
    assert "natively" in push.message
    assert "Spark SQL" in (push.suggestion or "")     # Databricks example
    assert "Snowflake" in (push.suggestion or "")     # ...but all targets


def test_shell_commands_are_manual_and_preserved(tmp_path):
    from metabridge.ir.model import IssueSeverity
    m = _mapping(tmp_path)
    cmds = [i for i in m.issues if i.code == "SESSION_SHELL_COMMAND"]
    assert len(cmds) == 2
    assert all(i.severity == IssueSeverity.MANUAL for i in cmds)
    assert any("sh /scripts/archive.sh" in i.message for i in cmds)
    assert any("sh /scripts/notify.sh done" in i.message for i in cmds)


def test_silent_error_tolerance_flagged(tmp_path):
    session = {"name": "s", "mapping": "m", "attributes":
               {"Stop on errors": "0"}, "extensions": [], "components": []}
    from metabridge.ir.model import Mapping
    m = Mapping(name="m")
    apply_session_cir(m, build_session_cir(session))
    thr = next(i for i in m.issues if i.code == "SESSION_ERROR_THRESHOLD")
    assert "SILENTLY" in thr.message


# ---------------------------------------------------------------------------
# the invariant: runtime configuration is NOT transformation logic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["dbt", "snowflake"])
def test_session_config_never_changes_generated_sql(tmp_path, monkeypatch,
                                                    target):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    outs = []
    for tag, extras in (("a", True), ("b", False)):
        f = tmp_path / ("%s.xml" % tag)
        f.write_text(_xml(session_extras=extras))
        out = tmp_path / ("out_%s" % tag)
        convert(str(f), str(out), source_format="powercenter",
                target_format=target)
        sub = "dbt" if target == "dbt" else "sql"
        outs.append({p.name: p.read_text()
                     for p in (out / sub).rglob("*.sql")
                     if "orders" in p.name})
    assert outs[0] == outs[1]     # tuning knobs must not touch the SQL
