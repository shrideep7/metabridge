"""Target columns keep the type the source declares, end to end.

A PowerCenter TARGET declares its columns with real types, but the TARGET
node's ports are built from the CONNECTOR list, which carries names and
nothing else. Every target column therefore arrived as an untyped `string`
that was indistinguishable from a real one — so a decimal(28,0) and a
date/time were both documented as `varchar`, with the true type sitting
unread in the export the whole time.
"""
import pathlib

import pytest
import yaml

from metabridge.engine import parse_input
from metabridge.generators.dbt_generator import generate_dbt_project
from metabridge.ir.model import (Link, Mapping, Pipeline, Port, Transformation,
                                 TransformationType)

REPO = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
RETAIL = EXAMPLES / "powercenter" / "wf_retail_analytics.xml"


def _target_ports(pipeline, mapping_name):
    m = pipeline.mapping(mapping_name)
    return {p.name: p for p in m.by_type(TransformationType.TARGET)[0].ports}


# ---------------------------------------------------------------------------
# the parser reads what the export declares
# ---------------------------------------------------------------------------

def test_target_ports_carry_the_declared_type():
    ports = _target_ports(parse_input(str(RETAIL), "powercenter"),
                          "customer_orders")
    assert ports["customer_id"].datatype == "integer"
    assert ports["last_order_date"].datatype == "timestamp"
    lifetime = ports["lifetime_value"]
    assert (lifetime.datatype, lifetime.precision, lifetime.scale) == \
        ("decimal", 28, 0)
    # and the precision on a text column survives, so it is not widened
    assert ports["customer_name"].precision == 255


def test_no_target_column_is_silently_string():
    """The failure this guards was not a wrong type here and there — it was
    EVERY target column arriving as `string`."""
    pipeline = parse_input(str(RETAIL), "powercenter")
    seen = set()
    for m in pipeline.mappings:
        for t in m.by_type(TransformationType.TARGET):
            seen.update(p.datatype for p in t.ports)
    assert seen - {"string"}, seen


def test_the_repository_ingest_path_agrees():
    """Two PowerCenter readers converge on one mapping parser; both had to
    start collecting declared PORTS rather than declared names."""
    pipeline = parse_input(
        str(EXAMPLES / "powercenter_repo" / "repo_export.xml"), "powercenter")
    typed = [p for m in pipeline.mappings
             for t in m.by_type(TransformationType.TARGET)
             for p in t.ports if p.datatype != "string"]
    assert typed, "the repository ingest path still drops target types"


def test_scd_still_sees_the_full_declared_shape():
    """`declared_columns` feeds SCD pattern detection and must stay a list of
    NAMES — including columns that are declared but never connected."""
    pipeline = parse_input(str(RETAIL), "powercenter")
    for m in pipeline.mappings:
        for t in m.by_type(TransformationType.TARGET):
            declared = t.properties.get("declared_columns")
            if declared:
                assert all(isinstance(c, str) for c in declared), declared


# ---------------------------------------------------------------------------
# what reaches the generated project
# ---------------------------------------------------------------------------

@pytest.fixture()
def retail_props(tmp_path):
    pipeline = parse_input(str(RETAIL), "powercenter")
    out = tmp_path / "dbt"
    generate_dbt_project(pipeline, str(out))
    doc = yaml.safe_load(
        next(out.rglob("models/marts/**/_*__models.yml")).read_text())
    return {m["name"]: m for m in doc["models"]}, pipeline


def test_property_files_document_the_real_types(retail_props):
    models, _pipeline = retail_props
    cols = {c["name"]: c.get("data_type")
            for c in models["fct_customer_orders"]["columns"]}
    assert cols == {"customer_id": "integer",
                    "customer_name": "varchar(255)",
                    "email": "varchar(255)",
                    "status_desc": "varchar(255)",
                    "order_count": "integer",
                    "lifetime_value": "decimal(28,0)",
                    "last_order_date": "timestamp"}


def test_nothing_is_documented_as_a_bare_varchar(retail_props):
    """`varchar` with no length was the tell-tale of the old fallback: it is
    what every unknown type collapsed to."""
    models, _pipeline = retail_props
    for model in models.values():
        for col in model.get("columns", []):
            assert col.get("data_type") != "varchar", (model["name"], col)


# ---------------------------------------------------------------------------
# an unknown type is admitted, not guessed
# ---------------------------------------------------------------------------

def _mixed_pipeline():
    m = Mapping(name="m_mixed")
    m.transformations = [
        Transformation(name="SRC", type=TransformationType.SOURCE,
                       ports=[Port(name="a")],
                       properties={"table": "raw_t", "schema": "raw"}),
        Transformation(name="SQ", type=TransformationType.SOURCE_QUALIFIER,
                       ports=[Port(name="a")]),
        Transformation(name="TGT", type=TransformationType.TARGET,
                       ports=[Port(name="a", datatype="integer", precision=10),
                              Port(name="b", type_declared=False)],
                       properties={"table": "fct_mixed"})]
    m.links = [Link("SRC", "SQ"), Link("SQ", "TGT")]
    return Pipeline(name="mixed", mappings=[m], source_format="powercenter")


def test_an_undeclared_type_omits_data_type_rather_than_guessing(tmp_path):
    """dbt's `data_type` is optional. Omitting it says "we were not told";
    the old `varchar` fallback asserted "this column is text" about a column
    nobody had ever typed."""
    pipeline = _mixed_pipeline()
    out = tmp_path / "dbt"
    generate_dbt_project(pipeline, str(out))
    doc = yaml.safe_load(
        next(out.rglob("models/marts/**/_*__models.yml")).read_text())
    cols = {c["name"]: c for c in doc["models"][0]["columns"]}
    assert cols["a"]["data_type"] == "integer"
    assert "data_type" not in cols["b"]


def test_an_undeclared_type_is_reported(tmp_path):
    pipeline = _mixed_pipeline()
    generate_dbt_project(pipeline, str(tmp_path / "dbt"))
    issue = next(i for i in pipeline.all_issues()
                 if i.code == "TYPE_UNDECLARED")
    assert "b" in issue.message
    assert issue.severity.value == "WARNING"


def test_a_connected_column_the_target_never_declares_is_untyped(tmp_path):
    """A CONNECTOR can name a column the TARGET does not declare. We know the
    name and nothing else, and must not imply otherwise."""
    from metabridge.parsers.powercenter_parser import _target_port
    assert _target_port("x", None).type_declared is False
    declared = Port(name="x", datatype="decimal", precision=12, scale=2)
    carried = _target_port("x", declared)
    assert (carried.datatype, carried.precision, carried.scale) == \
        ("decimal", 12, 2)
    assert carried.type_declared is True
