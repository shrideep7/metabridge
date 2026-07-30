"""Mapping graph builder: nodes/edges, navigation, lineage, diagnostics."""
import json
from pathlib import Path

import pytest

from metabridge.parsers.pc_graph import (
    MappingEdge, MappingGraph, MappingNode, build_mapping_graph,
    build_mapping_graphs,
)
from metabridge.parsers.pc_model import (
    PCConnector, PCInstance, PCMapping, build_pc_model,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_XML = EXAMPLES / "powercenter_repo" / "repo_export.xml"


@pytest.fixture(scope="module")
def graphs():
    return build_mapping_graphs(build_pc_model(str(REPO_XML)))


@pytest.fixture(scope="module")
def g(graphs):
    return graphs["sales/m_load_sales"]


# ---------------------------------------------------------------------------
# nodes + edges
# ---------------------------------------------------------------------------

def test_node_types(g):
    kinds = {n.name: n.node_type for n in g.nodes.values()}
    assert kinds == {"SRC_raw_sales": "SOURCE",
                     "SQ_raw_sales": "TRANSFORMATION",
                     "MPLT": "MAPPLET",
                     "EXP_R": "TRANSFORMATION",
                     "TGT_fct_sales": "TARGET"}


def test_edges_preserve_all_six_attributes(g):
    e = next(x for x in g.edges if x.to_instance == "MPLT")
    assert e.from_instance == "SQ_raw_sales"
    assert e.from_field == "region"
    assert e.to_instance == "MPLT"
    assert e.to_field == "region"
    assert e.from_instance_type == "Source Qualifier"
    assert e.to_instance_type == "Mapplet"


def test_explicit_connector_instance_types_win():
    """The pre-existing export carries FROMINSTANCETYPE attributes — they
    are preserved verbatim, not re-derived."""
    graphs = build_mapping_graphs(build_pc_model(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml")))
    g2 = next(iter(graphs.values()))
    assert any(e.from_instance_type == "Source Definition"
               for e in g2.edges)


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------

def test_upstream_downstream(g):
    assert {n.name for n in g.get_upstream_nodes("EXP_R")} == \
        {"SQ_raw_sales", "MPLT"}
    assert {n.name for n in g.get_downstream_nodes("SQ_raw_sales")} == \
        {"MPLT", "EXP_R"}
    assert g.get_upstream_nodes("SRC_raw_sales") == []


def test_source_target_getters(g):
    assert [n.name for n in g.get_source_nodes()] == ["SRC_raw_sales"]
    assert [n.name for n in g.get_target_nodes()] == ["TGT_fct_sales"]


def test_topological_order(g):
    order = g.get_topological_order()
    assert order.index("SRC_raw_sales") < order.index("SQ_raw_sales")
    assert order.index("SQ_raw_sales") < order.index("MPLT")
    assert order.index("MPLT") < order.index("EXP_R")
    assert order.index("EXP_R") < order.index("TGT_fct_sales")


# ---------------------------------------------------------------------------
# column lineage
# ---------------------------------------------------------------------------

def test_trace_target_column_origin_through_mapplet(g):
    (o,) = g.trace_target_column_origin("TGT_fct_sales", "region_up")
    assert o["source_instance"] == "SRC_raw_sales"
    assert o["source_field"] == "region"
    assert o["is_true_source"] is True
    assert o["path"][0] == "SRC_raw_sales.region"
    assert o["path"][-1] == "TGT_fct_sales.region_up"
    assert any(step.startswith("MPLT.") for step in o["path"])


def test_trace_passthrough_column(g):
    (o,) = g.trace_target_column_origin("TGT_fct_sales", "amount")
    assert o["source_field"] == "amount"
    # amount bypasses the mapplet entirely
    assert not any(step.startswith("MPLT.") for step in o["path"])


def test_trace_downstream(g):
    paths = g.trace_column_lineage("SRC_raw_sales", "region",
                                   direction="downstream")
    flat = [step for p in paths for step in p]
    assert "TGT_fct_sales.region_up" in flat


def test_expression_aware_contributors(graphs):
    """SUM(amount) in the aggregator: total_amount originates from amount."""
    ga = graphs["sales/m_agg_sales"]
    (o,) = ga.trace_target_column_origin("TGT_agg_sales", "total_amount")
    assert o["source_field"] == "amount"
    assert o["source_instance"] == "SRC_fct_sales"


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

def test_clean_fixture_validates_ok(graphs):
    for key, graph in graphs.items():
        d = graph.validate()
        assert d["ok"], (key, d["issues"])
        assert d["components"] == 1


def _mini(instances, connectors):
    m = PCMapping(name="broken")
    m.instances = [PCInstance(name=n, instance_type=t,
                              transformation_name=n) for n, t in instances]
    m.connectors = [PCConnector(from_instance=a, from_field=af,
                                to_instance=b, to_field=bf)
                    for a, af, b, bf in connectors]
    return build_mapping_graph(m, folder=None)


def test_orphan_transformation_detected():
    g2 = _mini([("S", "SOURCE"), ("T", "TARGET"),
                ("LONELY", "TRANSFORMATION")],
               [("S", "c", "T", "c")])
    d = g2.validate()
    assert d["issues"]["orphan_transformations"] == ["LONELY"]
    assert not d["ok"]


def test_missing_connectors_detected():
    g2 = _mini([("S", "SOURCE"), ("X", "TRANSFORMATION"),
                ("T", "TARGET")],
               [("S", "c", "X", "c")])       # X never reaches the target
    issues = g2.validate()["issues"]["missing_connectors"]
    assert any("X" in i and "outgoing" in i for i in issues)
    assert any("T" in i and "incoming" in i for i in issues)


def test_duplicate_connectors_detected():
    g2 = _mini([("S", "SOURCE"), ("T", "TARGET")],
               [("S", "c", "T", "c"), ("S", "c", "T", "c")])
    assert g2.validate()["issues"]["duplicate_connectors"] == \
        ["S.c -> T.c"]


def test_cycle_detected():
    g2 = _mini([("A", "TRANSFORMATION"), ("B", "TRANSFORMATION")],
               [("A", "x", "B", "x"), ("B", "x", "A", "x")])
    d = g2.validate()
    assert d["issues"]["cycles"]
    assert "A" in d["issues"]["cycles"][0]
    # topological order still returns every node
    assert set(g2.get_topological_order()) == {"A", "B"}


def test_disconnected_components_detected():
    g2 = _mini([("S1", "SOURCE"), ("T1", "TARGET"),
                ("S2", "SOURCE"), ("T2", "TARGET")],
               [("S1", "a", "T1", "a"), ("S2", "b", "T2", "b")])
    d = g2.validate()
    assert d["components"] == 2
    assert len(d["issues"]["disconnected_components"]) == 2


def test_invalid_fields_detected(graphs):
    from metabridge.parsers.pc_model import build_pc_model as bpm
    model = bpm(str(REPO_XML))
    folder = model.folder("sales")
    m = next(x for x in folder.mappings if x.name == "m_load_sales")
    m.connectors.append(PCConnector(
        from_instance="SRC_raw_sales", from_field="no_such_column",
        to_instance="SQ_raw_sales", to_field="id"))
    g2 = build_mapping_graph(m, folder)
    issues = g2.validate()["issues"]["invalid_fields"]
    assert any("no_such_column" in i for i in issues)


def test_unknown_instance_detected():
    g2 = _mini([("S", "SOURCE"), ("T", "TARGET")],
               [("S", "c", "GHOST", "c"), ("S", "c", "T", "c")])
    issues = g2.validate()["issues"]["invalid_fields"]
    assert any("GHOST" in i for i in issues)


# ---------------------------------------------------------------------------
# serialization + parse integration
# ---------------------------------------------------------------------------

def test_graph_serializes(g):
    doc = g.to_dict()
    json.dumps(doc)
    assert len(doc["nodes"]) == 5
    assert doc["topological_order"][0] == "SRC_raw_sales"
    assert doc["edges"][0]["from_instance_type"]


def test_parse_surfaces_graph_diagnostics(tmp_path):
    """A corrupted export (duplicate connector) shows up as a pipeline
    issue during normal parsing — not just via the standalone tool."""
    xml = REPO_XML.read_text().replace(
        '<CONNECTOR FROMFIELD="gl_id" FROMINSTANCE="SRC_raw_gl" '
        'TOFIELD="gl_id" TOINSTANCE="SQ_raw_gl"/>',
        '<CONNECTOR FROMFIELD="gl_id" FROMINSTANCE="SRC_raw_gl" '
        'TOFIELD="gl_id" TOINSTANCE="SQ_raw_gl"/>' * 2, 1)
    f = tmp_path / "corrupt.xml"
    f.write_text(xml)
    from metabridge.parsers.powercenter_ingest import (
        PowerCenterRepositoryParser,
    )
    pipeline, _ = PowerCenterRepositoryParser().parse_with_model(str(f))
    assert any(i.code == "GRAPH_DIAGNOSTICS" for i in pipeline.issues)
    assert "finance/m_load_sales" in pipeline.metadata["graph_diagnostics"]
