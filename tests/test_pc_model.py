"""PowerCenter domain model: 20 entities, common contract, field fidelity."""
import json
from dataclasses import fields as dc_fields
from pathlib import Path

import pytest

from metabridge.parsers.pc_model import (
    ALL_ENTITY_TYPES, PCEntity, PCMappingParameter, PCMappingVariable,
    PCPortType, build_pc_model, normalize_port_type,
)
from metabridge.parsers.powercenter_ingest import PowerCenterRepositoryParser

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_XML = EXAMPLES / "powercenter_repo" / "repo_export.xml"

COMMON = ("name", "description", "version", "repository_name",
          "folder_name", "object_type", "attributes", "metadata",
          "source_xml_reference")

FIELD_ATTRS = ("name", "datatype", "precision", "scale", "port_type",
               "expression", "expression_type", "default_value",
               "picture_text")


@pytest.fixture(scope="module")
def model():
    return build_pc_model(str(REPO_XML))


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------

def test_all_twenty_entities_exist_and_share_the_contract():
    assert len(ALL_ENTITY_TYPES) == 20
    for cls in ALL_ENTITY_TYPES:
        assert issubclass(cls, PCEntity), cls
        names = {f.name for f in dc_fields(cls)}
        for key in COMMON:
            assert key in names, (cls.__name__, key)


def test_transform_field_contract():
    from metabridge.parsers.pc_model import PCTransformField
    names = {f.name for f in dc_fields(PCTransformField)}
    for key in FIELD_ATTRS:
        assert key in names, key


def test_port_type_normalization():
    assert normalize_port_type("INPUT") == PCPortType.INPUT
    assert normalize_port_type("OUTPUT") == PCPortType.OUTPUT
    assert normalize_port_type("INPUT/OUTPUT") == PCPortType.INPUT_OUTPUT
    assert normalize_port_type("LOCAL VARIABLE") == PCPortType.VARIABLE
    assert normalize_port_type("VARIABLE") == PCPortType.VARIABLE
    assert normalize_port_type("RETURN") == PCPortType.RETURN
    assert normalize_port_type("RETURN OUTPUT") == PCPortType.RETURN
    assert normalize_port_type("") == PCPortType.INPUT_OUTPUT


# ---------------------------------------------------------------------------
# fidelity from the repository fixture
# ---------------------------------------------------------------------------

def test_repository_and_folders(model):
    assert model.name == "PROD_REPO"
    assert model.repository_version == "188.97"
    assert model.database_type == "Oracle"
    assert [f.name for f in model.folders] == ["sales", "finance"]
    assert model.folder("sales").object_type == "FOLDER"


def test_common_contract_is_filled(model):
    m = next(x for x in model.folder("sales").mappings
             if x.name == "m_load_sales")
    assert m.description == "load fct_sales"
    assert m.version == "1"
    assert m.repository_name == "PROD_REPO"
    assert m.folder_name == "sales"
    assert m.object_type == "MAPPING"
    assert m.attributes["ISVALID"] == "YES"        # raw XML attrs preserved
    assert m.source_xml_reference == \
        "repo_export.xml#FOLDER[sales]/MAPPING[m_load_sales]"


def test_source_and_target_fields(model):
    src = next(s for s in model.folder("sales").sources
               if s.name == "raw_sales")
    assert src.dbd_name == "SRC_DB" and src.owner_name == "STG"
    amount = next(f for f in src.fields if f.name == "amount")
    assert (amount.datatype, amount.precision, amount.scale) == \
        ("decimal", 18, 2)
    tgt = next(t for t in model.folder("sales").targets
               if t.name == "fct_sales")
    pk = next(f for f in tgt.fields if f.key_type == "PRIMARY KEY")
    assert pk.name == "id" and pk.nullable == "NOTNULL"


def test_transform_field_fidelity(model):
    exp = next(t for t in model.folder("sales").transformations
               if t.name == "exp_upper")
    assert exp.reusable is True
    up = next(f for f in exp.fields if f.name == "region_up")
    assert up.expression == "UPPER(region_clean)"
    assert up.port_type == PCPortType.OUTPUT
    clean = next(f for f in exp.fields if f.name == "region_clean")
    assert clean.port_type == PCPortType.INPUT
    agg = next(m for m in model.folder("sales").mappings
               if m.name == "m_agg_sales")
    gb = next(f for t in agg.transformations for f in t.fields
              if f.expression_type == "GROUPBY")
    assert gb.name == "region_up"


def test_variables_and_parameters_split(model):
    m = next(x for x in model.folder("sales").mappings
             if x.name == "m_load_sales")
    (var,) = m.variables
    assert isinstance(var, PCMappingVariable)
    assert var.name == "$$LAST_RUN_TS" and var.aggregation == "MAX"
    (param,) = m.parameters
    assert isinstance(param, PCMappingParameter)
    assert param.name == "$$REGION_FILTER" and param.is_param is True
    assert m.target_load_order == ["TGT_fct_sales"]


def test_mapplet_instances_connectors(model):
    (mp,) = model.folder("sales").mapplets
    assert mp.name == "mplt_clean"
    assert {t.name for t in mp.transformations} == \
        {"INP", "EXP_TRIM", "OUT"}
    assert len(mp.instances) == 3 and len(mp.connectors) == 2
    c = mp.connectors[0]
    assert (c.from_instance, c.from_field, c.to_instance, c.to_field) == \
        ("INP", "region", "EXP_TRIM", "region")


def test_session_and_connection_references(model):
    s = next(x for x in model.folder("sales").sessions
             if x.name == "s_m_load_sales")
    assert s.mapping_name == "m_load_sales" and s.reusable is True
    assert s.session_attributes["Treat source rows as"] == "Update"
    assert s.config_reference == "default_session_config"
    conns = {(c.connection_name, c.extension_name)
             for c in s.connection_references}
    assert conns == {("Oracle_SRC", "Relational Reader"),
                     ("Oracle_DWH", "Relational Writer")}


def test_workflow_worklet_task_links(model):
    (wf,) = model.folder("sales").workflows
    assert wf.is_enabled is True
    assert {t.task_type for t in wf.task_instances} == \
        {"Start", "Session", "Worklet", "Command"}
    link = next(l for l in wf.links if l.condition)
    assert link.from_task == "s_m_load_sales"
    (wl,) = model.folder("sales").worklets
    assert wl.sessions[0].name == "s_m_report_sales"
    (task,) = model.folder("sales").tasks
    assert task.task_type == "Command"
    assert "curl" in task.task_attributes["Command"]


def test_config_captured_in_folder_metadata(model):
    cfg = model.folder("sales").metadata["configs"]
    assert cfg["default_session_config"]["Stop on errors"] == "1"


# ---------------------------------------------------------------------------
# serialization + integration
# ---------------------------------------------------------------------------

def test_full_model_serializes(model):
    doc = model.to_dict()
    text = json.dumps(doc)
    assert "source_xml_reference" in text
    assert doc["folders"][0]["mappings"][0]["transformations"][0][
        "fields"][0]["port_type"] in ("INPUT", "OUTPUT", "INPUT_OUTPUT",
                                      "VARIABLE", "RETURN")


def test_summary_counts(model):
    e = model.summary()["entities"]
    assert e == {"folders": 2, "sources": 5, "targets": 5, "mappings": 5,
                 "mapplets": 1, "reusable_transformations": 1,
                 "workflows": 1, "worklets": 1, "sessions": 2, "tasks": 1,
                 "transform_fields": 35}


def test_model_is_built_before_ir_in_the_same_pass():
    pipeline, m = PowerCenterRepositoryParser().parse_with_model(
        str(REPO_XML))
    assert m.name == "PROD_REPO"
    assert len(pipeline.mappings) == 5
    # the IR pipeline carries the domain-model inventory
    assert pipeline.metadata["pc_model"]["entities"]["mappings"] == 5


def test_namespaced_export(tmp_path):
    xml = REPO_XML.read_text().replace(
        "<POWERMART ",
        '<POWERMART xmlns="http://www.informatica.com/POWERMART" ', 1)
    f = tmp_path / "ns.xml"
    f.write_text(xml)
    m = build_pc_model(str(f))
    assert m.name == "PROD_REPO"
    assert m.summary()["entities"]["mappings"] == 5
