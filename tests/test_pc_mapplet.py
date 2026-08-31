"""Mapplet handler: reusable CIR component, reuse tracking, shared
artifacts (dbt macro / warehouse template), no duplicated logic."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input
from metabridge.parsers.pc_mapplet import (
    compose_mapplet_outputs, mapplet_component, render_dbt_macro,
)
from metabridge.parsers.pc_model import build_pc_model
from conftest import model_sql

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_XML = EXAMPLES / "powercenter_repo" / "repo_export.xml"


def _two_user_xml() -> str:
    """One mapplet (clean_name) used by TWO mappings without bypasses."""
    mapping_tpl = """
   <MAPPING NAME="m_use%(i)d" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_%(i)d" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="raw_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_%(i)d" TYPE="SOURCE" TRANSFORMATION_NAME="people%(i)d" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_%(i)d" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_%(i)d" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="MPI_%(i)d" TYPE="MAPPLET" TRANSFORMATION_NAME="mplt_name" TRANSFORMATION_TYPE="Mapplet"/>
    <INSTANCE NAME="TGT_%(i)d" TYPE="TARGET" TRANSFORMATION_NAME="clean%(i)d" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="raw_name" FROMINSTANCE="SRC_%(i)d" TOFIELD="raw_name" TOINSTANCE="SQ_%(i)d"/>
    <CONNECTOR FROMFIELD="raw_name" FROMINSTANCE="SQ_%(i)d" TOFIELD="raw_name" TOINSTANCE="MPI_%(i)d"/>
    <CONNECTOR FROMFIELD="clean_name" FROMINSTANCE="MPI_%(i)d" TOFIELD="clean_name" TOINSTANCE="TGT_%(i)d"/>
   </MAPPING>"""
    sources = "".join("""
   <SOURCE NAME="people%d" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="raw_name" DATATYPE="string" PRECISION="64" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="clean%d" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="clean_name" DATATYPE="string" PRECISION="64" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
   </TARGET>""" % (i, i) for i in (1, 2))
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   %(sources)s
   <MAPPLET NAME="mplt_name" OBJECTVERSION="1" VERSIONNUMBER="1" DESCRIPTION="clean names">
    <TRANSFORMATION NAME="INP" TYPE="Input Transformation">
     <TRANSFORMFIELD NAME="raw_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="EXP_C" TYPE="Expression">
     <TRANSFORMFIELD NAME="raw_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT"/>
     <TRANSFORMFIELD NAME="clean_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="OUTPUT" EXPRESSION="INITCAP(LTRIM(RTRIM(raw_name)))"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="OUT" TYPE="Output Transformation">
     <TRANSFORMFIELD NAME="clean_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT"/>
    </TRANSFORMATION>
    <INSTANCE NAME="INP" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="INP" TRANSFORMATION_TYPE="Input Transformation"/>
    <INSTANCE NAME="EXP_C" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="EXP_C" TRANSFORMATION_TYPE="Expression"/>
    <INSTANCE NAME="OUT" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="OUT" TRANSFORMATION_TYPE="Output Transformation"/>
    <CONNECTOR FROMFIELD="raw_name" FROMINSTANCE="INP" TOFIELD="raw_name" TOINSTANCE="EXP_C"/>
    <CONNECTOR FROMFIELD="clean_name" FROMINSTANCE="EXP_C" TOFIELD="clean_name" TOINSTANCE="OUT"/>
   </MAPPLET>
   %(m1)s
   %(m2)s
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"sources": sources,
                   "m1": mapping_tpl % {"i": 1},
                   "m2": mapping_tpl % {"i": 2}}


# ---------------------------------------------------------------------------
# reusable CIR component
# ---------------------------------------------------------------------------

def test_component_and_composition():
    model = build_pc_model(str(REPO_XML))
    (mp,) = model.folder("sales").mapplets
    comp = mapplet_component(mp)
    assert comp["interface"] == {"inputs": ["region"],
                                 "outputs": ["region_clean"]}
    assert comp["composable"] is True
    assert comp["output_expressions"]["region_clean"] == \
        "LTRIM(RTRIM(region))"
    assert len(comp["nodes"]) == 3 and len(comp["edges"]) == 2


def test_macro_rendering():
    model = build_pc_model(str(REPO_XML))
    comp = mapplet_component(model.folder("sales").mapplets[0])
    macro = render_dbt_macro(comp)
    assert macro.startswith("{% macro mapplet_mplt_clean(relation) %}")
    assert "LTRIM(RTRIM(region)) as region_clean" in macro
    assert "from {{ relation }}" in macro


def test_active_mapplet_not_composable():
    from metabridge.parsers.pc_model import (
        PCInstance, PCMapplet, PCTransformation,
    )
    mp = PCMapplet(name="mp_agg")
    mp.transformations = [PCTransformation(
        name="AGG", transformation_type="Aggregator")]
    mp.instances = [PCInstance(name="AGG", transformation_name="AGG",
                               transformation_type="Aggregator")]
    assert compose_mapplet_outputs(mp) is None


# ---------------------------------------------------------------------------
# reuse relationships
# ---------------------------------------------------------------------------

def test_reuse_tracked_and_shared(tmp_path):
    f = tmp_path / "m.xml"
    f.write_text(_two_user_xml())
    p = parse_input(str(f), "powercenter")
    reuse = p.metadata["mapplet_reuse"]["mplt_name"]
    assert sorted(reuse["used_by"]) == ["use1", "use2"]
    assert reuse["shared_artifact"] is True
    assert any(i.code == "MAPPLET_REUSED" and "2 mappings" in i.message
               for i in p.issues)
    # inlined nodes carry their origin
    node = p.mapping("use1").transformation("MPI_1_EXP_C")
    assert node.properties["from_mapplet"] == "mplt_name"


def test_single_use_is_not_a_shared_artifact():
    p = parse_input(str(REPO_XML), "powercenter")
    reuse = p.metadata["mapplet_reuse"]["mplt_clean"]
    assert len(reuse["used_by"]) == 1
    assert reuse["shared_artifact"] is False
    assert not any(i.code == "MAPPLET_REUSED" for i in p.issues)


# ---------------------------------------------------------------------------
# generation: logic emitted once, consumers reference it
# ---------------------------------------------------------------------------

def test_dbt_macro_written_once_and_called_by_models(tmp_path,
                                                     monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "m.xml"
    f.write_text(_two_user_xml())
    from metabridge.engine import convert
    rep = convert(str(f), str(tmp_path / "out"),
                  source_format="powercenter", target_format="dbt")
    macro = (tmp_path / "out" / "dbt" / "macros" /
             "mapplet_mplt_name.sql").read_text()
    assert "INITCAP" in macro
    for i in (1, 2):
        model = model_sql(tmp_path / "out" / "dbt", "use%d" % i)
        assert "{{ mapplet_mplt_name('sq_%d') }}" % i in model
        assert "INITCAP" not in model      # logic NOT duplicated
    assert rep["conversion_output"]["errors"]["count"] == 0


def test_warehouse_shared_template(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "m.xml"
    f.write_text(_two_user_xml())
    from metabridge.engine import convert
    rep = convert(str(f), str(tmp_path / "out"),
                  source_format="powercenter", target_format="databricks")
    tmpl = (tmp_path / "out" / "sql" / "shared" /
            "mapplet_mplt_name_template.sql").read_text()
    assert "INITCAP" in tmpl
    assert "Python/shared function" in tmpl
    # deploy_all does not include the unbound template
    deploy = (tmp_path / "out" / "sql" / "deploy_all.sql").read_text()
    assert "input_relation" not in deploy
    assert rep["conversion_output"]["errors"]["count"] == 0


def test_bypass_folded_instance_keeps_inline(tmp_path, monkeypatch):
    """m_load_sales's mapplet gained folded pass-through columns — the
    shared macro would drop them, so that instance stays inline even if
    the mapplet were reused."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    convert(str(REPO_XML), str(tmp_path / "out"),
            source_format="powercenter", target_format="dbt")
    model = model_sql(tmp_path / "out" / "dbt", "load_sales")
    assert "LTRIM(RTRIM(region))" in model     # inline logic preserved
    assert "mapplet_mplt_clean" not in model
