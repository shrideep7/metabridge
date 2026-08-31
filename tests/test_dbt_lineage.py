"""Lineage over the GENERATED dbt project.

The graph is built from the ref()/source() calls the generator actually
emitted, not re-derived from the IR. That is the whole point: a lineage
diagram computed a second way drifts from the artifacts it describes, and the
drift is invisible because the picture still looks complete.
"""
import pathlib

import pytest

from metabridge.generators.dbt_generator import generate_dbt_project
from metabridge.ir.model import (Link, LoadStrategy, Mapping, Pipeline, Port,
                                 SourceTable, Transformation,
                                 TransformationType)
from metabridge.report.lineage import build_lineage, dbt_lineage, write_lineage

REPO = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _tx(name, ttype, cols, **props):
    return Transformation(name=name, type=ttype,
                          ports=[Port(name=c) for c in cols], properties=props)


@pytest.fixture()
def retail(tmp_path):
    from metabridge.engine import parse_input
    pipeline = parse_input(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
        "powercenter")
    generate_dbt_project(pipeline, str(tmp_path / "dbt"))
    return pipeline


# ---------------------------------------------------------------------------
# the graph describes what was emitted
# ---------------------------------------------------------------------------

def test_every_edge_connects_two_real_nodes(retail):
    graph = retail.metadata["dbt_graph"]
    names = {n["name"] for n in graph["nodes"]}
    assert graph["edges"]
    for e in graph["edges"]:
        assert e["from"] in names, e
        assert e["to"] in names, e


def test_edges_point_the_way_data_flows(retail):
    """A ref is recorded as 'this model reads that one'; the graph has to
    invert it, or every arrow on the diagram points upstream."""
    graph = retail.metadata["dbt_graph"]
    flow = {(e["from"], e["to"]) for e in graph["edges"]}
    assert ("stg_retail_db__customers", "fct_customer_orders") in flow
    assert ("fct_customer_orders", "fct_customer_ranking") in flow


def test_nodes_carry_their_layer_and_path(retail):
    by_name = {n["name"]: n for n in retail.metadata["dbt_graph"]["nodes"]}
    stg = by_name["stg_retail_db__customers"]
    assert stg["kind"] == "model" and stg["layer"] == "staging"
    assert stg["path"].endswith("stg_retail_db__customers.sql")
    # a staging model has one of two provenances, and the node says which: a
    # MAPPING that cleanses a source (here), or a bare source definition with
    # no mapping over it (test_source_derived_staging_names_its_source)
    assert stg["source_type"] == "mapping"
    assert stg["source_object"] == "m_stg_customers"
    src = by_name["source:retail_db.raw_customers"]
    assert src["kind"] == "source" and src["layer"] == "sources"


def test_terminal_models_are_the_consumption_boundary(retail):
    doc = dbt_lineage(retail)
    # fct_customer_orders is read by the ranking mart, so it is NOT terminal;
    # the other two are where the estate's own consumers attach
    assert doc["terminal_models"] == ["fct_customer_ranking",
                                      "fct_daily_revenue"]


def test_source_derived_staging_names_its_source(tmp_path):
    """Also covers the snapshot node kind."""
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
    pipeline = Pipeline(
        name="snap", mappings=[dim], source_format="powercenter",
        sources=[SourceTable(name="raw_customers", schema="raw", system="crm",
                             columns=[Port(name="cust_id")])])
    generate_dbt_project(pipeline, str(tmp_path / "dbt"))
    graph = pipeline.metadata["dbt_graph"]
    assert graph["counts"]["snapshot"] == 1
    snap = next(n for n in graph["nodes"] if n["kind"] == "snapshot")
    assert snap["name"] == "snap_customer"
    assert snap["layer"] == "snapshots"
    # nothing here stages raw_customers, so its staging model comes from the
    # source definition rather than from a mapping — the other provenance
    stg = next(n for n in graph["nodes"] if n.get("layer") == "staging")
    assert stg["source_type"] == "source_definition"
    assert stg["source_object"] == "raw_customers"


# ---------------------------------------------------------------------------
# a hole in the DAG is drawn as a hole
# ---------------------------------------------------------------------------

@pytest.fixture()
def unmanaged(tmp_path):
    """A model reading a relation the project neither builds nor declares."""
    m = Mapping(name="m_external")
    m.transformations = [
        _tx("SQ_OVERRIDE", TransformationType.SOURCE_QUALIFIER, ["id"],
            sql_override="select id from SOME_EXTERNAL_TABLE"),
        _tx("TGT", TransformationType.TARGET, ["id"], table="fct_out")]
    m.links = [Link("SQ_OVERRIDE", "TGT")]
    pipeline = Pipeline(name="ext", mappings=[m], source_format="powercenter")
    generate_dbt_project(pipeline, str(tmp_path / "dbt"))
    return pipeline


def test_unmanaged_relation_is_a_node_with_an_edge(unmanaged):
    """dbt itself cannot see this: `from SOME_TABLE` is valid SQL, so there is
    no error to report and no node in dbt's own DAG. If our graph also omitted
    it, the diagram would look complete while missing a real dependency."""
    doc = dbt_lineage(unmanaged)
    assert doc["unmanaged_relations"] == ["SOME_EXTERNAL_TABLE"]
    kinds = {n["name"]: n["kind"] for n in doc["nodes"]}
    assert kinds["SOME_EXTERNAL_TABLE"] == "unmanaged"
    # recorded against the MODEL, not the mapping — otherwise the node sits on
    # the graph with nothing pointing into it
    assert {"from": "SOME_EXTERNAL_TABLE", "to": "fct_out",
            "kind": "unmanaged"} in doc["edges"]


def test_unmanaged_is_visually_distinct(unmanaged):
    mermaid = dbt_lineage(unmanaged)["mermaid"]
    assert "SOME_EXTERNAL_TABLE -.-> fct_out" in mermaid
    assert "classDef unmanaged" in mermaid


def test_subgraph_ids_do_not_collide_with_class_names(unmanaged):
    """`subgraph unmanaged[...]` and `classDef unmanaged` in one diagram is a
    name clash mermaid should not have to resolve."""
    mermaid = dbt_lineage(unmanaged)["mermaid"]
    assert "subgraph layer_unmanaged[" in mermaid
    assert "subgraph unmanaged[" not in mermaid


# ---------------------------------------------------------------------------
# the document
# ---------------------------------------------------------------------------

def test_lineage_document_leads_with_the_generated_project(retail, tmp_path):
    doc = build_lineage(retail)
    assert "dbt_project" in doc
    write_lineage(doc, str(tmp_path))
    md = (tmp_path / "lineage.md").read_text(encoding="utf-8")
    assert md.index("## dbt project lineage") < \
        md.index("## Table-level lineage (source estate)")
    assert "```mermaid" in md
    assert "Consumption boundary" in md


def test_no_exposures_are_invented(retail, tmp_path):
    """An exposure is a record about a REAL downstream asset. We know a mart
    is terminal; we do not know who consumes it or who owns it, and emitting
    one exposure per terminal model would fabricate both."""
    project = tmp_path / "dbt"
    generate_dbt_project(retail, str(project))
    assert not list(project.rglob("*exposures*"))
    md_doc = build_lineage(retail)
    write_lineage(md_doc, str(tmp_path))
    md = (tmp_path / "lineage.md").read_text(encoding="utf-8")
    assert "topology, not telemetry" in md


def test_lineage_is_generated_by_default(tmp_path, monkeypatch):
    """It used to be opt-in, so the default output could not answer the
    question a migration is judged on."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    report = convert(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
        str(tmp_path / "out"), source_format="powercenter",
        target_format="dbt")
    assert report.get("lineage_generated") is True
    assert (tmp_path / "out" / "lineage.md").exists()
    assert (tmp_path / "out" / "lineage.json").exists()


def test_lineage_can_still_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    convert(str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
            str(tmp_path / "out"), source_format="powercenter",
            target_format="dbt", options={"generate_lineage": False})
    assert not (tmp_path / "out" / "lineage.md").exists()


def test_diagram_is_written_as_a_standalone_mmd(retail, tmp_path):
    """Mermaid fenced inside markdown only renders where the VIEWER supports
    it. A .mmd file IS the diagram — mermaid.live, the CLI, the VS Code
    extension and GitHub all take it directly."""
    write_lineage(build_lineage(retail), str(tmp_path))
    mmd = (tmp_path / "lineage.mmd").read_text(encoding="utf-8")
    assert mmd.startswith("graph LR\n")
    assert "subgraph layer_staging[" in mmd
    # no markdown fencing: this is not a document, it is the diagram
    assert "```" not in mmd


def test_the_diagram_carries_its_own_provenance(retail, tmp_path):
    """A diagram travels on its own, so what it is — and what it does NOT
    claim — has to travel with it."""
    write_lineage(build_lineage(retail), str(tmp_path))
    mmd = (tmp_path / "lineage.mmd").read_text(encoding="utf-8")
    comments = [l for l in mmd.splitlines() if l.startswith("%%")]
    assert comments, mmd[:200]
    # `%` is Mermaid's comment character, so %-formatting would collapse the
    # marker to a single `%` and the line would stop being a comment
    assert not any(l.startswith("% ") for l in mmd.splitlines())
    body = "\n".join(comments)
    assert "retail_analytics" in body
    assert "Topology, not telemetry" in body


def test_databricks_bundle_no_longer_syncs_a_file_that_never_existed(retail,
                                                                    tmp_path):
    """databricks_bundle copies lineage.json/md/mmd into the bundle; the .mmd
    was listed there but nothing ever wrote one."""
    write_lineage(build_lineage(retail), str(tmp_path))
    for name in ("lineage.json", "lineage.md", "lineage.mmd"):
        assert (tmp_path / name).exists(), name


# ---------------------------------------------------------------------------
# the diagram DRAWN, not the code for one
# ---------------------------------------------------------------------------

def _pdf_text(path):
    """Raw page content of a PDF written with compression disabled."""
    return pathlib.Path(path).read_bytes().decode("latin-1")


@pytest.fixture()
def readable_pdf(monkeypatch):
    """Run the real write path with reportlab's page compression off.

    reportlab Flate-compresses page streams by default, so the drawing
    operators and the labels are invisible in the bytes. Only the compression
    setting differs from a normal run — the document itself is the product's.
    """
    reportlab = pytest.importorskip("reportlab")
    monkeypatch.setattr(reportlab.rl_config, "pageCompression", 0)
    return True


def test_lineage_pdf_draws_the_graph(retail, tmp_path, readable_pdf):
    """Mermaid only becomes a picture in a viewer that renders it. The PDF
    draws the graph with reportlab, so the diagram is IN the document — no
    renderer, no browser, and no Node toolchain on the air-gapped path."""
    write_lineage(build_lineage(retail), str(tmp_path))
    pdf = tmp_path / "lineage.pdf"
    assert pdf.exists() and pdf.stat().st_size > 2000
    body = _pdf_text(pdf)
    assert body.startswith("%PDF")
    # real vector geometry, not just a page of text about a diagram
    import re
    assert len(re.findall(r"\bl\b", body)) > 10, "no lines drawn"
    assert len(re.findall(r"\bc\b", body)) > 4, "no rounded boxes drawn"


def test_lineage_pdf_names_the_layers_and_models(retail, tmp_path,
                                                readable_pdf):
    write_lineage(build_lineage(retail), str(tmp_path))
    body = _pdf_text(tmp_path / "lineage.pdf")
    for want in ("Data lineage", "SOURCES", "STAGING", "MARTS",
                 "stg_retail_db__customers", "fct_customer_orders",
                 "Consumption boundary", "Column lineage"):
        assert want in body, want


def test_lineage_pdf_is_optional(retail, tmp_path, monkeypatch):
    """PDF export lives in the `web` extra. A core install must still get the
    JSON, Markdown and Mermaid rather than fail the whole conversion."""
    import metabridge.report.lineage_pdf as mod
    monkeypatch.setattr(mod, "write_lineage_pdf",
                        lambda *a, **k: (_ for _ in ()).throw(ImportError()))
    write_lineage(build_lineage(retail), str(tmp_path))
    for name in ("lineage.json", "lineage.md", "lineage.mmd"):
        assert (tmp_path / name).exists(), name
