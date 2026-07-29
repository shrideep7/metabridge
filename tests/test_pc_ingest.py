"""PowerCenter XML ingestion engine: streaming, namespaces, repository
grammar (mapplets, worklets, sessions, variables, configs, tasks)."""
import re
import time
from pathlib import Path

import pytest

from metabridge.ir.model import LoadStrategy, TransformationType
from metabridge.parsers.powercenter_ingest import (
    PowerCenterRepositoryParser, PowerCenterXMLReader,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_XML = EXAMPLES / "powercenter_repo" / "repo_export.xml"


@pytest.fixture(scope="module")
def repo():
    return PowerCenterRepositoryParser().parse(str(REPO_XML))


# ---------------------------------------------------------------------------
# repository / folder exports
# ---------------------------------------------------------------------------

def test_repository_metadata(repo):
    assert repo.metadata["repository"]["name"] == "PROD_REPO"
    assert repo.metadata["repository"]["databasetype"] == "Oracle"
    assert repo.metadata["powermart"]["repository_version"] == "188.97"
    assert repo.metadata["folders"] == ["finance", "sales"]


def test_multi_folder_mappings_and_name_collision(repo):
    names = {m.name for m in repo.mappings}
    assert names == {"load_sales", "agg_sales", "report_sales",
                     "customer_enrich", "load_sales__finance"}
    assert any(i.code == "MAPPING_NAME_QUALIFIED" for i in repo.issues)
    fin = repo.mapping("load_sales__finance")
    assert fin.properties["folder"] == "finance"


def test_primary_key_becomes_unique_key(repo):
    assert repo.mapping("load_sales").unique_key == ["id"]


# ---------------------------------------------------------------------------
# mapplet inlining
# ---------------------------------------------------------------------------

def test_mapplet_inlined_with_boundary_nodes(repo):
    m = repo.mapping("load_sales")
    nodes = {t.name for t in m.transformations}
    assert {"MPLT_INP", "MPLT_EXP_TRIM", "MPLT_OUT"} <= nodes
    trim = m.transformation("MPLT_EXP_TRIM")
    assert trim.port("region_clean").expression == "LTRIM(RTRIM(region))"
    assert any(i.code == "MAPPLET_INLINED" for i in m.issues)
    # external connectors rewired to the boundary nodes
    edges = {(l.from_transformation, l.to_transformation) for l in m.links}
    assert ("SQ_raw_sales", "MPLT_INP") in edges
    assert ("MPLT_OUT", "EXP_R") in edges
    assert not any("MPLT" == a or "MPLT" == b for a, b in edges)


def test_passive_bypass_folded_through_mapplet(repo):
    """id/amount bypassed the mapplet — they must ride the inlined chain
    (diamonds around passive branches break row-wise SQL generation)."""
    m = repo.mapping("load_sales")
    for node in ("MPLT_INP", "MPLT_EXP_TRIM", "MPLT_OUT"):
        ports = {p.name for p in m.transformation(node).ports}
        assert {"id", "amount"} <= ports, node
    edges = {(l.from_transformation, l.to_transformation) for l in m.links}
    assert ("SQ_raw_sales", "EXP_R") not in edges     # diamond edge folded


def test_reusable_transformation_resolved(repo):
    m = repo.mapping("load_sales")
    exp = m.transformation("EXP_R")
    assert exp.port("region_up").expression == "UPPER(region_clean)"


def test_missing_mapplet_is_declared_not_silent(tmp_path):
    xml = REPO_XML.read_text()
    xml = re.sub(r"<MAPPLET NAME=\"mplt_clean\".*?</MAPPLET>", "", xml,
                 flags=re.DOTALL)
    f = tmp_path / "no_mapplet.xml"
    f.write_text(xml)
    p = PowerCenterRepositoryParser().parse(str(f))
    m = p.mapping("load_sales")
    assert any(i.code == "MAPPLET_UNRESOLVED" and i.severity.value == "MANUAL"
               for i in m.issues)
    # graph stays intact via placeholder node
    assert m.transformation("MPLT") is not None


# ---------------------------------------------------------------------------
# variables, load order, sessions, workflows
# ---------------------------------------------------------------------------

def test_mapping_variables_captured(repo):
    v = {x["name"]: x for x in
         repo.mapping("load_sales").properties["variables"]}
    assert v["$$LAST_RUN_TS"]["aggregation"] == "MAX"
    assert v["$$LAST_RUN_TS"]["is_param"] is False
    assert v["$$REGION_FILTER"]["is_param"] is True


def test_target_load_order(repo):
    assert repo.mapping("load_sales").properties["target_load_order"] == \
        ["TGT_fct_sales"]


def test_session_semantics_applied(repo):
    assert repo.mapping("load_sales").load_strategy == LoadStrategy.MERGE
    assert repo.mapping("agg_sales").load_strategy == LoadStrategy.FULL
    sess = repo.mapping("load_sales").properties["session"]
    assert sess["config"] == "default_session_config"
    conns = {c["name"] for c in sess["connections"]}
    assert conns == {"Oracle_SRC", "Oracle_DWH"}


def test_workflow_links_become_dependencies(repo):
    assert repo.mapping("agg_sales").depends_on == ["load_sales"]
    # dependency crosses the worklet boundary
    assert "agg_sales" in repo.mapping("report_sales").depends_on


def test_worklet_session_parsed(repo):
    wf = repo.metadata["workflows"][0]
    assert wf["name"] == "wf_sales"
    assert "wklt_post" in wf["worklets"]
    assert any(t["type"] == "Worklet" for t in wf["tasks"])
    assert any(l["condition"].startswith("$s_m_load_sales")
               for l in wf["links"])


def test_command_task_flagged_for_orchestration(repo):
    issue = next(i for i in repo.issues if i.code == "ORCHESTRATION_TASK")
    assert "cmd_notify" in issue.message and "Command" in issue.message
    assert issue.severity.value == "MANUAL"


def test_session_config_captured(repo):
    cfg = repo.metadata["session_configs"]["default_session_config"]
    assert cfg["Stop on errors"] == "1"


# ---------------------------------------------------------------------------
# namespaces + export shapes
# ---------------------------------------------------------------------------

def test_namespaced_export_parses_identically(repo, tmp_path):
    xml = REPO_XML.read_text().replace(
        "<POWERMART ",
        '<POWERMART xmlns="http://www.informatica.com/POWERMART" ', 1)
    f = tmp_path / "namespaced.xml"
    f.write_text(xml)
    p = PowerCenterRepositoryParser().parse(str(f))
    assert {m.name for m in p.mappings} == {m.name for m in repo.mappings}
    assert p.mapping("load_sales").unique_key == ["id"]
    assert p.mapping("load_sales").transformation("MPLT_EXP_TRIM") is not None


def test_single_mapping_export_regression():
    """The pre-existing single-workflow export still parses through the
    streaming engine with identical semantics."""
    p = PowerCenterRepositoryParser().parse(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"))
    names = {m.name for m in p.mappings}
    assert "stg_orders" in names and "customer_ranking" in names
    assert p.mapping("stg_orders").load_strategy == LoadStrategy.MERGE


def test_directory_of_exports(tmp_path):
    (tmp_path / "a.xml").write_text(REPO_XML.read_text())
    p = PowerCenterRepositoryParser().parse(str(tmp_path))
    assert len(p.mappings) == 5


# ---------------------------------------------------------------------------
# streaming / large files
# ---------------------------------------------------------------------------

def _big_export(path: Path, n: int) -> None:
    mapping_tpl = """
      <MAPPING NAME="m_gen_%(i)d" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
        <TRANSFORMATION NAME="SQ_g%(i)d" TYPE="Source Qualifier">
          <TRANSFORMFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
        </TRANSFORMATION>
        <INSTANCE NAME="SRC_g%(i)d" TYPE="SOURCE" TRANSFORMATION_NAME="raw_gen" TRANSFORMATION_TYPE="Source Definition"/>
        <INSTANCE NAME="SQ_g%(i)d" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_g%(i)d" TRANSFORMATION_TYPE="Source Qualifier"/>
        <INSTANCE NAME="TGT_g%(i)d" TYPE="TARGET" TRANSFORMATION_NAME="tgt_gen_%(i)d" TRANSFORMATION_TYPE="Target Definition"/>
        <CONNECTOR FROMFIELD="id" FROMINSTANCE="SRC_g%(i)d" TOFIELD="id" TOINSTANCE="SQ_g%(i)d"/>
        <CONNECTOR FROMFIELD="id" FROMINSTANCE="SQ_g%(i)d" TOFIELD="id" TOINSTANCE="TGT_g%(i)d"/>
      </MAPPING>"""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">',
             '<REPOSITORY NAME="BIG" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">',
             '<FOLDER NAME="bulk" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">',
             '<SOURCE NAME="raw_gen" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">'
             '<SOURCEFIELD NAME="id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/></SOURCE>']
    parts += [mapping_tpl % {"i": i} for i in range(n)]
    parts += ["</FOLDER></REPOSITORY></POWERMART>"]
    path.write_text("\n".join(parts))


def test_large_export_streams(tmp_path):
    f = tmp_path / "big.xml"
    _big_export(f, 600)
    t0 = time.time()
    p = PowerCenterRepositoryParser().parse(str(f))
    took = time.time() - t0
    assert len(p.mappings) == 600
    assert took < 20, "streaming parse too slow: %.1fs" % took


def test_reader_yields_incrementally_and_clears(tmp_path):
    f = tmp_path / "big.xml"
    _big_export(f, 50)
    reader = PowerCenterXMLReader(str(f))
    seen = 0
    for folder, tag, elem in reader.stream():
        if tag == "MAPPING":
            seen += 1
            assert elem.find("INSTANCE") is not None   # subtree intact
    assert seen == 50
    assert reader.folders[0]["name"] == "bulk"


def test_end_to_end_conversion_of_repository_export(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    report = convert(str(REPO_XML), str(tmp_path / "out"),
                     source_format="powercenter", target_format="dbt")
    co = report["conversion_output"]
    assert co["errors"]["count"] == 0
    assert set(co["converted_assets"]) == {"agg_sales", "load_sales",
                                           "load_sales__finance",
                                           "report_sales",
                                           "customer_enrich"}
    sql = next((tmp_path / "out" / "dbt").rglob("int_sales.sql")).read_text()
    assert "LTRIM(RTRIM(region))" in sql        # mapplet logic survived
    assert "incremental" in sql                 # session semantics survived
