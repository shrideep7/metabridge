"""dbt project generator (module 27): required structure, deterministic
naming (the m_LOAD_CUSTOMER_DIM example), migration_manifest.json."""
import json
import pathlib

import pytest

from metabridge.generators.dbt_naming import (base_name, mart_name,
                                              plan_names, stg_name)
from metabridge.ir.model import Mapping


def _xml() -> str:
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="src_customer" DATABASETYPE="Oracle" DBDNAME="CRM" OWNERNAME="crm" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="dim_customer" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="PRIMARY KEY" NULLABLE="NOTNULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
   </TARGET>
   <MAPPING NAME="m_LOAD_CUSTOMER_DIM" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_c" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="EXP_CLEAN" TYPE="Expression">
     <TRANSFORMFIELD NAME="cust_id" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="cust_name" DATATYPE="string" PRECISION="64" SCALE="0" PORTTYPE="OUTPUT" EXPRESSION="UPPER(LTRIM(RTRIM(cust_name)))"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_c" TYPE="SOURCE" TRANSFORMATION_NAME="src_customer" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_c" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_c" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="EXP_CLEAN" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="EXP_CLEAN" TRANSFORMATION_TYPE="Expression"/>
    <INSTANCE NAME="TGT_d" TYPE="TARGET" TRANSFORMATION_NAME="dim_customer" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SRC_c" TOFIELD="cust_id" TOINSTANCE="SQ_c"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="SRC_c" TOFIELD="cust_name" TOINSTANCE="SQ_c"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="SQ_c" TOFIELD="cust_id" TOINSTANCE="EXP_CLEAN"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="SQ_c" TOFIELD="cust_name" TOINSTANCE="EXP_CLEAN"/>
    <CONNECTOR FROMFIELD="cust_id" FROMINSTANCE="EXP_CLEAN" TOFIELD="cust_id" TOINSTANCE="TGT_d"/>
    <CONNECTOR FROMFIELD="cust_name" FROMINSTANCE="EXP_CLEAN" TOFIELD="cust_name" TOINSTANCE="TGT_d"/>
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>"""


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "p.xml"
    f.write_text(_xml())
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"),
            source_format="powercenter", target_format="dbt")
    return tmp_path / "out" / "dbt"


# ---------------------------------------------------------------------------
# deterministic naming
# ---------------------------------------------------------------------------

def test_base_name_strips_powercenter_noise():
    assert base_name("m_LOAD_CUSTOMER_DIM") == "customer"
    assert base_name("LOAD_CUSTOMER_DIM") == "customer"
    assert base_name("src_customer") == "customer"
    assert base_name("raw_sales") == "sales"
    assert base_name("customer_orders") == "customer_orders"


def test_mart_prefix_by_signal():
    m = Mapping(name="LOAD_CUSTOMER_DIM")
    m.properties["scd1_cir"] = {"type": "SCD_TYPE_1"}
    assert mart_name(m) == "dim_customer"
    f = Mapping(name="LOAD_SALES_FACT")
    assert mart_name(f) == "fct_sales"
    assert stg_name("src_customer") == "stg_customer"


def test_plan_is_deterministic(tmp_path):
    from metabridge.engine import parse_input
    f = tmp_path / "p.xml"
    f.write_text(_xml())
    a = plan_names(parse_input(str(f), "powercenter"))
    b = plan_names(parse_input(str(f), "powercenter"))
    assert a == b


# ---------------------------------------------------------------------------
# the spec's example: m_LOAD_CUSTOMER_DIM -> stg_/int_/dim_customer
# ---------------------------------------------------------------------------

def test_example_decomposition(project):
    stg = (project / "models" / "staging" / "crm"
           / "stg_crm__customer.sql").read_text()
    assert "{{ source('crm', 'src_customer') }}" in stg
    # ONE model per mapping: logic and config together in the mart. The old
    # layout put the logic in an int_ view and left the mart a `select *`
    # shell, which materialised an extra relation for nothing and left every
    # consumer guessing which of the two carried the real column list.
    mart = next(project.glob("models/marts/*/dim_customer.sql")).read_text()
    assert "{{ ref('stg_crm__customer') }}" in mart
    assert "UPPER(LTRIM(RTRIM(cust_name)))" in mart
    assert not list(project.glob("models/**/int_customer.sql"))
    # A mart that just takes the layer's default materialization carries NO
    # config block: dbt_project.yml declares it, and repeating it per model is
    # what made that declaration decorative (model config always wins).
    import yaml
    assert "{{ config(" not in mart
    proj = yaml.safe_load((project / "dbt_project.yml").read_text())
    layers = proj["models"][proj["name"]]
    assert layers["marts"]["+materialized"] == "table"


def test_required_structure(project):
    assert (project / "dbt_project.yml").exists()
    for d in ("models/staging", "models/intermediate", "models/marts",
              "snapshots", "macros", "tests", "seeds", "analyses"):
        assert (project / d).is_dir(), d
    for f in ("packages.yml", "selectors.yml", ".gitignore"):
        assert (project / f).exists(), f
    # sources and properties live in the folder they describe
    assert (project / "models" / "staging" / "crm"
            / "_crm__sources.yml").exists()
    assert (project / "models" / "staging" / "crm" / "_crm__docs.md").exists()
    assert not (project / "models" / "schema.yml").exists()
    assert not (project / "models" / "staging" / "sources.yml").exists()


def test_empty_layers_survive_a_commit(project):
    """git does not track an empty directory, so a layer that came out empty
    would vanish on clone and the project's shape would differ per machine."""
    for d in ("models/intermediate", "seeds", "analyses", "snapshots"):
        assert list((project / d).iterdir()), d


def test_declared_packages_do_not_block_a_parse(project):
    """dbt refuses to parse a project whose packages.yml names a package that
    is not installed, so a generated project must not declare one it never
    uses — an air-gapped install has no way to run `dbt deps`."""
    import yaml
    doc = yaml.safe_load((project / "packages.yml").read_text())
    assert not (doc or {}).get("packages")


def test_properties_document_and_link_back(project):
    import yaml
    mart = yaml.safe_load(
        next(project.glob("models/marts/*/_*__models.yml")).read_text())
    by_name = {m["name"]: m for m in mart["models"]}
    # under config:, not a top-level meta: property (dbt deprecated that)
    assert "m_LOAD_CUSTOMER_DIM" in \
        by_name["dim_customer"]["config"]["meta"]["powercenter_mapping"]
    stg = yaml.safe_load((project / "models" / "staging" / "crm"
                          / "_crm__models.yml").read_text())
    assert "description" in stg["models"][0]


# ---------------------------------------------------------------------------
# migration_manifest.json: PC object -> CIR object -> dbt object
# ---------------------------------------------------------------------------

def test_migration_manifest(project):
    doc = json.loads((project / "migration_manifest.json").read_text())
    assert doc["layout"] == "standard"
    objs = {o["powercenter_object"]: o for o in doc["objects"]}
    m = objs["m_LOAD_CUSTOMER_DIM"]
    assert m["cir_object"] == "LOAD_CUSTOMER_DIM"
    assert m["cir_type"] == "Mapping"
    names = {o["name"]: o for o in m["dbt_objects"]}
    assert names["dim_customer"]["role"] == "mart"
    assert names["dim_customer"]["path"].startswith("models/marts/")
    assert names["dim_customer"]["path"].endswith("/dim_customer.sql")
    src = objs["src_customer"]
    assert src["dbt_objects"][0]["name"] == "stg_crm__customer"


# ---------------------------------------------------------------------------
# ref integrity: every ref()/source() a model emits must resolve to something
# that was actually written. dbt refuses to parse the WHOLE project over one
# dangling ref, so these are ERRORs at generate time, not warnings.
# ---------------------------------------------------------------------------

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"

_BROKEN = ("DBT_REF_DANGLING", "DBT_SOURCE_UNDECLARED")


def _tx(name, ttype, cols, **props):
    from metabridge.ir.model import Port, Transformation
    return Transformation(name=name, type=ttype,
                          ports=[Port(name=c) for c in cols],
                          properties=props)


def _generate(pipeline, tmp_path, sub="dbt"):
    from metabridge.generators.dbt_generator import generate_dbt_project
    out = tmp_path / sub
    generate_dbt_project(pipeline, str(out))
    return out, [i for i in pipeline.all_issues() if i.code in _BROKEN]


def test_no_dangling_refs_in_a_real_project(tmp_path):
    from metabridge.engine import parse_input
    pipeline = parse_input(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
        "powercenter")
    _out, broken = _generate(pipeline, tmp_path)
    assert not broken, [i.message for i in broken]
    graph = pipeline.metadata["dbt_graph"]
    assert graph["nodes"], "the emitted ref graph was not published"
    names = {n["name"] for n in graph["nodes"]}
    for e in graph["edges"]:
        assert e["from"] in names and e["to"] in names, e


def test_snapshot_is_refable_by_its_downstream(tmp_path):
    """An SCD2 mapping emits a {% snapshot %} and NO model.

    Anything reading its target table has to ref the snapshot. Pointing it at
    the int_/dim_ name the plan reserved left every downstream model ref()-ing
    a node that was never written, and `dbt parse` failed on the project.
    """
    from metabridge.ir.model import (Link, LoadStrategy, Mapping, Pipeline,
                                     Port, SourceTable, TransformationType)
    dim = Mapping(name="m_load_customer_dim",
                  load_strategy=LoadStrategy.SCD2, unique_key=["cust_id"])
    dim.properties["scd"] = {"strategy": "timestamp",
                             "updated_at": "updated_at"}
    dim.transformations = [
        _tx("SRC_C", TransformationType.SOURCE, ["cust_id"],
            table="raw_customers", schema="raw"),
        _tx("SQ_C", TransformationType.SOURCE_QUALIFIER, ["cust_id"]),
        _tx("TGT_D", TransformationType.TARGET, ["cust_id"],
            table="DIM_CUSTOMER")]
    dim.links = [Link("SRC_C", "SQ_C"), Link("SQ_C", "TGT_D")]

    fact = Mapping(name="m_load_sales_fact", depends_on=["m_load_customer_dim"])
    fact.transformations = [
        _tx("SRC_D", TransformationType.SOURCE, ["cust_id"],
            table="DIM_CUSTOMER"),
        _tx("SQ_D", TransformationType.SOURCE_QUALIFIER, ["cust_id"]),
        _tx("TGT_F", TransformationType.TARGET, ["cust_id"],
            table="FCT_SALES")]
    fact.links = [Link("SRC_D", "SQ_D"), Link("SQ_D", "TGT_F")]

    pipeline = Pipeline(
        name="snap", mappings=[dim, fact], source_format="powercenter",
        sources=[SourceTable(name="raw_customers", schema="raw",
                             columns=[Port(name="cust_id")])])
    out, broken = _generate(pipeline, tmp_path)
    assert not broken, [i.message for i in broken]
    assert (out / "snapshots" / "snap_customer.sql").exists()
    downstream = next((out / "models").rglob("*sales*.sql")).read_text()
    assert "{{ ref('snap_customer') }}" in downstream


def test_sql_override_relations_join_the_dag(tmp_path):
    """A Source Qualifier SQL override used to be emitted verbatim.

    The model then read a physical relation dbt does not manage: no dependency
    edge, no lineage edge, and nothing stopping it running before its upstream
    was rebuilt.
    """
    from metabridge.ir.model import (Link, Mapping, Pipeline, Port,
                                     SourceTable, TransformationType)
    build = Mapping(name="m_build_orders")
    build.transformations = [
        _tx("SRC_O", TransformationType.SOURCE, ["order_id", "amount"],
            table="raw_orders", schema="raw"),
        _tx("SQ_O", TransformationType.SOURCE_QUALIFIER,
            ["order_id", "amount"]),
        _tx("TGT_O", TransformationType.TARGET, ["order_id", "amount"],
            table="CUSTOMER_ORDERS")]
    build.links = [Link("SRC_O", "SQ_O"), Link("SQ_O", "TGT_O")]

    rank = Mapping(name="m_rank_orders", depends_on=["m_build_orders"])
    rank.transformations = [
        _tx("SRC_R", TransformationType.SOURCE, ["order_id"],
            table="CUSTOMER_ORDERS"),
        _tx("SQ_OVERRIDE", TransformationType.SOURCE_QUALIFIER, ["order_id"],
            sql_override="select order_id, rank() over (order by amount desc) "
                         "as rnk from CUSTOMER_ORDERS"),
        _tx("TGT_R", TransformationType.TARGET, ["order_id"],
            table="ORDER_RANKS")]
    rank.links = [Link("SRC_R", "SQ_OVERRIDE"), Link("SQ_OVERRIDE", "TGT_R")]

    pipeline = Pipeline(
        name="ovr", mappings=[build, rank], source_format="powercenter",
        sources=[SourceTable(name="raw_orders", schema="raw",
                             columns=[Port(name="order_id"),
                                      Port(name="amount")])])
    out, broken = _generate(pipeline, tmp_path)
    assert not broken, [i.message for i in broken]
    ranked = next((out / "models").rglob("*rank*.sql")).read_text()
    assert "from CUSTOMER_ORDERS" not in ranked
    assert "ref(" in ranked
    assert any(i.code == "SQL_OVERRIDE_REFS_RESOLVED"
               for i in rank.issues)


def test_unmanaged_relation_is_declared_not_hidden(tmp_path):
    """An override reading something we neither generate nor declare as a
    source is a real hole in the DAG. It has to be visible."""
    from metabridge.ir.model import (Link, Mapping, Pipeline,
                                     TransformationType)
    m = Mapping(name="m_external")
    m.transformations = [
        _tx("SQ_OVERRIDE", TransformationType.SOURCE_QUALIFIER, ["id"],
            sql_override="select id from SOME_EXTERNAL_TABLE"),
        _tx("TGT", TransformationType.TARGET, ["id"], table="OUT_TABLE")]
    m.links = [Link("SQ_OVERRIDE", "TGT")]
    pipeline = Pipeline(name="ext", mappings=[m], source_format="powercenter")
    _out, broken = _generate(pipeline, tmp_path)
    assert not broken
    assert any(i.code == "SQL_OVERRIDE_UNMANAGED_RELATION" for i in m.issues)
    graph = pipeline.metadata["dbt_graph"]
    assert graph["unmanaged_relations"] == ["SOME_EXTERNAL_TABLE"]
    # and it reaches the model as a real (dashed) edge, not just a listing
    assert {"from": "SOME_EXTERNAL_TABLE", "to": "fct_out",
            "kind": "unmanaged"} in graph["edges"]


# ---------------------------------------------------------------------------
# every var a model uses is either declared with a value or raised as MANUAL.
# A var declared as null is worse than an undeclared one: var() then renders
# the string "None" straight into the SQL.
# ---------------------------------------------------------------------------

def test_declared_vars_carry_their_default(project):
    import yaml
    proj = yaml.safe_load((project / "dbt_project.yml").read_text())
    for name, value in (proj.get("vars") or {}).items():
        assert value not in (None, ""), \
            "var %r declared with no value renders as 'None' in SQL" % name


def test_var_without_a_default_is_raised_not_guessed(tmp_path, monkeypatch):
    """The stateful watermark case: PowerCenter persisted the value across
    runs, so substituting the variable's declared default would freeze the
    watermark. Undeclared makes dbt fail and name it."""
    from metabridge.engine import convert
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "p.xml"
    f.write_text(_xml_with_stateful_var())
    report = convert(str(f), str(tmp_path / "out"),
                     source_format="powercenter", target_format="dbt")
    # the mapping's own model, not the staging view over its source
    sql = next((tmp_path / "out" / "dbt" / "models" / "marts").rglob(
        "fct_sales.sql")).read_text()
    assert "{{ var('MANUAL_CUTOFF') }}" in sql          # no default injected
    codes = {i["code"] for i in report["project_issues"]}
    assert "DBT_VAR_REQUIRED" in codes


def _xml_with_stateful_var() -> str:
    return _xml().replace(
        '''   <MAPPING NAME="m_LOAD_CUSTOMER_DIM"''',
        '''   <SOURCE NAME="src_sales" DATABASETYPE="Oracle" DBDNAME="CRM" OWNERNAME="crm" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="amount" DATATYPE="integer" PRECISION="10" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="fct_sales" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="amount" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
   </TARGET>
   <MAPPING NAME="m_LOAD_SALES" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_s" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="amount" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="FIL_CUTOFF" TYPE="Filter">
     <TRANSFORMFIELD NAME="amount" DATATYPE="integer" PRECISION="10" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TABLEATTRIBUTE NAME="Filter Condition" VALUE="amount &gt; $$MANUAL_CUTOFF"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_s" TYPE="SOURCE" TRANSFORMATION_NAME="src_sales" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_s" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_s" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="FIL_CUTOFF" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="FIL_CUTOFF" TRANSFORMATION_TYPE="Filter"/>
    <INSTANCE NAME="TGT_s" TYPE="TARGET" TRANSFORMATION_NAME="fct_sales" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRC_s" TOFIELD="amount" TOINSTANCE="SQ_s"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SQ_s" TOFIELD="amount" TOINSTANCE="FIL_CUTOFF"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="FIL_CUTOFF" TOFIELD="amount" TOINSTANCE="TGT_s"/>
    <MAPPINGVARIABLE NAME="$$MANUAL_CUTOFF" DATATYPE="integer" PRECISION="10" SCALE="0" ISPARAM="NO" DESCRIPTION="stateful, no default"/>
   </MAPPING>
   <MAPPING NAME="m_LOAD_CUSTOMER_DIM"''')


# ---------------------------------------------------------------------------
# Layer assignment. The two rules that changed are the two that were putting
# models in the wrong folder, so both are pinned directly.
# ---------------------------------------------------------------------------

def _one_source_mapping(name, target, extra=()):
    """A mapping reading one raw source, with an optional extra node."""
    from metabridge.ir.model import Link, Mapping, TransformationType
    m = Mapping(name=name)
    m.transformations = [
        _tx("SRC", TransformationType.SOURCE, ["id", "amount"],
            table="raw_thing", schema="raw"),
        _tx("SQ", TransformationType.SOURCE_QUALIFIER, ["id", "amount"]),
    ]
    m.links = [Link("SRC", "SQ")]
    prev = "SQ"
    for tname, ttype in extra:
        m.transformations.append(_tx(tname, ttype, ["id", "amount"]))
        m.links.append(Link(prev, tname))
        prev = tname
    m.transformations.append(
        _tx("TGT", TransformationType.TARGET, ["id", "amount"], table=target))
    m.links.append(Link(prev, "TGT"))
    return m


def _pipeline_of(*mappings):
    from metabridge.ir.model import Pipeline, Port, SourceTable
    return Pipeline(
        name="layers", mappings=list(mappings), source_format="powercenter",
        sources=[SourceTable(name="raw_thing", schema="raw", system="crm",
                             columns=[Port(name="id"), Port(name="amount")])])


def test_legacy_mapping_name_does_not_pick_the_layer(tmp_path):
    """A PowerCenter mapping called m_stg_* was named by Informatica.

    Reading the dbt layer off that prefix filed business logic under staging/;
    the TARGET table's name is the estate's own statement of what the artifact
    is, and that is what decides.
    """
    from metabridge.ir.model import TransformationType
    from metabridge.generators.dbt_naming import plan_names
    pipeline = _pipeline_of(
        _one_source_mapping("m_stg_revenue", "fct_revenue",
                            [("AGG", TransformationType.AGGREGATOR)]))
    plan, _stg = plan_names(pipeline)
    assert plan["m_stg_revenue"]["layer"] == "marts"
    assert plan["m_stg_revenue"]["ref"] == "fct_revenue"


def test_simple_dimension_load_is_a_mart_not_staging(tmp_path):
    """One cleansing projection over one raw table still produces a DIMENSION.
    The shape of the logic does not demote it."""
    from metabridge.generators.dbt_naming import plan_names
    plan, _stg = plan_names(_pipeline_of(
        _one_source_mapping("m_load_cust", "dim_customer")))
    assert plan["m_load_cust"]["layer"] == "marts"
    assert plan["m_load_cust"]["ref"] == "dim_customer"


def test_cleanse_only_mapping_is_staging(tmp_path):
    from metabridge.generators.dbt_naming import plan_names
    plan, _stg = plan_names(_pipeline_of(
        _one_source_mapping("m_land_thing", "stg_thing")))
    entry = plan["m_land_thing"]
    assert entry["layer"] == "staging"
    assert entry["ref"] == "stg_crm__thing"
    assert entry["targets"][0]["dir"] == "staging/crm"


def test_work_table_target_is_intermediate(tmp_path):
    from metabridge.ir.model import TransformationType
    from metabridge.generators.dbt_naming import plan_names
    plan, _stg = plan_names(_pipeline_of(
        _one_source_mapping("m_build", "revenue_tmp",
                            [("JNR", TransformationType.JOINER)])))
    entry = plan["m_build"]
    assert entry["layer"] == "intermediate"
    # the verb says what the model DOES; `int_<base>` alone said nothing, so
    # two intermediates over one entity were indistinguishable
    assert entry["ref"] == "int_revenue_joined"


def test_one_model_per_mapping(tmp_path):
    from metabridge.generators.dbt_naming import plan_names
    plan, _stg = plan_names(_pipeline_of(
        _one_source_mapping("m_load_cust", "dim_customer")))
    assert len(plan["m_load_cust"]["targets"]) == 1
    assert plan["m_load_cust"]["mart"] == ""


def test_model_names_are_globally_unique(tmp_path):
    """dbt requires unique node names across the WHOLE project, whatever
    folder the file sits in."""
    from metabridge.engine import parse_input
    pipeline = parse_input(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
        "powercenter")
    from metabridge.generators.dbt_naming import plan_names
    plan, stg = plan_names(pipeline)
    names = [t["name"] for e in plan.values() for t in e["targets"]]
    names += [v["name"] for v in stg.values()]
    assert len(names) == len(set(names)), sorted(names)


def test_a_table_the_project_builds_is_not_declared_a_source(tmp_path):
    """The IR records a downstream mapping's input as a SourceTable whether or
    not another mapping produces it. Declaring those as dbt sources claimed a
    table the project rebuilds every run was raw input, and pointed source()
    at a relation in the wrong database."""
    import yaml
    from metabridge.engine import parse_input
    pipeline = parse_input(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
        "powercenter")
    out, broken = _generate(pipeline, tmp_path)
    assert not broken, [i.message for i in broken]
    declared = set()
    for f in out.rglob("_*__sources.yml"):
        for s in yaml.safe_load(f.read_text())["sources"]:
            declared.update(t["name"].lower() for t in s["tables"])
    from metabridge.generators.dbt_naming import built_tables
    assert not (declared & set(built_tables(pipeline))), declared
    assert declared == {"raw_customers", "raw_orders"}


# ---------------------------------------------------------------------------
# the legacy layout stays available for a project already deployed
# ---------------------------------------------------------------------------

def test_layered_layout_keeps_the_old_shape(tmp_path):
    from metabridge.engine import parse_input
    from metabridge.generators.dbt_generator import generate_dbt_project
    pipeline = parse_input(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
        "powercenter")
    out = tmp_path / "legacy"
    generate_dbt_project(pipeline, str(out), layout="layered")
    assert (out / "models" / "schema.yml").exists()
    assert (out / "models" / "staging" / "sources.yml").exists()
    assert not list(out.glob("models/**/_*__models.yml"))
    # flat folders, and a leaf mapping still decomposed into int_ + thin mart
    assert (out / "models" / "intermediate" / "int_customer_ranking.sql"
            ).exists()
    mart = (out / "models" / "marts" / "fct_customer_ranking.sql").read_text()
    assert "select * from {{ ref('int_customer_ranking') }}" in mart
    assert not broken_refs(pipeline)


def broken_refs(pipeline):
    return [i for i in pipeline.all_issues() if i.code in _BROKEN]


def test_mappings_sharing_a_target_keep_their_own_identity():
    """A target table only names a model while ONE mapping builds it.

    A real estate breaks that constantly: a stored procedure decomposed into
    statements, or an ETL mapping and a procedure that both maintain the same
    table. Naming every one of them after the target left them told apart by a
    meaningless `_2` — and the conversion report quotes the CIR name the parser
    assigned ("the model from prc_load_slv_orders is named
    'slv_orders__prc_load_slv_orders' instead"), so the artifact and the report
    disagreed about which models existed.
    """
    from metabridge.ir.model import (Link, Mapping, Pipeline, Port,
                                     SourceTable, TransformationType)
    from metabridge.generators.dbt_naming import plan_names

    def build(name):
        m = Mapping(name=name)
        m.transformations = [
            _tx("SRC", TransformationType.SOURCE, ["id"], table="orders",
                schema="raw"),
            _tx("SQ", TransformationType.SOURCE_QUALIFIER, ["id"]),
            _tx("TGT", TransformationType.TARGET, ["id"],
                table="SLV_ORDERS")]
        m.links = [Link("SRC", "SQ"), Link("SQ", "TGT")]
        return m

    names = ["SLV_ORDERS", "slv_orders__prc_load_slv_orders",
             "slv_orders__prc_load_slv_orders_2"]
    pipeline = Pipeline(
        name="shared", mappings=[build(n) for n in names],
        source_format="oracle",
        sources=[SourceTable(name="orders", schema="raw", system="oracle",
                             columns=[Port(name="id")])])
    plan, _stg = plan_names(pipeline)
    refs = {n: plan[n]["ref"] for n in names}
    assert len(set(refs.values())) == 3, refs
    # each model carries the CIR name the report quotes, not a bare counter
    for n in names[1:]:
        assert n in refs[n], refs[n]
    assert not any(r.endswith("_2") and "prc" not in r for r in refs.values())


def _schema_mapping(name, src_schema, src_table, tgt_schema, tgt_table):
    from metabridge.ir.model import Link, Mapping, TransformationType
    m = Mapping(name=name)
    m.transformations = [
        _tx("SRC", TransformationType.SOURCE, ["id"], table=src_table,
            schema=src_schema),
        _tx("SQ", TransformationType.SOURCE_QUALIFIER, ["id"]),
        _tx("TGT", TransformationType.TARGET, ["id"], table=tgt_table,
            schema=tgt_schema)]
    m.links = [Link("SRC", "SQ"), Link("SQ", "TGT")]
    return m


def test_crossing_a_schema_is_not_staging():
    """The estate's schemas ARE its layering. A mapping that reads
    RAW_SCHEMA.CUSTOMER and writes SILVER_SCHEMA.FINAL_CUSTOMER is promoting
    data between layers — business logic by definition, however simple the
    SQL. Filing it as staging threw away the one thing the schema names say.
    """
    from metabridge.ir.model import Pipeline, Port, SourceTable
    from metabridge.generators.dbt_naming import classify, built_tables
    m = _schema_mapping("promote", "RAW_SCHEMA", "CUSTOMER",
                        "SILVER_SCHEMA", "FINAL_CUSTOMER")
    pipeline = Pipeline(
        name="layers", mappings=[m], source_format="oracle",
        sources=[SourceTable(name="CUSTOMER", schema="RAW_SCHEMA",
                             system="raw_schema", columns=[Port(name="id")])])
    assert classify(m, pipeline, built_tables(pipeline)) == "marts"


def test_landing_within_one_schema_is_still_staging():
    """The guard only fires when BOTH ends declare a schema and they differ —
    a landing model whose target carries no schema is not crossing anything.
    """
    from metabridge.ir.model import Pipeline, Port, SourceTable
    from metabridge.generators.dbt_naming import classify, built_tables
    same = _schema_mapping("land", "RAW_SCHEMA", "CUSTOMER",
                           "RAW_SCHEMA", "stg_customer")
    none = _schema_mapping("land2", "RAW_SCHEMA", "CUSTOMER", "",
                           "stg_customer2")
    pipeline = Pipeline(
        name="layers", mappings=[same, none], source_format="oracle",
        sources=[SourceTable(name="CUSTOMER", schema="RAW_SCHEMA",
                             system="raw_schema", columns=[Port(name="id")])])
    built = built_tables(pipeline)
    assert classify(same, pipeline, built) == "staging"
    assert classify(none, pipeline, built) == "staging"


def test_a_filter_with_no_readable_condition_stays_valid_sql():
    """`.get("condition", "TRUE")` does not cover a key that EXISTS but is
    empty, so the model came out with a bare `where` and no predicate — a
    syntax error rather than a wrong result."""
    from metabridge.ir.model import (Link, Mapping, Pipeline,
                                     TransformationType)
    from metabridge.generators.dbt_generator import render_model_sql
    m = Mapping(name="m_f")
    m.transformations = [
        _tx("SRC", TransformationType.SOURCE, ["id"], table="t", schema="raw"),
        _tx("SQ", TransformationType.SOURCE_QUALIFIER, ["id"]),
        _tx("FIL", TransformationType.FILTER, ["id"], condition=""),
        _tx("TGT", TransformationType.TARGET, ["id"], table="fct_t")]
    m.links = [Link("SRC", "SQ"), Link("SQ", "FIL"), Link("FIL", "TGT")]
    sql = render_model_sql(m, Pipeline(name="p", mappings=[m]), {})
    assert "where TRUE" in sql
    assert "where \n" not in sql and not sql.rstrip().endswith("where")
    # and the lost predicate is declared, not quietly dropped
    assert any(i.code == "FILTER_CONDITION_UNREADABLE" for i in m.issues)
